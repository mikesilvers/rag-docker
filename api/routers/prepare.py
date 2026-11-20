"""The preparation area's routes (#638): SPECIFICATIONS.md §3.1.11.

Upload raw documents, prepare them in a background job, preview, download or
delete them, and import prepared documents into a collection through the normal
ingest path. Every route is Developer (auth/policy.py); the acting ones are
audited with names and sizes, never document text.
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from starlette.background import BackgroundTask

from auth import access
from config import settings
from models.schemas import PrepareImportRequest, PrepareJobEntry, PrepareJobList, PrepareStartRequest
from services import audit, ingest_pipeline, job_lists
from services import preparation_jobs as jobs
from services import preparation_store as store
from services.collection_writes import CollectionDeletingError
from utils import api_error, collection_check_unavailable, collection_deleting_error

router = APIRouter(prefix="/prepare")
_log = logging.getLogger(__name__)

_BLOCK = 1024 * 1024


def _error(exc: store.AreaError):
    return api_error(exc.status, exc.code, exc.message, detail=exc.detail)


# ── per-collection access (#606) ─────────────────────────────────────────────
# A document whose imports are all into collections the caller may not use is
# hidden from them and refused as an unknown document (Q18, Q19). One with at
# least one import into a collection they may use stays visible, with the other
# import records left out. Preparation job records leave hidden documents out
# (Q20). Engineers see everything. A document with no imports is shared as before.

def _import_names(record: dict) -> list[str]:
    return [item["collection"] for item in record.get("imports") or []
            if isinstance(item, dict) and isinstance(item.get("collection"), str) and item["collection"]]


def _visible_of(request):
    """The caller's visible-collection predicate; None for an Engineer or a
    direct call, who see everything. Reads the store: call it in a thread."""
    principal = access.principal_of(request)
    if principal is None or principal.role == "engineer":
        return None
    return access.visible_filter(principal)


def _blocker(request):
    """A function giving, for a document record, a collection among its imports
    the caller may not use when every import is into such a collection (the
    document is hidden), else None. None for an Engineer or a direct call.
    Reads the store: call it in a thread."""
    visible = _visible_of(request)
    if visible is None:
        return None

    def blocked(record: dict):
        names = _import_names(record)
        if names and not any(visible(name) for name in names):
            return names[0]
        return None
    return blocked


def _shown(record: dict, visible) -> dict:
    """A visible document as the caller sees it: without the import records
    for collections they may not use."""
    if visible is None or not record.get("imports"):
        return record
    kept = [item for item in record["imports"]
            if not (isinstance(item, dict) and isinstance(item.get("collection"), str)
                    and item["collection"] and not visible(item["collection"]))]
    return {**record, "imports": kept}


def _job_view(job: dict, blocked) -> dict:
    """A preparation job record without the documents hidden from the caller
    (Q20): their ids and names, `current` when it is one of them, and the
    errors that name them. The counters stay; they name no document. A
    document that can't be read (deleted since) isn't hidden."""
    if blocked is None:
        return job
    ids = list(job.get("document_ids") or [])
    names = list(job.get("names") or [])
    hidden = set()
    for document_id in ids:
        try:
            record = store.get(document_id)
        except store.AreaError:
            continue
        if blocked(record) is not None:
            hidden.add(document_id)
    if not hidden:
        return job
    hidden_names = {names[i] for i, document_id in enumerate(ids) if document_id in hidden and i < len(names)}
    view = dict(job)
    view["document_ids"] = [d for d in ids if d not in hidden]
    view["names"] = [n for i, n in enumerate(names) if not (i < len(ids) and ids[i] in hidden)]
    current = job.get("current")
    if isinstance(current, dict) and current.get("document_id") in hidden:
        view["current"] = None
    prefixes = tuple(f"{key}:" for key in hidden | hidden_names)
    view["errors"] = [e for e in job.get("errors") or [] if not (isinstance(e, str) and e.startswith(prefixes))]
    return view


async def _hidden(request, document_id: str):
    """The 404 for a document the caller may not use, audited; None otherwise
    (also for an unknown or unreadable id, which the route answers itself)."""
    blocked = await asyncio.to_thread(_blocker, request)
    if blocked is None:
        return None
    try:
        record = await asyncio.to_thread(store.get, document_id)
    except store.AreaError:
        return None
    collection = blocked(record)
    if collection is None:
        return None
    await asyncio.to_thread(audit.access_refused, request, collection, "DOCUMENT_NOT_FOUND")
    return api_error(404, "DOCUMENT_NOT_FOUND", f"Document '{document_id}' not found.")


async def _usable(request):
    """The predicate preparation_jobs uses to select documents (None: all)."""
    blocked = await asyncio.to_thread(_blocker, request)
    return None if blocked is None else (lambda record: blocked(record) is None)


async def _audit_refused_ids(request, document_ids) -> None:
    """Audit each explicitly selected document the caller may not use (#606)."""
    if not document_ids:
        return
    blocked = await asyncio.to_thread(_blocker, request)
    if blocked is None:
        return
    for document_id in dict.fromkeys(document_ids):
        try:
            record = await asyncio.to_thread(store.get, document_id)
        except store.AreaError:
            continue
        collection = blocked(record)
        if collection is not None:
            await asyncio.to_thread(audit.access_refused, request, collection, "DOCUMENT_NOT_FOUND")


# ── upload ───────────────────────────────────────────────────────────────────

@router.post("/upload", status_code=201)
async def upload(files: list[UploadFile] = File(...), request: Request = None):
    audit.note(files=[f.filename for f in files], file_count=len(files),
               total_bytes=sum(f.size or 0 for f in files))
    try:
        created, skipped = await jobs.upload(files, job_lists.job_owner(request))
    except store.AreaError as exc:
        return _error(exc)
    except ingest_pipeline.UploadRejected as exc:
        return api_error(exc.status, exc.code, exc.message, detail=exc.detail)
    except Exception:
        # %r: a filename can hold a newline that would forge a log line (#298).
        _log.exception("Preparation upload failed unexpectedly (files: %r)",
                       [f.filename for f in files])
        return api_error(500, "PREPARE_FAILED",
                         "The upload failed because of an unexpected server error. "
                         "The API log has the details.")
    return JSONResponse(status_code=201, content={
        "documents": [store.list_entry(r) for r in created], "skipped": skipped})


# ── reading ──────────────────────────────────────────────────────────────────

@router.get("/documents")
async def list_documents(request: Request = None):
    records = await asyncio.to_thread(store.list_documents)
    blocked = await asyncio.to_thread(_blocker, request)
    if blocked is not None:
        visible = await asyncio.to_thread(_visible_of, request)
        records = [_shown(r, visible) for r in records if blocked(r) is None]
    used = await asyncio.to_thread(store.usage)
    return {"documents": [store.list_entry(r) for r in records],
            "used_bytes": used, "limit_bytes": store.limit()}


@router.get("/documents/{document_id}")
async def get_document(document_id: str, request: Request = None):
    if (hidden := await _hidden(request, document_id)) is not None:
        return hidden
    try:
        record = await asyncio.to_thread(store.preview, document_id)
    except store.AreaError as exc:
        return _error(exc)
    return _shown(record, await asyncio.to_thread(_visible_of, request))


# ── downloads ────────────────────────────────────────────────────────────────

def _disposition(filename: str) -> str:
    fallback = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in filename)
    return f"attachment; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename, safe='')}"


def _stream(handle: Any) -> Iterator[bytes]:
    try:
        while True:
            block = handle.read(_BLOCK)
            if not block:
                break
            yield block
    finally:
        handle.close()


def _media_type(name: str) -> str:
    media, encoding = mimetypes.guess_type(name)
    if encoding == "gzip":
        return "application/gzip"
    return media or "application/octet-stream"


@router.get("/documents/{document_id}/raw")
async def download_raw(document_id: str, request: Request = None):
    if (hidden := await _hidden(request, document_id)) is not None:
        return hidden
    try:
        record, handle = await asyncio.to_thread(store.open_raw, document_id)
    except store.AreaError as exc:
        return _error(exc)
    audit.note(target=record["name"], kind="raw")
    filename = store.raw_file_name(record["name"])
    size = os.fstat(handle.fileno()).st_size
    return StreamingResponse(_stream(handle), media_type=_media_type(filename),
                             headers={"Content-Disposition": _disposition(filename),
                                      "Content-Length": str(size)})


def _build_zip(handles: list[tuple[str, Any]]) -> Path:
    fd, name = tempfile.mkstemp(prefix="prepare-", suffix=".zip", dir=settings.upload_dir)
    os.close(fd)
    path = Path(name)
    try:
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            for part_name, handle in handles:
                with archive.open(store.raw_file_name(part_name), "w") as out:
                    while True:
                        block = handle.read(_BLOCK)
                        if not block:
                            break
                        out.write(block)
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    finally:
        for _, handle in handles:
            handle.close()


@router.get("/documents/{document_id}/prepared")
async def download_prepared(document_id: str, request: Request = None):
    if (hidden := await _hidden(request, document_id)) is not None:
        return hidden
    try:
        record, handles = await asyncio.to_thread(store.open_parts, document_id)
    except store.AreaError as exc:
        return _error(exc)
    audit.note(target=record["name"], kind="prepared")
    if len(handles) == 1:
        part_name, handle = handles[0]
        filename = store.raw_file_name(part_name)
        size = os.fstat(handle.fileno()).st_size
        return StreamingResponse(_stream(handle), media_type=_media_type(filename),
                                 headers={"Content-Disposition": _disposition(filename),
                                          "Content-Length": str(size)})
    path = await asyncio.to_thread(_build_zip, handles)
    handle = path.open("rb")
    size = os.fstat(handle.fileno()).st_size
    filename = store.raw_file_name(record["name"])
    from services.preparation import stem_of
    return StreamingResponse(
        _stream(handle), media_type="application/zip",
        headers={"Content-Disposition": _disposition(f"{stem_of(filename)}-prepared.zip"),
                 "Content-Length": str(size)},
        background=BackgroundTask(path.unlink, missing_ok=True))


# ── deletes ──────────────────────────────────────────────────────────────────

@router.delete("/documents/{document_id}", status_code=204)
async def delete_document(document_id: str, request: Request = None):
    if (hidden := await _hidden(request, document_id)) is not None:
        return hidden
    try:
        record = await asyncio.to_thread(store.delete, document_id)
    except store.AreaError as exc:
        return _error(exc)
    audit.note(target=record["name"], kind="document")
    return Response(status_code=204)


@router.delete("/documents/{document_id}/prepared", status_code=204)
async def delete_prepared(document_id: str, request: Request = None):
    if (hidden := await _hidden(request, document_id)) is not None:
        return hidden
    try:
        record = await asyncio.to_thread(store.delete_prepared, document_id)
    except store.AreaError as exc:
        return _error(exc)
    audit.note(target=record["name"], kind="prepared")
    return Response(status_code=204)


# ── jobs ─────────────────────────────────────────────────────────────────────

@router.post("/jobs", status_code=202)
async def start_job(body: PrepareStartRequest, request: Request = None):
    if body.all:
        audit.note(all=True)
    else:
        audit.note(document_ids=body.document_ids)
    try:
        answer = await jobs.start(body.document_ids, job_lists.job_owner(request), await _usable(request))
    except store.AreaError as exc:
        if isinstance(exc, store.DocumentNotFound):
            await _audit_refused_ids(request, body.document_ids)
        return _error(exc)
    return JSONResponse(status_code=202, content=answer)


@router.get("/jobs", response_model=PrepareJobList)
async def list_jobs(request: Request):
    principal = request.state.auth

    def build():
        items = job_lists.job_items(jobs.job_records())
        blocked = _blocker(request)
        if blocked is not None:
            items = [(_job_view(record, blocked), running, finish) for record, running, finish in items]
        return job_lists.entries(PrepareJobEntry, items, principal)
    return PrepareJobList(jobs=await asyncio.to_thread(build))


@router.get("/job/{job_id}")
async def job_status(job_id: str, request: Request = None):
    job = jobs.get_job(job_id)
    if job is None:
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    blocked = await asyncio.to_thread(_blocker, request)
    view = await asyncio.to_thread(_job_view, dict(job), blocked)
    return {key: value for key, value in view.items() if not key.startswith("_")}


@router.post("/job/{job_id}/cancel", status_code=202)
async def cancel_job(job_id: str):
    audit.note(job_id=job_id)
    try:
        answer = await jobs.cancel(job_id)
    except store.AreaError as exc:
        return _error(exc)
    return JSONResponse(status_code=202, content=answer)


# ── import ───────────────────────────────────────────────────────────────────

@router.post("/import", status_code=202)
async def import_documents(body: PrepareImportRequest, request: Request = None):
    collection = body.collection
    audit.note(collection=collection)
    try:
        selected = await jobs.import_documents(body, await _usable(request))
    except store.AreaError as exc:
        if isinstance(exc, store.DocumentNotFound):
            await _audit_refused_ids(request, body.document_ids)
        return _error(exc)
    audit.note(names=selected["names"])
    # A collection the caller may not use answers as one that doesn't exist (#606).
    if not await access.check(request, collection):
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{collection}' not found.")
    try:
        exists = await jobs.collection_exists(collection)
    except Exception:                                       # noqa: BLE001
        _log.exception("Could not check whether %r exists for a preparation import", collection)
        return collection_check_unavailable(collection, "No job was started")
    if not exists:
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{collection}' not found.")
    try:
        job_id = await jobs.admit_import(selected["records"], selected["names"], body,
                                         job_lists.job_owner(request))
    except store.AreaError as exc:
        return _error(exc)
    except CollectionDeletingError:
        return collection_deleting_error(collection)
    except ingest_pipeline.UploadRejected as exc:
        return api_error(exc.status, exc.code, exc.message, detail=exc.detail)
    return JSONResponse(status_code=202, content={
        "job_id": job_id, "status": "queued", "files_queued": len(selected["names"]),
        "collection": collection})
