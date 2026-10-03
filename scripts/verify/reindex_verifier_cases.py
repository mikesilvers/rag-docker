"""Owned async lifecycle and parent-cleanup regressions; no backend/model calls."""
import ast,asyncio,json,os,subprocess,sys,tempfile,threading,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,os.environ.get('RAG_TEST_API_DIR',str(Path(__file__).resolve().parents[2]/'api')))
# Loaded from the host script on stdin with its helper passed alongside it.
source=Path(os.environ.get('RAG_REINDEX_VERIFIER_SOURCE',str(Path(__file__).with_name('reindex.py'))))
tree=ast.parse(source.read_text());assert isinstance(tree.body[-1],ast.Expr);tree.body.pop()
ns={'__file__':str(source),'__name__':'owned_verifier'};exec(compile(tree,str(source),'exec'),ns)

class LifecycleTests(unittest.TestCase):
    def test_failure_cleans_exact_collections_and_temp_off_loop_and_restores_cache(self):
        loop_thread=threading.get_ident();calls=[];created={};closed=[];temps=[]
        original_temp=tempfile.TemporaryDirectory
        class OwnedTemp(original_temp):
            def __init__(self,*args,**kwargs):calls.append(('temp_create',threading.get_ident()));super().__init__(*args,**kwargs);temps.append(self.name)
            def cleanup(self):calls.append(('temp_cleanup',threading.get_ident()));super().cleanup()
        def create(name,*args,**kwargs):name=ns['wc'].collection_writes.canonical(name);calls.append(('create',threading.get_ident()));created[name]=[]
        def delete(name):name=ns['wc'].collection_writes.canonical(name);calls.append(('delete',threading.get_ident()));del created[name]
        def collection(name):
            name=ns['wc'].collection_writes.canonical(name)
            def insert(properties,uuid=None,vector=None):
                if uuid is None:raise RuntimeError('connection refused 127.0.0.1:1')
                created[name].append(dict(id=uuid,properties=properties,vector=vector))
            return SimpleNamespace(config=SimpleNamespace(get=lambda:SimpleNamespace(name=name)),data=SimpleNamespace(insert=insert),aggregate=SimpleNamespace(over_all=lambda **kw:SimpleNamespace(total_count=len(created[name]))))
        client=SimpleNamespace(collections=SimpleNamespace(get=collection,exists=lambda name:ns["wc"].collection_writes.canonical(name) in created,delete=delete),close=lambda:closed.append(threading.get_ident()))
        protected={'protected':{'session_id':'gs_11111111'}}
        with patch.object(ns['tempfile'],'TemporaryDirectory',OwnedTemp),patch.object(ns['wc'],'get_client',return_value=client),patch.object(ns['wc'],'_create_collection_sync',side_effect=create),patch.object(ns['tuning'],'_existing_records',side_effect=lambda name:list(created[ns["wc"].collection_writes.canonical(name)])),patch.object(ns['gs'],'_sessions',protected),patch.object(ns['gs'],'store_session',side_effect=OSError('Owned session failure')):
            with self.assertRaisesRegex(OSError,'Owned session failure'):asyncio.run(ns['main']())
            self.assertIs(ns['gs']._sessions,protected)
        self.assertFalse(created);self.assertEqual(len(closed),1);self.assertTrue(all(identity!=loop_thread for _,identity in calls));self.assertNotEqual(closed[0],loop_thread)
        self.assertTrue(all(not Path(directory).exists() for directory in temps))
    def test_deletion_verifier_uses_actual_handler_with_owned_backend_and_sidecars(self):
        from services import ingest_config,retrieval_config
        backend={};checks=[];created=[]
        canonical=ns['wc'].collection_writes.canonical
        def create(name,*args,**kwargs):backend[canonical(name)]=0;created.append(canonical(name))
        def collection(name):
            name=canonical(name)
            def insert(**kwargs):backend[name]+=1
            return SimpleNamespace(config=SimpleNamespace(get=lambda:SimpleNamespace(name=name)),data=SimpleNamespace(insert=insert),aggregate=SimpleNamespace(over_all=lambda **kwargs:SimpleNamespace(total_count=backend[name])))
        client=SimpleNamespace(collections=SimpleNamespace(get=collection,exists=lambda name:canonical(name) in backend,delete=lambda name:backend.pop(canonical(name))))
        def check(condition,label):self.assertTrue(condition,label);checks.append(label)
        async def run(directory):
            async with ns['httpx'].AsyncClient(transport=ns['httpx'].ASGITransport(app=ns['app']),base_url='http://owned') as api:
                await ns['collection_deletion_checks'](api,client,'OwnedVerifier',directory,check)
        with tempfile.TemporaryDirectory() as directory,patch.object(ns['settings'],'upload_dir',directory),patch.object(ns['settings'],'sources_dir',str(Path(directory)/'sources')),patch.object(ns['gs'],'_sessions',{}),patch.object(ingest_config,'_DIR',None),patch.object(retrieval_config,'_DIR',None),patch.object(ns['wc'],'get_client',return_value=client),patch.object(ns['wc'],'_create_collection_sync',side_effect=create):
            asyncio.run(run(directory))
        self.assertEqual(len(checks),10);self.assertEqual(set(backend),{'OwnedVerifierAliasDeleteNeighbor','OwnedVerifierCanonicalDeleteNeighbor'});self.assertEqual(len(created),4)
    def test_client_failure_still_cleans_temporary_directory(self):
        original_temp=tempfile.TemporaryDirectory;temps=[]
        def create(*args,**kwargs):result=original_temp(*args,**kwargs);temps.append(result.name);return result
        with patch.object(ns['tempfile'],'TemporaryDirectory',side_effect=create),patch.object(ns['wc'],'get_client',side_effect=OSError('Owned client failure')):
            with self.assertRaisesRegex(OSError,'Owned client failure'):asyncio.run(ns['main']())
        self.assertTrue(temps);self.assertTrue(all(not Path(directory).exists() for directory in temps))
    def test_temp_creation_failure_does_not_open_client(self):
        with patch.object(ns['tempfile'],'TemporaryDirectory',side_effect=OSError('Owned temp failure')),patch.object(ns['wc'],'get_client') as client:
            with self.assertRaisesRegex(OSError,'Owned temp failure'):asyncio.run(ns['main']())
            client.assert_not_called()
    def test_parent_cleanup_preserves_exact_owned_namespace_and_receipt(self):
        prefix='VfyParent';owned=ns['owned_name'](prefix,'49000000');probe=owned+'Probe';parent=prefix+'Transfer'
        with tempfile.TemporaryDirectory(prefix='owned-parent-cleanup-') as directory:
            root=Path(directory);fixture=root/'collections.json';fixture.write_text(json.dumps({'collections':[{'name':name} for name in [owned,probe,parent]]}))
            receipt=ns['preserve_receipt'](directory,[owned,probe],[('tuning','owned-job','running')]);data=json.loads(Path(receipt).read_text())
            self.assertEqual(data['created_collections'],[owned,probe]);self.assertEqual(data['pending_jobs'],[['tuning','owned-job','running']])
            lib=Path(os.environ.get('RAG_VERIFIER_LIB',str(source.with_name('lib.sh'))))
            code='source "$1"; PREFIX="$2"; REPO_ROOT="$3"; api_get(){ cat "$4"; }; drop_collection(){ printf "%s\\n" "$1"; }; cleanup_prefixed'
            # api_get's function arguments differ from the script's, so retain
            # the controlled input path in a distinct variable before defining it.
            code=code.replace('api_get(){ cat "$4"; }','owned_fixture="$4"; api_get(){ cat "$owned_fixture"; }')
            run=subprocess.run(['bash','-c',code,'owned',str(lib),prefix,directory,str(fixture)],text=True,capture_output=True,cwd=directory)
            self.assertEqual(run.returncode,0,run.stderr);self.assertEqual(run.stdout.splitlines(),[parent]);self.assertTrue(Path(receipt).exists())
        with self.assertRaises(ValueError):ns['owned_name']('','owned')

if __name__=='__main__':unittest.main()
