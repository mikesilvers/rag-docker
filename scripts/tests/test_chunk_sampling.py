"""Synthetic sampling invariants and validation before persistence/model work."""
import asyncio
import json
import ast
import runpy
import os
import random
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call, patch
from uuid import UUID

sys.path.insert(0, os.environ.get('RAG_TEST_API_DIR') or str(Path(__file__).resolve().parents[2]/'api'))
from fastapi.testclient import TestClient
from main import app
from auth import testing as auth_testing  # a real session in a temporary store (#25)
from models.schemas import GenerateRequest
from services import chunk_sampling as sample, goldstandard as gs, weaviate_client as wc

NEEDS_REPOSITORY = 'needs the whole repository mounted (see scripts/verify/README.md)'


def repository_root():
    parents = Path(__file__).resolve().parents
    root = parents[2] if len(parents) > 2 else None
    return root if root is not None and (root / 'IMPLEMENTATION.md').is_file() else None


def objects(count=20):
    return [SimpleNamespace(uuid=UUID(int=i), properties={
        'content':f'synthetic {i}', 'source_file':'inert.txt', 'chunk_index':i,
    }) for i in range(1,count+1)]


def ids(rows):
    return [UUID(row).int if isinstance(row,str) else UUID(row['object_id']).int for row in rows]


class SelectionTests(unittest.TestCase):
    def test_known_seed_selects_from_entire_population_in_stable_order(self):
        self.assertEqual(ids(sample.select_chunk_ids(objects(),3,7)),[7,10,12])

    def test_repeat_reverse_and_shuffled_backend_order_are_equivalent(self):
        candidates=objects(100)
        expected=sample.select_chunk_ids(candidates,20,-9)
        shuffled=candidates.copy(); random.Random(5).shuffle(shuffled)
        for order in (candidates,list(reversed(candidates)),shuffled):
            self.assertEqual(sample.select_chunk_ids(iter(order),20,-9),expected)

    def test_empty_small_and_oversize_requests_report_available_unique_objects(self):
        self.assertEqual(sample.select_chunk_ids([],20,1),[])
        self.assertEqual(set(ids(sample.select_chunk_ids(objects(3),100,1))),{1,2,3})
        repeated=objects(5)*3
        self.assertEqual(len(sample.select_chunk_ids(repeated,100,1)),5)
        self.assertEqual(len(sample.select_chunk_ids(repeated,3,1)),3)

    def test_different_seeds_need_not_select_different_subsets(self):
        self.assertEqual(set(ids(sample.select_chunk_ids(objects(3),3,1))),
                         set(ids(sample.select_chunk_ids(objects(3),3,2))))
        self.assertNotEqual(ids(sample.select_chunk_ids(objects(20),3,1)),
                            ids(sample.select_chunk_ids(objects(20),3,2)))

    def test_repeated_null_seed_calls_each_draw_their_own_nonce(self):
        with patch.object(sample.secrets, 'token_bytes', side_effect=[b'a' * 32, b'b' * 32]) as entropy:
            first = sample.select_chunk_ids(objects(), 3, None)
            second = sample.select_chunk_ids(objects(), 3, None)
        self.assertEqual(entropy.call_args_list, [call(32)] * 2)
        self.assertNotEqual(first, second)

    def test_null_seed_draws_one_nonce_seeded_call_uses_no_entropy(self):
        with patch.object(sample.secrets,'token_bytes',return_value=b'a'*32) as entropy:
            self.assertEqual(ids(sample.select_chunk_ids(objects(),3,None)),[17,4,9])
            entropy.assert_called_once_with(32)
        with patch.object(sample.secrets,'token_bytes',side_effect=AssertionError('unexpected entropy')):
            sample.select_chunk_ids(objects(),3,0)

    def test_calls_do_not_change_global_pseudorandom_state(self):
        before=random.getstate()
        sample.select_chunk_ids(objects(),3,7)
        sample.select_chunk_ids(objects(),3,None)
        self.assertEqual(random.getstate(),before)

    def test_hash_collision_uses_uuid_tie_breaker_without_comparing_payloads(self):
        class Collision:
            def copy(self): return self
            def update(self,value): pass
            def digest(self): return b'\0'*32
        with patch.object(sample.hashlib,'sha256',return_value=Collision()):
            self.assertEqual(ids(sample.select_chunk_ids(reversed(objects()),3,7)),[1,2,3])

    def test_scans_full_population_with_at_most_requested_candidates(self):
        heap_sizes=[]; visited=[]
        real_push=sample.heapq.heappush; real_replace=sample.heapq.heapreplace
        def push(heap,item):
            real_push(heap,item); heap_sizes.append(len(heap))
        def replace(heap,item):
            result=real_replace(heap,item); heap_sizes.append(len(heap)); return result
        def stream():
            for candidate in objects(10000):
                visited.append(candidate.uuid); yield candidate
        with patch.object(sample.heapq,'heappush',side_effect=push), patch.object(sample.heapq,'heapreplace',side_effect=replace):
            rows=sample.select_chunk_ids(stream(),5,7)
        self.assertEqual(len(visited),10000)
        self.assertEqual(len(rows),5)
        self.assertLessEqual(max(heap_sizes),5)
        self.assertTrue(any(UUID(row).int>100 for row in rows))

    def test_invalid_limits_seeds_and_object_identities_fail(self):
        for limit in (0,-1,101,True,1.5,'3',None):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                sample.select_chunk_ids(objects(),limit,7)
        for seed in (True,1.5,'3'):
            with self.subTest(seed=seed), self.assertRaises(ValueError):
                sample.select_chunk_ids(objects(),3,seed)
        with self.assertRaises(ValueError):
            sample.select_chunk_ids([SimpleNamespace(uuid='invalid',properties={})],3,7)


class AdapterTests(unittest.TestCase):
    def test_sdk_iterator_fetches_only_required_properties_without_vectors(self):
        collection=MagicMock(); collection.iterator.return_value=iter([SimpleNamespace(uuid=o.uuid) for o in objects()])
        collection.query.fetch_objects.return_value=SimpleNamespace(objects=[objects()[i-1] for i in [12,7,10]])
        client=MagicMock(); client.collections.get.return_value=collection
        with patch.object(wc,'get_client',return_value=client):
            self.assertEqual(ids(wc._sample_chunks_sync('Inert',3,7)),[7,10,12])
        collection.iterator.assert_called_once_with(include_vector=False,
            return_properties=[],cache_size=100)
        kwargs=collection.query.fetch_objects.call_args.kwargs
        self.assertEqual(kwargs['limit'],3)
        self.assertFalse(kwargs['include_vector'])
        self.assertEqual(kwargs['return_properties'],['content','source_file','chunk_index'])
        self.assertEqual(set(kwargs['filters'].value),set(sample.select_chunk_ids(objects(),3,7)))

    def test_empty_population_does_not_fetch_payloads_and_deleted_winners_are_omitted(self):
        collection=MagicMock();client=MagicMock();client.collections.get.return_value=collection
        collection.iterator.return_value=iter([])
        with patch.object(wc,'get_client',return_value=client):
            self.assertEqual(wc._sample_chunks_sync('Inert',3,7),[])
        collection.query.fetch_objects.assert_not_called()
        collection.iterator.return_value=iter(objects())
        collection.query.fetch_objects.return_value=SimpleNamespace(objects=[objects()[11],objects()[6]])
        with patch.object(wc,'get_client',return_value=client):
            self.assertEqual(ids(wc._sample_chunks_sync('Inert',3,7)),[7,12])
        self.assertEqual(collection.query.fetch_objects.call_args.kwargs['limit'],3)

    def test_uuid_ranking_never_reads_unselected_payloads(self):
        class IdentityOnly:
            def __init__(self,identity): self.uuid=identity
            @property
            def properties(self): raise AssertionError('ranking requested payload')
        self.assertEqual(ids(sample.select_chunk_ids([IdentityOnly(o.uuid) for o in objects()],3,7)),[7,10,12])

    def test_invalid_settings_are_rejected_before_backend_client(self):
        with patch.object(wc,'get_client',side_effect=AssertionError('backend contacted')):
            for limit,seed in ((0,1),(101,1),(True,1),(3,False),(3,float('nan'))):
                with self.subTest(settings=(limit,seed)), self.assertRaises(ValueError):
                    wc._sample_chunks_sync('Inert',limit,seed)


class GenerationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.original_sessions=gs._sessions
        gs._sessions={}

    async def asyncTearDown(self):
        gs._sessions=self.original_sessions

    async def test_seed_reaches_sampler_actual_size_drives_generation(self):
        chosen=[{'object_id':identity,'content':'Synthetic','source_file':'inert.txt','chunk_index':0} for identity in sample.select_chunk_ids(objects(3),20,7)]
        with patch.object(gs.wc,'_sample_chunks_sync',new=MagicMock(return_value=chosen)) as sampler, \
             patch.object(gs,'_store_generated_session',side_effect=lambda session:{**session,'session_id':'gs_460abcdf'}) as save, \
             patch.object(gs,'_run_generation',new=AsyncMock()) as generate:
            result=await gs.start_generation('Inert',20,7)
            await asyncio.sleep(0)
            sampler.assert_called_once_with('Inert',limit=20,seed=7)
            self.assertEqual(result['pairs_total'],3)
            self.assertEqual(save.call_args.args[0]['pairs_total'],3)
            save.assert_called_once()
            generate.assert_awaited_once_with(result['session_id'],chosen)

    async def test_sample_and_publication_serialize_with_replacement_and_deletion(self):
        import tempfile
        import threading
        from config import settings
        from services.collection_writes import guard
        for marker in (gs.mark_stale, gs.mark_orphaned):
            with self.subTest(marker=marker.__name__), tempfile.TemporaryDirectory() as tmp:
                sampled, release, attempted, entered = (threading.Event() for _ in range(4))
                def sample_old(*args, **kwargs):
                    sampled.set()
                    if not release.wait(5): raise TimeoutError('test publication release')
                    return [{'content': 'old corpus', 'source_file': 'old.txt', 'chunk_index': 0}]
                def mutate():
                    attempted.set()
                    with guard('inert'):
                        entered.set()
                        return marker('Inert', 'Owned corpus mutation')
                with patch.object(settings, 'upload_dir', tmp), patch.object(gs, '_sessions', {}), patch.object(gs.wc, '_sample_chunks_sync', side_effect=sample_old), patch.object(gs, '_run_generation', new=AsyncMock()):
                    generation = asyncio.create_task(gs.start_generation('Inert', 1, 7))
                    try:
                        self.assertTrue(await asyncio.to_thread(sampled.wait, 5))
                        mutation = asyncio.create_task(asyncio.to_thread(mutate))
                        self.assertTrue(await asyncio.to_thread(attempted.wait, 5))
                        self.assertFalse(await asyncio.to_thread(entered.wait, .1))
                        # The collection guard does not block unrelated corpora.
                        def independent():
                            with guard('Other'): return True
                        self.assertTrue(await asyncio.wait_for(asyncio.to_thread(independent), 2))
                    finally:
                        release.set()
                    result = await generation
                    self.assertEqual(await mutation, 1)
                    await asyncio.gather(*list(gs._tasks))
                    current = gs.get_session(result['session_id'])
                    self.assertTrue(current['stale' if marker == gs.mark_stale else 'orphaned'])

    async def test_sampling_waits_for_an_in_progress_alias_mutation(self):
        # Reviewer test (#194): a generation started while a writer holds the
        # collection under its accepted alias must not sample until the writer ends.
        import threading
        from services.collection_writes import guard
        chunk = {'content': 'new corpus', 'source_file': 'new.txt', 'chunk_index': 0}
        holding, release = threading.Event(), threading.Event()
        def writer():
            with guard('inert'):
                holding.set()
                if not release.wait(5): raise TimeoutError('test writer release')
        sampler = MagicMock(return_value=[chunk])
        with patch.object(gs.wc, '_sample_chunks_sync', new=sampler), \
             patch.object(gs, '_store_generated_session', side_effect=lambda s: {**s, 'session_id': 'gs_460abcd1'}), \
             patch.object(gs, '_run_generation', new=AsyncMock()):
            mutation = asyncio.create_task(asyncio.to_thread(writer))
            try:
                self.assertTrue(await asyncio.to_thread(holding.wait, 5))
                generation = asyncio.create_task(gs.start_generation('Inert', 1, 7))
                await asyncio.sleep(.2)
                sampler.assert_not_called()
                self.assertFalse(generation.done(), 'generation published while a writer held the alias')
            finally:
                release.set()
            await mutation
            result = await asyncio.wait_for(generation, 5)
            sampler.assert_called_once_with('Inert', limit=1, seed=7)
            await asyncio.gather(*list(gs._tasks))
            self.assertEqual(result['session_id'], 'gs_460abcd1')

    async def test_busy_generation_returns_409_without_publishing_and_cleans_waiter(self):
        import threading
        from services import collection_writes as writes
        from routers import goldstandard as route
        from models.schemas import GenerateRequest
        held, release = threading.Event(), threading.Event()
        def writer():
            with writes.guard('inert'):
                held.set()
                if not release.wait(5): raise TimeoutError('writer release')
        task = asyncio.create_task(asyncio.to_thread(writer))
        try:
            self.assertTrue(await asyncio.to_thread(held.wait, 5))
            with patch.object(gs, '_GENERATION_GUARD_TIMEOUT_SECONDS', .02), \
                 patch.object(wc, 'collection_exists', new=AsyncMock(return_value=True)), \
                 patch.object(wc, '_sample_chunks_sync') as sample_mock, \
                 patch.object(gs, '_store_generated_session') as publish:
                response = await asyncio.wait_for(route.generate(GenerateRequest(collection='Inert', sample_size=1)), 2)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(json.loads(response.body)['error']['code'], 'COLLECTION_BUSY')
                sample_mock.assert_not_called()
                publish.assert_not_called()
                self.assertEqual(writes._registry['Inert'][1], 1)
        finally:
            release.set()
            await task
        self.assertNotIn('Inert', writes._registry)

    async def test_real_delete_before_sampling_returns_typed_404(self):
        import threading
        from routers import goldstandard as route
        from models.schemas import GenerateRequest
        deleting, release = threading.Event(), threading.Event()
        client = MagicMock()
        client.collections.get.return_value.config.get.return_value.name = 'Inert'
        client.collections.exists.return_value = True
        def remove(name):
            deleting.set()
            if not release.wait(5): raise TimeoutError('delete release')
            client.collections.exists.return_value = False
        client.collections.delete.side_effect = remove
        with patch.object(wc, 'get_client', return_value=client), \
             patch.object(wc, 'collection_exists', new=AsyncMock(return_value=True)), \
             patch.object(wc.collection_recovery, 'retire_deleted'), \
             patch.object(wc.sources, 'delete'), \
             patch.object(wc.retrieval_config, 'delete'), \
             patch.object(wc.ingest_config, 'delete'), \
             patch.object(gs, 'mark_orphaned'), \
             patch.object(gs, '_store_generated_session') as publish:
            deletion = asyncio.create_task(wc.delete_collection('inert'))
            try:
                self.assertTrue(await asyncio.to_thread(deleting.wait, 5))
                generation = asyncio.create_task(route.generate(GenerateRequest(collection='Inert', sample_size=1)))
                await asyncio.sleep(.05)
                self.assertFalse(generation.done())
            finally:
                release.set()
                await deletion
            response = await asyncio.wait_for(generation, 2)
            self.assertEqual(response.status_code, 404)
            self.assertEqual(json.loads(response.body)['error']['code'], 'COLLECTION_NOT_FOUND')
            publish.assert_not_called()
            client.collections.get.return_value.iterator.assert_not_called()

    @staticmethod
    def _guard_free(name, timeout=2):
        # A fresh thread, so a guard leaked by a reused worker thread (RLock) can't hide.
        import threading
        from services.collection_writes import guard
        acquired = threading.Event()
        def take():
            with guard(name): acquired.set()
        worker = threading.Thread(target=take, daemon=True)
        worker.start(); worker.join(timeout)
        return acquired.is_set()

    async def test_guard_is_released_before_model_generation_runs(self):
        from services import collection_writes as cw
        chunk = {'content': 'Synthetic', 'source_file': 'inert.txt', 'chunk_index': 0}
        started, finish = asyncio.Event(), asyncio.Event()
        async def generating(session_id, chunks):
            started.set()
            await finish.wait()
        with patch.object(gs.wc, '_sample_chunks_sync', new=MagicMock(return_value=[chunk])), \
             patch.object(gs, '_store_generated_session', side_effect=lambda s: {**s, 'session_id': 'gs_460abcd0'}), \
             patch.object(gs, '_run_generation', side_effect=generating):
            result = await gs.start_generation('Inert', 1, 7)
            try:
                await asyncio.wait_for(started.wait(), 5)
                self.assertNotIn(cw.canonical('Inert'), cw._registry, 'guard still registered during generation')
                for name in ('Inert', 'inert'):
                    self.assertTrue(await asyncio.to_thread(self._guard_free, name),
                                    f'mutation of {name} blocked while model generation runs')
            finally:
                finish.set()
                await asyncio.gather(*list(gs._tasks))
            self.assertEqual(result['session_id'], 'gs_460abcd0')

    async def test_sampling_or_publication_failure_releases_guard_and_starts_no_task(self):
        from services import collection_writes as cw
        chunk = {'content': 'Synthetic', 'source_file': 'inert.txt', 'chunk_index': 0}
        failures = {
            'sampling': dict(sample=MagicMock(side_effect=RuntimeError('Owned sampling failure')),
                             store=MagicMock(), raises=RuntimeError),
            'publication': dict(sample=MagicMock(return_value=[chunk]),
                                store=MagicMock(side_effect=gs.GoldStandardError('SESSION_WRITE_FAILED', 'Owned publication failure', 503)),
                                raises=gs.GoldStandardError),
        }
        for stage, case in failures.items():
            with self.subTest(stage=stage), \
                 patch.object(gs.wc, '_sample_chunks_sync', new=case['sample']), \
                 patch.object(gs, '_store_generated_session', new=case['store']), \
                 patch.object(gs, '_run_generation', new=AsyncMock()) as generate, \
                 patch.object(gs, '_tasks', set()):
                with self.assertRaises(case['raises']):
                    await gs.start_generation('Inert', 1, 7)
                generate.assert_not_called()
                self.assertEqual(gs._tasks, set())
                self.assertNotIn(cw.canonical('Inert'), cw._registry, f'guard left registered after {stage} failure')
                self.assertTrue(await asyncio.to_thread(self._guard_free, 'inert'), f'guard held after {stage} failure')

    async def test_invalid_settings_and_selection_failure_prevent_session_and_model_work(self):
        with patch.object(gs.wc,'_sample_chunks_sync',new=MagicMock(side_effect=ValueError('bad identity'))) as sampler, \
             patch.object(gs,'_store_generated_session') as save, \
             patch.object(gs,'_run_generation',new=AsyncMock()) as generate:
            for limit,seed in ((0,7),(101,7),(3,True),(3,float('inf'))):
                with self.subTest(settings=(limit,seed)), self.assertRaises(ValueError):
                    await gs.start_generation('Inert',limit,seed)
            sampler.assert_not_called()
            with self.assertRaises(ValueError):
                await gs.start_generation('Inert',3,7)
            self.assertEqual(gs._sessions,{})
            save.assert_not_called(); generate.assert_not_called()


class RequestTests(unittest.TestCase):
    def test_valid_defaults_bounds_and_existing_numeric_coercion(self):
        self.assertEqual(GenerateRequest(collection='Inert').sample_size,20)
        self.assertIsNone(GenerateRequest(collection='Inert').seed)
        for value in (1,100,'3',3.0):
            self.assertEqual(GenerateRequest(collection='Inert',sample_size=value,seed='-7').seed,-7)

    def test_invalid_requests_return_422_before_collection_lookup_or_generation(self):
        client=TestClient(app, headers=auth_testing.headers())  # No context manager: do not run startup sweeps.
        with patch.object(wc,'collection_exists',new=AsyncMock(side_effect=AssertionError('backend contacted'))) as lookup, \
             patch.object(gs,'start_generation',new=AsyncMock()) as generation:
            for update in ({'sample_size':0},{'sample_size':101},{'sample_size':True},
                           {'sample_size':1.5},{'seed':False},{'seed':1.5}):
                response=client.post('/goldstandard/generate',json={'collection':'Inert',**update})
                self.assertEqual(response.status_code,422,response.text)
            for field in ('sample_size','seed'):
                for number in ('NaN','Infinity','-Infinity'):
                    response=client.post('/goldstandard/generate',content=
                        '{"collection":"Inert","'+field+'":'+number+'}',
                        headers={'Content-Type':'application/json'})
                    self.assertEqual(response.status_code,422,response.text)
                    self.assertEqual(response.json()['error']['code'],'INVALID_PARAMETER')
                    self.assertTrue(response.json()['error']['detail'])
            lookup.assert_not_awaited(); generation.assert_not_awaited()


class VerificationBoundaryTests(unittest.TestCase):
    def test_failed_backend_creation_still_cleans_owned_custom_prefix(self):
        root=repository_root();exists=False;names=[]
        if root is None:self.skipTest(NEEDS_REPOSITORY)
        def create(name,*args):
            nonlocal exists
            names.append(name);exists=True
            raise RuntimeError('response lost after backend creation')
        def delete(name):
            nonlocal exists
            self.assertEqual(name,names[0]);exists=False
        with patch.dict(os.environ,{'RAG_TEST_PREFIX':'OwnedSamplingTest'}), \
             patch.object(wc,'_collection_exists_sync',side_effect=lambda name: exists), \
             patch.object(wc,'_create_collection_sync',side_effect=create), \
             patch.object(wc,'_delete_collection_sync',side_effect=delete) as removal, \
             patch.object(wc,'close_client'):
            with self.assertRaisesRegex(RuntimeError,'response lost'):
                runpy.run_path(str(root/'scripts/verify/chunk_sampling.py'),run_name='__main__')
            removal.assert_called_once_with(names[0])
            self.assertTrue(names[0].startswith('OwnedSamplingTestSampling'))
            self.assertFalse(exists)

    def test_parked_mcp_function_validates_before_api_call(self):
        root=repository_root()
        if root is None:self.skipTest(NEEDS_REPOSITORY)
        source=ast.parse((root/'mcp/tools/goldstandard.py').read_text())
        function=next(node for node in source.body if isinstance(node,ast.AsyncFunctionDef) and node.name=='rag_generate_goldstandard')
        function.decorator_list=[]
        post=AsyncMock(return_value={'session_id':'synthetic'})
        namespace={'ToolError':ValueError,'ragclient':SimpleNamespace(post=post)}
        exec(compile(ast.Module(body=[function],type_ignores=[]),'<actual parked MCP tool function>','exec'),namespace)
        for limit,seed in ((101,None),(200,None),(True,None),(1.5,None),(1,True),(1,1.5)):
            with self.assertRaises(ValueError):asyncio.run(namespace[function.name]('Inert',limit,seed))
        post.assert_not_awaited()
        self.assertEqual(asyncio.run(namespace[function.name]('Inert',100,-7)),{'session_id':'synthetic'})
        post.assert_awaited_once_with('/goldstandard/generate',json={'collection':'Inert','sample_size':100,'seed':-7})
        self.assertIn('integer 1–100',(root/'MCP_SPECIFICATIONS.md').read_text())


class ImplementationTests(unittest.TestCase):
    def test_changed_embedded_sources_match_runtime(self):
        root=repository_root()
        if root is None:self.skipTest(NEEDS_REPOSITORY)
        text=(root/'IMPLEMENTATION.md').read_text()
        names=('api/main.py','api/models/schemas.py','api/services/weaviate_client.py',
               'api/services/goldstandard.py','api/services/chunk_sampling.py',
               'scripts/verify/chunk_sampling.py','scripts/verify/README.md',
               'scripts/verify/04_goldstandard.sh','scripts/verify/09_sampling.sh')
        for name in names:
            fence='````' if name.endswith('README.md') else '```'
            language='markdown' if name.endswith('README.md') else 'bash' if name.endswith('.sh') else 'python'
            header='### '+name+'\n\n'+fence+language+'\n'
            start=text.index(header)+len(header); end=text.index('\n'+fence+'\n',start)
            with self.subTest(file=name):
                self.assertEqual(text[start:end],(root/name).read_text().rstrip('\n'))


if __name__=='__main__': unittest.main()
