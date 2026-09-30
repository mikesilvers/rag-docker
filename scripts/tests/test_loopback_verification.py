"""Exercise the verifier's actual Python assertions with adverse port metadata."""
import copy
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SOURCE = (ROOT / 'scripts/verify/01_infrastructure.sh').read_text()
BLOCKS = re.findall(r"python3(?:[^\n]*) <<'ENDPY'\n(.*?)\nENDPY", SOURCE, re.S)


class LoopbackVerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {**os.environ, 'RAG_INFRA_TMP': str(self.root), 'EXPECTED_PORT': '8080'}
        self.assertEqual(len(BLOCKS), 4)

    def run_block(self, index, *arguments):
        return subprocess.run([sys.executable, '-c', BLOCKS[index], *arguments],
                              env=self.env, capture_output=True).returncode

    def test_engine_floor(self):
        for version, accepted in [('27.5.1', False), ('28.0.0-rc.1', False),
                                  ('28.0.0', True), ('29.8.0', True), ('unknown', False)]:
            with self.subTest(version=version):
                self.assertEqual(self.run_block(0, version) == 0, accepted)

    def test_embedded_verification_copies_match_runnable_sources(self):
        implementation = (ROOT / 'IMPLEMENTATION.md').read_text()
        for name, fence, language in [('scripts/verify/01_infrastructure.sh', '```', 'bash'),
                                      ('scripts/verify/README.md', '````', 'markdown')]:
            with self.subTest(path=name):
                header = '### ' + name + '\n\n' + fence + language + '\n'
                start = implementation.index(header) + len(header)
                end = implementation.index('\n' + fence + '\n', start)
                self.assertEqual(implementation[start:end], (ROOT / name).read_text().rstrip('\n'))

    def test_default_resolved_configuration_and_adverse_variants(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith('COMPOSE_')}
        resolved = json.loads(subprocess.check_output(
            ['docker', 'compose', '-f', str(ROOT / 'docker-compose.yml'), 'config', '--format', 'json'],
            cwd=ROOT, env=env, text=True))
        port = resolved['services']['proxy']['ports'][0]
        self.assertEqual(str(port['published']), '8080')
        variants = [(resolved, True)]
        for field, value in [('host_ip', '0.0.0.0'), ('host_ip', None),
                             ('published', '18080'), ('target', 8000), ('protocol', 'udp')]:
            bad = copy.deepcopy(resolved)
            bad['services']['proxy']['ports'][0][field] = value
            variants.append((bad, False))
        extra = copy.deepcopy(resolved)
        extra['services']['api']['ports'] = [dict(port)]
        variants.append((extra, False))
        for value, accepted in variants:
            with self.subTest(value=value['services']['proxy']['ports']):
                (self.root / 'vfy_compose.json').write_text(json.dumps(value))
                self.assertEqual(self.run_block(1) == 0, accepted)
        alternate = copy.deepcopy(resolved)
        alternate['services']['proxy']['ports'][0]['published'] = '18080'
        (self.root / 'vfy_compose.json').write_text(json.dumps(alternate))
        self.env['EXPECTED_PORT'] = '18080'
        self.assertEqual(self.run_block(1), 0)

    def test_live_metadata_requires_expected_loopback_proxy(self):
        binding = {'service': 'proxy', 'container_port': '80/tcp',
                   'HostIp': '127.0.0.1', 'HostPort': '8080'}
        variants = [([binding], True), ([], False), ([binding, binding], False)]
        for field, value in [('service', 'api'), ('container_port', '8000/tcp'),
                             ('HostIp', '0.0.0.0'), ('HostIp', '::'), ('HostPort', '18080')]:
            variants.append(([{**binding, field: value}], False))
        for value, accepted in variants:
            with self.subTest(bindings=value):
                (self.root / 'vfy_bindings.json').write_text(json.dumps(value))
                self.assertEqual(self.run_block(3) == 0, accepted)
        self.env['EXPECTED_PORT'] = '18080'
        (self.root / 'vfy_bindings.json').write_text(json.dumps([{**binding, 'HostPort': '18080'}]))
        self.assertEqual(self.run_block(3), 0)


if __name__ == '__main__':
    unittest.main()
