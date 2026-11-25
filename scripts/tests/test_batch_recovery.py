"""Controlled final-flush faults against real writer and recovery services."""
import asyncio
import copy
import hashlib
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
from unittest.mock import AsyncMock, call, patch

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2] / 'api'))
from config import settings
from services import batch_write, collection_recovery as recovery, collection_writes, importer, tuning, weaviate_client as wc


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

    def fixed_size(self, batch_size, concurrent_requests):
        # Vectorless writes use bounded batches (#613); same fake behaviour.
        return self.dynamic()

    def __enter__(self):
        return self

    def add_object(self, properties, uuid, vector=None):
        self.pending.append(dict(id=str(uuid), properties=copy.deepcopy(properties),
                                 vector=copy.deepcopy(vector) if vector is not None else [0.25, 0.5]))

    def __exit__(self, *args):
        fault = self.collection.fault
        if fault == 'cancel':
            raise asyncio.CancelledError()
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
    def __init__(self, name, fault=None, description=None):
        self.name = name
        self.rows = {}
        self.fault = fault
        self.description = description
        self.config = SimpleNamespace(get=lambda: SimpleNamespace(name=self.name, description=self.description, vectorizer=None))
        self.batch = Batch(self)
        self.query = SimpleNamespace(fetch_objects=lambda filters, **kwargs: SimpleNamespace(
            objects=[obj for obj in self.iterator(include_vector=True) if str(obj.uuid) in filters.value]))
        self.data = SimpleNamespace(delete_many=self.delete_many)
        self.aggregate = SimpleNamespace(over_all=lambda **kwargs: SimpleNamespace(total_count=len(self.rows)))

    def delete_many(self, where):
        for key in where.value:
            self.rows.pop(key, None)
        return SimpleNamespace(failed=0)

    def iterator(self, include_vector=False, return_properties=None):
        if self.fault == 'read':
            raise OSError('backend unavailable')
        for row in self.rows.values():
            props = {'source_file': None, 'chunk_size': None, **copy.deepcopy(row['properties'])}
            if isinstance(props.get('created_at'), str):
                props['created_at'] = datetime.fromisoformat(props['created_at'].replace('Z', '+00:00'))
            if return_properties is not None:
                # As Weaviate does: only the properties asked for (#258).
                props = {key: value for key, value in props.items() if key in return_properties}
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

    def create(self, name, *args, description=None):
        if name == 'Corpus' and self.create_failure:
            raise RuntimeError('final create failed')
        fault = self.final_fault if name == 'Corpus' else self.staging_fault
        self.items[name] = Collection(name, fault, description)

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

    def test_collection_creation_records_an_optional_description(self):
        from unittest.mock import MagicMock
        client = MagicMock()
        with patch.object(wc, 'get_client', return_value=client):
            wc._create_collection_sync('Owned', 'hnsw', 'cosine', {}, description='rag-import:' + 'a' * 32)
            wc._create_collection_sync('Plain', 'hnsw', 'cosine', {})
        first, second = client.collections.create.call_args_list
        self.assertEqual(first.kwargs['description'], 'rag-import:' + 'a' * 32)
        self.assertIsNone(second.kwargs['description'])



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
        self.stack.enter_context(patch.object(wc, '_create_collection_sync', side_effect=lambda name, *args, **kwargs: self.cols.create(name, description=kwargs.get('description'))))
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
        blob = hashlib.sha256(b'original source').hexdigest() if record['operation'] == 'import' else 'source-blob'
        self.assertEqual((Path(settings.sources_dir) / name / blob).read_bytes(), b'original source')
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

    # #312: a re-chunk's reader is staged one part at a time.
    def source_reader(self, files):
        """A real re-chunk reader over retained files of a collection of their own."""
        from services import sources, splitter
        for name, data in files:
            sources.store('Retained', name, data, replace=True)
        documents = sources.load_index('Retained')['documents']
        return tuning._SourceChunks('Retained', documents, {name for name, _ in files},
                                    splitter.Settings('fixed', 1000, 0, 0.85, 0))

    def staging_inserts(self):
        inserts = []
        real = wc._insert_chunks_sync

        def insert(name, chunks):
            inserts.append((name, len(chunks)))
            return real(name, chunks)
        return inserts, patch.object(wc, '_insert_chunks_sync', side_effect=insert)

    def test_a_reader_is_staged_one_part_at_a_time(self):
        reader = self.source_reader([(f'f{i}.jsonl', ''.join(
            json.dumps({'t': f'{i}-{n} ' + 'x' * 400}) + '\n' for n in range(size)).encode())
            for i, size in enumerate((4, 9, 2))])
        expected = [c['content'] for c in reader]
        inserts, patched = self.staging_inserts()
        staged = []
        with patched:
            written = tuning._rebuild('Corpus', reader, None, None, None, staged=staged.append)
        sizes = [count for _, count in inserts]
        self.assertEqual(len(inserts), 3)
        self.assertTrue(all(name != 'Corpus' for name, _ in inserts), inserts)
        self.assertEqual(staged, [sizes[0], sizes[0] + sizes[1], sum(sizes)])
        self.assertEqual(written, len(expected))
        self.assertEqual(sorted(r['properties']['content'] for r in self.cols.get('Corpus').rows.values()),
                         sorted(expected))
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertFalse(list(recovery._root().glob('*.json')))

    def test_a_started_reader_stages_its_first_part_once(self):
        # _run calls start() before _rebuild (#312): the part read ahead is
        # staged once, then the rest, and nothing is lost or repeated.
        files = [(f'f{i}.jsonl', ''.join(
            json.dumps({'t': f'{i}-{n} ' + 'x' * 400}) + '\n' for n in range(size)).encode())
            for i, size in enumerate((4, 9, 2))]
        expected = [c['content'] for c in self.source_reader(files)]
        reader = self.source_reader(files)
        reader.start()
        inserts, patched = self.staging_inserts()
        staged = []
        with patched:
            written = tuning._rebuild('Corpus', reader, None, None, None, staged=staged.append)
        reader.close()
        sizes = [count for _, count in inserts]
        self.assertEqual(len(inserts), 3)
        self.assertEqual(staged, [sizes[0], sizes[0] + sizes[1], sum(sizes)])
        self.assertEqual(written, len(expected))
        self.assertEqual(sorted(r['properties']['content'] for r in self.cols.get('Corpus').rows.values()),
                         sorted(expected))
        self.assertFalse(list(Path(settings.upload_dir).glob('rechunk-*')))

    def test_a_started_reader_closed_unread_removes_its_work_folder(self):
        # _run's finally closes a reader whose rebuild never consumed it (#312).
        reader = self.source_reader([('a.jsonl', b'{"t": "alpha"}\n'), ('b.jsonl', b'{"t": "beta"}\n')])
        reader.start()
        self.assertEqual(len(list(Path(settings.upload_dir).glob('rechunk-*'))), 1)
        reader.close()
        reader.close()  # a second close is harmless
        self.assertFalse(list(Path(settings.upload_dir).glob('rechunk-*')))
        self.assertEqual(self.cols.get('Corpus').rows, {r['id']: r for r in self.original})

    def test_a_reader_that_fails_mid_way_leaves_the_original(self):
        bad = b'{"t": "x"}\n{bad\n'
        # The reader goes by digest: pick a good file whose digest sorts first.
        good = next(g for g in (b'{"t": "first %d"}\n' % n for n in range(1000))
                    if hashlib.sha256(g).hexdigest() < hashlib.sha256(bad).hexdigest())
        reader = self.source_reader([('a.jsonl', good), ('b.jsonl', bad)])
        inserts, patched = self.staging_inserts()
        with patched, self.assertRaisesRegex(ValueError, 'line 2 is not valid JSON'):
            tuning._rebuild('Corpus', reader, None, None, None)
        self.assertEqual(len(inserts), 1)  # a.jsonl was staged before b.jsonl failed
        self.assertEqual(self.cols.get('Corpus').rows, {r['id']: r for r in self.original})
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertFalse(list(recovery._root().glob('*.json')))
        self.assertFalse(list(Path(settings.upload_dir).glob('rechunk-*')))

    def test_a_reader_with_no_chunks_leaves_the_original(self):
        reader = self.source_reader([('empty.jsonl', b'{}\n')])
        with self.assertRaises(importer.PackageError) as error:
            tuning._rebuild('Corpus', reader, None, None, None)
        self.assertEqual(error.exception.code, 'SOURCES_REQUIRED')
        self.assertIn('produced no chunks', error.exception.message)
        self.assertEqual(self.cols.get('Corpus').rows, {r['id']: r for r in self.original})
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertFalse(list(recovery._root().glob('*.json')))

    # #881: the recovery copy never reads through a link. A link entry in the
    # sources folder is copied as a link; a linked sources folder is refused.
    def plant_source_link(self):
        outside = self.root / 'outside.txt'
        outside.write_bytes(b'outside secret bytes')
        digest = hashlib.sha256(outside.read_bytes()).hexdigest()
        (Path(settings.sources_dir) / 'Corpus' / digest).symlink_to(outside)
        return outside, digest

    def test_failed_cutover_copies_source_links_as_links_never_their_bytes(self):
        outside, digest = self.plant_source_link()
        folder = self.root / 'outside-folder'
        folder.mkdir()
        (folder / 'inside.txt').write_bytes(b'outside secret bytes')
        (Path(settings.sources_dir) / 'Corpus' / 'linked-folder').symlink_to(folder)
        flushed, real_fsync = [], os.fsync

        def fsync(fd):
            flushed.append(os.fstat(fd).st_ino)
            return real_fsync(fd)
        self.cols.create_failure = True
        with patch.object(recovery.os, 'fsync', side_effect=fsync), \
                patch.object(recovery, '_sync_dir', wraps=recovery._sync_dir) as sync_dir, \
                self.assertRaises(importer.PackageError):
            self.rebuild()
        synced = [Path(c.args[0]) for c in sync_dir.call_args_list]
        record = self.recovery_record()
        self.assert_retained(record)
        copy_dir = Path(settings.sources_dir) / record['staging']
        self.assertTrue((copy_dir / digest).is_symlink())
        self.assertEqual(os.readlink(copy_dir / digest), str(outside))
        self.assertTrue((copy_dir / 'linked-folder').is_symlink())
        self.assertEqual(os.readlink(copy_dir / 'linked-folder'), str(folder))
        for root in (copy_dir, recovery._root() / record['operation_id']):
            for path in root.rglob('*'):
                if path.is_file() and not path.is_symlink():
                    self.assertNotIn(b'outside secret bytes', path.read_bytes(), path)
        self.assertTrue(flushed)
        self.assertNotIn(outside.stat().st_ino, flushed)
        self.assertNotIn((folder / 'inside.txt').stat().st_ino, flushed)
        self.assertFalse([path for path in synced if path.is_symlink()])
        self.assertEqual(outside.read_bytes(), b'outside secret bytes')
        self.assertEqual([p.name for p in folder.iterdir()], ['inside.txt'])

    def test_linked_sources_folder_is_refused_before_cutover(self):
        sources_dir = Path(settings.sources_dir)
        folder = self.root / 'outside-sources'
        (sources_dir / 'Corpus').rename(folder)
        (sources_dir / 'Corpus').symlink_to(folder)
        with self.assertRaises(ValueError) as error:
            self.rebuild()
        self.assertIn('is a link', str(error.exception))
        self.assertIn("'Corpus' is a link", str(error.exception))  # #886: named relative to the sources root
        self.assertEqual(self.cols.get('Corpus').rows, {r['id']: r for r in self.original})
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertFalse(list(recovery._root().glob('*.json')))
        self.assertEqual([p.name for p in sources_dir.iterdir()], ['Corpus'])
        self.assertTrue((sources_dir / 'Corpus').is_symlink())
        self.assertEqual([p.name for p in folder.iterdir()], ['source-blob'])
        self.assertEqual((folder / 'source-blob').read_bytes(), b'original source')

    def test_linked_config_sidecar_is_refused_before_cutover(self):
        # #881: the file branch of _copy's refusal. A linked ingest or
        # retrieval config is never copied through into the recovery sidecars.
        for kind in ('ingest', 'retrieval'):
            with self.subTest(kind=kind):
                config = Path(settings.upload_dir) / f'{kind}_configs' / 'Corpus.json'
                outside = self.root / f'outside-{kind}.json'
                outside.write_text(json.dumps({'collection': 'Corpus', 'secret': 'outside config bytes'}))
                saved = config.read_bytes()
                config.unlink()
                config.symlink_to(outside)
                try:
                    with self.assertRaises(ValueError) as error:
                        self.rebuild()
                    self.assertIn('is a link', str(error.exception))
                    # #886: the error names the path under UPLOAD_DIR, not only the file.
                    self.assertIn(f"'{kind}_configs/Corpus.json' is a link", str(error.exception))
                    self.assertNotIn(str(self.root), str(error.exception))
                    self.assertEqual(self.cols.get('Corpus').rows, {r['id']: r for r in self.original})
                    self.assertEqual(list(self.cols.items), ['Corpus'])
                    self.assertFalse(list(recovery._root().glob('*.json')))
                    self.assertEqual([p.name for p in Path(settings.sources_dir).iterdir()], ['Corpus'])
                    for name in ('ingest_configs', 'retrieval_configs'):
                        self.assertEqual([p.name for p in (Path(settings.upload_dir) / name).iterdir()], ['Corpus.json'])
                    for path in recovery._root().rglob('*'):
                        if path.is_file():
                            self.assertNotIn(b'outside config bytes', path.read_bytes(), path)
                    self.assertTrue(config.is_symlink())
                    self.assertIn('outside config bytes', outside.read_text())
                finally:
                    config.unlink()
                    config.write_bytes(saved)

    def test_duplicate_uuid_is_a_typed_refusal(self):
        # #890: a repeated UUID is refused with a ValueError subclass carrying
        # the key, and the reindex snapshot recognises it by type, not wording.
        rows = records()
        rows.append(copy.deepcopy(rows[0]))
        key = rows[0]['id']
        with batch_write.ExpectedRecords() as expected, \
                self.assertRaises(batch_write.DuplicateRecordError) as error:
            expected.capture(rows)
        self.assertIsInstance(error.exception, ValueError)
        self.assertEqual(error.exception.key, key)
        self.assertEqual(str(error.exception), f'Duplicate chunk UUID {key}')
        with self.assertRaises(RuntimeError) as refused:
            tuning._snapshot(rows)
        self.assertEqual(str(refused.exception), f'Reindex received duplicate stored UUID {key}')
        self.assertIsInstance(refused.exception.__cause__, batch_write.DuplicateRecordError)
        self.assertFalse(list(Path(settings.upload_dir).glob('batch-verify-*')))

    def test_dangling_session_link_is_refused_naming_its_path(self):
        # #886: a gold-standard session that is a dangling link can't be read, so
        # it counts as the collection's; the rebuild is refused before cutover and
        # the error names the session's path under UPLOAD_DIR, never an absolute one.
        session = Path(settings.upload_dir) / 'goldstandard_sessions' / 'gs_0123abcd.json'
        session.unlink()
        session.symlink_to(self.root / 'missing-session.json')
        with self.assertRaises(ValueError) as error:
            self.rebuild()
        self.assertIn("Recovery source 'goldstandard_sessions/gs_0123abcd.json' is a link", str(error.exception))
        self.assertNotIn(str(self.root), str(error.exception))
        self.assertEqual(self.cols.get('Corpus').rows, {r['id']: r for r in self.original})
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertFalse(list(recovery._root().glob('*.json')))
        self.assertEqual([p.name for p in Path(settings.sources_dir).iterdir()], ['Corpus'])
        self.assertTrue(session.is_symlink())

    def test_linked_package_sidecar_is_refused_naming_its_package_path(self):
        # #886: in an import's replace-mode recovery copy the sidecars come from
        # the extracted package, so a linked one is named relative to the
        # package, never by an absolute path, and the original is kept.
        outside = self.root / 'outside'
        outside.mkdir()
        for name in ('goldstandard', 'collection.json', 'manifest.json'):
            with self.subTest(name=name):
                pkg, manifest = self.package()
                entry = pkg / name
                moved = outside / name
                entry.rename(moved)
                entry.symlink_to(moved)
                try:
                    with patch.object(self, 'package', return_value=(pkg, manifest)):
                        job = self.import_replace()
                    self.assertEqual(job['status'], 'failed')
                    self.assertIn(f"Recovery source '{name}' is a link", job['error'])
                    self.assertNotIn(str(self.root), job['error'])
                    self.assertEqual(self.cols.get('Corpus').rows, {r['id']: r for r in self.original})
                    self.assertEqual(list(self.cols.items), ['Corpus'])
                    self.assertFalse(list(recovery._root().glob('*.json')))
                finally:
                    entry.unlink()
                    moved.rename(entry)
        # A package's linked config is refused earlier, by the import's settings
        # check; retain() itself still names it relative to the package root.
        for kind in ('ingest', 'retrieval'):
            with self.subTest(kind=kind):
                pkg, _ = self.package()
                entry = pkg / f'{kind}_config.json'
                moved = outside / entry.name
                entry.rename(moved)
                entry.symlink_to(moved)
                try:
                    record = recovery.begin('Corpus', 'import', self.client)
                    with self.assertRaises(ValueError) as error:
                        recovery.retain(record, package=pkg)
                    self.assertEqual(str(error.exception), f"Recovery source '{kind}_config.json' is a link")
                finally:
                    entry.unlink()
                    moved.rename(entry)

    def test_dangling_source_link_is_copied_as_a_link(self):
        # #881: copytree(symlinks=True) keeps a dangling link entry as a link
        # instead of failing the snapshot on a missing target.
        missing = self.root / 'missing-target'
        (Path(settings.sources_dir) / 'Corpus' / 'dangling').symlink_to(missing)
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError):
            self.rebuild()
        record = self.recovery_record()
        self.assert_retained(record)
        copied = Path(settings.sources_dir) / record['staging'] / 'dangling'
        self.assertTrue(copied.is_symlink())
        self.assertEqual(os.readlink(copied), str(missing))
        self.assertFalse(missing.exists())

    def test_success_discards_copied_source_links_without_touching_targets(self):
        outside, digest = self.plant_source_link()
        written = tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, None)
        self.assertEqual(written, 2)
        self.assertFalse(list(recovery._root().glob('*.json')))
        sources_dir = Path(settings.sources_dir)
        self.assertEqual([p.name for p in sources_dir.iterdir()], ['Corpus'])
        self.assertTrue((sources_dir / 'Corpus' / digest).is_symlink())
        self.assertEqual(outside.read_bytes(), b'outside secret bytes')

    def package(self):
        pkg = self.root / 'package'
        pkg.mkdir(exist_ok=True)
        (pkg / 'chunks.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in self.original))
        source = pkg / 'sources'
        source.mkdir(exist_ok=True)
        digest = hashlib.sha256(b'original source').hexdigest()
        (source / digest).write_bytes(b'original source')
        (source / 'index.json').write_text(json.dumps({'version': 1, 'documents': {digest: {'filenames': ['source.txt']}}}))
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

    def test_interrupted_tuning_partial_target_is_checked_at_startup(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.rebuild()
        record = self.recovery_record()
        self.assertTrue(record['cutover_pending'])
        self.cols.interrupt = False
        self.cols.create('Corpus', description=recovery.cutover_description(record))
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertTrue(self.cols.exists(record['staging']))
        stale.assert_called_once()

    def test_interrupted_tuning_never_deletes_another_target_instance(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.rebuild()
        self.recovery_record()
        self.cols.interrupt = False
        self.cols.create('Corpus', description='new user collection')
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
        self.assertTrue(self.cols.exists('Corpus'))
        stale.assert_not_called()

    def test_interrupted_tuning_keeps_complete_target(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.rebuild()
        record = self.recovery_record()
        self.cols.interrupt = False
        self.cols.create('Corpus', description=recovery.cutover_description(record))
        self.cols.get('Corpus').rows = copy.deepcopy(self.cols.get(record['staging']).rows)
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
            self.assertTrue(self.cols.exists('Corpus'))
            stale.assert_not_called()

    def interrupted_cutover(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.rebuild()
        self.cols.interrupt = False
        record = self.recovery_record()
        self.cols.create('Corpus', description=recovery.cutover_description(record))
        return record

    def test_startup_check_flags_an_interrupted_cutover_only_once(self):
        record = self.interrupted_cutover()
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
            recovery.sweep(self.client)
        stale.assert_called_once_with('Corpus', 'interrupted tuning final write; verified recovery retained')
        checked = self.recovery_record()
        self.assertFalse(checked['cutover_pending'])
        self.assertEqual(checked['cutover_checked'], 'stale')
        self.assertTrue(self.cols.exists(record['staging']))
        self.assertTrue(self.cols.exists('Corpus'))

    def test_complete_cutover_is_not_rechecked_after_later_mutation(self):
        record = self.interrupted_cutover()
        self.cols.get('Corpus').rows = copy.deepcopy(self.cols.get(record['staging']).rows)
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
            self.assertEqual(self.recovery_record()['cutover_checked'], 'complete')
            self.cols.get('Corpus').rows.clear()
            with patch.object(batch_write, 'verify', side_effect=AssertionError('rechecked')) as verify:
                recovery.sweep(self.client)
                verify.assert_not_called()
            stale.assert_not_called()

    def test_other_instance_decision_is_retired_without_touching_sessions(self):
        record = self.interrupted_cutover()
        self.cols.get('Corpus').config = SimpleNamespace(get=lambda: SimpleNamespace(description='later'))
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
            recovery.sweep(self.client)
            stale.assert_not_called()
        self.assertEqual(self.recovery_record()['cutover_checked'], 'other-instance')
        self.assertTrue(self.cols.exists(record['staging']))

    def test_unreadable_cutover_stays_pending_until_check_can_finish(self):
        self.interrupted_cutover()
        self.cols.get('Corpus').fault = 'read'
        recovery.sweep(self.client)
        self.assertTrue(self.recovery_record()['cutover_pending'])
        self.cols.get('Corpus').fault = None
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
            recovery.sweep(self.client)
            stale.assert_called_once()
        self.assertFalse(self.recovery_record()['cutover_pending'])

    def test_failed_outcome_write_keeps_cutover_pending_for_retry(self):
        self.interrupted_cutover()
        with patch.object(tuning.goldstandard, 'mark_stale'), patch.object(recovery, '_write', side_effect=OSError('disk full')):
            recovery.sweep(self.client)
        self.assertTrue(self.recovery_record()['cutover_pending'])
        with patch.object(tuning.goldstandard, 'mark_stale'):
            recovery.sweep(self.client)
        self.assertFalse(self.recovery_record()['cutover_pending'])

    # Reviewer tests (PR #247 testing review, head 51c1706).
    def test_interrupted_tuning_missing_target_marks_stale_and_keeps_recovery(self):
        # A kill right after the target delete leaves no target at all.
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.rebuild()
        record = self.recovery_record()
        self.assertTrue(record['cutover_pending'])
        self.assertFalse(self.cols.exists('Corpus'))
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            self.assertEqual(recovery.sweep(self.client), [])
            recovery.sweep(self.client)
        stale.assert_called_once_with('Corpus', 'interrupted tuning cutover; verified recovery retained')
        self.assertTrue(self.cols.exists(record['staging']))
        checked = self.recovery_record()
        self.assertFalse(checked['cutover_pending'])
        self.assertEqual(checked['cutover_checked'], 'stale')
        self.assertTrue((recovery._root() / record['operation_id']).is_dir())

    def test_interrupted_tuning_missing_recovery_never_flags_or_deletes(self):
        self.cols.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.rebuild()
        record = self.recovery_record()
        self.cols.interrupt = False
        self.cols.create('Corpus', description=recovery.cutover_description(record))
        self.cols.items.pop(record['staging'])
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            self.assertEqual(recovery.sweep(self.client), [])
        stale.assert_not_called()
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertTrue(self.recovery_record()['cutover_pending'])
        self.assertTrue((recovery._root() / record['operation_id']).is_dir())

    def test_invalid_cutover_pending_records_never_authorize_cleanup(self):
        for operation, value in (('tune', 'yes'), ('tune', 1), ('import', True)):
            with self.subTest(operation=operation, value=value):
                record = recovery.begin('Corpus', operation, self.client)
                self.cols.create(record['staging'])
                path = recovery._root() / (record['operation_id'] + '.json')
                path.write_text(json.dumps({**record, 'cutover_pending': value}))
                self.assertEqual(recovery.sweep(self.client), [])
                self.assertTrue(path.exists())
                self.assertTrue(self.cols.exists(record['staging']))
                path.unlink()

    def test_successful_tuning_tags_target_and_leaves_no_pending_record(self):
        tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, None)
        self.assertTrue(self.cols.get('Corpus').description.startswith('rag-tune:'))
        self.assertFalse(list(recovery._root().glob('*.json')))
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
        stale.assert_not_called()

    # A handled failure after cutover records its own decision (#263).
    def test_handled_failure_after_cutover_records_the_decision(self):
        self.cols.final_fault = 'reject'
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            with self.assertRaises(importer.PackageError) as error:
                self.rebuild()
            stale.assert_called_once()
            record = self.recovery_record()
            self.assertEqual(error.exception.detail['recovered_as'], record['staging'])
            self.assertFalse(record['cutover_pending'])
            self.assertEqual(record['cutover_checked'], 'stale')
            recovery.sweep(self.client)
            recovery.sweep(self.client)
            stale.assert_called_once()
        self.assertTrue(self.cols.exists(record['staging']))

    def test_failed_post_cutover_marking_leaves_the_check_for_startup(self):
        self.cols.final_fault = 'reject'
        with patch.object(tuning.goldstandard, 'mark_stale', side_effect=OSError('session store unavailable')), \
                patch.object(tuning, '_log'), self.assertRaises(importer.PackageError):
            self.rebuild()
        self.assertTrue(self.recovery_record()['cutover_pending'])
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
            recovery.sweep(self.client)
        stale.assert_called_once_with('Corpus', 'interrupted tuning final write; verified recovery retained')
        self.assertEqual(self.recovery_record()['cutover_checked'], 'stale')

    # Reviewer tests (PR #879 testing review, head 8233a39).
    def test_failed_decision_write_after_handled_failure_leaves_the_check_for_startup(self):
        # #263: a failed decision write is logged, the job error is unchanged,
        # and the check stays pending for startup, as after a hard kill.
        self.cols.final_fault = 'reject'
        with patch.object(tuning.goldstandard, 'mark_stale') as stale, \
                patch.object(recovery, 'finish_cutover_check', side_effect=OSError('disk full')), \
                patch.object(tuning, '_log') as log, \
                self.assertRaises(importer.PackageError) as error:
            self.rebuild()
        stale.assert_called_once()
        record = self.recovery_record()
        self.assertEqual(error.exception.code, 'TUNE_FAILED')
        self.assertEqual(error.exception.detail['recovered_as'], record['staging'])
        self.assertTrue(record['cutover_pending'])
        self.assertTrue(any('cutover decision' in str(c.args[0]) for c in log.exception.call_args_list),
                        log.exception.call_args_list)
        with patch.object(tuning.goldstandard, 'mark_stale') as stale:
            recovery.sweep(self.client)
            recovery.sweep(self.client)
        stale.assert_called_once_with('Corpus', 'interrupted tuning final write; verified recovery retained')
        self.assertEqual(self.recovery_record()['cutover_checked'], 'stale')

    def test_handled_reindex_failure_records_the_decision_only_when_history_is_marked(self):
        # #263 on the records (reindex) path: a failure after cutover whose
        # preservation can't be verified marks history and records 'stale'; one
        # whose target verifies as intact marks nothing and keeps no record.
        records_ = [{'id': r['id'], 'vector': r['vector'], 'properties': r['properties']} for r in self.original]
        for intact in (False, True):
            with self.subTest(intact=intact):
                for path in recovery._root().glob('*.json'):
                    path.unlink()
                self.cols.items = {'Corpus': self.cols.get('Corpus')}
                def write(name, recs):
                    if name == 'Corpus':
                        raise RuntimeError('final write failed')
                verified = []
                def verify(name, recs):
                    verified.append(name)
                    if len(verified) > 1 and not intact:
                        raise RuntimeError('readback changed')
                with patch.object(wc, '_validate_reindex_vectorizer_sync'), \
                        patch.object(tuning, '_write_records', side_effect=write), \
                        patch.object(tuning, '_verify_records', side_effect=verify), \
                        patch.object(tuning.goldstandard, 'mark_stale') as stale, \
                        self.assertRaises(Exception):
                    tuning._rebuild('Corpus', [], None, None, None, records=records_)
                self.assertEqual(verified, ['Corpus', 'Corpus'])
                paths = list(recovery._root().glob('*.json'))
                if intact:
                    stale.assert_not_called()
                    self.assertEqual(paths, [])
                else:
                    stale.assert_called_once_with('Corpus', 'reindex replacement failed after cutover began; exact record preservation was not verified')
                    record = self.recovery_record()
                    self.assertFalse(record['cutover_pending'])
                    self.assertEqual(record['cutover_checked'], 'stale')
                    with patch.object(tuning.goldstandard, 'mark_stale') as again:
                        recovery.sweep(self.client)
                    again.assert_not_called()
                    recovery.discard(record, self.client)

    # #870: a cancelled tuning job ends, and after cutover reports what a failure does.
    CANCELLED = 'CancelledError: the tuning job was cancelled before it finished'
    STALE_AFTER_CUTOVER = 'collection replacement failed after cutover began; retained pairs require historical review'

    def run_tune(self, job_id, operation, params):
        tuning._jobs[job_id] = {'job_id': job_id, 'status': 'queued', 'collection': 'Corpus',
                                'operation': operation, 'chunks_total': 0, 'chunks_written': 0,
                                'notes': [], 'error': None, 'error_code': None, 'error_detail': None}
        # As start_tune_job admits it: the delete registration and the one-tune slot.
        collection_writes.register_job('Corpus', 'tune', job_id)
        self.addCleanup(collection_writes.end_job, 'tune', job_id)
        tuning._active.add('Corpus')
        self.addCleanup(tuning._active.discard, 'Corpus')
        with self.assertRaises(asyncio.CancelledError):
            tuning._run(job_id, 'Corpus', operation, params)
        self.assertNotIn(('tune', job_id), collection_writes.active_jobs('Corpus'))
        self.assertNotIn('Corpus', tuning._active)
        return tuning._jobs[job_id]

    def test_cancellation_before_cutover_fails_the_job_and_leaves_the_original(self):
        before = copy.deepcopy(self.cols.get('Corpus').rows)
        with patch.object(wc, '_insert_chunks_sync', side_effect=asyncio.CancelledError()), \
                patch.object(tuning.goldstandard, 'mark_stale') as stale, patch.object(tuning, '_log'):
            job = self.run_tune('cancel-early', 'reembed', {})
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error_code'], 'TUNE_FAILED')
        self.assertEqual(job['error'], self.CANCELLED + '.')
        self.assertIsNone(job['error_detail'])
        self.assertEqual(self.cols.get('Corpus').rows, before)
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertFalse(list(recovery._root().glob('*.json')))
        stale.assert_not_called()

    def test_cancellation_after_cutover_reports_the_retained_copy(self):
        self.cols.final_fault = 'cancel'
        with patch.object(tuning.goldstandard, 'mark_stale', return_value=0) as stale, \
                patch.object(tuning, '_log'):
            job = self.run_tune('cancel-late', 'reembed', {})
        record = self.recovery_record()
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error_code'], 'TUNE_FAILED')
        self.assertEqual(job['error'], f"{self.CANCELLED}. Verified data is retained as '{record['staging']}'.")
        self.assertEqual(job['error_detail']['recovered_as'], record['staging'])
        self.assertEqual(job['error_detail']['sidecar_snapshots'], recovery.sidecar_reference(record))
        # The re-embed's own marking runs before replacement; the failure path's after it.
        self.assertEqual([c for c in stale.call_args_list if c.args[1] == self.STALE_AFTER_CUTOVER],
                         [call('Corpus', self.STALE_AFTER_CUTOVER)])
        self.assertEqual(stale.call_count, 2)
        self.assertFalse(record['cutover_pending'])
        self.assertEqual(record['cutover_checked'], 'stale')
        self.assert_retained(record)

    def test_cancellation_after_reindex_cutover_marks_stale_and_reports_the_copy(self):
        records_ = [{'id': r['id'], 'vector': r['vector'], 'properties': r['properties']} for r in self.original]
        def write(name, recs):
            if name == 'Corpus':
                raise asyncio.CancelledError()
        verified = []
        def verify(name, recs):
            verified.append(name)
            if len(verified) > 1:
                raise RuntimeError('readback changed')
        seen = []
        with patch.object(wc, '_validate_reindex_vectorizer_sync'), \
                patch.object(tuning, '_write_records', side_effect=write), \
                patch.object(tuning, '_verify_records', side_effect=verify), \
                patch.object(tuning.goldstandard, 'mark_stale') as stale, \
                self.assertRaises(asyncio.CancelledError):
            tuning._rebuild('Corpus', [], None, None, None, records=records_, cancelled=seen.append)
        stale.assert_called_once_with('Corpus', 'reindex replacement failed after cutover began; exact record preservation was not verified')
        record = self.recovery_record()
        self.assertFalse(record['cutover_pending'])
        self.assertEqual(record['cutover_checked'], 'stale')
        self.assertEqual(len(seen), 1)
        self.assertIsInstance(seen[0], importer.PackageError)
        self.assertEqual(seen[0].code, 'TUNE_FAILED')
        self.assertEqual(seen[0].detail['recovered_as'], record['staging'])
        self.assertTrue(seen[0].message.startswith(self.CANCELLED + '. '), seen[0].message)
        self.assertTrue(self.cols.exists(record['staging']))

    # ── reviewer-added (#870): the windows between retain and cutover, and before the rebuild ──

    def test_cancellation_while_marking_stale_leaves_the_original_and_says_so(self):
        before = copy.deepcopy(self.cols.get('Corpus').rows)
        with patch.object(tuning.goldstandard, 'mark_stale', side_effect=asyncio.CancelledError()) as stale, \
                patch.object(tuning, '_log'):
            job = self.run_tune('cancel-marking', 'reembed', {})
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error_code'], 'TUNE_FAILED')
        self.assertEqual(job['error'], f"{self.CANCELLED}. Gold-standard sessions could not be marked stale "
                                       "before replacement, so the original collection is unchanged.")
        self.assertIsNone(job['error_detail'])
        stale.assert_called_once()
        self.assertEqual(self.cols.get('Corpus').rows, before)
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertEqual(list(recovery._root().iterdir()), [])

    def test_cancellation_at_the_cutover_record_leaves_the_original_and_says_so(self):
        before = copy.deepcopy(self.cols.get('Corpus').rows)
        with patch.object(recovery, 'begin_cutover', side_effect=asyncio.CancelledError()), \
                patch.object(tuning.goldstandard, 'mark_stale', return_value=0), patch.object(tuning, '_log'):
            job = self.run_tune('cancel-record', 'reembed', {})
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['error_code'], 'TUNE_FAILED')
        self.assertEqual(job['error'], f"{self.CANCELLED}. The cutover record could not be written, so the "
                                       "original collection is unchanged. Gold-standard sessions were already marked stale.")
        self.assertIsNone(job['error_detail'])
        self.assertEqual(self.cols.get('Corpus').rows, before)
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertEqual(list(recovery._root().iterdir()), [])

    def test_cancellation_before_the_rebuild_fails_the_job(self):
        before = copy.deepcopy(self.cols.get('Corpus').rows)
        with patch.object(tuning, '_existing_chunks', side_effect=asyncio.CancelledError()), \
                patch.object(tuning, '_rebuild') as rebuild, \
                patch.object(tuning.goldstandard, 'mark_stale') as stale, patch.object(tuning, '_log'):
            job = self.run_tune('cancel-read', 'reembed', {})
        self.assertEqual((job['status'], job['error_code'], job['error']), ('failed', 'TUNE_FAILED', self.CANCELLED + '.'))
        rebuild.assert_not_called()
        stale.assert_not_called()
        self.assertEqual(self.cols.get('Corpus').rows, before)
        self.assertEqual(list(self.cols.items), ['Corpus'])

    def test_a_failure_still_chains_its_cause(self):
        # The refactor builds the PackageError first; an Exception still raises it `from` its cause.
        cause = RuntimeError('session store unavailable')
        def fail_marking():
            raise cause
        seen = []
        with self.assertRaises(importer.PackageError) as error:
            tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, None,
                            before_replace=fail_marking, cancelled=seen.append)
        self.assertIs(error.exception.__cause__, cause)
        self.assertEqual(seen, [])

    def test_import_replace_note_counts_canonical_and_alias_sessions(self):
        seen = []
        def sessions(name):
            seen.append(name)
            return [{'session_id': 'gs_18000001'}] if name == 'Corpus' else [{'session_id': 'gs_18000002'}]
        with patch.object(importer.goldstandard, 'sessions_for', side_effect=sessions):
            job = self.import_replace()
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(set(seen), {'Corpus', 'corpus'})
        self.assertTrue(any('2 gold-standard session(s)' in note for note in job['notes']), job)

    def assert_replace_note_count(self, by_spelling, expected):
        # #180: the note counts retained sessions across both spellings, each session once.
        with patch.object(importer.goldstandard, 'sessions_for',
                          side_effect=lambda name: [{'session_id': sid} for sid in by_spelling.get(name, [])]):
            job = self.import_replace()
        self.assertEqual(job['status'], 'completed', job)
        notes = [note for note in job['notes'] if 'gold-standard session(s) from the replaced' in note]
        if expected:
            self.assertEqual(len(notes), 1, job)
            self.assertTrue(notes[0].startswith(f'{expected} gold-standard session(s)'), notes)
        else:
            self.assertEqual(notes, [], job)

    def test_import_replace_note_counts_alias_only_sessions(self):
        self.assert_replace_note_count({'corpus': ['gs_18000003']}, 1)

    def test_import_replace_note_counts_a_session_under_both_spellings_once(self):
        self.assert_replace_note_count({'Corpus': ['gs_18000004'], 'corpus': ['gs_18000004']}, 1)

    def test_import_replace_without_retained_sessions_adds_no_note(self):
        self.assert_replace_note_count({}, 0)

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

    def test_failed_replace_recovery_excludes_unindexed_future_blob(self):
        from services import sources
        pkg, manifest = self.package()
        future = b'future legitimate document'
        digest = hashlib.sha256(future).hexdigest()
        (pkg / 'sources' / digest).write_bytes(b'planted wrong bytes')
        (pkg / 'sources' / 'extra-folder').mkdir()
        self.cols.create_failure = True
        with patch.object(self, 'package', return_value=(pkg, manifest)):
            job = self.import_replace()
        self.assertEqual(job['status'], 'failed')
        recovered = job['error_detail']['recovered_as']
        directory = sources.collection_dir(recovered)
        self.assertEqual({p.name for p in directory.iterdir()},
                         set(sources.load_index(recovered)['documents']) | {'index.json'})
        self.assertFalse((directory / digest).exists())
        sources.store(recovered, 'future.txt', future)
        self.assertEqual((directory / digest).read_bytes(), future)

    def test_failed_replace_recovery_of_indexless_package_retains_no_sources(self):
        from services import sources
        pkg, manifest = self.package()
        (pkg / 'sources' / 'index.json').unlink()
        self.cols.create_failure = True
        with patch.object(self, 'package', return_value=(pkg, manifest)):
            job = self.import_replace()
        self.assertEqual(job['status'], 'failed')
        record = self.recovery_record()
        recovered = job['error_detail']['recovered_as']
        self.assertEqual(recovered, record['staging'])
        self.assertFalse(sources.collection_dir(recovered).exists(),
                         'an indexless package must leave no retained sources in the recovery copy')
        before = copy.deepcopy(self.cols.get(recovered).rows)
        recovery.sweep(self.client)
        self.assertEqual(self.cols.get(recovered).rows, before)
        self.assertEqual(len(before), len(self.original))
        recovery.discard(record, self.client)
        self.assertFalse(list(recovery._root().glob('*.json')))

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
            self.cols.create(name, description=kwargs.get('description'))
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

    def created_by_import(self, instance):
        self.cols.get('Corpus').description = importer._instance_description(instance)

    def test_interrupted_import_compares_records_not_only_count(self):
        self.created_by_import(importer._mark_started('Corpus', 2, 'job', self.original))
        self.cols.get('Corpus').rows[self.original[0]['id']]['properties']['content'] = 'mismatch'
        self.assertEqual(len(importer.sweep_interrupted_imports()), 1)
        self.assertFalse(self.cols.exists('Corpus'))

    def test_interrupted_import_read_failure_and_legacy_marker_preserve_data(self):
        self.created_by_import(importer._mark_started('Corpus', 2, 'job', self.original))
        self.cols.get('Corpus').fault = 'read'
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        marker = importer._marker_path('Corpus')
        marker.write_text(json.dumps({'collection': 'Corpus', 'expected_chunks': 1}))
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(marker.exists())
        self.assertTrue(self.cols.exists('Corpus'))

    def test_new_collection_under_unresolved_marker_name_is_kept(self):
        importer._mark_started('Corpus', 2, 'job', self.original)
        marker = importer._marker_path('Corpus')
        _, snapshot = importer._read_marker(marker)
        # A collection the user made after the marker could not be resolved.
        self.cols.get('Corpus').rows[self.original[0]['id']]['properties']['content'] = 'user data'
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertFalse(marker.exists())
        self.assertFalse(snapshot.exists())

    def test_unreadable_collection_identity_preserves_collection_and_marker(self):
        self.created_by_import(importer._mark_started('Corpus', 2, 'job', self.original))
        self.cols.get('Corpus').rows.clear()
        def unavailable():
            raise OSError('schema unavailable')
        self.cols.get('Corpus').config = SimpleNamespace(get=unavailable)
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertTrue(importer._marker_path('Corpus').exists())

    def legacy_marker(self, state):
        self.created_by_import(importer._mark_started('Corpus', 2, 'job', self.original))
        marker = importer._marker_path('Corpus')
        legacy = json.loads(marker.read_text())
        legacy['version'] = 3
        legacy['state'] = state
        legacy.pop('instance')
        marker.write_text(json.dumps(legacy))
        return marker, importer._markers_dir() / legacy['expected_snapshot']['file']

    def test_marker_without_instance_identity_never_deletes(self):
        marker, snapshot = self.legacy_marker('building')
        self.cols.get('Corpus').rows.clear()
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertTrue(marker.exists())
        self.assertTrue(snapshot.exists())
        logged = [str(call) for call in importer._log.warning.call_args_list]
        self.assertTrue(any('no instance identity' in line and str(marker) in line for line in logged), logged)
        importer._log.exception.assert_not_called()

    def test_legacy_marker_in_cleanup_is_finished_without_deleting(self):
        marker, snapshot = self.legacy_marker('cleanup')
        self.cols.get('Corpus').rows.clear()
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertNotIn('Corpus', self.cols.deleted)
        self.assertFalse(marker.exists())
        self.assertFalse(snapshot.exists())

    def test_import_creates_its_target_with_the_marker_identity(self):
        pkg, manifest = self.package()
        created = {}
        with patch.object(wc, '_create_collection_sync',
                          side_effect=lambda name, *args, **kwargs: created.update({name: kwargs})):
            importer._create_from_package('NewCorpus', pkg, 'a' * 32)
            importer._create_from_package('Staging', pkg)
        self.assertEqual(created['NewCorpus']['description'], importer._instance_description('a' * 32))
        self.assertIsNone(created['Staging']['description'])

    def test_orphaned_expectation_snapshots_are_swept_at_startup(self):
        self.created_by_import(importer._mark_started('Corpus', 2, 'job', self.original))
        self.cols.get('Corpus').fault = 'read'   # keeps the marker and its snapshot
        _, kept = importer._read_marker(importer._marker_path('Corpus'))
        orphan = importer._markers_dir() / (uuid.uuid4().hex + '.sqlite3')
        orphan.write_bytes(b'orphaned by a kill before its marker was written')
        unrelated = importer._markers_dir() / 'notes.sqlite3'
        unrelated.write_bytes(b'not an expectation snapshot name')
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertFalse(orphan.exists())
        self.assertTrue(kept.exists())
        self.assertTrue(unrelated.exists())

    def test_unreadable_marker_blocks_the_orphan_snapshot_sweep(self):
        orphan = importer._markers_dir() / (uuid.uuid4().hex + '.sqlite3')
        orphan.write_bytes(b'may belong to the unreadable marker')
        (importer._markers_dir() / 'Broken.json').write_text('{')
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(orphan.exists())

    def test_job_errors_name_sidecar_snapshots_relative_to_upload_dir(self):
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError) as error:
            self.rebuild()
        record = self.recovery_record()
        self.assertEqual(error.exception.detail['sidecar_snapshots'],
                         'collection_operations/' + record['operation_id'])
        self.cols.create_failure = False
        recovery.discard(record, self.client)
        self.cols.final_fault = 'reject'
        self.cols.items['Corpus'] = Collection('Corpus')
        job = self.import_replace()
        record = self.recovery_record()
        self.assertEqual(job['error_detail']['sidecar_snapshots'],
                         'collection_operations/' + record['operation_id'])
        self.assertTrue((Path(settings.upload_dir) / job['error_detail']['sidecar_snapshots']).is_dir())

    def test_absolute_sidecar_path_goes_to_the_server_log_only(self):
        # Covers tuning and both import failure branches: a general exception
        # (rejected batch) and a PackageError from the final insert.
        actual_insert = importer._insert_chunks
        def package_error(name, *args, **kwargs):
            if name == 'Corpus':
                raise importer.PackageError('IMPORT_FAILED', 'injected final insert failure')
            return actual_insert(name, *args, **kwargs)
        self.cols.create_failure = True
        with self.assertRaises(importer.PackageError) as error:
            self.rebuild()
        details = [error.exception.detail]
        records = [self.recovery_record()]
        self.cols.create_failure = False
        recovery.discard(records[-1], self.client)
        for fault in ('reject', 'package_error'):
            with self.subTest(fault=fault):
                self.cols.final_fault = 'reject' if fault == 'reject' else None
                self.cols.items['Corpus'] = Collection('Corpus')
                if fault == 'package_error':
                    with patch.object(importer, '_insert_chunks', side_effect=package_error):
                        job = self.import_replace()
                    self.assertEqual(job['error_code'], 'IMPORT_FAILED')
                    self.assertIn('injected final insert failure', job['error'])
                else:
                    job = self.import_replace()
                details.append(job['error_detail'])
                records.append(self.recovery_record())
                recovery.discard(records[-1], self.client)
        logged = ' '.join(str(call) for call in recovery.log.warning.call_args_list)
        for detail, record in zip(details, records):
            absolute = str(Path(settings.upload_dir) / 'collection_operations' / record['operation_id'])
            self.assertEqual(detail['sidecar_snapshots'], 'collection_operations/' + record['operation_id'])
            self.assertNotIn(settings.upload_dir, json.dumps(detail))
            self.assertIn(absolute, logged)

    def test_sidecar_reference_does_not_depend_on_where_the_root_resolves(self):
        # The reindex verifier keeps recovery records outside UPLOAD_DIR. The
        # reference must still be the documented form, not a failed relative_to.
        with tempfile.TemporaryDirectory() as outside, \
                patch.object(recovery, '_root', return_value=Path(outside) / 'owned'):
            self.assertEqual(recovery.sidecar_reference({'operation_id': 'op1', 'staging': 'S'}),
                             'collection_operations/op1')

    def test_stale_marking_discard_failure_is_logged_and_resumed_at_startup(self):
        def fail_marking():
            raise RuntimeError('session store unavailable')
        with patch.object(recovery.shutil, 'rmtree', side_effect=OSError('metadata cleanup failed')), \
                patch.object(tuning, '_log') as log, self.assertRaises(importer.PackageError) as error:
            tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, None,
                            before_replace=fail_marking)
        self.assertIn('original collection is unchanged', error.exception.message)
        log.exception.assert_called_once()
        paths = list(recovery._root().glob('*.json'))
        self.assertEqual(len(paths), 1)
        record = json.loads(paths[0].read_text())
        self.assertEqual(record['state'], 'cleanup')
        self.assertEqual(recovery.sweep(self.client), [record['staging']])
        self.assertEqual(list(recovery._root().iterdir()), [])
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertNotIn('Corpus', self.cols.deleted)

    def test_stale_marking_failure_before_cutover_discards_copy_and_keeps_original(self):
        def fail_marking():
            raise RuntimeError('session store unavailable')
        before = copy.deepcopy(self.cols.get('Corpus').rows)
        with self.assertRaises(importer.PackageError) as error:
            tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, None,
                            before_replace=fail_marking)
        self.assertEqual(error.exception.code, 'TUNE_FAILED')
        self.assertIn('could not be marked stale', error.exception.message)
        self.assertIn('original collection is unchanged', error.exception.message)
        self.assertNotIn('recovered_as', error.exception.detail or {})
        self.assertEqual(self.cols.get('Corpus').rows, before)
        self.assertEqual(list(self.cols.items), ['Corpus'])
        self.assertEqual(list(recovery._root().iterdir()), [])

    def test_cutover_record_failure_is_reported_as_such(self):
        # #263: the marking ran (or wasn't needed); only the cutover record failed.
        for marking in (True, False):
            with self.subTest(marking=marking):
                calls = []
                before = copy.deepcopy(self.cols.get('Corpus').rows)
                with patch.object(recovery, 'begin_cutover', side_effect=OSError('disk full')), \
                        self.assertRaises(importer.PackageError) as error:
                    tuning._rebuild('Corpus', [r['properties'] for r in self.original], None, None, None,
                                    before_replace=(lambda: calls.append('marked')) if marking else None)
                message = error.exception.message
                self.assertEqual(error.exception.code, 'TUNE_FAILED')
                self.assertIn('cutover record could not be written', message)
                self.assertIn('original collection is unchanged', message)
                self.assertNotIn('could not be marked stale', message)
                self.assertEqual('already marked stale' in message, marking)
                self.assertEqual(calls, ['marked'] if marking else [])
                self.assertNotIn('recovered_as', error.exception.detail or {})
                self.assertEqual(self.cols.get('Corpus').rows, before)
                self.assertEqual(list(self.cols.items), ['Corpus'])
                self.assertEqual(list(recovery._root().iterdir()), [])

    # ── reviewer-added (#126 item 1, 7) ──

    def test_every_import_mode_binds_its_target_to_the_marker_instance(self):
        seen = {}
        actual_finish = importer._mark_finished
        def finish(collection):
            data, _ = importer._read_marker(importer._marker_path(collection))
            others = {name: col.description for name, col in self.cols.items.items() if name != collection}
            seen[collection] = (data['version'], data['instance'],
                                self.cols.get(collection).description, others)
            actual_finish(collection)
        for mode in ('abort', 'rename', 'replace'):
            with self.subTest(mode=mode):
                seen.clear()
                self.cols.items.clear()
                if mode != 'abort':
                    self.cols.create('Corpus')
                pkg, manifest = self.package()
                importer._jobs['job'] = dict(chunks_written=0)
                with patch.object(importer.packager, 'open_package', return_value=(pkg, manifest)), \
                        patch.object(importer.packager, 'verify_digests'), \
                        patch.object(importer, '_check_embedding'), \
                        patch.object(importer, '_ensure_models', return_value=[]), \
                        patch.object(importer, '_mark_finished', side_effect=finish):
                    importer._run('job', 'fixture.tar.gz', mode)
                job = importer._jobs['job']
                self.assertEqual(job['status'], 'completed', job)
                self.assertEqual(list(seen), [job['collection']])
                version, instance, description, others = seen[job['collection']]
                self.assertEqual(version, 4)
                self.assertRegex(instance, r'^[0-9a-f]{32}$')
                self.assertEqual(description, importer._instance_description(instance))
                # Staging copies (replace) and pre-existing collections carry no token.
                self.assertTrue(all(value is None for value in others.values()), others)

    def test_v4_marker_with_invalid_instance_never_deletes(self):
        for bad in ('A' * 32, 'a' * 31, 7, None, '<missing>'):
            with self.subTest(instance=bad):
                self.cols.items['Corpus'] = Collection('Corpus')
                batch_write.insert(self.cols.get('Corpus'), self.original)
                importer._mark_started('Corpus', 2, 'job', self.original)
                marker = importer._marker_path('Corpus')
                data = json.loads(marker.read_text())
                if bad == '<missing>':
                    data.pop('instance')
                else:
                    data['instance'] = bad
                marker.write_text(json.dumps(data))
                if isinstance(bad, str):
                    self.cols.get('Corpus').description = importer._instance_description(bad)
                self.cols.get('Corpus').rows.clear()   # would be deleted if the marker were trusted
                self.assertEqual(importer.sweep_interrupted_imports(), [])
                self.assertTrue(self.cols.exists('Corpus'))
                self.assertNotIn('Corpus', self.cols.deleted)
                self.assertTrue(marker.exists())
                self.assertTrue((importer._markers_dir() / data['expected_snapshot']['file']).exists())
                marker.unlink()

    def test_other_import_token_keeps_collection_and_retires_marker(self):
        importer._mark_started('Corpus', 2, 'job', self.original)
        marker = importer._marker_path('Corpus')
        _, snapshot = importer._read_marker(marker)
        self.cols.get('Corpus').description = importer._instance_description(uuid.uuid4().hex)
        self.cols.get('Corpus').rows.clear()
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(self.cols.exists('Corpus'))
        self.assertNotIn('Corpus', self.cols.deleted)
        self.assertFalse(marker.exists())
        self.assertFalse(snapshot.exists())

    def test_orphan_sweep_keeps_snapshots_named_by_invalid_markers_and_symlinks(self):
        importer._mark_started('Corpus', 2, 'job', self.original)
        marker = importer._marker_path('Corpus')
        data = json.loads(marker.read_text())
        data['version'] = 2          # parseable, but not valid ownership: preserved by the sweep
        marker.write_text(json.dumps(data))
        named = importer._markers_dir() / data['expected_snapshot']['file']
        target = self.root / 'outside.sqlite3'
        target.write_bytes(b'outside the markers directory')
        link = importer._markers_dir() / (uuid.uuid4().hex + '.sqlite3')
        link.symlink_to(target)
        self.assertEqual(importer.sweep_interrupted_imports(), [])
        self.assertTrue(marker.exists())
        self.assertTrue(named.exists())
        self.assertTrue(link.is_symlink())
        self.assertTrue(target.exists())

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


class SourceCoverageTests(unittest.TestCase):
    def test_rechunk_requires_unambiguous_originals_for_all_stored_files(self):
        from services import sources
        for operation in ("rechunk", "reembed"):
            for missing in ("legacy.txt", None, "retained.txt"):
                with self.subTest(operation=operation, missing=missing), tempfile.TemporaryDirectory() as tmp:
                    cols = Collections()
                    cols.create("Corpus")
                    original = records(2)
                    original[0]["properties"]["source_file"] = missing
                    original[1]["properties"]["source_file"] = "retained.txt"
                    cols.get("Corpus").rows = {r["id"]: copy.deepcopy(r) for r in original}
                    with patch.object(settings, "upload_dir", tmp), patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)):
                        sources.store("Corpus", "retained.txt", b"retained original")
                        if missing == "retained.txt":
                            sources.store("Corpus", "retained.txt", b"ambiguous second original")
                        job = {}
                        with patch.dict(tuning._jobs, {"coverage": job}), patch.object(tuning, "_parse_file") as parse:
                            tuning._run("coverage", "Corpus", operation, {"chunking": {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0, "similarity_threshold": .85, "min_chunk_size": 0}})
                        self.assertEqual(job["status"], "failed")
                        self.assertEqual(job["error_code"], "SOURCES_REQUIRED")
                        parse.assert_not_called()
                        self.assertEqual(cols.get("Corpus").rows, {r["id"]: r for r in original})
                        self.assertEqual(cols.deleted, [])
                        self.assertEqual(set(cols.items), {"Corpus"})
                        self.assertEqual(list(recovery._root().glob("*.json")), [])

    def test_fully_retained_collection_completes_real_cutover(self):
        from services import sources
        with tempfile.TemporaryDirectory() as tmp:
            cols = Collections()
            cols.create("Corpus")
            original = records(1)[0]
            original["properties"]["source_file"] = "retained.txt"
            cols.get("Corpus").rows[original["id"]] = original
            with patch.object(settings, "upload_dir", tmp), patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)), patch.object(wc, "_create_collection_sync", side_effect=lambda name, *a, **kw: cols.create(name)), patch.object(wc, "_collection_config_sync", return_value={"index_type": "hnsw", "distance_metric": "cosine", "hnsw_config": {}}), patch.object(tuning, "_parse_file", side_effect=lambda p: (p.read_text(), [])), patch.object(tuning, "do_chunk", side_effect=lambda **kw: [kw["text"]]):
                sources.store("Corpus", "retained.txt", b"retained original")
                job = {}
                with patch.dict(tuning._jobs, {"coverage": job}):
                    tuning._run("coverage", "Corpus", "rechunk", {"chunking": {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0, "similarity_threshold": .85, "min_chunk_size": 0}})
                self.assertEqual(job["status"], "completed", job)
                self.assertEqual([r["properties"]["content"] for r in cols.get("Corpus").rows.values()], ["retained original"])
                self.assertEqual(set(cols.items), {"Corpus"})
                self.assertEqual(list(recovery._root().glob("*.json")), [])


    # Each case: retained originals as (name, bytes), stored chunks as
    # (source_file, chunk_index) with None for no chunk_index, and the
    # names the coverage rule must report.
    DROP_CASES = {
        # Stored before #22 made re-ingesting replace: one name, two versions.
        "a name with two retained versions": (
            [("a.txt", b"v1"), ("a.txt", b"v2")], [("a.txt", 0), ("a.txt", 1)], ["a.txt"]),
        "re-upload whose retention failed": (
            [("a.txt", b"v1")], [("a.txt", 0), ("a.txt", 1), ("a.txt", 0), ("a.txt", 1)], ["a.txt"]),
        "identical re-upload, retained twice": (
            [("a.txt", b"same"), ("a.txt", b"same")], [("a.txt", 0), ("a.txt", 0)], ["a.txt"]),
        "two chunks with no chunk_index": (
            [("a.txt", b"v1")], [("a.txt", None), ("a.txt", None)], ["a.txt"]),
    }

    @staticmethod
    def corpus(cols, chunks):
        cols.create("Corpus")
        rows = records(len(chunks))
        for row, (name, index) in zip(rows, chunks):
            row["properties"]["source_file"] = name
            if index is not None:
                row["properties"]["chunk_index"] = index
        cols.get("Corpus").rows = {r["id"]: copy.deepcopy(r) for r in rows}
        return rows

    def test_rechunk_refuses_names_it_would_drop(self):
        from services import sources
        chunking = {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0, "similarity_threshold": .85, "min_chunk_size": 0}
        for operation in ("rechunk", "reembed"):
            for case, (retained, chunks, expected) in self.DROP_CASES.items():
                with self.subTest(operation=operation, case=case), tempfile.TemporaryDirectory() as tmp:
                    cols = Collections()
                    original = self.corpus(cols, chunks)
                    with patch.object(settings, "upload_dir", tmp), patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)):
                        for name, data in retained:
                            sources.store("Corpus", name, data)
                        job = {}
                        with patch.dict(tuning._jobs, {"coverage": job}), patch.object(tuning, "_parse_file") as parse:
                            tuning._run("coverage", "Corpus", operation, {"chunking": dict(chunking)})
                        self.assertEqual(job["status"], "failed")
                        self.assertEqual(job["error_code"], "SOURCES_REQUIRED")
                        self.assertEqual(job["error_detail"]["uncovered_source_files"], expected)
                        self.assertEqual(job["error_detail"]["uncovered_source_files_omitted"], 0)
                        parse.assert_not_called()
                        self.assertEqual(cols.get("Corpus").rows, {r["id"]: r for r in original})
                        self.assertEqual(cols.deleted, [])
                        self.assertEqual(set(cols.items), {"Corpus"})
                        self.assertEqual(list(recovery._root().glob("*.json")), [])

    def test_single_chunk_set_per_name_completes(self):
        from services import sources
        with tempfile.TemporaryDirectory() as tmp:
            cols = Collections()
            self.corpus(cols, [("a.txt", 0), ("a.txt", 1), ("a.txt", 2)])
            with patch.object(settings, "upload_dir", tmp), patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)), patch.object(wc, "_create_collection_sync", side_effect=lambda name, *a, **kw: cols.create(name)), patch.object(wc, "_collection_config_sync", return_value={"index_type": "hnsw", "distance_metric": "cosine", "hnsw_config": {}}), patch.object(tuning, "_parse_file", side_effect=lambda p: (p.read_text(), [])), patch.object(tuning, "do_chunk", side_effect=lambda **kw: [kw["text"]]):
                sources.store("Corpus", "a.txt", b"retained original")
                job = {}
                with patch.dict(tuning._jobs, {"coverage": job}):
                    tuning._run("coverage", "Corpus", "rechunk", {"chunking": {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0, "similarity_threshold": .85, "min_chunk_size": 0}})
                self.assertEqual(job["status"], "completed", job)
                self.assertEqual([r["properties"]["source_file"] for r in cols.get("Corpus").rows.values()], ["a.txt"])


    def test_second_name_for_the_same_content_is_rebuilt_under_both_names(self):
        # #22 lifted this refusal: one chunk set per (digest, name), as ingested.
        from services import sources
        with tempfile.TemporaryDirectory() as tmp:
            cols = Collections()
            self.corpus(cols, [("a.txt", 0), ("docs/b.txt", 0)])
            with patch.object(settings, "upload_dir", tmp), patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)), patch.object(wc, "_create_collection_sync", side_effect=lambda name, *a, **kw: cols.create(name)), patch.object(wc, "_collection_config_sync", return_value={"index_type": "hnsw", "distance_metric": "cosine", "hnsw_config": {}}), patch.object(tuning, "_parse_file", side_effect=lambda p: (p.read_text(), [])), patch.object(tuning, "do_chunk", side_effect=lambda **kw: [kw["text"]]):
                sources.store("Corpus", "a.txt", b"same original")
                sources.store("Corpus", "docs/b.txt", b"same original")
                job = {}
                with patch.dict(tuning._jobs, {"coverage": job}):
                    tuning._run("coverage", "Corpus", "rechunk", {"chunking": {"strategy": "fixed", "chunk_size": 1000, "chunk_overlap": 0, "similarity_threshold": .85, "min_chunk_size": 0}})
                self.assertEqual(job["status"], "completed", job)
                rows = cols.get("Corpus").rows.values()
                self.assertEqual(sorted(r["properties"]["source_file"] for r in rows), ["a.txt", "docs/b.txt"])
                self.assertEqual({r["properties"]["content"] for r in rows}, {"same original"})

    def test_tune_options_match_the_refusal(self):
        from routers import tuning as tuning_router
        from services import sources
        chunks_only = ("No original documents were retained, so this collection cannot be "
                       "re-chunked. Re-embedding works from the stored chunk text, which "
                       "leaves chunk boundaries unchanged.")
        refused = ("Some stored source files have no single retained original, or a "
                   "retained original is missing, so this collection cannot be "
                   "re-chunked. Re-embedding works from the stored chunk text, which "
                   "leaves chunk boundaries unchanged, and re-indexing is available.")
        nothing_to_rebuild = ("No retained original file has stored chunks, so this collection "
                              "cannot be re-chunked: there is nothing to rebuild. Re-embedding works "
                              "from the stored chunk text, which leaves chunk boundaries unchanged, "
                              "and re-indexing is available.")
        two_versions = self.DROP_CASES["a name with two retained versions"]
        repeated_set = self.DROP_CASES["re-upload whose retention failed"]
        cases = {
            "nothing retained": ([], [("a.txt", 0)], False, chunks_only, False),
            "fully covered": ([("a.txt", b"v1")], [("a.txt", 0), ("a.txt", 1)], True,
                              "Every tuning operation is available.", False),
            "second name for the same content": ([("a.txt", b"same"), ("b.txt", b"same")],
                                                  [("a.txt", 0), ("b.txt", 0)], True,
                                                  "Every tuning operation is available.", False),
            "a name with two retained versions": (two_versions[0], two_versions[1], False, refused, False),
            "repeated chunk set": (repeated_set[0], repeated_set[1], False, refused, False),
            "retained original missing from disk": ([("a.txt", b"v1")], [("a.txt", 0)], False, refused, True),
            # #328: every retained file produced no chunks; the reason comes
            # first, as the start refuses it before the others.
            "retained originals with no stored chunks": ([("a.txt", b"v1")], [], False,
                                                         nothing_to_rebuild, False),
            "nothing to rebuild and a blob missing": ([("a.txt", b"v1")], [("b.txt", 0)], False,
                                                      nothing_to_rebuild, True),
        }
        for case, (retained, chunks, can_rechunk, note, drop_blob) in cases.items():
            with self.subTest(case=case), tempfile.TemporaryDirectory() as tmp:
                cols = Collections()
                self.corpus(cols, chunks)
                with patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)), patch.object(tuning_router.wc, "collection_exists", new_callable=AsyncMock, return_value=True):
                    for name, data in retained:
                        digest = sources.store("Corpus", name, data)
                    if drop_blob:
                        sources.blob_path("Corpus", digest).unlink()
                    response = asyncio.run(tuning_router.tune_options("Corpus"))
                self.assertEqual(response.can_rechunk, can_rechunk)
                self.assertEqual(response.note, note)
                self.assertEqual(response.fidelity, "with-sources" if retained else "chunks-only")

    def test_tune_options_report_a_linked_original_as_typed_error(self):
        # can_rechunk reads every blob path, so a retained original that is a
        # link is the index-invalid 409, not a 500 and not can_rechunk: true.
        from routers import tuning as tuning_router
        from services import sources
        with tempfile.TemporaryDirectory() as tmp:
            cols = Collections()
            self.corpus(cols, [("a.txt", 0)])
            with patch.object(settings, "sources_dir", str(Path(tmp)/"sources")), patch.object(wc, "get_client", return_value=SimpleNamespace(collections=cols)), patch.object(tuning_router.wc, "collection_exists", new_callable=AsyncMock, return_value=True):
                digest = sources.store("Corpus", "a.txt", b"v1")
                blob = sources.collection_dir("Corpus") / digest
                target = Path(tmp) / "outside.txt"
                target.write_bytes(b"v1")
                blob.unlink()
                blob.symlink_to(target)
                response = asyncio.run(tuning_router.tune_options("Corpus"))
            self.assertEqual(response.status_code, 409)
            self.assertEqual(json.loads(response.body)["error"]["code"], "SOURCE_INDEX_INVALID")


if __name__ == '__main__':
    unittest.main()
