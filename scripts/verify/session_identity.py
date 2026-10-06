"""Owned real HTTP/package/backend export-edit-rename-import-twice acceptance.

Pairs and vectors are synthetic; package/model metadata and import/export jobs
are real. No generation call or startup sweep runs in this process.
"""
import asyncio,copy,hashlib,json,os,subprocess,sys,tarfile,tempfile,threading,uuid
from pathlib import Path
from unittest.mock import patch
import httpx
from config import settings
from main import app
from services import exporter,goldstandard as gs,importer,weaviate_client as wc


def archive_session(archive, identity):
    with tarfile.open(archive) as package:
        member=next(m for m in package.getmembers() if m.name.endswith('/goldstandard/'+identity+'.json'))
        return json.load(package.extractfile(member))

def file_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def legacy_archive(archive,root,identity,source_id):
    with tempfile.TemporaryDirectory(prefix='owned-legacy-package-',dir=root) as temp:
        work=Path(temp)
        with tarfile.open(archive) as package:package.extractall(work,filter='data')
        package_root=next(path for path in work.iterdir() if path.is_dir())
        relative='goldstandard/'+identity+'.json';sidecar=package_root/relative
        data=json.loads(sidecar.read_text());data['session_id']=source_id;sidecar.write_text(json.dumps(data))
        manifest_path=package_root/'manifest.json';manifest=json.loads(manifest_path.read_text())
        manifest['files'][relative]='sha256:'+file_digest(sidecar);manifest_path.write_text(json.dumps(manifest,sort_keys=True,indent=2))
        output=archive.parent/(archive.name.removesuffix('.tar.gz')+'-legacy.tar.gz')
        with tarfile.open(output,'w:gz') as package:package.add(package_root,arcname=package_root.name)
        return output.name

async def completed(client,path,timeout=300,expected='completed'):
    deadline=asyncio.get_running_loop().time()+timeout;last='not observed'
    while True:
        remaining=deadline-asyncio.get_running_loop().time()
        if remaining<=0:raise TimeoutError(f'Owned job {path} exceeded {timeout}s; last status={last}')
        try:response=await asyncio.wait_for(client.get(path),remaining)
        except asyncio.TimeoutError as exc:raise TimeoutError(f'Owned job {path} exceeded {timeout}s; last status={last}') from exc
        assert response.status_code==200,response.text
        job=response.json();last=job.get('status','missing')
        if last not in ('queued','running'):
            assert last==expected,job
            return job
        await asyncio.sleep(min(.1,max(0,deadline-asyncio.get_running_loop().time())))

async def settle_owned_jobs(jobs,timeout=30):
    deadline=asyncio.get_running_loop().time()+timeout
    while True:
        pending=[(module.__name__,jobid,(module.get_job(jobid) or {}).get('status','missing')) for module,jobid in jobs if (module.get_job(jobid) or {}).get('status') not in ('completed','failed')]
        if not pending or asyncio.get_running_loop().time()>=deadline:return pending
        await asyncio.sleep(min(.1,max(0,deadline-asyncio.get_running_loop().time())))

async def bounded_poll_cases():
    from types import SimpleNamespace
    class StuckClient:
        async def get(self,path):return SimpleNamespace(status_code=200,text='',json=lambda:{'status':'running'})
    try:await completed(StuckClient(),'/owned/stuck-job',timeout=.01)
    except TimeoutError as exc:assert '/owned/stuck-job' in str(exc) and 'running' in str(exc)
    else:raise AssertionError('Stuck owned job did not meet its deadline')
    pending=await settle_owned_jobs([(SimpleNamespace(__name__='owned',get_job=lambda identity:{'status':'queued'}),'owned-cleanup-job')],timeout=.01)
    assert pending==[('owned','owned-cleanup-job','queued')]
    print('PASS two controlled job-poll and cleanup deadline cases',flush=True)

collection=os.environ.get('RAG_TEST_PREFIX','Vfy')+'Identity'+uuid.uuid4().hex[:10]
sid='gs_'+uuid.uuid4().hex[:8]
protected_neighbor=collection+'_protected'
neighbor_created=False
created_names=set()
created_lock=threading.Lock()
original_create=wc._create_collection_sync
def record_create(name,*args,**kwargs):
    original_create(name,*args,**kwargs)
    with created_lock:created_names.add(name)
jobs=[]
with tempfile.TemporaryDirectory(prefix='owned-import-session-') as directory:
    root=Path(directory)
    with patch.object(settings,'upload_dir',str(root/'uploads')),patch.object(settings,'sources_dir',str(root/'sources')),patch.object(settings,'exports_dir',str(root/'exports')),patch.object(gs,'_sessions',{}),patch.object(wc,'_create_collection_sync',side_effect=record_create):
        async def run():
            global neighbor_created
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://owned-fixture') as client:
                await bounded_poll_cases()
                assert not await asyncio.to_thread(wc._collection_exists_sync,collection),'Owned name already exists'
                assert not await asyncio.to_thread(wc._collection_exists_sync,protected_neighbor),'Protected fixture name already exists'
                await asyncio.to_thread(original_create,protected_neighbor,'hnsw','cosine',{})
                neighbor_created=True
                response=await client.post('/collections',json={'name':collection});assert response.status_code==201,response.text
                coll=await asyncio.to_thread(lambda:wc.get_client().collections.get(collection))
                await asyncio.to_thread(coll.data.insert,properties={'content':'Owned inert evaluation context','source_file':'inert.txt','chunk_index':0},vector=[0.1]*768)
                session={'session_id':sid,'collection':collection,'status':'completed','pairs_total':1,'pairs_completed':1,'pairs':[{'pair_id':'p_owned','question':'Inert question','answer':'Original exported answer','contexts':['Owned inert evaluation context'],'ground_truth':'Inert truth','source_file':'inert.txt','chunk_index':0,'status':'approved'}]}
                await asyncio.to_thread(gs.store_session,session)
                print('PASS owned real collection, supplied-vector object and synthetic evaluation session created',flush=True)
                start=await client.post('/export',json={'collection':collection,'include_models':False});assert start.status_code==202,start.text
                jobs.append((exporter,start.json()['job_id']))
                exported=await completed(client,'/export/job/'+start.json()['job_id']);archive=root/'exports'/exported['filename'];digest=await asyncio.to_thread(file_digest,archive)
                retained=await asyncio.to_thread(archive_session,archive,sid);assert retained['pairs'][0]['answer']=='Original exported answer'
                print('PASS actual completed export package contains the original session snapshot',flush=True)
                edited=await client.patch('/goldstandard/session/'+sid+'/pair/p_owned',json={'status':'edited','answer':'Newer acknowledged human answer'});assert edited.status_code==200,edited.text
                original_path=await asyncio.to_thread(gs._session_path,sid);original_bytes=await asyncio.to_thread(original_path.read_bytes)
                print('PASS newer original human edit acknowledged through HTTP after package export',flush=True)
                mappings=[];targets=[]
                for _ in range(2):
                    start=await client.post('/import',json={'filename':exported['filename'],'on_conflict':'rename'});assert start.status_code==202,start.text
                    jobs.append((importer,start.json()['job_id']))
                    imported=await completed(client,'/import/job/'+start.json()['job_id'])
                    assert imported['renamed'] and len(imported['restored_sessions'])==1,imported
                    mapping=imported['restored_sessions'][0];assert mapping['source_session_id']==sid and mapping['collection']==imported['collection']
                    assert any(mapping['session_id'] in note for note in imported['notes']),imported
                    mappings.append(mapping);targets.append(imported['collection'])
                    assert await asyncio.to_thread(original_path.read_bytes)==original_bytes,'Original human edit was overwritten'
                assert len({collection,*targets})==3 and len({sid,*[m['session_id'] for m in mappings]})==3
                print('PASS two actual rename imports expose distinct collections and independent local session mappings without changing original bytes',flush=True)
                for local_sid,target,expected in [(sid,collection,'Newer acknowledged human answer')]+[(m['session_id'],m['collection'],'Original exported answer') for m in mappings]:
                    response=await client.get('/goldstandard/session/'+local_sid);assert response.status_code==200,response.text
                    loaded=response.json();assert loaded['collection']==target and loaded['pairs'][0]['answer']==expected,loaded
                    if local_sid!=sid:
                        assert loaded['imported_from']['session_id']==sid and loaded['imported_from']['collection']==collection and loaded['imported_from']['imported_at']
                    saved=await client.post('/goldstandard/save',json={'session_id':local_sid,'filename':'owned-'+local_sid+'.json'});assert saved.status_code==200,saved.text
                    download=await client.get('/goldstandard/download/'+saved.json()['filename']);assert download.status_code==200,download.text
                    rows=download.json();assert len(rows)==1 and rows[0]['answer']==expected and set(rows[0])=={'question','answer','contexts','ground_truth'},rows
                print('PASS original and both reported imported IDs remain usable through HTTP lookup/provenance/save/download with exact four-field RAGAS rows',flush=True)
                code="from config import settings;from services import goldstandard as gs;import json,sys;settings.upload_dir=sys.argv[1];gs.load_sessions_from_disk();print(json.dumps([gs.get_session(s) for s in sys.argv[2:]]))"
                identities=[sid]+[m['session_id'] for m in mappings]
                reload=await asyncio.to_thread(subprocess.run,[sys.executable,'-c',code,str(root/'uploads'),*identities],text=True,capture_output=True,check=True)
                fresh=json.loads(reload.stdout);assert [row['collection'] for row in fresh]==[collection,*targets];assert fresh[0]['pairs'][0]['answer']=='Newer acknowledged human answer';assert all(row['imported_from']['session_id']==sid for row in fresh[1:])
                print('PASS fresh independent API process restores all three session identities, newer original review and imported provenance',flush=True)
                start=await client.post('/export',json={'collection':targets[0],'include_models':False});assert start.status_code==202,start.text
                jobs.append((exporter,start.json()['job_id']))
                reexported=await completed(client,'/export/job/'+start.json()['job_id'])
                imported_sid=mappings[0]['session_id'];retained=await asyncio.to_thread(archive_session,root/'exports'/reexported['filename'],imported_sid)
                assert retained['session_id']==imported_sid and retained['imported_from']['session_id']==sid
                assert await asyncio.to_thread(file_digest,archive)==digest
                print('PASS re-export uses the allocated session filename/provenance and the original package remains byte-identical',flush=True)
                source_id='legacy-review-2024';legacy_filename=await asyncio.to_thread(legacy_archive,archive,root,sid,source_id)
                start=await client.post('/import',json={'filename':legacy_filename,'on_conflict':'rename'});assert start.status_code==202,start.text
                jobs.append((importer,start.json()['job_id']));legacy=await completed(client,'/import/job/'+start.json()['job_id'],expected='failed')
                assert legacy['error_code']=='PACKAGE_CORRUPT' and legacy['error_detail']=={'file':'goldstandard/'+sid+'.json'} and not legacy['restored_sessions'],legacy
                assert await asyncio.to_thread(original_path.read_bytes)==original_bytes
                print('PASS digest-valid noncanonical-ID archive is refused PACKAGE_CORRUPT naming the sidecar, restores nothing and leaves the original newer review unchanged',flush=True)


        async def owned():
            try:await run()
            finally:
                pending=await settle_owned_jobs(jobs)
                if pending:
                    # This standalone verifier owns these worker threads. Hard
                    # exit prevents asyncio's executor shutdown joining a stuck
                    # job forever, and preserves its exact fixture directory.
                    print('FAIL owned jobs did not settle: '+repr(pending)+'; preserved fixture directory '+str(root),flush=True)
                    os._exit(2)
                for name in sorted(created_names):
                    if await asyncio.to_thread(wc._collection_exists_sync,name):
                        await asyncio.to_thread(wc._delete_collection_sync,name)
                if neighbor_created:
                    assert await asyncio.to_thread(wc._collection_exists_sync,protected_neighbor),'Exact cleanup deleted a similarly named protected collection'
                    await asyncio.to_thread(wc._delete_collection_sync,protected_neighbor)
                    print('PASS exact recorded-name cleanup leaves a similarly named collection intact until explicit fixture teardown',flush=True)
                await asyncio.to_thread(wc.close_client)
        asyncio.run(owned())
assert all(not wc._collection_exists_sync(name) for name in created_names);wc.close_client()
print('PASS only owned collections/packages/session fixtures removed',flush=True)
