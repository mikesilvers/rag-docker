"""Untrusted retained-source identities never select filesystem paths."""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from config import settings
from services import importer, packager, sources
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
             patch.object(packager.wc, "_collection_config_sync", return_value={}), \
             patch.object(packager, "_ingest_config", return_value=None), \
             patch.object(packager.retrieval_config, "resolve", return_value=({}, True)), \
             patch.object(packager, "_goldstandard_sessions", return_value=[]):
            with self.assertRaises(ValueError):
                packager.build("Imported")
        self.assertFalse(list(self.root.glob("*.tar.gz")))


if __name__ == "__main__":
    unittest.main()
