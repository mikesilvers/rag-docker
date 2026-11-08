"""LAN mode (issue #26): the RAG_LAN_MODE setting, the API's startup refusal
(api/lan_mode.py), the certificate step (api/tls_cert.py), LAN mode's nginx
configuration and the embedded copies of the new files.

Runs with the API's dependencies, in the built API image with no network
(`18_lan.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_lan_mode.py

Stores and certificate folders are temporary; nothing is started or published.
"""
import ast
import datetime
import io
import ipaddress
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

API_DIR = os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api")
sys.path.insert(0, API_DIR)
ROOT = Path(API_DIR).parent

from cryptography import x509  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import config  # noqa: E402
import lan_mode  # noqa: E402
import tls_cert  # noqa: E402
from auth import passwords, store  # noqa: E402
from config import settings  # noqa: E402

NOW = datetime.datetime(2026, 10, 28, 12, 0, tzinfo=datetime.timezone.utc)


class SettingTests(unittest.TestCase):
    def test_exactly_true_or_false(self):
        self.assertTrue(config.Settings(rag_lan_mode="true").rag_lan_mode)
        self.assertFalse(config.Settings(rag_lan_mode="false").rag_lan_mode)
        self.assertFalse(config.Settings().rag_lan_mode, "LAN mode must be off by default")
        for bad in ("1", "yes", "on", "True", "TRUE", ""):
            with self.subTest(value=bad):
                with self.assertRaises(ValidationError) as caught:
                    config.Settings(rag_lan_mode=bad)
                self.assertIn("RAG_LAN_MODE must be exactly true or false", str(caught.exception))

    def test_the_cookie_setting_keeps_its_own_name_in_the_message(self):
        with self.assertRaises(ValidationError) as caught:
            config.Settings(auth_cookie_secure="yes")
        self.assertIn("AUTH_COOKIE_SECURE must be exactly true or false", str(caught.exception))


class RefusalTests(unittest.TestCase):
    """S2.2: with LAN mode on, a Secure cookie (checked first) and an enabled human Engineer."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for name, value in (("auth_dir", temp.name), ("rag_lan_mode", True), ("auth_cookie_secure", True)):
            target = patch.object(settings, name, value)
            target.start()
            self.addCleanup(target.stop)

    def make(self, username, role):
        store.create_user(username, role, passwords.generate_password(), bootstrap=not store.is_set_up())

    def test_off_never_refuses(self):
        with patch.object(settings, "rag_lan_mode", False), patch.object(settings, "auth_cookie_secure", False):
            self.assertIsNone(lan_mode.refusal())

    def test_an_enabled_human_engineer_and_a_secure_cookie_pass(self):
        self.make("eng", "engineer")
        self.assertIsNone(lan_mode.refusal())

    def test_the_cookie_must_be_secure(self):
        self.make("eng", "engineer")
        with patch.object(settings, "auth_cookie_secure", False):
            line = lan_mode.refusal()
        self.assertTrue(line.startswith("LAN mode refused: AUTH_COOKIE_SECURE must be true"), line)

    def test_the_cookie_is_checked_first(self):
        with patch.object(settings, "auth_cookie_secure", False):
            self.assertIn("AUTH_COOKIE_SECURE", lan_mode.refusal())

    def test_who_counts_as_an_engineer(self):
        engineer_line = "LAN mode refused: there is no enabled AI Engineer account"
        self.assertTrue(lan_mode.refusal().startswith(engineer_line), "an empty store")
        self.make("user", "end_user")
        self.make("dev", "developer")
        self.assertTrue(lan_mode.refusal().startswith(engineer_line), "only an End User and a Developer")
        store.create_principal("svc", "engineer")
        self.assertTrue(lan_mode.refusal().startswith(engineer_line), "a service principal can't log in")
        self.make("eng", "engineer")
        self.make("eng2", "engineer")
        store.set_enabled("eng2", False)
        self.assertIsNone(lan_mode.refusal())
        # The last-Engineer rule refuses disabling eng now, so check a disabled-only store directly.
        with store._connect(write=True) as conn:
            conn.execute("UPDATE users SET enabled = 0 WHERE username = 'eng'")
        self.assertTrue(lan_mode.refusal().startswith(engineer_line), "a disabled Engineer")

    def test_each_refusal_is_one_line(self):
        for secure in (False, True):
            with self.subTest(secure=secure), patch.object(settings, "auth_cookie_secure", secure):
                line = lan_mode.refusal()
                self.assertNotIn("\n", line)

    def test_enforce_prints_the_line_and_exits(self):
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit) as caught:
            lan_mode.enforce()
        self.assertEqual(caught.exception.code, 1)
        self.assertEqual(len(err.getvalue().splitlines()), 1)
        self.make("eng", "engineer")
        lan_mode.enforce()   # no refusal: returns


class MainImportTests(unittest.TestCase):
    """S2.2/S2.6: uvicorn's import of main stops first, with one line and no traceback."""

    def test_enforce_is_mains_first_statement(self):
        tree = ast.parse((Path(API_DIR) / "main.py").read_text())
        body = [node for node in tree.body
                if not (isinstance(node, ast.ImportFrom) and node.module == "__future__")
                and not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
        self.assertEqual(ast.unparse(body[0]), "import lan_mode")
        self.assertEqual(ast.unparse(body[1]), "lan_mode.enforce()")

    def run_import(self, **env):
        with tempfile.TemporaryDirectory() as folder:
            full = {**os.environ, "AUTH_DIR": folder, "PYTHONDONTWRITEBYTECODE": "1", **env}
            return subprocess.run([sys.executable, "-c", "import main"], cwd=API_DIR, env=full,
                                  capture_output=True, text=True, timeout=300)

    def test_lan_mode_without_a_secure_cookie_exits_with_one_line(self):
        result = self.run_import(RAG_LAN_MODE="true", AUTH_COOKIE_SECURE="false")
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, "")
        lines = result.stderr.splitlines()
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertTrue(lines[0].startswith("LAN mode refused: AUTH_COOKIE_SECURE"), lines)

    def test_lan_mode_with_an_empty_store_exits_with_one_line(self):
        result = self.run_import(RAG_LAN_MODE="true", AUTH_COOKIE_SECURE="true")
        self.assertEqual(result.returncode, 1, result.stderr)
        lines = result.stderr.splitlines()
        self.assertEqual(len(lines), 1, result.stderr)
        self.assertIn("no enabled AI Engineer", lines[0])

    def test_an_invalid_value_stops_the_api(self):
        result = self.run_import(RAG_LAN_MODE="yes")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RAG_LAN_MODE must be exactly true or false", result.stderr)


# ── the certificate step ─────────────────────────────────────────────────────

def certificate(names, days=30, now=NOW):
    """A self-signed pair as a bring-your-own certificate would be: (cert PEM, key PEM)."""
    cert_bytes, key_bytes = tls_cert.self_signed(names, now - datetime.timedelta(days=825 - days))
    return cert_bytes, key_bytes


class CertTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.byo = Path(temp.name) / "byo"
        self.out = Path(temp.name) / "certs"
        self.byo.mkdir()

    def step(self, hostnames="office.example,192.168.1.20", now=NOW):
        out, err = io.StringIO(), io.StringIO()
        code = tls_cert.run(self.byo, self.out, hostnames, now, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def live(self):
        live = self.out / "live"
        cert = x509.load_pem_x509_certificate((live / "cert.pem").read_bytes())
        return live, cert, (live / "source").read_text().strip()

    def names(self, cert):
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        return ([n for n in san.get_values_for_type(x509.DNSName)],
                [str(a) for a in san.get_values_for_type(x509.IPAddress)])


class SelfSignedTests(CertTestCase):
    """S4.3-S4.6."""

    def test_generates_for_the_names_plus_localhost(self):
        code, out, err = self.step("Office.Example, 192.168.1.20 ,office.example,::1")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(len(out.splitlines()), 1)
        self.assertIn("generated a self-signed certificate", out)
        live, cert, source = self.live()
        self.assertEqual(source, "self-signed")
        self.assertEqual(self.names(cert), (["office.example", "localhost"], ["192.168.1.20", "::1"]))
        self.assertEqual(cert.subject.rfc4514_string(), "CN=office.example")
        self.assertEqual(cert.not_valid_after_utc - cert.not_valid_before_utc, datetime.timedelta(days=825))
        self.assertEqual(cert.not_valid_before_utc, NOW)
        self.assertFalse(cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca)
        self.assertTrue(cert.extensions.get_extension_for_class(x509.BasicConstraints).critical)
        self.assertEqual(list(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value),
                         [x509.oid.ExtendedKeyUsageOID.SERVER_AUTH])
        with self.assertRaises(x509.ExtensionNotFound):
            cert.extensions.get_extension_for_class(x509.KeyUsage)
        key = serialization.load_pem_private_key((live / "key.pem").read_bytes(), None)
        self.assertEqual(key.curve.name, "secp256r1")
        self.assertEqual((live / "key.pem").stat().st_mode & 0o777, 0o600)
        self.assertIn(tls_cert.fingerprint(cert), out)
        self.assertTrue((self.out / "live").is_symlink())

    def test_names_are_required_and_checked(self):
        for raw, part in ((None, "empty"), ("", "empty"), ("  ", "empty"), ("a,,b", "empty entry"),
                          ("bad_name", "'bad_name'"), ("-x.example", "'-x.example'"),
                          ("x" * 64 + ".example", "neither"), ("*.example", "'*.example'"),
                          ("a." * 127 + "ab", "neither")):
            with self.subTest(raw=raw):
                code, out, err = self.step(raw)
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(len(err.splitlines()), 1, err)
                self.assertIn(part, err)
                self.assertFalse((self.out / "live").exists())

    def test_reused_while_valid_and_for_the_same_names(self):
        self.step()
        _, first, _ = self.live()
        code, out, _ = self.step("192.168.1.20, OFFICE.example", now=NOW + datetime.timedelta(days=800))
        self.assertEqual(code, 0)
        self.assertIn("reusing the self-signed certificate", out)
        self.assertEqual(self.live()[1], first)
        self.assertEqual(len([p for p in self.out.iterdir() if p.name.startswith("pair-")]), 1)

    def regenerated(self, out):
        self.assertIn("generated a self-signed certificate", out)

    def test_regenerated_when_expired(self):
        self.step()
        _, first, _ = self.live()
        code, out, _ = self.step(now=NOW + datetime.timedelta(days=825))
        self.assertEqual(code, 0)
        self.regenerated(out)
        self.assertNotEqual(self.live()[1], first)

    def test_regenerated_when_the_names_change(self):
        self.step()
        code, out, _ = self.step("office.example")
        self.regenerated(out)
        self.assertEqual(self.names(self.live()[1]), (["office.example", "localhost"], []))

    def test_regenerated_when_missing_unreadable_or_mismatched(self):
        self.step()
        live, _, _ = self.live()
        (live / "key.pem").write_bytes(certificate(["other"])[1])
        _, out, _ = self.step()
        self.regenerated(out)
        live, _, _ = self.live()
        (live / "cert.pem").write_bytes(b"not a certificate")
        _, out, _ = self.step()
        self.regenerated(out)
        live, _, _ = self.live()
        (live / "key.pem").unlink()
        _, out, _ = self.step()
        self.regenerated(out)
        (self.out / "live").unlink()
        _, out, _ = self.step()
        self.regenerated(out)
        self.assertEqual(len([p for p in self.out.iterdir() if p.name.startswith("pair-")]), 1,
                         "older pairs are removed")

    def test_regenerated_after_bring_your_own_files_are_removed(self):
        cert_bytes, key_bytes = certificate(["office.example", "192.168.1.20", "localhost"])
        (self.byo / "cert.pem").write_bytes(cert_bytes)
        (self.byo / "key.pem").write_bytes(key_bytes)
        self.step()
        self.assertEqual(self.live()[2], "byo")
        (self.byo / "cert.pem").unlink()
        (self.byo / "key.pem").unlink()
        code, out, _ = self.step()
        self.assertEqual(code, 0)
        self.regenerated(out)
        self.assertEqual(self.live()[2], "self-signed")

    def test_a_failed_write_leaves_the_live_pair(self):
        self.step()
        before = os.readlink(self.out / "live")
        with patch.object(tls_cert.os, "symlink", side_effect=OSError("disk full")):
            code, out, err = self.step("office.example")
        self.assertEqual(code, 1)
        self.assertEqual(len(err.splitlines()), 1)
        self.assertEqual(os.readlink(self.out / "live"), before)
        live, cert, _ = self.live()
        key = serialization.load_pem_private_key((live / "key.pem").read_bytes(), None)
        self.assertEqual(tls_cert._public(key.public_key()), tls_cert._public(cert.public_key()))


class BringYourOwnTests(CertTestCase):
    """S4.2."""

    def put(self, cert_bytes=None, key_bytes=None):
        if cert_bytes is not None:
            (self.byo / "cert.pem").write_bytes(cert_bytes)
        if key_bytes is not None:
            (self.byo / "key.pem").write_bytes(key_bytes)

    def test_used_as_given_with_a_chain(self):
        server_cert, key_bytes = certificate(["files.example"])
        other_cert, _ = certificate(["intermediate.example"])
        self.put(server_cert + other_cert, key_bytes)
        code, out, err = self.step(hostnames=None)
        self.assertEqual((code, err), (0, ""))
        self.assertIn("using your certificate from proxy/certs", out)
        self.assertIn("files.example", out)
        live, _, source = self.live()
        self.assertEqual(source, "byo")
        self.assertEqual((live / "cert.pem").read_bytes(), server_cert + other_cert)
        self.assertEqual((live / "key.pem").read_bytes(), key_bytes)
        self.assertEqual((live / "key.pem").stat().st_mode & 0o777, 0o600)
        pair = os.readlink(self.out / "live")
        self.step(hostnames=None)
        self.assertEqual(os.readlink(self.out / "live"), pair, "unchanged files aren't installed again")

    def test_wins_over_hostnames_and_replaces_a_self_signed_pair(self):
        self.step()
        self.assertEqual(self.live()[2], "self-signed")
        self.put(*certificate(["files.example"]))
        code, out, _ = self.step()
        self.assertEqual(code, 0)
        self.assertEqual(self.live()[2], "byo")

    def test_refusals(self):
        cert_bytes, key_bytes = certificate(["files.example"])
        _, other_key = certificate(["other.example"])
        cases = [("only cert.pem", cert_bytes, None, "only one"),
                 ("only key.pem", None, key_bytes, "only one"),
                 ("not PEM", b"junk", key_bytes, "cert.pem"),
                 ("key not PEM", cert_bytes, b"junk", "key.pem"),
                 ("mismatched key", cert_bytes, other_key, "doesn't match")]
        for name, cert_part, key_part, part in cases:
            with self.subTest(case=name):
                for path in self.byo.iterdir():
                    path.unlink()
                self.put(cert_part, key_part)
                code, out, err = self.step()
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(len(err.splitlines()), 1, err)
                self.assertIn(part, err)
                self.assertFalse((self.out / "live").exists())

    def test_an_expired_certificate_is_used_with_a_warning(self):
        self.put(*certificate(["files.example"], days=-1))
        code, out, err = self.step()
        self.assertEqual(code, 0)
        self.assertIn("warning: the certificate in proxy/certs expired", err)
        self.assertEqual(self.live()[2], "byo")


# ── nginx ────────────────────────────────────────────────────────────────────

def directives(text):
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


class NginxTests(unittest.TestCase):
    """S3.1-S3.4."""

    def setUp(self):
        self.lan = (ROOT / "proxy" / "nginx.lan.conf").read_text()
        self.default = (ROOT / "proxy" / "nginx.conf").read_text()
        self.locations = (ROOT / "proxy" / "locations.conf").read_text()

    def test_one_tls_server_on_443(self):
        lines = directives(self.lan)
        self.assertEqual([l for l in lines if l.startswith("listen")], ["listen 443 ssl;"])
        self.assertEqual(lines.count("server {"), 1)
        self.assertIn("http2 on;", lines)
        self.assertIn("ssl_certificate     /etc/nginx/certs/live/cert.pem;", lines)
        self.assertIn("ssl_certificate_key /etc/nginx/certs/live/key.pem;", lines)

    def test_protocols_ciphers_and_no_hsts(self):
        lines = directives(self.lan)
        self.assertIn("ssl_protocols TLSv1.2 TLSv1.3;", lines)
        self.assertIn("ssl_ciphers ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:"
                      "ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305:"
                      "DHE-RSA-AES128-GCM-SHA256:DHE-RSA-AES256-GCM-SHA384:DHE-RSA-CHACHA20-POLY1305;", lines)
        self.assertIn("ssl_prefer_server_ciphers off;", lines)
        self.assertIn("ssl_session_tickets off;", lines)
        for text in (self.lan, self.locations):
            self.assertNotIn("strict-transport-security", "\n".join(directives(text)).lower())

    def test_both_servers_share_the_locations(self):
        include = "include /etc/nginx/rag/locations.conf;"
        for text in (self.lan, self.default):
            lines = directives(text)
            self.assertEqual(lines.count(include), 1)
            self.assertEqual([l for l in lines if l.startswith("location")], [], "locations belong in locations.conf")
        self.assertEqual([l for l in directives(self.default) if l.startswith("listen")], ["listen 80;"])
        upstreams = lambda text: re.findall(r"upstream \w+ \{[^}]*\}", text)
        self.assertEqual(upstreams(self.lan), upstreams(self.default))
        lines = directives(self.locations)
        self.assertEqual([l for l in lines if l.startswith("location")],
                         ["location = /api/goldstandard/regenerate {", "location = /api/auth/login {",
                          "location @login_rate_limited {", "location /api/ {", "location / {"])
        self.assertEqual(lines.count("proxy_set_header Host $http_host;"), 3)

    def test_logins_are_rate_limited_per_address(self):
        # #577: both servers define the zone; the login location uses it and
        # answers nginx's own refusals in the API's error envelope.
        zone = "limit_req_zone $binary_remote_addr zone=login:1m rate=10r/m;"
        for text in (self.lan, self.default):
            self.assertEqual(directives(text).count(zone), 1)
        block = self.locations[self.locations.index("location = /api/auth/login {"):]
        block = block[:block.index("\n}\n")]
        lines = directives(block)
        for line in ("limit_req zone=login burst=5 nodelay;", "limit_req_status 429;",
                     "error_page 429 = @login_rate_limited;", "proxy_pass http://api/auth/login;",
                     "proxy_set_header X-Real-IP $remote_addr;"):
            self.assertIn(line, lines)
        api = self.locations[self.locations.index("location /api/ {"):]
        api = directives(api[:api.index("\n}\n")])
        self.assertEqual([l for l in lines if not l.startswith(("location", "limit_req", "error_page", "proxy_pass"))],
                         [l for l in api if not l.startswith(("location", "proxy_pass"))],
                         "the login location proxies differently from /api/")
        named = self.locations[self.locations.index("location @login_rate_limited {"):]
        body = re.search(r"return 429 '([^']*)';", named).group(1)
        message = "Too many login attempts. Wait a minute and try again."
        self.assertEqual(json.loads(body), {"error": {"code": "LOGIN_RATE_LIMITED", "message": message, "detail": None}})
        self.assertIn("default_type application/json;", directives(named[:named.index("\n}\n")]))


# ── embedded copies ──────────────────────────────────────────────────────────

class ImplementationCopyTests(unittest.TestCase):
    def test_embedded_copies_match(self):
        document = (ROOT / "IMPLEMENTATION.md").read_text()
        for name in ("docker-compose.lan.yml", "docker-compose.lan.verify.yml", "proxy/nginx.lan.conf",
                     "proxy/locations.conf", "api/lan_mode.py", "api/tls_cert.py", "scripts/verify/18_lan.sh",
                     "scripts/verify/lan_config.py", "scripts/verify/lan_stack.py", "scripts/tests/test_lan_mode.py"):
            with self.subTest(name=name):
                # A file holding ``` itself (this one) is fenced with ````.
                header = "### " + name + "\n\n"
                fence_at = document.index(header) + len(header)
                fence_line = document[fence_at:document.index("\n", fence_at)]
                ticks = fence_line[:len(fence_line) - len(fence_line.lstrip("`"))]
                start = fence_at + len(fence_line) + 1
                end = document.index("\n" + ticks + "\n", start)
                self.assertEqual(document[start:end], (ROOT / name).read_text().rstrip("\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
