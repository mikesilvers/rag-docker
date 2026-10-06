"""The batch-recovery import fixture passes the importer's real package checks."""
import hashlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(ROOT / 'api'))
from services import importer, packager, sources


def load_fixture_module():
    spec = importlib.util.spec_from_file_location(
        'batch_recovery_fixture', ROOT / 'scripts/verify/batch_recovery.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ImportFixtureTests(unittest.TestCase):
    def test_import_fixture_passes_digest_and_source_checks(self):
        fixture = load_fixture_module()
        rows = [dict(id=f'00000000-0000-4000-8000-00000000000{i}', vector=[0.25, 0.5],
                     properties={'content': f'synthetic chunk {i}', 'source_file': 'synthetic.txt',
                                 'chunk_index': i, 'created_at': '2026-09-27T00:00:00Z'})
                for i in range(2)]
        with tempfile.TemporaryDirectory() as work:
            package = Path(work) / 'package'
            package.mkdir()
            manifest = fixture.write_import_package(package, 'VfyFixtureImport', rows, 2)

            self.assertEqual(manifest['fidelity'], 'with-sources')
            packager.verify_digests(package, manifest)
            importer._validate_package_sources(package, manifest)

            index = json.loads((package / 'sources' / sources.INDEX_NAME).read_text())
            self.assertEqual(len(index['documents']), 1)
            digest = next(iter(index['documents']))
            blob = (package / 'sources' / digest).read_bytes()
            self.assertTrue(blob)
            self.assertEqual(hashlib.sha256(blob).hexdigest(), digest)


if __name__ == '__main__':
    unittest.main()
