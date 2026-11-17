"""GET /health/details answers 503 with the per-service body when a dependency is down (issue #375).

Since #750 the full body is at `GET /health/details`, for Developers and AI Engineers, and
the public `GET /health` answers only `{"status": "ok" | "degraded"}` with the same 200/503
rule; both, and who may read the details, are checked here too. The Health page reads the
details body to show the failing services in red, so the body is
checked here: a model missing from Ollama, Ollama unreachable, Weaviate unreachable,
and Weaviate and Ollama down together. Memory below the recommendation, or unreadable,
is advisory: still 200 `ok` (#415). The body is unchanged by #375, #415 and #750;
this pins what the page relies on. `llm` is the answer model and `build_llm` the build
model, with `same_model` saying whether they are one (#671); a missing build model is 503
too, and the two settings resolve from LLM_MODEL as config.py says.

Runs with the API's dependencies, in the built API image with no network
(`01_infrastructure.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_health.py

Each request goes through `main.app` with FastAPI's TestClient, without the lifespan.
Ollama's `/api/tags` is answered by an httpx mock transport, so `ollama_client.check_health`
runs unchanged; the Weaviate client check is mocked, and so is the memory reading when
a case sets it. Nothing reaches a backend.
"""
import contextlib
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

import httpx
from fastapi.testclient import TestClient

from config import settings
from main import app
from auth import testing as auth_testing  # a real session in a temporary store (#25)
from services import system_info
from services import weaviate_client as wc

REAL_ASYNC_CLIENT = httpx.AsyncClient
UNSET = object()


def tagged(model):
    """A model name as Ollama lists it, with a tag."""
    return model if ":" in model else model + ":latest"


class HealthTestCase(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, headers=auth_testing.headers(), raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def get_health(self, *, models=None, ollama_down=False, weaviate=True, memory_gb=UNSET,
                   path="/health/details", client=None):
        """GET `path` (by default /health/details, as the engineer) with Ollama listing
        `models` (or refusing connections),
        Weaviate's client check returning, or raising, `weaviate`, and, when given,
        `memory_gb` as the Docker VM's memory (None: unreadable)."""
        if models is None:
            models = [settings.answer_model, settings.build_model, settings.embed_model]

        def ollama(request):
            if ollama_down:
                raise httpx.ConnectError("connection refused", request=request)
            if request.url.path != "/api/tags":
                return httpx.Response(404)
            return httpx.Response(200, json={"models": [{"name": tagged(m)} for m in models]})

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(ollama)
            return REAL_ASYNC_CLIENT(*args, **kwargs)

        check = (AsyncMock(side_effect=weaviate) if isinstance(weaviate, BaseException)
                 else AsyncMock(return_value=weaviate))
        memory = (patch.object(system_info, "_read_mem_total_gb", return_value=memory_gb)
                  if memory_gb is not UNSET else contextlib.nullcontext())
        with patch.object(httpx, "AsyncClient", client_factory), \
                patch.object(wc, "check_health", check), memory:
            return (client or self.client).get(path)

    def assert_body(self, response, status_code, overall, weaviate, llm, embed, build=None,
                    same_model=True):
        self.assertEqual(response.status_code, status_code, response.text)
        self.assertTrue(response.headers["content-type"].startswith("application/json"),
                        response.headers["content-type"])
        body = response.json()
        self.assertEqual(body["status"], overall)
        services = body["services"]
        self.assertEqual(services["weaviate"]["status"], weaviate)
        self.assertIsInstance(services["weaviate"]["latency_ms"], int)
        build = llm if build is None else build
        for key, want, model in (("llm", llm, settings.answer_model),
                                 ("build_llm", build, settings.build_model),
                                 ("embed", embed, settings.embed_model)):
            service = services["ollama"][key]
            self.assertEqual(service["status"], want, key)
            self.assertEqual(service["model"], model)
            self.assertIsInstance(service["latency_ms"], int)
        self.assertIs(services["ollama"]["same_model"], same_model)
        # Advisory resources are still reported when degraded.
        self.assertIn("memory", body["resources"])
        return body


class HealthBodyTests(HealthTestCase):
    def test_all_services_ok_is_200_ok(self):
        self.assert_body(self.get_health(), 200, "ok", "ok", "ok", "ok")

    def test_a_missing_llm_model_is_503_degraded_naming_it(self):
        response = self.get_health(models=[settings.embed_model])
        self.assert_body(response, 503, "degraded", "ok", "error", "ok")

    def test_a_missing_embedding_model_is_503_degraded_naming_it(self):
        response = self.get_health(models=[settings.llm_model])
        self.assert_body(response, 503, "degraded", "ok", "ok", "error")

    def test_ollama_unreachable_is_503_degraded_with_both_models_in_error(self):
        response = self.get_health(ollama_down=True)
        self.assert_body(response, 503, "degraded", "ok", "error", "error")

    def test_weaviate_not_ready_is_503_degraded(self):
        response = self.get_health(weaviate=False)
        self.assert_body(response, 503, "degraded", "error", "ok", "ok")

    def test_weaviate_unreachable_is_503_degraded(self):
        for cause in (ConnectionError("connection refused"), TimeoutError("connect timed out")):
            with self.subTest(cause=type(cause).__name__):
                response = self.get_health(weaviate=cause)
                self.assert_body(response, 503, "degraded", "error", "ok", "ok")

    def test_weaviate_and_ollama_down_together_is_503_with_every_service_in_error(self):
        response = self.get_health(ollama_down=True, weaviate=ConnectionError("connection refused"))
        self.assert_body(response, 503, "degraded", "error", "error", "error")


class TwoModelsTests(HealthTestCase):
    """A build model that differs from the answer model (#671)."""

    def setUp(self):
        super().setUp()
        for name, value in (("llm_model", "llama3.2"), ("build_llm_model", "qwen2.5:0.5b"),
                            ("answer_llm_model", "")):
            patcher = patch.object(settings, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_each_model_is_reported_and_same_model_is_false(self):
        body = self.assert_body(self.get_health(), 200, "ok", "ok", "ok", "ok", same_model=False)
        ollama = body["services"]["ollama"]
        self.assertEqual((ollama["llm"]["model"], ollama["build_llm"]["model"]),
                         ("llama3.2", "qwen2.5:0.5b"))

    def test_a_missing_build_model_is_503_degraded_naming_it(self):
        response = self.get_health(models=["llama3.2", settings.embed_model])
        self.assert_body(response, 503, "degraded", "ok", "ok", "ok", build="error", same_model=False)

    def test_a_missing_answer_model_is_503_degraded_naming_it(self):
        response = self.get_health(models=["qwen2.5:0.5b", settings.embed_model])
        self.assert_body(response, 503, "degraded", "ok", "error", "ok", build="ok", same_model=False)

    def test_ollama_unreachable_puts_all_three_in_error(self):
        response = self.get_health(ollama_down=True)
        self.assert_body(response, 503, "degraded", "ok", "error", "error", same_model=False)


class ChatModelTests(unittest.TestCase):
    """`chat` sends the answer model and `build_chat` the build model (#671)."""

    def test_each_call_sends_its_own_model(self):
        import asyncio
        import json as jsonlib
        from services import ollama_client
        sent = []

        def ollama(request):
            sent.append(jsonlib.loads(request.content)["model"])
            return httpx.Response(200, json={"message": {"content": "reply"}})

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(ollama)
            return REAL_ASYNC_CLIENT(*args, **kwargs)

        with patch.object(httpx, "AsyncClient", client_factory), \
                patch.object(settings, "llm_model", "llama3.2"), \
                patch.object(settings, "build_llm_model", "qwen2.5:0.5b"), \
                patch.object(settings, "answer_llm_model", ""):
            asyncio.run(ollama_client.chat("system", "user"))
            asyncio.run(ollama_client.build_chat("system", "user"))
        self.assertEqual(sent, ["llama3.2", "qwen2.5:0.5b"])

    def test_same_model_treats_a_missing_tag_as_latest(self):
        from services.ollama_client import same_model
        self.assertTrue(same_model("llama3.2", "llama3.2:latest"))
        self.assertTrue(same_model("someone/model", "someone/model:latest"))
        self.assertFalse(same_model("llama3.1", "llama3.1:8b"))


class ModelSettingsTests(unittest.TestCase):
    """BUILD_LLM_MODEL and ANSWER_LLM_MODEL fall back to LLM_MODEL, then llama3.1:8b (#671)."""

    def resolved(self, **values):
        from config import Settings
        with patch.dict(os.environ, {k.upper(): v for k, v in values.items()}, clear=False):
            for key in ("LLM_MODEL", "BUILD_LLM_MODEL", "ANSWER_LLM_MODEL"):
                if key.lower() not in values:
                    os.environ.pop(key, None)
            made = Settings(_env_file=None)
        return made.build_model, made.answer_model

    def test_the_default_is_one_model(self):
        self.assertEqual(self.resolved(), ("llama3.1:8b", "llama3.1:8b"))

    def test_llm_model_is_the_default_for_both(self):
        self.assertEqual(self.resolved(llm_model="llama3.2"), ("llama3.2", "llama3.2"))

    def test_each_setting_replaces_llm_model_for_its_task(self):
        self.assertEqual(self.resolved(llm_model="llama3.2", build_llm_model="qwen2.5:0.5b"),
                         ("qwen2.5:0.5b", "llama3.2"))
        self.assertEqual(self.resolved(llm_model="llama3.2", answer_llm_model="qwen2.5:0.5b"),
                         ("llama3.2", "qwen2.5:0.5b"))

    def test_an_empty_setting_counts_as_not_set(self):
        self.assertEqual(self.resolved(llm_model="llama3.2", build_llm_model="", answer_llm_model=""),
                         ("llama3.2", "llama3.2"))
        self.assertEqual(self.resolved(llm_model=""), ("llama3.1:8b", "llama3.1:8b"))

    def test_patching_llm_model_moves_both(self):
        with patch.object(settings, "llm_model", "someone/assistant"), \
                patch.object(settings, "build_llm_model", ""), \
                patch.object(settings, "answer_llm_model", ""):
            self.assertEqual((settings.build_model, settings.answer_model),
                             ("someone/assistant", "someone/assistant"))


class HealthMemoryTests(HealthTestCase):
    """Memory is advisory: a resource that isn't ok never degrades /health (#415)."""

    def test_memory_below_recommended_is_200_ok_and_reported(self):
        body = self.assert_body(self.get_health(memory_gb=1.0), 200, "ok", "ok", "ok", "ok")
        memory = body["resources"]["memory"]
        self.assertEqual(memory["status"], "below_recommended")
        self.assertEqual(memory["allocated_gb"], 1.0)
        self.assertEqual(memory["recommended_minimum_gb"], round(settings.recommended_memory_gb, 1))
        self.assertIn("note", memory)

    def test_memory_unknown_is_200_ok_and_reported(self):
        body = self.assert_body(self.get_health(memory_gb=None), 200, "ok", "ok", "ok", "ok")
        memory = body["resources"]["memory"]
        self.assertEqual(memory["status"], "unknown")
        self.assertIsNone(memory["allocated_gb"])


class PublicHealthTests(HealthTestCase):
    """The public GET /health answers only the overall status, with the same rule (#750)."""

    def setUp(self):
        super().setUp()
        self.anonymous = TestClient(app, raise_server_exceptions=False)   # no session
        self.addCleanup(self.anonymous.close)

    def get_public(self, **kw):
        return self.get_health(path="/health", client=self.anonymous, **kw)

    def assert_only_status(self, response, status_code, overall):
        self.assertEqual(response.status_code, status_code, response.text)
        self.assertEqual(response.json(), {"status": overall})

    def test_all_services_ok_is_200_with_only_the_status(self):
        self.assert_only_status(self.get_public(), 200, "ok")

    def test_a_dependency_down_is_503_with_only_the_status(self):
        for case in ({"ollama_down": True}, {"weaviate": False}, {"models": [settings.embed_model]}):
            with self.subTest(case=case):
                self.assert_only_status(self.get_public(**case), 503, "degraded")

    def test_low_memory_stays_200_with_only_the_status(self):
        self.assert_only_status(self.get_public(memory_gb=1.0), 200, "ok")


class DetailsAccessTests(HealthTestCase):
    """GET /health/details is for Developers and AI Engineers, sessions and keys (#750)."""

    def client_for(self, role=None):
        headers = auth_testing.headers(role) if role else {}
        client = TestClient(app, headers=headers, raise_server_exceptions=False)
        self.addCleanup(client.close)
        return client

    def test_without_a_session_it_is_401(self):
        response = self.get_health(client=self.client_for())
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (401, "AUTH_REQUIRED"))

    def test_an_end_user_is_refused(self):
        response = self.get_health(client=self.client_for("end_user"))
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (403, "FORBIDDEN"))

    def test_a_developer_gets_the_full_body(self):
        self.assert_body(self.get_health(client=self.client_for("developer")), 200, "ok", "ok", "ok", "ok")
        response = self.get_health(client=self.client_for("developer"), ollama_down=True)
        self.assert_body(response, 503, "degraded", "ok", "error", "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
