from __future__ import annotations
import asyncio
import logging
import mimetypes
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import settings
from models.schemas import IngestConfig
from services.chunker import chunk as do_chunk
from services import sources
from services import weaviate_client as wc

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt", ".md", ".csv", ".json"}

_jobs: dict[str, dict] = {}
_log = logging.getLogger(__name__)


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


def _save_upload(src, dest: Path) -> None:
    with dest.open("wb") as fh:
        shutil.copyfileobj(src, fh, length=1024 * 1024)


def _parse_file(path: Path) -> tuple[str, list[Any]]:
    ext = path.suffix.lower()
    if ext == ".pdf":
        from unstructured.partition.pdf import partition_pdf
        elements = partition_pdf(filename=str(path))
    elif ext == ".docx":
        from unstructured.partition.docx import partition_docx
        elements = partition_docx(filename=str(path))
    elif ext == ".txt":
        from unstructured.partition.text import partition_text
        elements = partition_text(filename=str(path))
    elif ext == ".md":
        from unstructured.partition.md import partition_md
        elements = partition_md(filename=str(path))
    elif ext == ".csv":
        from unstructured.partition.csv import partition_csv
        elements = partition_csv(filename=str(path))
    elif ext == ".json":
        from unstructured.partition.json import partition_json
        elements = partition_json(filename=str(path))
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    text = "\n".join(str(el) for el in elements)
    return text, elements


def _process_job_sync(
    job_id: str,
    file_paths: list[Path],
    tmp_dir: Path,
    collection: str,
    strategy: str,
    chunk_size: int,
    chunk_overlap: int,
    similarity_threshold: float,
    min_chunk_size: int,
) -> None:
    job = _jobs[job_id]
    job["status"] = "running"

    try:
        for path in file_paths:
            try:
                text, elements = _parse_file(path)
                ext = path.suffix.lower().lstrip(".")
                source_type = ext

                chunks = do_chunk(
                    text=text,
                    strategy=strategy,
                    chunk_size=chunk_size,
                    chunk_overlap_size=chunk_overlap,
                    similarity_threshold=similarity_threshold,
                    min_chunk_size=min_chunk_size,
                    elements=elements if strategy == "context_aware" else None,
                )

                now = datetime.now(timezone.utc).isoformat()
                weaviate_chunks = [
                    {
                        "content": c,
                        "source_file": path.name,
                        "source_type": source_type,
                        "chunk_index": i,
                        "chunk_strategy": strategy,
                        "chunk_size": chunk_size,
                        "chunk_overlap": chunk_overlap,
                        "created_at": now,
                    }
                    for i, c in enumerate(chunks)
                ]

                wc._insert_chunks_sync(collection, weaviate_chunks)

                # Retain the original only after the file has been parsed,
                # chunked and stored. A file that fails any of those steps
                # leaves no orphan source behind.
                try:
                    sources.store(
                        collection,
                        path.name,
                        path.read_bytes(),
                        mimetypes.guess_type(path.name)[0],
                    )
                except OSError as exc:
                    # Retention failing must not fail an otherwise good ingest;
                    # the chunks are already stored. It does cost this
                    # collection its full-fidelity export, so it is logged loudly.
                    _log.error("Could not retain source %s for %r: %s", path.name, collection, exc)

                job["chunks_stored"] += len(chunks)
                job["files_completed"] += 1
            except Exception as exc:
                job["files_failed"] += 1
                job["errors"].append(f"{path.name}: {exc}")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if job["files_failed"] == 0:
        job["status"] = "completed"
    elif job["files_failed"] == job["files_total"]:
        job["status"] = "failed"
    else:
        job["status"] = "partial"


async def start_ingest_job(
    files: list[Any],
    collection: str,
    strategy: str,
    chunk_size: int,
    chunk_overlap: int,
    similarity_threshold: float,
    min_chunk_size: int,
) -> str:
    config = IngestConfig(chunking_strategy=strategy, chunk_size=chunk_size,
                          chunk_overlap=chunk_overlap, similarity_threshold=similarity_threshold,
                          min_chunk_size=min_chunk_size)
    strategy, chunk_size, chunk_overlap, min_chunk_size = (
        config.chunking_strategy, config.chunk_size, config.chunk_overlap, config.min_chunk_size)
    similarity_threshold = config.similarity_threshold if config.similarity_threshold is not None else 0.85
    job_id = str(uuid.uuid4())[:8]

    tmp_dir = Path(tempfile.mkdtemp(dir=settings.upload_dir))
    file_paths: list[Path] = []
    # Files the caller sent that this build cannot parse. Dropping them without
    # a word makes a ZIP of 8 files silently report 6, with nothing saying which
    # two were ignored or why.
    skipped: list[str] = []

    try:
        for upload in files:
            raw_name = upload.filename or ""
            safe_name = Path(raw_name).name
            if not safe_name:
                continue
            dest = tmp_dir / safe_name
            # Copy in blocks rather than `await upload.read()`, which held the
            # whole file in memory. Uploads can now reach 512 MB (issue #21),
            # and this container already runs close to its memory budget. The
            # copy blocks, so it runs off the event loop.
            await asyncio.to_thread(_save_upload, upload.file, dest)

            if safe_name.lower().endswith(".zip"):
                resolved_tmp = tmp_dir.resolve()
                with zipfile.ZipFile(dest, "r") as z:
                    for member in z.infolist():
                        if member.is_dir():
                            continue
                        member_path = (tmp_dir / member.filename).resolve()
                        if not member_path.is_relative_to(resolved_tmp):
                            continue
                        if member_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                            z.extract(member, tmp_dir)
                            file_paths.append(member_path)
                        else:
                            skipped.append(
                                f"{safe_name}:{member.filename} "
                                f"(unsupported type '{member_path.suffix.lower() or 'none'}')")
                os.remove(dest)
            elif Path(safe_name).suffix.lower() in SUPPORTED_EXTENSIONS:
                file_paths.append(dest)
            else:
                skipped.append(
                    f"{safe_name} (unsupported type "
                    f"'{Path(safe_name).suffix.lower() or 'none'}')")
    except Exception:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    if not file_paths:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        detail = "; ".join(skipped)
        raise ValueError(
            "No supported files found in upload."
            + (f" Skipped: {detail}" if detail else ""))

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "files_total": len(file_paths),
        "files_completed": 0,
        "files_failed": 0,
        "chunks_stored": 0,
        "errors": [],
        "skipped": skipped,
    }

    def _on_done(future: asyncio.Future) -> None:
        if future.cancelled():
            return
        exc = future.exception()
        if exc is not None:
            _log.error("ingest job %s failed: %s", job_id, exc)
            job = _jobs.get(job_id)
            if job and job["status"] not in ("completed", "partial", "failed"):
                job["status"] = "failed"
                job["errors"].append(str(exc))

    future = asyncio.get_running_loop().run_in_executor(
        None,
        _process_job_sync,
        job_id,
        file_paths,
        tmp_dir,
        collection,
        strategy,
        chunk_size,
        chunk_overlap,
        similarity_threshold,
        min_chunk_size,
    )
    future.add_done_callback(_on_done)

    return job_id
