"""Real backend/session marking and in-process HTTP validity/export acceptance.

Run inside a disposable API: python - < scripts/verify/session_validity.py
Uses a unique real collection, synthetic pairs and temporary local files. No
startup sweep or model calls. Only its own collection/session/files are removed.
"""
import copy
import os
import tempfile
import uuid
from unittest.mock import patch
from fastapi.testclient import TestClient
from config import settings
from main import app
from auth import testing as auth_testing  # a real session in a temporary store (#25)
from services import goldstandard as gs, weaviate_client as wc, tuning

collection=os.environ.get('RAG_TEST_PREFIX','Vfy')+'Validity'+uuid.uuid4().hex[:12]
session_id='gs_'+uuid.uuid4().hex[:8]
client=TestClient(app, headers=auth_testing.headers())  # Do not run global startup sweeps alongside other tests.
creation_attempted=False
with tempfile.TemporaryDirectory(prefix='validity-live-') as directory, patch.object(settings,'upload_dir',directory):
    try:
        creation_attempted=True
        response=client.post('/collections',json={'name':collection})
        assert response.status_code==201,response.text
        print('PASS unique real backend collection created',flush=True)
        pairs=[{'pair_id':'validity_pair','question':'Inert question','answer':'Inert answer',
            'contexts':['Inert retained context'],'ground_truth':'Inert truth','source_file':'inert.txt',
            'chunk_index':0,'status':'approved'}]
        session={'session_id':session_id,'collection':collection,'status':'completed',
            'pairs_total':1,'pairs_completed':1,'pairs':pairs}
        gs.store_session(session)
        response=client.get('/goldstandard/session/'+session_id)
        assert response.status_code==200,response.text
        assert not response.json()['stale'] and not response.json()['orphaned']
        assert response.json()['pairs']==pairs
        print('PASS legacy defaults and retained pairs reach HTTP response',flush=True)
        for option in ({},{'allow_historical':False},{'allow_historical':True}):
            response=client.post('/goldstandard/save',json={'session_id':session_id,'filename':'current.json',**option})
            assert response.status_code==200 and not response.json()['historical'],response.text
        print('PASS current/legacy export keeps default and explicit choices',flush=True)
        for value in (1,0,'true','false',None,[],{}):
            response=client.post('/goldstandard/save',json={'session_id':session_id,'allow_historical':value})
            assert response.status_code==422,response.text
        for value in ('NaN','Infinity','-Infinity'):
            response=client.post('/goldstandard/save',content='{"session_id":"'+session_id+'","allow_historical":'+value+'}',headers={'Content-Type':'application/json'})
            assert response.status_code==422 and response.json()['error']['code']=='INVALID_PARAMETER',response.text
        print('PASS live HTTP choices reject bool coercion and nonfinite inputs',flush=True)
        assert client.get('/goldstandard/session/gs_00000000').status_code==404
        assert client.post('/goldstandard/save',json={'session_id':'gs_00000000','allow_historical':True}).status_code==404
        print('PASS unknown lookup/export remain 404',flush=True)
        assert gs.mark_stale(collection,'Synthetic chunk-identity change')==1
        response=client.get('/goldstandard/session/'+session_id)
        assert response.json()['stale'] and response.json()['stale_reason']=='Synthetic chunk-identity change'
        assert response.json()['stale_at']
        print('PASS real persistence marker reason/timestamp reach HTTP response',flush=True)
        response=client.post('/goldstandard/save',json={'session_id':session_id})
        assert response.status_code==409 and response.json()['error']['code']=='HISTORICAL_SESSION',response.text
        print('PASS stale export is refused without explicit choice',flush=True)
        empty_id='gs_'+uuid.uuid4().hex[:8]
        empty={**session,'session_id':empty_id,'pairs':[],'pairs_total':0,'pairs_completed':0,
               'stale':True,'stale_reason':'Synthetic empty historical fixture','stale_at':gs.get_session(session_id)['stale_at']}
        gs.store_session(empty)
        response=client.get('/goldstandard/session/'+empty_id)
        assert response.status_code==200 and response.json()['stale'] and response.json()['pairs']==[],response.text
        print('PASS empty retained history carries warning metadata through HTTP',flush=True)
        response=client.delete('/collections/'+collection+'?confirm=true')
        assert response.status_code==200,response.text
        creation_attempted=False
        response=client.get('/goldstandard/session/'+session_id)
        historical=response.json()
        assert response.status_code==200 and historical['orphaned'] and historical['orphaned_reason'] and historical['orphaned_at']
        assert historical['stale'] and historical['pairs']==pairs
        refused=client.post('/goldstandard/save',json={'session_id':session_id,'allow_historical':False})
        assert refused.status_code==409 and refused.json()['error']['code']=='HISTORICAL_SESSION',refused.text
        print('PASS actual collection deletion forwards orphan warning without deleting history',flush=True)
        before=copy.deepcopy(gs.get_session(session_id))
        response=client.post('/goldstandard/save',json={'session_id':session_id,'allow_historical':True,'filename':'historical.json'})
        assert response.status_code==200,response.text
        exported=response.json()
        assert exported['historical'] and exported['session_validity']['stale'] and exported['session_validity']['orphaned']
        download=client.get('/goldstandard/download/'+exported['filename'])
        assert download.status_code==200,download.text
        rows=download.json()
        assert len(rows)==1 and set(rows[0])=={'question','answer','contexts','ground_truth'}
        assert gs.get_session(session_id)==before
        print('PASS explicit historical export retains RAGAS fields and original history',flush=True)
        # Exercise the real destructive boundary with supplied vectors and an
        # injected final-create fault. No model contact or global fixture sweep.
        response=client.post('/collections',json={'name':collection})
        assert response.status_code==201,response.text
        creation_attempted=True
        fault_id='gs_'+uuid.uuid4().hex[:8]
        gs.store_session({'session_id':fault_id,'collection':collection,'status':'completed','pairs_total':1,'pairs_completed':1,'pairs':copy.deepcopy(pairs)})
        assert not client.get('/goldstandard/session/'+fault_id).json()['stale']
        real_create=wc._create_collection_sync
        def fail_final_create(name,*args,**kwargs):
            if name==collection:
                assert not wc._collection_exists_sync(collection)
                raise RuntimeError('Owned synthetic final-create fault')
            return real_create(name,*args,**kwargs)
        def insert_supplied(name,properties):
            target=wc.get_client().collections.get(name)
            for properties_row in properties:
                target.data.insert(properties=properties_row,vector=[0.125]*768)
        with patch.object(wc,'_create_collection_sync',side_effect=fail_final_create), patch.object(wc,'_insert_chunks_sync',side_effect=insert_supplied):
            try:
                tuning._rebuild(collection,[{'content':'Owned inert chunk','source_file':'inert.txt','chunk_index':0}],None,None,None)
                raise AssertionError('Expected final-create fault')
            except Exception as error:  # Recovery-enabled rebuilds wrap this fault.
                assert 'Owned synthetic final-create fault' in str(error),str(error)
        fault=client.get('/goldstandard/session/'+fault_id)
        assert fault.status_code==200 and fault.json()['stale'] and fault.json()['stale_at'],fault.text
        assert 'cutover' in fault.json()['stale_reason']
        refused=client.post('/goldstandard/save',json={'session_id':fault_id})
        assert refused.status_code==409 and refused.json()['error']['code']=='HISTORICAL_SESSION',refused.text
        print('PASS actual failed reindex cutover marks retained history and blocks default export',flush=True)
        creation_attempted=False
        # #191: the real tuning job path marks sessions retained under either
        # first-character backend alias, with supplied vectors and no model call.
        # A collection of its own, so only this block's sessions share its spellings.
        aliased=collection+'Alias'
        alias=aliased[:1].lower()+aliased[1:]
        distinct=aliased[:1]+aliased[1:2].swapcase()+aliased[2:]
        alias_ids=[];alias_names={}
        def store_alias_sessions():
            ids={name:'gs_'+uuid.uuid4().hex[:8] for name in (aliased,alias,distinct)}
            for name,sid in ids.items():
                gs.store_session({'session_id':sid,'collection':name,'status':'completed','pairs_total':1,'pairs_completed':1,'pairs':copy.deepcopy(pairs)})
                assert not client.get('/goldstandard/session/'+sid).json()['stale']
            alias_ids.extend(ids.values());alias_names.update({sid:name for name,sid in ids.items()})
            return ids
        def tune(caller,operation,params):
            job_id='alias'+uuid.uuid4().hex[:8]
            tuning._jobs[job_id]={'job_id':job_id,'status':'queued','notes':[]}
            try:
                with patch.object(wc,'_insert_chunks_sync',side_effect=insert_supplied):
                    tuning._run(job_id,caller,operation,params,source_collection=caller)
                return tuning._jobs[job_id]
            finally:
                tuning._jobs.pop(job_id,None)
        response=client.post('/collections',json={'name':aliased})
        assert response.status_code==201,response.text
        alias_created=True
        insert_supplied(aliased,[{'content':'Owned inert chunk','source_file':'inert.txt','chunk_index':0}])
        ids=store_alias_sessions()
        job=tune(alias,'reindex',{'index_type':None,'distance_metric':None})
        assert job['status']=='completed',job
        for sid in ids.values():
            assert not client.get('/goldstandard/session/'+sid).json()['stale']
        print('PASS identity-preserving reindex through an alias leaves every session current',flush=True)
        for caller in (alias,aliased):
            ids=store_alias_sessions()
            job=tune(caller,'reembed',{'chunking':None})
            assert job['status']=='completed',job
            expected=sum(name in (aliased,alias) for name in alias_names.values())  # either spelling, every retained one
            assert any(f'{expected} gold-standard session(s) marked stale' in note for note in job['notes']),(caller,expected,job['notes'])
            gs._sessions={};gs.load_sessions_from_disk()   # flags must come from disk
            for name in (aliased,alias):
                current=client.get('/goldstandard/session/'+ids[name]).json()
                assert current['stale'] and current['stale_at'] and 're-embedded' in current['stale_reason'],(caller,name,current)
                assert current['collection']==name,(caller,name,current['collection'])
                refused=client.post('/goldstandard/save',json={'session_id':ids[name]})
                assert refused.status_code==409 and refused.json()['error']['code']=='HISTORICAL_SESSION',refused.text
                allowed=client.post('/goldstandard/save',json={'session_id':ids[name],'allow_historical':True})
                assert allowed.status_code==200 and allowed.json()['historical'],allowed.text
            unrelated=client.get('/goldstandard/session/'+ids[distinct]).json()
            assert not unrelated['stale'] and unrelated['collection']==distinct,unrelated
        print('PASS real re-embed through either spelling marks both alias sessions on disk, keeps provenance and a case-distinct name',flush=True)
        response=client.delete('/collections/'+aliased+'?confirm=true')
        assert response.status_code==200,response.text
        alias_created=False

    finally:
        if globals().get('alias_created') and wc._collection_exists_sync(aliased):
            response=client.delete('/collections/'+aliased+'?confirm=true')
            assert response.status_code==200,response.text
        if creation_attempted and wc._collection_exists_sync(collection):
            response=client.delete('/collections/'+collection+'?confirm=true')
            assert response.status_code==200,response.text
        # Recovery-enabled source combinations may retain a verified stage.
        # Only this helper's unique collection prefix authorizes its cleanup.
        for name in wc.get_client().collections.list_all(simple=True):
            if name.startswith(collection+'__') or name.startswith(collection+'Alias__'):
                wc.get_client().collections.delete(name)
        gs._sessions.pop(session_id,None)
        if 'empty_id' in locals():gs._sessions.pop(empty_id,None)
        if 'fault_id' in locals():gs._sessions.pop(fault_id,None)
        for alias_id in locals().get('alias_ids',[]):gs._sessions.pop(alias_id,None)
        wc.close_client()
assert not wc._collection_exists_sync(collection) and not wc._collection_exists_sync(collection+'Alias')
wc.close_client()
print('PASS owned collection/session/files removed',flush=True)
