"""Real physical config and executed query limits on owned synthetic collections.

Inside disposable API: python - < scripts/verify/retrieval_controls.py
Actual SDK/backend storage and vector queries; only Ollama reformulation,
embedding and answer calls are controlled. No startup sweep/model calls.
"""
import importlib.util
import json
import os
import sys
import tempfile
import uuid
from unittest.mock import AsyncMock,patch
from fastapi.testclient import TestClient
from config import settings
from main import app
from services import weaviate_client as wc,rag_pipeline as rag

prefix=os.environ.get('RAG_TEST_PREFIX','Vfy')+'Controls'+uuid.uuid4().hex[:10]
collections=[]
owners=[]
persistent_upload=settings.upload_dir
if importlib.util.find_spec('services.collection_recovery'):
    from services import collection_recovery as recovery
else:
    recovery=None  # The pre-recovery baseline sweeps legacy staging markers.

client=TestClient(app)
with tempfile.TemporaryDirectory(prefix='retrieval-controls-') as directory,patch.object(settings,'upload_dir',directory):
    try:
        for index in ('hnsw','flat'):
            if recovery:
                with patch.object(settings,'upload_dir',persistent_upload):
                    owner=recovery.begin(prefix+index,'tune',wc.get_client())
                owners.append(owner)
                name=owner['staging']
            else:
                name=prefix+index+'__tuning_'+uuid.uuid4().hex
            assert not wc._collection_exists_sync(name)
            collections.append(name)
            response=client.post('/collections',json={'name':name,'index_type':index,'hnsw_config':{'ef':72,'efConstruction':160,'maxConnections':32}})
            assert response.status_code==201,response.text
            if os.environ.get('RAG_VERIFY_INTERRUPT_AFTER_CREATE')=='1':
                print('OWNED_INTERRUPTED_FIXTURE '+json.dumps({'name':name,'owner':owners[-1] if owners else None}),flush=True)
                os._exit(86)  # Acceptance injection: skips finally like a hard kill.
            coll=wc.get_client().collections.get(name)
            for i in range(1,11):
                coll.data.insert(uuid=uuid.UUID(int=i),properties={'content':f'Inert backend chunk{i}','source_file':'inert.txt','chunk_index':i},vector=[0.1]*768)
            row=next(row for row in client.get('/collections').json()['collections'] if row['name']==name)
            assert row['index_type']==index and row['distance_metric']=='cosine'
            assert row['hnsw_config']==({'ef':72,'efConstruction':160,'maxConnections':32} if index=='hnsw' else None),row
            print('PASS real '+index+' physical settings are observed, not query labels',flush=True)
            for mode,limit in (('hnsw',1),('flat',50)):
                with patch.object(rag.ollama,'chat',new=AsyncMock(return_value='Synthetic controlled answer')),patch.object(rag.ollama,'embed',new=AsyncMock(return_value=[0.1]*768)):
                    response=client.post('/query',json={'collection':name,'question':'Inert','retrieval_mode':mode,'top_k':limit,'include_citations':True,'response_format':'engineer'})
                assert response.status_code==200,response.text
                body=response.json()
                assert body['chunks_retrieved']==min(limit,10) and len(body['citations'])==min(limit,10),body
            after=next(row for row in client.get('/collections').json()['collections'] if row['name']==name)
            assert after['hnsw_config']==row['hnsw_config'] and after['index_type']==index
            print('PASS legacy query aliases execute different topK limits without changing '+index+' physical config',flush=True)
            config={'collection':name,'retrieval_mode':'hybrid','top_k':7,'alpha':0.25,'ef':96,'response_format':'engineer'}
            assert client.post('/retrieval/config',json=config).status_code==201
            loaded=client.get('/retrieval/config/'+name).json()
            assert all(loaded[key]==value for key,value in config.items()),loaded
            config['ef']=None
            assert client.post('/retrieval/config',json=config).status_code==201
            assert client.get('/retrieval/config/'+name).json()['ef'] is None
            for limit in (1,50):
                config['top_k']=limit
                saved=client.post('/retrieval/config',json=config)
                assert saved.status_code==201 and client.get('/retrieval/config/'+name).json()['top_k']==limit,saved.text
            for limit in (0,51):
                invalid=client.post('/retrieval/config',json={**config,'top_k':limit})
                assert invalid.status_code==422,invalid.text
            print('PASS saved method/topK boundaries/alpha/style roundtrip and inactive ef clears on '+index,flush=True)
    finally:
        # Every owned collection gets a deletion attempt; failures are reported
        # together afterwards so they never hide a failure from the checks.
        cleanup_failures=[]
        for name in collections:
            try:
                if wc._collection_exists_sync(name):
                    response=client.delete('/collections/'+name+'?confirm=true')
                    if response.status_code!=200:
                        cleanup_failures.append(f'{name}: {response.status_code} {response.text}')
            except Exception as error:
                cleanup_failures.append(f'{name}: {error!r}')
        if cleanup_failures:
            print('FAIL owned collection cleanup: '+'; '.join(cleanup_failures),flush=True)
        if recovery:
            with patch.object(settings,'upload_dir',persistent_upload):
                for owner in owners:
                    recovery.discard(owner,wc.get_client())
        wc.close_client()
        if cleanup_failures:
            if sys.exc_info()[0] is None:  # Otherwise the original failure stays the raised error.
                raise AssertionError('owned collection cleanup failed: '+'; '.join(cleanup_failures))
assert all(not wc._collection_exists_sync(name) for name in collections)
wc.close_client()
print('PASS owned synthetic collections/config/files removed',flush=True)
