"""Every protected API route refuses a request without a session (#25).

Before #25 the API had no authentication: any client could list, create and
delete collections, ingest, export and poll jobs. This sends plain HTTP through
`main.app`, so it runs unchanged on a checkout without the `auth` package and
fails there. With it, the store is pointed at a temporary folder holding one
account, so a refusal is 401 AUTH_REQUIRED rather than 503 SETUP_REQUIRED.

Runs in the built API image with no network (`17_auth.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_auth_enforcement.py
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi.testclient import TestClient

from main import app
from services import ollama_client
from services import weaviate_client as wc

try:
    from auth import testing as auth_testing
except ImportError:          # a checkout from before #25
    auth_testing = None

X = "VfyAuthNoSuch"
# Every route but GET /health, with a request that would reach it.
PROTECTED = (
    ("GET", "/collections"), ("POST", "/collections"), ("DELETE", f"/collections/{X}?confirm=true"),
    ("POST", "/query"), ("GET", f"/retrieval/config/{X}"), ("POST", "/retrieval/config"),
    ("POST", "/ingest/upload"), ("GET", "/ingest/job/nope"), ("GET", f"/ingest/config/{X}"),
    ("POST", "/ingest/config"),
    ("POST", "/goldstandard/generate"), ("GET", "/goldstandard/session/gs_nope"),
    ("PATCH", "/goldstandard/session/gs_nope/pair/p"), ("POST", "/goldstandard/regenerate"),
    ("POST", "/goldstandard/save"), ("GET", "/goldstandard/download/nope.json"),
    ("GET", "/goldstandard/diagnostics"),
    ("POST", "/export"), ("GET", "/export/job/nope"), ("POST", "/import"), ("GET", "/import/job/nope"),
    ("GET", "/packages"), ("GET", "/help/transfer"),
    ("POST", "/tune/rechunk"), ("POST", "/tune/reembed"), ("POST", "/tune/reindex"),
    ("GET", "/tune/job/nope"), ("GET", f"/tune/{X}"),
    ("GET", "/metrics/latency"),
    ("GET", "/docs"), ("GET", "/docs/oauth2-redirect"), ("GET", "/redoc"), ("GET", "/openapi.json"),
    ("GET", "/auth/users"), ("POST", "/auth/users"), ("POST", "/auth/users/nobody/reset-password"),
    ("POST", "/auth/users/nobody/role"), ("POST", "/auth/users/nobody/disable"),
    ("POST", "/auth/users/nobody/enable"),
    ("GET", "/auth/service-principals"), ("POST", "/auth/service-principals"),
    ("POST", "/auth/service-principals/nobody/role"), ("POST", "/auth/service-principals/nobody/disable"),
    ("POST", "/auth/service-principals/nobody/enable"), ("POST", "/auth/service-principals/nobody/keys"),
    ("POST", "/auth/service-principals/nobody/keys/nokey/revoke"),
)


class UnauthenticatedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if auth_testing is not None:
            auth_testing.account("end_user")      # set up, so the answer is 401, not 503
        cls.client = TestClient(app, raise_server_exceptions=False)

    def test_every_protected_route_answers_401_without_a_session(self):
        for method, path in PROTECTED:
            with self.subTest(route=f"{method} {path}"):
                response = self.client.request(method, path, json={} if method != "GET" else None)
                self.assertEqual(response.status_code, 401, response.text[:200])
                self.assertEqual(response.json()["error"]["code"], "AUTH_REQUIRED")

    def test_health_stays_open(self):
        # Backends mocked: nothing reaches a stack.
        with patch.object(wc, "check_health", AsyncMock(return_value=True)), \
                patch.object(ollama_client, "check_health", AsyncMock(return_value={})):
            self.assertNotIn(self.client.get("/health").status_code, (401, 403))


if __name__ == "__main__":
    unittest.main(verbosity=2)
