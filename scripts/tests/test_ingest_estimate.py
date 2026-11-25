"""How long ingesting a file will take on this machine (issue #614).

The rate store (measured from stored parts, rolling, saved across restarts),
the estimate a job status computes from it, and the per-file progress the
ingest job loop records. Weaviate is replaced by an in-memory collection and
the rate file lives in a temporary upload folder; nothing reaches a backend.

Runs with the API's dependencies, in the API image with no network, as the
ingest suite runs it:

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
        "$(docker compose images -q api)" python scripts/tests/test_ingest_estimate.py

or on the host with the API's virtualenv: python scripts/tests/test_ingest_estimate.py
"""
import json
import os
import shutil
import sys
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from config import settings
from models.schemas import IngestJobEntry, JobStatusResponse
from services import chunker, goldstandard, ingest_estimate, ingest_pipeline
from services import weaviate_client as wc

NOW = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)


class UploadDir(unittest.TestCase):
    """A temporary UPLOAD_DIR and a rate store that starts unloaded."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        patcher = patch.object(settings, "upload_dir", str(self.dir))
        patcher.start()
        self.addCleanup(patcher.stop)
        ingest_estimate._reset_for_tests()
        self.addCleanup(ingest_estimate._reset_for_tests)

    @property
    def rate_file(self):
        return self.dir / ingest_estimate.RATE_FILE

    @property
    def old_rate_file(self):
        return self.dir / ingest_estimate.OLD_RATE_FILE

    def restart(self):
        """What an API restart does to the store: the memory is gone, the file stays."""
        ingest_estimate._reset_for_tests()


class RateStoreTests(UploadDir):

    def test_no_rate_before_any_measurement(self):
        self.assertIsNone(ingest_estimate.rate())
        self.assertFalse(self.rate_file.exists())

    def test_rate_is_characters_over_seconds_weighted_by_size(self):
        ingest_estimate.record(10_000, 10.0)   # 1,000 characters/s
        ingest_estimate.record(100, 1.0)       # 100 characters/s, but small
        self.assertAlmostEqual(ingest_estimate.rate(), 10_100 / 11.0)

    def test_window_keeps_the_newest_measurements(self):
        for _ in range(ingest_estimate.RATE_WINDOW):
            ingest_estimate.record(1_000, 100.0)     # 10 characters/s
        for _ in range(ingest_estimate.RATE_WINDOW):
            ingest_estimate.record(1_000, 1.0)       # 1,000 characters/s
        self.assertAlmostEqual(ingest_estimate.rate(), 1_000.0)
        saved = json.loads(self.rate_file.read_text())["samples"]
        self.assertEqual(len(saved), ingest_estimate.RATE_WINDOW)

    def test_the_rate_survives_a_restart(self):
        ingest_estimate.record(5_000, 2.0)
        self.restart()
        self.assertAlmostEqual(ingest_estimate.rate(), 2_500.0)
        saved = json.loads(self.rate_file.read_text())
        self.assertEqual(saved, {"samples": [[5_000, 2.0]]})   # counts and times only

    def test_a_malformed_file_is_no_rate_and_the_next_measurement_replaces_it(self):
        for content in ("not json", '{"samples": 3}', '{"other": []}', '{"samples": [[1, 2, 3]]}'):
            with self.subTest(content=content):
                self.rate_file.write_text(content)
                self.restart()
                with self.assertLogs(ingest_estimate._log, "WARNING"):
                    self.assertIsNone(ingest_estimate.rate())
                ingest_estimate.record(300, 3.0)
                self.assertAlmostEqual(ingest_estimate.rate(), 100.0)
                self.restart()
                self.assertAlmostEqual(ingest_estimate.rate(), 100.0)

    def test_invalid_saved_samples_are_skipped(self):
        self.rate_file.write_text(json.dumps({"samples": [[0, 1.0], [100, 0], [100, -1],
                                                          [True, 1.0], [400, 2.0]]}))
        self.assertAlmostEqual(ingest_estimate.rate(), 200.0)

    def test_measurements_with_nothing_or_no_time_are_ignored(self):
        for characters, seconds in ((0, 1.0), (100, 0.0), (100, -2.0), (-5, 1.0)):
            ingest_estimate.record(characters, seconds)
        self.assertIsNone(ingest_estimate.rate())
        self.assertFalse(self.rate_file.exists())

    def test_the_rate_file_is_outside_the_startup_sweep(self):
        # A name the sweep's prefixes match could be removed on every start (#896).
        from services import importer
        self.assertEqual(ingest_estimate.RATE_FILE, "embedding-rate.json")
        self.assertFalse(ingest_estimate.RATE_FILE.startswith(importer._WORKDIR_PREFIXES))

    def test_the_old_rate_file_is_read_until_the_new_one_exists(self):
        # An instance that measured before the rename keeps its rate (#896).
        self.old_rate_file.write_text(json.dumps({"samples": [[1_000, 2.0]]}))
        self.restart()
        self.assertAlmostEqual(ingest_estimate.rate(), 500.0)
        ingest_estimate.record(3_000, 2.0)
        self.assertEqual(json.loads(self.rate_file.read_text()),
                         {"samples": [[1_000, 2.0], [3_000, 2.0]]})
        self.assertTrue(self.old_rate_file.exists())           # left where it is
        self.old_rate_file.write_text(json.dumps({"samples": [[9, 9.0]]}))
        self.restart()
        self.assertAlmostEqual(ingest_estimate.rate(), 1_000.0)   # the new file only

    def test_a_malformed_new_rate_file_does_not_fall_back_to_the_old_one(self):
        # Only a missing new file falls back (#896); an unreadable one means no rate.
        self.old_rate_file.write_text(json.dumps({"samples": [[1_000, 2.0]]}))
        self.rate_file.write_text("not json")
        self.restart()
        with self.assertLogs(ingest_estimate._log, "WARNING"):
            self.assertIsNone(ingest_estimate.rate())

    def test_failed_saves_are_logged_once(self):
        with patch.object(ingest_estimate.settings_store, "publish", side_effect=OSError("disk full")), \
                self.assertLogs(ingest_estimate._log, "DEBUG") as logs:
            ingest_estimate.record(1_000, 4.0)
            ingest_estimate.record(1_000, 4.0)
        self.assertEqual([r.levelname for r in logs.records], ["WARNING", "DEBUG"])
        self.assertIsNotNone(logs.records[0].exc_info)
        self.assertIsNone(logs.records[1].exc_info)
        self.assertAlmostEqual(ingest_estimate.rate(), 250.0)   # both counted

    def test_a_failed_save_keeps_the_measurement_and_never_raises(self):
        with patch.object(ingest_estimate.settings_store, "publish", side_effect=OSError("disk full")), \
                self.assertLogs(ingest_estimate._log, "WARNING"):
            ingest_estimate.record(1_000, 4.0)
        self.assertAlmostEqual(ingest_estimate.rate(), 250.0)
        self.assertFalse(self.rate_file.exists())


def running_job(**fields):
    job = {"job_id": "j1", "status": "running", "files_total": 3, "files_completed": 0,
           "files_failed": 0, "chunks_stored": 0, "errors": [], "file_progress": [],
           "file_progress_omitted": 0, "files_started": 0, "file_chunking": False,
           "storing_since": None}
    job.update(fields)
    return job


def entry(file, chunks, characters, stored=0, stored_characters=0, chunked=False, failed=False):
    return {"file": file, "chunks": chunks, "chunks_stored": stored, "characters": characters,
            "characters_stored": stored_characters, "chunked": chunked, "failed": failed}


class ViewTests(UploadDir):

    def test_no_rate_means_no_estimate_yet(self):
        job = running_job(files_started=1, file_progress=[entry("a.txt", 10, 1_000)])
        view = ingest_estimate.view(job, NOW)
        self.assertIsNone(view["rate_characters_per_second"])
        self.assertIsNone(view["seconds_left"])
        self.assertIsNone(view["file_progress"][0]["seconds_left"])
        self.assertEqual(view["files_not_chunked"], 2)
        self.assertTrue(view["seconds_left_is_minimum"])

    def test_time_left_from_the_saved_rate(self):
        ingest_estimate.record(1_000, 1.0)                     # 1,000 characters/s
        job = running_job(files_total=2, files_started=2, file_progress=[
            entry("a.txt", 5, 4_000, 5, 4_000, chunked=True),
            entry("b.txt", 20, 30_000, 10, 10_000, chunked=True)])
        view = ingest_estimate.view(job, NOW)
        self.assertEqual(view["rate_characters_per_second"], 1_000.0)
        self.assertEqual([f["seconds_left"] for f in view["file_progress"]], [0.0, 20.0])
        self.assertEqual(view["seconds_left"], 20.0)
        self.assertFalse(view["seconds_left_is_minimum"])
        self.assertEqual(view["files_not_chunked"], 0)

    def test_the_part_being_stored_counts_down_never_below_zero(self):
        since = (NOW - timedelta(seconds=30)).isoformat()
        job = running_job(files_total=1, files_started=1, storing_since=since,
                          file_progress=[entry("a.txt", 100, 100_000, chunked=True)])
        view = ingest_estimate.view(job, NOW, current_rate=1_000.0)
        self.assertEqual(view["file_progress"][0]["seconds_left"], 70.0)
        late = ingest_estimate.view(job, NOW + timedelta(hours=1), current_rate=1_000.0)
        self.assertEqual(late["file_progress"][0]["seconds_left"], 0.0)
        self.assertEqual(late["seconds_left"], 0.0)
        broken = ingest_estimate.view({**job, "storing_since": "yesterday"}, NOW, current_rate=1_000.0)
        self.assertEqual(broken["file_progress"][0]["seconds_left"], 100.0)

    def test_a_failed_file_has_nothing_left(self):
        job = running_job(files_total=2, files_started=2, file_progress=[
            entry("bad.txt", 10, 10_000, failed=True), entry("b.txt", 1, 500, chunked=True)])
        view = ingest_estimate.view(job, NOW, current_rate=100.0)
        self.assertEqual([f["seconds_left"] for f in view["file_progress"]], [0.0, 5.0])
        self.assertFalse(view["seconds_left_is_minimum"])

    def test_a_split_file_with_more_parts_to_come_makes_it_a_minimum(self):
        job = running_job(files_total=1, files_started=1,
                          file_progress=[entry("big.jsonl", 9_000, 9_000_000, 9_000, 9_000_000)])
        view = ingest_estimate.view(job, NOW, current_rate=1_000.0)
        self.assertEqual(view["seconds_left"], 0.0)
        self.assertEqual(view["files_not_chunked"], 0)
        self.assertTrue(view["seconds_left_is_minimum"])

    def test_a_file_being_parsed_is_not_chunked(self):
        # Started, no entry yet (#896): not chunked, so the time left is a minimum.
        parsing = running_job(files_total=1, files_started=1, file_chunking=True)
        view = ingest_estimate.view(parsing, NOW, current_rate=1_000.0)
        self.assertEqual(view["files_not_chunked"], 1)
        self.assertEqual(view["seconds_left"], 0.0)
        self.assertTrue(view["seconds_left_is_minimum"])
        second = running_job(files_total=2, files_started=2, file_chunking=True, file_progress=[
            entry("a.txt", 5, 4_000, 5, 4_000, chunked=True)])
        view = ingest_estimate.view(second, NOW, current_rate=1_000.0)
        self.assertEqual(view["files_not_chunked"], 1)
        self.assertTrue(view["seconds_left_is_minimum"])
        for job in (parsing, second):               # a record without the field, as before
            del job["file_chunking"]
            view = ingest_estimate.view(job, NOW, current_rate=1_000.0)
            self.assertEqual(view["files_not_chunked"], 0)
            self.assertFalse(view["seconds_left_is_minimum"])

    def test_only_a_running_job_has_an_estimate(self):
        ingest_estimate.record(1_000, 1.0)
        for status in ("queued", "completed", "partial", "failed"):
            with self.subTest(status=status):
                job = running_job(status=status, files_started=1,
                                  file_progress=[entry("a.txt", 10, 1_000, 5, 500)])
                view = ingest_estimate.view(job, NOW)
                self.assertIsNone(view["rate_characters_per_second"])
                self.assertIsNone(view["seconds_left"])
                self.assertFalse(view["seconds_left_is_minimum"])
                self.assertIsNone(view["file_progress"][0]["seconds_left"])
                self.assertEqual(view["file_progress"][0]["chunks"], 10)   # counts stay

    def test_an_old_record_reads_back_with_empty_fields(self):
        ingest_estimate.record(1_000, 1.0)
        old = {"job_id": "old", "status": "completed", "files_total": 2, "files_completed": 2,
               "files_failed": 0, "chunks_stored": 7, "errors": []}
        view = ingest_estimate.view(old, NOW)
        self.assertEqual(view, {"file_progress": [], "file_progress_omitted": 0,
                                "files_not_chunked": None, "rate_characters_per_second": None,
                                "seconds_left": None, "seconds_left_is_minimum": False})
        status = JobStatusResponse(**{**old, **view})
        self.assertEqual(status.file_progress, [])
        listed = IngestJobEntry.model_validate(old)
        self.assertEqual((listed.file_progress, listed.seconds_left), ([], None))

    def test_the_status_and_the_list_take_the_fields(self):
        job = running_job(files_started=1, storing_since=NOW.isoformat(),
                          file_progress=[entry("a.txt", 10, 1_000)])
        status = JobStatusResponse(**{**job, **ingest_estimate.view(job, NOW, current_rate=50.0)})
        self.assertEqual(status.file_progress[0].seconds_left, 20.0)
        self.assertEqual((status.seconds_left, status.rate_characters_per_second), (20.0, 50.0))
        self.assertNotIn("storing_since", status.model_dump())
        self.assertNotIn("files_started", status.model_dump())
        self.assertNotIn("file_chunking", status.model_dump())
        listed = IngestJobEntry.model_validate(job)        # the job list: no computed times
        self.assertEqual(listed.file_progress[0].chunks, 10)
        self.assertIsNone(listed.file_progress[0].seconds_left)
        self.assertIsNone(listed.rate_characters_per_second)

    def test_the_status_route_adds_the_estimate(self):
        from routers import ingest
        import asyncio
        ingest_estimate.record(2_000, 1.0)
        job_id = "e" + uuid.uuid4().hex[:7]
        ingest_pipeline._jobs[job_id] = running_job(
            job_id=job_id, files_started=1, file_progress=[entry("a.txt", 10, 4_000)])
        self.addCleanup(ingest_pipeline._jobs.pop, job_id, None)
        answer = asyncio.run(ingest.job_status(job_id))
        self.assertEqual(answer.rate_characters_per_second, 2_000.0)
        self.assertEqual(answer.seconds_left, 2.0)
        self.assertEqual(answer.files_not_chunked, 2)


class FakeCollection:
    """Inserted chunks in a list, as test_split_parts.py keeps them."""

    def __init__(self):
        self.chunks = []
        self.fail_insert_at = None
        self.inserts = 0

    def insert(self, collection, chunks):
        self.inserts += 1
        if self.fail_insert_at == self.inserts:
            raise RuntimeError("Weaviate rejected the batch")
        self.chunks.extend(dict(c) for c in chunks)

    def has(self, collection, name, accept=None):
        return False

    def delete_new(self, collection, name, created_at):
        stamp = created_at.isoformat()
        before = len(self.chunks)
        self.chunks = [c for c in self.chunks if c["created_at"] != stamp]
        return before - len(self.chunks)


class JobLoopTests(UploadDir):

    def run_job(self, files, chunk_size=1000, backend=None):
        backend = backend or FakeCollection()
        tmp = Path(tempfile.mkdtemp())  # the job removes it when it ends
        paths, names = [], {}
        for name, data in files:
            path = tmp / str(len(paths)) / Path(name).name
            path.parent.mkdir()
            path.write_bytes(data.encode() if isinstance(data, str) else data)
            paths.append(path)
            names[path] = name
        job_id = "t" + uuid.uuid4().hex[:7]
        ingest_pipeline._jobs[job_id] = running_job(
            job_id=job_id, status="queued", files_total=len(paths), chunks_submitted=0,
            chunks_accepted=0, chunks_replaced=0, skipped=[], split=[])
        self.addCleanup(ingest_pipeline._jobs.pop, job_id, None)
        self.seen = []
        record = ingest_estimate.record

        def spy(characters, seconds):
            self.seen.append((characters, seconds))
            record(characters, seconds)

        with patch.object(wc, "_insert_chunks_sync", side_effect=backend.insert), \
                patch.object(wc, "_source_has_chunks_sync", side_effect=backend.has), \
                patch.object(wc, "_delete_new_family_sync", side_effect=backend.delete_new, create=True), \
                patch.object(wc, "_collection_exists_sync", return_value=True), \
                patch.object(goldstandard, "mark_stale", return_value=None), \
                patch.object(ingest_pipeline.sources, "store", return_value=None), \
                patch.object(ingest_estimate, "record", side_effect=spy):
            ingest_pipeline._process_job_sync(job_id, paths, tmp, "Coll", "fixed", chunk_size,
                                              0, 0.85, 0, None, names)
        return ingest_pipeline._jobs[job_id], backend

    def test_a_whole_file_gets_one_entry_and_one_measurement(self):
        text = "word " * 2_000                                   # 10,000 characters
        job, backend = self.run_job([("a.json", json.dumps({"t": text}))])
        self.assertEqual(job["status"], "completed", job["errors"])
        [f] = job["file_progress"]
        characters = sum(len(c["content"]) for c in backend.chunks)
        self.assertEqual(f, entry("a.json", len(backend.chunks), characters, len(backend.chunks),
                                  characters, chunked=True))
        self.assertEqual(len(self.seen), 1)
        self.assertEqual(self.seen[0][0], characters)
        self.assertGreater(self.seen[0][1], 0)
        self.assertIsNotNone(ingest_estimate.rate())             # saved for the next job
        self.assertEqual((job["files_started"], job["storing_since"]), (1, None))
        view = ingest_estimate.view(job, NOW)
        self.assertIsNone(view["seconds_left"])                  # finished: no time left

    def test_a_split_file_has_one_entry_chunked_only_after_its_last_part(self):
        # 13,000 records of 600 characters: one chunk each under context_aware is
        # how test_split_parts.py splits; with fixed 1,000 the text is over the
        # text limit, so the file is read in parts.
        line = json.dumps({"t": "x" * 700}) + "\n"
        count = chunker.MAX_TEXT_CHARACTERS // 690 + 500
        states = []
        part_stored = ingest_estimate.part_stored

        def watch(job, *args):
            part_stored(job, *args)
            states.append(dict(job["file_progress"][-1]))

        with patch.object(ingest_estimate, "part_stored", side_effect=watch):
            job, backend = self.run_job([("big.jsonl", line * count)])
        self.assertEqual(job["status"], "completed", job["errors"])
        self.assertTrue(job["split"], job)
        [f] = job["file_progress"]
        self.assertEqual(f["chunks"], len(backend.chunks))
        self.assertEqual(f["chunks_stored"], len(backend.chunks))
        self.assertTrue(f["chunked"])
        self.assertGreaterEqual(len(states), 2)
        self.assertTrue(all(not s["chunked"] for s in states))  # never before the end
        self.assertEqual(len(self.seen), len(states))           # one measurement a part

    def test_a_failed_store_is_not_a_measurement_and_the_file_is_failed(self):
        backend = FakeCollection()
        backend.fail_insert_at = 1
        job, _ = self.run_job([("a.json", json.dumps({"t": "hello world"})), ("b.json", json.dumps({"t": "fine"}))],
                              backend=backend)
        self.assertEqual(job["status"], "partial")
        bad, good = job["file_progress"]
        self.assertEqual((bad["file"], bad["failed"], bad["chunked"]), ("a.json", True, False))
        self.assertEqual(bad["chunks_stored"], 0)
        self.assertEqual((good["failed"], good["chunked"]), (False, True))
        self.assertEqual(len(self.seen), 1)                     # only b.json's store

    def test_a_file_that_fails_before_any_chunks_has_no_entry_but_is_started(self):
        job, _ = self.run_job([("broken.json", "{not json"), ("b.json", json.dumps({"t": "ok"}))])
        self.assertEqual([f["file"] for f in job["file_progress"]], ["b.json"])
        self.assertEqual(job["files_started"], 2)
        self.assertEqual(job["files_failed"], 1)

    def test_a_one_file_job_read_before_its_first_part(self):
        # With a saved rate, the status while the file is parsed must not read as
        # nothing left (#896): the file is not chunked yet.
        ingest_estimate.record(1_000, 1.0)
        views = []
        part_chunked = ingest_estimate.part_chunked

        def watch(job, *args, **kwargs):
            views.append(ingest_estimate.view(job, NOW))
            part_chunked(job, *args, **kwargs)

        with patch.object(ingest_estimate, "part_chunked", side_effect=watch):
            job, _ = self.run_job([("a.json", json.dumps({"t": "word " * 500}))])
        self.assertEqual(job["status"], "completed", job["errors"])
        first = views[0]
        self.assertEqual(first["files_not_chunked"], 1)
        self.assertTrue(first["seconds_left_is_minimum"])
        self.assertEqual(first["seconds_left"], 0.0)
        self.assertIs(job["file_chunking"], False)
        self.assertEqual(ingest_estimate.view(job, NOW)["files_not_chunked"], 0)

    def test_a_last_file_failing_before_any_chunks_clears_the_flag(self):
        job, _ = self.run_job([("b.json", json.dumps({"t": "ok"})), ("broken.json", "{not json")])
        self.assertEqual(job["files_failed"], 1)
        self.assertEqual([f["file"] for f in job["file_progress"]], ["b.json"])
        self.assertIs(job["file_chunking"], False)
        self.assertEqual(ingest_estimate.view(job, NOW)["files_not_chunked"], 0)

    def test_a_middle_file_failing_before_any_chunks_leaves_earlier_entries_alone(self):
        job, _ = self.run_job([("a.json", json.dumps({"t": "ok"})), ("broken.json", "{not json"),
                               ("c.json", json.dumps({"t": "fine"}))])
        self.assertEqual(job["files_failed"], 1)
        self.assertEqual([(f["file"], f["failed"], f["chunked"]) for f in job["file_progress"]],
                         [("a.json", False, True), ("c.json", False, True)])
        self.assertIs(job["file_chunking"], False)

    def test_the_record_keeps_the_newest_entries(self):
        files = [(f"f{i}.json", json.dumps({"t": f"text {i}"})) for i in range(5)]
        with patch.object(ingest_estimate, "FILE_PROGRESS_LIMIT", 2):
            job, _ = self.run_job(files)
        self.assertEqual([f["file"] for f in job["file_progress"]], ["f3.json", "f4.json"])
        self.assertEqual(job["file_progress_omitted"], 3)
        self.assertEqual(job["files_started"], 5)

    def test_a_record_made_without_the_fields_still_works(self):
        job = {"files_failed": 0}
        ingest_estimate.file_started(job)
        self.assertIs(job["file_chunking"], True)
        ingest_estimate.part_chunked(job, "a", 2, 20, first=True)
        self.assertIs(job["file_chunking"], False)
        self.assertIsNotNone(job["storing_since"])
        ingest_estimate.part_stored(job, 2, 20, 0.5)
        ingest_estimate.file_ended(job, failed=False)
        self.assertEqual(job["file_progress"], [entry("a", 2, 20, 2, 20, chunked=True)])
        self.assertEqual((job["files_started"], job["storing_since"]), (1, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
