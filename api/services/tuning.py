"""Re-chunking, re-embedding and re-indexing a collection in place.

Spec §7. Every operation here rebuilds the collection, because chunk identity
or vector width changes and Weaviate cannot alter either in place.

**Safety.** Each rebuild is staged: the new chunks are built into a temporary
collection first, and the live one is replaced only once that succeeds. Weaviate
has no rename (see `importer.py`), so the final step copies vectors out of the
staging collection rather than re-embedding — one embedding pass, not two.
Preparation failures leave the original collection untouched. A failure after
replacement begins can leave it missing or partial. Identity-changing operations
flag retained evaluations before replacement; any failed cutover flags them too.

**Gold standard.** Anything that changes chunk identity marks every session for
the collection `stale`, with a reason and a timestamp. Sessions are never
deleted and never remapped (spec §7.3).
"""
from __future__ import annotations

import asyncio
import logging
import mimetypes
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from services import goldstandard
from services import sources
from services import weaviate_client as wc
from services.chunker import chunk as do_chunk
from services.ingest_pipeline import _parse_file
from services.packager import PackageError

_log = logging.getLogger(__name__)

_jobs: dict[str, dict] = {}
_active: set[str] = set()
_lock = threading.Lock()


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


# ── Reading the collection's own chunks ───────────────────────────────────────

def _existing_chunks(collection: str) -> list[dict]:
    """Stored properties, without vectors. Used when re-embedding chunk text."""
    col = wc.get_client().collections.get(collection)
    return [dict(o.properties or {}) for o in col.iterator()]


def _chunks_from_sources(collection: str, strategy: str, chunk_size: int,
                         chunk_overlap: int, similarity_threshold: float,
                         min_chunk_size: int) -> list[dict]:
    """Re-parse and re-chunk every retained original.

    Everything is parsed before the collection is touched: parsing is the
    failure-prone step, and discovering a bad file after the rebuild has started
    would cost the collection.
    """
    index = sources.load_index(collection)
    documents = index.get("documents") or {}
    if not documents:
        raise PackageError(
            "SOURCES_REQUIRED",
            f"Collection '{collection}' has no retained source documents, so it "
            "cannot be re-chunked. Only collections ingested after source "
            "retention was added carry their originals; export shows this as "
            "fidelity 'chunks-only'.",
            {"collection": collection})

    src_dir = sources.collection_dir(collection)
    out: list[dict] = []
    now = datetime.now(timezone.utc).isoformat()
    work = Path(tempfile.mkdtemp(prefix="rechunk-", dir=settings.upload_dir))
    try:
        for digest, entry in sorted(documents.items()):
            blob = src_dir / digest
            if not blob.is_file():
                raise PackageError(
                    "SOURCES_REQUIRED",
                    f"Retained source {digest[:12]} is missing from disk, so "
                    f"'{collection}' cannot be rebuilt from its originals.",
                    {"collection": collection, "digest": digest})
            # Parsers dispatch on the file extension, so the original filename
            # has to be restored before parsing.
            filename = (entry.get("filenames") or [digest])[0]
            staged = work / Path(filename).name
            shutil.copyfile(blob, staged)

            text, elements = _parse_file(staged)
            chunks = do_chunk(
                text=text,
                strategy=strategy,
                chunk_size=chunk_size,
                chunk_overlap_size=chunk_overlap,
                similarity_threshold=similarity_threshold,
                min_chunk_size=min_chunk_size,
                elements=elements if strategy == "context_aware" else None,
            )
            source_type = staged.suffix.lower().lstrip(".")
            out.extend({
                "content": c,
                "source_file": staged.name,
                "source_type": source_type,
                "chunk_index": i,
                "chunk_strategy": strategy,
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
                "created_at": now,
            } for i, c in enumerate(chunks))
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if not out:
        raise PackageError(
            "SOURCES_REQUIRED",
            f"Re-chunking '{collection}' produced no chunks; check the chunking "
            "parameters.", {"collection": collection})
    return out


# ── Rebuilding ────────────────────────────────────────────────────────────────

def _rebuild(collection: str, properties: list[dict], index_type: str | None,
             distance_metric: str | None, progress, before_replace=None) -> int:
    """Stage the new chunks, then swap them into place.

    Weaviate embeds during the staging insert. The final insert reuses those
    vectors verbatim, so the corpus is embedded once rather than twice.
    """
    cutover_started = False
    config = wc._collection_config_sync(collection)
    new_index = index_type or config["index_type"]
    new_distance = distance_metric or config["distance_metric"]
    hnsw = config.get("hnsw_config") or {}

    staging = f"{collection}__tuning_{uuid.uuid4().hex[:8]}"
    client = wc.get_client()
    wc._create_collection_sync(staging, new_index, new_distance, hnsw, preserve_hnsw=True)
    try:
        wc._insert_chunks_sync(staging, properties)
        staged = [
            {"id": str(o.uuid),
             "vector": (o.vector or {}).get("default"),
             "properties": dict(o.properties or {})}
            for o in client.collections.get(staging).iterator(include_vector=True)
        ]
        if len(staged) != len(properties):
            raise RuntimeError(
                f"staged {len(staged)} chunks but expected {len(properties)}")
        if progress:
            progress(len(staged))

        # Past this point the original is replaced. Preparation has succeeded,
        # but deletion, creation or final writes can still fail.
        # Persist the warning before deletion, so a failed replacement cannot
        # leave retained pairs claiming to be a current baseline.
        if before_replace:
            before_replace()
        cutover_started = True
        client.collections.delete(collection)
        wc._create_collection_sync(collection, new_index, new_distance, hnsw, preserve_hnsw=True)
        target = client.collections.get(collection)
        with target.batch.dynamic() as batch:
            for record in staged:
                batch.add_object(properties=record["properties"],
                                 uuid=record["id"], vector=record["vector"])
            if batch.number_errors > 0:
                raise RuntimeError(
                    f"{batch.number_errors} error(s) writing the rebuilt collection")
        return len(staged)
    except Exception:
        if cutover_started:
            goldstandard.mark_stale(collection, "collection replacement failed after cutover began; retained pairs require historical review")
        raise
    finally:
        try:
            client.collections.delete(staging)
        except Exception:                             # noqa: BLE001
            _log.exception("Could not remove staging collection %r", staging)


def _run(job_id: str, collection: str, operation: str, params: dict) -> None:
    job = _jobs[job_id]
    job["status"] = "running"

    def progress(n: int) -> None:
        job["chunks_written"] = n

    try:
        has_sources = sources.has_sources(collection)

        if operation == "rechunk":
            if not has_sources:
                raise PackageError(
                    "SOURCES_REQUIRED",
                    f"Collection '{collection}' is chunks-only: its original "
                    "documents were not retained, so it cannot be re-chunked. "
                    "Re-embedding from the stored chunk text is available, but "
                    "chunk boundaries cannot change.",
                    {"collection": collection})
            properties = _chunks_from_sources(collection, **params["chunking"])
            reason = "the collection was re-chunked, so its chunks no longer match these pairs"

        elif operation == "reembed":
            if params.get("chunking") is not None:
                # Spec §7.2: refuse the combination rather than silently
                # dropping one half of what was asked for.
                if not has_sources:
                    raise PackageError(
                        "SOURCES_REQUIRED",
                        f"Collection '{collection}' is chunks-only. Re-embedding "
                        "regenerates vectors from the stored chunk text, so chunk "
                        "boundaries cannot change; this request also asked for new "
                        "chunking parameters. Send one or the other.",
                        {"collection": collection})
                properties = _chunks_from_sources(collection, **params["chunking"])
                reason = "the collection was re-chunked and re-embedded"
            elif has_sources:
                properties = _existing_chunks(collection)
                reason = "the collection was re-embedded, so its vectors changed"
            else:
                properties = _existing_chunks(collection)
                reason = ("the collection was re-embedded from stored chunk text, "
                          "so its vectors changed")

        elif operation == "reindex":
            properties = _existing_chunks(collection)
            reason = None            # chunk identity is unchanged
        else:
            raise PackageError("TUNE_UNSUPPORTED", f"Unknown operation '{operation}'.")

        job["chunks_total"] = len(properties)
        written = _rebuild(collection, properties,
                           params.get("index_type"), params.get("distance_metric"),
                           progress, before_replace=(lambda: goldstandard.mark_stale(collection, reason)) if reason else None)

        notes = []
        if reason:
            stale = goldstandard.mark_stale(collection, reason)
            if stale:
                notes.append(f"{stale} gold-standard session(s) marked stale")
        else:
            notes.append("chunk identity unchanged, so gold-standard sessions "
                         "were left alone")

        job.update(status="completed", chunks_written=written, notes=notes)

    except PackageError as exc:
        job.update(status="failed", error_code=exc.code, error=exc.message,
                   error_detail=exc.detail)
    except Exception as exc:                          # noqa: BLE001
        _log.exception("Tuning %r on %r failed", operation, collection)
        job.update(status="failed", error_code="TUNE_FAILED",
                   error=f"{type(exc).__name__}: {exc}")
    finally:
        with _lock:
            _active.discard(collection)


async def start_tune_job(collection: str, operation: str, params: dict) -> str:
    job_id = str(uuid.uuid4())[:8]
    with _lock:
        if collection in _active:
            raise RuntimeError(collection)
        _active.add(collection)

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "queued",
        "collection": collection,
        "operation": operation,
        "chunks_total": 0,
        "chunks_written": 0,
        "notes": [],
        "error": None,
        "error_code": None,
        "error_detail": None,
    }
    asyncio.create_task(asyncio.to_thread(_run, job_id, collection, operation, params))
    return job_id
