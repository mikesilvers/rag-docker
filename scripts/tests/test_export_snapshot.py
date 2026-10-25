"""An export holds its collection's writer guard for the whole build (#509).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_export_snapshot.py

Drives `exporter._run` with the real `packager.build` and retained-source store
and a mocked Weaviate backend. Orderings are arranged with events and the
guard's own waiter count, not with sleeps.
"""
import asyncio
import hashlib
import json
import os
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from config import settings
from services import collection_writes as writes
from services import exporter, goldstandard, packager, sources
from services import weaviate_client as wc

COLL = "SnapshotColl"
OLD = b"old version of the document\n"
NEW = b"new version of the document\n"


class Stopped(BaseException):
    """A non-Exception that ends the build, as KeyboardInterrupt or SystemExit would."""


def wait_until(condition, timeout=3.0):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.005)
    return True


def waiters():
    """Owners and waiters of the collection's guard."""
    with writes._registry_lock:
        entry = writes._registry.get(writes.canonical(COLL))
        return entry[1] if entry else 0


def chunk(text, index):
    return SimpleNamespace(uuid=uuid.uuid4(), vector=[0.1, 0.2, 0.3], properties={
        "content": text, "source_file": "doc.txt", "source_type": "txt",
        "chunk_index": index, "chunk_strategy": "fixed", "chunk_size": 100,
        "chunk_overlap": 0, "created_at": "2026-10-24T00:00:00Z"})


class Backend:
    """A collection whose iterator reads the stored chunks lazily, one at a time,
    as Weaviate's paged iterator does, and calls `after_first` after the first."""

    def __init__(self, texts):
        self.chunks = [chunk(t, i) for i, t in enumerate(texts)]
        self.after_first = None

    def iterator(self, include_vector=True):
        position = 0
        while position < len(self.chunks):
            yield self.chunks[position]
            if position == 0 and self.after_first:
                self.after_first()
            position += 1

    def client(self):
        return SimpleNamespace(collections=SimpleNamespace(get=lambda name: self))


class ExportSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.exports = root / "exports"
        self.exports.mkdir()
        self.backend = Backend(["old chunk 0", "old chunk 1"])
        mocks = ExitStack()
        self.addCleanup(mocks.close)
        mocks.enter_context(patch.object(settings, "sources_dir", str(root / "retained")))
        mocks.enter_context(patch.object(packager, "exports_dir", return_value=self.exports))
        mocks.enter_context(patch.object(packager.wc, "get_client", side_effect=self.backend.client))
        mocks.enter_context(patch.object(packager.wc, "_collection_config_sync",
                                         return_value={"embedding_model": settings.embed_model}))
        mocks.enter_context(patch.object(packager.wc, "_meta_sync", return_value={}))
        mocks.enter_context(patch.object(packager, "_export_retrieval_settings",
                                         return_value=({}, True)))
        mocks.enter_context(patch.object(packager, "_ingest_config", return_value=None))
        mocks.enter_context(patch.object(packager, "_goldstandard_sessions", return_value=[]))
        self.old_digest = sources.store(COLL, "doc.txt", OLD, "text/plain", replace=True)

    def run_export(self):
        job_id = uuid.uuid4().hex[:8]
        exporter._jobs[job_id] = {"job_id": job_id, "status": "queued", "collection": COLL,
                                  "chunks_written": 0, "warnings": [], "error": None}
        self.addCleanup(exporter._jobs.pop, job_id, None)
        return job_id

    def package(self, job):
        self.assertEqual(job["status"], "completed", job.get("error"))
        with tarfile.open(self.exports / job["filename"]) as tar:
            files = {name.split("/", 1)[1]: tar.extractfile(name).read()
                     for name in tar.getnames() if tar.getmember(name).isfile()}
        records = [json.loads(line) for line in files["chunks.jsonl"].decode().splitlines()]
        return files, records

    def test_reingest_during_export_cannot_change_the_package(self):
        committed = threading.Event()
        built = threading.Event()
        order = []

        def reingest():
            # What an ingest of a changed doc.txt commits, under the same guard.
            with writes.guard(COLL):
                order.append("reingest after build" if built.is_set() else "reingest mid-export")
                self.backend.chunks = [chunk("new chunk 0", 0), chunk("new chunk 1", 1)]
                sources.store(COLL, "doc.txt", NEW, "text/plain", replace=True)
            committed.set()

        writer = threading.Thread(target=reingest)

        def after_first():
            writer.start()
            # Until the re-ingest waits on the guard or has committed.
            self.assertTrue(wait_until(lambda: waiters() >= 2 or committed.is_set()))

        self.backend.after_first = after_first
        real_build = packager.build

        def build(*args, **kwargs):
            result = real_build(*args, **kwargs)
            built.set()
            return result

        job_id = self.run_export()
        with patch.object(packager, "build", build):
            exporter._run(job_id, COLL, False)
        writer.join(timeout=5)
        self.assertTrue(committed.is_set())

        files, records = self.package(exporter._jobs[job_id])
        self.assertEqual([r["properties"]["content"] for r in records],
                         ["old chunk 0", "old chunk 1"])
        index = json.loads(files["sources/index.json"])
        self.assertEqual(set(index["documents"]), {self.old_digest})
        for record in records:
            digest = record["source_sha256"]
            self.assertEqual(digest, self.old_digest)
            blob = files[f"sources/{digest}"]
            self.assertEqual(hashlib.sha256(blob).hexdigest(), digest)
            self.assertEqual(blob, OLD)
        # The re-ingest landed once the export was done.
        self.assertEqual(order, ["reingest after build"])
        self.assertEqual(self.backend.chunks[0].properties["content"], "new chunk 0")

    def test_generation_during_export_is_refused_busy(self):
        outcome = {}

        def generate():
            try:
                outcome["result"] = goldstandard._prepare_generation_sync(COLL, 2, None)
            except goldstandard.GoldStandardError as exc:
                outcome["error"] = exc

        def after_first():
            thread = threading.Thread(target=generate)
            thread.start()
            thread.join(timeout=5)

        self.backend.after_first = after_first
        job_id = self.run_export()
        with patch.object(goldstandard.wc, "_sample_chunks_sync", return_value=[]), \
             patch.object(goldstandard, "_store_generated_session",
                          side_effect=lambda s: dict(s, session_id="gs_0000aaaa")), \
             patch.object(goldstandard, "register_job"):
            exporter._run(job_id, COLL, False)
        self.assertNotIn("result", outcome)
        self.assertEqual(outcome["error"].code, "COLLECTION_BUSY")
        self.assertEqual(outcome["error"].status, 409)
        self.package(exporter._jobs[job_id])

    def test_guarded_insert_waits_for_the_export(self):
        built = threading.Event()
        seen = []
        real_build = packager.build

        def build(*args, **kwargs):
            result = real_build(*args, **kwargs)
            built.set()
            return result

        def insert(*args, **kwargs):
            seen.append(built.is_set())

        writer = threading.Thread(target=wc._insert_chunks_sync, args=(COLL, [{"content": "x"}]))

        def after_first():
            writer.start()
            self.assertTrue(wait_until(lambda: waiters() >= 2 or seen))
            self.assertEqual(seen, [])

        self.backend.after_first = after_first
        job_id = self.run_export()
        with patch.object(packager, "build", build), \
             patch.object(wc.batch_write, "insert", side_effect=insert):
            exporter._run(job_id, COLL, False)
            writer.join(timeout=5)
        self.assertEqual(seen, [True])
        self.package(exporter._jobs[job_id])

    def test_export_waits_queued_behind_another_writer(self):
        job_id = self.run_export()
        runner = threading.Thread(target=exporter._run, args=(job_id, COLL, False))
        with writes.guard(COLL):
            runner.start()
            self.assertTrue(wait_until(lambda: waiters() >= 2))
            self.assertEqual(exporter._jobs[job_id]["status"], "queued")
        runner.join(timeout=5)
        self.package(exporter._jobs[job_id])

    def test_guard_released_after_success_failure_and_cancellation(self):
        outcomes = {
            "success": dict(return_value={
                "filename": "x.tar.gz", "size_bytes": 1, "chunk_count": 0,
                "source_document_count": 0, "fidelity": "chunks-only",
                "models_bundled": False, "retrieve_script": False, "warnings": []}),
            "refused": dict(side_effect=packager.PackageError("EMBEDDING_MISMATCH", "refused")),
            "failed": dict(side_effect=RuntimeError("build failed")),
            "cancelled": dict(side_effect=asyncio.CancelledError()),
            "stopped": dict(side_effect=Stopped()),
        }
        for name, behaviour in outcomes.items():
            with self.subTest(outcome=name):
                job_id = self.run_export()
                held = []

                def build(*args, _behaviour=behaviour, **kwargs):
                    held.append(waiters())
                    if "side_effect" in _behaviour:
                        raise _behaviour["side_effect"]
                    return _behaviour["return_value"]

                with patch.object(packager, "build", build):
                    if name in ("cancelled", "stopped"):
                        # The cancellation still propagates (#521).
                        with self.assertRaises(type(behaviour["side_effect"])):
                            exporter._run(job_id, COLL, False)
                    else:
                        exporter._run(job_id, COLL, False)
                self.assertEqual(held, [1])
                self.assertEqual(waiters(), 0)
                acquired = []

                def other_writer():
                    with writes.guard(COLL, timeout=0.5):
                        acquired.append(True)

                thread = threading.Thread(target=other_writer)
                thread.start()
                thread.join(timeout=5)
                self.assertEqual(acquired, [True])
                job = exporter._jobs[job_id]
                self.assertEqual(job["status"], "completed" if name == "success" else "failed")
                if name in ("cancelled", "stopped"):
                    # A terminal status, not `running` for the rest of the process (#521).
                    kind = type(behaviour["side_effect"]).__name__
                    self.assertEqual(job["error"], f"{kind}: the export was cancelled before "
                                     "it finished. Start the export again.")
                    self.assertIsNone(job.get("error_code"))
                    self.assertIsNone(job.get("error_detail"))


if __name__ == "__main__":
    unittest.main()
