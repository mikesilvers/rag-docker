"""Controlled final-flush faults against real writer and recovery services."""
import copy
import json
import os
import sys
import tempfile
import unittest
import uuid
import weakref
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR', str(Path(__file__).resolve().parents[2] / 'api')))
from config import settings
from services import batch_write, collection_recovery as recovery, importer, tuning, weaviate_client as wc


def records(n=2):
    return [dict(id=str(uuid.uuid4()), vector=[0.25, 0.5], properties={
        'content': f'chunk {i}', 'created_at': '2026-09-27T00:00:00Z'}) for i in range(n)]


class Batch:
    def __init__(self, collection):
        self.collection = collection
        self.failed_objects = []
        self.number_errors = 0

    def dynamic(self):
        self.failed_objects = []
        self.pending = []
        return self

    def __enter__(self):
        return self

    def add_object(self, properties, uuid, vector=None):
        self.pending.append(dict(id=str(uuid), properties=copy.deepcopy(properties),
                                 vector=copy.deepcopy(vector) if vector is not None else [0.25, 0.5]))

    def __exit__(self, *args):
        fault = self.collection.fault
        if fault == 'reject':
            self.failed_objects = [object()]
            return
        pending = self.pending[:-1] if fault == 'partial' else self.pending
        for record in pending:
            if fault == 'identities':
                record['id'] = str(uuid.uuid4())
            if fault == 'properties':
                record['properties']['content'] = 'wrong'
            if fault == 'invalid_vector':
                record['vector'] = []
            if fault == 'vector':
                record['vector'] = [9.0, 9.0]
            self.collection.rows[record['id']] = record
        if fault == 'extra':
            extra = records(1)[0]
            self.collection.rows[extra['id']] = extra


class Collection:
    def __init__(self, name, fault=None):
        self.name = name
        self.config = SimpleNamespace(get=lambda: SimpleNamespace(name=self.name, vectorizer=None))
        self.rows = {}
        self.fault = fault
        self.batch = Batch(self)
        self.query = SimpleNamespace(fetch_objects=lambda filters, **kwargs: SimpleNamespace(
            objects=[obj for obj in self.iterator(include_vector=True) if str(obj.uuid) in filters.value]))
        self.data = SimpleNamespace(delete_many=self.delete_many)
        self.aggregate = SimpleNamespace(over_all=lambda **kwargs: SimpleNamespace(total_count=len(self.rows)))

    def delete_many(self, where):
        for key in where.value:
            self.rows.pop(key, None)
        return SimpleNamespace(failed=0)

    def iterator(self, include_vector=False):
        if self.fault == 'read':
            raise OSError('backend unavailable')
        for row in self.rows.values():
            props = {'source_file': None, 'chunk_size': None, **copy.deepcopy(row['properties'])}
            if isinstance(props.get('created_at'), str):
                props['created_at'] = datetime.fromisoformat(props['created_at'].replace('Z', '+00:00'))
            yield SimpleNamespace(uuid=uuid.UUID(row['id']), properties=props,
                                  vector={'default': row['vector']})


class Collections:
    def __init__(self):
        self.items = {}
        self.deleted = []
        self.final_fault = None
        self.staging_fault = None
        self.create_failure = False
        self.interrupt = False

    def exists(self, name):
        return name in self.items

    def get(self, name):
        return self.items[name]

    def create(self, name, *args):
        if name == 'Corpus' and self.create_failure:
            raise RuntimeError('final create failed')
        fault = self.final_fault if name == 'Corpus' else self.staging_fault
        self.items[name] = Collection(name, fault)

    def delete(self, name):
        self.deleted.append(name)
        self.items.pop(name, None)
        if name == 'Corpus' and self.interrupt:
            raise KeyboardInterrupt('hard-kill boundary')


class WriterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        setting = patch.object(settings, 'upload_dir', self.temp.name)
        setting.start()
        self.addCleanup(setting.stop)

    def test_final_flush_rejection_is_observed_after_exit(self):
        col = Collection('Corpus', 'reject')
        with self.assertRaisesRegex(RuntimeError, 'rejected'):
            batch_write.insert(col, records())
        self.assertEqual(col.batch.number_errors, 0)

    def test_same_count_wrong_records_and_partial_writes_fail(self):
        for fault in ('partial', 'identities', 'properties', 'vector', 'extra'):
            with self.subTest(fault=fault), self.assertRaises(batch_write.BatchVerificationError):
                batch_write.insert(Collection('Corpus', fault), records())

    def test_duplicate_and_declared_count_fail_before_enqueue(self):
        row = records(1)[0]
        for rows, expected in (([row, row], 2), ([row], 2)):
            col = Collection('Corpus')
            with self.assertRaises(ValueError):
                batch_write.insert(col, rows, expected_count=expected)
            self.assertFalse(col.rows)
            self.assertFalse(hasattr(col.batch, 'pending'))

    def test_empty_vector_and_nonfinite_vector_fail_before_enqueue(self):
        for vector in ([], [float('nan')], [float('inf')], ['0.1']):
            col = Collection('Corpus')
            with self.assertRaises(ValueError):
                batch_write.insert(col, [{**records(1)[0], 'vector': vector}])
            self.assertFalse(col.rows)

    def test_additions_preserve_existing_objects_and_confirm_only_new_records(self):
        col = Collection('Corpus')
        batch_write.insert(col, records(1))
        batch_write.insert(col, [{'properties': {'content': 'new'}}], exact=False)
        self.assertEqual(len(col.rows), 2)

    def test_vectorless_storage_is_accepted_only_for_vectorizer_none(self):
        class VectorlessBatch(Batch):
            def add_object(self, properties, uuid, vector=None):
                self.pending.append(dict(id=str(uuid), properties=copy.deepcopy(properties),
                                         vector=None))

        col = Collection('TextOnly')
        col.batch = VectorlessBatch(col)
        row = [{'properties': {'content': 'inert text'}}]
        with self.assertRaises(batch_write.BatchVerificationError):
            batch_write.insert(col, row, exact=False)
        col.config = SimpleNamespace(get=lambda: SimpleNamespace(
            vectorizer=SimpleNamespace(value='none')))
        self.assertEqual(batch_write.insert(col, row, exact=False), 1)

    def test_ingestion_uses_completed_batch_verification(self):
        col = Collection('Corpus', 'reject')
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: col))
        with patch.object(wc, 'get_client', return_value=client), self.assertRaises(RuntimeError):
            wc._insert_chunks_sync('Corpus', [{'content': 'new'}])

    def test_generated_ingestion_failure_rolls_back_only_attempt_ids(self):
        for fault in ('partial', 'properties', 'invalid_vector', 'reject'):
            with self.subTest(fault=fault):
                col = Collection('Corpus')
                previous = records(1)
                batch_write.insert(col, previous)
                col.fault = fault
                client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: col))
                with patch.object(wc, 'get_client', return_value=client), self.assertRaises(RuntimeError):
                    wc._insert_chunks_sync('Corpus', [{'content': 'new 1'}, {'content': 'new 2'}])
                self.assertEqual(set(col.rows), {previous[0]['id']})

    def test_ingestion_cleanup_failure_preserves_original_error_and_reports_uncertainty(self):
        col = Collection('Corpus', 'partial')
        client = SimpleNamespace(collections=SimpleNamespace(get=lambda name: col))
        with patch.object(wc, 'get_client', return_value=client), \
                patch.object(col.data, 'delete_many', side_effect=OSError('cleanup outage')), \
                self.assertRaises(batch_write.BatchCleanupError) as error:
            wc._insert_chunks_sync('Corpus', [{'content': 'new 1'}, {'content': 'new 2'}])
        self.assertIsInstance(error.exception.original, batch_write.BatchVerificationError)
        self.assertIn('Accepted chunks may remain', str(error.exception))
        self.assertEqual(len(col.rows), 1)

    def test_reusable_stream_keeps_decoded_record_lifetime_bounded(self):
        live = weakref.WeakSet()
        class Record(dict):
            __hash__ = object.__hash__
        def stream():
            for i in range(500):
                record = Record(id=str(uuid.uuid5(uuid.NAMESPACE_OID, str(i))),
                                properties={'content': 'synthetic ' * 100}, vector=[0.25] * 32)
                live.add(record)
                self.assertLessEqual(len(live), 2)
                yield record
        col = Collection('Corpus')
        self.assertEqual(batch_write.insert(col, stream, expected_count=500), 500)



class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key in ('upload_dir', 'sources_dir', 'exports_dir'):
            path = self.root / key
            path.mkdir()
            self.stack.enter_context(patch.object(settings, key, str(path)))
        self.cols = Collections()
        self.cols.create('Corpus')
        self.original = records()
        batch_write.insert(self.cols.get('Corpus'), self.original)
        self.client = SimpleNamespace(collections=self.cols)
        self.stack.enter_context(patch.object(wc, 'get_client', return_value=self.client))
        self.stack.enter_context(patch.object(wc, '_create_collection_sync', side_effect=lambda name, *args, **kwargs: self.cols.create(name)))
        self.stack.enter_context(patch.object(wc, '_collection_config_sync', return_value={'index_type': 'hnsw', 'distance_metric': 'cosine'}))
        self.stack.enter_context(patch.dict(importer._jobs, {}, clear=True))
        self.stack.enter_context(patch.dict(tuning._jobs, {}, clear=True))
        self.stack.enter_context(patch.object(wc.ingest_config, '_DIR', None))
        self.stack.enter_context(patch.object(wc.retrieval_config, '_DIR', None))
        self.stack.enter_context(patch.object(importer, '_log'))
        self.stack.enter_context(patch.object(recovery, 'log'))
        self.stack.enter_context(patch.object(importer.goldstandard, 'sessions_for', return_value=[]))
        self.stack.enter_context(patch.object(importer.goldstandard, 'mark_orphaned', return_value=0))
        source = Path(settings.sources_dir) / 'Corpus'
        source.mkdir()
        (source / 'source-blob').write_bytes(b'original source')
        for kind in ('ingest', 'retrieval'):
            directory = Path(settings.upload_dir) / f'{kind}_configs'
            directory.mkdir()
            (directory / 'Corpus.json').write_text(json.dumps({'collection': 'Corpus', 'setting': kind}))
        directory = Path(settings.upload_dir) / 'goldstandard_sessions'
        directory.mkdir()
        (directory / 'gs_0123abcd.json').write_text(json.dumps({'collection': 'Corpus', 'pairs': ['preserve']}))

    def recovery_record(self):
        paths = list(recovery._root().glob('*.json'))
        self.assertEqual(len(paths), 1)
        record = json.loads(paths[0].read_text())
        self.assertEqual(record['state'], 'recovery')
        return record

    def assert_retained(self, record):
        name = record['staging']
        before = copy.deepcopy(self.cols.get(name).rows)
        recovery.sweep(self.client)
        self.assertEqual(self.cols.get(name).rows, before)
        self.assertEqual((Path(settings.sources_dir) / name / 'source-blob').read_bytes(), b'original source')
        metadata = recovery._root() / record['operation_id']
        self.assertTrue((metadata / 'goldstandard' / 'gs_0123abcd.json').is_file())
        for kind in ('ingest', 'retrieval'):
            self.assertEqual(json.loads((Path(settings.upload_dir) / f'{kind}_configs' / f'{name}.json').read_text())['collection'], name)

    def rebuild(self):
        return tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, None)

    def test_tuning_final_create_failure_retains_verified_data_and_sidecars(self):
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError) as error:
            self.rebuild()
        record = self.recovery_record()
        self.assertEqual(error.exception.detail['recovered_as'], record['staging'])
        self.assert_retained(record)

    def test_tuning_final_batch_and_verification_failures_retain_data(self):
        for fault in ('reject', 'partial', 'properties', 'vector'):
            with self.subTest(fault=fault):
                self.cols.final_fault = fault
                self.cols.items['Corpus'] = Collection('Corpus')
                with self.assertRaises(importer.PackageError):
                    self.rebuild()
                paths = list(recovery._root().glob('*.json'))
                record = json.loads(paths[-1].read_text())
                self.assert_retained(record)
                for p in paths:
                    recovery.discard(json.loads(p.read_text()), self.client)

    def test_restart_after_destructive_boundary_preserves_recovery(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.rebuild()
        self.assertFalse(self.cols.exists('Corpus'))
        self.assert_retained(self.recovery_record())

    def test_scratch_and_markerlike_unowned_names_are_distinguished(self):
        owned = recovery.begin('Corpus', 'tune', self.client)
        self.cols.create(owned['staging'])
        unowned = 'User__tuning_dataset'
        self.cols.create(unowned)
        old = 'Corpus__importing_1234abcd'
        self.cols.create(old)
        self.assertEqual(recovery.sweep(self.client), [owned['staging']])
        self.assertTrue(self.cols.exists(unowned))
        self.assertTrue(self.cols.exists(old))

    def test_corrupt_ownership_does_not_authorize_deletion(self):
        record = recovery.begin('Corpus', 'tune', self.client)
        self.cols.create(record['staging'])
        path = recovery._root() / (record['operation_id'] + '.json')
        path.write_text('{')
        self.assertEqual(recovery.sweep(self.client), [])
        self.assertTrue(path.exists())
        self.assertTrue(self.cols.exists(record['staging']))

    def test_snapshot_failure_preserves_original_and_cleans_owned_scratch(self):
        with patch.object(recovery, '_copy', side_effect=OSError('disk full')), self.assertRaises(OSError):
            self.rebuild()
        self.assertEqual(self.cols.get('Corpus').rows, {r['id']: r for r in self.original})
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertFalse(list(recovery._root().glob('*.json')))

    def test_failure_while_publishing_confirmation_preserves_named_recovery(self):
        def fail_progress(count):
            raise RuntimeError('confirmation callback failed')
        with self.assertRaises(importer.PackageError) as error:
            tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, fail_progress)
        record = self.recovery_record()
        self.assertEqual(error.exception.detail['recovered_as'], record['staging'])
        self.assert_retained(record)

    def test_success_reports_only_confirmed_target_and_removes_recovery(self):
        progress = []
        written = tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, progress.append)
        self.assertEqual(written, 2)
        self.assertEqual(progress, [2])
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertFalse(list(recovery._root().glob('*.json')))

    def package(self):
        pkg = self.root / 'package'
        pkg.mkdir(exist_ok=True)
        (pkg / 'chunks.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in self.original))
        source = pkg / 'sources'
        source.mkdir(exist_ok=True)
        (source / 'source-blob').write_bytes(b'original source')
        for kind in ('ingest', 'retrieval'):
            (pkg / f'{kind}_config.json').write_text(json.dumps({'collection': 'Corpus', 'setting': kind}))
        gold = pkg / 'goldstandard'
        gold.mkdir(exist_ok=True)
        (gold / 'gs_0123abcd.json').write_text(json.dumps({
            'session_id': 'gs_0123abcd', 'collection': 'Corpus', 'status': 'completed',
            'pairs_total': 1, 'pairs_completed': 1,
            'pairs': [{'pair_id': 'p_0123abcd', 'question': 'Question?',
                       'answer': 'Answer', 'ground_truth': 'Answer',
                       'contexts': ['Context'], 'source_file': 'source.txt',
                       'chunk_index': 0, 'status': 'approved'}]}))
        manifest = {'package_format': 1, 'collection': {'name': 'Corpus', 'chunk_count': 2},
                    'embedding': {'dimensions': 2, 'model': settings.embed_model}}
        (pkg / 'manifest.json').write_text(json.dumps(manifest))
        (pkg / 'collection.json').write_text('{}')
        return pkg, manifest

    def import_replace(self):
        pkg, manifest = self.package()
        importer._jobs['job'] = dict(chunks_written=0)
        with patch.object(importer.packager, 'open_package', return_value=(pkg, manifest)), \
                patch.object(importer.packager, 'verify_digests'), \
                patch.object(importer, '_check_embedding'), \
                patch.object(importer, '_ensure_models', return_value=[]):
            importer._run('job', 'fixture.tar.gz', 'replace')
        return importer._jobs['job']

    def test_replace_final_create_and_batch_failure_survive_restart(self):
        for fault in ('create', 'reject', 'partial', 'properties', 'vector'):
            with self.subTest(fault=fault):
                self.cols.create_failure = fault == 'create'
                self.cols.final_fault = None if fault == 'create' else fault
                self.cols.items['Corpus'] = Collection('Corpus')
                job = self.import_replace()
                self.assertEqual(job['status'], 'failed')
                self.assertEqual(job['chunks_written'], 0)
                record = self.recovery_record()
                self.assertEqual(job['error_detail']['recovered_as'], record['staging'])
                self.assertEqual(self.cols.get(record['staging']).rows, {r['id']: r for r in self.original})
                self.assert_retained(record)
                recovery.discard(record, self.client)

    def test_replace_restart_after_target_delete_preserves_staging(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.import_replace()
        self.assert_retained(self.recovery_record())

    def test_first_staging_batch_failure_preserves_original_for_import_and_tune(self):
        for operation in ('import', 'tune'):
            with self.subTest(operation=operation):
                self.cols.staging_fault = 'reject'
                before = copy.deepcopy(self.cols.get('Corpus').rows)
                if operation == 'import':
                    job = self.import_replace()
                    self.assertEqual(job['status'], 'failed')
                    self.assertNotIn('recovered_as', job.get('error_detail') or {})
                else:
                    with self.assertRaises(RuntimeError):
                        self.rebuild()
                self.assertEqual(self.cols.get('Corpus').rows, before)
                self.assertFalse(list(recovery._root().glob('*.json')))

    def test_chunks_only_recovery_remains_verified_without_sources(self):
        import shutil
        shutil.rmtree(Path(settings.sources_dir) / 'Corpus')
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError):
            self.rebuild()
        record = self.recovery_record()
        before = copy.deepcopy(self.cols.get(record['staging']).rows)
        recovery.sweep(self.client)
        self.assertEqual(self.cols.get(record['staging']).rows, before)
        self.assertEqual(len(before), len(self.original))

    def test_missing_recovery_backend_does_not_discard_retained_sidecars(self):
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError):
            self.rebuild()
        record = self.recovery_record()
        self.cols.items.pop(record['staging'])
        self.assertEqual(recovery.sweep(self.client), [])
        self.assertTrue((recovery._root() / record['operation_id']).is_dir())
        self.assertTrue((recovery._root() / (record['operation_id'] + '.json')).is_file())

    def test_marker_is_small_and_uses_compact_bounded_expectations(self):
        def stream():
            for i in range(2000):
                yield dict(id=str(uuid.uuid5(uuid.NAMESPACE_OID, str(i))),
                           properties={'content': 'x' * 4096}, vector=[0.25] * 64)
        importer._mark_started('Corpus', 2000, 'job', stream)
        marker = importer._marker_path('Corpus')
        data, snapshot = importer._read_marker(marker)
        self.assertLess(marker.stat().st_size, 1024)
        self.assertNotIn('records', data)
        self.assertLess(snapshot.stat().st_size, 2000 * 512)
        importer._mark_finished('Corpus')
        self.assertFalse(marker.exists())
        self.assertFalse(snapshot.exists())

    def import_new_target(self, pkg, manifest):
        # These cases exercise chunk preflight, not evaluation sidecar identity.
        for sidecar in (pkg / 'goldstandard').glob('*.json'):
            sidecar.unlink()
        importer._jobs['preflight'] = {'chunks_written': 0}
        with patch.object(importer.packager, 'open_package', return_value=(pkg, manifest)), \
                patch.object(importer.packager, 'verify_digests'), \
                patch.object(importer, '_check_embedding'), \
                patch.object(importer, '_ensure_models', return_value=[]), \
                patch.object(importer, '_create_from_package') as create:
            importer._run('preflight', 'fixture.tar.gz', 'abort')
        create.assert_not_called()
        return importer._jobs['preflight']

    def test_new_target_invalid_package_preflight_reports_package_corrupt(self):
        for fault in ('duplicate', 'count', 'timestamp'):
            with self.subTest(fault=fault):
                pkg, manifest = self.package()
                manifest['collection']['name'] = 'NewCorpus'
                rows = json.loads(json.dumps(self.original))
                if fault == 'duplicate':
                    rows[1]['id'] = rows[0]['id']
                elif fault == 'count':
                    manifest['collection']['chunk_count'] = 3
                else:
                    rows[0]['properties']['created_at'] = 'not-a-date'
                (pkg / 'chunks.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
                job = self.import_new_target(pkg, manifest)
                self.assertEqual(job['status'], 'failed')
                self.assertEqual(job['error_code'], 'PACKAGE_CORRUPT')
                self.assertEqual(job['error_detail'], {'file': 'chunks.jsonl'})
                self.assertEqual(job['chunks_written'], 0)
                self.assertEqual(list(importer._markers_dir().iterdir()), [])
                self.assertEqual(self.cols.get('Corpus').rows, {r['id']: r for r in self.original})

    def test_new_target_snapshot_io_error_remains_import_failed(self):
        pkg, manifest = self.package()
        manifest['collection']['name'] = 'NewCorpus'
        with patch.object(batch_write.ExpectedRecords, 'snapshot', side_effect=OSError('disk unavailable')):
            job = self.import_new_target(pkg, manifest)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error_code'], 'IMPORT_FAILED')
        self.assertIn('disk unavailable', job['error'])
        self.assertEqual(list(importer._markers_dir().iterdir()), [])

    def test_failed_new_target_cleanup_keeps_marker_for_startup_retry(self):
        pkg, manifest = self.package()
        importer._jobs['job'] = {'chunks_written': 0}
        actual_delete = self.cols.delete
        def create_rejected(name, *args, **kwargs):
            self.cols.create(name)
            self.cols.get(name).fault = 'reject'
        def fail_partial_delete(name):
            if '_imported_' in name:
                raise OSError('partial target cleanup unavailable')
            return actual_delete(name)
        with patch.object(importer.packager, 'open_package', return_value=(pkg, manifest)), \
                patch.object(importer.packager, 'verify_digests'), \
                patch.object(importer, '_check_embedding'), \
                patch.object(importer, '_ensure_models', return_value=[]), \
                patch.object(wc, '_create_collection_sync', side_effect=create_rejected), \
                patch.object(self.cols, 'delete', side_effect=fail_partial_delete):
            importer._run('job', 'fixture.tar.gz', 'rename')
        job = importer._jobs['job']
        self.assertEqual(job['status'], 'failed')
        self.assertTrue(job['error_detail']['cleanup_pending'])
        target = job['error_detail']['collection']
        self.assertTrue(importer._marker_path(target).exists())
        self.assertTrue(self.cols.exists(target))
        self.assertEqual(len(importer.sweep_interrupted_imports()), 1)
        self.assertFalse(self.cols.exists(target))
        self.assertFalse(importer._marker_path(target).exists())
        self.assertTrue(self.cols.exists('Corpus'))

    def test_oversized_legacy_marker_is_preserved_without_decoding(self):
        marker = importer._marker_path('Corpus')
        marker.write_bytes(b'{' + b'x' * 5000)
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not decode large metadata')):
            self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(marker.exists())
        self.assertTrue(self.cols.exists('Corpus'))

    def test_corrupt_expected_snapshot_preserves_backend_and_ownership(self):
        importer._mark_started('Corpus', 2, 'job', self.original)
        marker = importer._marker_path('Corpus')
        data, snapshot = importer._read_marker(marker)
        snapshot.write_bytes(b'corrupt snapshot')
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(marker.exists())
        self.assertTrue(self.cols.exists('Corpus'))

    def test_recovery_metadata_cleanup_failure_is_resumed_at_startup(self):
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError):
            self.rebuild()
        record = self.recovery_record()
        with patch.object(recovery.shutil, 'rmtree', side_effect=OSError('metadata cleanup failed')), \
                self.assertRaises(OSError):
            recovery.discard(record, self.client)
        path = recovery._root() / (record['operation_id'] + '.json')
        self.assertEqual(json.loads(path.read_text())['state'], 'cleanup')
        self.assertFalse(self.cols.exists(record['staging']))
        self.assertEqual(recovery.sweep(self.client), [record['staging']])
        self.assertFalse(path.exists())
        self.assertFalse((recovery._root() / record['operation_id']).exists())

    def test_cleanup_intent_is_persisted_before_backend_deletion(self):
        record = recovery.begin('Corpus', 'tune', self.client)
        self.cols.create(record['staging'])
        with patch.object(recovery, '_write', side_effect=OSError('cannot persist cleanup')), \
                self.assertRaises(OSError):
            recovery.discard(record, self.client)
        self.assertTrue(self.cols.exists(record['staging']))
        self.assertEqual(record['state'], 'scratch')

    def test_marker_cleanup_failure_never_deletes_verified_target_on_restart(self):
        importer._mark_started('Corpus', 2, 'job', self.original)
        marker = importer._marker_path('Corpus')
        actual_unlink = Path.unlink
        def fail_unlink(path, *args, **kwargs):
            if path == marker:
                raise OSError('marker cleanup failed')
            return actual_unlink(path, *args, **kwargs)
        with patch.object(Path, 'unlink', new=fail_unlink), self.assertRaises(OSError):
            importer._mark_finished('Corpus')
        self.assertEqual(json.loads(marker.read_text())['state'], 'cleanup')
        self.cols.get('Corpus').rows.clear()  # later legitimate changes must not be swept as partial
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertFalse(marker.exists())

    def test_interrupted_import_compares_records_not_only_count(self):
        importer._mark_started('Corpus', 2, 'job', self.original)
        self.cols.get('Corpus').rows[self.original[0]['id']]['properties']['content'] = 'mismatch'
        self.assertEqual(len(importer.sweep_interrupted_imports()), 1)
        self.assertFalse(self.cols.exists('Corpus'))

    def test_interrupted_import_read_failure_and_legacy_marker_preserve_data(self):
        importer._mark_started('Corpus', 2, 'job', self.original)
        self.cols.get('Corpus').fault = 'read'
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        marker = importer._marker_path('Corpus')
        marker.write_text(json.dumps({'collection': 'Corpus', 'expected_chunks': 1}))
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(marker.exists())
        self.assertTrue(self.cols.exists('Corpus'))

    def test_import_final_flush_never_reports_attempts_as_written(self):
        pkg = self.root / 'package'
        pkg.mkdir()
        (pkg / 'chunks.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in self.original))
        manifest = {'collection': {'chunk_count': 2}, 'embedding': {'dimensions': 2}}
        self.cols.get('Corpus').rows.clear()
        self.cols.get('Corpus').fault = 'reject'
        progress = []
        with self.assertRaises(RuntimeError):
            importer._insert_chunks('Corpus', pkg, manifest, progress.append)
        self.assertEqual(progress, [])


if __name__ == '__main__':
    unittest.main()
