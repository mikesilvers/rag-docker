"""External-Ollama mode (#673): SPECIFICATIONS.md §2.7, RAG_EXPORT_SPECIFICATIONS.md §6.3.

The settings (RAG_EXTERNAL_OLLAMA, OLLAMA_NUM_CTX, OLLAMA_KEEP_ALIVE), the bodies
of rag-docker's own Ollama requests in both modes (num_ctx on chat only,
keep_alive everywhere) and the /api/embed fallback, the tag-exact model check,
the version and, in external mode, the memory report and what Ollama has loaded
in /health/details, the start-up lines for missing models, import's check-only
path and the refused export with models, `python -m ollama_check`, the overlay
file and the base file's two settings, and install-offline.sh's options.

Runs with the API's dependencies, on the host (`~/.venvs/rag-api/bin/python`) or
in the built API image with no network (`25_external_ollama.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_external_ollama.py

Ollama is answered by an httpx mock transport; nothing reaches a backend.
"""
import asyncio
import contextlib
import io
import json
import logging
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(ROOT / "api"))

import httpx
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

import ollama_check
from config import Settings, settings
from main import app
from auth import testing as auth_testing
from services import importer, model_bundle, ollama_client, ollama_startup, system_info
from services import weaviate_client as wc
from services.packager import PackageError

REAL_ASYNC_CLIENT = httpx.AsyncClient
REAL_CLIENT = httpx.Client


def tagged(model):
    return model if ":" in model.rsplit("/", 1)[-1] else model + ":latest"


class FakeOllama:
    """An Ollama answering /api/tags, /api/version, /api/ps, /api/chat and the two
    embedding endpoints, recording every request's path and JSON body."""

    def __init__(self, models=None, down=False, version="0.40.18", embeddings_gone=False,
                 loaded=None, ps_fails=False):
        self.models = models
        self.down = down
        self.version = version
        self.embeddings_gone = embeddings_gone
        self.loaded = loaded if loaded is not None else []
        self.ps_fails = ps_fails
        self.requests = []

    def __call__(self, request):
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        if path == "/api/tags":
            models = self.models
            if models is None:
                models = [settings.answer_model, settings.build_model, settings.embed_model]
            return httpx.Response(200, json={"models": [{"name": tagged(m)} for m in models]})
        if path == "/api/version":
            return httpx.Response(200, json={"version": self.version})
        if path == "/api/ps":
            if self.ps_fails:
                return httpx.Response(500)
            return httpx.Response(200, json={"models": self.loaded})
        if path == "/api/chat":
            return httpx.Response(200, json={"message": {"content": "an answer"}})
        if path == "/api/embeddings":
            if self.embeddings_gone:
                return httpx.Response(404, text="404 page not found")
            return httpx.Response(200, json={"embedding": [0.1, 0.2, 0.3]})
        if path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [[0.4, 0.5, 0.6]]})
        return httpx.Response(404)

    @contextlib.contextmanager
    def serving(self):
        def async_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(self)
            return REAL_ASYNC_CLIENT(*args, **kwargs)

        def sync_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(self)
            return REAL_CLIENT(*args, **kwargs)

        with patch.object(httpx, "AsyncClient", async_factory), \
                patch.object(httpx, "Client", sync_factory):
            yield self

    def bodies(self, path):
        return [body for _, p, body in self.requests if p == path]


def external(on=True):
    return patch.object(settings, "rag_external_ollama", on)


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        for key in ("RAG_EXTERNAL_OLLAMA", "OLLAMA_NUM_CTX", "OLLAMA_KEEP_ALIVE"):
            os.environ.pop(key, None)

    def test_defaults(self):
        s = Settings(_env_file=None)
        self.assertIs(s.rag_external_ollama, False)
        self.assertEqual(s.ollama_num_ctx, 2048)
        self.assertEqual(s.ollama_keep_alive, "5m")

    def test_external_is_exactly_true_or_false(self):
        for value, want in (("true", True), ("false", False)):
            os.environ["RAG_EXTERNAL_OLLAMA"] = value
            self.assertIs(Settings(_env_file=None).rag_external_ollama, want)
        for value in ("yes", "1", "True", ""):
            with self.subTest(value=value):
                os.environ["RAG_EXTERNAL_OLLAMA"] = value
                with self.assertRaises(ValidationError) as caught:
                    Settings(_env_file=None)
                self.assertIn("RAG_EXTERNAL_OLLAMA must be exactly true or false", str(caught.exception))

    def test_num_ctx_bounds(self):
        for value in ("256", "2048", "1048576"):
            os.environ["OLLAMA_NUM_CTX"] = value
            self.assertEqual(Settings(_env_file=None).ollama_num_ctx, int(value))
        for value in ("255", "1048577", "0", "-1"):
            with self.subTest(value=value):
                os.environ["OLLAMA_NUM_CTX"] = value
                with self.assertRaises(ValidationError) as caught:
                    Settings(_env_file=None)
                self.assertIn("OLLAMA_NUM_CTX must be 256 to 1048576", str(caught.exception))

    def test_keep_alive_is_a_duration(self):
        for value in ("5m", "1h30m", "300", "-1", "0", "500ms", "1.5h", "30s"):
            with self.subTest(value=value):
                os.environ["OLLAMA_KEEP_ALIVE"] = value
                self.assertEqual(Settings(_env_file=None).ollama_keep_alive, value)
        for value in ("", "5x", "m", "5 m", "forever", "5m;"):
            with self.subTest(value=value):
                os.environ["OLLAMA_KEEP_ALIVE"] = value
                with self.assertRaises(ValidationError) as caught:
                    Settings(_env_file=None)
                self.assertIn("OLLAMA_KEEP_ALIVE must be a duration", str(caught.exception))


class RequestBodyTests(unittest.TestCase):
    """S3: the same bodies in both modes; num_ctx on chat only."""

    def setUp(self):
        ollama_client._use_embed = False
        self.addCleanup(setattr, ollama_client, "_use_embed", False)

    def test_chat_carries_num_ctx_and_keep_alive_in_both_modes(self):
        for on in (False, True):
            with self.subTest(external=on), external(on), \
                    patch.object(settings, "ollama_num_ctx", 4096), \
                    patch.object(settings, "ollama_keep_alive", "10m"), FakeOllama().serving() as fake:
                asyncio.run(ollama_client.chat("system", "user"))
                asyncio.run(ollama_client.build_chat("system", "user"))
                bodies = fake.bodies("/api/chat")
                self.assertEqual([b["model"] for b in bodies], [settings.answer_model, settings.build_model])
                for body in bodies:
                    self.assertEqual(body["options"], {"num_ctx": 4096})
                    self.assertEqual(body["keep_alive"], "10m")
                    self.assertIs(body["stream"], False)

    def test_embedding_carries_keep_alive_and_no_num_ctx(self):
        for on in (False, True):
            with self.subTest(external=on), external(on), FakeOllama().serving() as fake:
                vector = asyncio.run(ollama_client.embed("a question"))
                self.assertEqual(vector, [0.1, 0.2, 0.3])
                [body] = fake.bodies("/api/embeddings")
                self.assertEqual(body, {"model": settings.embed_model, "prompt": "a question",
                                        "keep_alive": settings.ollama_keep_alive})
                self.assertNotIn("options", body)
                self.assertEqual(fake.bodies("/api/embed"), [])

    def test_a_404_from_embeddings_falls_back_to_embed_for_good(self):
        with FakeOllama(embeddings_gone=True).serving() as fake, self.assertLogs(
                "services.ollama_client", "WARNING") as logs:
            self.assertEqual(asyncio.run(ollama_client.embed("one")), [0.4, 0.5, 0.6])
            self.assertEqual(asyncio.run(ollama_client.embed("two")), [0.4, 0.5, 0.6])
        self.assertEqual(len(fake.bodies("/api/embeddings")), 1, "the older endpoint is asked once")
        self.assertEqual(fake.bodies("/api/embed"), [
            {"model": settings.embed_model, "input": "one", "keep_alive": settings.ollama_keep_alive},
            {"model": settings.embed_model, "input": "two", "keep_alive": settings.ollama_keep_alive}])
        self.assertEqual(len(logs.records), 1)
        self.assertIn("using /api/embed", logs.output[0])

    def test_another_error_is_not_a_fallback(self):
        def failing(request):
            return httpx.Response(500)
        fake = FakeOllama()
        fake.__call__ = failing
        with patch.object(httpx, "AsyncClient",
                          lambda *a, **k: REAL_ASYNC_CLIENT(*a, **{**k, "transport": httpx.MockTransport(failing)})):
            with self.assertRaises(httpx.HTTPStatusError):
                asyncio.run(ollama_client.embed("x"))
        self.assertIs(ollama_client._use_embed, False)

    def test_only_reads_besides_chat_and_embeddings(self):
        with external(), FakeOllama().serving() as fake:
            asyncio.run(ollama_client.check_health())
            asyncio.run(ollama_client.loaded_models())
            asyncio.run(ollama_client.missing_models())
            asyncio.run(ollama_client.version())
        self.assertEqual({(m, p) for m, p, _ in fake.requests},
                         {("GET", "/api/tags"), ("GET", "/api/version"), ("GET", "/api/ps")})


class ModelCheckTests(unittest.TestCase):
    def test_the_check_is_tag_exact(self):
        with patch.object(settings, "llm_model", "llama3.1:8b"), \
                patch.object(settings, "build_llm_model", ""), patch.object(settings, "answer_llm_model", ""), \
                FakeOllama(models=["llama3.1:70b", "nomic-embed-text"]).serving():
            result = asyncio.run(ollama_client.check_health())
            self.assertEqual(result["llm"]["status"], "error")
            self.assertEqual(result["build_llm"]["status"], "error")
            self.assertEqual(result["embed"]["status"], "ok")
            self.assertEqual(asyncio.run(ollama_client.missing_models()), ["llama3.1:8b"])

    def test_a_name_without_a_tag_is_latest(self):
        with patch.object(settings, "embed_model", "nomic-embed-text"), \
                FakeOllama(models=["nomic-embed-text:v1.5", settings.answer_model, settings.build_model]).serving():
            result = asyncio.run(ollama_client.check_health())
        self.assertEqual(result["embed"]["status"], "error")

    def test_required_models_are_distinct_and_tagged(self):
        with patch.object(settings, "embed_model", "nomic-embed-text"), \
                patch.object(settings, "llm_model", "llama3.1:8b"), \
                patch.object(settings, "build_llm_model", ""), patch.object(settings, "answer_llm_model", "llama3.1:8b"):
            self.assertEqual(ollama_client.required_models(), ["nomic-embed-text:latest", "llama3.1:8b"])

    def test_missing_models_is_none_when_unreachable(self):
        with FakeOllama(down=True).serving():
            self.assertIsNone(asyncio.run(ollama_client.missing_models()))
            self.assertIsNone(asyncio.run(ollama_client.version()))


class HealthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app, headers=auth_testing.headers(), raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def details(self, fake):
        with fake.serving(), patch.object(wc, "check_health", AsyncMock(return_value=True)), \
                patch.object(system_info, "_read_mem_total_gb", return_value=11.0):
            return self.client.get("/health/details")

    def test_the_version_is_reported_in_both_modes(self):
        for on in (False, True):
            with self.subTest(external=on), external(on):
                response = self.details(FakeOllama(version="0.40.18"))
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["services"]["ollama"]["version"], "0.40.18")

    def test_an_unreadable_version_is_null_and_decides_nothing(self):
        fake = FakeOllama(version=None)
        response = self.details(fake)
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["services"]["ollama"]["version"])

    def test_container_mode_keeps_the_memory_advice(self):
        with external(False):
            body = self.details(FakeOllama()).json()
        self.assertIn("recommended_minimum_gb", body["resources"]["memory"])
        self.assertNotIn("ollama_loaded", body["resources"])

    def test_external_mode_reports_what_ollama_has_loaded_without_the_advice(self):
        loaded = [{"name": "someone-elses:7b", "model": "someone-elses:7b", "size": 5, "size_vram": 5,
                   "expires_at": "2026-11-26T22:00:00Z", "digest": "abc"}]
        with external():
            response = self.details(FakeOllama(loaded=loaded))
        self.assertEqual(response.status_code, 200, response.text)
        resources = response.json()["resources"]
        self.assertEqual(resources["memory"]["status"], "external")
        self.assertNotIn("recommended_minimum_gb", resources["memory"])
        self.assertNotIn("allocated_gb", resources["memory"])
        self.assertEqual(resources["ollama_loaded"], [
            {"name": "someone-elses:7b", "size": 5, "size_vram": 5, "expires_at": "2026-11-26T22:00:00Z"}])

    def test_an_unreadable_ps_is_a_note_not_a_failure(self):
        with external():
            response = self.details(FakeOllama(ps_fails=True))
        self.assertEqual(response.status_code, 200)
        resources = response.json()["resources"]
        self.assertIsNone(resources["ollama_loaded"])
        self.assertIn("/api/ps", resources["ollama_loaded_note"])

    def test_a_missing_model_is_still_503_in_external_mode(self):
        with external():
            response = self.details(FakeOllama(models=[settings.embed_model]))
            public = None
            with FakeOllama(models=[settings.embed_model]).serving(), \
                    patch.object(wc, "check_health", AsyncMock(return_value=True)):
                public = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(public.status_code, 503)
        self.assertEqual(public.json(), {"status": "degraded"})


class StartupLogTests(unittest.TestCase):
    def run_check(self, fake):
        with fake.serving(), self.assertLogs("services.ollama_startup", "WARNING") as logs:
            asyncio.run(ollama_startup.log_missing_models())
        return [record.getMessage() for record in logs.records]

    def test_external_mode_names_the_pull_on_the_ollama_host(self):
        with external(), patch.object(settings, "llm_model", "llama3.1:8b"), \
                patch.object(settings, "build_llm_model", "qwen2.5:7b"), patch.object(settings, "answer_llm_model", ""):
            lines = self.run_check(FakeOllama(models=[settings.embed_model]))
        self.assertEqual(lines, [
            "Ollama model missing: llama3.1:8b. On the Ollama host, run: ollama pull llama3.1:8b",
            "Ollama model missing: qwen2.5:7b. On the Ollama host, run: ollama pull qwen2.5:7b"])

    def test_container_mode_names_the_container(self):
        with external(False):
            lines = self.run_check(FakeOllama(models=[settings.answer_model, settings.build_model]))
        ref = tagged(settings.embed_model)
        self.assertEqual(lines, [f"Ollama model missing: {ref}. Run: docker compose exec ollama ollama pull {ref}"])

    def test_unreachable_is_one_line(self):
        with external():
            lines = self.run_check(FakeOllama(down=True))
        self.assertEqual(len(lines), 1)
        self.assertIn("could not be reached", lines[0])
        self.assertIn("scripts/check-external-ollama.sh", lines[0])

    def test_nothing_missing_logs_nothing(self):
        with FakeOllama().serving(), patch.object(ollama_startup._log, "warning") as warning:
            asyncio.run(ollama_startup.log_missing_models())
        warning.assert_not_called()


class ImportTests(unittest.TestCase):
    """S5.1: check, never install, never touch a store."""

    def setUp(self):
        self.pkg = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.pkg, ignore_errors=True))
        self.manifest = {"embedding": {"model": settings.embed_model}}
        # Any store access fails the test.
        self.guards = [patch.object(model_bundle, name, side_effect=AssertionError(name + " was called"))
                       for name in ("installed_state", "install_model", "is_installed", "export_model")]
        for guard in self.guards:
            guard.start()
            self.addCleanup(guard.stop)

    def ensure(self, fake):
        with external(), fake.serving():
            return importer._ensure_models(self.pkg, self.manifest)

    def test_present_models_are_notes(self):
        notes = self.ensure(FakeOllama())
        self.assertIn(f"model '{tagged(settings.embed_model)}' reported by the external Ollama", notes)
        self.assertIn(f"model '{tagged(settings.answer_model)}' reported by the external Ollama", notes)

    def test_a_missing_embedding_model_is_refused_with_the_pull(self):
        ref = tagged(settings.embed_model)
        with self.assertRaises(PackageError) as caught:
            self.ensure(FakeOllama(models=[settings.answer_model]))
        self.assertEqual(caught.exception.code, "EMBEDDING_MODEL_MISSING")
        self.assertIn(f"On the Ollama host, run `ollama pull {ref}`", str(caught.exception))

    def test_a_bundled_package_installs_nothing(self):
        folder = self.pkg / "models" / "nomic-embed-text" / "latest"
        folder.mkdir(parents=True)
        (folder / "manifest.json").write_text("{}")
        ref = tagged(settings.embed_model)
        with patch.object(model_bundle, "bundled_models", return_value=[ref]):
            with self.assertRaises(PackageError) as caught:
                self.ensure(FakeOllama(models=[settings.answer_model]))
            self.assertEqual(caught.exception.code, "EMBEDDING_MODEL_MISSING")
            notes = self.ensure(FakeOllama())
        self.assertIn("the package bundles models; with an external Ollama they are not installed", notes)

    def test_a_missing_answer_model_is_a_note(self):
        notes = self.ensure(FakeOllama(models=[settings.embed_model]))
        ref = tagged(settings.answer_model)
        self.assertIn(f"model '{ref}' is absent from the external Ollama; pull it there with "
                      f"`ollama pull {ref}` before querying", notes)

    def test_unreachable_is_import_failed(self):
        with self.assertRaises(PackageError) as caught:
            self.ensure(FakeOllama(down=True))
        self.assertEqual(caught.exception.code, "IMPORT_FAILED")
        self.assertIn("scripts/check-external-ollama.sh", str(caught.exception))

    def test_the_store_is_never_available_in_external_mode(self):
        with tempfile.TemporaryDirectory() as store:
            (Path(store) / "models").mkdir()
            with patch.object(settings, "ollama_models_dir", store):
                with external(False):
                    self.assertTrue(model_bundle.store_available())
                with external():
                    self.assertFalse(model_bundle.store_available())


class ExportTests(unittest.TestCase):
    def setUp(self):
        audit = tempfile.TemporaryDirectory()
        self.addCleanup(audit.cleanup)
        directory = patch.object(settings, "audit_dir", audit.name)
        directory.start()
        self.addCleanup(directory.stop)
        self.client = TestClient(app, headers=auth_testing.headers(), raise_server_exceptions=False)
        self.addCleanup(self.client.close)

    def test_export_with_models_is_refused_in_external_mode(self):
        with external(), patch.object(wc, "collection_exists", AsyncMock(side_effect=AssertionError("asked"))):
            response = self.client.post("/export", json={"collection": "Anything", "include_models": True,
                                                         "encryption": "none"})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "MODELS_NOT_EXPORTABLE")
        self.assertIn("Export without models", response.json()["error"]["message"])

    def test_without_models_it_goes_on_as_before(self):
        with external(), patch.object(wc, "collection_exists", AsyncMock(return_value=False)):
            response = self.client.post("/export", json={"collection": "Missing", "include_models": False,
                                                         "encryption": "none"})
        self.assertNotEqual(response.json().get("error", {}).get("code"), "MODELS_NOT_EXPORTABLE")

    def test_container_mode_doesnt_refuse(self):
        with external(False), patch.object(wc, "collection_exists", AsyncMock(return_value=False)):
            response = self.client.post("/export", json={"collection": "Missing", "include_models": True,
                                                         "encryption": "none"})
        self.assertNotEqual(response.json().get("error", {}).get("code"), "MODELS_NOT_EXPORTABLE")


class OllamaCheckTests(unittest.TestCase):
    def run_check(self, fake, on=True):
        out = io.StringIO()
        with external(on), fake.serving():
            with httpx.Client(timeout=5) as client:
                status = ollama_check.run(client, out)
        return status, out.getvalue().splitlines()

    def test_all_good_is_0(self):
        status, lines = self.run_check(FakeOllama(version="0.40.18"))
        self.assertEqual(status, 0, lines)
        self.assertTrue(all(line.startswith("ollama-check: ok: ") for line in lines), lines)
        self.assertIn("ollama-check: ok: reached Ollama 0.40.18 at http://ollama:11434"
                      if settings.ollama_host == "ollama" else "reached Ollama 0.40.18", "\n".join(lines))

    def test_a_missing_model_names_its_pull(self):
        status, lines = self.run_check(FakeOllama(models=[settings.embed_model]))
        self.assertEqual(status, 1)
        ref = tagged(settings.answer_model)
        self.assertIn(f"ollama-check: problem: model {ref} is missing. On the Ollama host, run: ollama pull {ref}",
                      lines)

    def test_unreachable_is_1_with_the_platform_hint(self):
        status, lines = self.run_check(FakeOllama(down=True))
        self.assertEqual(status, 1)
        self.assertTrue(any("can't reach Ollama" in line and "docker0" in line for line in lines), lines)

    def test_mode_off_is_a_problem(self):
        status, lines = self.run_check(FakeOllama(), on=False)
        self.assertEqual(status, 1)
        self.assertIn("external Ollama mode is off", lines[0])

    def test_it_only_reads(self):
        fake = FakeOllama()
        self.run_check(fake)
        self.assertEqual({(m, p) for m, p, _ in fake.requests}, {("GET", "/api/version"), ("GET", "/api/tags")})


def compose_yaml(path):
    """A compose file, with the `!override` and `!reset` tags read as plain values."""
    class Loader(yaml.SafeLoader):
        pass

    def tagged_value(loader, tag_suffix, node):
        if isinstance(node, yaml.MappingNode):
            return loader.construct_mapping(node, deep=True)
        if isinstance(node, yaml.SequenceNode):
            return loader.construct_sequence(node, deep=True)
        return loader.construct_scalar(node)

    Loader.add_multi_constructor("!", tagged_value)
    return yaml.load((ROOT / path).read_text(), Loader=Loader)


class ComposeFileTests(unittest.TestCase):
    def test_the_overlay(self):
        overlay = compose_yaml("docker-compose.external-ollama.yml")
        text = (ROOT / "docker-compose.external-ollama.yml").read_text()
        services = overlay["services"]
        self.assertEqual(set(overlay), {"services"})
        self.assertEqual(set(services), {"ollama", "weaviate", "api"})
        self.assertEqual(services["ollama"], {"profiles": ["contained-ollama"]})
        self.assertEqual(services["weaviate"], {"extra_hosts": ["ollama:host-gateway"]})
        api = services["api"]
        self.assertEqual(api["extra_hosts"], ["ollama:host-gateway"])
        self.assertEqual(api["environment"], {"RAG_EXTERNAL_OLLAMA": "true"})
        self.assertEqual(api["depends_on"], {"weaviate": {"condition": "service_healthy"}})
        self.assertIn("depends_on: !override", text)
        self.assertIn("volumes: !override", text)
        base = compose_yaml("docker-compose.yml")
        self.assertEqual(api["volumes"], [m for m in base["services"]["api"]["volumes"] if m != "ollama_models:/ollama"])
        self.assertIn("ollama_models:/ollama", base["services"]["api"]["volumes"])
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("0.0.0.0", code)

    def test_the_base_file_keeps_the_contained_default_and_sets_the_two_options(self):
        base = compose_yaml("docker-compose.yml")
        api = base["services"]["api"]["environment"]
        self.assertEqual((api["OLLAMA_HOST"], api["OLLAMA_PORT"]), ("ollama", 11434))
        self.assertEqual((api["OLLAMA_NUM_CTX"], api["OLLAMA_KEEP_ALIVE"]), ("2048", "5m"))
        self.assertNotIn("RAG_EXTERNAL_OLLAMA", api)
        self.assertNotIn("extra_hosts", base["services"]["api"])
        self.assertIn("ollama", base["services"])
        self.assertEqual(base["services"]["weaviate"]["environment"]["TEXT2VEC_OLLAMA_APIENDPOINT"],
                         "http://ollama:11434")
        # Nothing rag-docker ships sets an Ollama server's own settings (#673).
        for name in ("OLLAMA_NUM_PARALLEL", "OLLAMA_CONTEXT_LENGTH"):
            self.assertNotIn(name, (ROOT / "docker-compose.yml").read_text())
            self.assertNotIn(name, (ROOT / "docker-compose.external-ollama.yml").read_text())
        self.assertEqual(Settings(_env_file=None).ollama_num_ctx, int(api["OLLAMA_NUM_CTX"]))
        self.assertEqual(Settings(_env_file=None).ollama_keep_alive, api["OLLAMA_KEEP_ALIVE"])

    def test_listed_after_lan_and_query_only_it_keeps_their_settings(self):
        merged = {}
        for path in ("docker-compose.yml", "docker-compose.lan.yml", "docker-compose.query-only.yml",
                     "docker-compose.external-ollama.yml"):
            env = (compose_yaml(path)["services"].get("api") or {}).get("environment") or {}
            merged.update(env)
        self.assertEqual((merged["RAG_EXTERNAL_OLLAMA"], merged["RAG_QUERY_ONLY"], merged["RAG_LAN_MODE"]),
                         ("true", "true", "true"))


INSTALL_STUBS = {
    "docker": """#!/bin/bash
printf 'docker %s\\n' "$*" >> "$STUB_LOG"
if [ "$1 $2" = "compose config" ]; then
  echo '{"services": {"ollama": {"environment": {"LLM_MODEL": "llama3.1:8b", "BUILD_LLM_MODEL": "", "ANSWER_LLM_MODEL": ""}}}}'
fi
if [ "$1 $2" = "compose ps" ]; then printf 'a\\nb\\nc\\nd\\ne\\n' | head -n "${STUB_RUNNING:-5}"; fi
exit 0
""",
    "sleep": "#!/bin/bash\nexit 0\n",
}


class InstallOfflineTests(unittest.TestCase):
    def run_install(self, *args, models_tarball=True, running=5):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(folder, ignore_errors=True))
        bundle = folder / "bundle"
        (bundle / "offline").mkdir(parents=True)
        (bundle / "ollama").mkdir()
        for name in ("install-offline.sh", "ollama/compose-models.sh", "ollama/models.sh"):
            (bundle / name).write_text((ROOT / name).read_text())
        (bundle / "offline/images.tar.gz").write_text("x")
        if models_tarball:
            (bundle / "offline/ollama_models.tar.gz").write_text("x")
        stubs = folder / "bin"
        stubs.mkdir()
        for name, body in INSTALL_STUBS.items():
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)
        log = folder / "log"
        env = dict(os.environ, PATH=f"{stubs}{os.pathsep}{os.environ['PATH']}", STUB_LOG=str(log),
                   STUB_RUNNING=str(running))
        env.pop("COMPOSE_FILE", None)
        out = subprocess.run(["bash", str(bundle / "install-offline.sh"), *args], capture_output=True,
                             text=True, timeout=120, env=env)
        calls = log.read_text() if log.exists() else ""
        return out, calls

    def test_external_skips_the_weights_and_prints_the_pulls(self):
        out, calls = self.run_install("--external-ollama", models_tarball=False, running=4)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("tar xzf", calls)
        self.assertIn("docker compose up -d --no-build", calls)
        self.assertIn("ollama pull llama3.1:8b", out.stdout)
        self.assertIn("ollama pull nomic-embed-text", out.stdout)
        self.assertIn("bash scripts/check-external-ollama.sh", out.stdout)

    def test_without_it_the_weights_are_still_required_and_restored(self):
        out, _ = self.run_install(models_tarball=False)
        self.assertEqual(out.returncode, 1)
        self.assertIn("offline/ollama_models.tar.gz missing", out.stderr)
        out, calls = self.run_install()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("tar xzf /backup/ollama_models.tar.gz", calls)
        self.assertNotIn("ollama pull", out.stdout)

    def test_an_unknown_option_is_a_usage_error(self):
        out, _ = self.run_install("--nope")
        self.assertEqual(out.returncode, 2)
        self.assertIn("Usage: bash install-offline.sh [--telemetry] [--external-ollama]", out.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
