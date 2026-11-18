"""Export and import endpoints.

Import arrives in Phase 5; this module carries the export half and the shared
job-polling shape so both live behind one prefix-free router.
"""
from __future__ import annotations

import asyncio
import logging
import os
import stat
from pathlib import Path

from fastapi import APIRouter, Request
from starlette.responses import Response

from auth import access

from models.schemas import (
    ExportJobEntry,
    ExportJobList,
    ExportJobStatusResponse,
    ImportJobEntry,
    ImportJobList,
    ExportRequest,
    ExportStartResponse,
    ImportJobStatusResponse,
    ImportRequest,
    ImportStartResponse,
    PackageListResponse,
    PackageSummary,
)
from services import age_stream, audit, exporter, importer, job_lists, package_retention, packager
from services import weaviate_client as wc
from services.collection_writes import CollectionDeletingError
from utils import api_error, collection_check_unavailable, collection_deleting_error

router = APIRouter()
_log = logging.getLogger(__name__)


@router.post("/export", response_model=ExportStartResponse, status_code=202)
async def start_export(body: ExportRequest, request: Request = None):
    # The audit record's parameters (#29): whether it is encrypted, never the passphrase.
    audit.note(collection=body.collection, include_models=body.include_models, encrypted=body.passphrase is not None)
    # A collection the caller may not use answers as one that doesn't exist (#606).
    if not await access.check(request, body.collection):
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{body.collection}' not found.")
    try:
        exists = await wc.collection_exists(body.collection)
    except Exception:                                 # noqa: BLE001
        # Weaviate couldn't be asked; the cause is logged, not sent (#366).
        _log.exception("Could not check whether %r exists for an export", body.collection)
        return collection_check_unavailable(body.collection, "No job was started")
    if not exists:
        # Checked before a job exists, so a typo does not leave a failed job
        # lying around for the user to interpret.
        return api_error(404, "COLLECTION_NOT_FOUND",
                         f"Collection '{body.collection}' not found.")
    try:
        job_id = await exporter.start_export_job(
            body.collection, body.include_models,
            body.passphrase.get_secret_value() if body.passphrase is not None else None,
            owner=job_lists.job_owner(request))
    except CollectionDeletingError:
        return collection_deleting_error(body.collection)
    except exporter.ExportStartingError:
        # The running start hasn't stored its record yet, so no job_id to name (#338).
        return api_error(409, "EXPORT_STARTING",
                         f"An export of '{body.collection}' is starting; retry shortly.")
    except exporter.ExportInProgressError as exc:
        # The exception names the running job; any other error is not this
        # answer, and its text is not sent (#657).
        return api_error(
            409, "EXPORT_IN_PROGRESS",
            f"An export of '{body.collection}' is already running.",
            detail={"job_id": exc.job_id},
        )
    return ExportStartResponse(job_id=job_id, status="queued", collection=body.collection)


@router.get("/export/jobs", response_model=ExportJobList)
async def list_export_jobs(request: Request):
    """The caller's running and recent exports (#617; see services/job_lists)."""
    principal = request.state.auth
    def build():
        return job_lists.entries(ExportJobEntry, job_lists.job_items(exporter.job_records()), principal)
    return ExportJobList(jobs=await asyncio.to_thread(build))


@router.get("/export/job/{job_id}", response_model=ExportJobStatusResponse)
async def export_job_status(job_id: str, request: Request = None):
    job = exporter.get_job(job_id)
    # A job on a collection the caller may not use answers as a missing one (#606).
    if job is None or (job.get("collection") and not await access.check(request, job["collection"], "JOB_NOT_FOUND")):
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    # When its package expires (#677), while it is still recorded.
    expires = None
    if job.get("status") == "completed" and isinstance(job.get("filename"), str):
        expires = await asyncio.to_thread(package_retention.expires_at, job["filename"])
    return ExportJobStatusResponse(**{**job, "expires_at": expires})


@router.post("/import", response_model=ImportStartResponse, status_code=202)
async def start_import(body: ImportRequest, request: Request = None):
    # The audit record's parameters (#29); the collection is added when the job ends.
    audit.note(package=body.filename, on_conflict=body.on_conflict, encrypted=body.passphrase is not None)
    # Everything else — unreadable archive, bad digest, embedding mismatch,
    # name collision — is reported through the job, because it is only knowable
    # after reading the package, which takes long enough to need a job.
    passphrase = body.passphrase.get_secret_value() if body.passphrase is not None else None
    try:
        job_id = await importer.start_import_job(body.filename, body.on_conflict, passphrase,
                                                 owner=job_lists.job_owner(request))
    except importer.PassphraseError as exc:
        # Decided by the filename before any job exists (#27).
        return api_error(400, exc.code, str(exc), detail={"filename": body.filename})
    except importer.ImportInProgressError as exc:
        return api_error(409, "IMPORT_IN_PROGRESS",
                         f"An import of '{exc.filename}' is already running.",
                         detail={"filename": exc.filename})
    return ImportStartResponse(job_id=job_id, status="queued", filename=body.filename)


@router.get("/import/jobs", response_model=ImportJobList)
async def list_import_jobs(request: Request):
    """The caller's running and recent imports (#617; see services/job_lists)."""
    principal = request.state.auth
    def build():
        return job_lists.entries(ImportJobEntry, job_lists.job_items(importer.job_records()), principal)
    return ImportJobList(jobs=await asyncio.to_thread(build))


@router.get("/import/job/{job_id}", response_model=ImportJobStatusResponse)
async def import_job_status(job_id: str, request: Request = None):
    job = importer.get_job(job_id)
    # An import job names its collection once its package is read (#606).
    if job is None or (job.get("collection") and not await access.check(request, job["collection"], "JOB_NOT_FOUND")):
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    return ImportJobStatusResponse(**job)


def _summary(p: Path) -> dict:
    """One package as the listing shows it."""
    if age_stream.is_encrypted_name(p.name):
        # No metadata without the passphrase, and no scrypt to list (#27).
        try:
            with p.open("rb") as fh:
                readable = age_stream.check_header(fh)
            size = p.stat().st_size
        except Exception:                             # noqa: BLE001
            # One bad file never breaks the listing of the others.
            readable, size = False, 0
        return {"filename": p.name, "size_bytes": size,
                "collection": None, "chunk_count": None, "fidelity": None,
                "created_at": None, "readable": readable, "encrypted": True}
    try:
        fields = packager.listing_fields(packager.read_manifest(p))
    except Exception:
        # A file that is not a readable package, or whose manifest has a
        # wrongly typed field (#451), still belongs in the listing; import
        # will report precisely why it cannot be used.
        return {"filename": p.name, "size_bytes": p.stat().st_size,
                "collection": None, "chunk_count": None,
                "fidelity": None, "created_at": None, "readable": False}
    return {
        "filename": p.name,
        "size_bytes": p.stat().st_size,
        **fields,
        "readable": True,
    }


def _list_packages_sync() -> list[dict]:
    root = packager.exports_dir()
    expiries = package_retention.expiries()
    return [{**_summary(p), "expires_at": expiries.get(p.name)}
            for p in sorted([*root.glob("ragpkg-*.tar.gz"), *root.glob("ragpkg-*" + age_stream.ENCRYPTED_SUFFIX)])]


async def _hidden(principal, packages: list[dict]) -> set[str]:
    """The packages a caller may not see (#606): unencrypted ones whose
    collection exists here and is restricted to others. Weaviate failing to
    answer counts as existing, so the listing fails closed."""
    visible = await asyncio.to_thread(access.visible_filter, principal)
    hidden = set()

    async def exists(name):
        try:
            return await wc.collection_exists(name)
        except Exception:                             # noqa: BLE001
            _log.exception("Could not check whether %r exists for the package listing", name)
            return True
    for p in packages:
        name = p.get("collection")
        if p.get("encrypted") or not p.get("readable") or not isinstance(name, str) or not name:
            continue
        # A package of a discarded staging or recovery copy follows its target.
        source = access.canonical(name)
        if access.target_of(source) != source and not await exists(source):
            source = access.target_of(source)
        if not visible(source) and await exists(source):
            hidden.add(p["filename"])
    return hidden


@router.get("/packages", response_model=PackageListResponse)
async def list_packages(request: Request = None):
    """Packages sitting in ./exports — both exported here and dropped in to import."""
    packages = await asyncio.to_thread(_list_packages_sync)
    hidden = await _hidden(access.principal_of(request), packages)
    packages = [p for p in packages if p["filename"] not in hidden]
    return PackageListResponse(packages=[PackageSummary(**p) for p in packages])


# ── Downloading a package (#677) ─────────────────────────────────────────────

_CHUNK = 1024 * 1024


def _package_not_found(filename: str):
    return api_error(404, "PACKAGE_NOT_FOUND", f"No package named '{filename}' in the exports directory.")


def _package_busy(filename: str):
    return api_error(409, "PACKAGE_BUSY",
                     f"'{filename}' is being imported or downloaded. Try again when that has finished.")


class _PackageStream(Response):
    """Streams one package and, after a complete GET, has it deleted when this
    instance exported it (package_retention.after_download).

    The download slot is claimed and the file opened here, not in the handler,
    so a response that is never called holds neither. Complete means every byte
    was handed to the connection and no disconnect was seen before the last
    message; nginx doesn't buffer ahead of the client (X-Accel-Buffering). There
    is no Range support: a download is the whole file or it is incomplete, and
    an incomplete one keeps the package."""

    def __init__(self, path: Path, filename: str, head: bool):
        super().__init__(status_code=200)
        self.path, self.filename, self.head = path, filename, head

    async def _refuse(self, send, response) -> None:
        await send({"type": "http.response.start", "status": response.status_code,
                    "headers": response.raw_headers})
        await send({"type": "http.response.body", "body": response.body, "more_body": False})

    async def __call__(self, scope, receive, send) -> None:
        completed = claimed = False
        fd = None
        listener = None
        try:
            if not self.head:
                claimed = await asyncio.to_thread(package_retention.claim, self.filename)
                if not claimed:
                    return await self._refuse(send, _package_busy(self.filename))
            try:
                fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
                info = os.fstat(fd)
            except OSError:
                return await self._refuse(send, _package_not_found(self.filename))
            if not stat.S_ISREG(info.st_mode):
                return await self._refuse(send, _package_not_found(self.filename))
            size = info.st_size
            await send({"type": "http.response.start", "status": 200, "headers": [
                (b"content-type", b"application/octet-stream"),
                (b"content-length", str(size).encode()),
                (b"content-disposition", f'attachment; filename="{self.filename}"'.encode()),
                (b"cache-control", b"no-store"),
                (b"x-accel-buffering", b"no"),
            ]})
            if self.head:
                await send({"type": "http.response.body", "body": b"", "more_body": False})
                return
            gone = asyncio.Event()

            async def listen():
                while True:
                    message = await receive()
                    if message["type"] == "http.disconnect":
                        gone.set()
                        return
            listener = asyncio.create_task(listen())
            sent = 0
            while sent < size and not gone.is_set():
                chunk = await asyncio.to_thread(os.pread, fd, min(_CHUNK, size - sent), sent)
                if not chunk:
                    break                             # the file shrank: incomplete
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
                sent += len(chunk)
            await asyncio.sleep(0)                    # let the listener see a disconnect
            if sent == size and not gone.is_set():
                completed = True
                await send({"type": "http.response.body", "body": b"", "more_body": False})
            # Otherwise no final message: uvicorn drops a lost connection and
            # closes one that is still open, so the client sees it cut short.
        finally:
            if listener is not None:
                listener.cancel()
            if fd is not None:
                os.close(fd)
            audit.note(completed=completed)
            try:
                if completed and not self.head:
                    await asyncio.to_thread(package_retention.after_download, self.filename, scope)
            finally:
                if claimed:
                    await asyncio.to_thread(package_retention.release, self.filename)
            if self.background is not None:
                await self.background()


# HEAD is its own route, out of the API schema, so the schema and the policy
# list the download once (GET); policy.required answers HEAD as GET.
@router.head("/packages/{filename}", include_in_schema=False)
@router.get("/packages/{filename}")
async def download_package(filename: str, request: Request = None):
    """One package from ./exports, streamed (#677). A full download deletes a
    package this instance exported; any other package is served and kept."""
    audit.note(completed=False)
    if not package_retention.valid_name(filename):
        return _package_not_found(filename)
    path = packager.exports_dir() / filename
    try:
        info = await asyncio.to_thread(os.lstat, path)
    except OSError:
        return _package_not_found(filename)
    if not stat.S_ISREG(info.st_mode):
        return _package_not_found(filename)
    # The listing's access rule (#606): a package hidden from the caller there
    # answers as one that doesn't exist, and the refusal is audited.
    summary = await asyncio.to_thread(_summary, path)
    if filename in await _hidden(access.principal_of(request), [summary]):
        await asyncio.to_thread(audit.access_refused, request, access.canonical(summary["collection"]),
                                "PACKAGE_NOT_FOUND")
        return _package_not_found(filename)
    if await asyncio.to_thread(package_retention.busy, filename):
        return _package_busy(filename)
    head = request is not None and request.method == "HEAD"
    return _PackageStream(path, filename, head=head)
