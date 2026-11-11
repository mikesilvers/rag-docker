"""Text ingest makes no network call (issue #695).

Unstructured's text partitioning loads the spaCy model en_core_web_sm, and
downloaded it from GitHub on first use when the image didn't carry it, so a
.txt or .md ingest failed with no DNS. Unstructured also sends usage telemetry
unless DO_NOT_TRACK is set. This runs real ingest jobs, the real parse and
chunking, in a fresh container from the api image with no network:

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo \
      -e RAG_TEST_API_DIR=/repo/api <api image> python scripts/tests/test_offline_ingest.py

20_offline_model.sh runs it so on the verify project's api image. Every name
lookup and every connect on a non-Unix socket is recorded and refused, from
before the API is imported, so a dial-out whose error is swallowed is caught
too. Weaviate, source retention and gold-standard marking are in-memory fakes.
Besides .txt and .md, one file of each other supported type (.pdf, .docx, .csv,
.json, from scripts/verify/fixtures.py) is ingested the same way. On the image
before #695 the .txt, .md, .pdf and .docx jobs fail with "Failed to download
spaCy model".

Document preparation (#638) checks each prepared .txt or .md part with the same
parser, so a .txt and a .md are also prepared here, with a stub LLM, their
parts written and checked by the real Markdown parser (#640).
"""
import asyncio
import importlib.metadata
import os
import shutil
import socket
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

CALLS = []
_getaddrinfo = socket.getaddrinfo
_connect = socket.socket.connect
_connect_ex = socket.socket.connect_ex


def _refused(what):
    CALLS.append(what)
    return OSError("network call blocked by test_offline_ingest: {}".format(what))


def _guarded_getaddrinfo(host, *args, **kwargs):
    raise _refused("getaddrinfo {!r}".format(host))


def _guarded_connect(self, address):
    if self.family == socket.AF_UNIX:
        return _connect(self, address)
    raise _refused("connect {!r}".format(address))


def _guarded_connect_ex(self, address):
    if self.family == socket.AF_UNIX:
        return _connect_ex(self, address)
    raise _refused("connect_ex {!r}".format(address))


# Installed before any API module, so an import-time call is recorded too.
socket.getaddrinfo = _guarded_getaddrinfo
socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from config import settings  # noqa: E402
from services import goldstandard, ingest_pipeline, preparation  # noqa: E402
from services import weaviate_client as wc  # noqa: E402

# The text 16_api_paths.sh ingests in suite 01: twelve paragraphs, more than
# five chunks at fixed 150 with a minimum of 40.
TOPICS = ["overtime approval", "holiday requests", "expense claims", "travel booking",
          "remote work", "sick leave", "training budgets", "equipment loans",
          "parking permits", "visitor badges", "parental leave", "jury duty"]
OVERTIME = "".join(
    "The policy on {t} says a manager must approve every request about {t} "
    "before the end of the month, and the HR team records the decision.\n\n".format(t=t)
    for t in TOPICS)
MARKDOWN = (
    "# Leave policy\n\n"
    "Every request for leave is approved by a manager before it is taken.\n\n"
    "## Records\n\n"
    "The HR team records each decision and keeps it for six years.\n\n"
    "- Holiday requests go through the portal.\n"
    "- Sick leave is reported on the first day.\n")


class OfflineIngestTests(unittest.TestCase):
    def run_job(self, name, text, strategy="fixed", chunk_size=150, minimum=40, content=None):
        stored = []
        tmp = Path(tempfile.mkdtemp())  # the job removes it when it ends
        path = tmp / "0" / name
        path.parent.mkdir()
        if content is None:
            path.write_text(text, encoding="utf-8")
        else:
            path.write_bytes(content)
        job_id = "o" + uuid.uuid4().hex[:7]
        ingest_pipeline._jobs[job_id] = {
            "job_id": job_id, "status": "queued", "files_total": 1,
            "files_completed": 0, "files_failed": 0, "chunks_stored": 0,
            "chunks_submitted": 0, "chunks_accepted": 0, "chunks_replaced": 0,
            "errors": [], "skipped": [], "split": []}
        self.addCleanup(ingest_pipeline._jobs.pop, job_id, None)
        with patch.object(wc, "_insert_chunks_sync",
                          side_effect=lambda collection, chunks: stored.extend(chunks)), \
                patch.object(wc, "_source_has_chunks_sync", return_value=False), \
                patch.object(wc, "_delete_earlier_chunks_sync", return_value=0), \
                patch.object(wc, "_delete_new_family_sync", return_value=0), \
                patch.object(wc, "_collection_exists_sync", return_value=True), \
                patch.object(goldstandard, "mark_stale"), \
                patch.object(ingest_pipeline.sources, "store"):
            ingest_pipeline._process_job_sync(job_id, [path], tmp, "Coll", strategy, chunk_size,
                                              0, 0.85, minimum, None, {path: name})
        return ingest_pipeline._jobs[job_id], stored

    def assertNoNetwork(self):
        self.assertEqual(CALLS, [], "network calls were attempted")

    def test_environment(self):
        # The image's ENV turns unstructured's telemetry off, and the bundled
        # model is the version the locked unstructured would download.
        import unstructured.nlp.tokenize as tokenize
        self.assertEqual(os.environ.get("DO_NOT_TRACK"), "1")
        self.assertEqual(importlib.metadata.version("en_core_web_sm"), tokenize._SPACY_MODEL_VERSION)
        self.assertNoNetwork()

    def test_long_text_is_ingested_offline(self):
        job, stored = self.run_job("overtime.txt", OVERTIME)
        self.assertEqual(job["status"], "completed", job["errors"])
        self.assertEqual(job["errors"], [])
        self.assertGreater(len(stored), 5)
        self.assertEqual(job["chunks_stored"], len(stored))
        self.assertNoNetwork()

    def test_markdown_is_ingested_offline(self):
        job, stored = self.run_job("policy.md", MARKDOWN)
        self.assertEqual(job["status"], "completed", job["errors"])
        self.assertEqual(job["errors"], [])
        self.assertGreater(len(stored), 0)
        self.assertNoNetwork()

    def test_every_supported_type_is_ingested_offline(self):
        # The suites' corpus (fixtures.py): one file of each other supported
        # type, so a dial-out outside text partitioning is caught too.
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "verify"))
        import fixtures
        corpus = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, corpus, True)
        fixtures.write(corpus)
        for name in ("policies.pdf", "policies.docx", "policies.csv", "policies.json"):
            with self.subTest(name=name):
                job, stored = self.run_job(name, "", chunk_size=400, minimum=1,
                                           content=(corpus / name).read_bytes())
                self.assertEqual(job["status"], "completed", job["errors"])
                self.assertEqual(job["errors"], [])
                self.assertGreater(len(stored), 0)
        self.assertNoNetwork()

    def test_text_and_markdown_are_prepared_offline(self):
        # #640: a prepared part is checked with ingest's parser (preparation.
        # check_part -> ingest_pipeline._parse_file -> partition_md), the parser
        # #695 made offline. The LLM is a stub; nothing else is faked.
        async def chat(system, user):
            if system.startswith(preparation.CLEAN_SYSTEM):
                return '{"remove": []}'
            return '{"title": "Leave policy", "summary": "How leave is approved and recorded."}'

        async def models():
            return {settings.llm_model}

        for name, text in (("minutes.txt", OVERTIME), ("policy.md", MARKDOWN)):
            with self.subTest(name=name):
                folder = Path(tempfile.mkdtemp())
                self.addCleanup(shutil.rmtree, folder, True)
                raw = folder / name
                raw.write_text(text, encoding="utf-8")

                async def write_part(index, suffix, data, folder=folder):
                    path = folder / "{:03d}{}".format(index + 1, suffix)
                    path.write_bytes(data)
                    return path
                result = asyncio.run(preparation.prepare_document(
                    raw, name, Path(name).suffix, chat, models, lambda: False, write_part))
                self.assertEqual(len(result.parts), 1)
                [part] = result.parts
                self.assertEqual(part["title"], "Leave policy")
                self.assertGreater(part["characters"], len(text) // 2)
                self.assertEqual(part["fallbacks"], [])
        self.assertNoNetwork()


if __name__ == "__main__":
    unittest.main()
