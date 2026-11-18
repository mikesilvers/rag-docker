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
from auth import access, passwords, store
from services import audit, exporter, goldstandard, importer, ingest_pipeline, rag_pipeline
from services import weaviate_client as wc

ORIGIN = "http://testserver"
SECRET, OPEN = "Secret", "Open1"
STAGING = SECRET + "__tuning_" + "0" * 32
NESTED = STAGING + "__tuning_" + "2" * 32         # a rebuild of the kept copy (#792)


def _no_weaviate():
    raise ConnectionError("test_collection_access.py runs without a network: there is no Weaviate")


class AccessTestCase(unittest.TestCase):
    """A fresh store, upload and audit folder; people of each role, a
    Developer service principal with a key; Weaviate holding OPEN, SECRET and
    a staging copy of SECRET; SECRET restricted to `dev` and the principal, and
    the copy given SECRET's access when it was made."""

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
        # The staging copy got SECRET's access when it was made (collection_recovery.begin).
        store.copy_collection_access(SECRET, STAGING)

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

    def test_no_store_means_nothing_to_copy_or_drop(self):
        empty = self.root / "empty-auth"
        with patch.object(settings, "auth_dir", str(empty)):
            self.assertEqual(store.copy_collection_access(SECRET, STAGING),
                             (store.CollectionAccess(False), store.CollectionAccess(False)))
            store.drop_collection_access(SECRET)
            self.assertFalse((empty / "auth.db").exists())

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
        # The copy keeps its own state, which still restricts it (CopyTests, #788).
        self.assertEqual(self.names("user"), [OPEN, SECRET])


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
        self.assertEqual(self.names("dev"), [OPEN])                        # the copy follows its target (#788)
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

        async def selected(body, usable=None):
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

    def run_import(self, name, on_conflict, owner, **patches):
        """importer._run on a package of collection `name`, with the package
        checks and the build stubbed out; returns the job record."""
        manifest = {"collection": {"name": name, "chunk_count": 1}}
        record = {"job_id": "imp2", "status": "queued", "filename": "ragpkg-x.tar.gz", "collection": None,
                  "owner": owner, "notes": [], "restored_sessions": []}
        stubs = {"_validate_package_sources": lambda *a: None, "_check_embedding": lambda *a: None,
                 "_read_goldstandard_sessions": lambda *a: [], "_read_retrieval_config": lambda *a: None,
                 "_read_ingest_config": lambda *a: None, "_ensure_models": lambda *a: [],
                 "_claim_target": lambda job_id, target, instead_of=None: target,
                 "_mark_started": lambda target, *a: target, "_mark_finished": lambda *a: None, **patches}
        targets = [patch.object(importer, "_jobs", {"imp2": record}),
                   patch.object(importer.packager, "open_package", lambda *a: (self.root, manifest)),
                   patch.object(importer.packager, "verify_digests", lambda *a: None),
                   patch.object(importer.packager, "sha256_file", lambda *a: "ab12cd34" * 8)]
        targets += [patch.object(importer, k, v) for k, v in stubs.items()]
        for target in targets:
            target.start()
        try:
            importer._run("imp2", "ragpkg-x.tar.gz", on_conflict)
        finally:
            for target in reversed(targets):
                target.stop()
        return record

    def test_a_failed_import_of_a_new_name_leaves_no_access_state(self):
        seen = []

        def build(target, *a):
            seen.append(store.collection_access(target))      # restricted before any data is written
            raise importer.PackageError("IMPORT_FAILED", "the build failed")
        record = self.run_import("Brandnew", "rename", {"name": "dev2", "kind": "user"}, _build=build)
        self.assertEqual(record["status"], "failed", record)
        self.assertEqual(seen, [store.CollectionAccess(True, ("dev2",), ())])
        self.assertFalse(store.collection_access("Brandnew").restricted)

    def test_a_failed_cleanup_keeps_the_new_collection_access(self):
        # The build's own delete of the partial collection failed (#788).
        def build(target, *a):
            raise importer.PackageError("IMPORT_FAILED", "the insert failed; partial target cleanup failed",
                                        {"collection": target, "cleanup_pending": True})
        record = self.run_import("Halfbuilt", "rename", {"name": "dev2", "kind": "user"}, _build=build)
        self.assertEqual(record["status"], "failed", record)
        self.assertEqual(store.collection_access("Halfbuilt"), store.CollectionAccess(True, ("dev2",), ()))

    def test_a_failed_build_that_left_the_collection_keeps_its_access(self):
        # The create was applied but answered with an error (#788).
        def build(target, *a):
            self.collections.add(target)
            raise RuntimeError("timed out")
        record = self.run_import("Applied", "rename", {"name": "dev2", "kind": "user"}, _build=build)
        self.assertEqual(record["status"], "failed", record)
        self.assertEqual(store.collection_access("Applied"), store.CollectionAccess(True, ("dev2",), ()))

    def test_a_failed_build_with_no_answer_on_existence_keeps_its_access(self):
        # Weaviate can't say whether the partial collection is gone: fail closed (#788).
        built = []

        def build(target, *a):
            built.append(target)
            raise RuntimeError("timed out")

        def unknown(name):
            if built:
                raise ConnectionError("no answer")
            return name[:1].upper() + name[1:] in self.collections
        with patch.object(wc, "_collection_exists_sync", unknown):
            record = self.run_import("Unanswered", "rename", {"name": "dev2", "kind": "user"}, _build=build)
        self.assertEqual((record["status"], built), ("failed", ["Unanswered"]), record)
        self.assertEqual(store.collection_access("Unanswered"), store.CollectionAccess(True, ("dev2",), ()))

    def test_a_replace_keeps_the_target_access(self):
        def begin(*a):
            raise importer.PackageError("IMPORT_FAILED", "stopped before the replace")
        before = store.collection_access(SECRET)
        with patch.object(importer.collection_recovery, "begin", begin), \
                patch.object(wc, "get_client", lambda: None):
            record = self.run_import(SECRET, "replace", {"name": "dev", "kind": "user"})
        self.assertEqual((record["status"], record["error"]), ("failed", "stopped before the replace"), record)
        self.assertEqual(store.collection_access(SECRET), before)

    def test_a_failure_after_the_build_keeps_the_new_collection_access(self):
        # The build finished, so the collection exists; a later failure must
        # not put back (open) the state the import gave it.
        def restore(*a):
            raise importer.PackageError("IMPORT_FAILED", "the sidecars failed")
        record = self.run_import("Builtnew", "rename", {"name": "dev2", "kind": "user"},
                                 _build=lambda *a: 1, _restore_sidecars=restore)
        self.assertEqual((record["status"], record["error"]), ("failed", "the sidecars failed"), record)
        self.assertEqual(store.collection_access("Builtnew"), store.CollectionAccess(True, ("dev2",), ()))


class CopyTests(AccessTestCase):
    """A staging or recovery copy gets its target's access when it is made
    (#606) and keeps it as its own state; while the target has a state, a
    caller other than an Engineer needs both (#788)."""

    def test_begin_snapshots_and_discard_drops(self):
        from services import collection_recovery

        class Client:
            class collections:
                @staticmethod
                def exists(name):
                    return False

                @staticmethod
                def delete(name):
                    pass
        record = collection_recovery.begin(SECRET, "import", Client)
        copy = record["staging"]
        self.assertEqual(store.collection_access(copy), store.collection_access(SECRET))
        record = collection_recovery.begin(OPEN, "tune", Client)
        self.assertFalse(store.collection_access(record["staging"]).restricted)
        collection_recovery.discard(collection_recovery.begin(SECRET, "tune", Client), Client)
        collection_recovery.discard({**collection_recovery.begin(SECRET, "import", Client)}, Client)
        last = collection_recovery.begin(SECRET, "import", Client)
        collection_recovery.discard(last, Client)
        self.assertFalse(store.collection_access(last["staging"]).restricted)
        self.assertTrue(store.collection_access(copy).restricted)          # a copy not discarded keeps it

    def test_a_copy_follows_a_deleted_and_re_created_target(self):
        async def delete(name):
            self.collections.discard(name)
            return 1

        async def create(**kwargs):
            self.collections.add(kwargs["name"])
        body = {"question": "What?", "collection": STAGING}
        with patch.object(wc, "delete_collection_when_idle", delete):
            self.assertEqual(self.client.delete(f"/collections/{SECRET}?confirm=true",
                                                headers=self.as_("eng")).status_code, 200)
        self.assertNotIn(STAGING, self.names("user"))                     # not opened by the delete
        self.assertIn(STAGING, self.names("dev"))                         # its own state alone now
        with patch.object(wc, "create_collection", create):
            self.assertEqual(self.client.post("/collections", json={"name": SECRET},
                                              headers=self.as_("dev2")).status_code, 201)
        # The new SECRET is dev2's; the copy is dev's: each lacks one side.
        self.assertEqual(self.names("dev2"), [OPEN, SECRET])
        self.assertEqual(self.names("dev"), [OPEN])
        for who in ("dev", "dev2"):
            self.refused(self.client.post("/query", json=body, headers=self.as_(who)))
        self.assertIn(STAGING, self.names("eng"))

    def test_a_copy_with_no_state_follows_its_target(self):
        # A copy kept from before #606 has no state of its own (#788).
        store.drop_collection_access(STAGING)
        for who, sees in (("dev", True), ("svc", True), ("eng", True), ("user", False), ("dev2", False)):
            with self.subTest(who=who):
                self.assertEqual(STAGING in self.names(who), sees)
        body = {"question": "What?", "collection": STAGING}
        self.refused(self.client.post("/query", json=body, headers=self.as_("user")))

    def test_narrowing_the_target_reaches_the_copy(self):
        self.assertEqual(self.put(SECRET, {"restricted": True, "users": ["dev"]}).status_code, 200)
        self.assertNotIn(STAGING, self.names("svc"))
        self.assertIn(STAGING, self.names("dev"))

    def test_opening_the_target_leaves_the_copy_its_own_state(self):
        self.assertEqual(self.put(SECRET, {"restricted": False}).status_code, 200)
        self.assertNotIn(STAGING, self.names("user"))
        self.assertIn(STAGING, self.names("dev"))


class NestedCopyTests(AccessTestCase):
    """A kept copy can be rebuilt, so a copy's target can be a copy too. The
    copy of the copy follows every collection in its chain (#792)."""

    def setUp(self):
        super().setUp()
        self.collections.add(NESTED)
        store.copy_collection_access(STAGING, NESTED)    # the copy's own state, as begin gave it before #792

    def open_copies(self):
        for name in (STAGING, NESTED):
            self.assertEqual(self.put(name, {"restricted": False}).status_code, 200)

    def test_the_chain_reaches_the_original(self):
        self.assertEqual(access.chain(NESTED), (NESTED, STAGING, SECRET))
        self.assertEqual(access.scope(NESTED), (NESTED, STAGING, SECRET))
        self.assertEqual(access.scope(STAGING), (STAGING, SECRET))
        self.assertEqual(access.scope(OPEN), (OPEN,))
        self.assertEqual(access.chain(NESTED.lower()), (NESTED, STAGING, SECRET))

    def test_narrowing_the_original_reaches_a_copy_of_a_copy(self):
        self.open_copies()
        body = {"question": "What?", "collection": NESTED}
        self.assertNotIn(NESTED, self.names("dev2"))
        self.refused(self.client.post("/query", json=body, headers=self.as_("dev2")))
        self.assertIn(NESTED, self.names("svc"))
        self.assertEqual(self.put(SECRET, {"restricted": True, "users": ["dev"]}).status_code, 200)
        self.assertNotIn(NESTED, self.names("svc"))
        self.refused(self.client.post("/query", json=body, headers=self.as_("svc")))
        self.assertIn(NESTED, self.names("dev"))
        self.assertIn(NESTED, self.names("eng"))

    def test_a_copy_of_a_copy_with_no_state_follows_the_original(self):
        # Copies kept from before #606 have no state of their own.
        store.drop_collection_access(STAGING)
        store.drop_collection_access(NESTED)
        for who, sees in (("dev", True), ("svc", True), ("eng", True), ("user", False), ("dev2", False)):
            with self.subTest(who=who):
                self.assertEqual(NESTED in self.names(who), sees)

    def test_package_checks_of_an_existing_copy_of_a_copy(self):
        self.open_copies()
        self.assertEqual(importer.package_source(NESTED), NESTED)
        self.assertTrue(importer._refused({"name": "dev2", "kind": "user"}, NESTED))
        self.assertFalse(importer._refused({"name": "dev", "kind": "user"}, NESTED))
        self.assertEqual(self.packages_seen(NESTED), {"dev2": 0, "dev": 1})

    def packages_seen(self, collection):
        from routers import transfer
        listed = [{"filename": "ragpkg-nested.tar.gz", "size_bytes": 1, "collection": collection, "chunk_count": 1,
                   "fidelity": "chunks-only", "created_at": None, "readable": True}]
        with patch.object(transfer, "_list_packages_sync", lambda: [dict(p) for p in listed]):
            return {who: len(self.client.get("/packages", headers=self.as_(who)).json()["packages"])
                    for who in ("dev2", "dev")}

    def test_a_package_of_a_discarded_copy_of_a_copy_follows_the_nearest_existing(self):
        gone_copy = STAGING + "__importing_" + "3" * 32          # STAGING is still here
        gone_both = SECRET + "__tuning_" + "4" * 32 + "__tuning_" + "5" * 32
        self.assertEqual(importer.package_source(gone_copy), STAGING)
        self.assertEqual(importer.package_source(gone_both), SECRET)
        self.assertTrue(importer._refused({"name": "dev2", "kind": "user"}, importer.package_source(gone_both)))
        self.assertFalse(importer._refused({"name": "dev", "kind": "user"}, importer.package_source(gone_both)))
        self.assertEqual(self.packages_seen(gone_both), {"dev2": 0, "dev": 1})
        _, written = importer._start_access({"name": "dev", "kind": "user"}, importer.package_source(gone_both),
                                            "Fromgone")
        self.assertEqual(written, store.collection_access(SECRET))

        def unanswered(name):
            if name == gone_copy:
                raise ConnectionError("no answer")
            return name[:1].upper() + name[1:] in self.collections
        with patch.object(wc, "_collection_exists_sync", unanswered):
            self.assertEqual(importer.package_source(gone_copy), gone_copy)      # no answer counts as existing

    def test_package_source_asks_in_chain_order_and_never_about_the_original(self):
        asked = []

        def none_exist(name):
            asked.append(name)
            return False
        self.assertEqual(access.package_source(OPEN, none_exist), OPEN)
        self.assertEqual(asked, [])
        self.assertEqual(access.package_source(NESTED, none_exist), SECRET)
        self.assertEqual(asked, [NESTED, STAGING])
        asked.clear()
        self.assertEqual(access.package_source(NESTED, lambda n: asked.append(n) or n == STAGING), STAGING)
        self.assertEqual(asked, [NESTED, STAGING])

    def test_begin_on_a_copy_takes_the_narrowest_state_of_its_chain(self):
        from services import collection_recovery

        class Client:
            class collections:
                @staticmethod
                def exists(name):
                    return False
        self.assertEqual(self.put(STAGING, {"restricted": False}).status_code, 200)
        record = collection_recovery.begin(STAGING, "tune", Client)
        self.assertEqual(store.collection_access(record["staging"]), store.CollectionAccess(True, ("dev",), ("svc",)))
        self.assertEqual(self.put(STAGING, {"restricted": True, "users": ["dev"]}).status_code, 200)
        record = collection_recovery.begin(STAGING, "tune", Client)
        self.assertEqual(store.collection_access(record["staging"]), store.CollectionAccess(True, ("dev",), ()))
        for name in (SECRET, STAGING):
            self.assertEqual(self.put(name, {"restricted": False}).status_code, 200)
        record = collection_recovery.begin(STAGING, "tune", Client)
        self.assertFalse(store.collection_access(record["staging"]).restricted)

    def test_an_import_from_a_copy_of_a_copy_takes_the_narrowest_state(self):
        self.open_copies()
        self.assertEqual(self.put(SECRET, {"restricted": True, "users": ["dev"]}).status_code, 200)
        _, written = importer._start_access({"name": "dev", "kind": "user"}, NESTED, "Fromnested")
        self.assertEqual(written, store.CollectionAccess(True, ("dev",), ()))
        self.assertEqual(self.put(SECRET, {"restricted": False}).status_code, 200)
        _, written = importer._start_access({"name": "dev", "kind": "user"}, NESTED, "Fromnested2")
        self.assertEqual(written, store.CollectionAccess(False))


class CreateFailureTests(AccessTestCase):
    def test_a_failed_restriction_creates_nothing(self):
        created = []

        async def create(**kwargs):
            created.append(kwargs["name"])

        def broken(*args):
            raise sqlite3.OperationalError("database is locked")
        with patch.object(wc, "create_collection", create), patch.object(store, "restrict_to", broken):
            answer = self.client.post("/collections", json={"name": "Fresh"}, headers=self.as_("dev2"))
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (500, "CREATE_FAILED"))
        self.assertEqual(created, [])

    def test_a_failed_create_puts_the_state_back(self):
        seen = []

        async def create(**kwargs):
            seen.append(store.collection_access(kwargs["name"]))          # restricted before it exists
            raise RuntimeError("weaviate refused")
        with patch.object(wc, "create_collection", create):
            answer = self.client.post("/collections", json={"name": "Fresh"}, headers=self.as_("dev2"))
        self.assertEqual(answer.status_code, 500)
        self.assertEqual(seen, [store.CollectionAccess(True, ("dev2",), ())])
        self.assertFalse(store.collection_access("Fresh").restricted)

    def test_a_failed_create_that_left_the_collection_keeps_the_restriction(self):
        # Weaviate applied the create but the client got an error (#788).
        async def create(**kwargs):
            self.collections.add(kwargs["name"])
            raise TimeoutError("read timed out")
        with patch.object(wc, "create_collection", create):
            answer = self.client.post("/collections", json={"name": "Fresh"}, headers=self.as_("dev2"))
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (500, "CREATE_FAILED"))
        self.assertEqual(store.collection_access("Fresh"), store.CollectionAccess(True, ("dev2",), ()))

    def test_a_failed_create_with_no_answer_on_existence_keeps_the_restriction(self):
        async def create(**kwargs):
            raise RuntimeError("weaviate refused")

        asked = []

        async def unknown(name):
            asked.append(name)
            if len(asked) > 1:                       # the check after the failed create
                raise ConnectionError("no answer")
            return False
        with patch.object(wc, "create_collection", create), patch.object(wc, "collection_exists", unknown):
            answer = self.client.post("/collections", json={"name": "Fresh"}, headers=self.as_("dev2"))
        self.assertEqual((answer.status_code, asked), (500, ["Fresh", "Fresh"]))
        self.assertEqual(store.collection_access("Fresh"), store.CollectionAccess(True, ("dev2",), ()))

    def test_put_back_only_restores_its_own_state(self):
        before, mine = store.restrict_to("Race", store.get_user("dev2").id)
        theirs = store.restrict_to("Race", store.get_user("dev").id)[1]     # another create wrote since
        self.assertFalse(store.put_back("Race", mine, before))
        self.assertEqual(store.collection_access("Race"), theirs)
        self.assertTrue(store.put_back("Race", theirs, mine))
        self.assertEqual(store.collection_access("Race"), mine)
        self.assertTrue(store.put_back("Race", mine, before))
        self.assertFalse(store.collection_access("Race").restricted)

    def test_a_failed_import_after_a_concurrent_create_keeps_the_creator(self):
        # The import wrote its state, then a create of the same name wrote its
        # own and made the collection; the import's build fails.
        before, mine = importer._start_access({"name": "dev2", "kind": "user"}, "Race", "Race")
        theirs = store.restrict_to("Race", store.get_user("dev").id)[1]
        self.collections.add("Race")
        store.put_back("Race", mine, before)
        self.assertEqual(store.collection_access("Race"), theirs)


class PackageSourceTests(AccessTestCase):
    def test_a_package_of_an_existing_copy_follows_the_copy_and_its_target(self):
        # The copy's own state is open; SECRET still restricts it (#788).
        self.assertEqual(self.put(STAGING, {"restricted": False}).status_code, 200)
        dev2 = {"name": "dev2", "kind": "user"}
        self.assertEqual(importer.package_source(STAGING), STAGING)
        self.assertTrue(importer._refused(dev2, STAGING))
        self.assertFalse(importer._refused({"name": "dev", "kind": "user"}, STAGING))
        from routers import transfer
        listed = [{"filename": "ragpkg-copy.tar.gz", "size_bytes": 1, "collection": STAGING, "chunk_count": 1,
                   "fidelity": "chunks-only", "created_at": None, "readable": True}]
        with patch.object(transfer, "_list_packages_sync", lambda: [dict(p) for p in listed]):
            self.assertEqual(self.client.get("/packages", headers=self.as_("dev2")).json()["packages"], [])
            self.assertEqual(len(self.client.get("/packages", headers=self.as_("dev")).json()["packages"]), 1)

    def test_an_import_from_a_copy_takes_the_narrower_state(self):
        self.assertEqual(self.put(STAGING, {"restricted": True, "users": ["dev", "dev2"]}).status_code, 200)
        self.assertEqual(self.put(SECRET, {"restricted": True, "users": ["dev"]}).status_code, 200)
        _, written = importer._start_access({"name": "dev", "kind": "user"}, STAGING, "Fromcopy")
        self.assertEqual(written, store.CollectionAccess(True, ("dev",), ()))
        self.assertEqual(self.put(STAGING, {"restricted": False}).status_code, 200)
        _, written = importer._start_access({"name": "dev", "kind": "user"}, STAGING, "Fromcopy2")
        self.assertEqual(written, store.CollectionAccess(True, ("dev",), ()))      # the target's alone
        self.assertEqual(self.put(SECRET, {"restricted": False}).status_code, 200)
        _, written = importer._start_access({"name": "dev", "kind": "user"}, STAGING, "Fromcopy3")
        self.assertEqual(written, store.CollectionAccess(False))

    def test_a_package_of_a_discarded_copy_follows_its_target(self):
        gone = SECRET + "__importing_" + "1" * 32
        self.assertEqual(importer.package_source(gone), SECRET)                   # the copy no longer exists
        self.assertEqual(importer.package_source(STAGING), STAGING)               # an existing copy is itself
        self.assertEqual(importer.package_source(OPEN), OPEN)
        self.assertTrue(importer._refused({"name": "dev2", "kind": "user"}, importer.package_source(gone)))
        before, after = importer._start_access({"name": "dev", "kind": "user"}, importer.package_source(gone),
                                               gone + "_ab12cd34")
        self.assertEqual(after, store.collection_access(SECRET))
        from routers import transfer
        listed = [{"filename": "ragpkg-gone-copy.tar.gz", "size_bytes": 1, "collection": gone, "chunk_count": 1,
                   "fidelity": "chunks-only", "created_at": None, "readable": True}]
        with patch.object(transfer, "_list_packages_sync", lambda: [dict(p) for p in listed]):
            self.assertEqual(self.client.get("/packages", headers=self.as_("dev2")).json()["packages"], [])
            self.assertEqual(len(self.client.get("/packages", headers=self.as_("dev")).json()["packages"]), 1)

    def test_an_encrypted_package_is_refused_once_decrypted(self):
        import io
        import tarfile
        from services import age_stream, packager
        manifest = json.dumps({"package_format": 1, "collection": {"name": SECRET, "chunk_count": 0}}).encode()
        raw = io.BytesIO()
        with tarfile.open(fileobj=raw, mode="w:gz") as tar:
            info = tarfile.TarInfo("package/manifest.json")
            info.size = len(manifest)
            tar.addfile(info, io.BytesIO(manifest))
        exports = Path(settings.exports_dir)
        exports.mkdir(parents=True, exist_ok=True)
        name = "ragpkg-secret.tar.gz.age"
        with (exports / name).open("wb") as out:
            enc = age_stream.Encryptor(out, "a test passphrase", 1)
            enc.write(raw.getvalue())
            enc.close()
        record = {"job_id": "enc1", "status": "queued", "filename": name, "collection": None,
                  "owner": {"name": "dev2", "kind": "user"}, "notes": [], "restored_sessions": []}
        reached = []
        with patch.object(importer, "_jobs", {"enc1": record}), \
                patch.object(packager, "verify_digests", lambda *a: reached.append("digests")), \
                patch.object(importer, "_validate_package_sources", lambda *a: None), \
                patch.object(importer, "_check_embedding", lambda *a: None):
            importer._run("enc1", name, "rename", "a test passphrase")
        self.assertEqual(reached, ["digests"])                              # decrypted and opened for real
        self.assertEqual((record["status"], record["error_code"]), ("failed", "PACKAGE_UNREADABLE"))
        self.assertEqual(record["error"], f"No package named '{name}' in the exports directory.")
        self.assertIsNone(record["collection"])
        self.assertTrue(record["access_refused"])


class GoldIndexOrderTests(AccessTestCase):
    def test_the_collection_is_recorded_before_the_file(self):
        order = []
        session = {"session_id": "gs_9", "collection": SECRET, "status": "completed", "pairs": [],
                   "pairs_total": 0, "pairs_completed": 0}
        with patch.dict(goldstandard._sessions, {"gs_9": session}), \
                patch.object(goldstandard, "_record_export_collection", lambda *a: order.append("index")), \
                patch.object(goldstandard, "_save_export_sync", lambda *a: order.append("file")):
            asyncio.run(goldstandard.save_session("gs_9", "gold.json", True))
        self.assertEqual(order, ["index", "file"])


class PrepareAreaTests(AccessTestCase):
    """Documents imported only into collections the caller can't use are hidden
    (#606, Q18, Q19); job records leave them out (Q20)."""

    def setUp(self):
        super().setUp()
        from services import preparation_store as prep
        target = patch.object(settings, "preparation_dir", str(self.root / "preparation"))
        target.start()
        self.addCleanup(target.stop)
        source = self.root / "note.txt"
        source.write_text("A note.\n")
        self.secret_doc = prep.create("secret-note.txt", source, None)["id"]
        self.open_doc = prep.create("open-note.txt", source, None)["id"]
        self.plain_doc = prep.create("plain-note.txt", source, None)["id"]
        self.mixed_doc = prep.create("mixed-note.txt", source, None)["id"]
        prep.add_import([self.secret_doc], SECRET, "job1")
        prep.add_import([self.open_doc], OPEN, "job2")
        prep.add_import([self.mixed_doc], SECRET, "job3")
        prep.add_import([self.mixed_doc], OPEN, "job4")

    def listed(self, who):
        answer = self.client.get("/prepare/documents", headers=self.as_(who))
        self.assertEqual(answer.status_code, 200, answer.text)
        return {d["id"] for d in answer.json()["documents"]}

    def test_the_list_hides_documents_of_restricted_collections(self):
        everything = {self.secret_doc, self.open_doc, self.plain_doc, self.mixed_doc}
        self.assertEqual(self.listed("dev2"), everything - {self.secret_doc})
        self.assertEqual(self.listed("dev"), everything)
        self.assertEqual(self.listed("svc2"), everything - {self.secret_doc})
        self.assertEqual(self.listed("eng"), everything)
        self.assertFalse(self.records("collection.access.refused"))

    def test_the_document_routes_refuse_a_hidden_document(self):
        doc = self.secret_doc
        for method, path in (("GET", f"/prepare/documents/{doc}"), ("GET", f"/prepare/documents/{doc}/raw"),
                             ("GET", f"/prepare/documents/{doc}/prepared"),
                             ("DELETE", f"/prepare/documents/{doc}/prepared"), ("DELETE", f"/prepare/documents/{doc}")):
            with self.subTest(method=method, path=path):
                answer = self.client.request(method, path, headers=self.as_("dev2"))
                self.assertEqual((answer.status_code, answer.json()["error"]["code"],
                                  answer.json()["error"]["message"]),
                                 (404, "DOCUMENT_NOT_FOUND", f"Document '{doc}' not found."))
        self.assertEqual(self.client.get(f"/prepare/documents/{doc}", headers=self.as_("dev")).status_code, 200)
        self.assertEqual(self.client.get(f"/prepare/documents/{doc}/raw", headers=self.as_("eng")).status_code, 200)
        item = self.records("collection.access.refused")[-1]
        self.assertEqual((item["actor"], item["collection"], item["details"]["code"]),
                         ("dev2", SECRET, "DOCUMENT_NOT_FOUND"))
        download = [r for r in self.records("prepare.download") if r["actor"] == "dev2"][0]
        self.assertEqual((download["result"], download["details"]["reason"]), ("refused", "collection_access"))

    def test_a_selection_treats_a_hidden_document_as_unknown(self):
        for path, body in (("/prepare/jobs", {"document_ids": [self.secret_doc]}),
                           ("/prepare/import", {"collection": OPEN, "document_ids": [self.secret_doc]})):
            with self.subTest(path=path):
                answer = self.client.post(path, json=body, headers=self.as_("dev2"))
                self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (404, "DOCUMENT_NOT_FOUND"))
                self.assertEqual(answer.json()["error"]["detail"], {"documents": [self.secret_doc]})
        from services import preparation_jobs
        from routers import prepare
        usable = lambda record: prepare._blocker(None) is None  # noqa: E731 - no request: everything
        self.assertEqual(len(preparation_jobs._resolve(None, {"raw"}, usable)), 4)
        dev2 = type("P", (), {"user_id": store.get_user("dev2").id, "role": "developer"})()
        visible = __import__("auth.access", fromlist=["x"]).visible_filter(dev2)
        blocked = prepare._blocker(type("R", (), {"state": type("S", (), {"auth": dev2})()})())
        shown = lambda record: blocked(record) is None  # noqa: E731
        self.assertEqual({r["id"] for r in preparation_jobs._resolve(None, {"raw"}, shown)},
                         {self.open_doc, self.plain_doc, self.mixed_doc})
        self.assertTrue(visible(OPEN))

    def test_a_document_with_one_usable_import_stays_visible_without_the_other_records(self):
        # Q19: mixed imports keep the document visible; the SECRET record is left out.
        doc = self.mixed_doc
        listed = {d["id"]: d for d in self.client.get("/prepare/documents", headers=self.as_("dev2")).json()["documents"]}
        self.assertEqual([i["collection"] for i in listed[doc]["imports"]], [OPEN])
        detail = self.client.get(f"/prepare/documents/{doc}", headers=self.as_("dev2"))
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual([i["collection"] for i in detail.json()["imports"]], [OPEN])
        self.assertEqual(self.client.get(f"/prepare/documents/{doc}/raw", headers=self.as_("dev2")).status_code, 200)
        for who in ("dev", "eng"):
            seen = self.client.get(f"/prepare/documents/{doc}", headers=self.as_(who)).json()
            self.assertEqual([i["collection"] for i in seen["imports"]], [SECRET, OPEN])
        self.assertFalse(self.records("collection.access.refused"))

    def test_job_records_leave_hidden_documents_out(self):
        # Q20: a job naming the hidden document and two visible ones.
        from services import preparation_jobs
        job = {"job_id": "pj1", "status": "running", "documents_total": 3, "documents_completed": 1,
               "documents_failed": 1, "document_ids": [self.secret_doc, self.mixed_doc, self.plain_doc],
               "names": ["secret-note.txt", "mixed-note.txt", "plain-note.txt"],
               "current": {"document_id": self.secret_doc, "name": "secret-note.txt"},
               "errors": ["secret-note.txt: the model failed", f"{self.secret_doc}: unreadable",
                          "plain-note.txt: the model failed"],
               "owner": {"name": "dev2", "kind": "user"}, "created_at": "2026-11-17T10:00:00+00:00"}
        with patch.object(preparation_jobs, "get_job", lambda job_id: dict(job)), \
                patch.object(preparation_jobs, "job_records", lambda: [("pj1", dict(job), None)]):
            seen = self.client.get("/prepare/job/pj1", headers=self.as_("dev2")).json()
            self.assertEqual(seen["document_ids"], [self.mixed_doc, self.plain_doc])
            self.assertEqual(seen["names"], ["mixed-note.txt", "plain-note.txt"])
            self.assertIsNone(seen["current"])
            self.assertEqual(seen["errors"], ["plain-note.txt: the model failed"])
            self.assertEqual(seen["documents_total"], 3)
            listed = self.client.get("/prepare/jobs", headers=self.as_("dev2")).json()["jobs"]
            self.assertEqual([(j["document_ids"], j["names"]) for j in listed],
                             [([self.mixed_doc, self.plain_doc], ["mixed-note.txt", "plain-note.txt"])])
            full = self.client.get("/prepare/job/pj1", headers=self.as_("eng")).json()
            self.assertEqual((full["document_ids"], full["current"], full["errors"]),
                             (job["document_ids"], job["current"], job["errors"]))
            mine = self.client.get("/prepare/job/pj1", headers=self.as_("dev")).json()
            self.assertEqual(mine["document_ids"], job["document_ids"])
        self.assertFalse(self.records("collection.access.refused"))

    def test_a_refused_selection_is_audited_as_an_access_refusal(self):
        for path, body, action in (
                ("/prepare/jobs", {"document_ids": [self.secret_doc, self.open_doc]}, "prepare.start"),
                ("/prepare/import", {"collection": OPEN, "document_ids": [self.secret_doc]}, "prepare.import")):
            with self.subTest(path=path):
                answer = self.client.post(path, json=body, headers=self.as_("dev2"))
                self.assertEqual((answer.status_code, answer.json()["error"]["detail"]),
                                 (404, {"documents": [self.secret_doc]}))
                item = [r for r in self.records(action) if r["actor"] == "dev2"][-1]
                self.assertEqual((item["result"], item["collection"], item["details"].get("reason")),
                                 ("refused", SECRET, "collection_access"), item)


if __name__ == "__main__":
    unittest.main()
