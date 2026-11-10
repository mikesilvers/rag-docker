"""Uploads, preparation jobs and imports of the preparation area (#638).

SPECIFICATIONS.md §3.1.11.

- **Upload.** Files are staged and validated exactly as ingest stages them
  (`ingest_pipeline.stage_uploads`: ZIP limits, supported types), then each
  becomes a `raw` document. The space check is best-effort: made once, before
  the files are written to the area, with nothing reserved.
- **Jobs.** A job (kind `prepare` in the job store) prepares its documents one
  at a time. One job runs at a time on the instance; later ones wait, oldest
  first. A job can be cancelled: a queued one at once, a running one after its
  current LLM call. Documents it finished keep their output; the one in
  progress and the ones not reached go back to their earlier state.
- **Import.** The selected documents' parts go to one ordinary ingest job,
  copied from the area into its staging and stored under their part names. The
  documents are busy only while the import is admitted.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import tempfile
import uuid
from pathlib import Path
from typing import Any

from config import settings
from services import ingest_pipeline, jobstore, preparation
from services import ollama_client as ollama
from services import preparation_store as store
from services import weaviate_client as wc

_log = logging.getLogger(__name__)

_jobs = jobstore.JobStore("prepare")
_queue: asyncio.Queue | None = None
_worker: asyncio.Task | None = None
_cancel_requested: set[str] = set()


class NoDocuments(store.AreaError):
    status, code = 400, "NO_DOCUMENTS"


class JobNotFound(store.AreaError):
    status, code = 404, "JOB_NOT_FOUND"


class JobNotRunning(store.AreaError):
    status, code = 409, "JOB_NOT_RUNNING"


class DuplicateNames(store.AreaError):
    status, code = 409, "DUPLICATE_NAMES"


class NoSupportedFiles(store.AreaError):
    status, code = 400, "NO_SUPPORTED_FILES"


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def job_records() -> list[tuple[str, dict, float | None]]:
    return _jobs.snapshot()


# ── upload ───────────────────────────────────────────────────────────────────

async def upload(files: list[Any], owner: dict | None) -> tuple[list[dict], list[str]]:
    """Stage, validate and store an upload's files as `raw` documents. Nothing
    is kept when any step fails."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="prepare-", dir=settings.upload_dir))
    created: list[dict] = []
    try:
        queue, skipped = await ingest_pipeline.stage_uploads(files, tmp_dir)
        if not queue.entries:
            detail = "; ".join(skipped)
            raise NoSupportedFiles("No supported files found in upload."
                                   + (f" Skipped: {detail}" if detail else ""))
        total = sum(path.stat().st_size for path, _ in queue.entries.values())
        await asyncio.to_thread(store.check_space, total, "this upload")
        for logical, (path, _label) in queue.entries.items():
            created.append(await asyncio.to_thread(store.create, logical, path, owner))
        return created, skipped
    except BaseException:
        for record in created:
            try:
                await asyncio.to_thread(store.delete, record["id"])
            except Exception:                               # noqa: BLE001
                _log.exception("Could not remove document %r after a failed upload",
                               record.get("id"))
        raise
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


# ── selection ────────────────────────────────────────────────────────────────

def _resolve(document_ids: list[str] | None, all_of: set[str]) -> list[dict]:
    """The records selected: the ids in the order given, or every document whose
    status is in `all_of`, oldest first."""
    if document_ids is None:
        records = [r for r in store.list_documents() if r.get("status") in all_of]
        records.sort(key=lambda r: r.get("created_at") or "")
    else:
        records, missing = [], []
        for document_id in dict.fromkeys(document_ids):
            try:
                records.append(store.get(document_id))
            except store.DocumentNotFound:
                missing.append(document_id)
        if missing:
            raise store.DocumentNotFound(
                f"Document{'s' if len(missing) > 1 else ''} not found: {', '.join(missing)}.",
                detail={"documents": missing})
    if not records:
        raise NoDocuments("No documents were selected.")
    return records


# ── jobs ─────────────────────────────────────────────────────────────────────

async def start(document_ids: list[str] | None, owner: dict | None) -> dict:
    """Queue a preparation job for the selected documents (or every document that
    isn't being prepared, with document_ids None)."""
    await asyncio.to_thread(store.check_not_full)
    job_id = uuid.uuid4().hex[:8]

    def admit() -> list[dict]:
        records = _resolve(document_ids, {"raw", "prepared", "failed"})
        busy = [r["name"] for r in records
                if r.get("status") in ("queued", "preparing") or store.is_busy(r["id"])]
        if busy:
            raise store.DocumentBusy(
                f"Being prepared or imported already: {', '.join(busy[:10])}"
                f"{' and more' if len(busy) > 10 else ''}.", detail={"documents": busy})
        store.queue_for_job([r["id"] for r in records], job_id)
        return records

    records = await asyncio.to_thread(admit)
    ids = [r["id"] for r in records]
    try:
        await _jobs.create(job_id, {
            "job_id": job_id,
            "status": "queued",
            "documents_total": len(ids),
            "documents_completed": 0,
            "documents_failed": 0,
            "document_ids": ids,
            "names": [r["name"] for r in records],
            "current": None,
            "errors": [],
            "owner": owner,
        })
    except BaseException:
        await asyncio.to_thread(store.abandon, ids)
        raise
    _enqueue(job_id)
    return {"job_id": job_id, "status": "queued", "documents_queued": len(ids)}


_loop: asyncio.AbstractEventLoop | None = None


def _enqueue(job_id: str) -> None:
    """Put a job on the queue of the running event loop and make sure its one
    worker runs. The API has one loop; a new loop (a test's) gets a new queue."""
    global _queue, _worker, _loop
    loop = asyncio.get_running_loop()
    if _queue is None or _loop is not loop:
        _queue, _worker, _loop = asyncio.Queue(), None, loop
    _queue.put_nowait(job_id)
    if _worker is None or _worker.done():
        _worker = loop.create_task(_work(_queue))


async def _work(queue: asyncio.Queue) -> None:
    while True:
        job_id = await queue.get()
        try:
            job = _jobs.get(job_id)
            if job is None or job.get("status") != "queued":
                continue
            await _run(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:                                   # noqa: BLE001
            _log.exception("Preparation job %s failed unexpectedly", job_id)
            job = _jobs.get(job_id)
            if job is not None and job.get("status") not in jobstore.TERMINAL:
                await asyncio.to_thread(store.abandon, job.get("document_ids") or [])
                job["errors"].append("the job stopped because of an unexpected server error; "
                                     "the API log has the details")
                job["status"] = "failed"
        finally:
            _cancel_requested.discard(job_id)


async def _run(job_id: str) -> None:
    job = _jobs[job_id]
    job["status"] = "running"
    ids: list[str] = list(job["document_ids"])
    cancelled = False

    def should_stop() -> bool:
        return job_id in _cancel_requested

    for position, document_id in enumerate(ids):
        if should_stop():
            await asyncio.to_thread(store.abandon, ids[position:])
            cancelled = True
            break
        try:
            record, folder = await asyncio.to_thread(store.begin, document_id, job_id)
        except store.AreaError as exc:
            # Deleted or unreadable since it was queued.
            job["documents_failed"] = job["documents_failed"] + 1
            job["errors"].append(f"{document_id}: {exc.message}")
            store.release([document_id])
            continue
        name = record["name"]
        job["current"] = {"document_id": document_id, "name": name,
                          "windows_done": 0, "windows_total": 0}

        def progress(done: int, total: int, document_id=document_id, name=name) -> None:
            job["current"] = {"document_id": document_id, "name": name,
                              "windows_done": done, "windows_total": total}

        async def write_part(index: int, suffix: str, data: bytes, folder=folder) -> Path:
            try:
                await asyncio.to_thread(store.check_space, len(data), "this prepared part")
            except store.StorageFull as exc:
                raise preparation.StorageRefused(exc.message) from None
            path = folder / f"{index + 1:03d}{suffix}"
            await asyncio.to_thread(path.write_bytes, data)
            return path

        try:
            raw = await asyncio.to_thread(store.raw_path, document_id)
            result = await preparation.prepare_document(
                raw, name, record["raw_type"], ollama.chat, ollama.list_models,
                should_stop, write_part, progress)
            await asyncio.to_thread(store.commit, document_id, result.parts, result.fallback)
            job["documents_completed"] = job["documents_completed"] + 1
        except preparation.Cancelled:
            await asyncio.to_thread(store.abandon, ids[position:])
            cancelled = True
            break
        except asyncio.CancelledError:
            await asyncio.to_thread(store.abandon, ids[position:])
            raise
        except Exception as exc:                            # noqa: BLE001
            reason = str(exc) or type(exc).__name__
            if not isinstance(exc, (ValueError, store.AreaError)):
                _log.warning("Preparing %r failed", name, exc_info=True)
            await asyncio.to_thread(store.fail, document_id, reason)
            job["errors"].append(f"{name}: {reason}")
            job["documents_failed"] = job["documents_failed"] + 1

    job["current"] = None
    if cancelled:
        job["status"] = "cancelled"
    elif job["documents_failed"] == 0:
        job["status"] = "completed"
    elif job["documents_failed"] >= job["documents_total"]:
        job["status"] = "failed"
    else:
        job["status"] = "partial"


async def cancel(job_id: str) -> dict:
    job = _jobs.get(job_id)
    if job is None:
        raise JobNotFound(f"Job '{job_id}' not found.")
    if job.get("status") in jobstore.TERMINAL:
        raise JobNotRunning(f"Job '{job_id}' has already ended ({job['status']}).")
    if job.get("status") == "queued":
        job["status"] = "cancelled"
        await asyncio.to_thread(store.abandon, job.get("document_ids") or [])
    else:
        _cancel_requested.add(job_id)
    return {"job_id": job_id, "status": job["status"]}


def recover() -> list[str]:
    """At startup, after the job store marked unfinished jobs interrupted."""
    return store.recover()


# ── import ───────────────────────────────────────────────────────────────────

class _Part:
    """A prepared part as start_ingest_job reads an upload."""

    def __init__(self, name: str, handle: Any) -> None:
        self.filename = store.raw_file_name(name)
        self.file = handle
        self.size = None


async def import_documents(body: Any, owner: dict | None) -> dict:
    """Start one ingest job with every part of the selected prepared documents."""

    def select() -> tuple[list[dict], list[str]]:
        records = _resolve(body.document_ids, {"prepared"})
        busy = [r["name"] for r in records
                if r.get("status") in ("queued", "preparing") or store.is_busy(r["id"])]
        if busy:
            raise store.DocumentBusy(
                f"Being prepared or imported: {', '.join(busy[:10])}"
                f"{' and more' if len(busy) > 10 else ''}.", detail={"documents": busy})
        unprepared = [r["name"] for r in records if r.get("status") != "prepared"]
        if unprepared:
            raise store.DocumentNotPrepared(
                f"Not prepared: {', '.join(unprepared[:10])}"
                f"{' and more' if len(unprepared) > 10 else ''}.",
                detail={"documents": unprepared})
        names = [part["name"] for r in records for part in r.get("parts") or []]
        seen, duplicates = set(), []
        for name in names:
            if name in seen and name not in duplicates:
                duplicates.append(name)
            seen.add(name)
        if duplicates:
            raise DuplicateNames(
                f"Two selected documents have parts with the same name: "
                f"{', '.join(duplicates[:10])}. Import them separately.",
                detail={"names": duplicates})
        return records, names

    records, names = await asyncio.to_thread(select)
    return {"records": records, "names": names}


async def admit_import(records: list[dict], names: list[str], body: Any,
                       owner: dict | None) -> str:
    """Open the parts and start the ingest job; the documents are busy meanwhile."""
    ids = [r["id"] for r in records]
    await asyncio.to_thread(store.mark_busy, ids, "import")
    handles: list[Any] = []
    try:
        files = []
        for record in records:
            _rec, opened = await asyncio.to_thread(store.open_parts, record["id"])
            for part_name, handle in opened:
                handles.append(handle)
                files.append(_Part(part_name, handle))
        job_id = await ingest_pipeline.start_ingest_job(
            files=files, collection=body.collection, strategy=body.strategy,
            chunk_size=body.chunk_size, chunk_overlap=body.chunk_overlap,
            similarity_threshold=body.similarity_threshold,
            min_chunk_size=body.min_chunk_size, owner=owner, names=names)
        await asyncio.to_thread(store.add_import, ids, body.collection, job_id)
        return job_id
    finally:
        for handle in handles:
            try:
                handle.close()
            except OSError:
                pass
        await asyncio.to_thread(store.release, ids)


async def collection_exists(collection: str) -> bool:
    return await wc.collection_exists(collection)
