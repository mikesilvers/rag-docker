"""Retained-session metadata and explicit historical export policy."""
import asyncio
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2]/'api'))
from config import settings
from models.schemas import SessionResponse
from services import goldstandard as gs

NEEDS_REPOSITORY = 'needs the whole repository mounted (see scripts/verify/README.md)'


def repository_root():
    parents = Path(__file__).resolve().parents
    root = parents[2] if len(parents) > 2 else None
    return root if root is not None and (root / 'IMPLEMENTATION.md').is_file() else None


def session():
    pairs=[{'pair_id':f'pair{i}','question':'Inert question','answer':'Inert answer',
            'contexts':['Inert historical context'],'ground_truth':'Inert truth',
            'source_file':'inert.txt','chunk_index':i,'status':status}
           for i,status in enumerate(('approved','edited','pending','rejected'))]
    return {'session_id':'gs_47abcdef','collection':'ValidityFixture','status':'completed',
            'pairs_total':4,'pairs_completed':4,'pairs':pairs}


class ValidityServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.upload=patch.object(settings,'upload_dir',self.temp.name);self.upload.start();self.addCleanup(self.upload.stop)
        self.original=gs._sessions;gs._sessions={};self.addCleanup(setattr,gs,'_sessions',self.original)
        self.data=session();gs.store_session(self.data)

    def test_default_validity_and_actual_disk_marking_survive_reload(self):
        response=SessionResponse.model_validate(self.data)
        self.assertFalse(response.stale);self.assertFalse(response.orphaned)
        self.assertEqual(gs.mark_stale('ValidityFixture','Synthetic rechunk'),1)
        self.assertEqual(gs.mark_orphaned('ValidityFixture','Synthetic deletion'),1)
        expected=gs.get_session(self.data['session_id']);gs._sessions={};gs.load_sessions_from_disk()
        self.assertEqual(gs.get_session(self.data['session_id']),expected)

    def test_tuning_invalidates_both_backend_aliases_without_remapping(self):
        from services import tuning
        for caller in ('ValidityFixture', 'validityFixture'):
            for operation in ('reembed', 'rechunk'):
                with self.subTest(caller=caller, operation=operation):
                    for sid, name in (('gs_11111111', 'ValidityFixture'), ('gs_22222222', 'validityFixture'), ('gs_33333333', 'Validityfixture')):
                        data = session()
                        data.update(session_id=sid, collection=name)
                        gs.store_session(data)
                    job = {}
                    def rebuild(*args, **kwargs):
                        kwargs['before_replace']()
                        return 1
                    with patch.dict(tuning._jobs, {'alias': job}), patch.object(tuning.sources, 'has_sources', return_value=True), patch.object(tuning, '_chunks_from_sources', return_value=[{'content': 'Inert'}]), patch.object(tuning, '_existing_chunks', return_value=[{'content': 'Inert'}]), patch.object(tuning, '_rebuild', side_effect=rebuild):
                        tuning._run('alias', caller, operation, {'chunking': {} if operation == 'rechunk' else None})
                    self.assertEqual(job['status'], 'completed')
                    gs._sessions = {}
                    gs.load_sessions_from_disk()
                    for sid, name in (('gs_11111111', 'ValidityFixture'), ('gs_22222222', 'validityFixture')):
                        current = gs.get_session(sid)
                        self.assertEqual(current['collection'], name)
                        self.assertTrue(current['stale'])
                        with self.assertRaises(gs.GoldStandardError):
                            asyncio.run(gs.save_session(sid, 'current.json'))
                    self.assertFalse(gs.get_session('gs_33333333').get('stale', False))

    def test_historical_guard_rejects_before_export_writes(self):
        for flags in ({'stale':True},{'orphaned':True},{'stale':True,'orphaned':True}):
            self.data.pop('stale',None);self.data.pop('orphaned',None);self.data.update(flags)
            gs.store_session(self.data)
            before=copy.deepcopy(self.data)
            with patch.object(gs,'_save_export_sync') as write:
                with self.assertRaises(gs.GoldStandardError) as error:
                    asyncio.run(gs.save_session(self.data['session_id'],'guard.json'))
                self.assertEqual((error.exception.code,error.exception.status),('HISTORICAL_SESSION',409))
                write.assert_not_called()
            self.assertEqual(before,self.data)

    def test_explicit_service_export_reports_metadata_without_mutating_history(self):
        self.data.update(stale=True,stale_reason='Synthetic stale',stale_at='2026-09-28T00:00:00+00:00')
        gs.store_session(self.data)
        before=copy.deepcopy(self.data)
        result=asyncio.run(gs.save_session(self.data['session_id'],'historical.json',True))
        self.assertTrue(result['historical']);self.assertEqual((result['pairs_saved'],result['pairs_excluded']),(2,2))
        self.assertEqual(before,self.data)
        rows=json.loads((Path(self.temp.name)/gs.EXPORT_DIRNAME/'historical.json').read_text())
        self.assertTrue(all(set(row)=={'question','answer','contexts','ground_truth'} for row in rows))

    def test_boolean_only_choice_and_unknown_identity(self):
        from models.schemas import SaveRequest
        for value in (1,0,'true','false',None,[],{}):
            with self.assertRaises(ValueError):SaveRequest(session_id=self.data['session_id'],allow_historical=value)
        with self.assertRaises(ValueError):asyncio.run(gs.save_session(self.data['session_id'],None,1))
        self.assertIsNone(asyncio.run(gs.save_session('gs_00000000',None,True)))

    def test_replacement_failure_matrix_flags_only_after_preparation(self):
        from services import tuning
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        for failure in ('stage','delete','create','write'):
            self.data.pop('stale',None);self.data.pop('stale_reason',None);self.data.pop('stale_at',None)
            gs.store_session(self.data)
            client=MagicMock();stage=MagicMock();target=MagicMock()
            obj=SimpleNamespace(uuid='00000000-0000-0000-0000-000000000001',vector={'default':[0.1]},properties={'content':'Inert'})
            stage.iterator.return_value=[obj];stage.aggregate.over_all.return_value=SimpleNamespace(total_count=1)
            ownership={'staging':'ValidityFixture__test','state':'scratch','operation_id':'test'}
            client.collections.get.side_effect=lambda name:target if name=='ValidityFixture' else stage
            def delete(name):
                if name=='ValidityFixture':
                    self.assertTrue(gs.get_session(self.data['session_id'])['stale'])
                    with self.assertRaises(gs.GoldStandardError):asyncio.run(gs.save_session(self.data['session_id'],'guard.json'))
                    if failure=='delete':raise RuntimeError('delete fault')
            client.collections.delete.side_effect=delete
            def create(name,*args,**kwargs):
                if name=='ValidityFixture' and failure=='create':raise RuntimeError('create fault')
            insert=RuntimeError('stage fault') if failure=='stage' else None
            with patch.object(tuning.wc,'get_client',return_value=client), \
                 patch.object(tuning.wc,'_collection_config_sync',return_value={'index_type':'hnsw','distance_metric':'cosine','hnsw_config':{}}), \
                 patch.object(tuning.wc,'_create_collection_sync',side_effect=create), \
                 patch.object(tuning.wc,'_insert_chunks_sync',side_effect=insert), \
                 patch.object(tuning.collection_recovery,'begin',return_value=ownership), \
                 patch.object(tuning.collection_recovery,'retain',side_effect=lambda owner:owner.update(state='recovery')), \
                 patch.object(tuning.collection_recovery,'discard'), \
                 patch.object(tuning.batch_write,'insert',side_effect=RuntimeError('write fault') if failure=='write' else None):
                with self.assertRaises(Exception):tuning._rebuild('ValidityFixture',[{'content':'Inert'}],None,None,None,before_replace=lambda:gs.mark_stale('ValidityFixture','Synthetic replacement'))
            current=gs.get_session(self.data['session_id'])
            self.assertEqual(bool(current.get('stale')),failure!='stage')
            if failure!='stage':self.assertTrue(current['stale_at'])

    def test_failed_cutover_marks_both_backend_aliases(self):
        # #191: tuning.py's after-cutover failure paths call mark_stale with the
        # caller's spelling and no before_replace; both aliases must be marked.
        from services import tuning
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        for caller in ('ValidityFixture','validityFixture'):
            with self.subTest(caller=caller):
                for sid,name in (('gs_11111111','ValidityFixture'),('gs_22222222','validityFixture'),('gs_33333333','Validityfixture')):
                    data=session();data.update(session_id=sid,collection=name);gs.store_session(data)
                client=MagicMock();stage=MagicMock()
                stage.aggregate.over_all.return_value=SimpleNamespace(total_count=1)
                client.collections.get.return_value=stage
                ownership={'staging':'ValidityFixture__test','state':'scratch','operation_id':'test'}
                def create(name,*args,**kwargs):
                    if name=='ValidityFixture':raise RuntimeError('create fault')
                with patch.object(tuning.wc,'get_client',return_value=client), \
                     patch.object(tuning.wc,'_collection_config_sync',return_value={'index_type':'hnsw','distance_metric':'cosine','hnsw_config':{}}), \
                     patch.object(tuning.wc,'_create_collection_sync',side_effect=create), \
                     patch.object(tuning.wc,'_insert_chunks_sync'), \
                     patch.object(tuning.collection_recovery,'begin',return_value=ownership), \
                     patch.object(tuning.collection_recovery,'retain',side_effect=lambda owner,**kw:owner.update(state='recovery')), \
                     patch.object(tuning.collection_recovery,'sidecar_reference',return_value=None), \
                     patch.object(tuning.collection_recovery,'discard'):
                    with self.assertRaises(Exception):
                        tuning._rebuild(caller,[{'content':'Inert'}],None,None,None,source_collection=caller)
                gs._sessions={};gs.load_sessions_from_disk()
                for sid,name in (('gs_11111111','ValidityFixture'),('gs_22222222','validityFixture')):
                    current=gs.get_session(sid)
                    self.assertEqual(current['collection'],name)
                    self.assertTrue(current.get('stale'),(caller,name))
                    self.assertIn('cutover',current['stale_reason'])
                self.assertFalse(gs.get_session('gs_33333333').get('stale',False))

    def test_successful_identity_preserving_reindex_keeps_validity_semantics(self):
        from services import tuning
        job={'status':'queued'}
        with patch.dict(tuning._jobs,{'synthetic':job}), \
             patch.object(tuning.sources,'has_sources',return_value=False), \
             patch.object(tuning,'_existing_records',return_value=[{'id':'00000000-0000-0000-0000-000000000001','vector':[0.1],'properties':{'content':'Inert'}}]), \
             patch.object(tuning,'_rebuild',return_value=1) as rebuild, \
             patch.object(gs,'mark_stale') as mark:
            tuning._run('synthetic','ValidityFixture','reindex',{})
        self.assertIsNone(rebuild.call_args.kwargs['before_replace']);mark.assert_not_called()
        self.assertEqual(job['status'],'completed');self.assertTrue(any('unchanged' in note for note in job['notes']))

    def test_identity_changing_runs_flag_sessions_before_replacement(self):
        from services import tuning
        cases=(('rechunk',{'chunking':{}},True,'re-chunked'),
               ('reembed',{},True,'re-embedded'),
               ('reembed',{},False,'stored chunk text'),
               ('reembed',{'chunking':{}},True,'re-chunked and re-embedded'))
        for operation,params,has_sources,reason in cases:
            with self.subTest(operation=operation,params=params,has_sources=has_sources):
                for key in ('stale','stale_reason','stale_at'):self.data.pop(key,None)
                gs.store_session(self.data)
                seen={}
                def rebuild(collection,properties,index_type,distance_metric,progress,before_replace=None, **kwargs):
                    self.assertIsNotNone(before_replace)
                    self.assertFalse(self.data.get('stale'))
                    before_replace()
                    current=gs.get_session(self.data['session_id'])
                    seen['stale']=current.get('stale');seen['reason']=current.get('stale_reason')
                    with self.assertRaises(gs.GoldStandardError) as error:
                        asyncio.run(gs.save_session(self.data['session_id'],'guard.json'))
                    seen['code']=error.exception.code
                    return 1
                job={'status':'queued'}
                with patch.dict(tuning._jobs,{'synthetic':job}), \
                     patch.object(tuning.sources,'has_sources',return_value=has_sources), \
                     patch.object(tuning,'_chunks_from_sources',return_value=[{'content':'Inert'}]), \
                     patch.object(tuning,'_existing_chunks',return_value=[{'content':'Inert'}]), \
                     patch.object(tuning,'_rebuild',side_effect=rebuild):
                    tuning._run('synthetic','ValidityFixture',operation,params)
                self.assertEqual(job['status'],'completed',job)
                self.assertTrue(seen['stale']);self.assertIn(reason,seen['reason'])
                self.assertEqual(seen['code'],'HISTORICAL_SESSION')
                gs._sessions={};gs.load_sessions_from_disk()
                reloaded=gs.get_session(self.data['session_id'])
                self.assertTrue(reloaded['stale']);self.assertIn(reason,reloaded['stale_reason'])
                self.data=reloaded

    def test_cutover_requires_a_durable_session_or_pending_marker(self):
        from services import tuning
        for pending_saved in (False, True):
            with self.subTest(pending_saved=pending_saved):
                job = {}
                reached_delete = []
                def rebuild(*args, **kwargs):
                    kwargs['before_replace']()
                    reached_delete.append(True)
                    return 1
                with patch.dict(tuning._jobs, {'barrier': job}), \
                     patch.object(tuning.sources, 'has_sources', return_value=False), \
                     patch.object(tuning, '_existing_chunks', return_value=[{'content': 'Inert'}]), \
                     patch.object(tuning, '_rebuild', side_effect=rebuild), \
                     patch.object(gs, '_save_session_sync', side_effect=OSError('disk full')), \
                     patch.object(gs, '_write_pending_marker', return_value=pending_saved):
                    tuning._run('barrier', 'ValidityFixture', 'reembed', {})
                self.assertEqual(bool(reached_delete), pending_saved)
                self.assertEqual(job['status'], 'completed' if pending_saved else 'failed')
                self.data = session()
                gs.store_session(self.data)

    def _run_real_rebuild(self, jobid):
        # Real _run -> real _rebuild, with only the backend mocked, so develop's
        # pre-cutover abort branch is exercised with the real durable marker.
        from services import tuning
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        client = MagicMock(); stage = MagicMock(); target = MagicMock()
        stage.aggregate.over_all.return_value = SimpleNamespace(total_count=1)
        stage.iterator.return_value = [SimpleNamespace(uuid='00000000-0000-0000-0000-000000000001',
                                                       vector={'default': [0.1]}, properties={'content': 'Inert'})]
        client.collections.get.side_effect = lambda name: target if name == 'ValidityFixture' else stage
        events = []
        marker = gs._pending_path(self.data['session_id'])
        client.collections.delete.side_effect = lambda name: events.append(('delete', name, marker.exists()))
        ownership = {'staging': 'ValidityFixture__tuning_test', 'state': 'scratch', 'operation_id': 'test'}
        job = {'status': 'queued'}
        with patch.dict(tuning._jobs, {jobid: job}), \
             patch.object(tuning.sources, 'has_sources', return_value=False), \
             patch.object(tuning, '_existing_chunks', return_value=[{'content': 'Inert'}]), \
             patch.object(tuning.wc, 'get_client', return_value=client), \
             patch.object(tuning.wc, '_collection_config_sync', return_value={'index_type': 'hnsw', 'distance_metric': 'cosine', 'hnsw_config': {}}), \
             patch.object(tuning.wc, '_create_collection_sync'), \
             patch.object(tuning.wc, '_insert_chunks_sync'), \
             patch.object(tuning.collection_recovery, 'begin', return_value=ownership), \
             patch.object(tuning.collection_recovery, 'retain', side_effect=lambda owner, **kw: owner.update(state='recovery')), \
             patch.object(tuning.collection_recovery, 'begin_cutover', side_effect=lambda owner: events.append(('cutover',))), \
             patch.object(tuning.collection_recovery, 'sidecar_reference', return_value=None), \
             patch.object(tuning.collection_recovery, 'discard', side_effect=lambda owner, c: events.append(('discard', owner['staging']))), \
             patch.object(tuning.batch_write, 'insert', return_value=1), \
             patch.object(gs, '_save_session_sync', side_effect=OSError('Owned disk fault')):
            yield job, events

    def test_unpersistable_marker_aborts_real_rebuild_before_live_delete(self):
        # Reviewer (#93): both writes fail -> develop's pre-cutover branch must
        # report the original as unchanged, never delete it, and drop staging.
        with patch.object(gs, '_write_pending_marker', return_value=False):
            for job, events in self._run_real_rebuild('abort'):
                from services import tuning
                tuning._run('abort', 'ValidityFixture', 'reembed', {})
        self.assertEqual(job['status'], 'failed', job)
        self.assertEqual(job['error_code'], 'TUNE_FAILED')
        self.assertIn('historical marker could not be persisted', job['error'])
        self.assertIn('original collection is unchanged', job['error'])
        self.assertNotIn('recovered_as', job.get('error_detail') or {})
        self.assertEqual(job.get('chunks_written', 0), 0)
        self.assertEqual(events, [('discard', 'ValidityFixture__tuning_test')])
        gs._sessions = {}; gs.load_sessions_from_disk()
        self.assertFalse(gs.get_session(self.data['session_id']).get('stale', False))

    def test_pending_marker_alone_lets_real_rebuild_replace_and_survives_restart(self):
        # Reviewer (#93): session write fails but the pending marker is durable
        # before delete; after a restart the session still loads historical.
        for job, events in self._run_real_rebuild('pending'):
            from services import tuning
            tuning._run('pending', 'ValidityFixture', 'reembed', {})
        self.assertEqual(job['status'], 'completed', job)
        self.assertEqual(events[:2], [('cutover',), ('delete', 'ValidityFixture', True)])
        gs._sessions = {}; gs.load_sessions_from_disk()
        reloaded = gs.get_session(self.data['session_id'])
        self.assertTrue(reloaded['stale']); self.assertIn('re-embedded', reloaded['stale_reason'])
        with self.assertRaises(gs.GoldStandardError) as error:
            asyncio.run(gs.save_session(self.data['session_id'], 'guard.json'))
        self.assertEqual(error.exception.code, 'HISTORICAL_SESSION')

    def test_post_cutover_marking_stays_best_effort(self):
        # Reviewer (#93): only the pre-replacement call is strict; a failed
        # cutover still reports the original failure when markers cannot persist.
        with patch.object(gs, '_save_session_sync', side_effect=OSError('Owned disk fault')), \
             patch.object(gs, '_write_pending_marker', return_value=False):
            self.assertEqual(gs.mark_stale('ValidityFixture', 'Synthetic after cutover'), 0)
            with self.assertRaises(gs.GoldStandardError):
                gs.mark_stale('ValidityFixture', 'Synthetic before cutover', require_durable=True)

    # #275: an aborted durable marking rolls back what it already wrote. The
    # canonical spelling is marked first, so gs_11111111 (and setUp's session)
    # are written before gs_22222222, under the lowercase-first spelling, fails.
    def _two_spellings(self, earlier=None):
        first = session(); first.update(session_id='gs_11111111', collection='ValidityFixture', **(earlier or {}))
        second = session(); second.update(session_id='gs_22222222', collection='validityFixture')
        gs.store_session(first); gs.store_session(second)

    def _failing_writes(self, save_fails, marker_fails):
        real_save, real_marker = gs._save_session_sync, gs._write_pending_marker
        def save(snapshot):
            if save_fails(snapshot):
                raise gs.GoldStandardError('SESSION_WRITE_FAILED', 'Synthetic write fault', 503)
            return real_save(snapshot)
        def marker(session_id, fields):
            return False if session_id in marker_fails else real_marker(session_id, fields)
        return patch.object(gs, '_save_session_sync', side_effect=save), patch.object(gs, '_write_pending_marker', side_effect=marker)

    @staticmethod
    def _stale_fields(data):
        return {key: data.get(key) for key in ('stale', 'stale_reason', 'stale_at')}

    def test_aborted_marking_rolls_back_written_sessions_across_spellings(self):
        older = {'stale': True, 'stale_reason': 'Synthetic older reason', 'stale_at': '2026-01-01T00:00:00+00:00'}
        for earlier in ({}, older):
            with self.subTest(earlier=bool(earlier)):
                gs._sessions = {}; self.data = session(); gs.store_session(self.data)
                self._two_spellings(earlier)
                before = {sid: self._stale_fields(gs.get_session(sid)) for sid in ('gs_47abcdef', 'gs_11111111', 'gs_22222222')}
                save, marker = self._failing_writes(lambda s: s['session_id'] == 'gs_22222222', {'gs_22222222'})
                with save, marker, self.assertRaises(gs.GoldStandardError) as error:
                    gs.mark_stale('ValidityFixture', 'Synthetic replacement', require_durable=True)
                self.assertEqual(error.exception.code, 'SESSION_WRITE_FAILED')
                for reload in (False, True):
                    if reload:
                        gs._sessions = {}; gs.load_sessions_from_disk()
                    for sid, fields in before.items():
                        with self.subTest(session=sid, reload=reload):
                            self.assertEqual(self._stale_fields(gs.get_session(sid)), fields)

    def test_aborted_marking_restores_pending_markers(self):
        earlier_marker = json.dumps({'session_id': 'gs_11111111', 'orphaned': True,
                                     'orphaned_reason': 'Synthetic deletion', 'orphaned_at': '2026-01-01T00:00:00+00:00'}, indent=2)
        for existing in (None, earlier_marker):
            with self.subTest(existing=bool(existing)):
                gs._sessions = {}; self.data = session(); gs.store_session(self.data)
                self._two_spellings()
                path = gs._pending_path('gs_11111111')
                if existing:
                    path.parent.mkdir(exist_ok=True); path.write_text(existing)
                elif path.exists():
                    path.unlink()
                save, marker = self._failing_writes(lambda s: s['session_id'] in ('gs_11111111', 'gs_22222222'), {'gs_22222222'})
                with save, marker, self.assertRaises(gs.GoldStandardError):
                    gs.mark_stale('ValidityFixture', 'Synthetic replacement', require_durable=True)
                if existing:
                    self.assertEqual(path.read_text(), existing)
                else:
                    self.assertFalse(path.exists())
                for sid in ('gs_11111111', 'gs_22222222'):
                    self.assertFalse(gs.get_session(sid).get('stale', False), sid)
                message = gs._diagnostics[str(gs._session_key('gs_11111111'))]['message']
                self.assertIn('previous snapshot remains authoritative', message)
                self.assertNotIn('pending marker', message)

    def test_unrollbackable_marker_stays_and_is_reported(self):
        self._two_spellings()
        # gs_11111111's marking write succeeds; the write that restores it fails.
        fails = lambda s: s['session_id'] == 'gs_22222222' or (s['session_id'] == 'gs_11111111' and not s.get('stale'))
        save, marker = self._failing_writes(fails, {'gs_22222222'})
        with save, marker, self.assertRaises(gs.GoldStandardError) as error:
            gs.mark_stale('ValidityFixture', 'Synthetic replacement', require_durable=True)
        self.assertIn('historical marker could not be persisted', str(error.exception))
        self.assertEqual(gs._diagnostics[str(gs._session_key('gs_11111111'))]['message'], gs._ROLLBACK_FAILED_MESSAGE)
        gs._sessions = {}; gs.load_sessions_from_disk()
        current = gs.get_session('gs_11111111')
        self.assertTrue(current['stale']); self.assertEqual(current['stale_reason'], 'Synthetic replacement')

    def test_request_validation_errors_use_project_envelope_without_input(self):
        from fastapi.testclient import TestClient
        from main import app
        from auth import testing as auth_testing  # a real session in a temporary store (#25)
        client=TestClient(app, headers=auth_testing.headers())
        response=client.post('/goldstandard/save',json={'filename':'secret-input.json'})
        self.assertEqual(response.status_code,422,response.text)
        body=response.json()
        self.assertEqual(body['error']['code'],'INVALID_PARAMETER')
        self.assertTrue(body['error']['detail'])
        self.assertTrue(all('input' not in error and 'ctx' not in error for error in body['error']['detail']))
        self.assertNotIn('secret-input.json',response.text)


class ImplementationTests(unittest.TestCase):
    def test_changed_embedded_sources_match_runtime(self):
        root=repository_root()
        if root is None:self.skipTest(NEEDS_REPOSITORY)
        text=(root/'IMPLEMENTATION.md').read_text()
        for name in ('api/main.py','api/models/schemas.py','api/routers/goldstandard.py',
                     'api/services/goldstandard.py','api/services/tuning.py','ui/src/api/client.ts','ui/src/pages/GoldStandardPage.tsx','scripts/verify/session_validity.py','scripts/verify/10_validity.sh','scripts/verify/05_transfer.sh','scripts/verify/README.md','scripts/verify/browser/ui_criteria.js'):
            language='typescript' if name.endswith(('.ts','.tsx')) else 'javascript' if name.endswith('.js') else 'bash' if name.endswith('.sh') else 'markdown' if name.endswith('.md') else 'python'
            fence='````' if name.endswith('.md') else '```'
            header='### '+name+'\n\n'+fence+language+'\n'
            start=text.index(header)+len(header);end=text.index('\n'+fence+'\n',start)
            with self.subTest(file=name):self.assertEqual(text[start:end],(root/name).read_text().rstrip('\n'))


if __name__=='__main__': unittest.main()
