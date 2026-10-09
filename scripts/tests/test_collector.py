"""Focused optional collector checks. Host stdlib plus Docker Compose config only.
No application container is started by these checks.
"""
import copy
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def archive(path, config, layer=b'synthetic layer', tags=None):
    with tarfile.open(path, 'w') as tar:
        data = {'config': config, 'layer': layer,
                'manifest.json': json.dumps([{'Config': 'config', 'Layers': ['layer'], 'RepoTags': tags}]).encode(),
                'index.json': b'{"untrusted":"must not pass to Docker"}'}
        for name, body in data.items():
            item = tarfile.TarInfo(name); item.size = len(body)
            tar.addfile(item, io.BytesIO(body))


class Offline(unittest.TestCase):
    def setUp(self):
        self.module = load('collector_offline', 'scripts/collector_offline.py')
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.input = Path(self.temp.name) / 'input.tar'
        self.output = Path(self.temp.name) / 'output.tar'
        self.config = json.dumps({'architecture': 'arm64', 'os': 'linux',
            'rootfs': {'diff_ids': [self.module.sha(b'synthetic layer')]}}).encode()
        self.module.PIN = {'config_ids': {'arm64': self.module.sha(self.config)}}

    def test_verified_bytes_strip_untrusted_tags_and_index(self):
        archive(self.input, self.config, tags=['rag-docker-api:latest'])
        expected = self.module.sanitize(self.input, self.output, 'arm64')
        self.assertEqual(expected, self.module.sha(self.config))
        with tarfile.open(self.output) as tar:
            self.assertEqual(set(tar.getnames()), {'config.json', 'manifest.json', 'layer-0.tar'})
            manifest = json.load(tar.extractfile('manifest.json'))
            self.assertIsNone(manifest[0]['RepoTags'])

    def test_layer_tampering_rejected_before_output(self):
        archive(self.input, self.config, b'tampered')
        with self.assertRaisesRegex(ValueError, 'layer differs'):
            self.module.sanitize(self.input, self.output, 'arm64')
        self.assertFalse(self.output.exists())

    def test_untrusted_config_cannot_select_image(self):
        archive(self.input, b'{}')
        with self.assertRaisesRegex(ValueError, 'trusted platform pin'):
            self.module.sanitize(self.input, self.output, 'arm64')

    def test_wrong_platform_refused(self):
        archive(self.input, self.config)
        with self.assertRaises(KeyError):
            self.module.sanitize(self.input, self.output, 'amd64')

    def test_duplicate_archive_names_refused(self):
        archive(self.input, self.config)
        with tarfile.open(self.input, 'a') as tar:
            data = b'[]'; item = tarfile.TarInfo('manifest.json'); item.size = len(data)
            tar.addfile(item, io.BytesIO(data))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            self.module.sanitize(self.input, self.output, 'arm64')

    def test_tar_terminators_and_trailing_bytes(self):
        archive(self.input, self.config)
        original = self.input.read_bytes()
        with tarfile.open(self.input) as tar:
            list(tar)
            end = tar.offset
        for suffix in (b'', bytes(512), bytes(1023), bytes(1024)+b'x', bytes(1024)+b'x'*512):
            with self.subTest(length=len(suffix)):
                self.input.write_bytes(original[:end]+suffix)
                with self.assertRaisesRegex(ValueError, 'terminator'):
                    self.module.sanitize(self.input,self.output,'arm64')
                self.assertFalse(self.output.exists())
        for padding in (1024, 2048):
            self.input.write_bytes(original[:end]+bytes(padding))
            self.module.sanitize(self.input,self.output,'arm64')
            self.output.unlink()

    def test_manifest_shapes_and_missing_references(self):
        for image in (None, [], 'bad', {}, {'Config':None,'Layers':[]},
                      {'Config':'config','Layers':None}, {'Config':'config','Layers':['']},
                      {'Config':'config','Layers':[1]}, {'Config':'missing','Layers':[]},
                      {'Config':'config','Layers':['missing']}):
            with self.subTest(image=image):
                with tarfile.open(self.input,'w') as tar:
                    for name, data in [('manifest.json',json.dumps([image]).encode()),('config',self.config)]:
                        item=tarfile.TarInfo(name); item.size=len(data);tar.addfile(item,io.BytesIO(data))
                with self.assertRaises(ValueError):
                    self.module.sanitize(self.input,self.output,'arm64')
                self.assertFalse(self.output.exists())

    def test_prepare_metadata_shapes(self):
        for data in (None, [], 'bad', 42, True):
            with self.subTest(data=data):
                Path(self.temp.name,'collector.json').write_text(json.dumps(data))
                with patch('sys.argv',['collector_offline','prepare',self.temp.name,'--output',str(self.output)]), \
                     patch.object(self.module.subprocess,'check_output',return_value='arm64'):
                    with self.assertRaisesRegex(ValueError,'metadata object'):
                        self.module.main()
                self.assertFalse(self.output.exists())


class Installer(unittest.TestCase):
    def test_default_and_optional_commands_and_missing_payload(self):
        for mode in ('default', 'optional', 'missing'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                for sub in ('scripts', 'telemetry', 'offline', 'bin'):
                    (root/sub).mkdir()
                shutil.copy(ROOT/'install-offline.sh', root/'install-offline.sh')
                shutil.copy(ROOT/'scripts/collector_offline.py', root/'scripts/collector_offline.py')
                layer = b'fixture layer'
                config = json.dumps({'architecture':'arm64', 'os':'linux',
                    'rootfs':{'diff_ids':['sha256:'+hashlib.sha256(layer).hexdigest()]}}).encode()
                identity = 'sha256:'+hashlib.sha256(config).hexdigest()
                (root/'telemetry/image.json').write_text(json.dumps({'config_ids':{'arm64':identity}}))
                (root/'offline/images.tar.gz').touch()
                (root/'offline/ollama_models.tar.gz').touch()
                if mode == 'optional':
                    target = root/'offline/collector.tar'
                    archive(target, config, layer)
                    (root/'offline/collector.json').write_text(json.dumps({'architecture':'arm64',
                        'archive_sha256':hashlib.sha256(target.read_bytes()).hexdigest()}))
                stub = root/'bin/docker'
                stub.write_text(r"""#!/usr/bin/env python3
import json,os,sys
with open(os.environ['TEST_LOG'],'a') as f: f.write(json.dumps(sys.argv[1:])+'\n')
a=sys.argv[1:]
if a[:1]==['info'] and '--format' in a: print('aarch64')
elif a[:2]==['image','inspect']: print(os.environ['TEST_ID'])
elif a[:2]==['compose','ps'] and '--services' in a:
 for i in range(int(os.environ['TEST_COUNT'])): print('service'+str(i))
""")
                stub.chmod(0o755)
                env = {**os.environ, 'PATH':str(root/'bin')+os.pathsep+os.environ['PATH'],
                       'TEST_LOG':str(root/'calls'), 'TEST_ID':identity, 'TEST_COUNT':'5' if mode=='default' else '6'}
                run = subprocess.run(['bash', str(root/'install-offline.sh'), *([] if mode=='default' else ['--telemetry'])],
                                     env=env, text=True, capture_output=True)
                calls = [json.loads(x) for x in (root/'calls').read_text().splitlines()]
                if mode == 'missing':
                    self.assertNotEqual(run.returncode,0)
                    self.assertFalse(any(x[0]=='load' or x[:2]==['compose','up'] for x in calls))
                    continue
                self.assertEqual(run.returncode,0,run.stderr)
                up = next(x for x in calls if x[:2]==['compose','up'])
                restore = next(x for x in calls if x[:2]==['compose','run'])
                if mode == 'optional':
                    self.assertIn('--pull',up); self.assertIn('never',up)
                    self.assertIn('--pull=never',restore)
                else:
                    self.assertEqual(up,['compose','up','-d','--no-build'])
                    self.assertNotIn('--pull=never',restore)


class Packager(unittest.TestCase):
    def test_printed_install_command_matches_bundle_mode(self):
        for telemetry in (False,True):
            with self.subTest(telemetry=telemetry), tempfile.TemporaryDirectory() as folder:
                root=Path(folder);(root/'bin').mkdir();(root/'scripts').mkdir()
                shutil.copy(ROOT/'package-offline.sh',root/'package-offline.sh')
                (root/'scripts/collector_offline.py').write_text("from pathlib import Path\nimport sys\nPath(sys.argv[2],'collector.tar').touch()\n")
                stub=root/'bin/docker'
                stub.write_text("""#!/usr/bin/env python3
import sys,json,pathlib
args=sys.argv[1:]
if args[:2]==['compose','config']:print(json.dumps({'name':'fixture'}))
if args[:1]==['run']:
 for arg in args:
  if arg.endswith(':/out'):pathlib.Path(arg[:-5],'ollama_models.tar.gz').touch()
""")
                stub.chmod(0o755)
                result=subprocess.run(['bash',str(root/'package-offline.sh'),*(['--telemetry'] if telemetry else []),'bundle.tar'],
                    env={**os.environ,'PATH':str(root/'bin')+os.pathsep+os.environ['PATH']},text=True,capture_output=True)
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertEqual(result.stdout.splitlines()[-1],'  bash install-offline.sh'+(' --telemetry' if telemetry else ''))
                with tarfile.open(root/'bundle.tar') as tar:
                    self.assertEqual('rag-docker/offline/collector.tar' in tar.getnames(),telemetry)


class Compose(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.env = {**os.environ, 'RAG_VERIFY_PORT': '18081', 'RAG_EXPORTS_DIR': '/tmp/collector-test-exports',
                   'COMPOSE_PROFILES': 'telemetry', 'RAG_VERIFY_HARNESS': str(ROOT)}
        cls.cmd = ['docker', 'compose', '-p', 'rag-verify', '-f', str(ROOT/'docker-compose.yml'),
                   '-f', str(ROOT/'docker-compose.telemetry.yml'), '-f', str(ROOT/'docker-compose.verify.yml'),
                   '-f', str(ROOT/'docker-compose.telemetry.verify.yml'), 'config', '--format', 'json']
        cls.config = json.loads(subprocess.check_output(cls.cmd, env=cls.env))
        cls.guard = re.findall(r"python3 - [^\n]*<<'GUARDPY'\n(.*?)\nGUARDPY", (ROOT/'scripts/verify/stack.sh').read_text(), re.S)[0]

    def run_guard(self, config, checkout=ROOT):
        with tempfile.NamedTemporaryFile(mode='w') as file:
            json.dump(config, file); file.flush()
            return subprocess.run(['python3', '-', str(checkout), self.env['RAG_EXPORTS_DIR'], '18081', file.name, str(ROOT)],
                                  input=self.guard, text=True, capture_output=True)

    def test_optional_config_passes_full_guard(self):
        result = self.run_guard(self.config)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        api = self.config['services']['api']['environment']
        self.assertEqual(api['RAG_OTEL_ENABLED'], 'true')
        self.assertEqual(api['RAG_OTEL_ENDPOINT'], 'http://otel-collector:4318')

    def test_default_does_not_include_collector_or_enable_api(self):
        value = json.loads(subprocess.check_output(['docker', 'compose', '-f', str(ROOT/'docker-compose.yml'),
                                                    'config', '--format', 'json']))
        self.assertEqual(len(value['services']), 5)
        self.assertNotIn('RAG_OTEL_ENABLED', value['services']['api']['environment'])

    def test_ambient_inputs_and_distinct_checkout(self):
        hostile={k:'invalid' for k in self.config['services']['api']['environment'] if k.startswith('RAG_OTEL_')}
        config=json.loads(subprocess.check_output(self.cmd,env={**self.env,**hostile}))
        self.assertEqual(config,self.config)
        with tempfile.TemporaryDirectory() as folder:
            checkout=Path(folder)
            for name in ('docker-compose.yml','docker-compose.telemetry.yml'):
                shutil.copy(ROOT/name,checkout/name)
            cmd=[str(checkout/Path(x).name) if x in (str(ROOT/'docker-compose.yml'),str(ROOT/'docker-compose.telemetry.yml')) else x for x in self.cmd]
            value=json.loads(subprocess.check_output(cmd,env=self.env))
            for service in ('otel-collector','otel-capture'):
                mount=next(v for v in value['services'][service]['volumes'] if v['type']=='bind')
                self.assertTrue(mount['source'].startswith(str(ROOT/'scripts/verify')))
            result=self.run_guard(value,checkout)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            guard=load('guard','scripts/verify/telemetry_guard.py')
            self.assertEqual(guard.check(value,ROOT),[])
            for service in ('otel-collector','otel-capture'):
                changed=copy.deepcopy(value)
                next(v for v in changed['services'][service]['volumes'] if v['type']=='bind')['source']=str(checkout/'scripts/verify')
                self.assertTrue(guard.check(changed,ROOT))

    def test_unsafe_variants_still_refused(self):
        mutations = [
            lambda c: c['services']['otel-collector'].update(command=['--config=http://outside.invalid/config']),
            lambda c: c['services']['otel-capture'].update(command=['python', '/tmp/replacement.py']),
            lambda c: c['services']['otel-capture'].update(entrypoint=['sh']),
            lambda c: c['services']['otel-collector'].update(ports=[{'target':4318, 'published':'4318'}]),
            lambda c: c['services']['otel-collector'].update(mem_limit='0'),
            lambda c: c['services']['otel-collector'].update(image='evil:latest'),
            lambda c: c['services']['api'].update(mem_limit='268435456'),
            lambda c: c['services']['api']['depends_on'].update({'otel-collector': {'condition':'service_started'}}),
            lambda c: c['services']['otel-capture']['volumes'][0].update(source='/private/secret'),
            lambda c: c['services']['otel-collector'].update(logging={'driver':'json-file'}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                config = copy.deepcopy(self.config); mutate(config)
                self.assertNotEqual(self.run_guard(config).returncode, 0)


if __name__ == '__main__':
    unittest.main()
