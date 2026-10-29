"""The audit log (#29): records, rotation, concurrent writers, the settings,
write failures, jobs, the request middleware, the API and CSV, and the operator
command.

Runs with the API's dependencies, in the built API image with no network
(`19_audit.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_audit.py

Every test uses its own temporary audit folder, account store and upload
folder; requests go through `main.app` with FastAPI's TestClient, without the
lifespan, and nothing reaches a backend.
"""
import csv
import io
import json
import logging
import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

API_DIR = os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api")
sys.path.insert(0, API_DIR)

from fastapi.testclient import TestClient
from pydantic import ValidationError

import config
from config import settings
from main import app
from auth import passwords, policy, store
from services import audit, jobstore

ORIGIN = "http://testserver"
ID = re.compile(r"\d{19}-[0-9a-f]{6}\Z")
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z\Z")


class AuditTestCase(unittest.TestCase):
    """A fresh audit folder, account store and upload folder per test."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.audit_dir = self.root / "audit"
        for name, value in (("audit_dir", str(self.audit_dir)), ("auth_dir", str(self.root / "auth")),
                            ("upload_dir", str(self.root / "uploads"))):
            target = patch.object(settings, name, value)
            target.start()
            self.addCleanup(target.stop)
        (self.root / "auth").mkdir()
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def records(self):
        """Every record, oldest first."""
        return list(reversed(list(audit.newest_first())))

    def raw(self):
        return "".join(p.read_text() for p in sorted(self.audit_dir.glob("audit.jsonl*")))

    def make(self, username, role):
        password = passwords.generate_password()
        store.create_user(username, role, password, bootstrap=not store.is_set_up(), must_change_password=False)
        return password

    def setup_roles(self):
        self.passwords = {role: self.make(name, role) for role, name in
                          (("engineer", "eng"), ("developer", "dev"), ("end_user", "user"))}

    def session(self, username):
        token = store.create_session(store.get_user(username).id)
        return {"Cookie": f"{store.COOKIE}={token}", "Origin": ORIGIN}

    def last(self, action):
        found = [r for r in self.records() if r["action"] == action]
        self.assertTrue(found, f"no {action} record in {[r['action'] for r in self.records()]}")
        return found[-1]


class RecordTests(AuditTestCase):
    def test_shape(self):
        self.assertTrue(audit.record("query", result="success", actor="eng", actor_kind="user", role="engineer",
                                     client_ip="10.0.0.1", collection="Docs", status_code=200,
                                     details={"top_k": 5}))
        (item,) = self.records()
        self.assertEqual(list(item), list(audit.KEYS))
        self.assertRegex(item["id"], ID)
        self.assertRegex(item["time"], TIME)
        self.assertEqual((item["actor"], item["role"], item["collection"], item["details"], item["key_id"]),
                         ("eng", "engineer", "Docs", {"top_k": 5}, None))
        self.assertEqual(stat.S_IMODE(self.audit_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.audit_dir / "audit.jsonl").stat().st_mode), 0o600)

    def test_ids_follow_line_order(self):
        for n in range(50):
            audit.record("query", result="success", details={"n": n})
        ids = [r["id"] for r in self.records()]
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(len(set(ids)), 50)
        self.assertEqual([r["details"]["n"] for r in self.records()], list(range(50)))

    def test_rotation_and_keep(self):
        with patch.object(settings, "audit_log_max_bytes", 4096), patch.object(settings, "audit_log_keep", 3):
            for n in range(400):
                audit.record("query", result="success", details={"n": n, "pad": "x" * 100})
            names = sorted(p.name for p in self.audit_dir.glob("audit.jsonl*"))
            self.assertEqual(names, ["audit.jsonl", "audit.jsonl.1", "audit.jsonl.2", "audit.jsonl.3"])
            for path in self.audit_dir.glob("audit.jsonl.*"):
                self.assertGreaterEqual(path.stat().st_size, 4096)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            kept = [r["details"]["n"] for r in self.records()]
            # The newest records, contiguous, ending with the last one written.
            self.assertEqual(kept, list(range(kept[0], 400)))
            self.assertGreater(kept[0], 0)

    def test_torn_line_is_skipped(self):
        audit.record("query", result="success")
        with open(self.audit_dir / "audit.jsonl", "a") as handle:
            handle.write('{"id": "torn')
        audit.record("query", result="refused")
        self.assertEqual([r["result"] for r in self.records()], ["success", "refused"])

    def test_concurrent_threads_and_a_second_process(self):
        script = ("import sys; sys.path.insert(0, sys.argv[1]);"
                  "from services import audit\n"
                  "for n in range(200): audit.record('operator.enable', result='success', details={'n': n})")
        env = {**os.environ, "AUDIT_DIR": str(self.audit_dir), "AUTH_DIR": str(self.root / "auth")}
        child = subprocess.Popen([sys.executable, "-c", script, API_DIR], env=env, cwd=API_DIR)

        def write():
            for n in range(100):
                audit.record("query", result="success", details={"n": n, "pad": "y" * 300})

        threads = [threading.Thread(target=write) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(child.wait(timeout=120), 0)
        lines = (self.audit_dir / "audit.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 800)
        items = [json.loads(line) for line in lines]          # every line whole
        ids = [i["id"] for i in items]
        self.assertEqual(len(set(ids)), 800)
        self.assertEqual(ids, sorted(ids))
        self.assertEqual(sum(1 for i in items if i["action"] == "operator.enable"), 200)

    def test_write_failure_is_counted_and_logged_never_raised(self):
        blocker = self.root / "file"
        blocker.write_text("not a folder")
        before = audit.failed_writes()
        with patch.object(settings, "audit_dir", str(blocker / "audit")), \
                self.assertLogs("audit", level="ERROR") as logs:
            self.assertFalse(audit.record("query", result="success"))
        self.assertEqual(audit.failed_writes(), before + 1)
        self.assertIn("query", logs.output[0])

    def test_write_failure_leaves_the_answer_alone(self):
        self.setup_roles()
        blocker = self.root / "file"
        blocker.write_text("not a folder")
        with patch.object(settings, "audit_dir", str(blocker / "audit")), self.assertLogs("audit", level="ERROR"):
            answer = self.client.post("/auth/login", json={"username": "eng", "password": self.passwords["engineer"]})
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.json()["username"], "eng")


class SettingsTests(unittest.TestCase):
    def test_question_setting_is_an_exact_boolean(self):
        self.assertFalse(config.Settings().audit_log_questions)
        self.assertTrue(config.Settings(audit_log_questions="true").audit_log_questions)
        for value in ("yes", "1", "True", "on", ""):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                config.Settings(audit_log_questions=value)

    def test_rotation_settings(self):
        defaults = config.Settings()
        self.assertEqual((defaults.audit_log_max_bytes, defaults.audit_log_keep, defaults.audit_dir),
                         (10 * 1024 * 1024, 10, "/app/uploads/audit"))
        for field, value in (("audit_log_max_bytes", 4095), ("audit_log_keep", 0), ("audit_log_keep", 1001)):
            with self.subTest(field=field, value=value), self.assertRaises(ValidationError):
                config.Settings(**{field: value})
        self.assertEqual(config.Settings(audit_log_max_bytes=4096, audit_log_keep=1000).audit_log_keep, 1000)


class PolicyTests(unittest.TestCase):
    def test_audit_routes_are_engineer_and_human_only(self):
        for path in ("/audit", "/audit.csv"):
            self.assertEqual(policy.ACCESS[("GET", path)], policy.ENGINEER)
            self.assertIn(path, policy.HUMAN_ONLY)

    def test_every_audited_route_is_a_policy_route(self):
        for key in audit.ACTIONS:
            self.assertIn(key, policy.ACCESS)
        self.assertEqual(len(set(audit.ACTIONS.values())), len(audit.ACTIONS))


class MiddlewareTests(AuditTestCase):
    def setUp(self):
        super().setUp()
        self.setup_roles()

    def test_login_success_failure_and_throttle(self):
        password = self.passwords["developer"]
        self.client.post("/auth/login", json={"username": "Dev", "password": password},
                         headers={"X-Real-IP": "192.168.1.7"})
        ok = self.last("auth.login")
        self.assertEqual((ok["actor"], ok["actor_kind"], ok["role"], ok["result"], ok["status_code"], ok["client_ip"]),
                         ("dev", "user", "developer", "success", 200, "192.168.1.7"))
        self.client.post("/auth/login", json={"username": "Nobody", "password": "wrong-password-1"})
        failed = self.last("auth.login")
        self.assertEqual((failed["actor"], failed["actor_kind"], failed["role"], failed["target"], failed["result"],
                          failed["status_code"], failed["details"]),
                         (None, "anonymous", None, "Nobody", "refused", 401, {"code": "LOGIN_FAILED"}))
        for _ in range(6):                                   # the sixth is throttled
            self.client.post("/auth/login", json={"username": "user", "password": "wrong-password-1"})
        self.assertEqual(self.last("auth.login")["details"], {"code": "LOGIN_THROTTLED"})
        self.assertEqual(self.last("auth.login")["status_code"], 429)
        text = self.raw()
        for secret in (password, "wrong-password-1"):
            self.assertNotIn(secret, text)

    def test_logout_and_password_change(self):
        headers = self.session("user")
        self.client.post("/auth/password", headers=headers,
                         json={"current_password": "wrong-current-pw", "new_password": "a-new-password-123"})
        refused = self.last("auth.password")
        self.assertEqual((refused["actor"], refused["result"], refused["details"]),
                         ("user", "refused", {"code": "PASSWORD_INCORRECT"}))
        self.client.post("/auth/logout", headers=headers)
        out = self.last("auth.logout")
        self.assertEqual((out["actor"], out["role"], out["result"]), ("user", "end_user", "success"))
        self.assertNotIn("wrong-current-pw", self.raw())
        self.assertNotIn("a-new-password-123", self.raw())

    def test_identified_403_is_recorded_401_and_unaudited_routes_are_not(self):
        self.client.delete("/collections/Secret?confirm=true", headers=self.session("dev"))
        refused = self.last("collection.delete")
        self.assertEqual((refused["actor"], refused["role"], refused["collection"], refused["result"],
                          refused["status_code"], refused["details"]),
                         ("dev", "developer", "Secret", "refused", 403, {"code": "FORBIDDEN"}))
        count = len(self.records())
        self.client.delete("/collections/Secret?confirm=true")                    # 401: not recorded
        self.client.post("/auth/users/dev/role", json={"role": "engineer"})       # 401 with a target: not recorded
        self.client.get("/collections", headers=self.session("user"))             # not an audited route
        self.assertEqual(len(self.records()), count)

    def test_cross_origin_and_password_change_required_refusals(self):
        headers = {**self.session("eng"), "Origin": "http://elsewhere.example"}
        self.client.post("/ingest/config", headers=headers, json={})
        self.assertEqual(self.last("ingest.config")["details"], {"code": "CROSS_ORIGIN_REFUSED"})
        store.reset_password("dev", passwords.generate_password())
        self.client.post("/retrieval/config", headers=self.session("dev"), json={})
        record = self.last("retrieval.config")
        self.assertEqual((record["actor"], record["details"]), ("dev", {"code": "PASSWORD_CHANGE_REQUIRED"}))

    def test_service_key_actor(self):
        store.create_principal("nightly", "engineer")
        key, secret = store.issue_key("nightly", 30, "")
        answer = self.client.get("/audit", headers={"Authorization": "Bearer " + secret})
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (403, "KEY_NOT_ALLOWED"))
        record = self.last("audit.view")
        self.assertEqual((record["actor"], record["actor_kind"], record["role"], record["key_id"], record["result"]),
                         ("nightly", "service", "engineer", key.id, "refused"))
        self.assertNotIn(secret, self.raw())
        self.assertNotIn(secret.split("_", 2)[2], self.raw())

    def test_query_question_only_when_turned_on(self):
        body = {"question": "What is the patient's diagnosis?", "collection": "Docs", "top_k": 3}
        with patch("services.weaviate_client.collection_exists", AsyncMock(return_value=False)):
            self.assertEqual(self.client.post("/query", headers=self.session("user"), json=body).status_code, 404)
            off = self.last("query")
            with patch.object(settings, "audit_log_questions", True):
                self.client.post("/query", headers=self.session("user"), json=body)
            on = self.last("query")
        self.assertEqual(off["details"], {"retrieval_mode": "hnsw", "top_k": 3, "alpha": 0.75,
                                          "response_format": "end_user", "include_citations": False,
                                          "code": "COLLECTION_NOT_FOUND"})
        self.assertEqual((off["collection"], off["result"]), ("Docs", "refused"))
        self.assertEqual(on["details"]["question"], body["question"])
        self.assertEqual(sum(body["question"] in json.dumps(r) for r in self.records()), 1)

    def test_export_records_encryption_never_the_passphrase(self):
        with patch("services.weaviate_client.collection_exists", AsyncMock(return_value=False)):
            self.client.post("/export", headers=self.session("dev"),
                             json={"collection": "Docs", "passphrase": "correct horse battery staple"})
        record = self.last("export")
        self.assertEqual(record["details"], {"include_models": False, "encrypted": True,
                                             "code": "COLLECTION_NOT_FOUND"})
        self.assertNotIn("correct horse battery staple", self.raw())

    def test_validation_refusal_is_recorded(self):
        self.client.post("/collections", headers=self.session("dev"), json={})
        record = self.last("collection.create")
        self.assertEqual((record["result"], record["status_code"], record["details"]),
                         ("refused", 422, {"code": "INVALID_PARAMETER"}))

    def test_admin_actions_name_the_target_never_the_password(self):
        headers = self.session("eng")
        created = self.client.post("/auth/users", headers=headers, json={"username": "New.Person", "role": "developer"})
        self.assertEqual(created.status_code, 201)
        record = self.last("admin.user.create")
        self.assertEqual((record["actor"], record["target"], record["details"], record["result"]),
                         ("eng", "new.person", {"role": "developer"}, "success"))
        self.assertNotIn(created.json()["password"], self.raw())
        self.client.post("/auth/users/new.person/role", headers=headers, json={"role": "end_user"})
        self.assertEqual(self.last("admin.user.set-role")["details"], {"role": "end_user"})
        self.client.post("/auth/users/eng/reset-password", headers=headers)
        self.assertEqual(self.last("admin.user.reset-password")["details"], {"code": "SELF_RESET_REFUSED"})
        self.client.post("/auth/service-principals", headers=headers, json={"name": "svc", "role": "end_user"})
        issued = self.client.post("/auth/service-principals/svc/keys", headers=headers, json={})
        record = self.last("admin.key.issue")
        self.assertEqual((record["target"], record["details"]["key_id"]), ("svc", issued.json()["key"]["id"]))
        self.assertNotIn(issued.json()["secret"], self.raw())
        self.client.post(f"/auth/service-principals/svc/keys/{issued.json()['key']['id']}/revoke", headers=headers)
        self.assertEqual(self.last("admin.key.revoke")["details"], {"key_id": issued.json()["key"]["id"]})


class ApiTests(AuditTestCase):
    def setUp(self):
        super().setUp()
        for n in range(5):
            audit.record("query", result="success", actor="a", actor_kind="user", role="end_user",
                         collection="Docs", details={"n": n})
        audit.record("auth.login", result="refused", target="=HYPERLINK(1)", status_code=401,
                     details={"code": "LOGIN_FAILED"})
        audit.record("collection.delete", result="success", actor="b", actor_kind="user", role="engineer",
                     collection="Other")
        self.make("eng", "engineer")
        self.make("dev", "developer")

    def get(self, path, user="eng"):
        return self.client.get(path, headers=self.session(user))

    def test_refused_below_engineer(self):
        for path in ("/audit", "/audit.csv"):
            self.assertEqual(self.get(path, "dev").status_code, 403)

    def test_page_newest_first_with_filters_and_cursor(self):
        answer = self.get("/audit")
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.headers["cache-control"], "no-store")
        body = answer.json()
        self.assertEqual([r["action"] for r in body["records"][:2]], ["collection.delete", "auth.login"])
        self.assertIsNone(body["next_before"])
        self.assertFalse(body["question_text_logged"])
        self.assertEqual(body["failed_writes"], audit.failed_writes())
        first = self.get("/audit?action=query&limit=2").json()
        self.assertEqual([r["details"]["n"] for r in first["records"]], [4, 3])
        second = self.get(f"/audit?action=query&limit=2&before={first['next_before']}").json()
        self.assertEqual([r["details"]["n"] for r in second["records"]], [2, 1])
        third = self.get(f"/audit?action=query&limit=2&before={second['next_before']}").json()
        self.assertEqual(([r["details"]["n"] for r in third["records"]], third["next_before"]), ([0], None))
        self.assertEqual(len(self.get("/audit?collection=Other").json()["records"]), 1)
        self.assertEqual(len(self.get("/audit?actor=a&result=success").json()["records"]), 5)
        self.assertEqual(self.get("/audit?to=2000-01-01T00:00:00Z").json()["records"], [])
        self.assertGreaterEqual(len(self.get("/audit?from=2000-01-01T00:00:00").json()["records"]), 7)

    def test_view_is_recorded_after_it_answers(self):
        body = self.get("/audit?action=query").json()
        self.assertNotIn("audit.view", [r["action"] for r in body["records"]])
        record = self.last("audit.view")
        self.assertEqual((record["actor"], record["details"], record["result"]),
                         ("eng", {"action": "query"}, "success"))

    def test_invalid_parameters(self):
        for query in ("limit=0", "limit=1001", "limit=x", "before=abc", "result=maybe", "from=yesterday"):
            with self.subTest(query=query):
                answer = self.get("/audit?" + query)
                self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (422, "INVALID_PARAMETER"))

    def test_csv(self):
        answer = self.get("/audit.csv?action=auth.login")
        self.assertEqual(answer.status_code, 200)
        self.assertTrue(answer.headers["content-type"].startswith("text/csv"))
        self.assertRegex(answer.headers["content-disposition"], r'attachment; filename="audit-\d{8}T\d{6}Z\.csv"')
        rows = list(csv.reader(io.StringIO(answer.text)))
        self.assertEqual(rows[0], list(audit.CSV_COLUMNS))
        self.assertEqual(len(rows), 2)
        row = dict(zip(rows[0], rows[1]))
        self.assertEqual((row["target"], row["actor"], row["status_code"], row["details"]),
                         ("'=HYPERLINK(1)", "", "401", '{"code":"LOGIN_FAILED"}'))
        everything = list(csv.reader(io.StringIO(self.get("/audit.csv").text)))
        self.assertEqual(len(everything), 1 + 7 + 1)      # header, the records, the earlier download
        self.assertEqual(self.last("audit.download")["details"], {})


class JobTests(AuditTestCase):
    PRINCIPAL = store.Principal(1, "dev", "developer", False, "")

    def started(self, store_, job_id, action="export", **details):
        ctx = {"scope": {"state": {"auth": self.PRINCIPAL}}, "action": action, "actor": None,
               "actor_kind": "anonymous", "role": None, "key_id": None, "client_ip": "10.1.1.1",
               "collection": "Docs", "target": None, "details": details, "job_recorded": False}
        token = audit._current.set(ctx)
        try:
            store_[job_id] = {"job_id": job_id, "status": "queued", "collection": "Docs"}
        finally:
            audit._current.reset(token)
        self.assertTrue(ctx["job_recorded"])
        return store_[job_id]

    def test_accepted_then_one_end_record(self):
        jobs = jobstore.JobStore("export")
        jobs.load()
        job = self.started(jobs, "abc123", include_models=False, encrypted=False)
        job["status"] = "running"
        job.update(status="completed", filename="docs.tar.gz", fidelity="with-sources")
        job["status"] = "completed"
        job["warnings"] = []
        accepted, ended = [r for r in self.records() if r["job_id"] == "abc123"]
        self.assertEqual((accepted["result"], accepted["status_code"], accepted["actor"], accepted["client_ip"]),
                         ("accepted", 202, "dev", "10.1.1.1"))
        self.assertEqual((ended["result"], ended["status_code"], ended["actor"], ended["role"], ended["collection"]),
                         ("succeeded", None, "dev", "developer", "Docs"))
        self.assertEqual(ended["details"], {"include_models": False, "encrypted": False, "job_status": "completed",
                                            "package": "docs.tar.gz", "fidelity": "with-sources"})
        self.assertNotIn("_audit", jobstore.JobStore.__dict__)

    def test_partial_is_failed_and_unaudited_jobs_write_nothing(self):
        jobs = jobstore.JobStore("ingest")
        jobs.load()
        job = self.started(jobs, "ing1", action="ingest", files=["a.pdf"], file_count=1, total_bytes=10)
        job["status"] = "partial"
        self.assertEqual(self.records()[-1]["result"], "failed")
        self.assertEqual(self.records()[-1]["details"]["job_status"], "partial")
        jobs["plain"] = {"job_id": "plain", "status": "queued"}
        jobs["plain"]["status"] = "completed"
        self.assertEqual([r for r in self.records() if r["job_id"] == "plain"], [])

    def test_interrupted_at_start(self):
        jobs = jobstore.JobStore("tune")
        jobs.load()
        job = self.started(jobs, "tun1", action="tune.rechunk", operation="rechunk")
        job["status"] = "running"
        again = jobstore.JobStore("tune")
        self.assertEqual(again.load(), ["tun1"])
        ended = self.records()[-1]
        self.assertEqual((ended["action"], ended["result"], ended["job_id"], ended["actor"]),
                         ("tune.rechunk", "interrupted", "tun1", "dev"))
        once_more = jobstore.JobStore("tune")
        once_more.load()
        self.assertEqual(sum(1 for r in self.records() if r["job_id"] == "tun1"), 2)

    def test_import_end_names_the_collection(self):
        jobs = jobstore.JobStore("import")
        jobs.load()
        job = self.started(jobs, "imp1", action="import", package="p.tar.gz", on_conflict="rename", encrypted=False)
        job.update(status="completed", collection="Docs_2")
        self.assertEqual(self.records()[-1]["collection"], "Docs_2")


class CommandTests(AuditTestCase):
    def run_auth(self, *args):
        env = {**os.environ, "AUTH_DIR": str(self.root / "auth"), "AUDIT_DIR": str(self.audit_dir)}
        return subprocess.run([sys.executable, "-m", "auth", *args], cwd=API_DIR, env=env,
                              capture_output=True, text=True, timeout=120)

    def test_operator_actions_are_recorded_never_the_password(self):
        made = self.run_auth("bootstrap", "Admin")
        self.assertEqual(made.returncode, 0, made.stderr)
        record = self.last("operator.bootstrap")
        self.assertEqual((record["actor"], record["actor_kind"], record["target"], record["details"],
                          record["result"], record["client_ip"], record["status_code"]),
                         ("operator", "operator", "admin", {"role": "engineer"}, "success", None, None))
        refused = self.run_auth("bootstrap", "second")
        self.assertEqual(refused.returncode, 1)
        self.assertEqual(self.last("operator.bootstrap")["result"], "refused")
        self.assertIn("code", self.last("operator.bootstrap")["details"])
        reset = self.run_auth("reset-password", "admin")
        self.assertEqual(self.last("operator.reset-password")["target"], "admin")
        count = len(self.records())
        self.run_auth("list")
        self.run_auth("set-role", "!!", "developer")                   # usage error
        self.assertEqual(len(self.records()), count)
        for secret in (made.stdout.strip(), reset.stdout.strip()):
            self.assertNotIn(secret, self.raw())

    def test_unwritable_log_is_one_more_stderr_line(self):
        blocker = self.root / "file"
        blocker.write_text("x")
        env = {**os.environ, "AUTH_DIR": str(self.root / "auth"), "AUDIT_DIR": str(blocker / "audit")}
        result = subprocess.run([sys.executable, "-m", "auth", "bootstrap", "admin"], cwd=API_DIR, env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the audit record could not be written", result.stderr)


class ImplementationCopyTests(unittest.TestCase):
    def test_embedded_copies_match(self):
        root = Path(API_DIR).parent
        document = (root / "IMPLEMENTATION.md").read_text()
        for name in ("api/services/audit.py", "api/routers/audit.py", "ui/src/pages/AuditPage.tsx",
                     "scripts/verify/19_audit.sh", "scripts/verify/audit_stack.py"):
            with self.subTest(name=name):
                header = "### " + name + "\n\n```"
                start = document.index("\n", document.index(header) + len(header)) + 1
                end = document.index("\n```\n", start)
                self.assertEqual(document[start:end], (root / name).read_text().rstrip("\n"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
