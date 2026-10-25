"""Host-only check that the pinned dependencies are at or above their patched versions (#522).

Reads the Python locks, api/requirements.in and ui/package-lock.json. The
floors are the first patched versions of the Dependabot alerts #522 closed.
filelock (3.x, no patched release that unstructured accepts) and
postcss-selector-parser (6.x, required by tailwindcss 3) are left out on
purpose; the #522 PR explains those alerts.

Standard library only; no Docker or stack needed.
Run: python3 scripts/tests/test_dependency_floors.py
"""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]

FLOORS = {
    'api/requirements.txt': {
        'pillow': '13.0.0', 'certifi': '2026.10.19', 'pypdf': '6.21.2', 'urllib3': '2.8.0',
        'opentelemetry-api': '1.45.1', 'opentelemetry-sdk': '1.45.1', 'opentelemetry-proto': '1.45.1',
        'opentelemetry-exporter-otlp-proto-common': '1.45.1',
        'opentelemetry-exporter-otlp-proto-http': '1.45.1',
    },
    'mcp/requirements.txt': {
        'certifi': '2026.10.19', 'pyjwt': '2.15.0', 'opentelemetry-api': '1.45.1',
    },
}
NPM_FLOORS = {'vite': '6.4.4', 'source-map-js': '1.2.2'}
OTEL_FAMILY = ('opentelemetry-api', 'opentelemetry-sdk', 'opentelemetry-proto',
               'opentelemetry-exporter-otlp-proto-common', 'opentelemetry-exporter-otlp-proto-http')


def normalise(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def pins(path):
    """name -> version for every `name==version` line of a requirements file."""
    found = {}
    for line in (ROOT / path).read_text().splitlines():
        line = line.split('#', 1)[0].strip()
        match = re.fullmatch(r'([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==(\S+)', line)
        if match:
            found[normalise(match.group(1))] = match.group(2)
    return found


def release(version):
    """The numeric release part of a version, as a tuple of ints."""
    match = re.match(r'\d+(?:\.\d+)*', version)
    if not match:
        raise ValueError('not a release version: ' + version)
    return tuple(int(part) for part in match.group(0).split('.'))


class DependencyFloorTests(unittest.TestCase):
    def test_python_locks_meet_patched_versions(self):
        for path, floors in FLOORS.items():
            lock = pins(path)
            for name, floor in floors.items():
                with self.subTest(lock=path, package=name):
                    self.assertIn(name, lock, '{} does not pin {}'.format(path, name))
                    self.assertGreaterEqual(release(lock[name]), release(floor),
                                            '{} pins {}=={}, below {}'.format(path, name, lock[name], floor))

    def test_opentelemetry_family_moves_together(self):
        api, mcp, inputs = pins('api/requirements.txt'), pins('mcp/requirements.txt'), pins('api/requirements.in')
        versions = {api[name] for name in OTEL_FAMILY} | {mcp['opentelemetry-api']}
        self.assertEqual(versions, {'1.45.1'})
        self.assertEqual(api['opentelemetry-semantic-conventions'], '0.66b1')
        for name in ('opentelemetry-sdk', 'opentelemetry-exporter-otlp-proto-http'):
            with self.subTest(package=name):
                self.assertEqual(inputs.get(name), api[name])

    def test_ui_lock_meets_patched_versions(self):
        packages = json.loads((ROOT / 'ui/package-lock.json').read_text())['packages']
        for name, floor in NPM_FLOORS.items():
            entries = {key: value['version'] for key, value in packages.items()
                       if key == 'node_modules/' + name or key.endswith('/node_modules/' + name)}
            with self.subTest(package=name):
                self.assertTrue(entries, 'ui/package-lock.json has no ' + name)
                for key, version in entries.items():
                    self.assertGreaterEqual(release(version), release(floor),
                                            '{} resolves {}, below {}'.format(key, version, floor))


if __name__ == '__main__':
    unittest.main()
