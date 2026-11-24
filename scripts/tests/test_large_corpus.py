"""Large JSON Lines corpora (#312): streamed retention, the re-chunk reader that
stages one file part at a time, and export and import of a large retained original.

Runs with the API's dependencies, in the API image with the checkout mounted
read-only and no network (registered by `02_ingest.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
        "$(docker compose images -q api)" python scripts/tests/test_large_corpus.py

Weaviate is mocked; nothing reaches a backend. Retained sources, uploads and
exports go to temporary folders. Memory is measured with tracemalloc, which
sees Python's allocations (every block read or written here is one).
"""
import gc
import gzip
import hashlib
import json
import os
import sys
import tarfile
import tempfile
import tracemalloc
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from config import settings
from services import exporter, packager, parts, sources, splitter, tuning
from services import weaviate_client as wc
from services.ingest_pipeline import _parse_file, source_type_of

MIB = 1024 * 1024
COLL = "LargeCorpus"


def write_blocks(path, size, seed=b"large corpus "):
    """A file of `size` bytes, written a block at a time, so the test itself
    never holds it. Each block differs, so no two files of one size match."""
    block = (seed * (MIB // len(seed) + 1))[:MIB]
    with open(path, "wb") as out:
        left, n = size, 0
        while left:
            piece = (block[:-8] + n.to_bytes(8, "big"))[:min(MIB, left)]
            out.write(piece)
            left -= len(piece)
            n += 1
    return path


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(MIB), b""):
            digest.update(block)
    return digest.hexdigest()


def traced_peak(fn, *args, **kwargs):
    """(peak traced bytes above the start, result) of one call."""
    gc.collect()
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        start = tracemalloc.get_traced_memory()[0]
        result = fn(*args, **kwargs)
        return tracemalloc.get_traced_memory()[1] - start, result
    finally:
        tracemalloc.stop()


class Folders(unittest.TestCase):
    """Temporary sources, uploads and exports folders for every test."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key in ("sources_dir", "upload_dir"):
            path = self.root / key
            path.mkdir()
            self.stack.enter_context(patch.object(settings, key, str(path)))
        self.files = self.root / "files"
        self.files.mkdir()

    def leftovers(self, collection=COLL):
        """Temporary files left beside the blobs."""
        folder = sources.collection_dir(collection)
        return sorted(p.name for p in folder.iterdir() if p.name.endswith(".tmp")) if folder.exists() else []


# ── S3: streamed retention ────────────────────────────────────────────────────

class RetentionTests(Folders):
    def test_a_path_is_retained_with_its_digest_size_and_bytes_in_bounded_memory(self):
        path = write_blocks(self.files / "corpus.jsonl.gz", 32 * MIB)
        peak, digest = traced_peak(sources.store, COLL, "corpus.jsonl.gz", path,
                                   "application/gzip", replace=True)
        self.assertEqual(digest, file_sha256(path))
        entry = sources.load_index(COLL)["documents"][digest]
        self.assertEqual((entry["filenames"], entry["size"], entry["media_type"]),
                         (["corpus.jsonl.gz"], 32 * MIB, "application/gzip"))
        self.assertEqual(file_sha256(sources.blob_path(COLL, digest)), digest)
        self.assertLess(peak, 4 * MIB, f"peak {peak} bytes")
        self.assertEqual(self.leftovers(), [])

    def test_memory_does_not_grow_with_the_file(self):
        small = write_blocks(self.files / "small.jsonl", 16 * MIB, b"small ")
        large = write_blocks(self.files / "large.jsonl", 64 * MIB, b"large ")
        small_peak, _ = traced_peak(sources.store, COLL, "small.jsonl", small)
        large_peak, _ = traced_peak(sources.store, COLL, "large.jsonl", large)
        self.assertLess(large_peak - small_peak, MIB, (small_peak, large_peak))

    def test_replace_by_path_releases_the_earlier_version(self):
        first = write_blocks(self.files / "a.jsonl", 3 * MIB, b"first ")
        second = write_blocks(self.files / "b.jsonl", 2 * MIB, b"second ")
        old = sources.store(COLL, "corpus.jsonl", first, replace=True)
        new = sources.store(COLL, "corpus.jsonl", second, replace=True)
        self.assertEqual(list(sources.load_index(COLL)["documents"]), [new])
        self.assertFalse(sources.blob_path(COLL, old).exists())
        self.assertEqual(file_sha256(sources.blob_path(COLL, new)), file_sha256(second))

    def test_a_file_that_changes_while_it_is_copied_is_not_retained(self):
        earlier = write_blocks(self.files / "earlier.jsonl", MIB, b"earlier ")
        kept = sources.store(COLL, "corpus.jsonl", earlier, replace=True)
        changed = write_blocks(self.files / "changed.jsonl", MIB, b"changed ")
        stale = ("f" * 64, MIB)  # the digest a first read would have given
        with patch.object(sources, "_file_digest", return_value=stale), \
                self.assertRaises(ValueError) as caught:
            sources.store(COLL, "corpus.jsonl", changed, replace=True)
        self.assertEqual(str(caught.exception), "Retained source changed while it was being "
                                                "retained; nothing was retained")
        # Nothing published under the wrong digest, no temporary file, and the
        # name released from the version it replaced, as for any failed write.
        self.assertFalse(sources.blob_path(COLL, "f" * 64).exists())
        self.assertEqual(self.leftovers(), [])
        self.assertEqual(sources.load_index(COLL)["documents"], {})
        self.assertFalse(sources.blob_path(COLL, kept).exists())

    def test_a_path_blob_is_fsynced_before_the_index_882(self):
        path = write_blocks(self.files / "synced.jsonl", 2 * MIB)
        real_fsync, synced = os.fsync, []

        def fsync(fd):
            synced.append(fd)
            return real_fsync(fd)

        with patch.object(sources.os, "fsync", side_effect=fsync):
            digest = sources.store(COLL, "synced.jsonl", path, replace=True)
        self.assertEqual(len(synced), 2)            # the blob, then the index
        self.assertEqual(file_sha256(sources.blob_path(COLL, digest)), digest)

    def test_an_unreadable_path_releases_its_name_880(self):
        old = sources.store(COLL, "corpus.jsonl", b"v1 text", replace=True)
        with self.assertRaises(OSError):
            sources.store(COLL, "corpus.jsonl", self.files / "missing.jsonl", replace=True)
        self.assertEqual(sources.digests_for_filename(COLL, "corpus.jsonl"), [])
        self.assertFalse(sources.blob_path(COLL, old).exists())

    def test_bytes_and_a_path_of_the_same_bytes_are_retained_alike(self):
        data = b'{"bill": "HR-1"}\n' * 1000
        path = self.files / "same.jsonl"
        path.write_bytes(data)
        by_bytes = sources.store("Bytes", "same.jsonl", data, "text/plain")
        by_path = sources.store("Path", "same.jsonl", path, "text/plain")
        self.assertEqual(by_bytes, by_path)
        self.assertEqual(by_bytes, hashlib.sha256(data).hexdigest())
        entries = [sources.load_index(c)["documents"][by_bytes] for c in ("Bytes", "Path")]
        for entry in entries:
            entry.pop("first_seen"), entry.pop("last_seen")
        self.assertEqual(entries[0], entries[1])
        self.assertEqual(sources.blob_path("Path", by_path).read_bytes(), data)


# ── S5: export and import of a large retained original ───────────────────────

class Backend:
    """A collection of a few chunks, read as Weaviate's paged iterator reads it."""

    def __init__(self, name):
        self.chunks = [SimpleNamespace(uuid=uuid.uuid4(), vector=[0.1, 0.2, 0.3], properties={
            "content": f"chunk {i}", "source_file": name, "source_type": "jsonl",
            "chunk_index": i, "chunk_strategy": "fixed", "chunk_size": 1000,
            "chunk_overlap": 0, "created_at": "2026-11-23T00:00:00Z"}) for i in range(2)]

    def iterator(self, include_vector=True, **_):
        yield from self.chunks

    def client(self):
        return SimpleNamespace(collections=SimpleNamespace(get=lambda name: self))


class ExportImportTests(Folders):
    def setUp(self):
        super().setUp()
        self.exports = self.root / "exports"
        self.exports.mkdir()
        backend = Backend("corpus.jsonl.gz")
        for change in (
                patch.object(packager, "exports_dir", return_value=self.exports),
                patch.object(packager.wc, "get_client", side_effect=backend.client),
                patch.object(packager.wc, "_collection_config_sync",
                             return_value={"embedding_model": settings.embed_model}),
                patch.object(packager.wc, "_meta_sync", return_value={}),
                patch.object(packager, "_export_retrieval_settings", return_value=({}, True)),
                patch.object(packager, "_ingest_config", return_value=None),
                patch.object(packager, "_goldstandard_sessions", return_value=[])):
            self.stack.enter_context(change)

    def test_a_large_original_survives_export_and_import_in_bounded_memory(self):
        original = write_blocks(self.files / "corpus.jsonl.gz", 48 * MIB)
        digest = sources.store(COLL, "corpus.jsonl.gz", original, "application/gzip", replace=True)

        job_id = uuid.uuid4().hex[:8]
        exporter._jobs[job_id] = {"job_id": job_id, "status": "queued", "collection": COLL,
                                  "chunks_written": 0, "warnings": [], "error": None}
        self.addCleanup(exporter._jobs.pop, job_id, None)
        peak, _ = traced_peak(exporter._run, job_id, COLL, False)
        job = exporter._jobs[job_id]
        self.assertEqual(job["status"], "completed", job.get("error"))
        self.assertLess(peak, 8 * MIB, f"export peak {peak} bytes")

        # The package's sources, extracted as import extracts them.
        package = self.root / "package"
        with tarfile.open(self.exports / job["filename"]) as tar:
            for member in tar.getmembers():
                rel = member.name.split("/", 1)[1] if "/" in member.name else ""
                if member.isfile() and rel.startswith("sources/"):
                    target = package / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as src, open(target, "wb") as out:
                        for block in iter(lambda: src.read(MIB), b""):
                            out.write(block)
        self.assertEqual(file_sha256(package / "sources" / digest), digest)
        index = json.loads((package / "sources" / "index.json").read_text())
        self.assertEqual(index["documents"][digest]["filenames"], ["corpus.jsonl.gz"])

        peak, _ = traced_peak(sources.restore_package, package, "Imported")
        self.assertEqual(file_sha256(sources.blob_path("Imported", digest)), digest)
        self.assertEqual(list(sources.load_index("Imported")["documents"]), [digest])
        self.assertLess(peak, 4 * MIB, f"restore peak {peak} bytes")


# ── S4: the re-chunk reader ───────────────────────────────────────────────────

def jsonl(records, width=180, seed="r"):
    return "".join(json.dumps({"t": f"{seed}{i:06d} " + "x" * width}) + "\n"
                   for i in range(records)).encode()


class ReaderTests(Folders):
    def setUp(self):
        super().setUp()
        self.stored = {}     # collection: the chunk names Weaviate is mocked to hold
        self.stack.enter_context(patch.object(wc, "get_client", return_value=SimpleNamespace(
            collections=SimpleNamespace(get=lambda name: SimpleNamespace(
                iterator=lambda **_: iter(self.stored.get(name, [])))))))

    def retain(self, files, collection=COLL):
        """Retain (name, bytes) pairs and mock the collection as storing their chunks."""
        for name, data in files:
            sources.store(collection, name, data, replace=True)
        self.stored[collection] = [SimpleNamespace(properties={"source_file": name, "chunk_index": 0})
                                   for name, _ in files]

    def reader(self, strategy="fixed", size=1000, collection=COLL):
        return tuning._chunks_from_sources(collection, strategy, size, 0, 0.85, 0)

    def work_folders(self):
        return sorted(Path(settings.upload_dir).glob("rechunk-*"))

    def reference(self, strategy="fixed", size=1000):
        """The chunks a re-chunk gives, built here from splitter.split_file in
        today's order: digests sorted, each digest's names in index order."""
        expected = []
        stage = self.root / "reference"
        documents = sources.load_index(COLL)["documents"]
        settings_ = splitter.Settings(strategy, size, 0, 0.85, 0)
        for n, (digest, entry) in enumerate(sorted(documents.items())):
            for m, name in enumerate(entry["filenames"]):
                copy = stage / f"{n}-{m}" / Path(name).name
                copy.parent.mkdir(parents=True)
                copy.write_bytes(sources.blob_path(COLL, digest).read_bytes())
                number = 0
                for kind, chunks in splitter.split_file(copy, settings_, _parse_file, tuning.do_chunk):
                    number += 1
                    source = name if kind == "whole" else parts.part_name(name, number)
                    expected.extend({"content": c, "source_file": source,
                                     "source_type": source_type_of(copy), "chunk_index": i,
                                     "chunk_strategy": strategy, "chunk_size": size,
                                     "chunk_overlap": 0} for i, c in enumerate(chunks))
        return expected

    def test_the_reader_gives_the_chunks_a_rechunk_gave(self):
        # copy.jsonl has bills.jsonl's bytes: one digest, two names.
        self.retain([("bills.jsonl", jsonl(40)), ("notes/a.txt", ("Alpha paragraph. " * 80 + "\n\n").encode() * 5),
                     ("copy.jsonl", jsonl(40)), ("corpus.jsonl.gz", gzip.compress(jsonl(60, seed="g")))])
        got = list(self.reader())
        self.assertEqual(len({c.pop("created_at") for c in got}), 1)
        self.assertEqual(got, self.reference())
        self.assertEqual(self.work_folders(), [])

    def test_a_split_file_is_given_part_by_part(self):
        # 13,000 records of about 600 characters: over 10,000 chunks, so parts.
        self.retain([("big.jsonl", jsonl(13_000, width=589))])
        produced = self.reader("context_aware", 1000).parts()
        first = next(produced)
        self.assertEqual({c["source_file"] for c in first}, {"big.jsonl (part 1)"})
        rest = list(produced)
        self.assertEqual([{c["source_file"] for c in p} for p in rest], [{"big.jsonl (part 2)"}])
        self.assertEqual(len(first) + sum(len(p) for p in rest), 13_000)

    def test_a_later_file_is_read_only_when_its_part_is_asked_for(self):
        self.retain([(f"f{i}.jsonl", jsonl(5, seed=f"f{i}")) for i in range(3)])
        calls = []
        real = splitter.split_file

        def split(path, *args):
            calls.append(path.name)
            return real(path, *args)

        with patch.object(tuning.splitter, "split_file", split):
            produced = self.reader().parts()
            next(produced)
            self.assertEqual(len(calls), 1)
            next(produced)
            self.assertEqual(len(calls), 2)
            produced.close()
        self.assertEqual(self.work_folders(), [])

    def test_memory_does_not_grow_with_the_number_of_files(self):
        def consume(count):
            collection = f"Files{count}"
            self.retain([(f"f{i:03d}.jsonl", jsonl(300, seed=f"f{i:03d}")) for i in range(count)],
                        collection)
            reader = self.reader(collection=collection)
            total = 0

            def run():
                nonlocal total
                for part in reader.parts():
                    total += len(part)   # staged, then dropped, as the rebuild does
            peak, _ = traced_peak(run)
            return peak, total

        few, few_total = consume(10)
        many, many_total = consume(40)
        self.assertEqual(many_total, 4 * few_total)
        self.assertLess(many - few, MIB, (few, many))

    def test_a_missing_blob_is_refused_before_anything_is_read(self):
        self.retain([("a.jsonl", jsonl(3, seed="a")), ("b.jsonl", jsonl(3, seed="b"))])
        last = max(sources.load_index(COLL)["documents"])
        sources.blob_path(COLL, last).unlink()
        with patch.object(tuning.splitter, "split_file") as split, \
                self.assertRaises(tuning.PackageError) as caught:
            self.reader()
        split.assert_not_called()
        self.assertEqual(caught.exception.code, "SOURCES_REQUIRED")
        self.assertEqual(caught.exception.message,
                         f"Retained source {last[:12]} is missing from disk, so '{COLL}' cannot "
                         "be rebuilt from its originals.")
        self.assertEqual(self.work_folders(), [])

    def test_a_blob_removed_after_the_check_fails_when_reached(self):
        self.retain([("a.jsonl", jsonl(3, seed="a")), ("b.jsonl", jsonl(3, seed="b"))])
        reader = self.reader()
        last = max(sources.load_index(COLL)["documents"])
        sources.blob_path(COLL, last).unlink()
        with self.assertRaises(tuning.PackageError) as caught:
            list(reader)
        self.assertEqual(caught.exception.message,
                         f"Retained source {last[:12]} is missing from disk, so '{COLL}' cannot "
                         "be rebuilt from its originals.")
        self.assertEqual(self.work_folders(), [])

    def test_a_part_failure_and_the_stored_text_cap_keep_their_messages(self):
        self.retain([("big.jsonl", jsonl(3))])
        first = [("part", ["one chunk"])]

        def failing_part(*_):
            yield from first
            raise splitter.PartError(2, ValueError("owned failure"))

        def too_large(*_):
            raise splitter.StoredTextTooLarge(5_000, 400)
            yield  # a generator, as split_file is

        for split, message in (
                (failing_part, "big.jsonl: part 2: owned failure"),
                (too_large, "big.jsonl: it would store 5,000 characters of chunks, more than "
                            f"{splitter.MAX_STORED_RATIO} times its own size (400 characters)")):
            with self.subTest(message=message), \
                    patch.object(tuning.splitter, "split_file", split), \
                    self.assertRaises(ValueError) as caught:
                list(self.reader())
            self.assertTrue(str(caught.exception).startswith(message), str(caught.exception))
            self.assertEqual(self.work_folders(), [])

    def test_no_chunks_is_refused_as_before(self):
        self.retain([("empty.jsonl", b"{}\n{}\n")])
        with self.assertRaises(tuning.PackageError) as caught:
            list(self.reader())
        self.assertEqual(caught.exception.message,
                         f"Re-chunking '{COLL}' produced no chunks; check the chunking parameters.")
        self.assertEqual(self.work_folders(), [])


# ── S4.6: the job's chunks_total follows staging ──────────────────────────────

class Reader:
    """Stands in for _SourceChunks: parts of 2, 3 and 1 chunks."""

    def __init__(self):
        self.sizes = (2, 3, 1)

    def parts(self):
        for size in self.sizes:
            yield [{"content": "x"}] * size

    def __iter__(self):
        for part in self.parts():
            yield from part


class JobRecordTests(unittest.TestCase):
    def run_job(self, operation, params, properties):
        job = {}
        seen = []

        def rebuild(collection, props, index_type, distance, progress, **kwargs):
            total = 0
            if "staged" in kwargs:
                for part in props.parts():
                    total += len(part)
                    kwargs["staged"](total)
                    seen.append(job["chunks_total"])
            else:
                seen.append(job["chunks_total"])
                total = len(props)
            progress(total)
            return total

        with patch.dict(tuning._jobs, {"job": job}), \
                patch.object(tuning.sources, "has_sources", return_value=True), \
                patch.object(tuning, "_chunks_from_sources", return_value=properties), \
                patch.object(tuning, "_existing_chunks", return_value=properties), \
                patch.object(tuning, "_rebuild", side_effect=rebuild):
            tuning._run("job", COLL, operation, params)
        return job, seen

    def test_a_rechunk_counts_its_chunks_as_they_are_staged(self):
        for operation, params in (("rechunk", {"chunking": {}}), ("reembed", {"chunking": {}})):
            with self.subTest(operation=operation):
                job, seen = self.run_job(operation, params, Reader())
                self.assertEqual(seen, [2, 5, 6])
                self.assertEqual(job["status"], "completed", job.get("error"))
                self.assertEqual((job["chunks_total"], job["chunks_written"]), (6, 6))

    def test_a_reembed_from_stored_chunks_knows_its_total_first(self):
        job, seen = self.run_job("reembed", {"chunking": None}, [{"content": "x"}] * 4)
        self.assertEqual(seen, [4])
        self.assertEqual((job["chunks_total"], job["chunks_written"]), (4, 4))


if __name__ == "__main__":
    unittest.main()
