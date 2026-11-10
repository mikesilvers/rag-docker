"""Running and recent jobs (#617): what job records carry, and what the lists show.

Runs with the API's dependencies, in the built API image with no network
(`02_ingest.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_job_lists.py

Requests go through `main.app` with FastAPI's TestClient and real sessions in a
temporary account store (`auth.testing`), without the lifespan. Job stores are
bound to a temporary folder; nothing reaches a backend.
"""
import asyncio
import io
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi.testclient import TestClient

from config import settings
from main import app
from auth import store as auth_store
from auth import testing as auth_testing
from services import exporter, goldstandard, importer, ingest_pipeline, job_lists, jobstore, packager
from services import weaviate_client as wc

DEV = SimpleNamespace(username="dev", kind="user", role="developer")
OTHER = SimpleNamespace(username="other", kind="user", role="developer")
KEY = SimpleNamespace(username="dev", kind="service", role="developer")
ENG = SimpleNamespace(username="eng", kind="user", role="engineer")
NOW = time.time()


def iso(seconds_ago: float) -> str:
    return datetime.fromtimestamp(NOW - seconds_ago, timezone.utc).isoformat()


def owner(name="dev", kind="user"):
    return {"name": name, "kind": kind}


def ingest_record(job_id, status="completed", **extra):
    record = {"job_id": job_id, "status": status, "files_total": 1, "files_completed": 1,
              "files_failed": 0, "chunks_stored": 2, "errors": [], "skipped": []}
    record.update(extra)
    return record


class TempUploadCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        change = patch.object(settings, "upload_dir", temp.name)
        change.start()
        self.addCleanup(change.stop)

    def bound(self, kind):
        store = jobstore.JobStore(kind)
        self.assertEqual(store.load(), [])
        return store


class RecordTimestampTests(TempUploadCase):
    def test_new_record_gets_created_at_and_no_finished_at(self):
        store = self.bound("ingest")
        store["a1"] = ingest_record("a1", status="queued")
        self.assertIsNotNone(job_lists._parse(store["a1"]["created_at"]))
        self.assertNotIn("finished_at", store["a1"])
        on_disk = json.loads((self.root / "jobs" / "ingest" / "a1.json").read_text())
        self.assertEqual(on_disk["created_at"], store["a1"]["created_at"])

    def test_finished_at_on_first_terminal_status_and_never_moved(self):
        store = self.bound("export")
        store["e1"] = {"job_id": "e1", "status": "queued"}
        store["e1"]["status"] = "running"
        self.assertNotIn("finished_at", store["e1"])
        store["e1"]["status"] = "completed"
        first = store["e1"]["finished_at"]
        on_disk = json.loads((self.root / "jobs" / "export" / "e1.json").read_text())
        self.assertEqual(on_disk["finished_at"], first)
        time.sleep(0.01)
        store["e1"].update(status="failed", error="later")
        self.assertEqual(store["e1"]["finished_at"], first)

    def test_a_replaced_record_keeps_its_timestamps(self):
        store = self.bound("export")
        store["r1"] = {"job_id": "r1", "status": "queued"}
        store["r1"]["status"] = "completed"
        created, finished = store["r1"]["created_at"], store["r1"]["finished_at"]
        time.sleep(0.01)
        store["r1"] = {"job_id": "r1", "status": "failed", "error": "replaced"}
        self.assertEqual((store["r1"]["created_at"], store["r1"]["finished_at"]), (created, finished))
        on_disk = json.loads((self.root / "jobs" / "export" / "r1.json").read_text())
        self.assertEqual((on_disk["created_at"], on_disk["finished_at"]), (created, finished))

    def test_interrupted_at_startup_gets_finished_at(self):
        directory = self.root / "jobs" / "import"
        directory.mkdir(parents=True)
        (directory / "i1.json").write_text(json.dumps({"job_id": "i1", "status": "running"}))
        store = jobstore.JobStore("import")
        self.assertEqual(store.load(), ["i1"])
        on_disk = json.loads((directory / "i1.json").read_text())
        self.assertEqual(on_disk["error_code"], "INTERRUPTED")
        self.assertIsNotNone(job_lists._parse(on_disk["finished_at"]))

    def test_snapshot_copies_and_saved_time(self):
        directory = self.root / "jobs" / "ingest"
        directory.mkdir(parents=True)
        path = directory / "old1.json"
        path.write_text(json.dumps({**ingest_record("old1"), "_audit": {"actor": "x"}}))
        stamp = NOW - 3600
        os.utime(path, (stamp, stamp))
        store = jobstore.JobStore("ingest")
        store.load()
        [(job_id, record, saved)] = store.snapshot()
        self.assertEqual(job_id, "old1")
        self.assertNotIn("_audit", record)
        self.assertAlmostEqual(saved, stamp, delta=1)
        record["status"] = "changed"
        self.assertEqual(store["old1"]["status"], "completed")

    def test_unbound_store_is_unchanged(self):
        store = jobstore.JobStore("ingest")
        doc = {"status": "queued"}
        store["u1"] = doc
        self.assertIs(store["u1"], doc)
        self.assertEqual(doc, {"status": "queued"})
        self.assertEqual(store.snapshot(), [("u1", {"status": "queued"}, None)])


class SelectTests(unittest.TestCase):
    def items(self, *records):
        return [(r, r["status"] == "running", job_lists._parse(r.get("finished_at"))) for r in records]

    def ids(self, items, principal):
        return [r["job_id"] for r in job_lists.select(items, principal, NOW)]

    def test_developer_sees_only_own_jobs_and_engineer_sees_all(self):
        items = self.items(
            {"job_id": "mine", "status": "running", "owner": owner(), "created_at": iso(10)},
            {"job_id": "theirs", "status": "running", "owner": owner("other"), "created_at": iso(20)},
            {"job_id": "keys", "status": "running", "owner": owner("dev", "service"), "created_at": iso(30)},
            {"job_id": "unknown", "status": "running", "created_at": iso(40)},
            {"job_id": "nullowner", "status": "running", "owner": None, "created_at": iso(50)})
        self.assertEqual(self.ids(items, DEV), ["mine"])
        self.assertEqual(self.ids(items, OTHER), ["theirs"])
        self.assertEqual(self.ids(items, KEY), ["keys"])
        self.assertEqual(self.ids(items, ENG), ["mine", "theirs", "keys", "unknown", "nullowner"])

    def test_running_always_listed_and_finished_capped_to_newest_twenty(self):
        running = [{"job_id": f"r{i}", "status": "running", "owner": owner(), "created_at": iso(i)}
                   for i in range(25)]
        finished = [{"job_id": f"f{i}", "status": "completed", "owner": owner(),
                     "created_at": iso(1000 + i), "finished_at": iso(500 + i)} for i in range(25)]
        listed = self.ids(self.items(*running, *finished), DEV)
        self.assertEqual(len([i for i in listed if i.startswith("r")]), 25)
        self.assertEqual([i for i in listed if i.startswith("f")], [f"f{i}" for i in range(20)])

    def test_finished_outside_the_window_is_dropped(self):
        items = self.items(
            {"job_id": "recent", "status": "failed", "owner": owner(), "finished_at": iso(23 * 3600)},
            {"job_id": "old", "status": "failed", "owner": owner(), "finished_at": iso(25 * 3600)})
        self.assertEqual(self.ids(items, DEV), ["recent"])

    def test_finished_at_boundary_of_the_window(self):
        inside = {"job_id": "in", "status": "completed", "owner": owner()}
        outside = {"job_id": "out", "status": "completed", "owner": owner()}
        listed = job_lists.select([(inside, False, NOW - job_lists.WINDOW_S),
                                   (outside, False, NOW - job_lists.WINDOW_S - 1)], DEV, NOW)
        self.assertEqual([r["job_id"] for r in listed], ["in"])

    def test_a_finish_time_after_the_clock_is_listed(self):
        # The API's clock stepped back: a just-finished job stays listed (#629).
        listed = job_lists.select([({"job_id": "ahead", "status": "completed", "owner": owner()}, False, NOW + 3600)],
                                  DEV, NOW)
        self.assertEqual([r["job_id"] for r in listed], ["ahead"])
        self.assertAlmostEqual(job_lists._parse(listed[0]["finished_at"]), NOW + 3600, delta=1)

    def test_order_newest_first_by_created_at_else_finish(self):
        items = self.items(
            {"job_id": "a", "status": "completed", "owner": owner(), "created_at": iso(300), "finished_at": iso(10)},
            {"job_id": "b", "status": "running", "owner": owner(), "created_at": iso(100)},
            {"job_id": "c", "status": "completed", "owner": owner(), "finished_at": iso(200)})
        self.assertEqual(self.ids(items, DEV), ["b", "c", "a"])

    def test_listed_finished_at_is_the_finish_time_and_none_while_running(self):
        listed = job_lists.select([({"job_id": "x", "status": "completed", "owner": owner()}, False, NOW - 60),
                                   ({"job_id": "y", "status": "running", "owner": owner()}, True, None)],
                                  DEV, NOW)
        by_id = {r["job_id"]: r for r in listed}
        self.assertAlmostEqual(job_lists._parse(by_id["x"]["finished_at"]), NOW - 60, delta=1)
        self.assertIsNone(by_id["y"]["finished_at"])

    def test_job_items_fall_back_to_saved_time_for_old_records(self):
        rows = [("old", {"status": "completed"}, NOW - 60),
                ("new", {"status": "completed", "finished_at": iso(120)}, NOW),
                ("run", {"status": "queued"}, NOW),
                ("lost", {"status": "partial"}, None)]
        items = {r_id: item for (r_id, _, _), item in zip(rows, job_lists.job_items(rows))}
        self.assertEqual(items["old"][2], NOW - 60)
        self.assertAlmostEqual(items["new"][2], NOW - 120, delta=1)
        self.assertEqual(items["run"][1:], (True, None))
        self.assertIsNone(items["lost"][2])         # never listed

    def test_session_items(self):
        sessions = [{"session_id": "gen", "status": "generating"},
                    {"session_id": "done", "status": "completed", "finished_at": iso(60)},
                    {"session_id": "legacy", "status": "completed"}]
        items = job_lists.session_items(sessions)
        self.assertEqual([(i[1], i[2] is not None) for i in items], [(True, False), (False, True), (False, False)])
        listed = job_lists.select([(dict(s, owner=owner()), r, f) for s, r, f in items], DEV, NOW)
        # "gen" has neither a start nor a finish time, so it sorts last (S3.6).
        self.assertEqual([s["session_id"] for s in listed], ["done", "gen"])


def session(session_id, **extra):
    record = {"session_id": session_id, "collection": "OwnedLists", "status": "completed",
              "pairs_total": 0, "pairs_completed": 0, "pairs": []}
    record.update(extra)
    return record


class GoldStandardTests(TempUploadCase):
    def setUp(self):
        super().setUp()
        for attribute, value in (("_sessions", {}), ("_diagnostics", {}), ("_scan_cache", {})):
            patcher = patch.object(goldstandard, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_generation_sessions_leave_out_imported_ones(self):
        goldstandard.store_session(session("gs_0000000a", owner=owner(), created_at=iso(5)))
        goldstandard.store_session(session("gs_0000000b", imported_from={
            "session_id": "gs_0000000c", "collection": "X", "imported_at": iso(1)}))
        self.assertEqual([s["session_id"] for s in goldstandard.generation_sessions()], ["gs_0000000a"])

    def test_settling_a_generation_sets_finished_at_once(self):
        goldstandard.store_session(session("gs_0000000d", status="generating", pairs_total=1,
                                           owner=owner(), created_at=iso(5)))
        asyncio.run(goldstandard._settle_generation("gs_0000000d", cancelled=False))
        settled = goldstandard.get_session("gs_0000000d")
        self.assertEqual(settled["status"], "completed")
        first = settled["finished_at"]
        self.assertIsNotNone(job_lists._parse(first))
        goldstandard._failed_generation(settled, RuntimeError("later"))
        self.assertEqual(settled["finished_at"], first)

    def test_failed_generation_sets_finished_at(self):
        current = {"status": "generating"}
        goldstandard._failed_generation(current, RuntimeError("boom"))
        self.assertEqual(current["status"], "failed")
        self.assertIsNotNone(job_lists._parse(current["finished_at"]))

    def test_restart_settlement_sets_finished_at(self):
        goldstandard.store_session(session("gs_0000000e", status="generating", pairs_total=1))
        with patch.object(goldstandard, "_tasks", set()):
            goldstandard.reconcile_interrupted_generations()
        self.assertIsNotNone(job_lists._parse(goldstandard.get_session("gs_0000000e")["finished_at"]))

    def test_import_drops_local_fields(self):
        saved = goldstandard.store_imported_session(
            session("gs_0000000f", owner=owner(), created_at=iso(5), finished_at=iso(1)), "Source")
        for key in goldstandard.LOCAL_FIELDS:
            self.assertNotIn(key, saved)
            self.assertNotIn(key, goldstandard.get_session(saved["session_id"]))
        self.assertEqual(goldstandard.generation_sessions(), [])

    def test_packages_leave_out_local_fields(self):
        with patch.object(goldstandard, "_sessions_on_disk", return_value=[
                session("gs_00000010", owner=owner(), created_at=iso(5), finished_at=iso(1))]):
            [packed] = packager._goldstandard_sessions("OwnedLists")
        for key in goldstandard.LOCAL_FIELDS:
            self.assertNotIn(key, packed)
        self.assertEqual(packed["session_id"], "gs_00000010")


class RouteTests(TempUploadCase):
    def setUp(self):
        super().setUp()
        self.stores = {}
        for module, kind in ((ingest_pipeline, "ingest"), (exporter, "export"), (importer, "import")):
            store = self.bound(kind)
            self.stores[kind] = store
            patcher = patch.object(module, "_jobs", store)
            patcher.start()
            self.addCleanup(patcher.stop)
        for attribute, value in (("_sessions", {}), ("_diagnostics", {}), ("_scan_cache", {})):
            patcher = patch.object(goldstandard, attribute, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.dev_headers = auth_testing.headers("developer")
        self.eng_headers = auth_testing.headers("engineer")
        self.dev = auth_testing._accounts["developer"].username
        name = f"vfy-s-lists-{time.time_ns()}"
        auth_store.create_principal(name, "developer")
        _, secret = auth_store.issue_key(name)
        self.key_name = name
        self.key_headers = {"Authorization": f"Bearer {secret}"}
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def get(self, path, headers):
        response = self.client.get(path, headers=headers)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_lists_are_scoped_and_carry_the_new_fields(self):
        ingest = self.stores["ingest"]
        ingest["d1"] = ingest_record("d1", status="running", owner=owner(self.dev),
                                     collection="OwnedLists", files=["a.txt", "b.md"])
        ingest["k1"] = ingest_record("k1", owner=owner(self.key_name, "service"), collection="OwnedLists",
                                     files=["c.txt"])
        ingest["o1"] = ingest_record("o1")                          # from before #617
        mine = self.get("/ingest/jobs", self.dev_headers)["jobs"]
        self.assertEqual([j["job_id"] for j in mine], ["d1"])
        self.assertEqual(mine[0]["owner"], owner(self.dev))
        self.assertEqual((mine[0]["collection"], mine[0]["files"]), ("OwnedLists", ["a.txt", "b.md"]))
        self.assertIsNotNone(mine[0]["created_at"])
        self.assertIsNone(mine[0]["finished_at"])
        keys = self.get("/ingest/jobs", self.key_headers)["jobs"]
        self.assertEqual([j["job_id"] for j in keys], ["k1"])
        self.assertIsNotNone(keys[0]["finished_at"])
        everyone = self.get("/ingest/jobs", self.eng_headers)["jobs"]
        self.assertEqual(sorted(j["job_id"] for j in everyone), ["d1", "k1", "o1"])
        self.assertIsNone(next(j for j in everyone if j["job_id"] == "o1")["owner"])

    def test_export_import_and_session_lists(self):
        self.stores["export"]["e1"] = {
            "job_id": "e1", "status": "completed", "collection": "OwnedLists", "chunks_written": 3,
            "filename": "ragpkg-x.tar.gz", "size_bytes": 10, "source_document_count": 1,
            "fidelity": "with-sources", "models_bundled": False, "retrieve_script": True,
            "warnings": [], "error": None, "error_code": None, "error_detail": None,
            "owner": owner(self.dev)}
        self.stores["import"]["i1"] = {
            "job_id": "i1", "status": "failed", "filename": "ragpkg-x.tar.gz", "on_conflict": "abort",
            "collection": None, "original_collection": None, "chunks_written": 0, "fidelity": None,
            "renamed": False, "notes": [], "error": "Interrupted", "error_code": "INTERRUPTED",
            "error_detail": None, "owner": owner(self.dev)}
        goldstandard.store_session(session("gs_00000011", status="generating", pairs_total=4,
                                           pairs_attempted=1, owner=owner(self.dev), created_at=iso(5),
                                           pairs=[]))
        goldstandard.store_session(session("gs_00000012", owner=owner(self.dev)))   # no finished_at
        exports = self.get("/export/jobs", self.dev_headers)["jobs"]
        self.assertEqual([(j["job_id"], j["filename"]) for j in exports], [("e1", "ragpkg-x.tar.gz")])
        imports = self.get("/import/jobs", self.dev_headers)["jobs"]
        self.assertEqual([(j["job_id"], j["error_code"]) for j in imports], [("i1", "INTERRUPTED")])
        self.assertEqual(self.get("/export/jobs", self.key_headers)["jobs"], [])
        sessions = self.get("/goldstandard/sessions", self.dev_headers)["sessions"]
        self.assertEqual([(s["session_id"], s["pairs_attempted"]) for s in sessions], [("gs_00000011", 1)])
        self.assertNotIn("pairs", sessions[0])
        self.assertEqual(self.get("/goldstandard/sessions", self.key_headers)["sessions"], [])

    def test_a_malformed_record_is_left_out(self):
        self.stores["ingest"]["bad"] = {"job_id": "bad", "status": "completed", "owner": owner(self.dev)}
        self.stores["ingest"]["good"] = ingest_record("good", owner=owner(self.dev))
        self.assertEqual([j["job_id"] for j in self.get("/ingest/jobs", self.dev_headers)["jobs"]], ["good"])

    def test_lookups_by_id_answer_without_the_new_fields(self):
        self.stores["ingest"]["d2"] = ingest_record("d2", owner=owner(self.dev), collection="C", files=["a"])
        answer = self.get("/ingest/job/d2", self.dev_headers)
        for key in ("owner", "created_at", "finished_at", "collection", "files"):
            self.assertNotIn(key, answer)
        goldstandard.store_session(session("gs_00000013", owner=owner(self.dev), created_at=iso(5),
                                           finished_at=iso(1)))
        answer = self.get("/goldstandard/session/gs_00000013", self.dev_headers)
        for key in goldstandard.LOCAL_FIELDS:
            self.assertNotIn(key, answer)

    def test_lists_need_the_developer_role(self):
        user_headers = auth_testing.headers("end_user")
        for path in ("/ingest/jobs", "/export/jobs", "/import/jobs", "/goldstandard/sessions"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 401)
                self.assertEqual(self.client.get(path, headers=user_headers).status_code, 403)

    def test_starts_record_the_owner(self):
        with patch.object(wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(ingest_pipeline, "start_ingest_job", AsyncMock(return_value="j1")) as ingest, \
                patch.object(exporter, "start_export_job", AsyncMock(return_value="j2")) as export, \
                patch.object(importer, "start_import_job", AsyncMock(return_value="j3")) as imp, \
                patch.object(goldstandard, "start_generation", AsyncMock(return_value={
                    "session_id": "gs_00000014", "status": "generating", "pairs_total": 1,
                    "pairs_completed": 0})) as gen:
            response = self.client.post("/ingest/upload", headers=self.dev_headers,
                                        data={"collection": "OwnedLists"},
                                        files=[("files", ("a.txt", io.BytesIO(b"x"), "text/plain"))])
            self.assertEqual(response.status_code, 202, response.text)
            response = self.client.post("/export", headers=self.key_headers, json={"collection": "OwnedLists"})
            self.assertEqual(response.status_code, 202, response.text)
            response = self.client.post("/import", headers=self.dev_headers,
                                        json={"filename": "ragpkg-x.tar.gz", "on_conflict": "abort"})
            self.assertEqual(response.status_code, 202, response.text)
            response = self.client.post("/goldstandard/generate", headers=self.eng_headers,
                                        json={"collection": "OwnedLists", "sample_size": 1})
            self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(ingest.call_args.kwargs["owner"], owner(self.dev))
        self.assertEqual(export.call_args.kwargs["owner"], owner(self.key_name, "service"))
        self.assertEqual(imp.call_args.kwargs["owner"], owner(self.dev))
        self.assertEqual(gen.call_args.kwargs["owner"],
                         owner(auth_testing._accounts["engineer"].username))


class IngestRecordTests(TempUploadCase):
    def test_ingest_record_carries_collection_files_and_owner(self):
        store = self.bound("ingest")

        class Upload:
            def __init__(self, name, data):
                self.filename = name
                self.file = io.BytesIO(data)
                self.size = len(data)

        async def start():
            # The worker is replaced: only the record the admission stores is checked.
            with patch.object(ingest_pipeline, "_jobs", store), \
                    patch.object(ingest_pipeline, "_process_job_sync", lambda *args: None):
                job_id = await ingest_pipeline._admit_ingest_job(
                    "rec1", [Upload("notes.txt", b"hello"), Upload("skip.exe", b"x")], "OwnedLists",
                    "fixed", 300, 0, 0.85, 0, owner=owner())
                await asyncio.sleep(0.1)
                return job_id
        self.assertEqual(asyncio.run(start()), "rec1")
        record = store.get("rec1")
        self.assertIsNotNone(record)
        self.assertEqual(record["collection"], "OwnedLists")
        self.assertEqual(record["files"], ["notes.txt", "skip.exe"])
        self.assertEqual(record["owner"], owner())
        self.assertIsNotNone(record["created_at"])


if __name__ == "__main__":
    unittest.main()
