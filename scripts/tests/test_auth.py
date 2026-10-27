"""Accounts, sessions, the endpoint/role matrix and the operator command (#25).

Runs with the API's dependencies, in the built API image with no network
(`17_auth.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_auth.py

Every test uses its own temporary store (AUTH_DIR); requests go through
`main.app` with FastAPI's TestClient, without the lifespan, and nothing reaches
a backend.
"""
import asyncio
import hashlib
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

API_DIR = os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api")
sys.path.insert(0, API_DIR)

from fastapi.testclient import TestClient
from pydantic import ValidationError

import config
from config import settings
from main import app
from auth import passwords, policy, store
from auth.middleware import AuthMiddleware, same_origin

ORIGIN = "http://testserver"


def logged(logs):
    return "\n".join(logging.Formatter().format(record) for record in logs.records)


class StoreTestCase(unittest.TestCase):
    """A fresh, empty store per test."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = temp.name
        target = patch.object(settings, "auth_dir", self.dir)
        target.start()
        self.addCleanup(target.stop)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def make(self, username, role, *, must_change=False):
        password = passwords.generate_password()
        store.create_user(username, role, password, bootstrap=not store.is_set_up(),
                          must_change_password=must_change)
        return password

    def setup_roles(self):
        """An engineer, a developer and an end user, all past their first login."""
        self.passwords = {role: self.make(name, role) for role, name in
                          (("engineer", "eng"), ("developer", "dev"), ("end_user", "user"))}

    def login(self, username, password, **headers):
        return self.client.post("/auth/login", json={"username": username, "password": password}, headers=headers)

    def session(self, username):
        """Cookie and Origin headers for a new session of `username`."""
        token = store.create_session(store.get_user(username).id)
        return {"Cookie": f"{store.COOKIE}={token}", "Origin": ORIGIN}, token


class PasswordTests(unittest.TestCase):
    def test_hash_format_and_parameters(self):
        stored = passwords.hash_password("correct horse battery")
        scheme, log2_n, r, p, salt, digest = stored.split("$")
        self.assertEqual((scheme, log2_n, r, p), ("scrypt", "15", "8", "1"))
        import base64
        self.assertEqual(len(base64.b64decode(salt)), 16)
        self.assertEqual(len(base64.b64decode(digest)), 32)
        self.assertNotEqual(stored, passwords.hash_password("correct horse battery"), "salt is random")

    def test_verify(self):
        stored = passwords.hash_password("correct horse battery")
        self.assertTrue(passwords.verify_password("correct horse battery", stored))
        self.assertFalse(passwords.verify_password("correct horse batterY", stored))
        for broken in ("", "plain", "md5$1$1$1$a$b", "scrypt$15$8$1$!!$!!", "scrypt$40$8$1$YQ==$YQ=="):
            self.assertFalse(passwords.verify_password("x", broken), broken)

    def test_username_rule(self):
        self.assertEqual(passwords.normalize_username("Alice.B_c-1"), "alice.b_c-1")
        for bad in ("ab", "a" * 33, "has space", "naïve", "semi;colon", "", None):
            self.assertIsNone(passwords.normalize_username(bad), bad)

    def test_password_rule(self):
        self.assertIsNone(passwords.password_problem("x" * 12, "alice"))
        self.assertIsNone(passwords.password_problem("x" * 256, "alice"))
        self.assertIn("12", passwords.password_problem("x" * 11, "alice"))
        self.assertIn("256", passwords.password_problem("x" * 257, "alice"))
        self.assertIn("username", passwords.password_problem("Alice.Builder", "alice.builder"))

    def test_generated_passwords(self):
        generated = {passwords.generate_password() for _ in range(50)}
        self.assertEqual(len(generated), 50)
        self.assertTrue(all(len(p) >= 20 and passwords.password_problem(p, "alice") is None for p in generated))


class SettingTests(unittest.TestCase):
    def test_cookie_secure_accepts_only_true_or_false(self):
        self.assertTrue(config.Settings(auth_cookie_secure="true").auth_cookie_secure)
        self.assertFalse(config.Settings(auth_cookie_secure="false").auth_cookie_secure)
        for bad in ("1", "yes", "on", "True", ""):
            with self.subTest(value=bad), self.assertRaises(ValidationError):
                config.Settings(auth_cookie_secure=bad)
        with patch.dict(os.environ, {"AUTH_COOKIE_SECURE": "yes"}), self.assertRaises(ValidationError):
            config.Settings()


class StoreTests(StoreTestCase):
    def test_file_and_folder_are_private(self):
        nested = Path(self.dir) / "nested"
        with patch.object(settings, "auth_dir", str(nested)):
            self.assertFalse(store.is_set_up())
            self.assertEqual(nested.stat().st_mode & 0o777, 0o700)
            self.assertEqual((nested / "auth.db").stat().st_mode & 0o777, 0o600)

    def test_bootstrap_only_once_and_create_only_after(self):
        with self.assertRaises(store.Refused):
            store.create_user("dev", "developer", "x" * 12)
        store.create_user("eng", "engineer", "x" * 12, bootstrap=True)
        with self.assertRaises(store.Refused):
            store.create_user("eng2", "engineer", "x" * 12, bootstrap=True)
        with self.assertRaises(store.Refused):
            store.create_user("eng", "developer", "x" * 12)
        self.assertTrue(store.get_user("eng").must_change_password)

    def test_only_hashes_and_token_hashes_are_stored(self):
        password = self.make("eng", "engineer")
        token = store.create_session(store.get_user("eng").id)
        raw = Path(self.dir, "auth.db").read_bytes()
        self.assertNotIn(password.encode(), raw)
        self.assertNotIn(token.encode(), raw)
        rows = sqlite3.connect(Path(self.dir, "auth.db")).execute("SELECT token_hash FROM sessions").fetchall()
        self.assertEqual(rows, [(hashlib.sha256(token.encode()).hexdigest(),)])

    def test_idle_and_absolute_expiry(self):
        self.make("eng", "engineer")
        user = store.get_user("eng")
        start = 1000.0
        with patch.object(store, "_now", return_value=start):
            idle = store.create_session(user.id)
            busy = store.create_session(user.id)
        # Used every 30 minutes, a session lasts until the absolute limit and no longer.
        at = start
        while at + 1800 < start + store.ABSOLUTE_S:
            at += 1800
            with patch.object(store, "_now", return_value=at):
                self.assertIsNotNone(store.authenticate(busy), f"ended after {(at - start) / 3600} h")
        with patch.object(store, "_now", return_value=start + store.ABSOLUTE_S + 1):
            self.assertIsNone(store.authenticate(busy))
        # Unused, it lasts 12 hours.
        with patch.object(store, "_now", return_value=start + store.IDLE_S - 1):
            self.assertIsNotNone(store.authenticate(idle))
        with patch.object(store, "_now", return_value=start + store.IDLE_S - 1 + store.IDLE_S + 1):
            self.assertIsNone(store.authenticate(idle))
        with patch.object(store, "_now", return_value=start + 1):
            self.assertIsNone(store.authenticate(idle), "an ended session is deleted, not revived")

    def test_revalidation_of_role_and_disabled_state(self):
        self.make("eng", "engineer")
        self.make("dev", "developer")
        token = store.create_session(store.get_user("dev").id)
        store.set_role("dev", "end_user")
        self.assertEqual(store.authenticate(token).role, "end_user")
        store.set_enabled("dev", False)
        self.assertIsNone(store.authenticate(token))
        store.set_enabled("dev", True)
        self.assertIsNone(store.authenticate(token), "disabling deleted the session")

    def test_reset_ends_sessions_and_forces_a_change(self):
        self.make("eng", "engineer")
        self.make("dev", "developer")
        token = store.create_session(store.get_user("dev").id)
        store.reset_password("dev", "n" * 20)
        self.assertIsNone(store.authenticate(token))
        self.assertTrue(store.get_user("dev").must_change_password)

    def test_last_enabled_engineer_is_protected(self):
        self.make("eng", "engineer")
        for action in (lambda: store.set_enabled("eng", False), lambda: store.set_role("eng", "developer")):
            with self.assertRaises(store.Refused):
                action()
        self.make("eng2", "engineer")
        store.set_enabled("eng2", False)
        with self.assertRaises(store.Refused, msg="a disabled engineer doesn't count"):
            store.set_role("eng", "end_user")
        store.set_enabled("eng2", True)
        store.set_role("eng", "developer")
        with self.assertRaises(store.Refused):
            store.set_enabled("eng2", False)

    def test_throttle_locks_a_pair_and_survives_a_new_handle(self):
        for attempt in range(4):
            self.assertFalse(store.record_failure("alice", "10.0.0.1"))
        self.assertTrue(store.record_failure("alice", "10.0.0.1"))
        store._ready.clear()                            # as after an API restart
        self.assertTrue(store.locked("alice", "10.0.0.1"))
        self.assertFalse(store.locked("alice", "10.0.0.2"))
        self.assertFalse(store.locked("bob", "10.0.0.1"))
        with patch.object(store, "_now", return_value=store._now() + store.LOCKOUT_S + 1):
            self.assertFalse(store.locked("alice", "10.0.0.1"))

    def test_failures_outside_the_window_do_not_count(self):
        start = 5000.0
        for minute in (0, 4, 8, 12):
            with patch.object(store, "_now", return_value=start + minute * 60):
                store.record_failure("alice", "a")
        with patch.object(store, "_now", return_value=start + 16 * 60):
            self.assertFalse(store.record_failure("alice", "a"), "the first failure left the 15-minute window")
        store.clear_failures("alice", "a")
        for _ in range(4):
            store.record_failure("alice", "a")
        self.assertFalse(store.locked("alice", "a"))


class PolicyTests(unittest.TestCase):
    def app_routes(self):
        keys = set()
        for path, item in app.openapi()["paths"].items():
            keys |= {(method.upper(), path) for method in item}
        for route in app.routes:      # FastAPI's own docs routes aren't in the schema
            if getattr(route, "include_in_schema", True) is False or type(route).__name__ == "Route":
                keys |= {(m, route.path) for m in route.methods if m != "HEAD"}
        return keys

    def test_every_route_has_exactly_one_entry_and_every_entry_a_route(self):
        routes = self.app_routes()
        self.assertEqual(sorted(routes - set(policy.ACCESS)), [], "routes with no policy entry")
        self.assertEqual(sorted(set(policy.ACCESS) - routes), [], "policy entries with no route")

    def test_the_matrix_is_the_agreed_one(self):
        counts = {}
        for access in policy.ACCESS.values():
            counts[access] = counts.get(access, 0) + 1
        self.assertEqual(counts, {"public": 3, "session": 3, "end_user": 3, "developer": 19, "engineer": 11})
        self.assertEqual(policy.ACCESS[("GET", "/health")], "public")
        self.assertEqual(policy.ACCESS[("DELETE", "/collections/{name}")], "engineer")
        self.assertEqual(policy.ACCESS[("POST", "/collections")], "developer")
        self.assertEqual(policy.ACCESS[("GET", "/tune/{collection}")], "developer")
        self.assertEqual(policy.ACCESS[("GET", "/tune/job/{job_id}")], "engineer")

    def test_each_template_resolves_to_itself(self):
        for method, template in policy.ACCESS:
            sample = template.replace("{", "x").replace("}", "")
            with self.subTest(route=f"{method} {template}"):
                self.assertEqual(policy.match(method, sample), template)
        self.assertEqual(policy.match("GET", "/tune/job/abc"), "/tune/job/{job_id}")
        self.assertEqual(policy.match("GET", "/tune/abc"), "/tune/{collection}")
        self.assertEqual(policy.match("HEAD", "/collections"), "/collections")
        self.assertIsNone(policy.match("PUT", "/collections"))
        self.assertIsNone(policy.match("GET", "/collections/a/b"))

    def test_an_unlisted_route_is_refused_for_everyone(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.object(settings, "auth_dir", temp.name):
            store.create_user("eng", "engineer", "x" * 12, bootstrap=True, must_change_password=False)
            token = store.create_session(store.get_user("eng").id)

            @app.get("/vfy-unlisted")
            async def unlisted():
                return {"reached": True}
            self.addCleanup(lambda: app.router.routes.pop())
            client = TestClient(app, headers={"Cookie": f"{store.COOKIE}={token}", "Origin": ORIGIN})
            for headers in ({}, {"Cookie": ""}):
                response = client.get("/vfy-unlisted", headers=headers)
                self.assertEqual(response.status_code, 403, response.text)
                self.assertEqual(response.json()["error"]["code"], "FORBIDDEN")


class OriginTests(unittest.TestCase):
    def scope(self, **headers):
        return {"headers": [(k.replace("_", "-").encode(), v.encode()) for k, v in headers.items()]}

    def test_same_origin_rules(self):
        allowed = (dict(host="localhost:8080", origin="http://localhost:8080"),
                   dict(host="localhost", origin="http://localhost:80"),
                   dict(host="example.test", origin="https://example.test:443"),
                   dict(host="localhost:8080", sec_fetch_site="same-origin"))
        refused = (dict(host="localhost:8080"),
                   dict(host="localhost:8080", origin="http://localhost:8081"),
                   dict(host="localhost:8080", origin="http://evil.test:8080"),
                   dict(host="localhost:8080", origin="null"),
                   dict(host="localhost:8080", origin="file://"),
                   dict(origin="http://localhost:8080"),
                   dict(host="localhost:8080", sec_fetch_site="same-site"),
                   dict(host="localhost:8080", sec_fetch_site="cross-site"))
        for headers in allowed:
            with self.subTest(allowed=headers):
                self.assertTrue(same_origin(self.scope(**headers)))
        for headers in refused:
            with self.subTest(refused=headers):
                self.assertFalse(same_origin(self.scope(**headers)))


class MiddlewareTests(StoreTestCase):
    def test_setup_required_before_bootstrap(self):
        for method, path in (("GET", "/collections"), ("GET", "/auth/me"), ("POST", "/auth/login")):
            response = self.client.request(method, path, json={"username": "a", "password": "b"} if method == "POST" else None)
            self.assertEqual(response.status_code, 503, path)
            self.assertEqual(response.json()["error"]["code"], "SETUP_REQUIRED")
        self.assertEqual(self.client.get("/auth/status").json(), {"setup_required": True})
        self.make("eng", "engineer")
        self.assertEqual(self.client.get("/auth/status").json(), {"setup_required": False})

    def test_role_matrix_through_the_app(self):
        self.setup_roles()
        need = {("GET", "/packages"): "developer", ("GET", "/metrics/latency"): "engineer",
                ("GET", "/goldstandard/diagnostics"): "engineer", ("GET", "/openapi.json"): "engineer"}
        names = {"engineer": "eng", "developer": "dev", "end_user": "user"}
        for role, name in names.items():
            headers, _ = self.session(name)
            for (method, path), required in need.items():
                with self.subTest(role=role, route=path):
                    response = self.client.request(method, path, headers=headers)
                    if policy.rank(role) < policy.rank(required):
                        self.assertEqual(response.status_code, 403)
                        self.assertEqual(response.json()["error"],
                                         {"code": "FORBIDDEN", "message": "Your role can't do this.",
                                          "detail": {"required_role": required}})
                    else:
                        self.assertNotIn(response.status_code, (401, 403), response.text[:200])

    def test_refusal_order(self):
        self.setup_roles()
        headers, _ = self.session("user")
        # Cross-origin wins over the role: an end user's cross-origin delete.
        response = self.client.delete("/collections/X?confirm=true",
                                      headers={**headers, "Origin": "http://evil.test"})
        self.assertEqual(response.json()["error"]["code"], "CROSS_ORIGIN_REFUSED")
        # A due password change wins over the role.
        store.reset_password("user", "n" * 20)
        user = store.get_user("user")
        token = store.create_session(user.id)
        response = self.client.delete("/collections/X?confirm=true",
                                      headers={"Cookie": f"{store.COOKIE}={token}", "Origin": ORIGIN})
        self.assertEqual(response.json()["error"]["code"], "PASSWORD_CHANGE_REQUIRED")
        self.assertEqual(self.client.get("/auth/me", headers={"Cookie": f"{store.COOKIE}={token}"}).status_code, 200)
        # No session at all: 401, whatever the origin.
        response = self.client.delete("/collections/X?confirm=true", headers={"Origin": "http://evil.test"})
        self.assertEqual(response.json()["error"]["code"], "AUTH_REQUIRED")

    def test_a_refused_upload_never_reads_its_body(self):
        self.make("eng", "engineer")
        reads = []

        async def receive():
            reads.append(1)
            return {"type": "http.request", "body": b"x" * 1024, "more_body": False}

        sent = []

        async def send(message):
            sent.append(message)

        scope = {"type": "http", "method": "POST", "path": "/ingest/upload", "raw_path": b"/ingest/upload",
                 "root_path": "", "query_string": b"", "scheme": "http", "server": ("testserver", 80),
                 "headers": [(b"host", b"testserver"), (b"content-type", b"multipart/form-data; boundary=x")]}

        async def never(scope, receive, send):
            raise AssertionError("the app was called")

        asyncio.run(AuthMiddleware(never)(scope, receive, send))
        self.assertEqual(reads, [])
        self.assertEqual(sent[0]["status"], 401)

    def test_a_refused_request_keeps_its_route_label(self):
        self.make("eng", "engineer")
        scope = {"type": "http", "method": "DELETE", "path": "/collections/Abc", "headers": []}

        async def send(message):
            pass

        asyncio.run(AuthMiddleware(None)(scope, None, send))
        self.assertEqual(scope["route"].path, "/collections/{name}")

    def test_no_cors_headers(self):
        self.setup_roles()
        headers, _ = self.session("eng")
        response = self.client.get("/collections", headers={**headers, "Origin": "http://evil.test"})
        self.assertFalse([k for k in response.headers if k.lower().startswith("access-control-")])
        response = self.client.options("/collections", headers={"Origin": "http://evil.test",
                                                                "Access-Control-Request-Method": "DELETE"})
        self.assertFalse([k for k in response.headers if k.lower().startswith("access-control-")])


class RouteTests(StoreTestCase):
    def test_login_success_cookie_and_me(self):
        password = self.make("eng", "engineer", must_change=True)
        with self.assertLogs("auth", "WARNING") as logs:
            response = self.login("ENG", password)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"username": "eng", "role": "engineer", "must_change_password": True})
        cookie = response.headers["set-cookie"]
        attributes = [a.strip().lower() for a in cookie.split(";")[1:]]
        for want in ("httponly", "samesite=strict", "path=/", f"max-age={store.ABSOLUTE_S}"):
            self.assertIn(want, attributes)
        self.assertNotIn("secure", attributes)
        self.assertIn("Login succeeded for 'eng'", logged(logs))
        self.assertNotIn(password, logged(logs))
        token = cookie.split(";")[0].split("=", 1)[1]
        self.assertNotIn(token, logged(logs))
        me = self.client.get("/auth/me", headers={"Cookie": f"{store.COOKIE}={token}"})
        self.assertEqual(me.json()["username"], "eng")

    def test_secure_cookie_under_the_setting(self):
        password = self.make("eng", "engineer")
        with patch.object(settings, "auth_cookie_secure", True):
            response = self.login("eng", password)
        self.assertIn("secure", [a.strip().lower() for a in response.headers["set-cookie"].split(";")])

    def test_failures_are_generic(self):
        password = self.make("eng", "engineer")
        self.make("off", "developer")
        store.set_enabled("off", False)
        off_password = passwords.generate_password()
        store.reset_password("off", off_password)
        answers = [self.login("eng", password + "x"), self.login("nobody", password),
                   self.login("x", password), self.login("off", off_password)]
        bodies = {(r.status_code, r.text) for r in answers}
        self.assertEqual(len(bodies), 1, bodies)
        self.assertEqual(answers[0].status_code, 401)
        self.assertEqual(answers[0].json()["error"]["code"], "LOGIN_FAILED")
        self.assertNotIn("set-cookie", answers[0].headers)

    def test_unknown_user_is_checked_against_a_hash_too(self):
        self.make("eng", "engineer")
        with patch.object(passwords, "verify_password", wraps=passwords.verify_password) as verify:
            self.login("nobody", "x" * 12)
        verify.assert_called_once()
        self.assertEqual(verify.call_args.args[1], passwords.dummy_hash())

    def test_throttle(self):
        password = self.make("eng", "engineer")
        with self.assertLogs("auth", "WARNING") as logs:
            for _ in range(5):
                self.assertEqual(self.login("eng", "wrong" * 3).status_code, 401)
            response = self.login("eng", password)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(response.json()["error"]["code"], "LOGIN_THROTTLED")
        self.assertIn("too many failed attempts", logged(logs))
        # Another address isn't locked; the proxy's X-Real-IP is the address.
        self.assertEqual(self.login("eng", password, **{"X-Real-IP": "10.9.9.9"}).status_code, 200)
        # The same for a user that doesn't exist: throttling reveals nothing.
        for _ in range(5):
            self.login("ghost", "wrong" * 3)
        self.assertEqual(self.login("ghost", "wrong" * 3).status_code, 429)

    def test_success_clears_the_count(self):
        password = self.make("eng", "engineer")
        for _ in range(4):
            self.login("eng", "wrong" * 3)
        self.assertEqual(self.login("eng", password).status_code, 200)
        for _ in range(4):
            self.login("eng", "wrong" * 3)
        self.assertEqual(self.login("eng", password).status_code, 200)

    def test_logout(self):
        self.setup_roles()
        headers, token = self.session("dev")
        response = self.client.post("/auth/logout", headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn("max-age=0", response.headers["set-cookie"].lower())
        self.assertEqual(self.client.get("/auth/me", headers=headers).status_code, 401)
        self.assertEqual(self.client.post("/auth/logout", headers=headers).status_code, 401)

    def test_logout_is_origin_checked(self):
        self.setup_roles()
        headers, token = self.session("dev")
        response = self.client.post("/auth/logout", headers={**headers, "Origin": "http://evil.test"})
        self.assertEqual(response.json()["error"]["code"], "CROSS_ORIGIN_REFUSED")
        self.assertIsNotNone(store.authenticate(token))

    def test_password_change(self):
        self.setup_roles()
        password = self.passwords["end_user"]
        headers, token = self.session("user")
        other_headers, other = self.session("user")
        wrong = self.client.post("/auth/password", headers=headers,
                                 json={"current_password": "nope" * 3, "new_password": "n" * 14})
        self.assertEqual((wrong.status_code, wrong.json()["error"]["code"]), (403, "PASSWORD_INCORRECT"))
        for new in ("short", "u" * 257):
            response = self.client.post("/auth/password", headers=headers,
                                        json={"current_password": password, "new_password": new})
            self.assertEqual((response.status_code, response.json()["error"]["code"]), (422, "INVALID_PASSWORD"))
        response = self.client.post("/auth/password", headers=headers,
                                    json={"current_password": password, "new_password": "a-new-password"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNotNone(store.authenticate(token))
        self.assertIsNone(store.authenticate(other))
        self.assertEqual(self.login("user", "a-new-password").status_code, 200)
        self.assertEqual(self.login("user", password).status_code, 401)

    def test_password_equal_to_username_is_refused(self):
        self.make("eng", "engineer")
        self.make("long-username", "developer")
        store.reset_password("long-username", "temporary-pass-1")
        headers, _ = self.session("long-username")
        response = self.client.post("/auth/password", headers=headers,
                                    json={"current_password": "temporary-pass-1", "new_password": "LONG-USERNAME"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("username", response.json()["error"]["message"])

    def test_forced_change_allows_only_session_routes(self):
        self.make("eng", "engineer")
        generated = passwords.generate_password()
        store.create_user("dev", "developer", generated)
        response = self.login("dev", generated)
        self.assertTrue(response.json()["must_change_password"])
        token = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        headers = {"Cookie": f"{store.COOKIE}={token}", "Origin": ORIGIN}
        self.assertEqual(self.client.get("/collections", headers=headers).json()["error"]["code"],
                         "PASSWORD_CHANGE_REQUIRED")
        self.assertEqual(self.client.get("/auth/me", headers=headers).status_code, 200)
        changed = self.client.post("/auth/password", headers=headers,
                                   json={"current_password": generated, "new_password": "chosen-by-dev"})
        self.assertEqual(changed.json()["must_change_password"], False)
        self.assertNotEqual(self.client.get("/collections", headers=headers).status_code, 403)


class CommandTests(unittest.TestCase):
    """`python -m auth` in a subprocess, as an operator runs it."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.env = {**os.environ, "AUTH_DIR": temp.name}
        self.dir = temp.name

    def run_auth(self, *args):
        return subprocess.run([sys.executable, "-m", "auth", *args], cwd=API_DIR, env=self.env,
                              capture_output=True, text=True, timeout=120)

    def users(self):
        with patch.object(settings, "auth_dir", self.dir):
            store._ready.discard(str(store.db_path()))
            return {u.username: u for u in store.list_users()}

    def test_bootstrap_create_and_reset_print_only_the_password(self):
        result = self.run_auth("bootstrap", "Admin")
        self.assertEqual(result.returncode, 0, result.stderr)
        password = result.stdout.strip()
        self.assertEqual(result.stdout, password + "\n")
        self.assertGreaterEqual(len(password), 20)
        self.assertEqual(len(result.stderr.strip().splitlines()), 1)
        self.assertNotIn(password, result.stderr)
        admin = self.users()["admin"]
        self.assertEqual((admin.role, admin.must_change_password), ("engineer", True))
        self.assertTrue(passwords.verify_password(password, admin.password_hash))

        refused = self.run_auth("bootstrap", "second")
        self.assertEqual((refused.returncode, refused.stdout), (1, ""))
        self.assertIn("already set up", refused.stderr)

        created = self.run_auth("create-user", "dev", "developer")
        self.assertEqual(created.returncode, 0, created.stderr)
        self.assertTrue(self.users()["dev"].must_change_password)
        reset = self.run_auth("reset-password", "dev")
        self.assertEqual(reset.returncode, 0)
        self.assertNotEqual(reset.stdout, created.stdout)
        self.assertTrue(passwords.verify_password(reset.stdout.strip(), self.users()["dev"].password_hash))

    def test_create_before_bootstrap_is_refused(self):
        result = self.run_auth("create-user", "dev", "developer")
        self.assertEqual((result.returncode, result.stdout), (1, ""))
        self.assertIn("bootstrap", result.stderr)

    def test_usage_errors(self):
        for args in (("create-user", "x", "developer"), ("create-user", "dev", "admin"), ("frobnicate",), ()):
            with self.subTest(args=args):
                result = self.run_auth(*args)
                self.assertEqual((result.returncode, result.stdout), (2, ""))

    def test_last_engineer_and_recovery(self):
        self.run_auth("bootstrap", "admin")
        for args in (("disable", "admin"), ("set-role", "admin", "developer")):
            result = self.run_auth(*args)
            self.assertEqual(result.returncode, 1, args)
            self.assertIn("last enabled engineer", result.stderr)
        self.run_auth("create-user", "eng2", "engineer")
        self.assertEqual(self.run_auth("disable", "admin").returncode, 0)
        self.assertFalse(self.users()["admin"].enabled)
        self.assertEqual(self.run_auth("enable", "admin").returncode, 0)
        self.assertEqual(self.run_auth("set-role", "eng2", "end_user").returncode, 0)
        self.assertEqual(self.users()["eng2"].role, "end_user")
        self.assertEqual(self.run_auth("disable", "nobody").returncode, 1)

    def test_list_shows_no_secret(self):
        password = self.run_auth("bootstrap", "admin").stdout.strip()
        result = self.run_auth("list")
        self.assertEqual(result.returncode, 0)
        self.assertIn("admin\tengineer\tenabled\tpassword change due\t", result.stdout)
        self.assertNotIn(password, result.stdout)
        self.assertNotIn("scrypt", result.stdout)


class TestingHelperTests(unittest.TestCase):
    def test_headers_are_a_real_session(self):
        from auth import testing
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        with patch.object(testing, "_folder", temp.name), patch.object(testing, "_accounts", {}), \
                patch.object(settings, "auth_dir", settings.auth_dir):
            headers = testing.headers("developer", base_url="http://owned/")
            self.assertEqual(headers["Origin"], "http://owned")
            token = headers["Cookie"].split("=", 1)[1]
            self.assertEqual(store.authenticate(token).role, "developer")


class ImplementationCopyTests(unittest.TestCase):
    def test_embedded_copies_match(self):
        root = Path(API_DIR).parent
        document = (root / "IMPLEMENTATION.md").read_text()
        for name in ("api/auth/__init__.py", "api/auth/passwords.py", "api/auth/store.py", "api/auth/policy.py",
                     "api/auth/middleware.py", "api/auth/__main__.py", "api/auth/testing.py",
                     "api/routers/auth.py", "api/main.py", "api/config.py", "proxy/nginx.conf",
                     "scripts/verify/17_auth.sh", "scripts/verify/auth_setup.py", "scripts/verify/auth_stack.py",
                     "scripts/verify/auth_headers.py"):
            with self.subTest(name=name):
                header = "### " + name + "\n\n```"
                start = document.index("\n", document.index(header) + len(header)) + 1
                end = document.index("\n```\n", start)
                self.assertEqual(document[start:end], (root / name).read_text().rstrip("\n"))


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
