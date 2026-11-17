"""Per-collection access (#606): who may use a restricted collection.

A collection is open, or restricted to the people and service principals
granted it; Engineers reach every collection. A caller without access gets the
answer the route gives something that doesn't exist, and the refusal is
audited. SPECIFICATIONS.md §3.1.8 "Per-collection access".

Runs with the API's dependencies, in the built API image with no network
(`17_auth.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_collection_access.py

or on the host with them installed. Every test has its own account store,
upload folder and audit folder. Requests go through `main.app` with FastAPI's
TestClient, without the lifespan, with real sessions and keys. Weaviate is a
fake set of collection names; nothing reaches a backend. On the code before
#606 the listing, query and creation cases fail.
"""
import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

API_DIR = os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api")
sys.path.insert(0, API_DIR)

from fastapi.testclient import TestClient

from config import settings
from main import app
from auth import passwords, store
from services import audit, exporter, goldstandard, importer, ingest_pipeline, rag_pipeline
from services import weaviate_client as wc

ORIGIN = "http://testserver"
SECRET, OPEN = "Secret", "Open1"
STAGING = SECRET + "__tuning_" + "0" * 32


def _no_weaviate():
    raise ConnectionError("test_collection_access.py runs without a network: there is no Weaviate")


class AccessTestCase(unittest.TestCase):
    """A fresh store, upload and audit folder; people of each role, a
    Developer service principal with a key; Weaviate holding OPEN, SECRET and
    a staging copy of SECRET; SECRET restricted to `dev` and the principal."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in (("audit_dir", str(self.root / "audit")), ("auth_dir", str(self.root / "auth")),
                            ("upload_dir", str(self.root / "uploads")), ("exports_dir", str(self.root / "exports"))):
            target = patch.object(settings, name, value)
            target.start()
            self.addCleanup(target.stop)
        (self.root / "uploads").mkdir()
        self.collections = {OPEN, SECRET, STAGING}

        async def exists(name):
            return name[:1].upper() + name[1:] in self.collections

        async def listing():
            return [{"name": n, "object_count": 1, "index_type": "hnsw", "distance_metric": "cosine"}
                    for n in sorted(self.collections)]

        for target in (patch.object(wc, "get_client", _no_weaviate), patch.object(wc, "collection_exists", exists),
                       patch.object(wc, "_collection_exists_sync", lambda n: n[:1].upper() + n[1:] in self.collections),
                       patch.object(wc, "get_collections", listing)):
            target.start()
            self.addCleanup(target.stop)
        for username, role in (("eng", "engineer"), ("dev", "developer"), ("dev2", "developer"),
                               ("user", "end_user")):
            store.create_user(username, role, passwords.generate_password(), bootstrap=not store.is_set_up(),
                              must_change_password=False)
        store.create_principal("svc", "developer")
        store.create_principal("svc2", "developer")
        self.key = store.issue_key("svc")[1]
        self.key2 = store.issue_key("svc2")[1]
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        status = self.put(SECRET, {"restricted": True, "users": ["dev"], "service_principals": ["svc"]}).status_code
        self.assertEqual(status, 200)

    def as_(self, who):
        if who in ("svc", "svc2"):
            return {"Authorization": "Bearer " + (self.key if who == "svc" else self.key2)}
        token = store.create_session(store.get_user(who).id)
        return {"Cookie": f"{store.COOKIE}={token}", "Origin": ORIGIN}

    def put(self, name, body, who="eng"):
        return self.client.put(f"/collections/{name}/access", json=body, headers=self.as_(who))

    def names(self, who):
        answer = self.client.get("/collections", headers=self.as_(who))
        self.assertEqual(answer.status_code, 200, answer.text)
        return [c["name"] for c in answer.json()["collections"]]

    def records(self, action=None):
        return [r for r in reversed(list(audit.newest_first())) if action is None or r["action"] == action]

    def refused(self, answer, code="COLLECTION_NOT_FOUND"):
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (404, code), answer.text)


class StoreTests(AccessTestCase):
    def test_a_version_3_store_gains_the_tables_and_every_collection_stays_open(self):
        path = store.db_path()
        conn = sqlite3.connect(path)
        conn.executescript("DROP TABLE collection_grants; DROP TABLE restricted_collections; PRAGMA user_version = 3;")
        conn.close()
        store._ready.clear()
        self.assertTrue(store.has_access(store.get_user("user").id, "end_user", SECRET))
        version = sqlite3.connect(path).execute("PRAGMA user_version").fetchone()[0]
        self.assertEqual(version, 4)
        self.assertEqual(store.get_user("dev").role, "developer")       # rows kept

    def test_the_rule(self):
        dev, user = store.get_user("dev"), store.get_user("user")
        self.assertTrue(store.has_access(dev.id, "developer", SECRET))
        self.assertTrue(store.has_access(dev.id, "developer", "secret"))          # canonical name
        self.assertFalse(store.has_access(user.id, "end_user", SECRET))
        self.assertTrue(store.has_access(user.id, "engineer", SECRET))            # Engineers reach everything
        self.assertTrue(store.has_access(user.id, "end_user", OPEN))

    def test_grants_survive_disable_and_a_demoted_engineer_is_bound(self):
        store.set_enabled("dev", False)
        store.set_enabled("dev", True)
        self.assertEqual(store.collection_access(SECRET).users, ("dev",))
        eng = store.get_user("eng")
        self.assertTrue(store.has_access(eng.id, "engineer", SECRET))
        self.assertFalse(store.has_access(eng.id, "developer", SECRET))           # once demoted

    def test_copy_and_drop(self):
        store.copy_collection_access(SECRET, "Copy")
        self.assertEqual(store.collection_access("Copy"), store.collection_access(SECRET))
        store.copy_collection_access(OPEN, "Copy")
        self.assertFalse(store.collection_access("Copy").restricted)
        store.drop_collection_access(SECRET)
        self.assertFalse(store.collection_access(SECRET).restricted)


class ListingTests(AccessTestCase):
    def test_the_list_holds_only_usable_collections(self):
        self.assertEqual(self.names("user"), [OPEN])
        self.assertEqual(self.names("dev2"), [OPEN])
        self.assertEqual(self.names("dev"), [OPEN, SECRET, STAGING])              # a staging copy has its target's access
        self.assertEqual(self.names("svc"), [OPEN, SECRET, STAGING])
        self.assertEqual(self.names("svc2"), [OPEN])
        self.assertEqual(self.names("eng"), [OPEN, SECRET, STAGING])
        self.assertFalse(self.records("collection.access.refused"))               # a filtered list is no refusal

    def test_a_change_applies_to_the_next_request(self):
        self.assertEqual(self.put(SECRET, {"restricted": False}).status_code, 200)
        self.assertEqual(self.names("user"), [OPEN, SECRET, STAGING])


class CreateDeleteTests(AccessTestCase):
    def test_a_new_collection_is_restricted_to_its_creator(self):
        async def create(**kwargs):
            self.collections.add(kwargs["name"])
        with patch.object(wc, "create_collection", create):
            answer = self.client.post("/collections", json={"name": "fresh"}, headers=self.as_("dev2"))
        self.assertEqual(answer.status_code, 201, answer.text)
        self.assertEqual(self.names("dev2"), ["Fresh", OPEN])
        self.assertNotIn("Fresh", self.names("user"))
        self.assertNotIn("Fresh", self.names("dev"))
        self.assertEqual(store.collection_access("Fresh"), store.CollectionAccess(True, ("dev2",), ()))

    def test_a_key_creates_for_its_principal(self):
        async def create(**kwargs):
            self.collections.add(kwargs["name"])
        with patch.object(wc, "create_collection", create):
            answer = self.client.post("/collections", json={"name": "Keyed"}, headers=self.as_("svc2"))
        self.assertEqual(answer.status_code, 201, answer.text)
        self.assertEqual(store.collection_access("Keyed").service_principals, ("svc2",))

    def test_a_delete_drops_the_restriction(self):
        async def delete(name):
            self.collections.discard(name)
            return 1
        with patch.object(wc, "delete_collection_when_idle", delete):
            answer = self.client.delete(f"/collections/{SECRET}?confirm=true", headers=self.as_("eng"))
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertFalse(store.collection_access(SECRET).restricted)


class AccessRouteTests(AccessTestCase):
    def test_engineers_in_a_session_only(self):
        self.assertEqual(self.client.get(f"/collections/{SECRET}/access", headers=self.as_("dev")).status_code, 403)
        store.create_principal("boss", "engineer")
        key = store.issue_key("boss")[1]
        answer = self.client.get(f"/collections/{SECRET}/access", headers={"Authorization": "Bearer " + key})
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (403, "KEY_NOT_ALLOWED"))

    def test_read_and_set(self):
        answer = self.client.get(f"/collections/{SECRET.lower()}/access", headers=self.as_("eng"))
        self.assertEqual(answer.json(), {"restricted": True, "users": ["dev"], "service_principals": ["svc"]})
        self.assertEqual(self.client.get(f"/collections/{OPEN}/access", headers=self.as_("eng")).json(),
                         {"restricted": False, "users": [], "service_principals": []})
        self.refused(self.client.get("/collections/Nowhere/access", headers=self.as_("eng")))
        answer = self.put(SECRET, {"restricted": True, "users": ["DEV2", "user"], "service_principals": []})
        self.assertEqual(answer.json(), {"restricted": True, "users": ["dev2", "user"], "service_principals": []})
        item = self.records("collection.access")[-1]
        self.assertEqual((item["actor"], item["collection"], item["result"]), ("eng", SECRET, "success"))
        self.assertEqual(item["details"]["added"], {"users": ["dev2", "user"], "service_principals": []})
        self.assertEqual(item["details"]["removed"], {"users": ["dev"], "service_principals": ["svc"]})
        self.assertEqual((item["details"]["restricted_before"], item["details"]["restricted_after"]), (True, True))

    def test_refused_changes_change_nothing(self):
        for body in ({"restricted": False, "users": ["dev"]}, {"restricted": True, "users": ["nobody"]},
                     {"restricted": True, "users": ["svc"]}, {"restricted": True, "service_principals": ["dev"]},
                     {"restricted": True, "extra": 1}, {}):
            with self.subTest(body=body):
                answer = self.put(SECRET, body)
                self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (422, "INVALID_PARAMETER"))
        self.assertEqual(store.collection_access(SECRET), store.CollectionAccess(True, ("dev",), ("svc",)))
        self.refused(self.put("Nowhere", {"restricted": False}))

    def test_an_empty_grant_set_leaves_engineers_only(self):
        self.put(SECRET, {"restricted": True})
        self.assertEqual(self.names("dev"), [OPEN])
        self.assertEqual(self.names("eng"), [OPEN, SECRET, STAGING])


class RefusalTests(AccessTestCase):
    """Every route that names a collection refuses one the caller may not use
    as it answers one that doesn't exist, and the refusal is audited."""

    def unavailable(self, *args, **kwargs):
        raise rag_pipeline.ServiceUnavailable("Ollama")

    def test_query(self):
        body = {"question": "What?", "collection": SECRET}
        with patch.object(rag_pipeline, "run_query", self.unavailable):
            self.refused(self.client.post("/query", json=body, headers=self.as_("user")))
            self.assertEqual(self.client.post("/query", json=body, headers=self.as_("dev")).status_code, 503)
            self.assertEqual(self.client.post("/query", json=body, headers=self.as_("svc")).status_code, 503)
            self.refused(self.client.post("/query", json=body, headers=self.as_("svc2")))
            self.refused(self.client.post("/query", json={**body, "collection": STAGING}, headers=self.as_("user")))
        item = [r for r in self.records("query") if r["actor"] == "user"][0]
        self.assertEqual((item["result"], item["status_code"], item["collection"]), ("refused", 404, SECRET))
        self.assertEqual(item["details"]["reason"], "collection_access")
        self.assertEqual(item["details"]["code"], "COLLECTION_NOT_FOUND")

    def test_settings_routes(self):
        for path in (f"/retrieval/config/{SECRET}", f"/ingest/config/{SECRET}"):
            with self.subTest(path=path):
                who = "user" if path.startswith("/retrieval") else "dev2"
                self.refused(self.client.get(path, headers=self.as_(who)))
                self.assertEqual(self.client.get(path, headers=self.as_("dev")).status_code, 200)
                item = self.records("collection.access.refused")[-1]
                self.assertEqual((item["actor"], item["collection"], item["status_code"], item["result"]),
                                 (who, SECRET, 404, "refused"))
                self.assertEqual(item["details"]["route"], "GET " + path.replace(SECRET, "{collection}"))
        saves = (("/retrieval/config", {"collection": SECRET, "top_k": 4}),
                 ("/ingest/config", {"collection": SECRET, "chunking_strategy": "fixed", "chunk_size": 300,
                                     "chunk_overlap": 0, "min_chunk_size": 50}))
        for path, body in saves:
            with self.subTest(path=path):
                self.refused(self.client.post(path, json=body, headers=self.as_("dev2")))
                self.assertEqual(self.client.post(path, json=body, headers=self.as_("dev")).status_code, 201)

    def test_settings_routes_answer_404_for_a_missing_collection(self):
        self.refused(self.client.get("/retrieval/config/Nowhere", headers=self.as_("eng")))
        self.refused(self.client.get("/ingest/config/Nowhere", headers=self.as_("eng")))
        self.refused(self.client.post("/retrieval/config", json={"collection": "Nowhere", "top_k": 4},
                                      headers=self.as_("eng")))
        self.refused(self.client.post("/ingest/config", json={"collection": "Nowhere", "chunking_strategy": "fixed",
                                                              "chunk_size": 300, "chunk_overlap": 0,
                                                              "min_chunk_size": 50}, headers=self.as_("eng")))
        self.assertFalse((self.root / "uploads" / "retrieval_configs").exists()
                         and any((self.root / "uploads" / "retrieval_configs").iterdir()))

    def test_developer_routes(self):
        dev2 = self.as_("dev2")
        self.refused(self.client.post("/export", json={"collection": SECRET}, headers=dev2))
        self.refused(self.client.post("/goldstandard/generate", json={"collection": SECRET}, headers=dev2))
        self.refused(self.client.get(f"/tune/{SECRET}", headers=dev2))
        files = {"files": ("a.txt", b"text", "text/plain")}
        self.refused(self.client.post("/ingest/upload", data={"collection": SECRET}, files=files, headers=dev2))
        from services import preparation_jobs

        async def selected(body):
            return {"records": [], "names": ["a.md"]}
        with patch.object(preparation_jobs, "import_documents", selected):
            self.refused(self.client.post("/prepare/import", json={"collection": SECRET, "all": True}, headers=dev2))

    def test_job_status_by_id(self):
        common = {"job_id": "j1", "status": "running", "collection": SECRET}
        jobs = {
            "/ingest/job/j1": (ingest_pipeline, {**common, "files_total": 1, "files_completed": 0, "files_failed": 0,
                                                 "chunks_stored": 0, "errors": []}),
            "/export/job/j1": (exporter, {**common, "chunks_written": 0, "filename": None, "size_bytes": None,
                                          "source_document_count": None, "fidelity": None, "models_bundled": None,
                                          "retrieve_script": None, "warnings": [], "error": None}),
            "/import/job/j1": (importer, {**common, "filename": "p.tar.gz", "on_conflict": "rename",
                                          "original_collection": None, "chunks_written": 0, "fidelity": None,
                                          "renamed": False, "notes": [], "error": None, "error_code": None,
                                          "error_detail": None}),
        }
        for path, (module, job) in jobs.items():
            with self.subTest(path=path), patch.object(module, "get_job", lambda job_id, job=job: dict(job)):
                self.refused(self.client.get(path, headers=self.as_("dev2")), "JOB_NOT_FOUND")
                self.assertEqual(self.client.get(path, headers=self.as_("dev")).status_code, 200)
        # An import whose package isn't read yet names no collection.
        unread = {**jobs["/import/job/j1"][1], "collection": None}
        with patch.object(importer, "get_job", lambda job_id: dict(unread)):
            self.assertEqual(self.client.get("/import/job/j1", headers=self.as_("dev2")).status_code, 200)

    def test_gold_standard_sessions(self):
        session = {"session_id": "gs_1", "collection": SECRET, "status": "completed", "pairs": [],
                   "pairs_total": 0, "pairs_completed": 0}
        with patch.object(goldstandard, "get_session", lambda session_id: dict(session)):
            for method, path, body in (("GET", "/goldstandard/session/gs_1", None),
                                       ("PATCH", "/goldstandard/session/gs_1/pair/p1", {"status": "approved"}),
                                       ("POST", "/goldstandard/regenerate", {"session_id": "gs_1", "pair_id": "p1"}),
                                       ("POST", "/goldstandard/save", {"session_id": "gs_1"})):
                with self.subTest(path=path):
                    self.refused(self.client.request(method, path, json=body, headers=self.as_("dev2")),
                                 "SESSION_NOT_FOUND")
            self.assertEqual(self.client.get("/goldstandard/session/gs_1", headers=self.as_("dev")).status_code, 200)

    def test_gold_standard_download(self):
        folder = self.root / "uploads" / goldstandard.EXPORT_DIRNAME
        folder.mkdir()
        for name in ("known.json", "unknown.json"):
            (folder / name).write_text("[]")
        goldstandard._record_export_collection("known.json", SECRET)
        self.refused(self.client.get("/goldstandard/download/known.json", headers=self.as_("dev2")), "FILE_NOT_FOUND")
        self.assertEqual(self.client.get("/goldstandard/download/known.json", headers=self.as_("dev")).status_code, 200)
        self.refused(self.client.get("/goldstandard/download/unknown.json", headers=self.as_("dev")), "FILE_NOT_FOUND")
        self.assertEqual(self.client.get("/goldstandard/download/unknown.json", headers=self.as_("eng")).status_code, 200)
        self.assertEqual(goldstandard.export_collection("known.json"), SECRET)


class PackageTests(AccessTestCase):
    PACKAGES = [
        {"filename": "ragpkg-secret.tar.gz", "size_bytes": 1, "collection": SECRET, "chunk_count": 1,
         "fidelity": "chunks-only", "created_at": None, "readable": True},
        {"filename": "ragpkg-open.tar.gz", "size_bytes": 1, "collection": OPEN, "chunk_count": 1,
         "fidelity": "chunks-only", "created_at": None, "readable": True},
        {"filename": "ragpkg-gone.tar.gz", "size_bytes": 1, "collection": "Gone", "chunk_count": 1,
         "fidelity": "chunks-only", "created_at": None, "readable": True},
        {"filename": "ragpkg-x.tar.gz.age", "size_bytes": 1, "collection": None, "chunk_count": None,
         "fidelity": None, "created_at": None, "readable": True, "encrypted": True},
    ]

    def listed(self, who):
        from routers import transfer
        with patch.object(transfer, "_list_packages_sync", lambda: [dict(p) for p in self.PACKAGES]):
            return [p["filename"] for p in self.client.get("/packages", headers=self.as_(who)).json()["packages"]]

    def test_the_listing_hides_restricted_packages(self):
        store.restrict_to("Gone", None)       # restricted, but no such collection here: listed
        everything = [p["filename"] for p in self.PACKAGES]
        self.assertEqual(self.listed("dev"), everything)
        self.assertEqual(self.listed("eng"), everything)
        self.assertEqual(self.listed("dev2"), [n for n in everything if n != "ragpkg-secret.tar.gz"])

    def test_the_import_job_refuses_and_names_no_collection(self):
        self.assertTrue(importer._refused({"name": "dev2", "kind": "user"}, SECRET))
        self.assertFalse(importer._refused({"name": "dev", "kind": "user"}, SECRET))
        self.assertFalse(importer._refused({"name": "svc", "kind": "service"}, SECRET))
        self.assertTrue(importer._refused({"name": "svc", "kind": "user"}, SECRET))    # kind must match
        self.assertFalse(importer._refused({"name": "dev2", "kind": "user"}, OPEN))
        self.assertFalse(importer._refused(None, SECRET))
        store.restrict_to("Gone", None)
        self.assertFalse(importer._refused({"name": "dev2", "kind": "user"}, "Gone"))   # not here

        manifest = {"collection": {"name": SECRET}}
        job_id = "imp1"
        record = {"job_id": job_id, "status": "queued", "filename": "ragpkg-secret.tar.gz", "collection": None,
                  "owner": {"name": "dev2", "kind": "user"}, "notes": [], "restored_sessions": []}
        with patch.object(importer, "_jobs", {job_id: record}), \
                patch.object(importer.packager, "open_package", lambda *a: (self.root, manifest)), \
                patch.object(importer.packager, "verify_digests", lambda *a: None), \
                patch.object(importer, "_validate_package_sources", lambda *a: None), \
                patch.object(importer, "_check_embedding", lambda *a: None):
            importer._run(job_id, "ragpkg-secret.tar.gz", "rename")
        self.assertEqual((record["status"], record["error_code"]), ("failed", "PACKAGE_UNREADABLE"))
        self.assertEqual(record["error"], "No package named 'ragpkg-secret.tar.gz' in the exports directory.")
        self.assertIsNone(record["collection"])
        self.assertTrue(record["access_refused"])

    def test_an_import_job_refusal_is_audited_as_refused(self):
        audit.job_ended({"job_id": "imp1", "status": "failed", "access_refused": True,
                         "_audit": {"action": "import", "actor": "dev2", "actor_kind": "user", "role": "developer",
                                    "details": {}}}, False)
        item = self.records("import")[-1]
        self.assertEqual((item["result"], item["details"]["reason"]), ("refused", "collection_access"))

    def test_a_new_collection_from_an_import(self):
        importer._start_access({"name": "dev2", "kind": "user"}, "Brandnew", "Brandnew")
        self.assertEqual(store.collection_access("Brandnew"), store.CollectionAccess(True, ("dev2",), ()))
        importer._start_access({"name": "dev", "kind": "user"}, SECRET, "Secret_ab12cd34")
        self.assertEqual(store.collection_access("Secret_ab12cd34"), store.collection_access(SECRET))
        importer._start_access({"name": "dev", "kind": "user"}, OPEN, "Open1_ab12cd34")
        self.assertFalse(store.collection_access("Open1_ab12cd34").restricted)
        importer._start_access(None, "Untracked", "Untracked")
        self.assertFalse(store.collection_access("Untracked").restricted)


if __name__ == "__main__":
    unittest.main()
