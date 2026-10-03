"""Controlled package preflight and generated-script trust-boundary regressions."""
import ast
import json
import os
import sys
import tarfile
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2] / 'api'))
from config import settings
from models.schemas import SaveRetrievalConfigBody
from services import importer, packager, retrieval_config


INVALID = [None, [], 3, 'settings', {'top_k': '5; injected = True'},
           {'retrieval_mode': 'hybrid"; injected = True #'},
           {'response_format': 'engineer"; injected = True #'},
           {'top_k': True}, {'top_k': 0}, {'top_k': 51}, {'top_k': 1.5},
           {'alpha': False}, {'alpha': -0.1}, {'alpha': 1.1},
           {'alpha': float('nan')}, {'alpha': float('inf')},
           {'ef': True}, {'ef': 15}, {'ef': 513}]


class RetrievalImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pkg = self.root / 'package'
        self.pkg.mkdir()
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir()
        self.exports = self.root / 'exports'
        self.exports.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in [('upload_dir', self.uploads), ('exports_dir', self.exports),
                           ('sources_dir', self.root / 'sources')]:
            self.stack.enter_context(patch.object(settings, key, str(value)))
        self.stack.enter_context(patch.object(retrieval_config, '_DIR', None))
        self.stack.enter_context(patch.dict(importer._jobs, {}, clear=True))
        self.stack.enter_context(patch.object(importer, '_log'))

    def archive(self, data):
        (self.pkg / 'retrieval_config.json').write_text(data)
        (self.pkg / 'chunks.jsonl').write_text('')
        manifest = {'package_format': 1, 'collection': {'name': 'Corpus', 'chunk_count': 0},
                    'embedding': {'model': settings.embed_model},
                    'files': {p.name: 'sha256:' + packager.sha256_file(p)
                              for p in self.pkg.iterdir() if p.name != 'manifest.json'}}
        (self.pkg / 'manifest.json').write_text(json.dumps(manifest))
        with tarfile.open(self.exports / 'fixture.tar.gz', 'w:gz') as tar:
            tar.add(self.pkg, arcname='package')

    def test_digest_valid_bad_settings_refused_before_any_live_mutation(self):
        original = retrieval_config.validate({'top_k': 9}, 'Corpus')
        retrieval_config.save(original)
        saved = retrieval_config._path('Corpus')
        before = saved.read_bytes()
        for data in [json.dumps(value) for value in INVALID] + ['{']:
            self.archive(data)
            for conflict in ('abort', 'rename', 'replace'):
                with self.subTest(data=data, conflict=conflict), ExitStack() as mocks:
                    operations = [mocks.enter_context(patch.object(module, name)) for module, name in [
                        (importer, '_ensure_models'), (importer, '_build'),
                        (importer.wc, '_collection_exists_sync'), (importer.wc, '_delete_collection_sync'),
                        (importer.wc, 'get_client'), (importer, '_restore_sidecars'),
                        (importer, '_mark_started'), (importer.collection_recovery, 'begin')]]
                    importer._jobs['test'] = {'status': 'queued'}
                    importer._run('test', 'fixture.tar.gz', conflict)
                    result = importer._jobs['test']
                    self.assertEqual(result['status'], 'failed')
                    self.assertEqual(result['error_code'], 'PACKAGE_CORRUPT')
                    self.assertEqual(result['error_detail'], {'file': 'retrieval_config.json'})
                    for operation in operations:
                        operation.assert_not_called()
                    self.assertEqual(saved.read_bytes(), before)
                    self.assertFalse((self.root / 'sources').exists())
                    self.assertFalse(list(self.uploads.glob('import-*')))

    def test_valid_historical_defaults_coercion_and_ef_match_api_and_roundtrip(self):
        for data in [{}, {'collection': 'OldName', 'top_k': '7', 'alpha': '0.5', 'ef': '64'},
                     {'retrieval_mode': 'flat', 'top_k': 50, 'alpha': 0, 'ef': 512,
                      'response_format': 'engineer', 'legacy_field': 'ignored'}]:
            with self.subTest(data=data):
                self.archive(json.dumps(data))
                expected = SaveRetrievalConfigBody.model_validate({**data, 'collection': 'Corpus'}).model_dump()
                with patch.object(importer, '_ensure_models', return_value=[]), \
                     patch.object(importer.wc, '_collection_exists_sync', return_value=False), \
                     patch.object(importer, '_build', return_value=0):
                    importer._jobs['test'] = {'status': 'queued'}
                    importer._run('test', 'fixture.tar.gz', 'abort')
                self.assertEqual(importer._jobs['test']['status'], 'completed')
                self.assertEqual(retrieval_config.load('Corpus'), expected)

    def test_restore_uses_preflight_snapshot_and_rebinds_renamed_collection(self):
        self.archive(json.dumps({'top_k': '8', 'ef': 64}))
        validated = importer._read_retrieval_config(self.pkg, 'Corpus')
        (self.pkg / 'retrieval_config.json').write_text('{')
        importer._restore_sidecars('Renamed', self.pkg, 'Corpus', [], validated_retrieval=validated)
        self.assertEqual(retrieval_config.load('Renamed'), {**validated, 'collection': 'Renamed'})
        self.assertEqual(validated['collection'], 'Corpus')

    def test_absent_settings_remain_optional_and_nonregular_settings_fail(self):
        self.assertIsNone(importer._read_retrieval_config(self.pkg, 'Corpus'))
        (self.pkg / 'retrieval_config.json').mkdir()
        with self.assertRaises(packager.PackageError):
            importer._read_retrieval_config(self.pkg, 'Corpus')

    def test_saved_invalid_settings_cannot_generate_script(self):
        for data in INVALID:
            with self.subTest(data=data), self.assertRaises(ValueError):
                packager._render_retrieve('Corpus', data, {})

    def test_generated_script_defaults_are_literals_and_metadata_stays_data(self):
        malicious = '\"\"\"\nraise RuntimeError("injected")\n#\\\n@@TOP_K@@'
        cfg = {'retrieval_mode': 'hybrid', 'top_k': '8', 'alpha': '0.25',
               'response_format': 'engineer', 'ef': 64, 'unknown': malicious}
        source = packager._render_retrieve(malicious, cfg, {'embed_model': malicious})
        tree = ast.parse(source)
        literals = {node.targets[0].id: ast.literal_eval(node.value)
                    for node in tree.body if isinstance(node, ast.Assign)}
        self.assertEqual(literals['COLLECTION'], malicious)
        self.assertEqual(literals['PACKAGE_METADATA'], {'embed_model': malicious})
        self.assertEqual(literals['DEFAULT_TOP_K'], 8)
        self.assertIs(type(literals['DEFAULT_TOP_K']), int)
        self.assertEqual(literals['DEFAULT_ALPHA'], 0.25)
        namespace = {'__name__': 'retrieval_import_test'}
        exec(compile(tree, 'retrieve.py', 'exec'), namespace)
        args = namespace['build_parser']().parse_args(['a question'])
        self.assertEqual((args.mode, args.top_k, args.alpha, args.response_format),
                         ('hybrid', 8, 0.25, 'engineer'))

    def test_export_emits_normalized_settings_and_executable_defaults(self):
        saved = {'collection': 'Corpus', 'top_k': '7', 'alpha': '0.5', 'ef': 64}
        retrieval_config.save(saved)
        with patch.object(packager, 'read_chunks', return_value=[]), \
             patch.object(packager.wc, '_collection_config_sync', return_value={}), \
             patch.object(packager, '_ingest_config', return_value=None), \
             patch.object(packager, '_goldstandard_sessions', return_value=[]), \
             patch.object(packager.sources, 'load_index', return_value={'documents': {}}), \
             patch.object(packager.wc, '_meta_sync', return_value={'version': 'test'}):
            result = packager.build('Corpus')
        with tarfile.open(self.exports / result['filename']) as archive:
            members = {Path(m.name).name: m for m in archive.getmembers() if m.isfile()}
            cfg = json.load(archive.extractfile(members['retrieval_config.json']))
            source = archive.extractfile(members['retrieve.py']).read().decode()
        self.assertEqual(cfg, SaveRetrievalConfigBody.model_validate(saved).model_dump())
        namespace = {'__name__': 'retrieval_import_test'}
        exec(compile(source, 'retrieve.py', 'exec'), namespace)
        self.assertEqual(namespace['DEFAULT_TOP_K'], 7)
        self.assertEqual(namespace['DEFAULT_ALPHA'], 0.5)
        self.assertEqual(namespace['PACKAGE_METADATA']['package_filename'], result['filename'])

    def test_export_rejects_invalid_stored_settings_without_publishing_package(self):
        for value in INVALID:
            with self.subTest(value=value), ExitStack() as mocks:
                mocks.enter_context(patch.object(packager, 'read_chunks', return_value=[]))
                mocks.enter_context(patch.object(packager.wc, '_collection_config_sync', return_value={}))
                mocks.enter_context(patch.object(packager, '_ingest_config', return_value=None))
                mocks.enter_context(patch.object(retrieval_config, 'load', return_value=value))
                if value is None:  # None denotes no saved file, not an invalid saved config.
                    continue
                with self.assertRaises((ValueError, TypeError)):
                    packager.build('Corpus')
                self.assertEqual(list(self.exports.iterdir()), [])


if __name__ == '__main__':
    unittest.main()
