"""Host-only checks that the verify harness keeps its test credentials private (#25).

auth_setup.py keeps the test accounts' passwords in a 0600 file in a 0700
folder of this user's own, refuses a folder someone could have planted, and
never runs the operator command without --operator. auth_headers.py reads the
session from curl's cookie jar. lib.sh passes --operator only on the verify
project, and every curl a suite makes carries the session. Standard library
only; no Docker or stack needed.

Run: python3 scripts/tests/test_verify_auth.py
"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
VERIFY = ROOT / "scripts" / "verify"
sys.path.insert(0, str(VERIFY))

import auth_headers  # noqa: E402
import auth_setup    # noqa: E402


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)

    def use(self, path):
        return patch.dict(os.environ, {"RAG_AUTH_DIR": str(path)})

    def test_folder_is_created_private_and_credentials_file_0600(self):
        folder = self.base / "auth"
        with self.use(folder):
            made = auth_setup.private_dir()
            auth_setup.write_credentials(made, {"engineer": ("vfy-engineer", "p" * 20)})
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((folder / "credentials").stat().st_mode), 0o600)
        self.assertEqual(auth_setup.read_credentials(folder), {"engineer": ("vfy-engineer", "p" * 20)})
        self.assertEqual([p.name for p in folder.iterdir()], ["credentials"], "no temporary file left behind")

    def test_a_loose_folder_is_tightened(self):
        folder = self.base / "auth"
        folder.mkdir(mode=0o755)
        with self.use(folder):
            auth_setup.private_dir()
        self.assertEqual(stat.S_IMODE(folder.stat().st_mode), 0o700)

    def test_a_symlink_or_file_is_refused(self):
        target = self.base / "elsewhere"
        target.mkdir()
        link = self.base / "auth"
        link.symlink_to(target)
        with self.use(link), self.assertRaises(auth_setup.SetupError):
            auth_setup.private_dir()
        plain = self.base / "plain"
        plain.write_text("")
        with self.use(plain), self.assertRaises(auth_setup.SetupError):
            auth_setup.private_dir()

    def test_a_symlinked_credentials_file_is_refused(self):
        folder = self.base / "auth"
        folder.mkdir(mode=0o700)
        (folder / "credentials").symlink_to(self.base / "planted")
        (self.base / "planted").write_text("engineer vfy-engineer planted\n")
        with self.assertRaises(auth_setup.SetupError):
            auth_setup.read_credentials(folder)

    def test_default_folder_is_outside_the_checkout(self):
        with patch.dict(os.environ, {"TMPDIR": "/tmp/x/"}, clear=False):
            os.environ.pop("RAG_AUTH_DIR", None)
            self.assertEqual(auth_setup.auth_dir(), Path("/tmp/x/rag-verify-auth-{}".format(os.getuid())))

    def test_jar_is_0600_and_readable_by_auth_headers(self):
        jar = self.base / "engineer.jar"
        auth_setup.write_jar(jar, "tok-123", api="http://localhost:8081/api")
        self.assertEqual(stat.S_IMODE(jar.stat().st_mode), 0o600)
        self.assertEqual(auth_headers.session_token(str(jar)), "tok-123")
        with patch.dict(os.environ, {"RAG_AUTH_JAR": str(jar), "RAG_AUTH_ORIGIN": "http://localhost:8081"}):
            self.assertEqual(auth_headers.headers(),
                             {"Cookie": "rag_session=tok-123", "Origin": "http://localhost:8081"})

    def test_auth_headers_reads_curls_own_jar_format(self):
        jar = self.base / "curl.jar"
        jar.write_text("# Netscape HTTP Cookie File\n# https://curl.se/docs/http-cookies.html\n\n"
                       "#HttpOnly_localhost\tFALSE\t/\tFALSE\t1893456000\trag_session\tabc\n"
                       "localhost\tFALSE\t/\tFALSE\t0\tother\tzzz\n")
        self.assertEqual(auth_headers.session_token(str(jar)), "abc")
        self.assertIsNone(auth_headers.session_token(str(self.base / "missing")))


class OperatorTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {"RAG_AUTH_DIR": temp.name + "/auth"})
        env.start()
        self.addCleanup(env.stop)

    def test_without_operator_nothing_is_reset_or_bootstrapped(self):
        with patch.object(auth_setup, "operator") as operator, \
                patch.object(auth_setup, "request", return_value=(200, {"setup_required": True}, None)):
            with self.assertRaises(auth_setup.SetupError):
                auth_setup.ensure(False)
        operator.assert_not_called()
        with patch.object(auth_setup, "operator") as operator, \
                patch.object(auth_setup, "request", side_effect=[(200, {"setup_required": False}, None),
                                                                 (401, None, None)]):
            with self.assertRaises(auth_setup.SetupError):
                auth_setup.ensure(False)
        operator.assert_not_called()

    def test_a_generated_password_is_replaced_and_stored_never_printed(self):
        calls = []

        def operator(*args):
            calls.append(args)
            return 0, "generated-" + args[1]

        def request(method, path, body=None, token=None, api=None):
            if path == "/auth/status":
                return 200, {"setup_required": True}, None
            if path == "/auth/login":
                return 200, {"must_change_password": body["password"].startswith("generated-")}, "tok"
            return 200, {}, None

        with patch.object(auth_setup, "operator", side_effect=operator), \
                patch.object(auth_setup, "request", side_effect=request), \
                patch("sys.stdout") as out:
            auth_setup.ensure(True)
        self.assertEqual(calls[0], ("bootstrap", "vfy-engineer"))
        stored = auth_setup.read_credentials(auth_setup.private_dir())
        self.assertEqual(set(stored), {"engineer", "developer", "end_user"})
        self.assertTrue(all(not p.startswith("generated-") for _, p in stored.values()))
        self.assertFalse(out.write.called, "ensure printed something")


class PasswordPipeTests(unittest.TestCase):
    def test_the_password_goes_only_into_a_pipe(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        folder = Path(temp.name) / "auth"
        env = {**os.environ, "RAG_AUTH_DIR": str(folder)}
        with patch.dict(os.environ, {"RAG_AUTH_DIR": str(folder)}):
            auth_setup.write_credentials(auth_setup.private_dir(), {"end_user": ("vfy-end-user", "s3cret pass word")})
        piped = subprocess.run([sys.executable, str(VERIFY / "auth_setup.py"), "password", "end_user"],
                               capture_output=True, text=True, env=env)
        self.assertEqual((piped.returncode, piped.stdout, piped.stderr), (0, "s3cret pass word\n", ""))
        with patch.dict(os.environ, {"RAG_AUTH_DIR": str(folder)}), patch.object(auth_setup.os, "isatty", return_value=True), \
                self.assertRaises(auth_setup.SetupError):
            auth_setup.password_to_pipe("end_user")


class LibraryTests(unittest.TestCase):
    def test_operator_only_on_the_verify_project_and_curl_carries_the_session(self):
        text = (VERIFY / "lib.sh").read_text()
        ensure = text[text.index("auth_ensure() {"):]
        ensure = ensure[:ensure.index("\n}\n")]
        self.assertIn('[ -n "$(volume_removal_refusal_reason)" ] || operator=--operator', ensure)
        function = text[text.index("curl() {"):]
        function = function[:function.index("\n}\n")]
        self.assertIn('command curl -b "$RAG_AUTH_JAR" -H "Origin: $RAG_AUTH_ORIGIN" "$@"', function)
        self.assertIn("auth_ensure", text[text.index("require_stack() {"):text.index("# ── test accounts")])

    def test_no_password_on_a_command_line(self):
        for name in ("lib.sh", "17_auth.sh", "06_ui.sh"):
            text = (VERIFY / name).read_text()
            with self.subTest(file=name):
                self.assertNotIn("password", " ".join(line for line in text.splitlines()
                                                      if "curl" in line and not line.lstrip().startswith("#")))

    def test_the_suites_pass_the_shell_check(self):
        for name in ("lib.sh", "17_auth.sh", "06_ui.sh", "all.sh"):
            with self.subTest(file=name):
                self.assertEqual(subprocess.run(["bash", "-n", str(VERIFY / name)]).returncode, 0)


class LoginPacingTests(unittest.TestCase):
    """#577: nginx refuses logins beyond 10 a minute per address; the harness
    waits for a slot instead of failing, and only for that refusal."""

    LIMITED = (b'{"error":{"code":"LOGIN_RATE_LIMITED","message":"Too many login attempts. Wait a minute and try '
               b'again.","detail":null}}')

    def answer(self, status, body):
        import email.message
        import io
        import urllib.error
        headers = email.message.Message()
        if status == 200:
            headers["Set-Cookie"] = auth_setup.COOKIE + "=tok; Path=/"
            response = unittest.mock.MagicMock(status=200, headers=headers)
            response.read.return_value = body
            response.__enter__.return_value = response
            return response
        return urllib.error.HTTPError("http://x/auth/login", status, "refused", headers, io.BytesIO(body))

    def send(self, answers, path="/auth/login"):
        with patch.object(auth_setup, "api_base", return_value="http://127.0.0.1:8081/api"), \
                patch.object(auth_setup.urllib.request, "urlopen", side_effect=answers) as urlopen, \
                patch.object(auth_setup.time, "sleep") as sleep:
            result = auth_setup.request("POST", path, {"username": "u", "password": "p"})
        return result, urlopen.call_count, sleep.call_count

    def test_a_rate_limited_login_is_sent_again(self):
        answers = [self.answer(429, self.LIMITED), self.answer(429, self.LIMITED), self.answer(200, b'{"ok":1}')]
        (status, body, token), calls, sleeps = self.send(answers)
        self.assertEqual((status, body, token, calls, sleeps), (200, {"ok": 1}, "tok", 3, 2))

    def test_the_apis_own_throttle_and_other_paths_are_returned_at_once(self):
        throttled = b'{"error":{"code":"LOGIN_THROTTLED","message":"wait","detail":null}}'
        (status, body, _), calls, sleeps = self.send([self.answer(429, throttled)])
        self.assertEqual((status, body["error"]["code"], calls, sleeps), (429, "LOGIN_THROTTLED", 1, 0))
        (status, _, _), calls, sleeps = self.send([self.answer(429, self.LIMITED)], path="/auth/password")
        self.assertEqual((status, calls, sleeps), (429, 1, 0))

    def test_it_gives_up_after_the_wait(self):
        with patch.object(auth_setup, "LOGIN_WAIT_S", 0):
            (status, body, _), calls, sleeps = self.send([self.answer(429, self.LIMITED)])
        self.assertEqual((status, body["error"]["code"], calls, sleeps), (429, "LOGIN_RATE_LIMITED", 1, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
