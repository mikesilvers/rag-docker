"""Query-only mode (#670): the setting, the refused routes, the flag in the
user answers, the loader and the access of a collection it creates.

Runs with the API's dependencies: in the built API image with no network, as
`24_query_only.sh` does,

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_query_only.py

or on the host with them installed. Every test uses its own temporary account
store, audit folder, upload folder and exports folder; requests go through
`main.app` with FastAPI's TestClient, without the lifespan. Weaviate and Ollama
are blocked for the whole run, as in test_auth.py, and the loader's import run
is replaced where it would need them.
"""
import asyncio
import contextlib
import fcntl
import io
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

API_DIR = os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api")
sys.path.insert(0, API_DIR)
ROOT = Path(API_DIR).parent

import httpx
from fastapi.testclient import TestClient
from pydantic import ValidationError

import config
import loader
from config import settings
from main import app
from auth import keys, passwords, policy, store
from auth.middleware import AuthMiddleware
from services import importer, ollama_client, weaviate_client

ORIGIN = "http://testserver"

# The 19 routes refused in query-only mode (#670, decision Q2), written out
# here rather than read from the policy, so a change to either is a decision.
REFUSED = {
    ("POST", "/collections"), ("DELETE", "/collections/{name}"),
    ("POST", "/ingest/upload"), ("POST", "/ingest/config"), ("POST", "/retrieval/config"),
    ("POST", "/goldstandard/generate"), ("POST", "/goldstandard/regenerate"),
    ("PATCH", "/goldstandard/session/{session_id}/pair/{pair_id}"), ("POST", "/goldstandard/save"),
    ("POST", "/import"),
    ("POST", "/prepare/upload"), ("DELETE", "/prepare/documents/{document_id}"),
    ("DELETE", "/prepare/documents/{document_id}/prepared"), ("POST", "/prepare/jobs"),
    ("POST", "/prepare/job/{job_id}/cancel"), ("POST", "/prepare/import"),
    ("POST", "/tune/rechunk"), ("POST", "/tune/reembed"), ("POST", "/tune/reindex"),
}
# Every other state-changing route keeps working: a new one fails this test
# until it is put in one list or the other.
KEPT = {
    ("POST", "/auth/login"), ("POST", "/auth/logout"), ("POST", "/auth/password"),
    ("POST", "/auth/login/mfa"), ("POST", "/auth/mfa/enroll"), ("POST", "/auth/mfa/enroll/confirm"),
    ("POST", "/auth/mfa/recovery-codes"), ("POST", "/auth/mfa/disable"), ("POST", "/auth/mfa/policy"),
    ("POST", "/auth/users"), ("POST", "/auth/users/{username}/reset-password"),
    ("POST", "/auth/users/{username}/role"), ("POST", "/auth/users/{username}/disable"),
    ("POST", "/auth/users/{username}/enable"), ("POST", "/auth/users/{username}/reset-mfa"),
    ("POST", "/auth/service-principals"), ("POST", "/auth/service-principals/{name}/role"),
    ("POST", "/auth/service-principals/{name}/disable"), ("POST", "/auth/service-principals/{name}/enable"),
    ("POST", "/auth/service-principals/{name}/keys"),
    ("POST", "/auth/service-principals/{name}/keys/{key_id}/revoke"),
    ("POST", "/query"), ("POST", "/export"), ("GET", "/packages/{filename}"),
    ("PUT", "/collections/{name}/access"),
    # Groups for collection access (#784): /auth routes, like account administration.
    ("POST", "/auth/groups"), ("POST", "/auth/groups/{name}/rename"), ("PUT", "/auth/groups/{name}/members"),
    ("POST", "/auth/groups/{name}/delete"),
}


class _NoNetwork(httpx.AsyncBaseTransport):
    async def handle_async_request(self, request):
        raise httpx.ConnectError("test_query_only.py runs without a network", request=request)


class _OfflineAsyncClient(httpx.AsyncClient):
    def __init__(self, *args, **kwargs):
        kwargs["transport"] = _NoNetwork()
        super().__init__(*args, **kwargs)


def _no_weaviate():
    raise ConnectionError("test_query_only.py runs without a network: there is no Weaviate")


_OFFLINE = (patch.object(weaviate_client, "get_client", _no_weaviate),
            patch.object(httpx, "AsyncClient", _OfflineAsyncClient))


def setUpModule():
    for offline in _OFFLINE:
        offline.start()


def tearDownModule():
    for offline in reversed(_OFFLINE):
        offline.stop()


def concrete(template):
    return re.sub(r"\{[a-z_]+\}", "x", template)


class Folders(unittest.TestCase):
    """Temporary account store, audit, upload and exports folders, and the mode."""

    QUERY_ONLY = True

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name in ("auth", "audit", "uploads", "exports"):
            (self.root / name).mkdir()
        for name, value in (("auth_dir", str(self.root / "auth")), ("audit_dir", str(self.root / "audit")),
                            ("upload_dir", str(self.root / "uploads")),
                            ("exports_dir", str(self.root / "exports")),
                            ("rag_query_only", self.QUERY_ONLY)):
            target = patch.object(settings, name, value)
            target.start()
            self.addCleanup(target.stop)

    def records(self, action=None):
        path = self.root / "audit" / "audit.jsonl"
        if not path.exists():
            return []
        found = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        return [r for r in found if action is None or r["action"] == action]


# ── S1: the setting ──────────────────────────────────────────────────────────

class SettingTests(unittest.TestCase):
    def test_exactly_true_or_false(self):
        for value, expected in (("true", True), ("false", False)):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"RAG_QUERY_ONLY": value}):
                    self.assertIs(config.Settings().rag_query_only, expected)
        self.assertIs(config.Settings.model_fields["rag_query_only"].default, False)
        for value in ("yes", "1", "TRUE", "on", ""):
            with self.subTest(value=value):
                with patch.dict(os.environ, {"RAG_QUERY_ONLY": value}):
                    with self.assertRaises(ValidationError) as caught:
                        config.Settings()
                self.assertEqual(config.refusal_lines(caught.exception),
                                 ["Settings refused: RAG_QUERY_ONLY must be exactly true or false"])

    def test_the_overlay_sets_only_the_api_switch(self):
        text = (ROOT / "docker-compose.query-only.yml").read_text()
        body = [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        self.assertEqual(body, ["services:", "  api:", "    environment:", '      RAG_QUERY_ONLY: "true"'])
        base = (ROOT / "docker-compose.yml").read_text()
        self.assertEqual(base.count('RAG_QUERY_ONLY: "false"'), 1)

    def test_config_check_compares_the_whole_configuration(self):
        sys.path.insert(0, str(ROOT / "scripts" / "verify"))
        try:
            import query_only_config
        finally:
            sys.path.pop(0)
        base = {"services": {"api": {"environment": {"RAG_QUERY_ONLY": "false", "A": "1"}}, "ui": {}}}
        good = {"services": {"api": {"environment": {"RAG_QUERY_ONLY": "true", "A": "1"}}, "ui": {}}}
        self.assertEqual(query_only_config.problems(base, good), [])
        worse = json.loads(json.dumps(good))
        worse["services"]["ui"] = {"ports": ["0.0.0.0:1:1"]}
        self.assertEqual(len(query_only_config.problems(base, worse)), 1)
        self.assertEqual(len(query_only_config.problems(base, base)), 1)


# ── S2: the policy ───────────────────────────────────────────────────────────

class PolicyTests(unittest.TestCase):
    def test_the_refused_routes_are_the_decided_ones(self):
        self.assertEqual(set(policy.QUERY_ONLY_REFUSED), REFUSED)

    def test_every_state_changing_route_is_classified_once(self):
        changing = {key for key in policy.ACCESS
                    if key[0] != "GET" or key[1] in policy.STATE_CHANGING_GET}
        self.assertEqual(set(policy.QUERY_ONLY_KEPT), KEPT,
                         "a state-changing route is not classified for query-only mode (#670)")
        self.assertEqual(REFUSED | KEPT, changing)
        self.assertEqual(REFUSED & KEPT, set())
        self.assertTrue(REFUSED <= set(policy.ACCESS))


# ── S2, S3: through the app ──────────────────────────────────────────────────

class AppTests(Folders):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.passwords = {}
        for name, role in (("eng", "engineer"), ("dev", "developer"), ("user", "end_user")):
            self.passwords[name] = passwords.generate_password()
            store.create_user(name, role, self.passwords[name], bootstrap=not store.is_set_up(),
                              must_change_password=False)

    def session(self, username):
        token = store.create_session(store.get_user(username).id)
        return {"Cookie": f"{store.COOKIE}={token}", "Origin": ORIGIN}

    def code(self, response):
        try:
            return response.json()["error"]["code"]
        except (ValueError, KeyError, TypeError):
            return None

    def test_each_refused_route_answers_query_only(self):
        for method, template in sorted(REFUSED):
            with self.subTest(route=(method, template)):
                response = self.client.request(method, concrete(template), json={},
                                               headers=self.session("eng"))
                self.assertEqual((response.status_code, self.code(response)), (403, "QUERY_ONLY"))
                self.assertIn("query-only", response.json()["error"]["message"])

    def test_other_refusals_come_first(self):
        # No session: 401. A Developer on an Engineer's route: FORBIDDEN. Cross-origin first too.
        response = self.client.post("/import", json={})
        self.assertEqual((response.status_code, self.code(response)), (401, "AUTH_REQUIRED"))
        response = self.client.delete("/collections/x", headers=self.session("dev"))
        self.assertEqual((response.status_code, self.code(response)), (403, "FORBIDDEN"))
        response = self.client.post("/import", json={}, headers={**self.session("user")})
        self.assertEqual(self.code(response), "FORBIDDEN")
        headers = {**self.session("eng"), "Origin": "http://elsewhere.example"}
        response = self.client.post("/import", json={}, headers=headers)
        self.assertEqual(self.code(response), "CROSS_ORIGIN_REFUSED")

    def test_a_key_is_refused_too(self):
        store.create_principal("svc", "engineer")
        _, key = store.issue_key("svc", 30, "")
        response = self.client.post("/tune/reindex", json={}, headers={"Authorization": f"Bearer {key}"})
        self.assertEqual((response.status_code, self.code(response)), (403, "QUERY_ONLY"))
        response = self.client.post("/import", json={}, headers={"Authorization": "Bearer nonsense"})
        self.assertEqual(self.code(response), "KEY_INVALID")

    def test_kept_routes_are_not_refused(self):
        for method, template in sorted(KEPT):
            with self.subTest(route=(method, template)):
                response = self.client.request(method, concrete(template), json={},
                                               headers=self.session("eng"))
                self.assertNotEqual(self.code(response), "QUERY_ONLY")
        self.assertEqual(self.client.get("/collections", headers=self.session("user")).status_code != 403, True)

    def test_off_nothing_is_refused_for_the_mode(self):
        with patch.object(settings, "rag_query_only", False):
            for method, template in sorted(REFUSED):
                with self.subTest(route=(method, template)):
                    response = self.client.request(method, concrete(template), json={},
                                                   headers=self.session("eng"))
                    self.assertNotEqual(self.code(response), "QUERY_ONLY")

    def test_the_refusal_is_audited_with_its_principal(self):
        self.client.post("/import", json={"filename": "ragpkg-x.tar.gz"}, headers=self.session("eng"))
        found = self.records("import")
        self.assertEqual(len(found), 1, found)
        self.assertEqual((found[0]["actor"], found[0]["result"], found[0]["status_code"]),
                         ("eng", "refused", 403))
        self.assertIn("QUERY_ONLY", json.dumps(found[0]))

    def test_the_body_of_a_refused_request_is_never_read(self):
        token = store.create_session(store.get_user("eng").id)
        scope = {"type": "http", "method": "POST", "path": "/ingest/upload", "query_string": b"",
                 "headers": [(b"cookie", f"{store.COOKIE}={token}".encode()), (b"origin", ORIGIN.encode()),
                             (b"host", b"testserver")]}
        sent = []

        async def inner(scope, receive, send):
            raise AssertionError("the route ran")

        async def receive():
            raise AssertionError("the body was read")

        async def send(message):
            sent.append(message)

        asyncio.run(AuthMiddleware(inner)(scope, receive, send))
        self.assertEqual(sent[0]["status"], 403)
        self.assertIn(b"QUERY_ONLY", sent[1]["body"])

    def test_user_answers_carry_the_mode(self):
        response = self.client.post("/auth/login", json={"username": "user", "password": self.passwords["user"]},
                                    headers={"Origin": ORIGIN})
        self.assertIs(response.json()["query_only"], True)
        self.assertIs(self.client.get("/auth/me", headers=self.session("user")).json()["query_only"], True)
        with patch.object(settings, "rag_query_only", False):
            self.assertIs(self.client.get("/auth/me", headers=self.session("user")).json()["query_only"], False)
        self.assertNotIn("query_only", self.client.get("/auth/status").json())

    def test_user_answers_carry_lan_mode(self):
        """LAN mode (#26) is in every user answer, so the Transfer page can hide
        the unencrypted export (#676)."""
        with patch.object(settings, "rag_lan_mode", True):
            response = self.client.post("/auth/login", json={"username": "user", "password": self.passwords["user"]},
                                        headers={"Origin": ORIGIN})
            self.assertIs(response.json()["lan_mode"], True)
            self.assertIs(self.client.get("/auth/me", headers=self.session("user")).json()["lan_mode"], True)
        self.assertIs(self.client.get("/auth/me", headers=self.session("user")).json()["lan_mode"], False)
        self.assertNotIn("lan_mode", self.client.get("/auth/status").json())


# ── S4: the loader ───────────────────────────────────────────────────────────

class LoaderTests(Folders):
    def setUp(self):
        super().setUp()
        self.sigint = patch.object(loader.signal, "signal")   # never change the test runner's SIGINT
        self.sigint.start()
        self.addCleanup(self.sigint.stop)
        self.sweep = patch.object(importer, "sweep_interrupted_imports", return_value=[])
        self.swept = self.sweep.start()
        self.addCleanup(self.sweep.stop)
        self.calls = []

    def package(self, name="ragpkg-vfy-1.tar.gz"):
        (self.root / "exports" / name).write_bytes(b"not read: the import run is replaced")
        return name

    def fake_run(self, status="completed", **fields):
        def run(job_id, filename, on_conflict, passphrase=None, identity=None):
            job = importer._jobs[job_id]
            self.calls.append({"job": dict(job), "filename": filename, "on_conflict": on_conflict,
                               "passphrase": passphrase, "identity": identity,
                               "lock_held": self.lock_is_held()})
            job.update({"status": status, **fields})
        return patch.object(importer, "_run", run)

    def lock_is_held(self):
        with open(self.root / "uploads" / "loader.lock", "a") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(other, fcntl.LOCK_UN)
            return False

    def main(self, argv, stdin=""):
        err = io.StringIO()
        with patch.object(sys, "stdin", io.StringIO(stdin)), contextlib.redirect_stderr(err):
            try:
                code = loader.main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, err.getvalue()

    def test_usage_errors_exit_2_and_record_nothing(self):
        for argv in ([], ["../etc/passwd"], ["notes.txt"], ["exports/ragpkg-a.tar.gz"],
                     ["ragpkg-a.tar.gz", "--on-conflict", "merge"], ["ragpkg-a.tar.gz", "--passphrase", "x"]):
            with self.subTest(argv=argv):
                code, _ = self.main(argv)
                self.assertEqual(code, 2)
        self.assertEqual(self.records(), [])

    def test_refused_on_a_full_instance(self):
        name = self.package()
        with patch.object(settings, "rag_query_only", False), self.fake_run():
            code, err = self.main([name])
        self.assertEqual(code, 1)
        self.assertIn("load refused: NOT_QUERY_ONLY", err)
        self.assertEqual(self.calls, [])
        record = self.records("operator.load")[-1]
        self.assertEqual((record["actor"], record["actor_kind"], record["result"], record["target"]),
                         ("operator", "operator", "refused", name))
        self.assertEqual(record["details"], {"on_conflict": "abort", "encrypted": False, "code": "NOT_QUERY_ONLY"})

    def test_one_load_at_a_time(self):
        name = self.package()
        with open(self.root / "uploads" / "loader.lock", "a") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            with self.fake_run():
                code, err = self.main([name])
        self.assertEqual(code, 1)
        self.assertIn("LOAD_IN_PROGRESS", err)
        self.assertEqual(self.calls, [])

    def test_a_missing_package(self):
        with self.fake_run():
            code, err = self.main(["ragpkg-absent.tar.gz"])
        self.assertEqual(code, 1)
        self.assertIn("PACKAGE_NOT_FOUND", err)
        (self.root / "exports" / "ragpkg-dir.tar.gz").mkdir()
        os.symlink(self.root / "elsewhere", self.root / "exports" / "ragpkg-link.tar.gz")
        for name in ("ragpkg-dir.tar.gz", "ragpkg-link.tar.gz"):
            with self.subTest(name=name), self.fake_run():
                code, err = self.main([name])
                self.assertEqual((code, "PACKAGE_NOT_FOUND" in err), (1, True))
        self.assertEqual(self.calls, [])

    def test_a_load_runs_the_import_code_under_the_lock(self):
        name = self.package()
        with self.fake_run(collection="Policies", original_collection="Policies", chunks_written=12,
                           fidelity="with-sources", notes=["a note"]):
            code, err = self.main([name, "--on-conflict", "replace"])
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.calls), 1)
        call = self.calls[0]
        self.assertEqual((call["filename"], call["on_conflict"], call["passphrase"], call["lock_held"]),
                         (name, "replace", None, True))
        self.assertIs(call["job"]["operator"], True)
        self.assertIsNone(call["job"]["owner"])
        self.swept.assert_called_once()
        self.assertIn("loaded into collection Policies: 12 chunks, with-sources", err)
        self.assertIn("note: a note", err)
        self.assertFalse(self.lock_is_held())
        record = self.records("operator.load")[-1]
        self.assertEqual((record["result"], record["collection"], record["target"]), ("success", "Policies", name))
        self.assertEqual(record["details"], {"on_conflict": "replace", "encrypted": False, "chunks": 12})

    def test_a_failed_load(self):
        name = self.package()
        with self.fake_run(status="failed", error_code="EMBEDDING_MISMATCH", error="another model",
                           collection=None):
            code, err = self.main([name])
        self.assertEqual(code, 1)
        self.assertIn("load failed: EMBEDDING_MISMATCH: another model", err)
        record = self.records("operator.load")[-1]
        # A load that ran and failed is `failed`, not `refused` (maintainer, 2026-11-18).
        self.assertEqual((record["result"], record["details"]["code"]), ("failed", "EMBEDDING_MISMATCH"))

    def test_an_encrypted_package_reads_its_passphrase_from_stdin(self):
        name = self.package("ragpkg-vfy-2.tar.gz.age")
        secret = "correct horse battery staple"
        with self.fake_run(collection="P", original_collection="P", chunks_written=1):
            code, err = self.main([name], stdin=secret + "\n")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.calls[0]["passphrase"], secret)
        self.assertNotIn(secret, err)
        self.assertNotIn(secret, (self.root / "audit" / "audit.jsonl").read_text())
        self.assertIs(self.records("operator.load")[-1]["details"]["encrypted"], True)
        with self.fake_run():
            code, err = self.main([name], stdin="\n")
        self.assertEqual(code, 1)
        self.assertIn("PASSPHRASE_REQUIRED", err)
        self.assertEqual(len(self.calls), 1)

    def key_package(self, name="ragpkg-vfy-3.tar.gz.age"):
        """A package encrypted to a new key (#676), and the key file's text."""
        from datetime import datetime, timezone
        from services import age_stream
        identity = age_stream.generate_identity()
        with open(self.root / "exports" / name, "wb") as fh:
            enc = age_stream.Encryptor(fh, recipient=identity.public_bytes)
            enc.write(b"not read: the import run is replaced")
            enc.close()
        return name, identity, age_stream.identity_file(identity, datetime.now(timezone.utc), name)

    def test_a_key_package_reads_its_key_file_from_stdin(self):
        name, identity, text = self.key_package()
        with self.fake_run(collection="K", original_collection="K", chunks_written=1):
            code, err = self.main([name], stdin=text)
        self.assertEqual(code, 0, err)
        call = self.calls[0]
        self.assertIsNone(call["passphrase"])
        self.assertEqual(call["identity"].public_bytes, identity.public_bytes)
        self.assertEqual(call["job"]["encryption"], "key")
        line = identity.encode()
        self.assertNotIn(line, err)
        self.assertNotIn(line, (self.root / "audit" / "audit.jsonl").read_text())
        details = self.records("operator.load")[-1]["details"]
        self.assertEqual((details["encrypted"], details["encryption"]), (True, "key"))

    def test_a_key_package_without_a_valid_key_is_refused_before_the_load(self):
        name, identity, text = self.key_package()
        for stdin, code_name in (("", "KEY_REQUIRED"), ("\n# just a comment\n", "KEY_INVALID"),
                                 ("correct horse battery staple\n", "KEY_INVALID"),
                                 ("x" * 5000, "KEY_INVALID")):
            with self.subTest(code=code_name, stdin=stdin[:12]), self.fake_run():
                code, err = self.main([name], stdin=stdin)
                self.assertEqual(code, 1, err)
                self.assertIn(code_name, err)
                record = self.records("operator.load")[-1]
                self.assertEqual((record["result"], record["details"]["code"],
                                  record["details"]["encryption"]), ("refused", code_name, "key"))
        self.assertEqual(self.calls, [])
        self.assertNotIn(identity.encode(), (self.root / "audit" / "audit.jsonl").read_text())

    def test_progress_lines(self):
        name = self.package()

        def run(job_id, filename, on_conflict, passphrase=None):
            job = importer._jobs[job_id]
            job["chunks_written"] = 7
            import time
            time.sleep(0.3)
            job.update({"status": "completed", "collection": "P", "original_collection": "P"})

        with patch.object(loader, "PROGRESS_SECONDS", 0.1), patch.object(importer, "_run", run):
            code, err = self.main([name])
        self.assertEqual(code, 0)
        self.assertIn("loader: chunks written: 7", err)

    def test_ctrl_c_is_recorded_then_acts_as_a_kill(self):
        # The handler is installed once the arguments are read; on SIGINT it
        # records the load as interrupted (with the job's collection), restores
        # SIG_DFL and sends SIGINT again, so the import's `finally` never runs.
        name = self.package()
        with self.fake_run(collection="P", original_collection="P"):
            self.main([name])
        signum, handler = loader.signal.signal.call_args_list[0][0]
        self.assertEqual(signum, loader.signal.SIGINT)
        self.assertTrue(callable(handler))
        loader._state.clear()
        loader._state["job"] = {"collection": "Policies"}
        loader.signal.signal.reset_mock()
        real_write = os.write
        with patch.object(loader.os, "kill") as kill, \
                patch.object(loader.os, "write", side_effect=lambda fd, data: len(data) if fd == 2
                             else real_write(fd, data)) as write:
            handler(loader.signal.SIGINT, None)
        loader.signal.signal.assert_called_once_with(loader.signal.SIGINT, loader.signal.SIG_DFL)
        kill.assert_called_once_with(os.getpid(), loader.signal.SIGINT)
        # Straight to fd 2, not through sys.stderr (a reentrant print could raise).
        self.assertEqual(write.call_args_list[0][0], (2, b"loader: load interrupted: INTERRUPTED\n"))
        record = self.records("operator.load")[-1]
        self.assertEqual((record["result"], record["collection"], record["target"], record["details"]["code"]),
                         ("interrupted", "Policies", name, "INTERRUPTED"))

    def test_the_kill_happens_even_when_the_record_fails(self):
        handler = loader._interrupted("ragpkg-x.tar.gz", "abort", False)
        loader._state.clear()
        with patch.object(loader.os, "kill") as kill, patch.object(loader.os, "write"), \
                patch.object(loader, "_audit", side_effect=RuntimeError("reentrant call")):
            with self.assertRaises(RuntimeError):
                handler(loader.signal.SIGINT, None)
        kill.assert_called_once_with(os.getpid(), loader.signal.SIGINT)
        with patch.object(loader.os, "kill") as kill, \
                patch.object(loader.os, "write", side_effect=OSError("closed")):
            with self.assertRaises(OSError):
                handler(loader.signal.SIGINT, None)
        kill.assert_called_once_with(os.getpid(), loader.signal.SIGINT)
        loader._state.clear()

    def test_after_the_runs_own_record_ctrl_c_writes_none(self):
        # The run marks itself done before its final record, whatever the result.
        name = self.package()
        for run, argv in ((self.fake_run(collection="P", original_collection="P"), [name]),
                          (self.fake_run(status="failed", error_code="IMPORT_FAILED"), [name]),
                          (self.fake_run(), ["ragpkg-absent.tar.gz"])):
            with self.subTest(argv=argv, run=run), run:
                self.main(argv)
                self.assertIs(loader._state.get("done"), True)
                handler = loader.signal.signal.call_args_list[0][0][1]
                before = len(self.records())
                with patch.object(loader.os, "kill") as kill, patch.object(loader.os, "write") as write:
                    handler(loader.signal.SIGINT, None)
                kill.assert_called_once()
                write.assert_not_called()
                self.assertEqual(len(self.records()), before)
                loader.signal.signal.reset_mock()
        loader._state.clear()

    def test_ctrl_c_while_a_record_is_written_writes_no_second_one(self):
        handler = loader._interrupted("ragpkg-x.tar.gz", "abort", False)
        loader._state.clear()
        loader._state["auditing"] = True
        before = len(self.records())
        with patch.object(loader.os, "kill") as kill:
            handler(loader.signal.SIGINT, None)
        kill.assert_called_once()
        self.assertEqual(len(self.records()), before)
        loader._state.clear()


# ── S5: access to a collection the loader creates ────────────────────────────

class AccessTests(Folders):
    def setUp(self):
        super().setUp()
        store.create_user("eng", "engineer", passwords.generate_password(), bootstrap=True,
                          must_change_password=False)

    def test_an_operator_load_restricts_a_new_collection_with_no_grants(self):
        with patch.object(importer, "exists_fail_closed", return_value=False):
            states = importer._start_access(None, "Policies", "Policies", operator=True)
        self.assertIsNotNone(states)
        self.assertEqual(store.collection_access("Policies"), store.CollectionAccess(True, (), ()))

    def test_without_an_owner_or_the_operator_nothing_is_recorded(self):
        self.assertIsNone(importer._start_access(None, "Policies", "Policies"))
        self.assertEqual(store.collection_access("Policies"), store.CollectionAccess(False))

    def test_a_rename_starts_engineers_only_too(self):
        # Maintainer, 2026-11-18 (#808): the loader never copies an existing
        # collection's access, even an open one; only replace keeps access.
        store.set_collection_access("Policies", False)
        with patch.object(importer, "exists_fail_closed", return_value=True):
            importer._start_access(None, "Policies", "Policies_ab12cd34", operator=True)
        self.assertEqual(store.collection_access("Policies_ab12cd34"), store.CollectionAccess(True, (), ()))
        self.assertEqual(store.collection_access("Policies"), store.CollectionAccess(False))

    def test_a_staging_copys_package_starts_engineers_only(self):
        store.set_collection_access("Policies", False)
        staging = "Policies__importing_" + "0" * 32
        with patch.object(importer, "exists_fail_closed", return_value=True):
            importer._start_access(None, "Policies", staging, operator=True)
        self.assertEqual(store.collection_access(staging), store.CollectionAccess(True, (), ()))

    def test_an_owners_import_still_copies_the_source_state(self):
        # The API's import is unchanged (#606): a rename copies the source's state.
        store.set_collection_access("Policies", False)
        with patch.object(importer, "exists_fail_closed", return_value=True):
            importer._start_access({"name": "eng", "kind": "user"}, "Policies", "Policies_ab12cd34")
        self.assertEqual(store.collection_access("Policies_ab12cd34"), store.CollectionAccess(False))

    def test_the_operator_is_never_refused_for_access(self):
        store.restrict_to("Policies", None)
        with patch.object(importer, "exists_fail_closed", return_value=True):
            self.assertFalse(importer._refused(None, "Policies"))


class ImplementationNotes(unittest.TestCase):
    def test_the_loader_docstring_is_its_usage(self):
        self.assertIn("python -m loader PACKAGE [--on-conflict abort|rename|replace]", loader.__doc__)


if __name__ == "__main__":
    unittest.main(verbosity=2)
