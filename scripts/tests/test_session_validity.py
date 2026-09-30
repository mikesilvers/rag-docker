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
sys.path.insert(0,os.environ.get('RAG_TEST_API_DIR',str(Path(__file__).resolve().parents[2]/'api')))
from config import settings
from models.schemas import SessionResponse
from services import goldstandard as gs


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
        expected=copy.deepcopy(self.data);gs._sessions={};gs.load_sessions_from_disk()
        self.assertEqual(gs.get_session(self.data['session_id']),expected)

    def test_historical_guard_rejects_before_export_writes(self):
        for flags in ({'stale':True},{'orphaned':True},{'stale':True,'orphaned':True}):
            self.data.pop('stale',None);self.data.pop('orphaned',None);self.data.update(flags)
            before=copy.deepcopy(self.data)
            with patch.object(gs,'_save_export_sync') as write:
                with self.assertRaises(gs.GoldStandardError) as error:
                    asyncio.run(gs.save_session(self.data['session_id'],'guard.json'))
                self.assertEqual((error.exception.code,error.exception.status),('HISTORICAL_SESSION',409))
                write.assert_not_called()
            self.assertEqual(before,self.data)

    def test_explicit_service_export_reports_metadata_without_mutating_history(self):
        self.data.update(stale=True,stale_reason='Synthetic stale',stale_at='2026-09-28T00:00:00+00:00')
        before=copy.deepcopy(self.data)
        result=asyncio.run(gs.save_session(self.data['session_id'],'historical.json',True))
        self.assertTrue(result['historical']);self.assertEqual((result['pairs_saved'],result['pairs_excluded']),(2,2))
        self.assertEqual(before,self.data)
        rows=json.loads((Path(self.temp.name)/'historical.json').read_text())
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
            client=MagicMock();stage=MagicMock();target=MagicMock()
            obj=SimpleNamespace(uuid='00000000-0000-0000-0000-000000000001',vector={'default':[0.1]},properties={'content':'Inert'})
            stage.iterator.return_value=[obj];stage.aggregate.over_all.return_value=SimpleNamespace(total_count=1)
            client.collections.get.side_effect=lambda name:target if name=='ValidityFixture' else stage
            batch=target.batch.dynamic.return_value.__enter__.return_value;batch.number_errors=0
            if failure=='write':batch.add_object.side_effect=RuntimeError('write fault')
            def delete(name):
                if name=='ValidityFixture':
                    self.assertTrue(self.data['stale'])
                    with self.assertRaises(gs.GoldStandardError):asyncio.run(gs.save_session(self.data['session_id'],'guard.json'))
                    if failure=='delete':raise RuntimeError('delete fault')
            client.collections.delete.side_effect=delete
            def create(name,*args,**kwargs):
                if name=='ValidityFixture' and failure=='create':raise RuntimeError('create fault')
            insert=RuntimeError('stage fault') if failure=='stage' else None
            with patch.object(tuning.wc,'get_client',return_value=client), \
                 patch.object(tuning.wc,'_collection_config_sync',return_value={'index_type':'hnsw','distance_metric':'cosine','hnsw_config':{}}), \
                 patch.object(tuning.wc,'_create_collection_sync',side_effect=create), \
                 patch.object(tuning.wc,'_insert_chunks_sync',side_effect=insert):
                with self.assertRaises(Exception):tuning._rebuild('ValidityFixture',[{'content':'Inert'}],None,None,None,before_replace=lambda:gs.mark_stale('ValidityFixture','Synthetic replacement'))
            self.assertEqual(bool(self.data.get('stale')),failure!='stage')
            if failure!='stage':self.assertTrue(self.data['stale_at'])

    def test_successful_identity_preserving_reindex_keeps_validity_semantics(self):
        from services import tuning
        job={'status':'queued'}
        with patch.dict(tuning._jobs,{'synthetic':job}), \
             patch.object(tuning.sources,'has_sources',return_value=False), \
             patch.object(tuning,'_existing_chunks',return_value=[{'content':'Inert'}]), \
             patch.object(tuning,'_rebuild',return_value=1) as rebuild, \
             patch.object(gs,'mark_stale') as mark:
            tuning._run('synthetic','ValidityFixture','reindex',{})
        self.assertIsNone(rebuild.call_args.kwargs['before_replace']);mark.assert_not_called()
        self.assertEqual(job['status'],'completed');self.assertTrue(any('unchanged' in note for note in job['notes']))


class ImplementationTests(unittest.TestCase):
    def test_changed_embedded_sources_match_runtime(self):
        root=Path(__file__).resolve().parents[2];text=(root/'IMPLEMENTATION.md').read_text()
        for name in ('api/main.py','api/models/schemas.py','api/routers/goldstandard.py',
                     'api/services/goldstandard.py','api/services/tuning.py','ui/src/api/client.ts','ui/src/pages/GoldStandardPage.tsx','scripts/verify/session_validity.py','scripts/verify/10_validity.sh','scripts/verify/05_transfer.sh','scripts/verify/README.md','scripts/verify/browser/ui_criteria.js'):
            language='typescript' if name.endswith(('.ts','.tsx')) else 'javascript' if name.endswith('.js') else 'bash' if name.endswith('.sh') else 'markdown' if name.endswith('.md') else 'python'
            fence='````' if name.endswith('.md') else '```'
            header='### '+name+'\n\n'+fence+language+'\n'
            start=text.index(header)+len(header);end=text.index('\n'+fence+'\n',start)
            with self.subTest(file=name):self.assertEqual(text[start:end],(root/name).read_text().rstrip('\n'))


if __name__=='__main__': unittest.main()
