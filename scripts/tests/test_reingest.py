"""Re-ingesting replaces a name's chunks; same-named inputs don't collide (issue #22).

Runs with the API's dependencies, inside the verify project's API container:

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_reingest.py

Weaviate and gold-standard sessions are mocked; nothing reaches a backend.
"""
import asyncio
import contextlib
import hashlib
import io
import json
import logging
import os
import shutil
import sys
import tempfile
import threading
import unittest
import uuid
import warnings
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi import UploadFile

from config import settings
from models.schemas import JobStatusResponse
from routers import ingest
from services import goldstandard, ingest_pipeline, sources, tuning
from services import weaviate_client as wc


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z, warnings.catch_warnings():
        # writestr warns on a repeated name; repeating is the point here.
        warnings.simplefilter("ignore", UserWarning)
        for name, data in members:
            z.writestr(zipfile.ZipInfo(name), data)
    return buf.getvalue()


def upload(name, data):
    return UploadFile(file=io.BytesIO(data if isinstance(data, bytes) else data.encode()),
                      filename=name)


class AdmissionTests(unittest.TestCase):
    """What an upload queues: one file per logical name, the last one winning."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for change in (patch.object(settings, "upload_dir", temp.name),
                       patch.object(wc, "collection_exists", AsyncMock(return_value=True))):
            change.start(); self.addCleanup(change.stop)

    def admit(self, *files):
        started = {}
        with patch.object(ingest_pipeline, "_process_job_sync",
                          lambda *args: started.setdefault("args", args)):
            response = asyncio.run(ingest.ingest_upload(
                collection="Coll", strategy="fixed", chunk_size="200", chunk_overlap="0",
                similarity_threshold="0.85", min_chunk_size="0", files=list(files)))
        self.assertFalse(hasattr(response, "status_code"), getattr(response, "body", b""))
        job = ingest_pipeline.get_job(response.job_id)
        self.addCleanup(ingest_pipeline._jobs.pop, response.job_id, None)
        paths, labels, names = started["args"][1], started["args"][-2], started["args"][-1]
        queued = [(names[p], labels[p], p.read_text()) for p in paths]
        return job, queued, paths

    def test_same_name_twice_in_one_body_keeps_the_last_bytes(self):
        job, queued, _ = self.admit(upload("r.txt", "first"), upload("r.txt", "second"))
        self.assertEqual(queued, [("r.txt", "r.txt", "second")])
        self.assertEqual(job["files_total"], 1)
        self.assertEqual(job["chunks_replaced"], 0)
        self.assertEqual(job["skipped"],
                         ["r.txt (replaced by r.txt later in this upload, same name 'r.txt')"])

    def test_repeated_zip_entries_keep_the_last(self):
        job, queued, _ = self.admit(upload("u.zip", zip_bytes(
            [("a/r.txt", "one"), ("a/r.txt", "two")])))
        self.assertEqual(queued, [("a/r.txt", "u.zip:a/r.txt", "two")])
        self.assertEqual(job["skipped"], [
            "u.zip:a/r.txt (replaced by u.zip:a/r.txt later in this upload, same name 'a/r.txt')"])

    def test_same_path_in_two_zips_is_a_collision(self):
        job, queued, _ = self.admit(upload("one.zip", zip_bytes([("docs/r.txt", "one")])),
                                    upload("two.zip", zip_bytes([("docs/r.txt", "two")])))
        self.assertEqual(queued, [("docs/r.txt", "two.zip:docs/r.txt", "two")])
        self.assertEqual(len(job["skipped"]), 1)
        self.assertTrue(job["skipped"][0].startswith("one.zip:docs/r.txt (replaced by two.zip:docs/r.txt"))

    def test_direct_upload_and_top_level_member_share_a_name(self):
        job, queued, _ = self.admit(upload("u.zip", zip_bytes([("r.txt", "zipped")])),
                                    upload("r.txt", "direct"))
        self.assertEqual(queued, [("r.txt", "r.txt", "direct")])
        self.assertEqual(job["skipped"],
                         ["u.zip:r.txt (replaced by r.txt later in this upload, same name 'r.txt')"])

    def test_folders_inside_a_zip_keep_documents_apart(self):
        job, queued, paths = self.admit(upload("u.zip", zip_bytes(
            [("a/report.txt", "alpha"), ("b/report.txt", "beta"), ("report.txt", "top")])))
        self.assertEqual(sorted(queued), [("a/report.txt", "u.zip:a/report.txt", "alpha"),
                                          ("b/report.txt", "u.zip:b/report.txt", "beta"),
                                          ("report.txt", "u.zip:report.txt", "top")])
        self.assertEqual(job["skipped"], [])
        # Each in a directory of its own, under its own basename for the parser.
        self.assertEqual({p.name for p in paths}, {"report.txt"})
        self.assertEqual(len({p.parent for p in paths}), 3)

    def test_internal_dot_dot_is_normalised_and_its_bytes_are_queued(self):
        # ZipFile.extract would write a/../b.txt to a/b.txt; the queue named b.txt,
        # a file that did not exist.
        job, queued, paths = self.admit(upload("u.zip", zip_bytes(
            [("a/../b.txt", "dotted"), ("./c.txt", "dot")])))
        self.assertEqual(sorted(queued), [("b.txt", "u.zip:a/../b.txt", "dotted"),
                                          ("c.txt", "u.zip:./c.txt", "dot")])
        self.assertTrue(all(p.is_file() for p in paths))

    def test_normalised_name_collides_with_its_plain_spelling(self):
        job, queued, _ = self.admit(upload("u.zip", zip_bytes(
            [("b.txt", "plain"), ("a/../b.txt", "dotted")])))
        self.assertEqual(queued, [("b.txt", "u.zip:a/../b.txt", "dotted")])
        self.assertEqual(job["skipped"], [
            "u.zip:b.txt (replaced by u.zip:a/../b.txt later in this upload, same name 'b.txt')"])

    def test_members_leaving_the_archive_are_still_dropped(self):
        job, queued, _ = self.admit(upload("u.zip", zip_bytes(
            [("../out.txt", "x"), ("/abs.txt", "y"), ("a/../../up.txt", "z"), ("in.txt", "ok")])))
        self.assertEqual(queued, [("in.txt", "u.zip:in.txt", "ok")])
        # Never extracted, but no longer dropped without a word (#28).
        leaves = "(its path leaves the archive, so it was not extracted)"
        self.assertEqual(job["skipped"], [f"u.zip:../out.txt {leaves}", f"u.zip:/abs.txt {leaves}",
                                          f"u.zip:a/../../up.txt {leaves}"])

    def test_a_dropped_input_leaves_no_staged_file(self):
        _, _, paths = self.admit(upload("r.txt", "first"), upload("r.txt", "second"))
        staged = [p for p in Path(paths[0]).parents[1].rglob("*") if p.is_file()]
        self.assertEqual(staged, paths)

    # A Finder "Compress" ZIP (#378): each file's resource fork and extended
    # attributes as an AppleDouble file under __MACOSX/, keeping the file's suffix.
    APPLEDOUBLE = b"\x00\x05\x16\x07\x00\x02\x00\x00" + b"Mac OS X        " + b"\x00" * 58
    MACOS_SKIP = "(macOS metadata (AppleDouble), not a document)"

    def test_finder_zip_queues_the_file_not_its_appledouble_member(self):
        job, queued, _ = self.admit(upload("finder.zip", zip_bytes(
            [("docs/", ""), ("docs/report.txt", "the real report"), ("__MACOSX/", ""),
             ("__MACOSX/docs/", ""), ("__MACOSX/docs/._report.txt", self.APPLEDOUBLE)])))
        self.assertEqual(queued, [("docs/report.txt", "finder.zip:docs/report.txt",
                                   "the real report")])
        self.assertEqual(job["files_total"], 1)
        self.assertEqual(job["skipped"],
                         [f"finder.zip:__MACOSX/docs/._report.txt {self.MACOS_SKIP}"])

    def test_bare_dot_underscore_and_other_macosx_members_are_skipped(self):
        # ditto and copies to non-Mac volumes write ._<name> beside the file;
        # under __MACOSX/ the macOS reason wins over an unsupported suffix.
        job, queued, _ = self.admit(upload("u.zip", zip_bytes(
            [("notes.md", "notes"), ("._notes.md", self.APPLEDOUBLE),
             ("a/._b.pdf", self.APPLEDOUBLE), ("__MACOSX/._tool.exe", self.APPLEDOUBLE),
             ("__MACOSX/readme.txt", "x")])))
        self.assertEqual(queued, [("notes.md", "u.zip:notes.md", "notes")])
        self.assertEqual(job["skipped"], [
            f"u.zip:._notes.md {self.MACOS_SKIP}", f"u.zip:a/._b.pdf {self.MACOS_SKIP}",
            f"u.zip:__MACOSX/._tool.exe {self.MACOS_SKIP}",
            f"u.zip:__MACOSX/readme.txt {self.MACOS_SKIP}"])

    def test_names_that_only_look_like_macos_metadata_are_queued(self):
        names = ["_report.txt", "notes._v2.txt", ".hidden.txt", "__MACOSX_notes.txt",
                 "docs/__MACOSX.txt", "docs/__MACOSX/readme.txt", "__macosx/lower.txt"]
        job, queued, _ = self.admit(upload("u.zip", zip_bytes([(n, n) for n in names])))
        self.assertEqual(sorted(name for name, _, _ in queued), sorted(names))
        self.assertEqual(job["skipped"], [])

    def test_zip_of_only_macos_metadata_has_no_supported_files(self):
        response = asyncio.run(ingest.ingest_upload(
            collection="Coll", strategy="fixed", chunk_size="200", chunk_overlap="0",
            similarity_threshold="0.85", min_chunk_size="0",
            files=[upload("only.zip", zip_bytes([("__MACOSX/._r.txt", self.APPLEDOUBLE)]))]))
        self.assertEqual(response.status_code, 400)
        error = json.loads(response.body)["error"]
        self.assertEqual(error["code"], "NO_SUPPORTED_FILES")
        self.assertIn(f"only.zip:__MACOSX/._r.txt {self.MACOS_SKIP}", error["message"])

    def refused(self, *files):
        response = asyncio.run(ingest.ingest_upload(
            collection="Coll", strategy="fixed", chunk_size="200", chunk_overlap="0",
            similarity_threshold="0.85", min_chunk_size="0", files=list(files)))
        self.assertEqual(getattr(response, "status_code", None), 400,
                         "expected a refusal, got a job")
        return json.loads(response.body)["error"]

    def test_a_damaged_macos_metadata_member_still_refuses_the_zip(self):
        # Validation is unchanged (#378): metadata is read like any skipped member.
        payload = b"z" * 100
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
            z.writestr("report.txt", "the real report")
            z.writestr("__MACOSX/._report.txt", payload)
        data = bytearray(buf.getvalue())
        data[data.index(payload)] ^= 0x01   # bad CRC in the AppleDouble member
        error = self.refused(upload("damaged.zip", bytes(data)))
        self.assertEqual(error["code"], "ARCHIVE_UNREADABLE")
        self.assertEqual([m["member"] for m in error["detail"]["members"]],
                         ["__MACOSX/._report.txt"])

    def test_macos_metadata_counts_toward_the_expansion_cap(self):
        # The AppleDouble member's bytes still count (#298, #378).
        data = zip_bytes([("report.txt", "r" * 100), ("__MACOSX/._report.txt", b"m" * 200)])
        with patch.object(settings, "max_zip_expanded_bytes", 250):
            error = self.refused(upload("big.zip", data))
        self.assertEqual(error["code"], "ARCHIVE_TOO_LARGE")
        with patch.object(settings, "max_zip_expanded_bytes", 300):
            job, queued, _ = self.admit(upload("big.zip", data))
        self.assertEqual([name for name, _, _ in queued], ["report.txt"])

    def test_normalised_metadata_paths_are_skipped_under_their_archive_spelling(self):
        # The match is on the logical (normalised) name; skipped keeps the raw name.
        job, queued, _ = self.admit(upload("n.zip", zip_bytes(
            [("r.txt", "real"), ("./__MACOSX/._r.txt", self.APPLEDOUBLE),
             ("x/../__MACOSX/notes.txt", "x"), ("d/./._e.txt", self.APPLEDOUBLE)])))
        self.assertEqual(queued, [("r.txt", "n.zip:r.txt", "real")])
        self.assertEqual(job["skipped"], [
            f"n.zip:./__MACOSX/._r.txt {self.MACOS_SKIP}",
            f"n.zip:x/../__MACOSX/notes.txt {self.MACOS_SKIP}",
            f"n.zip:d/./._e.txt {self.MACOS_SKIP}"])

    def test_a_direct_upload_named_like_appledouble_is_still_ingested(self):
        # Only ZIP members are filtered; direct uploads are unchanged (#378).
        job, queued, _ = self.admit(upload("._notes.txt", "a direct upload"))
        self.assertEqual(queued, [("._notes.txt", "._notes.txt", "a direct upload")])
        self.assertEqual(job["skipped"], [])


class FakeCollection:
    """Chunks by name, with the replacement helpers' contract. `accept`, as the
    real helpers take it, selects a name's family (#621); without it, the name."""

    def __init__(self, **existing):
        self.chunks = {name: [{"content": c, "created_at": "old"} for c in contents]
                       for name, contents in existing.items()}
        self.calls = []

    def selected(self, name, accept=None):
        return [n for n in self.chunks if (accept(n) if accept else n == name)]

    def has(self, collection, name, accept=None):
        self.calls.append(("has", name))
        return any(self.chunks[n] for n in self.selected(name, accept))

    def insert(self, collection, chunks):
        self.calls.append(("insert", chunks[0]["source_file"] if chunks else None))
        for chunk in chunks:
            self.chunks.setdefault(chunk["source_file"], []).append(chunk)

    def delete(self, collection, name, created_at, expected, accept=None):
        self.calls.append(("delete", name))
        stamp = created_at.isoformat()
        names = self.selected(name, accept)
        mine = {n: [c for c in self.chunks[n] if c["created_at"] == stamp] for n in names}
        if sum(len(m) for m in mine.values()) != expected:
            raise wc.ReplaceError("mismatch", 0)
        removed = sum(len(self.chunks[n]) - len(mine[n]) for n in names)
        for n in names:
            self.chunks[n] = mine[n]
        return removed


class WorkerTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.stored = []
        self.marks = []

    def run_job(self, backend, files, *, mark=None, delete=None, store=None):
        paths, names, labels = [], {}, {}
        for i, (name, text) in enumerate(files):
            path = self.root / str(i) / Path(name).name
            path.parent.mkdir(); path.write_text(text)
            paths.append(path); names[path] = name; labels[path] = f"up.zip:{name}"
        job_id = "r" + uuid.uuid4().hex[:7]
        ingest_pipeline._jobs[job_id] = {
            "job_id": job_id, "status": "queued", "files_total": len(paths),
            "files_completed": 0, "files_failed": 0, "chunks_stored": 0,
            "chunks_replaced": 0, "errors": [], "skipped": []}
        self.addCleanup(ingest_pipeline._jobs.pop, job_id, None)

        def default_mark(collection, reason, require_durable=False):
            self.marks.append((collection, reason, require_durable))
            backend.calls.append(("mark", reason))
            return 1

        def default_store(collection, name, data, media_type=None, *, replace=False):
            self.stored.append((name, data, replace))

        with patch.object(wc, "_source_has_chunks_sync", side_effect=backend.has), \
                patch.object(wc, "_insert_chunks_sync", side_effect=backend.insert), \
                patch.object(wc, "_delete_earlier_chunks_sync", side_effect=delete or backend.delete), \
                patch.object(goldstandard, "mark_stale", side_effect=mark or default_mark), \
                patch.object(ingest_pipeline.sources, "store", side_effect=store or default_store), \
                patch.object(ingest_pipeline, "do_chunk", side_effect=lambda text, **_: text.split()):
            ingest_pipeline._process_job_sync(job_id, paths, self.root, "Coll", "fixed", 200, 0,
                                              0.85, 0, labels, names)
        return ingest_pipeline._jobs[job_id]

    def test_new_documents_mark_nothing_and_replace_nothing(self):
        backend = FakeCollection()
        job = self.run_job(backend, [("a/r.txt", "one two"), ("b/r.txt", "three")])
        self.assertEqual((job["status"], job["files_completed"], job["chunks_stored"],
                          job["chunks_replaced"]), ("completed", 2, 3, 0))
        self.assertEqual(self.marks, [])
        self.assertNotIn("delete", [c[0] for c in backend.calls])
        self.assertEqual(sorted(backend.chunks), ["a/r.txt", "b/r.txt"])
        self.assertEqual([(n, r) for n, _, r in self.stored], [("a/r.txt", True), ("b/r.txt", True)])

    def test_a_replacement_marks_once_then_inserts_before_deleting(self):
        backend = FakeCollection(**{"p.txt": ["v1a", "v1b", "v1c"], "q.txt": ["old"]})
        job = self.run_job(backend, [("p.txt", "v2a v2b"), ("new.txt", "n"), ("q.txt", "q2")])
        self.assertEqual((job["status"], job["files_completed"], job["chunks_replaced"]),
                         ("completed", 3, 4))
        self.assertEqual([c["content"] for c in backend.chunks["p.txt"]], ["v2a", "v2b"])
        self.assertEqual(len(self.marks), 1)
        collection, reason, durable = self.marks[0]
        self.assertEqual((collection, durable), ("Coll", True))
        self.assertEqual(reason, "documents were re-ingested, replacing their earlier chunks: p.txt, q.txt")
        steps = [c[0] for c in backend.calls if c[0] != "has"]
        self.assertEqual(steps, ["mark", "insert", "delete", "insert", "insert", "delete"])

    def real_sources(self):
        """Retention into a temporary sources folder, through the real store."""
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        change = patch.object(settings, "sources_dir", temp.name)
        change.start(); self.addCleanup(change.stop)
        return sources.store

    def unreadable(self):
        """Path.read_bytes failing, as only retention reads a file that way."""
        def read_bytes(path):
            raise OSError(f"cannot read {path.name}")
        return patch.object(Path, "read_bytes", read_bytes)

    def test_a_reupload_that_cant_be_read_releases_its_name_880(self):
        # #880: the read failed before sources.store ran, so the index kept the
        # name on the old version while the collection held the new chunks.
        store = self.real_sources()
        old = store("Coll", "p.txt", b"v1 text", replace=True)
        backend = FakeCollection(**{"p.txt": ["v1"]})
        with self.unreadable(), self.assertLogs(ingest_pipeline._log, "ERROR") as logged:
            job = self.run_job(backend, [("p.txt", "v2a v2b")], store=store)
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"]),
                         ("completed", 1, 0))
        self.assertIn("Could not retain source 'p.txt' for 'Coll': cannot read p.txt",
                      "\n".join(logged.output))
        self.assertEqual(sources.digests_for_filename("Coll", "p.txt"), [])
        self.assertFalse(sources.blob_path("Coll", old).exists())
        self.assertEqual([c["content"] for c in backend.chunks["p.txt"]], ["v2a", "v2b"])

    def test_a_failed_release_is_logged_and_the_file_still_counts_880(self):
        store = self.real_sources()
        old = store("Coll", "p.txt", b"v1 text", replace=True)
        backend = FakeCollection(**{"p.txt": ["v1"]})
        with self.unreadable(), \
                patch.object(sources, "_save_index", side_effect=OSError("read-only")), \
                self.assertLogs(sources.log, "ERROR") as released, \
                self.assertLogs(ingest_pipeline._log, "ERROR") as retained:
            job = self.run_job(backend, [("p.txt", "v2a")], store=store)
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"]),
                         ("completed", 1, 0))
        self.assertIn("a re-chunk could rebuild that earlier version", "\n".join(released.output))
        self.assertIn("cannot read p.txt", "\n".join(retained.output))
        # Best effort: the index couldn't be saved, so it still names the old version.
        self.assertEqual(sources.digests_for_filename("Coll", "p.txt"), [old])

    def test_unchanged_content_is_still_replaced(self):
        backend = FakeCollection(**{"p.txt": ["same", "words"]})
        job = self.run_job(backend, [("p.txt", "same words")])
        self.assertEqual((job["status"], job["chunks_stored"], job["chunks_replaced"]),
                         ("completed", 2, 2))
        self.assertEqual(len(backend.chunks["p.txt"]), 2)

    def test_a_marking_failure_fails_only_that_file_and_changes_nothing(self):
        backend = FakeCollection(**{"p.txt": ["v1"], "q.txt": ["old"]})
        attempts = []

        def mark(collection, reason, require_durable=False):
            attempts.append(reason)
            if len(attempts) == 1:
                raise goldstandard.GoldStandardError("SESSION_WRITE_FAILED", "disk full", 503)
            return 0

        with self.assertLogs("services.ingest_pipeline", "WARNING"):
            job = self.run_job(backend, [("p.txt", "v2"), ("q.txt", "q2")], mark=mark)
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"]),
                         ("partial", 1, 1))
        self.assertEqual(job["errors"], [
            "up.zip:p.txt: could not mark gold-standard sessions stale before replacing its "
            "chunks, so nothing was changed: disk full"])
        self.assertEqual([c["content"] for c in backend.chunks["p.txt"]], ["v1"])
        # The next replacing file tries the marking again.
        self.assertEqual(len(attempts), 2)
        self.assertEqual([c["content"] for c in backend.chunks["q.txt"]], ["q2"])
        self.assertEqual([n for n, _, _ in self.stored], ["q.txt"])

    def test_a_failed_insert_keeps_the_earlier_version(self):
        backend = FakeCollection(**{"p.txt": ["v1"]})

        def insert(collection, chunks):
            raise RuntimeError("Weaviate rejected 1 batch object(s)")

        with patch.object(wc, "_collection_exists_sync", return_value=True), \
                self.assertLogs("services.ingest_pipeline", "WARNING"):
            backend.insert = insert
            job = self.run_job(backend, [("p.txt", "v2")])
        self.assertEqual(job["status"], "failed")
        self.assertEqual([c["content"] for c in backend.chunks["p.txt"]], ["v1"])
        self.assertNotIn("delete", [c[0] for c in backend.calls])
        self.assertEqual(self.stored, [])

    def test_a_removal_failure_keeps_new_chunks_and_reports_partial(self):
        backend = FakeCollection(**{"p.txt": ["v1", "v1b"]})

        def delete(collection, name, created_at, expected, accept=None):
            raise wc.ReplaceError("1 earlier chunk(s) remain", 1)

        with self.assertLogs("services.ingest_pipeline", "ERROR"):
            job = self.run_job(backend, [("p.txt", "v2"), ("n.txt", "n")], delete=delete)
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"],
                          job["chunks_stored"], job["chunks_replaced"]), ("partial", 1, 1, 2, 1))
        self.assertEqual(job["errors"], [
            "up.zip:p.txt: its new chunks were stored, but earlier chunks could not all be "
            "removed (1 earlier chunk(s) remain); upload it again to retry"])
        # Retention still follows the new version.
        self.assertEqual([(n, r) for n, _, r in self.stored], [("p.txt", True), ("n.txt", True)])

    def test_a_deleted_collection_is_reported_at_the_lookup(self):
        backend = FakeCollection()

        def has(collection, name, accept=None):
            raise RuntimeError("could not find class Coll in schema")

        backend.has = has
        with patch.object(wc, "_collection_exists_sync", return_value=False), \
                self.assertLogs("services.ingest_pipeline", "WARNING"):
            job = self.run_job(backend, [("p.txt", "v2"), ("q.txt", "q")])
        self.assertEqual((job["status"], job["files_failed"]), ("failed", 2))
        self.assertEqual(job["errors"], ["collection 'Coll' was deleted while this job was running"])

    def test_a_failed_lookup_is_reported_as_the_lookup(self):
        # #311: the lookup of the job's later files is not the marking, so its
        # failure is named as the lookup; the file still changes nothing.
        backend = FakeCollection(**{"p.txt": ["v1"], "q.txt": ["old"]})
        lookups = []
        has = backend.has

        def flaky_has(collection, name, accept=None):
            lookups.append(name)
            if name == "q.txt" and lookups.count("q.txt") == 1:
                raise RuntimeError("weaviate read timed out")
            return has(collection, name, accept)

        backend.has = flaky_has
        with self.assertLogs("services.ingest_pipeline", "WARNING"):
            job = self.run_job(backend, [("p.txt", "v2"), ("q.txt", "q2")])
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"]),
                         ("partial", 1, 1))
        self.assertEqual(job["errors"], [
            "up.zip:p.txt: could not check which later files in this upload replace earlier "
            "chunks, so nothing was changed: weaviate read timed out"])
        self.assertEqual([c["content"] for c in backend.chunks["p.txt"]], ["v1"])
        # p.txt marked nothing and retained nothing; q.txt then marks and replaces.
        self.assertEqual([r for _, r, _ in self.marks],
                         ["documents were re-ingested, replacing their earlier chunks: q.txt"])
        self.assertEqual([c["content"] for c in backend.chunks["q.txt"]], ["q2"])
        self.assertEqual([n for n, _, _ in self.stored], ["q.txt"])

    def test_reason_lists_ten_names_then_a_count(self):
        names = {f"f{i:02}.txt": ["old"] for i in range(12)}
        backend = FakeCollection(**names)
        self.run_job(backend, [(n, "new") for n in sorted(names)])
        self.assertEqual(len(self.marks), 1)
        self.assertTrue(self.marks[0][1].endswith("f08.txt, f09.txt and 2 more"), self.marks[0][1])


class FakeSourceCollection:
    """A collection whose word filter matches every stored object, a valid
    superset of any name, as `source_file`'s word filter is in Weaviate (#622).
    `count` overrides the superset size to force a path."""

    def __init__(self, objects, count=None, fail=None, remove=True, also_remove=()):
        self.objects = list(objects)
        self.count, self.fail, self.remove, self.also_remove = count, dict(fail or {}), remove, set(also_remove)
        self.calls, self.deleted_batches, self.taken = [], [], 0
        self.aggregate = SimpleNamespace(over_all=self.over_all)
        self.query = SimpleNamespace(fetch_objects=self.fetch_objects)
        self.data = SimpleNamespace(delete_many=self.delete_many)

    def over_all(self, filters=None, total_count=False):
        self.calls.append(("count", None))
        return SimpleNamespace(total_count=len(self.objects) if self.count is None else self.count)

    def fetch_objects(self, filters=None, limit=None, include_vector=None, return_properties=None):
        self.calls.append(("fetch", (limit, include_vector, tuple(return_properties))))
        return SimpleNamespace(objects=list(self.objects[:limit]))

    def iterator(self, include_vector=None, return_properties=None, cache_size=None):
        self.calls.append(("walk", (include_vector, tuple(return_properties), cache_size)))
        for obj in list(self.objects):
            self.taken += 1
            yield obj

    def delete_many(self, where=None):
        ids = set(where.value)
        self.calls.append(("delete", len(ids)))
        self.deleted_batches.append(sorted(ids))
        failed = self.fail.get(len(self.deleted_batches), 0)
        if self.remove:
            gone = ids | self.also_remove
            self.objects = [o for o in self.objects if str(o.uuid) not in gone]
        return SimpleNamespace(successful=len(ids) - failed, failed=failed, matches=len(ids))

    def kinds(self):
        return [kind for kind, _ in self.calls]


WHEN = datetime(2026, 10, 18, tzinfo=timezone.utc)
EARLIER = datetime(2026, 10, 1, tzinfo=timezone.utc)


def stored(name, when, n):
    return [SimpleNamespace(uuid=uuid.uuid4(), properties={"source_file": name, "created_at": when})
            for _ in range(n)]


class ExactSourceTests(unittest.TestCase):
    """Replacing and looking up a name touch exactly that name (#22, #622)."""

    def others(self):
        return (stored("a/report.pdf", EARLIER, 2) + stored("Report.PDF", EARLIER, 2)
                + stored("report.pdf (part 1)", EARLIER, 2))

    def replace(self, coll, expected=3):
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: coll))
        with patch.object(wc, "get_client", return_value=client):
            try:
                return wc._delete_earlier_chunks_sync("Coll", "report.pdf", WHEN, expected)
            except wc.ReplaceError as exc:
                return exc

    def has(self, coll, name="report.pdf"):
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: coll))
        with patch.object(wc, "get_client", return_value=client):
            return wc._source_has_chunks_sync("Coll", name)

    def names(self, coll):
        return sorted(o.properties["source_file"] for o in coll.objects)

    def test_only_the_exact_names_earlier_chunks_are_deleted(self):
        others = self.others()
        earlier, new = stored("report.pdf", EARLIER, 2), stored("report.pdf", WHEN, 3)
        coll = FakeSourceCollection(others + earlier + new)
        self.assertEqual(self.replace(coll), 2)
        self.assertEqual(coll.deleted_batches, [sorted(str(o.uuid) for o in earlier)])
        self.assertEqual(coll.objects, others + new)
        for kind, args in coll.calls:
            if kind == "fetch":
                self.assertEqual(args, (wc._QUERY_MAXIMUM_RESULTS, False, ("source_file", "created_at")))

    def test_a_before_check_mismatch_deletes_nothing(self):
        coll = FakeSourceCollection(self.others() + stored("report.pdf", EARLIER, 2)
                                    + stored("report.pdf", WHEN, 2))
        exc = self.replace(coll)
        self.assertIsInstance(exc, wc.ReplaceError)
        self.assertEqual(exc.deleted, 0)
        self.assertIn("found 2 of its 3 new chunks", str(exc))
        self.assertNotIn("delete", coll.kinds())

    def test_other_names_with_the_new_timestamp_are_not_counted(self):
        # Before #622 the word filter counted them: 5 "new" chunks for 3 expected.
        coll = FakeSourceCollection(stored("a/report.pdf", WHEN, 2) + stored("report.pdf", WHEN, 3))
        self.assertEqual(self.replace(coll), 0)
        self.assertNotIn("delete", coll.kinds())
        self.assertEqual(len(coll.objects), 5)

    def test_deletes_in_batches_never_empty(self):
        coll = FakeSourceCollection(stored("report.pdf", EARLIER, 5) + stored("report.pdf", WHEN, 3))
        with patch.object(wc, "_QUERY_MAXIMUM_RESULTS", 2):
            self.assertEqual(self.replace(coll), 5)
        self.assertEqual([len(b) for b in coll.deleted_batches], [2, 2, 1])
        coll = FakeSourceCollection(self.others() + stored("report.pdf", WHEN, 3))
        self.assertEqual(self.replace(coll), 0)
        self.assertNotIn("delete", coll.kinds())

    def test_a_failed_delete_stops_with_the_count_so_far(self):
        coll = FakeSourceCollection(stored("report.pdf", EARLIER, 5) + stored("report.pdf", WHEN, 3),
                                    fail={2: 1})
        with patch.object(wc, "_QUERY_MAXIMUM_RESULTS", 2):
            exc = self.replace(coll)
        self.assertEqual((exc.deleted, str(exc)), (3, "Weaviate failed to delete 1 earlier chunk(s)"))
        self.assertEqual(len(coll.deleted_batches), 2)

    def test_leftover_and_missing_chunks_are_reported(self):
        new = stored("report.pdf", WHEN, 3)
        coll = FakeSourceCollection(self.others() + stored("report.pdf", EARLIER, 2) + new, remove=False)
        exc = self.replace(coll)
        self.assertEqual((exc.deleted, str(exc)), (2, "2 earlier chunk(s) remain"))
        coll = FakeSourceCollection(self.others() + stored("report.pdf", EARLIER, 2) + new,
                                    also_remove={str(new[0].uuid)})
        exc = self.replace(coll)
        self.assertEqual((exc.deleted, str(exc)), (2, "1 of its 3 new chunks are missing afterwards"))

    def test_the_bound_refuses_before_deleting(self):
        coll = FakeSourceCollection(stored("report.pdf", EARLIER, 3) + stored("report.pdf", WHEN, 3))
        with patch.object(wc, "_QUERY_MAXIMUM_RESULTS", 2), patch.object(wc, "_REPLACE_DELETE_ROUNDS", 1):
            exc = self.replace(coll)
        self.assertEqual((exc.deleted, str(exc)), (
            0, "3 earlier chunks are more than one replacement removes (2), so none were removed"))
        self.assertNotIn("delete", coll.kinds())

    def test_the_path_follows_the_word_match_count(self):
        objects = self.others() + stored("report.pdf", WHEN, 3)
        coll = FakeSourceCollection(objects, count=10_001)
        self.assertEqual(self.replace(coll), 0)
        self.assertEqual(coll.kinds(), ["count", "walk", "count", "walk"])
        # The walk reads pages as large as one query returns, not the client's 100 (#628).
        self.assertEqual(coll.calls[1][1], (False, ("source_file", "created_at"), wc._QUERY_MAXIMUM_RESULTS))
        coll = FakeSourceCollection(objects, count=10_000)
        self.assertEqual(self.replace(coll), 0)
        self.assertEqual(coll.kinds(), ["count", "fetch", "count", "fetch"])
        self.assertEqual(coll.calls[1][1][0], 10_000)
        coll = FakeSourceCollection([], count=0)
        self.assertFalse(self.has(coll))
        self.assertEqual(coll.kinds(), ["count"])

    def test_a_name_has_chunks_only_when_stored_exactly(self):
        coll = FakeSourceCollection(stored("a/report.pdf", EARLIER, 2) + stored("Report.PDF", EARLIER, 2))
        self.assertFalse(self.has(coll))
        self.assertTrue(self.has(coll, "Report.PDF"))
        coll = FakeSourceCollection(stored("report.pdf", EARLIER, 1) + self.others(), count=10_001)
        self.assertTrue(self.has(coll))
        self.assertEqual(coll.taken, 1)
        self.assertEqual(coll.kinds(), ["count", "walk"])

    def test_a_rule_selects_what_it_accepts(self):
        coll = FakeSourceCollection(self.others() + stored("report.pdf", WHEN, 1))
        family = {"report.pdf", "report.pdf (part 1)"}
        selected = list(wc._exact_source_objects(coll, "report.pdf", lambda value: value in family))
        self.assertEqual(sorted(o.properties["source_file"] for o in selected),
                         ["report.pdf", "report.pdf (part 1)", "report.pdf (part 1)"])


class SourcesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        change = patch.object(settings, "sources_dir", temp.name)
        change.start(); self.addCleanup(change.stop)

    def docs(self):
        return sources.load_index("Coll")["documents"]

    def test_replace_releases_the_name_and_drops_the_orphan(self):
        v1 = sources.store("Coll", "p.txt", b"v1", replace=True)
        v2 = sources.store("Coll", "p.txt", b"v2", replace=True)
        self.assertEqual({d: e["filenames"] for d, e in self.docs().items()}, {v2: ["p.txt"]})
        self.assertFalse(sources.blob_path("Coll", v1).exists())
        self.assertTrue(sources.blob_path("Coll", v2).is_file())

    def test_shared_content_keeps_both_names_e2(self):
        d = sources.store("Coll", "a.txt", b"same", replace=True)
        sources.store("Coll", "b.txt", b"same", replace=True)
        self.assertEqual(self.docs()[d]["filenames"], ["a.txt", "b.txt"])
        # Replacing one name keeps the digest for the other.
        sources.store("Coll", "a.txt", b"changed", replace=True)
        self.assertEqual(self.docs()[d]["filenames"], ["b.txt"])
        self.assertTrue(sources.blob_path("Coll", d).is_file())

    def test_without_replace_behaves_as_before(self):
        v1 = sources.store("Coll", "p.txt", b"v1")
        v2 = sources.store("Coll", "p.txt", b"v2")
        self.assertEqual(sorted(sources.digests_for_filename("Coll", "p.txt")), sorted([v1, v2]))

    def index_bytes(self):
        return (sources.collection_dir("Coll") / sources.INDEX_NAME).read_bytes()

    def test_release_drops_the_name_and_an_orphan_after_the_save_880(self):
        old = sources.store("Coll", "p.txt", b"v1", replace=True)
        shared = sources.store("Coll", "a.txt", b"same", replace=True)
        sources.store("Coll", "b.txt", b"same", replace=True)
        sources.release("Coll", "p.txt")
        self.assertNotIn(old, self.docs())
        self.assertFalse(sources.blob_path("Coll", old).exists())
        # A digest another name still lists keeps its entry and its blob.
        sources.release("Coll", "a.txt")
        self.assertEqual(self.docs()[shared]["filenames"], ["b.txt"])
        self.assertTrue(sources.blob_path("Coll", shared).is_file())
        self.assertEqual(sources.digests_for_filename("Coll", "p.txt"), [])
        self.assertEqual(sources.digests_for_filename("Coll", "a.txt"), [])

    def test_release_writes_nothing_for_an_unlisted_name_880(self):
        sources.store("Coll", "p.txt", b"v1", replace=True)
        before = self.folder()
        with patch.object(sources, "_save_index", wraps=sources._save_index) as save:
            sources.release("Coll", "other.txt")
            sources.release("Empty", "p.txt")
        save.assert_not_called()
        self.assertEqual(self.folder(), before)
        self.assertFalse(sources.collection_dir("Empty").exists())

    def test_release_refuses_an_unreadable_index_without_writing_880(self):
        sources.store("Coll", "p.txt", b"v1", replace=True)
        (sources.collection_dir("Coll") / sources.INDEX_NAME).write_text("{not json")
        before = self.folder()
        with self.assertRaisesRegex(ValueError, "Unreadable retained source index"):
            sources.release("Coll", "p.txt")
        self.assertEqual(self.folder(), before)

    def test_a_failed_final_save_leaves_the_name_released_880(self):
        # #880: the release used to be saved only with the new entry, so a
        # failed final save left the name on the version it replaced.
        old = sources.store("Coll", "p.txt", b"v1", replace=True)
        real, calls = sources._save_index, []

        def second_fails(collection, index):
            calls.append(collection)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(collection, index)

        with patch.object(sources, "_save_index", side_effect=second_fails), \
                self.assertRaisesRegex(OSError, "disk full"):
            sources.store("Coll", "p.txt", b"v2", replace=True)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.docs(), {})
        self.assertEqual(sources.digests_for_filename("Coll", "p.txt"), [])
        self.assertFalse(sources.blob_path("Coll", old).exists())

    def test_a_failed_release_save_keeps_the_index_and_is_logged_880(self):
        sources.store("Coll", "p.txt", b"v1", replace=True)
        before = self.index_bytes()
        with patch.object(sources, "_save_index", side_effect=OSError("read-only")), \
                self.assertLogs(sources.log, "ERROR") as logged, \
                self.assertRaisesRegex(OSError, "read-only"):
            sources.store("Coll", "p.txt", b"v2", replace=True)
        self.assertEqual(self.index_bytes(), before)
        self.assertIn("Could not release 'p.txt' from its earlier version in 'Coll'; a "
                      "re-chunk could rebuild that earlier version: read-only",
                      "\n".join(logged.output))
        with patch.object(sources, "_save_index", side_effect=OSError("read-only")), \
                self.assertLogs(sources.log, "ERROR") as logged, \
                self.assertRaisesRegex(OSError, "read-only"):
            sources.release("Coll", "p.txt")
        self.assertEqual(self.index_bytes(), before)
        self.assertIn("a re-chunk could rebuild that earlier version", "\n".join(logged.output))

    def test_nothing_to_release_saves_once_880(self):
        cases = (("first upload", "p.txt", b"v1", True),
                 ("the same content again", "p.txt", b"v1", True),
                 ("without replace", "q.txt", b"v2", False))
        for case, name, data, replace in cases:
            with self.subTest(case=case), \
                    patch.object(sources, "_save_index", wraps=sources._save_index) as save:
                sources.store("Coll", name, data, replace=replace)
                self.assertEqual(save.call_count, 1)
        with patch.object(sources, "_save_index", wraps=sources._save_index) as save:
            sources.store("Coll", "p.txt", b"v3", replace=True)
        self.assertEqual(save.call_count, 2)        # the release, then the new entry

    def failing_blob_write(self):
        """The blob's replace fails; index saves go through. Records the
        temporary files the failed writes had created."""
        real, attempted = os.replace, []

        def replace(src, dst):
            if Path(dst).name != sources.INDEX_NAME:
                attempted.append(Path(src))
                raise OSError("disk full")
            return real(src, dst)

        return patch.object(sources.os, "replace", side_effect=replace), attempted

    def test_name_is_released_even_when_the_blob_write_fails(self):
        v1 = sources.store("Coll", "p.txt", b"v1", replace=True)
        failing, attempted = self.failing_blob_write()
        with failing, self.assertRaises(OSError):
            sources.store("Coll", "p.txt", b"v2", replace=True)
        self.assertEqual(len(attempted), 1)
        self.assertEqual(self.docs(), {})
        self.assertFalse(sources.blob_path("Coll", v1).exists())

    def test_a_failed_blob_write_leaves_no_temporary_file(self):
        # #52: the blob's temporary file is created exclusively before it is
        # written, so a failed write must remove it, not leave it in the folder.
        sources.store("Coll", "p.txt", b"v1", replace=True)
        failing, attempted = self.failing_blob_write()
        with failing, self.assertRaises(OSError):
            sources.store("Coll", "q.txt", b"v2", replace=False)
        self.assertEqual(len(attempted), 1)
        self.assertEqual(attempted[0].parent, sources.collection_dir("Coll"))
        self.assertFalse(attempted[0].exists())
        self.assertEqual([p.name for p in sources.collection_dir("Coll").iterdir()
                          if p.name.endswith(".tmp")], [])
        self.assertFalse(sources.blob_path("Coll", hashlib.sha256(b"v2").hexdigest()).exists())
        self.assertEqual(sorted(n for e in self.docs().values() for n in e["filenames"]), ["p.txt"])

    def test_the_blob_is_published_and_fsynced_882(self):
        # #882: the blob was written without an fsync while the index naming
        # it was fsynced, so a power loss could leave the index naming an
        # empty or truncated blob.
        real_fsync, synced = os.fsync, []

        def fsync(fd):
            synced.append(fd)
            return real_fsync(fd)

        with patch.object(sources, "_publish", wraps=sources._publish) as publish, \
                patch.object(sources.os, "fsync", side_effect=fsync):
            digest = sources.store("Coll", "p.txt", b"v1", replace=True)
        blob = sources.blob_path("Coll", digest)
        self.assertEqual(publish.call_args_list[0].args, (blob, b"v1"))
        self.assertEqual(len(synced), 2)            # the blob, then the index
        self.assertEqual(blob.read_bytes(), b"v1")
        # An existing blob isn't written again: only the index is.
        with patch.object(sources, "_publish", wraps=sources._publish) as publish:
            sources.store("Coll", "q.txt", b"v1", replace=True)
        self.assertEqual([c.args[0].name for c in publish.call_args_list], [sources.INDEX_NAME])

    def test_delete_drops_the_collections_lock_882(self):
        sources.store("Coll", "p.txt", b"v1", replace=True)
        key = str(sources.collection_dir("Coll"))
        self.assertIn(key, sources._locks)
        sources.delete("Coll")
        self.assertNotIn(key, sources._locks)
        self.assertFalse(sources.collection_dir("Coll").exists())
        digest = sources.store("Coll", "p.txt", b"v2", replace=True)
        self.assertIn(key, sources._locks)
        self.assertEqual(self.docs(), {digest: self.docs()[digest]})

    def test_the_startup_sweep_removes_only_interrupted_saves_882(self):
        digest = sources.store("Coll", "p.txt", b"v1", replace=True)
        folder = sources.collection_dir("Coll")
        leftovers = [folder / f"{sources.INDEX_NAME}.0123456789abcdef.tmp",
                     folder / f"{digest}.fedcba9876543210.tmp"]
        kept = [folder / "notes.tmp", folder / "x.abc.tmp",
                folder / f"{sources.INDEX_NAME}.0123.tmp"]
        for path in leftovers + kept:
            path.write_bytes(b"partial")
        # A linked folder could lead outside the sources root: never followed.
        outside = tempfile.TemporaryDirectory()
        self.addCleanup(outside.cleanup)
        foreign = Path(outside.name) / f"{sources.INDEX_NAME}.0123456789abcdef.tmp"
        foreign.write_bytes(b"not ours")
        (Path(settings.sources_dir) / "Linked").symlink_to(outside.name)
        (Path(settings.sources_dir) / "stray.0123456789abcdef.tmp").write_bytes(b"x")
        sources.remove_interrupted_saves()
        self.assertEqual([p for p in leftovers if p.exists()], [])
        self.assertEqual([p for p in kept if not p.exists()], [])
        self.assertTrue(foreign.exists())
        self.assertTrue((Path(settings.sources_dir) / "stray.0123456789abcdef.tmp").exists())
        self.assertEqual(sources.digests_for_filename("Coll", "p.txt"), [digest])
        self.assertTrue(sources.blob_path("Coll", digest).is_file())

    def test_a_leftover_that_cant_be_removed_is_logged_and_kept_882(self):
        sources.store("Coll", "p.txt", b"v1", replace=True)
        folder = sources.collection_dir("Coll")
        stuck = folder / f"{sources.INDEX_NAME}.0000000000000000.tmp"
        other = folder / f"{sources.INDEX_NAME}.1111111111111111.tmp"
        stuck.write_bytes(b"partial"); other.write_bytes(b"partial")
        real = Path.unlink

        def unlink(path, *args, **kwargs):
            if path == stuck:
                raise PermissionError("in use")
            return real(path, *args, **kwargs)

        with patch.object(Path, "unlink", unlink), \
                self.assertLogs(sources.log, "ERROR") as logged:
            sources.remove_interrupted_saves()
        self.assertTrue(stuck.exists())
        self.assertFalse(other.exists())
        self.assertIn(f"Could not remove interrupted source write {stuck}", "\n".join(logged.output))

    def test_the_startup_sweep_needs_no_sources_folder_882(self):
        with patch.object(settings, "sources_dir", str(Path(settings.sources_dir) / "missing")):
            sources.remove_interrupted_saves()
            self.assertFalse(Path(settings.sources_dir).exists())

    def folder(self):
        """Every file in Coll's retained folder, by name, with its bytes."""
        directory = sources.collection_dir("Coll")
        return {p.name: p.read_bytes() if p.is_file() else None for p in directory.iterdir()}

    def test_an_unreadable_index_is_refused_and_left_unchanged(self):
        # #52: an unreadable index was treated as empty, so the next store
        # replaced every earlier entry. It is refused like an invalid one.
        directory = sources.collection_dir("Coll")
        directory.mkdir(parents=True)
        index = directory / sources.INDEX_NAME
        for form, content in (("invalid JSON", b"{not json"), ("invalid UTF-8", b"\xff\xfe{}")):
            with self.subTest(form=form):
                index.write_bytes(content)
                with self.assertRaisesRegex(ValueError, "Unreadable retained source index"):
                    sources.load_index("Coll")
                self.assertEqual(index.read_bytes(), content)
        index.unlink()
        index.mkdir()                      # reading it raises IsADirectoryError, an OSError
        with self.subTest(form="read error"):
            with self.assertRaisesRegex(ValueError, "Unreadable retained source index"):
                sources.load_index("Coll")
            self.assertTrue(index.is_dir())

    def test_store_refuses_an_unreadable_index_and_writes_nothing(self):
        # #52: a corrupt index followed by an ingest must not become a partial
        # index; nothing in the folder changes, no blob or temporary file appears.
        sources.store("Coll", "a.txt", b"one", replace=True)
        (sources.collection_dir("Coll") / sources.INDEX_NAME).write_text("{not json")
        before = self.folder()
        for replace in (True, False):
            with self.subTest(replace=replace), \
                    self.assertRaisesRegex(ValueError, "Unreadable retained source index"):
                sources.store("Coll", "b.txt", b"two", replace=replace)
            self.assertEqual(self.folder(), before)

    def test_concurrent_stores_into_one_collection_keep_both_entries(self):
        # #52: two stores read the same index and the later save dropped the
        # other's entry. The first store waits inside its cycle for the second
        # to start reading; with the lock, the second can't start until the
        # first has saved, and both entries are on disk.
        real = sources.load_index
        inside, second = threading.Event(), threading.Event()
        calls = []

        def load_index(collection):
            calls.append(collection)
            if len(calls) == 1:
                inside.set()
                second.wait(0.5)
            else:
                second.set()
            return real(collection)

        errors = []

        def store(name, data):
            try:
                sources.store("Coll", name, data, replace=True)
            except BaseException as exc:         # noqa: BLE001 - reported below
                errors.append(exc)

        with patch.object(sources, "load_index", load_index):
            first = threading.Thread(target=store, args=("a.txt", b"one"))
            first.start()
            self.assertTrue(inside.wait(5))
            other = threading.Thread(target=store, args=("b.txt", b"two"))
            other.start()
            first.join(10)
            other.join(10)
        self.assertEqual(errors, [])
        self.assertEqual({d: e["filenames"] for d, e in real("Coll")["documents"].items()},
                         {hashlib.sha256(b"one").hexdigest(): ["a.txt"],
                          hashlib.sha256(b"two").hexdigest(): ["b.txt"]})

    def test_index_saves_use_unique_temporary_files_and_a_failed_save_keeps_the_index(self):
        real = os.replace
        published = []

        def record(src, dst):
            if Path(dst).name == sources.INDEX_NAME:
                published.append(Path(src))
            return real(src, dst)

        with patch.object(sources.os, "replace", side_effect=record):
            sources.store("Coll", "a.txt", b"one", replace=True)
            sources.store("Coll", "b.txt", b"two", replace=True)
        self.assertEqual(len(published), 2)
        self.assertNotEqual(published[0], published[1])
        for tmp in published:
            self.assertEqual(tmp.parent, sources.collection_dir("Coll"))
            self.assertTrue(tmp.name.startswith("index.json.") and tmp.name.endswith(".tmp"))
            self.assertFalse(tmp.exists())

        index = sources.collection_dir("Coll") / sources.INDEX_NAME
        saved = index.read_bytes()

        def fail_index(src, dst):
            if Path(dst).name == sources.INDEX_NAME:
                raise OSError("disk full")
            return real(src, dst)

        with patch.object(sources.os, "replace", side_effect=fail_index), \
                self.assertRaises(OSError):
            sources.store("Coll", "c.txt", b"three", replace=True)
        self.assertEqual(index.read_bytes(), saved)
        self.assertEqual([p.name for p in index.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_restore_package_publishes_the_index_atomically(self):
        package = Path(tempfile.mkdtemp(dir=settings.sources_dir))
        (package / "sources").mkdir()
        digest = hashlib.sha256(b"packaged").hexdigest()
        (package / "sources" / digest).write_bytes(b"packaged")
        payload = json.dumps({"version": 1, "documents": {digest: {"filenames": ["p.txt"]}}}).encode()
        (package / "sources" / sources.INDEX_NAME).write_bytes(payload)
        real = os.replace
        published = []

        def record(src, dst):
            published.append((Path(src), Path(dst)))
            return real(src, dst)

        with patch.object(sources.os, "replace", side_effect=record):
            sources.restore_package(package, "Restored")
        index = sources.collection_dir("Restored") / sources.INDEX_NAME
        self.assertEqual([dst for _, dst in published], [index])
        self.assertEqual(published[0][0].parent, index.parent)
        self.assertTrue(published[0][0].name.endswith(".tmp"))
        self.assertEqual(index.read_bytes(), payload)
        self.assertEqual(sources.load_index("Restored")["documents"].keys(), {digest})


class RechunkTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for change in (patch.object(settings, "sources_dir", str(Path(temp.name) / "s")),
                       patch.object(settings, "upload_dir", temp.name)):
            change.start(); self.addCleanup(change.stop)

    def stored(self, *chunks):
        objects = [SimpleNamespace(properties={"source_file": n, "chunk_index": i}) for n, i in chunks]
        coll = SimpleNamespace(iterator=lambda **_: iter(objects))
        return patch.object(wc, "get_client", return_value=SimpleNamespace(
            collections=SimpleNamespace(get=lambda name: coll)))

    def test_same_basename_in_two_folders_rebuilds_both_under_full_names(self):
        sources.store("Coll", "a/report.txt", b"alpha text", replace=True)
        sources.store("Coll", "b/report.txt", b"beta text", replace=True)
        sources.store("Coll", "copy.txt", b"alpha text", replace=True)
        with self.stored(("a/report.txt", 0), ("b/report.txt", 0), ("copy.txt", 0)):
            chunks = tuning._chunks_from_sources("Coll", "fixed", 1000, 0, 0.85, 0)
        got = sorted((c["source_file"], c["content"], c["source_type"]) for c in chunks)
        self.assertEqual(got, [("a/report.txt", "alpha text", "txt"),
                               ("b/report.txt", "beta text", "txt"),
                               ("copy.txt", "alpha text", "txt")])

    def test_a_name_with_two_digests_is_still_refused(self):
        sources.store("Coll", "p.txt", b"v1")
        sources.store("Coll", "p.txt", b"v2")
        with self.stored(("p.txt", 0)):
            self.assertEqual(tuning._uncovered_source_files("Coll", sources.load_index("Coll")["documents"]),
                             ["p.txt"])

    def test_a_second_name_is_covered(self):
        sources.store("Coll", "a.txt", b"same", replace=True)
        sources.store("Coll", "b.txt", b"same", replace=True)
        with self.stored(("a.txt", 0), ("b.txt", 0)):
            self.assertEqual(tuning._uncovered_source_files("Coll", sources.load_index("Coll")["documents"]), [])

    def test_the_coverage_refusal_names_at_most_ten_files(self):
        # The detail goes to the job API, so it is capped (#208), as #28 caps
        # an upload refusal's members.
        sources.store("Coll", "kept.txt", b"kept text", replace=True)
        names = [f"n{i:02d}.txt" for i in range(12)]
        with self.stored(("kept.txt", 0), *[(name, 0) for name in reversed(names)]):
            with self.assertRaises(tuning.PackageError) as refused:
                tuning._chunks_from_sources("Coll", "fixed", 1000, 0, 0.85, 0)
        self.assertEqual(refused.exception.code, "SOURCES_REQUIRED")
        self.assertEqual(refused.exception.detail, {
            "collection": "Coll", "uncovered_source_files": names[:10],
            "uncovered_source_files_omitted": 2})

    def test_the_coverage_refusal_cap_boundary(self):
        # Reviewer-added: exactly ten names are all listed with none omitted;
        # the eleventh is the first one counted instead of named (#208).
        sources.store("Coll", "kept.txt", b"kept text", replace=True)
        for count, omitted in ((10, 0), (11, 1)):
            with self.subTest(uncovered=count):
                names = [f"n{i:02d}.txt" for i in range(count)]
                with self.stored(("kept.txt", 0), *[(name, 0) for name in names]):
                    with self.assertRaises(tuning.PackageError) as refused:
                        tuning._chunks_from_sources("Coll", "fixed", 1000, 0, 0.85, 0)
                self.assertEqual(refused.exception.detail["uncovered_source_files"], names[:10])
                self.assertEqual(refused.exception.detail["uncovered_source_files_omitted"], omitted)

    def test_the_coverage_walk_reads_only_source_file_and_chunk_index(self):
        # Not each chunk's text and vector, on every GET /tune (#258).
        sources.store("Coll", "a.txt", b"alpha", replace=True)
        calls = []

        def iterator(**kwargs):
            calls.append(kwargs)
            return iter([SimpleNamespace(properties={"source_file": "a.txt", "chunk_index": 0})])

        coll = SimpleNamespace(iterator=iterator)
        with patch.object(wc, "get_client", return_value=SimpleNamespace(
                collections=SimpleNamespace(get=lambda name: coll))):
            self.assertEqual(tuning._uncovered_source_files("Coll", sources.load_index("Coll")["documents"]), [])
        self.assertEqual(calls, [{"include_vector": False,
                                  "return_properties": ["source_file", "chunk_index"]}])


    def test_a_name_without_stored_chunks_is_not_rebuilt(self):
        # #311: an index can list names the collection never showed; only the
        # stored ones are rebuilt, and the index keeps the others.
        sources.store("Coll", "kept.txt", b"shared text", replace=True)
        sources.store("Coll", "ghost.txt", b"shared text", replace=True)
        copies = []

        def copyfile(src, dst, *args, **kwargs):
            copies.append(Path(dst).name)
            return real_copyfile(src, dst, *args, **kwargs)

        real_copyfile = shutil.copyfile
        with self.stored(("kept.txt", 0)), patch.object(tuning.shutil, "copyfile", copyfile):
            chunks = tuning._chunks_from_sources("Coll", "fixed", 1000, 0, 0.85, 0)
        self.assertEqual([(c["source_file"], c["content"]) for c in chunks],
                         [("kept.txt", "shared text")])
        self.assertEqual(copies, ["kept.txt"])
        filenames = [n for e in sources.load_index("Coll")["documents"].values() for n in e["filenames"]]
        self.assertEqual(sorted(filenames), ["ghost.txt", "kept.txt"])

    def test_a_missing_blob_is_refused_even_for_names_without_chunks(self):
        sources.store("Coll", "kept.txt", b"kept", replace=True)
        digest = sources.store("Coll", "ghost.txt", b"ghost", replace=True)
        sources.blob_path("Coll", digest).unlink()
        with self.stored(("kept.txt", 0)), self.assertRaises(tuning.PackageError) as caught:
            tuning._chunks_from_sources("Coll", "fixed", 1000, 0, 0.85, 0)
        self.assertEqual((caught.exception.code, caught.exception.detail.get("digest")),
                         ("SOURCES_REQUIRED", digest))

    def test_one_blob_under_many_names_is_parsed_once_per_stored_name(self):
        # #311 (reviewer): the crafted-package case. One blob listed under many
        # names, and a second digest none of whose names has chunks: only the
        # stored name is parsed, and the chunk-less digest is never staged.
        for i in range(50):
            sources.store("Coll", f"n{i:02}.txt", b"one blob", replace=True)
        sources.store("Coll", "orphan.txt", b"other blob", replace=True)
        real_parse = tuning._parse_file
        parsed = []

        def parse(path):
            parsed.append(Path(path).name)
            return real_parse(path)

        with self.stored(("n07.txt", 0)), patch.object(tuning, "_parse_file", parse):
            chunks = tuning._chunks_from_sources("Coll", "fixed", 1000, 0, 0.85, 0)
        self.assertEqual(parsed, ["n07.txt"])
        self.assertEqual([(c["source_file"], c["content"]) for c in chunks],
                         [("n07.txt", "one blob")])
        filenames = [n for e in sources.load_index("Coll")["documents"].values() for n in e["filenames"]]
        self.assertEqual(len(filenames), 51)

    NOTHING_TO_REBUILD = ("Cannot re-chunk 'Coll': no retained original file has stored "
                          "chunks, so there is nothing to rebuild.")

    def test_nothing_to_rebuild_is_refused_before_staging(self):
        # #328: every retained file produced no chunks at ingest. That is
        # refused as such, not blamed on the chunking parameters.
        sources.store("Coll", "empty.txt", b"one", replace=True)
        sources.store("Coll", "blank.txt", b"two", replace=True)
        copies = []
        with self.stored(), patch.object(tuning.shutil, "copyfile", lambda s, d, *a, **k: copies.append(d)), \
                self.assertRaises(tuning.PackageError) as caught:
            tuning._chunks_from_sources("Coll", "fixed", 1000, 0, 0.85, 0)
        self.assertEqual((caught.exception.code, caught.exception.message, caught.exception.detail),
                         ("SOURCES_REQUIRED", self.NOTHING_TO_REBUILD, {"collection": "Coll"}))
        self.assertEqual(copies, [])

    def test_a_start_with_nothing_to_rebuild_is_refused_without_a_job(self):
        # #328: the re-chunk start, and a re-embed with chunking parameters,
        # refuse before any job record exists and release their guards.
        from routers import tuning as tuning_router
        from services import collection_writes
        from models.schemas import RechunkRequest
        sources.store("Coll", "empty.txt", b"one", replace=True)
        chunking = {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0,
                    "similarity_threshold": 0.85, "min_chunk_size": 0}
        before = set(tuning._jobs)
        with self.stored(("other.txt", 0)):
            for operation in ("rechunk", "reembed"):
                with self.subTest(operation=operation), self.assertRaises(tuning.PackageError) as caught:
                    asyncio.run(tuning.start_tune_job("Coll", operation, {"chunking": chunking}))
                self.assertEqual((caught.exception.code, caught.exception.message),
                                 ("SOURCES_REQUIRED", self.NOTHING_TO_REBUILD))
                self.assertEqual(set(tuning._jobs), before)
                self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")), (set(), []))

            with patch.object(tuning_router.wc, "collection_exists", AsyncMock(return_value=True)):
                response = asyncio.run(tuning_router.rechunk(RechunkRequest(collection="Coll")))
            self.assertEqual(response.status_code, 409)
            self.assertEqual(json.loads(response.body)["error"]["code"], "SOURCES_REQUIRED")
            self.assertEqual(set(tuning._jobs), before)

            # Re-embedding the stored chunk text has nothing to rebuild from
            # the originals, so it still starts.
            def run(job_id, collection, *args, **kwargs):
                collection_writes.end_job("tune", job_id)
                with tuning._lock:
                    tuning._active.discard(collection)

            async def start():
                job_id = await tuning.start_tune_job("Coll", "reembed", {"chunking": None})
                for _ in range(200):
                    if not tuning._active:
                        break
                    await asyncio.sleep(0.01)
                return job_id

            with patch.object(tuning, "_run", run):
                job_id = asyncio.run(start())
        self.assertIn(job_id, set(tuning._jobs) - before)
        self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")), (set(), []))

    def test_the_up_front_check_runs_after_the_guards_and_only_for_rechunks(self):
        # #328 (reviewer): TUNE_IN_PROGRESS and COLLECTION_DELETING are answered
        # before the walk; a re-index and a collection with a stored listed name
        # start a job. An invalid index is refused, with no job (#389).
        from services import collection_writes
        sources.store("Coll", "empty.txt", b"one", replace=True)
        chunking = {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0,
                    "similarity_threshold": 0.85, "min_chunk_size": 0}

        def run(job_id, collection, *args, **kwargs):
            collection_writes.end_job("tune", job_id)
            with tuning._lock:
                tuning._active.discard(collection)

        async def start(operation, params):
            job_id = await tuning.start_tune_job("Coll", operation, params)
            for _ in range(200):
                if not tuning._active:
                    break
                await asyncio.sleep(0.01)
            return job_id

        before = set(tuning._jobs)
        started = []
        with self.stored(("other.txt", 0)) as get_client, patch.object(tuning, "_run", run):
            with tuning._lock:
                tuning._active.add("Coll")
            try:
                with self.assertRaises(RuntimeError):
                    asyncio.run(tuning.start_tune_job("Coll", "rechunk", {"chunking": chunking}))
            finally:
                with tuning._lock:
                    tuning._active.discard("Coll")
            with patch.object(collection_writes, "register_job",
                              side_effect=collection_writes.CollectionDeletingError("Coll")), \
                    self.assertRaises(collection_writes.CollectionDeletingError):
                asyncio.run(tuning.start_tune_job("Coll", "rechunk", {"chunking": chunking}))
            self.assertEqual((get_client.call_count, set(tuning._jobs)), (0, before))
            self.assertEqual(tuning._active, set())

            started.append(asyncio.run(start("reindex", {})))
            self.assertEqual(get_client.call_count, 0)
            with patch.object(sources, "load_index", side_effect=ValueError("bad index")), \
                    self.assertRaises(tuning.PackageError) as caught:
                asyncio.run(start("rechunk", {"chunking": chunking}))
            self.assertEqual(caught.exception.code, "SOURCE_INDEX_INVALID")
            self.assertEqual((get_client.call_count, tuning._active), (0, set()))
        with self.stored(("other.txt", 0), ("empty.txt", 0)), patch.object(tuning, "_run", run):
            started.append(asyncio.run(start("rechunk", {"chunking": chunking})))
        self.assertEqual(set(tuning._jobs) - before, set(started))
        self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")), (set(), []))

    SOURCE_INDEX_INVALID = ("Cannot re-chunk 'Coll': its retained source index is invalid. "
                            "Repair it before re-chunking; re-embedding from stored chunks "
                            "and re-indexing remain available.")

    def invalid_indexes(self):
        """Each way load_index refuses an index, written in turn for Coll."""
        directory = sources.collection_dir("Coll")
        directory.mkdir(parents=True, exist_ok=True)
        index = directory / sources.INDEX_NAME
        valid = directory.parent / "valid-index.json"
        valid.write_text(json.dumps({"version": 1, "documents": {
            hashlib.sha256(b"one").hexdigest(): {"filenames": ["a.txt"]}}}))
        for form, write in (
                ("non-digest key", lambda: index.write_text(json.dumps(
                    {"version": 1, "documents": {"../outside.txt": {"filenames": ["a.txt"]}}}))),
                ("non-object document", lambda: index.write_text("[]")),
                ("linked index", lambda: index.symlink_to(valid)),
                # #52: unreadable indexes were treated as empty, not refused.
                ("unreadable JSON", lambda: index.write_text("{not json")),
                ("invalid UTF-8", lambda: index.write_bytes(b"\xff\xfe{}"))):
            index.unlink(missing_ok=True)
            write()
            with self.assertRaises(ValueError):
                sources.load_index("Coll")
            yield form

    def test_an_invalid_source_index_is_refused_at_each_tune_start_over_http(self):
        # #389: re-chunk, and re-embed with chunking fields, on an invalid
        # retained source index answered 202 and the job failed TUNE_FAILED.
        # The start is refused, 409 SOURCE_INDEX_INVALID, before any job exists,
        # with the guards released and Weaviate never read.
        from fastapi.testclient import TestClient
        from main import app
        from auth import testing as auth_testing  # a real session in a temporary store (#25)
        from services import collection_writes
        client = TestClient(app, headers=auth_testing.headers())  # No lifespan: nothing connects to a stack.
        self.addCleanup(client.close)
        requests = {
            "rechunk": ("/tune/rechunk", {"collection": "Coll"}),
            "reembed with chunking": ("/tune/reembed", {
                "collection": "Coll", "chunking_strategy": "fixed", "chunk_size": 80}),
        }
        before = set(tuning._jobs)
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(wc, "get_client") as get_client:
            for form in self.invalid_indexes():
                for case, (path, body) in requests.items():
                    with self.subTest(form=form, case=case):
                        response = client.post(path, json=body)
                        self.assertEqual(response.status_code, 409, response.text)
                        self.assertTrue(
                            response.headers["content-type"].startswith("application/json"))
                        self.assertEqual(response.json()["error"], {
                            "code": "SOURCE_INDEX_INVALID",
                            "message": self.SOURCE_INDEX_INVALID,
                            "detail": {"collection": "Coll"}})
                        self.assertEqual(set(tuning._jobs), before)
                        self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")),
                                         (set(), []))
            self.assertEqual(get_client.call_count, 0)

    def test_an_invalid_source_index_still_starts_record_only_tuning_over_http(self):
        # #389: re-indexing and re-embedding the stored chunk text never read the
        # retained sources, so an invalid index doesn't refuse them.
        from fastapi.testclient import TestClient
        from main import app
        from auth import testing as auth_testing  # a real session in a temporary store (#25)
        from services import collection_writes
        import anyio.from_thread
        client = TestClient(app, headers=auth_testing.headers())  # No lifespan: nothing connects to a stack.
        self.addCleanup(client.close)
        # #389 (reviewer): one event loop for every request. With TestClient's
        # per-request loop, a started job's task could be cancelled before it
        # reached _run, leaving the tune guard held for the next request.
        stack = contextlib.ExitStack()
        client.portal = stack.enter_context(anyio.from_thread.start_blocking_portal())
        self.addCleanup(stack.close)

        def run(job_id, collection, *args, **kwargs):
            collection_writes.end_job("tune", job_id)
            with tuning._lock:
                tuning._active.discard(collection)

        def settle():
            for _ in range(200):
                if not tuning._active:
                    return
                threading.Event().wait(0.01)

        before = set(tuning._jobs)
        started = []
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(tuning, "_run", run):
            for form in self.invalid_indexes():
                for case, path in (("reindex", "/tune/reindex"),
                                   ("reembed without chunking", "/tune/reembed")):
                    with self.subTest(form=form, case=case):
                        response = client.post(path, json={"collection": "Coll"})
                        settle()
                        self.assertEqual(response.status_code, 202, response.text)
                        self.assertEqual(response.json()["status"], "queued")
                        self.assertIn(response.json()["job_id"], set(tuning._jobs))
                        started.append(response.json()["job_id"])
        self.assertEqual(set(tuning._jobs) - before, set(started))
        self.assertEqual(len(started), 10)    # five index forms, two routes
        self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")), (set(), []))

    def test_the_guards_answer_before_an_invalid_source_index(self):
        # #389 (reviewer): the invalid-index refusal runs after the guards, so a
        # busy or deleting collection still answers TUNE_IN_PROGRESS or
        # COLLECTION_DELETING, and the refusal leaves the other tune's guard alone.
        from fastapi.testclient import TestClient
        from main import app
        from auth import testing as auth_testing  # a real session in a temporary store (#25)
        from services import collection_writes
        client = TestClient(app, headers=auth_testing.headers())  # No lifespan: nothing connects to a stack.
        self.addCleanup(client.close)
        body = {"collection": "Coll", "chunking_strategy": "fixed", "chunk_size": 80}
        before = set(tuning._jobs)
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(wc, "get_client") as get_client:
            for form in self.invalid_indexes():
                for path in ("/tune/rechunk", "/tune/reembed"):
                    with self.subTest(form=form, path=path, guard="tune"):
                        with tuning._lock:
                            tuning._active.add("Coll")
                        try:
                            response = client.post(path, json=body)
                            self.assertEqual(tuning._active, {"Coll"})
                        finally:
                            with tuning._lock:
                                tuning._active.discard("Coll")
                        self.assertEqual(response.status_code, 409, response.text)
                        self.assertEqual(response.json()["error"]["code"], "TUNE_IN_PROGRESS")
                    with self.subTest(form=form, path=path, guard="delete"), \
                            patch.object(collection_writes, "register_job",
                                         side_effect=collection_writes.CollectionDeletingError("Coll")):
                        response = client.post(path, json=body)
                        self.assertEqual(response.status_code, 409, response.text)
                        self.assertEqual(response.json()["error"]["code"], "COLLECTION_DELETING")
                    self.assertEqual(set(tuning._jobs), before)
                    self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")),
                                     (set(), []))
            self.assertEqual(get_client.call_count, 0)

    def test_a_weaviate_error_in_the_up_front_check_is_a_503(self):
        # #340: a Weaviate failure while the start reads the collection was a
        # bare 500. It is SERVICE_UNAVAILABLE, with no job, the guards released
        # and the cause logged but not sent.
        from routers import tuning as tuning_router
        from services import collection_writes
        from models.schemas import RechunkRequest
        sources.store("Coll", "empty.txt", b"one", replace=True)
        chunking = {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0,
                    "similarity_threshold": 0.85, "min_chunk_size": 0}

        def broken(**_):
            raise ConnectionError("weaviate down")

        coll = SimpleNamespace(iterator=broken)
        before = set(tuning._jobs)
        with patch.object(wc, "get_client", return_value=SimpleNamespace(
                collections=SimpleNamespace(get=lambda name: coll))):
            for operation in ("rechunk", "reembed"):
                with self.subTest(operation=operation), self.assertLogs(tuning._log, "ERROR"), \
                        self.assertRaises(tuning.ServiceUnavailableError):
                    asyncio.run(tuning.start_tune_job("Coll", operation, {"chunking": chunking}))
                self.assertEqual(set(tuning._jobs), before)
                self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")), (set(), []))

            with patch.object(tuning_router.wc, "collection_exists", AsyncMock(return_value=True)), \
                    self.assertLogs(tuning._log, "ERROR"):
                response = asyncio.run(tuning_router.rechunk(RechunkRequest(collection="Coll")))
        self.assertEqual(response.status_code, 503)
        self.assertEqual(json.loads(response.body)["error"], {
            "code": "SERVICE_UNAVAILABLE",
            "message": "Cannot check whether 'Coll' can be re-chunked: Weaviate could not be "
                       "read. No job was started; try again when Weaviate is available.",
            "detail": {"collection": "Coll"}})
        self.assertNotIn(b"weaviate down", response.body)
        self.assertEqual(set(tuning._jobs), before)
        self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")), (set(), []))

    def test_a_connect_failure_and_the_reembed_route_are_503_too(self):
        # #340 (reviewer): a failure in get_client itself (no connection), not
        # only in the walk, is SERVICE_UNAVAILABLE; so is a re-embed with
        # chunking fields through its route. A re-embed without them never
        # reads the collection, so it still starts.
        from routers import tuning as tuning_router
        from services import collection_writes
        from models.schemas import ReembedRequest
        sources.store("Coll", "empty.txt", b"one", replace=True)

        def run(job_id, collection, *args, **kwargs):
            collection_writes.end_job("tune", job_id)
            with tuning._lock:
                tuning._active.discard(collection)

        before = set(tuning._jobs)
        with patch.object(wc, "get_client", side_effect=ConnectionError("no route to weaviate")), \
                patch.object(tuning_router.wc, "collection_exists", AsyncMock(return_value=True)):
            with self.assertLogs(tuning._log, "ERROR"):
                response = asyncio.run(tuning_router.reembed(ReembedRequest(
                    collection="Coll", chunking_strategy="fixed", chunk_size=80)))
            self.assertEqual(response.status_code, 503)
            self.assertEqual(json.loads(response.body)["error"]["code"], "SERVICE_UNAVAILABLE")
            self.assertNotIn(b"no route", response.body)
            self.assertEqual(set(tuning._jobs), before)
            self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")), (set(), []))

            async def start():
                response = await tuning_router.reembed(ReembedRequest(collection="Coll"))
                for _ in range(200):
                    if not tuning._active:
                        break
                    await asyncio.sleep(0.01)
                return response

            with patch.object(tuning, "_run", run):
                response = asyncio.run(start())
        self.assertEqual(response.status, "queued")
        self.assertIn(response.job_id, set(tuning._jobs) - before)
        self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")), (set(), []))

    def test_a_weaviate_error_checking_the_collection_at_a_tune_start_is_a_503(self):
        # #349: the existence check every tune start makes was a bare 500 when
        # Weaviate failed. It is SERVICE_UNAVAILABLE, with no job, nothing
        # registered and the cause logged but not sent.
        from routers import tuning as tuning_router
        from services import collection_writes
        from models.schemas import RechunkRequest, ReembedRequest, ReindexRequest
        requests = {
            "rechunk": (tuning_router.rechunk, RechunkRequest(collection="Coll")),
            "reembed": (tuning_router.reembed, ReembedRequest(collection="Coll")),
            "reindex": (tuning_router.reindex, ReindexRequest(collection="Coll")),
        }
        before = set(tuning._jobs)
        with patch.object(tuning_router.wc, "collection_exists",
                          AsyncMock(side_effect=ConnectionError("weaviate down"))):
            for operation, (route, body) in requests.items():
                with self.subTest(operation=operation):
                    with self.assertLogs(logging.getLogger("routers.tuning"), "ERROR"):
                        response = asyncio.run(route(body))
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(json.loads(response.body)["error"], {
                        "code": "SERVICE_UNAVAILABLE",
                        "message": "Cannot check whether 'Coll' exists: Weaviate could not be "
                                   "read. No job was started; try again when Weaviate is "
                                   "available.",
                        "detail": {"collection": "Coll"}})
                    self.assertNotIn(b"weaviate down", response.body)
                    self.assertEqual(set(tuning._jobs), before)
                    self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")),
                                     (set(), []))

    def test_a_weaviate_error_reading_the_tuning_options_is_a_503(self):
        # #349: GET /tune/{collection} reads Weaviate for the existence check
        # and, with retained originals, for the re-chunk check. Either failing
        # was a bare 500; it is SERVICE_UNAVAILABLE, the cause logged but not sent.
        from routers import tuning as tuning_router
        sources.store("Coll", "a.txt", b"one", replace=True)

        def broken(**_):
            raise ConnectionError("weaviate down")

        coll = SimpleNamespace(iterator=broken)
        responses = {}
        with patch.object(tuning_router.wc, "collection_exists",
                          AsyncMock(side_effect=ConnectionError("weaviate down"))), \
                self.assertLogs(logging.getLogger("routers.tuning"), "ERROR"):
            responses["existence check"] = asyncio.run(tuning_router.tune_options("Coll"))
        with patch.object(tuning_router.wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(wc, "get_client", return_value=SimpleNamespace(
                    collections=SimpleNamespace(get=lambda name: coll))), \
                self.assertLogs(tuning._log, "ERROR"):
            responses["re-chunk check"] = asyncio.run(tuning_router.tune_options("Coll"))
        for case, response in responses.items():
            with self.subTest(case=case):
                self.assertEqual(response.status_code, 503)
                self.assertEqual(json.loads(response.body)["error"], {
                    "code": "SERVICE_UNAVAILABLE",
                    "message": "Cannot read the tuning options for 'Coll': Weaviate could not "
                               "be read. Try again when Weaviate is available.",
                    "detail": {"collection": "Coll"}})
                self.assertNotIn(b"weaviate down", response.body)

    def test_each_tune_route_answers_the_503_over_http(self):
        # #349, through the app as a client calls it: the JSON 503 reaches the
        # client (not a plain 500) from each tune start and from the tuning
        # options, for its existence check and its re-chunk check (#369).
        from fastapi.testclient import TestClient
        from main import app
        from auth import testing as auth_testing  # a real session in a temporary store (#25)
        from services import collection_writes
        client = TestClient(app, headers=auth_testing.headers())  # No lifespan: nothing connects to a stack.
        self.addCleanup(client.close)
        sources.store("Coll", "a.txt", b"one", replace=True)

        def broken(**_):
            raise ConnectionError("weaviate down")

        coll = SimpleNamespace(iterator=broken)
        unreadable = patch.object(wc, "collection_exists",
                                  AsyncMock(side_effect=ConnectionError("weaviate down")))
        cases = [
            (f"{operation} existence check", unreadable, "routers",
             lambda operation=operation: client.post(f"/tune/{operation}",
                                                     json={"collection": "Coll"}))
            for operation in ("rechunk", "reembed", "reindex")
        ] + [
            ("options existence check", unreadable, "routers", lambda: client.get("/tune/Coll")),
            ("options re-chunk check",
             patch.object(wc, "collection_exists", AsyncMock(return_value=True)), tuning._log,
             lambda: client.get("/tune/Coll")),
        ]
        before = set(tuning._jobs)
        for case, exists, logger, send in cases:
            with self.subTest(case=case), exists, \
                    patch.object(wc, "get_client", return_value=SimpleNamespace(
                        collections=SimpleNamespace(get=lambda name: coll))), \
                    self.assertLogs(logger, "ERROR"):
                response = send()
                self.assertEqual(response.status_code, 503, response.text)
                self.assertTrue(response.headers["content-type"].startswith("application/json"))
                self.assertEqual(response.json()["error"]["code"], "SERVICE_UNAVAILABLE")
                self.assertEqual(response.json()["error"]["detail"], {"collection": "Coll"})
                self.assertNotIn("weaviate down", response.text)
                self.assertEqual(set(tuning._jobs), before)
                self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")),
                                 (set(), []))

    def test_the_rechunk_check_503_reaches_the_client_over_http_at_each_tune_start(self):
        # #409: the 503 a re-chunk, and a re-embed with chunking fields, give when
        # their re-chunk coverage check can't read Weaviate (#340), through the
        # app as a client calls it: the JSON 503 reaches the client, whether the
        # walk fails or the connection does, with no job and the guards released.
        from fastapi.testclient import TestClient
        from main import app
        from auth import testing as auth_testing  # a real session in a temporary store (#25)
        from services import collection_writes
        client = TestClient(app, headers=auth_testing.headers())  # No lifespan: nothing connects to a stack.
        self.addCleanup(client.close)
        sources.store("Coll", "a.txt", b"one", replace=True)

        def broken(**_):
            raise ConnectionError("weaviate down")

        coll = SimpleNamespace(iterator=broken)
        requests = {
            "rechunk": ("/tune/rechunk", {"collection": "Coll"}),
            "reembed with chunking": ("/tune/reembed", {
                "collection": "Coll", "chunking_strategy": "fixed", "chunk_size": 80}),
        }
        failures = {
            "walk": {"return_value": SimpleNamespace(
                collections=SimpleNamespace(get=lambda name: coll))},
            "connection": {"side_effect": ConnectionError("weaviate down")},
        }
        before = set(tuning._jobs)
        for failure, effect in failures.items():
            for case, (path, body) in requests.items():
                with self.subTest(failure=failure, case=case), \
                        patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                        patch.object(wc, "get_client", **effect), \
                        self.assertLogs(tuning._log, "ERROR"):
                    response = client.post(path, json=body)
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertTrue(response.headers["content-type"].startswith("application/json"))
                    self.assertEqual(response.json()["error"], {
                        "code": "SERVICE_UNAVAILABLE",
                        "message": "Cannot check whether 'Coll' can be re-chunked: Weaviate "
                                   "could not be read. No job was started; try again when "
                                   "Weaviate is available.",
                        "detail": {"collection": "Coll"}})
                    self.assertNotIn("weaviate down", response.text)
                    self.assertEqual(set(tuning._jobs), before)
                    self.assertEqual((tuning._active, collection_writes.active_jobs("Coll")),
                                     (set(), []))

    def test_a_missing_collection_is_still_404_at_tune_starts_and_options(self):
        # #349 (reviewer): the existence check now sits in a try; a collection
        # Weaviate reports as missing is still COLLECTION_NOT_FOUND, with no job.
        from routers import tuning as tuning_router
        from services import collection_writes
        from models.schemas import RechunkRequest, ReembedRequest, ReindexRequest
        calls = {
            "rechunk": lambda: tuning_router.rechunk(RechunkRequest(collection="Gone")),
            "reembed": lambda: tuning_router.reembed(ReembedRequest(collection="Gone")),
            "reindex": lambda: tuning_router.reindex(ReindexRequest(collection="Gone")),
            "options": lambda: tuning_router.tune_options("Gone"),
        }
        before = set(tuning._jobs)
        with patch.object(tuning_router.wc, "collection_exists", AsyncMock(return_value=False)):
            for case, call in calls.items():
                with self.subTest(case=case):
                    response = asyncio.run(call())
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(json.loads(response.body)["error"]["code"],
                                     "COLLECTION_NOT_FOUND")
                    self.assertEqual(set(tuning._jobs), before)
                    self.assertEqual((tuning._active, collection_writes.active_jobs("Gone")),
                                     (set(), []))


class ExistenceCheckTests(unittest.TestCase):
    """#366: the other routers' collection existence checks, as #349 did for the tune starts."""

    def setUp(self):
        from routers import collections as collection_router
        from services import exporter, metrics, rag_pipeline
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.upload_dir = Path(temp.name)
        # Each route's next step, so a test can see that none of them ran.
        self.next_steps = {
            "run_query": AsyncMock(), "record": AsyncMock(), "start_generation": AsyncMock(),
            "create_collection": AsyncMock(), "delete_collection_when_idle": AsyncMock(),
            "start_export_job": AsyncMock(),
        }
        for change in (patch.object(settings, "upload_dir", temp.name),
                       patch.object(collection_router, "_REGISTRY_FILE", None),
                       patch.object(rag_pipeline, "run_query", self.next_steps["run_query"]),
                       patch.object(metrics, "record", self.next_steps["record"]),
                       patch.object(goldstandard, "start_generation",
                                    self.next_steps["start_generation"]),
                       patch.object(wc, "create_collection", self.next_steps["create_collection"]),
                       patch.object(wc, "delete_collection_when_idle",
                                    self.next_steps["delete_collection_when_idle"]),
                       patch.object(exporter, "start_export_job",
                                    self.next_steps["start_export_job"])):
            change.start(); self.addCleanup(change.stop)

    def routes(self):
        """(case, router logger, call, what the 503 says was not done) for each route."""
        from routers import collections as collection_router
        from routers import goldstandard as goldstandard_router
        from routers import query as query_router
        from routers import transfer as transfer_router
        from models.schemas import (CreateCollectionRequest, ExportRequest, GenerateRequest,
                                    QueryRequest)
        return [
            ("query", "routers.query",
             lambda: query_router.run_query(QueryRequest(question="q?", collection="Coll")),
             "No query was run"),
            ("goldstandard", "routers.goldstandard",
             lambda: goldstandard_router.generate(GenerateRequest(collection="Coll")),
             "No generation was started"),
            ("create", "routers.collections",
             lambda: collection_router.create_collection(CreateCollectionRequest(name="Coll")),
             "No collection was created"),
            ("delete", "routers.collections",
             lambda: collection_router.delete_collection("Coll", confirm="true"),
             "Nothing was deleted"),
            ("ingest", "routers.ingest",
             lambda: ingest.ingest_upload(
                 collection="Coll", strategy="fixed", chunk_size="200", chunk_overlap="0",
                 similarity_threshold="0.85", min_chunk_size="0",
                 files=[upload("a.txt", "text")]),
             "No job was started"),
            ("export", "routers.transfer",
             lambda: transfer_router.start_export(ExportRequest(collection="Coll")),
             "No job was started"),
        ]

    def assert_nothing_created(self, jobs_before):
        from services import collection_writes
        for name, step in self.next_steps.items():
            with self.subTest(step=name):
                step.assert_not_awaited()
        # No registry write, no saved upload, no ingest job or registration.
        self.assertEqual(list(self.upload_dir.iterdir()), [])
        self.assertEqual(set(ingest_pipeline._jobs), jobs_before)
        self.assertEqual(collection_writes.active_jobs("Coll"), [])

    def test_a_weaviate_error_checking_the_collection_is_a_503(self):
        # #366: each of these checks was a bare 500 (ingest: 500 INGEST_FAILED)
        # when Weaviate failed. It is SERVICE_UNAVAILABLE, with nothing started
        # or changed and the cause logged but not sent.
        jobs_before = set(ingest_pipeline._jobs)
        with patch.object(wc, "collection_exists",
                          AsyncMock(side_effect=ConnectionError("weaviate down"))):
            for case, logger, call, outcome in self.routes():
                with self.subTest(case=case):
                    with self.assertLogs(logging.getLogger(logger), "ERROR") as logs:
                        response = asyncio.run(call())
                    self.assertIsInstance(logs.records[0].exc_info[1], ConnectionError)
                    self.assertEqual(response.status_code, 503)
                    self.assertEqual(json.loads(response.body)["error"], {
                        "code": "SERVICE_UNAVAILABLE",
                        "message": "Cannot check whether 'Coll' exists: Weaviate could not be "
                                   f"read. {outcome}; try again when Weaviate is available.",
                        "detail": {"collection": "Coll"}})
                    self.assertNotIn(b"weaviate down", response.body)
                    self.assert_nothing_created(jobs_before)

    def test_a_collection_reported_missing_or_present_keeps_its_404_or_409(self):
        # #366: the checks now sit in a try; what Weaviate reports is answered
        # as before, with nothing started or changed.
        jobs_before = set(ingest_pipeline._jobs)
        for case, _, call, _ in self.routes():
            present = case == "create"
            with self.subTest(case=case), \
                    patch.object(wc, "collection_exists", AsyncMock(return_value=present)):
                response = asyncio.run(call())
                if present:
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(json.loads(response.body)["error"], {
                        "code": "COLLECTION_EXISTS",
                        "message": "Collection 'Coll' already exists.", "detail": None})
                else:
                    self.assertEqual(response.status_code, 404)
                    self.assertEqual(json.loads(response.body)["error"], {
                        "code": "COLLECTION_NOT_FOUND",
                        "message": "Collection 'Coll' not found.", "detail": None})
                self.assert_nothing_created(jobs_before)

    def test_each_route_answers_the_503_over_http(self):
        # #366, through the app as a client calls it: the JSON 503 reaches the
        # client (not a plain 500), for each of the six routes (#369).
        from fastapi.testclient import TestClient
        from main import app
        from auth import testing as auth_testing  # a real session in a temporary store (#25)
        client = TestClient(app, headers=auth_testing.headers())  # No lifespan: nothing connects to a stack.
        self.addCleanup(client.close)
        requests = {
            "query": lambda: client.post("/query", json={"question": "q?", "collection": "Coll"}),
            "goldstandard": lambda: client.post("/goldstandard/generate",
                                                json={"collection": "Coll"}),
            "create": lambda: client.post("/collections", json={"name": "Coll"}),
            "delete": lambda: client.delete("/collections/Coll?confirm=true"),
            "ingest": lambda: client.post("/ingest/upload",
                                          data={"collection": "Coll", "strategy": "fixed",
                                                "chunk_size": "200", "chunk_overlap": "0",
                                                "min_chunk_size": "0"},
                                          files={"files": ("a.txt", b"text")}),
            "export": lambda: client.post("/export", json={"collection": "Coll"}),
        }
        jobs_before = set(ingest_pipeline._jobs)
        with patch.object(wc, "collection_exists",
                          AsyncMock(side_effect=ConnectionError("weaviate down"))):
            for case, send in requests.items():
                with self.subTest(case=case), self.assertLogs("routers", "ERROR"):
                    response = send()
                    self.assertEqual(response.status_code, 503, response.text)
                    self.assertTrue(response.headers["content-type"].startswith(
                        "application/json"))
                    self.assertEqual(response.json()["error"]["code"], "SERVICE_UNAVAILABLE")
                    self.assertEqual(response.json()["error"]["detail"], {"collection": "Coll"})
                    self.assertNotIn("weaviate down", response.text)
                    self.assert_nothing_created(jobs_before)


class SchemaTests(unittest.TestCase):
    def test_a_record_written_before_the_field_reads_as_zero(self):
        old = {"job_id": "j", "status": "completed", "files_total": 1, "files_completed": 1,
               "files_failed": 0, "chunks_stored": 2, "errors": [], "skipped": []}
        self.assertEqual(JobStatusResponse(**old).chunks_replaced, 0)
        self.assertEqual(JobStatusResponse(**old, chunks_replaced=5).chunks_replaced, 5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
