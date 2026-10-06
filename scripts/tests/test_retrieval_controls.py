"""Observed physical-index reporting and executed query-method controls."""
import contextlib
import importlib.util
import io
import json
import os
import runpy
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,MagicMock,patch
api_dir=os.environ.get('RAG_TEST_API_DIR')
sys.path.insert(0,api_dir or str(Path(__file__).resolve().parents[2]/'api'))
from config import settings
from services import weaviate_client as wc,rag_pipeline as rag,retrieval_config as saved
from weaviate.classes.config import VectorDistances
NEEDS_REPOSITORY='needs the whole repository mounted (see scripts/verify/README.md)'

def repository_root():
    parents=Path(__file__).resolve().parents
    root=parents[2] if len(parents)>2 else None
    return root if root is not None and (root/'IMPLEMENTATION.md').is_file() else None

class ReportingTests(unittest.TestCase):
    def test_unrecognized_index_does_not_break_collection_listing(self):
        dynamic = type('ObservedDynamic', (), {'distance_metric': VectorDistances.COSINE})()
        collections = {}
        for name, config in [('NamedVectors', None), ('Dynamic', dynamic)]:
            coll = MagicMock()
            coll.config.get.return_value = SimpleNamespace(vector_index_config=config)
            coll.aggregate.over_all.return_value = SimpleNamespace(total_count=1)
            collections[name] = coll
        client = MagicMock()
        client.collections.list_all.return_value = collections
        client.collections.get.side_effect = collections.__getitem__
        with patch.object(wc, 'get_client', return_value=client):
            rows = wc._get_collections_sync()
        self.assertEqual([row['name'] for row in rows], ['NamedVectors', 'Dynamic'])
        self.assertEqual([row['index_type'] for row in rows], ['unknown', 'dynamic'])
        self.assertEqual([row['distance_metric'] for row in rows], ['unknown', 'cosine'])
        self.assertTrue(all(row['hnsw_config'] is None for row in rows))

    def test_backend_values_reach_list_without_default_substitution(self):
        hnsw=type('ObservedHNSW',(),{'ef':-1,'ef_construction':1000,'max_connections':256,'distance_metric':VectorDistances.DOT})()
        flat=type('ObservedFlat',(),{'distance_metric':VectorDistances.L2_SQUARED})()
        collections={}
        for name,config in [('SyntheticHnsw',hnsw),('SyntheticFlat',flat)]:
            coll=MagicMock()
            coll.config.get.return_value=SimpleNamespace(vector_index_config=config)
            coll.aggregate.over_all.return_value=SimpleNamespace(total_count=3)
            collections[name]=coll
        client=MagicMock()
        client.collections.list_all.return_value=collections
        client.collections.get.side_effect=collections.__getitem__
        with patch.object(wc,'get_client',return_value=client):
            rows=wc._get_collections_sync()
        self.assertEqual(rows[0]['hnsw_config'],{'ef':-1,'efConstruction':1000,'maxConnections':256})
        self.assertEqual(rows[0]['distance_metric'],'dot')
        self.assertEqual(rows[1]['index_type'],'flat')
        self.assertEqual(rows[1]['distance_metric'],'l2-squared')
        self.assertIsNone(rows[1]['hnsw_config'])


class BackendControlTests(unittest.TestCase):
    def test_hybrid_weight_and_semantic_top_k_reach_supported_sdk_calls(self):
        client=MagicMock()
        coll=MagicMock()
        client.collections.get.return_value=coll
        coll.query.hybrid.return_value=SimpleNamespace(objects=[])
        coll.query.near_text.return_value=SimpleNamespace(objects=[])
        with patch.object(wc,'get_client',return_value=client):
            self.assertEqual(wc._hybrid_query_sync('Synthetic','inert query',0.25,7),[])
            self.assertEqual(wc._near_text_query_sync('Synthetic','inert query',11),[])
        hybrid=coll.query.hybrid.call_args.kwargs
        self.assertEqual((hybrid['query'],hybrid['alpha'],hybrid['limit']),('inert query',0.25,7))
        semantic=coll.query.near_text.call_args.kwargs
        self.assertEqual((semantic['query'],semantic['limit']),('inert query',11))

class ExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_vector_aliases_share_existing_index_top_k_and_answer_style_are_executed(self):
        chunk={'content':'Inert','source_file':'inert.txt','chunk_index':0,'score':1}
        for mode in ('hnsw','flat'):
            with patch.object(rag.ollama,'chat',new=AsyncMock(side_effect=['Reformulated','Answer'])) as chat, \
                 patch.object(rag.ollama,'embed',new=AsyncMock(return_value=[0.1,0.2])), \
                 patch.object(wc,'near_vector_query',new=AsyncMock(return_value=[chunk])) as query:
                response=await rag.run_query('Inert','Synthetic',mode,17,0.25,True,'engineer')
                query.assert_awaited_once_with('Synthetic',[0.1,0.2],17)
                self.assertEqual(chat.await_args_list[-1].args[0],rag.SYNTHESIS_ENGINEER_SYSTEM)
                self.assertEqual(response['chunks_retrieved'],1)

    async def test_hybrid_alpha_top_k_and_semantic_method_forward_to_backend(self):
        with patch.object(rag.ollama,'chat',new=AsyncMock(return_value='Synthetic')), \
             patch.object(wc,'hybrid_query',new=AsyncMock(return_value=[])) as hybrid, \
             patch.object(wc,'near_text_query',new=AsyncMock(return_value=[])) as semantic:
            await rag.run_query('Inert','Synthetic','hybrid',19,0.4,False,'end_user')
            hybrid.assert_awaited_once_with('Synthetic','Synthetic',0.4,19)
            await rag.run_query('Inert','Synthetic','semantic',11,0.4,False,'end_user')
            semantic.assert_awaited_once_with('Synthetic','Synthetic',11)

    async def test_saved_settings_roundtrip_keeps_legacy_ef_but_ui_equivalent_clears_it(self):
        with tempfile.TemporaryDirectory() as directory,patch.object(settings,'upload_dir',directory),patch.object(saved,'_DIR',None):
            config={'collection':'Synthetic','retrieval_mode':'flat','top_k':17,'alpha':0.25,'ef':96,'response_format':'engineer'}
            saved.save(config)
            self.assertEqual(saved.resolve('Synthetic'),(config,False))
            config.update(retrieval_mode='hnsw',ef=None)
            saved.save(config)
            self.assertIsNone(saved.load('Synthetic')['ef'])

class TargetTests(unittest.TestCase):
    def test_in_container_target_cannot_silently_select_another_stack(self):
        if repository_root() is None:self.skipTest(NEEDS_REPOSITORY)
        spec=importlib.util.spec_from_file_location('compose_target',Path(__file__).resolve().parents[1]/'verify/compose_target.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        match=module.matches_local_proxy
        for api in ('http://localhost:18080/api','http://127.0.0.1:18080/api/'):
            self.assertTrue(match(api,'127.0.0.1:18080'))
        self.assertTrue(match('http://[::1]:18080/api','[::]:18080'))
        for api in ('http://remote.example:18080/api','http://127.0.0.1:8080/api','https://localhost:18080/api','http://localhost:18080/other','http://user@localhost:18080/api','http://localhost:18080/api?other=1'):
            self.assertFalse(match(api,'127.0.0.1:18080'))
        self.assertFalse(match('http://localhost:18080/api',''))

class VerifierCleanupTests(unittest.TestCase):
    """The live verifier's cleanup, run against a fake API and backend."""

    def setUp(self):
        if repository_root() is None:
            self.skipTest(NEEDS_REPOSITORY)

    def run_verifier(self, fail_query_on=None, fail_delete_on=None, raise_delete_on=None, fail_discard=False):
        created, deleted, saved_configs = {}, [], {}

        class Response:
            def __init__(self, status_code, body=None):
                self.status_code = status_code
                self.text = json.dumps(body)
                self._body = body

            def json(self):
                return self._body

        class FakeClient:
            def __init__(self, app):
                pass

            def post(self, path, json):
                if path == '/collections':
                    created[json['name']] = json['index_type']
                    return Response(201)
                if path == '/query':
                    if created[json['collection']] == fail_query_on:
                        raise RuntimeError('original verifier failure')
                    count = min(json['top_k'], 10)
                    return Response(200, {'chunks_retrieved': count, 'citations': [{}] * count})
                if not 1 <= json['top_k'] <= 50:
                    return Response(422)
                saved_configs[json['collection']] = dict(json)
                return Response(201)

            def get(self, path):
                if path == '/collections':
                    hnsw = {'ef': 72, 'efConstruction': 160, 'maxConnections': 32}
                    rows = [{'name': name, 'index_type': index, 'distance_metric': 'cosine', 'hnsw_config': hnsw if index == 'hnsw' else None} for name, index in created.items()]
                    return Response(200, {'collections': rows})
                return Response(200, saved_configs[path.rsplit('/', 1)[1]])

            def delete(self, path):
                name = path.split('/')[2].split('?')[0]
                deleted.append(name)
                if created[name] == raise_delete_on:
                    raise RuntimeError('synthetic delete exception')
                return Response(500, {'error': 'synthetic delete failure'}) if created[name] == fail_delete_on else Response(200)

        recovery = importlib.import_module('services.collection_recovery')
        script = Path(__file__).resolve().parents[1] / 'verify/retrieval_controls.py'
        stdout = io.StringIO()
        discard_effect = RuntimeError('synthetic discard failure') if fail_discard else None
        with patch('fastapi.testclient.TestClient', FakeClient), \
             patch.object(wc, '_collection_exists_sync', side_effect=lambda name: name in created and name not in deleted), \
             patch.object(wc, 'get_client', return_value=MagicMock()), \
             patch.object(wc, 'close_client') as close, \
             patch.object(recovery, 'begin', side_effect=lambda target, operation, client: {'staging': target + '__tuning_owned'}), \
             patch.object(recovery, 'discard', side_effect=discard_effect) as discard, \
             contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(io.StringIO()):
            try:
                runpy.run_path(str(script))
                error = None
            except BaseException as raised:
                error = raised
        return error, created, deleted, discard, close, stdout.getvalue()

    def test_cleanup_failure_does_not_replace_original_error(self):
        error, created, deleted, discard, close, stdout = self.run_verifier(fail_query_on='flat', fail_delete_on='hnsw')
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), 'original verifier failure')
        self.assertEqual(deleted, list(created))
        self.assertEqual(discard.call_count, 2)
        close.assert_called()
        hnsw_name = next(name for name, index in created.items() if index == 'hnsw')
        report = [line for line in stdout.splitlines() if line.startswith('FAIL owned collection cleanup: ')]
        self.assertEqual(len(report), 1, stdout)
        self.assertIn(hnsw_name, report[0])
        self.assertIn('500', report[0])

    def test_cleanup_failure_is_reported_after_every_deletion(self):
        error, created, deleted, discard, close, stdout = self.run_verifier(fail_delete_on='hnsw', raise_delete_on='flat')
        self.assertIsInstance(error, AssertionError)
        self.assertEqual(deleted, list(created))
        self.assertEqual(discard.call_count, 2)
        close.assert_called()
        report = [line for line in stdout.splitlines() if line.startswith('FAIL owned collection cleanup: ')]
        self.assertEqual(len(report), 1, stdout)
        for name in created:
            self.assertIn(name, str(error))
            self.assertIn(name, report[0])
        for reason in ('500', 'synthetic delete failure', 'synthetic delete exception'):
            self.assertIn(reason, str(error))
            self.assertIn(reason, report[0])

    def test_discard_failure_keeps_its_existing_behaviour(self):
        error, created, deleted, discard, close, stdout = self.run_verifier(fail_discard=True)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), 'synthetic discard failure')
        self.assertEqual(deleted, list(created))
        self.assertEqual(discard.call_count, 1)
        close.assert_not_called()
        self.assertNotIn('FAIL owned collection cleanup', stdout)

    def test_cleanup_failure_is_reported_when_discard_fails(self):
        error, created, deleted, discard, close, stdout = self.run_verifier(fail_delete_on='hnsw', fail_discard=True)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual(str(error), 'synthetic discard failure')
        self.assertEqual(deleted, list(created))
        self.assertEqual(discard.call_count, 1)
        close.assert_not_called()
        hnsw_name = next(name for name, index in created.items() if index == 'hnsw')
        report = [line for line in stdout.splitlines() if line.startswith('FAIL owned collection cleanup: ')]
        self.assertEqual(len(report), 1, stdout)
        self.assertIn(hnsw_name, report[0])
        self.assertIn('500', report[0])

    def test_clean_run_still_passes(self):
        error, created, deleted, discard, close, stdout = self.run_verifier()
        self.assertIsNone(error)
        self.assertEqual(deleted, list(created))
        self.assertEqual(len(created), 2)
        self.assertEqual(discard.call_count, 2)
        self.assertTrue(stdout.rstrip().endswith('PASS owned synthetic collections/config/files removed'), stdout)
        self.assertNotIn('FAIL', stdout)


class DocumentationTests(unittest.TestCase):
    def test_changed_embedded_sources_match_runtime(self):
        root=repository_root()
        if root is None:self.skipTest(NEEDS_REPOSITORY)
        text=(root/'IMPLEMENTATION.md').read_text()
        for name in ('api/models/schemas.py','api/routers/collections.py','api/services/weaviate_client.py','ui/src/api/client.ts','ui/src/pages/RetrievalPage.tsx','ui/src/pages/QAPage.tsx','scripts/verify/03_query.sh','scripts/verify/11_retrieval.sh','scripts/verify/retrieval_controls.py','scripts/verify/README.md','scripts/verify/compose_target.py','scripts/verify/browser/ui_criteria.js'):
            lang='typescript' if name.endswith(('.ts','.tsx')) else 'bash' if name.endswith('.sh') else 'markdown' if name.endswith('.md') else 'javascript' if name.endswith('.js') else 'python'
            fence='````' if name.endswith('.md') else '```'
            h='### '+name+'\n\n'+fence+lang+'\n'
            a=text.index(h)+len(h)
            b=text.index('\n'+fence+'\n',a)
            with self.subTest(file=name):
                self.assertEqual(text[a:b],(root/name).read_text().rstrip('\n'))

if __name__=='__main__':
    unittest.main()
