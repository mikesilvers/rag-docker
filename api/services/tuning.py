"""Re-chunking, re-embedding and re-indexing a collection in place.

Spec §7. Every operation here rebuilds the collection, because chunk identity
or vector width changes and Weaviate cannot alter either in place.

**Safety.** Each rebuild is staged: the new chunks are built into a temporary
collection first, and the live one is replaced only once that succeeds. Weaviate
has no rename (see `importer.py`), so the final step copies vectors out of the
staging collection rather than re-embedding — one embedding pass, not two. A
failure before replacement leaves the original untouched. After replacement
starts, a verified recovery copy and its sidecars survive failure and restart.
Identity-changing operations flag retained evaluations before replacement;
failed reindex cutovers flag them when exact identity cannot be verified.

**Memory.** Re-embedding from stored chunk text and reindexing read and copy the
collection a page at a time; a reindex verifies every copy against a disk-backed
fingerprint snapshot of the original, never decoded records in memory (#583).
Re-chunking, and re-embedding with chunking parameters, stage the new chunks one
file part at a time as each retained original is read (#312).

**Gold standard.** Anything that changes chunk identity marks every session for
the collection `stale`, with a reason and a timestamp. Sessions are never
deleted and never remapped (spec §7.3).
"""
from __future__ import annotations
from services import telemetry

import asyncio
import contextlib
import copy
import logging
import math
import shutil
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from services import collection_writes, jobstore
from config import settings
from services import goldstandard
from services import parts, sources, splitter
from services import weaviate_client as wc
from services import batch_write, collection_recovery
from services.chunker import chunk as do_chunk
from services.ingest_pipeline import _parse_file, source_type_of
from services.packager import PackageError

_log = logging.getLogger(__name__)

# Job records survive an API restart (#23); see jobstore.py.
_jobs = jobstore.JobStore("tune")
_active: set[str] = set()
_lock = threading.Lock()


def get_job(job_id: str) -> dict | None:
    return _jobs.get(job_id)


# ── Reading the collection's own chunks ───────────────────────────────────────

class _StoredChunks:
    """Stored properties, without vectors, read a page at a time (#583).

    Re-iterable: every pass walks the collection again, so the batch writer's
    preflight, write and resend passes stream it instead of holding it. The
    collection writer guard keeps the walks identical. The count is Weaviate's
    aggregate, taken once.
    """

    def __init__(self, collection: str) -> None:
        self.collection = collection
        self._count: int | None = None

    def __iter__(self):
        col = wc.get_client().collections.get(self.collection)
        return (dict(o.properties or {}) for o in telemetry.iterate(col.iterator))

    def __len__(self) -> int:
        if self._count is None:
            col = wc.get_client().collections.get(self.collection)
            self._count = telemetry.call("weaviate.aggregate", col.aggregate.over_all,
                                         total_count=True).total_count or 0
        return self._count


def _existing_chunks(collection: str) -> _StoredChunks:
    """Stored properties, without vectors. Used when re-embedding chunk text."""
    return _StoredChunks(collection)


def _existing_records(collection: str) -> list[dict]:
    """Read the supported single-vector corpus without regenerating identity."""
    return list(_iter_existing_records(collection))


def _iter_existing_records(collection: str):
    # Duplicate UUIDs are refused by the snapshot's unique key (_snapshot), so
    # nothing here grows with the collection (#583).
    col = wc.get_client().collections.get(collection)
    for obj in telemetry.iterate(col.iterator, include_vector=True):
        identity = str(obj.uuid)
        vector = obj.vector
        if isinstance(vector, dict):
            if set(vector) != {"default"}:
                raise RuntimeError("Reindex requires the collection's single default vector")
            vector = vector["default"]
        if (not isinstance(vector, list) or not vector
                or any(isinstance(value, bool) or not isinstance(value, (int, float))
                       or not math.isfinite(value) for value in vector)):
            raise RuntimeError(f"Reindex cannot preserve the stored vector for {identity}")
        yield {"id": identity, "vector": copy.deepcopy(vector),
               "properties": copy.deepcopy(dict(obj.properties or {}))}


class _RecordSnapshot:
    """A reindex's fixed expectation and where to read the records it copies.

    `expected` holds the original's UUIDs and property/float32-vector
    fingerprints on disk (batch_write.ExpectedRecords, spec §6.5); `read()`
    streams records from one collection. The snapshot never changes, so a check
    after cutover still compares against the original (#583).
    """

    def __init__(self, expected, read) -> None:
        self.expected = expected
        self.read = read

    def __len__(self) -> int:
        return self.expected.count

    def reading(self, collection: str) -> "_RecordSnapshot":
        return _RecordSnapshot(self.expected, lambda: _iter_existing_records(collection))

    def close(self) -> None:
        self.expected.__exit__(None, None, None)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


_DUPLICATE = "Duplicate chunk UUID "


def _snapshot(records) -> _RecordSnapshot:
    """Fingerprint a reusable record source once: a zero-argument callable
    returning an iterator, or a list."""
    read = records if callable(records) else (lambda: iter(records))
    expected = batch_write.ExpectedRecords()
    try:
        expected.capture(read)
    except BaseException as exc:
        expected.__exit__(None, None, None)
        if isinstance(exc, ValueError) and str(exc).startswith(_DUPLICATE):
            raise RuntimeError("Reindex received duplicate stored UUID "
                               + str(exc)[len(_DUPLICATE):]) from exc
        raise
    return _RecordSnapshot(expected, read)


def _record_snapshot(collection: str) -> _RecordSnapshot:
    """The reindex snapshot of the stored collection, read a page at a time."""
    return _snapshot(lambda: _iter_existing_records(collection))


def _as_snapshot(records) -> tuple[_RecordSnapshot, bool]:
    """A snapshot, and whether the caller made it and must close it."""
    if isinstance(records, _RecordSnapshot):
        return records, False
    return _snapshot(records), True


def _write_records(collection: str, records) -> None:
    """Copy a snapshot's records a page at a time, drain the batch, then compare
    backend readback with the snapshot."""
    snapshot, owned = _as_snapshot(records)
    try:
        col = wc.get_client().collections.get(collection)
        with telemetry.span("weaviate.batch"):
            with col.batch.dynamic() as batch:
                for record in snapshot.read():
                    batch.add_object(properties=copy.deepcopy(record["properties"]),
                                     uuid=record["id"], vector=copy.deepcopy(record["vector"]))
            if batch.number_errors:
                raise RuntimeError(f"{batch.number_errors} error(s) copying reindex records")
        _verify_records(collection, snapshot)
    finally:
        if owned:
            snapshot.close()


def _verify_records(collection: str, records) -> None:
    """Stream the collection against the snapshot: every UUID once, nothing
    else, the same property and float32 vector fingerprints."""
    snapshot, owned = _as_snapshot(records)
    try:
        snapshot.expected.verify(wc.get_client().collections.get(collection), exact=True)
    except batch_write.BatchVerificationError as exc:
        raise RuntimeError("Reindex backend readback changed UUIDs, properties or vectors") from exc
    finally:
        if owned:
            snapshot.close()


_MISSING_CHUNK_INDEX = object()

# How many uncovered names a coverage refusal keeps in
# error_detail.uncovered_source_files, which the job API returns;
# uncovered_source_files_omitted counts the rest (#208), as #28 caps members.
_MAX_NAMED_UNCOVERED = 10


def _source_coverage(collection: str, documents: dict) -> tuple[list[str], set[str]]:
    """The stored source files a rebuild from the retained originals would not
    reproduce, and every name that has stored chunks, from one walk of the
    collection. A rebuild emits only the stored names (#311). A part
    `<name> (part N)` that no digest lists is covered by its original `<name>`,
    which then counts as stored (#621)."""
    by_filename: dict[str, list[str]] = {}
    for digest, entry in documents.items():
        for filename in entry["filenames"]:
            by_filename.setdefault(filename, []).append(digest)
    seen: dict[str, set] = {}
    uncovered = set()
    stored = set()
    # Only the two properties the rule reads, and no vector: not each chunk's
    # text, on every GET /tune (#258).
    for obj in telemetry.iterate(wc.get_client().collections.get(collection).iterator,
                                 include_vector=False,
                                 return_properties=["source_file", "chunk_index"]):
        props = obj.properties or {}
        filename = props.get("source_file")
        if not isinstance(filename, str) or not filename:
            uncovered.add("<unknown>")
            continue
        listed = parts.resolve(filename, by_filename) or filename
        stored.add(listed)
        # A rebuild emits one chunk set per (digest, name) (#22), so a name is
        # covered when exactly one digest lists it, whichever of its names it is.
        if len(by_filename.get(listed, [])) != 1:
            uncovered.add(filename)
            continue
        index = props.get("chunk_index")
        if not isinstance(index, int) or isinstance(index, bool):
            index = _MISSING_CHUNK_INDEX
        indices = seen.setdefault(filename, set())
        if index in indices:
            uncovered.add(filename)
        indices.add(index)
    return sorted(uncovered), stored


def _uncovered_source_files(collection: str, documents: dict) -> list[str]:
    """Stored source files a rebuild from the retained originals would not reproduce."""
    return _source_coverage(collection, documents)[0]


def _listed_names(documents: dict) -> set[str]:
    return {filename for entry in documents.values() for filename in entry["filenames"]}


class ServiceUnavailableError(Exception):
    """Weaviate couldn't be read for the up-front re-chunk check (#340) or for
    the tuning options (#349)."""


class TuneInProgressError(RuntimeError):
    """This collection is already being tuned. Its own type, so the router
    never answers another RuntimeError as TUNE_IN_PROGRESS (#657)."""

    def __init__(self, collection: str) -> None:
        super().__init__(collection)
        self.collection = collection


# A collection whose retained originals all lack stored chunks (every file
# produced none at ingest) has nothing to rebuild, since a rebuild emits only
# stored names (#311). It isn't offered re-chunking, and a request is refused
# before any job runs (#328).
def _nothing_to_rebuild(collection: str) -> PackageError:
    return PackageError(
        "SOURCES_REQUIRED",
        f"Cannot re-chunk '{collection}': no retained original file has stored "
        "chunks, so there is nothing to rebuild.",
        {"collection": collection})


# An invalid retained source index (sources.load_index raises ValueError) is
# refused before a job is created too, as the tuning options already report it
# (#389). Re-embedding from stored chunks and re-indexing never read it.
def _source_index_invalid(collection: str) -> PackageError:
    return PackageError(
        "SOURCE_INDEX_INVALID",
        f"Cannot re-chunk '{collection}': its retained source index is invalid. "
        "Repair it before re-chunking; re-embedding from stored chunks and "
        "re-indexing remain available.",
        {"collection": collection})


def _has_stored_listed_name(collection: str, documents: dict) -> bool:
    """Whether any name the index lists has stored chunks; stops at the first."""
    names = _listed_names(documents)
    for obj in wc.get_client().collections.get(collection).iterator():
        filename = (obj.properties or {}).get("source_file")
        # A part counts for its original (#621).
        if isinstance(filename, str) and parts.resolve(filename, names) is not None:
            return True
    return False


def _refuse_nothing_to_rebuild(collection: str) -> None:
    """The re-chunk refusals made before a job is created: an invalid
    retained source index (#389) and nothing to rebuild (#328).

    The other refusals are still reported by the job. A Weaviate failure
    during the walk raises ServiceUnavailableError, which the router answers
    with 503 rather than a bare 500 (#340).
    """
    try:
        documents = sources.load_index(collection)["documents"]
    except ValueError as exc:
        raise _source_index_invalid(collection) from exc
    if not documents:
        return
    try:
        stored = _has_stored_listed_name(collection, documents)
    except Exception as exc:                          # noqa: BLE001
        _log.exception("Could not read %r for the re-chunk check", collection)
        raise ServiceUnavailableError(collection) from exc
    if not stored:
        raise _nothing_to_rebuild(collection)


# Why the tuning options don't offer re-chunking; see rechunk_refusal.
RECHUNK_NOTHING_TO_REBUILD = "nothing-to-rebuild"
RECHUNK_NOT_COVERED = "not-covered"


def rechunk_refusal(collection: str) -> str | None:
    """Why re-chunking would fail the checks made before parsing, or None.

    Used by the tuning options, so they offer re-chunking only when the job
    would accept it. Nothing to rebuild is reported first, as the start refuses
    it before the others (#328). An invalid index or blob path raises ValueError.
    A Weaviate failure during the walk raises ServiceUnavailableError, which
    the router answers with 503 rather than a bare 500 (#349).
    """
    documents = sources.load_index(collection)["documents"]
    if not documents:
        return RECHUNK_NOT_COVERED
    try:
        uncovered, stored = _source_coverage(collection, documents)
    except Exception as exc:                          # noqa: BLE001
        _log.exception("Could not read %r for the tuning options", collection)
        raise ServiceUnavailableError(collection) from exc
    if not stored & _listed_names(documents):
        return RECHUNK_NOTHING_TO_REBUILD
    for digest in documents:
        if not sources.blob_path(collection, digest).is_file():
            return RECHUNK_NOT_COVERED
    return RECHUNK_NOT_COVERED if uncovered else None


def _missing_source(collection: str, digest: str) -> PackageError:
    return PackageError(
        "SOURCES_REQUIRED",
        f"Retained source {digest[:12]} is missing from disk, so "
        f"'{collection}' cannot be rebuilt from its originals.",
        {"collection": collection, "digest": digest})


@telemetry.traced("rag.chunk")
def _chunks_from_sources(collection: str, strategy: str, chunk_size: int,
                         chunk_overlap: int, similarity_threshold: float,
                         min_chunk_size: int) -> "_SourceChunks":
    """Check that every retained original can be re-parsed and re-chunked, and
    return a reader that re-chunks them one file part at a time (#312).

    Every check that needs no parsing runs here, before any collection is
    created. Parsing, the failure-prone step, runs as the rebuild consumes the
    reader: each part is staged before the next is read, so the collection is
    never held in memory, and a bad file found then fails the rebuild while the
    live collection is still untouched (it is only replaced once staging
    succeeds). `list(reader)` gives every chunk, in order.

    Its `rag.chunk` span covers only these checks and the reader's creation
    (#893): the parsing and chunking run as the rebuild consumes the reader,
    inside `rag.rebuild`, where the chunker's own `rag.chunk` spans appear per
    part.
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

    # Re-chunking rebuilds one chunk set per (digest, name) pair, as ingest
    # stored it (#22), so a second name for the same content is rebuilt too. A
    # stored name is covered only when exactly one digest lists it and its
    # chunks form one set (no repeated chunk_index). A name with more than one
    # digest (re-uploaded before #22 made re-ingesting replace), duplicate chunk
    # sets and a name with no retained original are refused before staging:
    # replacing from the retained subset would lose chunks, and a successful
    # cutover discards their recovery copy. Only names with stored chunks are
    # rebuilt (#311): an index can list a name the collection never showed (a
    # file that produced no chunks, or an imported package listing one blob
    # under many names), and rebuilding it would cost a parse and an embedding
    # each time. With no listed name stored there is nothing to rebuild, which
    # is refused as such rather than as a chunking problem (#328).
    uncovered, stored = _source_coverage(collection, documents)
    if not stored & _listed_names(documents):
        raise _nothing_to_rebuild(collection)
    if uncovered:
        raise PackageError(
            "SOURCES_REQUIRED",
            "Cannot change chunk boundaries: some stored chunks have missing or "
            "ambiguous retained originals. Re-embed without chunking parameters "
            "or re-index to preserve the existing chunks.",
            {"collection": collection,
             "uncovered_source_files": uncovered[:_MAX_NAMED_UNCOVERED],
             "uncovered_source_files_omitted": max(0, len(uncovered) - _MAX_NAMED_UNCOVERED)})
    # Every blob is checked before anything is parsed or staged (#312), in the
    # order the reader reaches them.
    for digest in sorted(documents):
        if not sources.blob_path(collection, digest).is_file():
            raise _missing_source(collection, digest)
    return _SourceChunks(collection, documents, stored, splitter.Settings(
        strategy, chunk_size, chunk_overlap, similarity_threshold, min_chunk_size))


class _SourceChunks:
    """A re-chunk's new chunks, read from the retained originals one file part
    at a time (#312).

    `parts()` yields each part's chunks as a list, parsing the next part only
    when asked, so whoever stages a part before asking for the next holds one
    part (at most the per-file limits) whatever the collection's size. It reads
    the originals once per call. Iterating gives every chunk in turn.

    `start()` reads the first part ahead, before any collection is created, so
    a re-chunk that produces no chunks, or whose first file can't be read,
    fails before the rebuild starts, as it did when everything was parsed first;
    the next `parts()` begins with that part. `close()` ends a started read.
    """

    def __init__(self, collection: str, documents: dict, stored: set[str],
                 split_settings: splitter.Settings) -> None:
        self.collection = collection
        self.documents = documents
        self.stored = stored
        self.settings = split_settings
        # One created_at for the whole rebuild.
        self.now = datetime.now(timezone.utc).isoformat()
        self._started = None
        self._first = None

    def start(self) -> None:
        reading = self._read()
        # An empty read raises the no-chunks error here (see _read).
        self._first = next(reading)
        self._started = reading

    def close(self) -> None:
        if self._started is not None:
            self._started.close()
            self._started = self._first = None

    def parts(self):
        if self._started is None:
            yield from self._read()
            return
        reading, first = self._started, self._first
        self._started = self._first = None
        try:
            yield first
            del first
            yield from reading
        finally:
            reading.close()

    def _read(self):
        collection, split_settings = self.collection, self.settings
        produced = 0
        work = Path(tempfile.mkdtemp(prefix="rechunk-", dir=settings.upload_dir))
        try:
            slots = 0
            for digest, entry in sorted(self.documents.items()):
                blob = sources.blob_path(collection, digest)
                if not blob.is_file():
                    # Removed since _chunks_from_sources checked it.
                    raise _missing_source(collection, digest)
                for filename in entry["filenames"]:
                    if filename not in self.stored:
                        continue
                    # Parsers dispatch on the file extension, so the original name
                    # has to be restored before parsing; each pair is staged in a
                    # directory of its own, so a/report.txt and b/report.txt can't
                    # collide.
                    slots += 1
                    slot = work / str(slots)
                    slot.mkdir()
                    staged = slot / PurePosixPath(filename).name
                    shutil.copyfile(blob, staged)

                    source_type = source_type_of(staged)
                    # Split as ingest splits it (#621): a file over the per-file
                    # limits under the new settings is rebuilt as parts, however
                    # many parts it had before.
                    number = 0
                    try:
                        for kind, chunks in splitter.split_file(staged, split_settings,
                                                                _parse_file, do_chunk):
                            number += 1
                            source = filename if kind == "whole" else parts.part_name(filename, number)
                            part = [{
                                "content": c,
                                "source_file": source,
                                "source_type": source_type,
                                "chunk_index": i,
                                "chunk_strategy": split_settings.strategy,
                                "chunk_size": split_settings.chunk_size,
                                "chunk_overlap": split_settings.chunk_overlap,
                                "created_at": self.now,
                            } for i, c in enumerate(chunks)]
                            del chunks
                            if part:
                                produced += len(part)
                                yield part
                            # Dropped before the next part is read, so only one
                            # part is held at a time.
                            del part
                    except splitter.PartError as exc:
                        raise ValueError(f"{filename}: part {exc.number}: {exc}") from exc.cause
                    except splitter.StoredTextTooLarge as exc:
                        # The same cap as ingest (PR #630): the re-chunk fails whole,
                        # before the collection is replaced.
                        raise ValueError(f"{filename}: {exc}") from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)

        if not produced:
            raise PackageError(
                "SOURCES_REQUIRED",
                f"Re-chunking '{collection}' produced no chunks; check the chunking "
                "parameters.", {"collection": collection})

    def __iter__(self):
        for part in self.parts():
            yield from part


# ── Rebuilding ────────────────────────────────────────────────────────────────

@telemetry.traced("rag.rebuild")
@collection_writes.serialized("collection")
def _rebuild(collection: str, properties: list[dict], index_type: str | None,
             distance_metric: str | None, progress, *, records: list[dict] | None = None,
             source_collection: str | None = None, before_replace=None, staged=None) -> int:
    """Stage and verify, then replace the live collection under its writer guard.

    Reindex `records` are a fingerprint snapshot of the original (a list is
    snapshotted here); nothing holds the collection in memory (#583). A
    re-chunk's `properties` is a _SourceChunks reader: each part is inserted
    into staging before the next is read, and `staged(n)` is told the running
    count (#312). Other properties are inserted in one write.
    """
    source_collection = source_collection or collection
    collection = collection_writes.canonical(collection)
    if records is not None:
        wc._validate_reindex_vectorizer_sync(collection)
    config = wc._collection_config_sync(collection)
    new_index = index_type or config["index_type"]
    new_distance = distance_metric or config["distance_metric"]
    hnsw = config.get("hnsw_config") or {}
    client = wc.get_client()
    ownership = collection_recovery.begin(collection, "tune", client)
    staging = ownership["staging"]
    cutover_started = False
    replace_ready = False
    history_marked = False
    completed = False
    original_intact = False
    owned_records = False
    try:
        if records is not None:
            records, owned_records = _as_snapshot(records)
        wc._create_collection_sync(staging, new_index, new_distance, hnsw, preserve_hnsw=True)
        if records is not None:
            _write_records(staging, records)
            # The guard covers application writers; independently connected
            # backend writers are detected by comparing the source again.
            _verify_records(collection, records)
        else:
            produce = getattr(properties, "parts", None)
            if produce is not None:
                expected = 0
                with contextlib.closing(produce()) as produced:
                    for part in produced:
                        wc._insert_chunks_sync(staging, part)
                        expected += len(part)
                        del part
                        if staged:
                            staged(expected)
            else:
                wc._insert_chunks_sync(staging, properties)
                expected = len(properties)
            staged_count = telemetry.call("weaviate.aggregate", client.collections.get(staging).aggregate.over_all, total_count=True).total_count
            if staged_count != expected:
                raise RuntimeError(f"staged {staged_count} chunks but expected {expected}")
        if source_collection != collection:
            collection_recovery.retain(ownership, source_collection=source_collection)
        else:
            collection_recovery.retain(ownership)
        if before_replace:
            before_replace()
        replace_ready = True
        collection_recovery.begin_cutover(ownership)
        cutover_started = True
        telemetry.call("weaviate.delete", client.collections.delete, collection)
        wc._create_collection_sync(collection, new_index, new_distance, hnsw, preserve_hnsw=True,
                                   description=collection_recovery.cutover_description(ownership))
        if records is not None:
            # The original is gone: copy the verified staging collection.
            _write_records(collection, records.reading(staging))
            written = len(records)
        else:
            def staged_records():
                return (
                    {"id": str(obj.uuid), "vector": (obj.vector or {}).get("default"),
                     "properties": dict(obj.properties or {})}
                    for obj in telemetry.iterate(client.collections.get(staging).iterator, include_vector=True)
                )
            written = batch_write.insert(client.collections.get(collection), staged_records,
                                         expected_count=expected)
        if progress:
            progress(written)
        completed = True
        return written
    except Exception as exc:
        if records is not None and cutover_started:
            try:
                _verify_records(collection, records)
                original_intact = wc._collection_config_sync(collection) == config
            except Exception:
                original_intact = False
            if not original_intact:
                try:
                    goldstandard.mark_stale(source_collection, "reindex replacement failed after cutover began; exact record preservation was not verified")
                    history_marked = True
                except Exception:
                    _log.exception("Could not mark evaluation historical after failed reindex")
        elif cutover_started:
            try:
                goldstandard.mark_stale(source_collection, "collection replacement failed after cutover began; retained pairs require historical review")
                history_marked = True
            except Exception:
                _log.exception("Could not mark evaluation historical after failed rebuild")
        if history_marked:
            # History is already marked: record the decision so that a restart
            # doesn't check and flag again; only a hard kill leaves it pending (#263).
            try:
                collection_recovery.finish_cutover_check(ownership, "stale")
            except Exception:
                _log.exception("Could not record the cutover decision for %r; startup will check it", staging)
        if cutover_started and not original_intact and ownership["state"] == "recovery":
            raise PackageError(
                "TUNE_FAILED", f"{type(exc).__name__}: {exc}. Verified data is retained as '{staging}'.",
                {"recovered_as": staging,
                 "sidecar_snapshots": collection_recovery.sidecar_reference(ownership)}) from exc
        if not cutover_started and ownership["state"] == "recovery":
            # Between retain and cutover run before_replace (stale marking) and
            # begin_cutover (the cutover record). The original was never deleted,
            # so the copy is discarded below.
            if not replace_ready:
                raise PackageError(
                    "TUNE_FAILED", f"{type(exc).__name__}: {exc}. Gold-standard sessions could not be "
                    "marked stale before replacement, so the original collection is unchanged.") from exc
            marked = " Gold-standard sessions were already marked stale." if before_replace else ""
            raise PackageError(
                "TUNE_FAILED", f"{type(exc).__name__}: {exc}. The cutover record could not be "
                f"written, so the original collection is unchanged.{marked}") from exc
        raise
    finally:
        # Before cutover the original is intact, so a retained copy is not needed.
        if completed or original_intact or not cutover_started or ownership["state"] == "scratch":
            try:
                collection_recovery.discard(ownership, client)
            except Exception:
                _log.exception("Could not remove owned staging collection %r", staging)
        if owned_records:
            records.close()


@telemetry.traced("rag.tuning")
@collection_writes.serialized("collection")
def _run(job_id: str, collection: str, operation: str, params: dict, *, source_collection: str | None = None) -> None:
    telemetry.attribute("rag.tuning_operation", operation)
    source_collection = source_collection or collection
    collection = collection_writes.canonical(collection)
    job = _jobs[job_id]
    job["collection"] = collection
    job["status"] = "running"

    def progress(n: int) -> None:
        job["chunks_written"] = n

    def finish(**fields) -> None:
        # The registration ends before the status turns terminal, so a client
        # that sees the job finish can delete the collection at once (#40).
        collection_writes.end_job("tune", job_id)
        job.update(fields)

    records = None
    properties = None
    try:
        needs_sources = operation == "rechunk" or (
            operation == "reembed" and params.get("chunking") is not None)
        has_sources = sources.has_sources(source_collection) if needs_sources else False

        if operation == "rechunk":
            if not has_sources:
                raise PackageError(
                    "SOURCES_REQUIRED",
                    f"Collection '{collection}' is chunks-only: its original "
                    "documents were not retained, so it cannot be re-chunked. "
                    "Re-embedding from the stored chunk text is available, but "
                    "chunk boundaries cannot change.",
                    {"collection": collection})
            properties = _chunks_from_sources(source_collection, **params["chunking"])
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
                properties = _chunks_from_sources(source_collection, **params["chunking"])
                reason = "the collection was re-chunked and re-embedded"
            else:
                properties = _existing_chunks(collection)
                reason = ("the collection was re-embedded from stored chunk text, "
                          "so its vectors changed")

        elif operation == "reindex":
            records = _record_snapshot(collection)
            properties = records     # its len() is the snapshot's count
            reason = None            # exact UUID/vector/property copy is verified
        else:
            raise PackageError("TUNE_UNSUPPORTED", f"Unknown operation '{operation}'.")

        # A re-chunk's reader counts its chunks only as they are staged (#312):
        # its total starts at 0 and follows staging. Its first part is read
        # now, so a re-chunk with no chunks fails before any collection exists.
        streaming = hasattr(properties, "parts")
        if streaming and hasattr(properties, "start"):
            properties.start()
        job["chunks_total"] = 0 if streaming else len(properties)
        stale_count = 0

        def mark_before_replace() -> None:
            nonlocal stale_count
            stale_count = goldstandard.mark_stale(source_collection, reason, require_durable=True)

        def staged(n: int) -> None:
            job["chunks_total"] = n

        written = _rebuild(
            collection, properties, params.get("index_type"), params.get("distance_metric"),
            progress, records=records, source_collection=source_collection,
            before_replace=mark_before_replace if reason else None,
            **({"staged": staged} if streaming else {}))

        notes = []
        if reason:
            if stale_count:
                notes.append(f"{stale_count} gold-standard session(s) marked stale")
        else:
            notes.append("UUIDs, properties and vectors verified unchanged after reindex; "
                         "gold-standard sessions were left alone")

        finish(status="completed", chunks_written=written, notes=notes)

    except PackageError as exc:
        telemetry.outcome("error", exc)
        finish(status="failed", error_code=exc.code, error=exc.message,
               error_detail=exc.detail)
    except Exception as exc:                          # noqa: BLE001
        telemetry.outcome("error", exc)
        _log.exception("Tuning %r on %r failed", operation, collection)
        finish(status="failed", error_code="TUNE_FAILED",
               error=f"{type(exc).__name__}: {exc}")
    finally:
        if isinstance(records, _RecordSnapshot):
            records.close()
        if isinstance(properties, _SourceChunks):
            properties.close()
        collection_writes.end_job("tune", job_id)
        with _lock:
            _active.discard(collection)


async def start_tune_job(collection: str, operation: str, params: dict) -> str:
    source_collection = collection
    collection = collection_writes.canonical(collection)
    job_id = str(uuid.uuid4())[:8]
    with _lock:
        if collection in _active:
            raise TuneInProgressError(collection)
        _active.add(collection)
    # Lets DELETE /collections refuse while this job rebuilds the collection
    # (#40). Before the job record, so a refusal during a delete leaves none (#302).
    try:
        collection_writes.register_job(collection, "tune", job_id)
    except collection_writes.CollectionDeletingError:
        with _lock:
            _active.discard(collection)
        raise

    # The first write is durable, so it runs off the event loop (#309). A start
    # cancelled meanwhile releases its guards; the store removes the record.
    try:
        # A re-chunk with an invalid source index (#389) or nothing to rebuild
        # (#328) is refused here, before any job exists; the walk runs off the
        # event loop too.
        if operation == "rechunk" or (operation == "reembed" and params.get("chunking") is not None):
            await asyncio.to_thread(_refuse_nothing_to_rebuild, source_collection)
        await _jobs.create(job_id, {
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
        })
    except BaseException:
        collection_writes.end_job("tune", job_id)
        with _lock:
            _active.discard(collection)
        raise
    task = asyncio.create_task(asyncio.to_thread(telemetry.admitted(_run), job_id, collection, operation, params, source_collection=source_collection))
    # _run ends it first; this also covers a task that never ran its worker.
    task.add_done_callback(lambda _task: collection_writes.end_job("tune", job_id))
    return job_id
