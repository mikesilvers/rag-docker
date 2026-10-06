"""Untrusted retained-source identities never select filesystem paths."""
import asyncio
import hashlib
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from config import settings
from services import importer, ingest_pipeline, packager, sources
from services.packager import PackageError


class SourceIndexBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source_patch = patch.object(settings, "sources_dir", str(self.root / "retained"))
        self.source_patch.start()
        self.addCleanup(self.source_patch.stop)
        self.package = self.root / "package"
        (self.package / "sources").mkdir(parents=True)
        self.outside = self.root / "outside.txt"
        self.outside.write_text("controlled outside sentinel")

    def index(self, digest):
        return {"version": 1, "documents": {
            digest: {"filenames": ["document.txt"], "size": 10}}}

    def test_valid_content_addressed_source_round_trips(self):
        content = b"retained source"
        digest = hashlib.sha256(content).hexdigest()
        (self.package / "sources" / digest).write_bytes(content)
        (self.package / "sources" / "index.json").write_text(json.dumps(self.index(digest)))
        importer._validate_package_sources(self.package, {"fidelity": "with-sources"})
        retained = sources.collection_dir("Valid")
        retained.mkdir(parents=True)
        (retained / digest).write_bytes(content)
        (retained / "index.json").write_text(json.dumps(self.index(digest)))
        self.assertEqual(sources.load_index("Valid")["documents"].keys(), {digest})
        self.assertEqual(sources.blob_path("Valid", digest).read_bytes(), content)

    def test_import_rejects_paths_before_restoring_sources(self):
        for key in (str(self.outside), "../outside.txt", "a/b", "index.json"):
            with self.subTest(key=key):
                (self.package / "sources" / "index.json").write_text(json.dumps(self.index(key)))
                with self.assertRaises(PackageError) as raised:
                    importer._validate_package_sources(self.package, {"fidelity": "with-sources"})
                self.assertEqual(raised.exception.code, "PACKAGE_CORRUPT")
                self.assertFalse(sources.collection_dir("Imported").exists())

    def test_import_job_rejects_index_before_backend_or_model_work(self):
        (self.package / "sources" / "index.json").write_text(
            json.dumps(self.index(str(self.outside))))
        check_embedding = Mock()
        ensure_models = Mock()
        backend = Mock()
        job = {"status": "queued"}
        with patch.object(settings, "upload_dir", str(self.root)), \
             patch.object(importer, "_jobs", {"owned": job}), \
             patch.object(importer, "_active", {"owned.tar.gz"}), \
             patch.object(importer.packager, "exports_dir", return_value=self.root), \
             patch.object(importer.packager, "open_package", return_value=(
                 self.package, {"fidelity": "with-sources"})), \
             patch.object(importer.packager, "verify_digests"), \
             patch.object(importer, "_check_embedding", check_embedding), \
             patch.object(importer, "_ensure_models", ensure_models), \
             patch.object(importer.wc, "get_client", backend):
            importer._run("owned", "owned.tar.gz", "replace")
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error_code"], "PACKAGE_CORRUPT")
        check_embedding.assert_not_called()
        ensure_models.assert_not_called()
        backend.assert_not_called()

    def test_export_rejects_outside_identity_and_link(self):
        retained = sources.collection_dir("Imported")
        retained.mkdir(parents=True)
        (retained / "index.json").write_text(json.dumps(self.index(str(self.outside))))
        with self.assertRaises(ValueError):
            sources.load_index("Imported")
        digest = hashlib.sha256(self.outside.read_bytes()).hexdigest()
        (retained / digest).symlink_to(self.outside)
        (retained / "index.json").write_text(json.dumps(self.index(digest)))
        with self.assertRaises(ValueError):
            sources.blob_path("Imported", digest)
        (retained / "index.json").unlink()
        (retained / "index.json").symlink_to(self.outside)
        with self.assertRaises(ValueError):
            sources.load_index("Imported")
        (retained / "index.json").unlink()
        (retained / "index.json").write_text(json.dumps(self.index(digest)))
        with patch.object(packager, "exports_dir", return_value=self.root), \
             patch.object(packager, "read_chunks", return_value=iter(())), \
             patch.object(packager.wc, "_collection_config_sync", return_value={"embedding_model": settings.embed_model}), \
             patch.object(packager, "_ingest_config", return_value=None), \
             patch.object(packager.retrieval_config, "resolve", return_value=({}, True)), \
             patch.object(packager, "_goldstandard_sessions", return_value=[]):
            with self.assertRaises(ValueError):
                packager.build("Imported")
        self.assertFalse(list(self.root.glob("*.tar.gz")))


    def test_validate_index_rejects_malformed_shapes(self):
        digest = "a" * 64
        good = {"filenames": ["document.txt"]}
        for bad in ([], {"documents": []}, {"documents": {digest: "entry"}},
                    {"documents": {digest: {"filenames": "document.txt"}}},
                    {"documents": {digest: {"filenames": [1]}}},
                    {"documents": {digest.upper(): good}},
                    {"documents": {digest + "\n": good}}):
            with self.subTest(index=bad):
                with self.assertRaises(ValueError):
                    sources.validate_index(bad)
        self.assertEqual(sources.validate_index({"documents": {digest: good}})["documents"].keys(),
                         {digest})

    def test_import_rejects_missing_or_mismatched_sources(self):
        bare = self.root / "bare"
        bare.mkdir()
        importer._validate_package_sources(bare, {"fidelity": "chunks-only"})
        with self.assertRaises(PackageError) as raised:
            importer._validate_package_sources(bare, {"fidelity": "with-sources"})
        self.assertEqual(raised.exception.code, "PACKAGE_CORRUPT")
        digest = hashlib.sha256(b"retained source").hexdigest()
        (self.package / "sources" / "index.json").write_text(json.dumps(self.index(digest)))
        for blob in (None, b"different bytes"):
            with self.subTest(blob=blob):
                path = self.package / "sources" / digest
                if blob is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(blob)
                with self.assertRaises(PackageError) as raised:
                    importer._validate_package_sources(self.package, {"fidelity": "with-sources"})
                self.assertEqual(raised.exception.code, "PACKAGE_CORRUPT")

    def test_blob_path_refuses_linked_collection_directory(self):
        content = self.outside.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / digest).write_bytes(content)
        Path(settings.sources_dir).mkdir(parents=True)
        sources.collection_dir("Linked").symlink_to(elsewhere, target_is_directory=True)
        with self.assertRaises(ValueError):
            sources.blob_path("Linked", digest)

    def test_export_omits_missing_blob_from_shipped_index(self):
        content = b"available source"
        available = hashlib.sha256(content).hexdigest()
        missing = hashlib.sha256(b"missing source").hexdigest()
        retained = sources.collection_dir("Exported")
        retained.mkdir(parents=True)
        (retained / available).write_bytes(content)
        (retained / "index.json").write_text(json.dumps({
            "version": 1, "documents": {
                available: {"filenames": ["available.txt"]},
                missing: {"filenames": ["missing.txt"]},
            }}))
        with patch.object(packager, "exports_dir", return_value=self.root), \
             patch.object(packager, "read_chunks", return_value=iter(())), \
             patch.object(packager.wc, "_collection_config_sync", return_value={"embedding_model": settings.embed_model}), \
             patch.object(packager.wc, "_meta_sync", return_value={}), \
             patch.object(packager, "_ingest_config", return_value=None), \
             patch.object(packager.retrieval_config, "resolve", return_value=({}, True)), \
             patch.object(packager.retrieval_config, "validate", return_value={}), \
             patch.object(packager, "_goldstandard_sessions", return_value=[]):
            result = packager.build("Exported")
        self.assertEqual(result["source_document_count"], 1)
        self.assertTrue(any("missing on disk" in warning for warning in result["warnings"]))
        with tarfile.open(self.root / result["filename"]) as archive:
            names = archive.getnames()
            index_name = next(name for name in names if name.endswith("/sources/index.json"))
            index = json.load(archive.extractfile(index_name))
            self.assertEqual(set(index["documents"]), {available})
            self.assertTrue(any(name.endswith(f"/sources/{available}") for name in names))
            self.assertFalse(any(name.endswith(f"/sources/{missing}") for name in names))

    def test_tune_options_reports_invalid_index_as_typed_error(self):
        from routers import tuning as tuning_router
        retained = sources.collection_dir("Invalid")
        retained.mkdir(parents=True)
        (retained / "index.json").write_text(json.dumps(self.index("../outside.txt")))
        with patch.object(tuning_router.wc, "collection_exists", new_callable=AsyncMock,
                          return_value=True):
            response = asyncio.run(tuning_router.tune_options("Invalid"))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(json.loads(response.body)["error"]["code"], "SOURCE_INDEX_INVALID")

    def test_ingest_keeps_stored_chunks_when_source_index_is_invalid(self):
        upload = self.root / "accepted.txt"
        upload.write_text("accepted text")
        job_id = "source-index-retention-error"
        job = {"status": "queued", "files_total": 1, "files_completed": 0,
               "files_failed": 0, "chunks_stored": 0, "errors": []}
        with patch.dict(ingest_pipeline._jobs, {job_id: job}, clear=True), \
             patch.object(ingest_pipeline, "_parse_file", return_value=("accepted text", [])), \
             patch.object(ingest_pipeline.wc, "_insert_chunks_sync") as insert, \
             patch.object(ingest_pipeline.sources, "store", side_effect=ValueError("Invalid retained source index")):
            ingest_pipeline._process_job_sync(job_id, [upload], self.root,
                                              "Invalid", "fixed", 150, 0, 0.85, 0)
        insert.assert_called_once()
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"],
                          job["chunks_stored"]), ("completed", 1, 0, 1))


if __name__ == "__main__":
    unittest.main()
