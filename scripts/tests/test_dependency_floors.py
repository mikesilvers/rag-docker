"""Host-only check that the pinned dependencies are at or above their patched versions (#522, #562, #651, #749), that the Python locks stay fully pinned (#524)
and that the MCP lock satisfies mcp/requirements.in (#524).

Reads the Python locks, api/requirements.in, ui/package.json and
ui/package-lock.json. The floors are the first patched versions of the
Dependabot alerts #522, #562, #651 and #749 closed. filelock (3.x, no patched release that
unstructured accepts) is left out on purpose; the #522 PR explains its alerts.
postcss-selector-parser stays on 6.x: @tailwindcss/typography, its only user
since Tailwind 4 (RD-157), pins 6.0.10 exactly, and a scoped npm override in
ui/package.json lifts it to the patched 6.1.5 (#562).

The floors are the contract: the tests don't compare the pins with exact
versions, so a bump needs no test change unless it moves a floor.

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
        'pillow': '13.0.0', 'certifi': '2026.10.19', 'pypdf': '6.21.4', 'pyasn1': '0.6.5', 'urllib3': '2.8.0', 'soupsieve': '4.0',
        'opentelemetry-api': '1.45.2', 'opentelemetry-sdk': '1.45.2', 'opentelemetry-proto': '1.45.2',
        'opentelemetry-exporter-otlp-proto-common': '1.45.2',
        'opentelemetry-exporter-otlp-proto-http': '1.45.2',
        'authlib': '1.9.1',
    },
    'mcp/requirements.txt': {
        'certifi': '2026.10.19', 'pyjwt': '2.15.0', 'opentelemetry-api': '1.45.2',
    },
}
NPM_FLOORS = {'vite': '6.4.4', 'source-map-js': '1.2.2', 'postcss-selector-parser': '6.1.5'}
TYPOGRAPHY_OVERRIDE = {'@tailwindcss/typography': {'postcss-selector-parser': '^6.1.5'}}
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


LOCKS = ('api/requirements.txt', 'mcp/requirements.txt')
EXACT_PIN = re.compile(r'([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==[0-9][^\s;]*')
CLAUSE = re.compile(r'(>=|<=|==|!=|>|<)\s*(\d+(?:\.\d+)*)')
DIRECT = re.compile(r'([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)')


def lock_problems(text):
    """What stops a lock's text from being fully pinned: a line that isn't an exact
    `name==version` pin, and a package pinned twice."""
    problems, seen = [], set()
    for number, line in enumerate(text.splitlines(), 1):
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        match = EXACT_PIN.fullmatch(line)
        if not match:
            problems.append('line {} is not an exact name==version pin: {}'.format(number, line))
            continue
        name = normalise(match.group(1))
        if name in seen:
            problems.append('{} is pinned twice (line {})'.format(name, number))
        seen.add(name)
    return problems


def direct_requirements(text):
    """(normalised name, specifier text) for each requirement of a requirements.in."""
    found = []
    for line in text.splitlines():
        line = line.split('#', 1)[0].strip()
        if not line:
            continue
        match = DIRECT.fullmatch(line)
        if not match or (match.group(2) and match.group(2)[0] not in '<>=!'):
            raise ValueError('not a requirement with a version range: ' + line)
        found.append((normalise(match.group(1)), match.group(2).strip()))
    return found


def satisfies(version, specifier):
    """True when the version meets every comma-separated clause of the specifier.
    Only >=, <=, ==, !=, > and < are understood; any other clause raises ValueError,
    so an edit to a requirements.in is never silently left unchecked."""
    have = release(version)
    for clause in filter(None, (part.strip() for part in specifier.split(','))):
        match = CLAUSE.fullmatch(clause)
        if not match:
            raise ValueError('unsupported requirement clause: ' + clause)
        operator, bound = match.group(1), tuple(int(part) for part in match.group(2).split('.'))
        width = max(len(have), len(bound))
        left, right = have + (0,) * (width - len(have)), bound + (0,) * (width - len(bound))
        holds = {'>=': left >= right, '<=': left <= right, '==': left == right,
                 '!=': left != right, '>': left > right, '<': left < right}[operator]
        if not holds:
            return False
    return True


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
        self.assertEqual(versions, {'1.45.2'})
        self.assertEqual(api['opentelemetry-semantic-conventions'], '0.66b2')
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

    def test_ui_postcss_selector_parser_stays_on_6x(self):
        # @tailwindcss/typography, its only user since Tailwind 4 (RD-157), pins 6.0.10
        # exactly; the scoped override lifts it to 6.1.5, the patched 6.x release (#562).
        packages = json.loads((ROOT / 'ui/package-lock.json').read_text())['packages']
        entries = {key: value['version'] for key, value in packages.items()
                   if key.endswith('node_modules/postcss-selector-parser')}
        self.assertTrue(entries)
        for key, version in entries.items():
            with self.subTest(entry=key):
                self.assertEqual(release(version)[0], 6, '{} resolves {}, not 6.x'.format(key, version))

    def test_ui_typography_override(self):
        # @tailwindcss/typography pins postcss-selector-parser 6.0.10 exactly; the
        # scoped override lifts it to the patched 6.x line (#562).
        manifest = json.loads((ROOT / 'ui/package.json').read_text())
        self.assertEqual(manifest.get('overrides'), TYPOGRAPHY_OVERRIDE)

    def test_python_locks_are_fully_pinned(self):
        # #524: every requirement line of both locks is `name==version`, and no package
        # is pinned twice. pins() above skips any other line, so it would not notice.
        for path in LOCKS:
            with self.subTest(lock=path):
                self.assertEqual(lock_problems((ROOT / path).read_text()), [])

    def test_lock_problems_reports_loose_lines(self):
        for label, text in (('a range', 'Jinja2>=3.1\n'),
                            ('a bare name', 'Jinja2\n'),
                            ('a marker', 'Jinja2==3.1.6; python_version < "3.9"\n'),
                            ('a hash option', 'Jinja2==3.1.6 --hash=sha256:00\n'),
                            ('a repeat', 'Jinja2==3.1.6\njinja2==3.1.6\n')):
            with self.subTest(case=label):
                self.assertTrue(lock_problems(text), label)
        self.assertEqual(lock_problems('# header\n\nJinja2==3.1.6  # note\nrequests[socks]==2.32.3\n'), [])

    def test_mcp_lock_satisfies_requirements_in(self):
        # #524: the MCP image isn't built by any suite (MCP is parked), so check the lock
        # against the direct ranges the human-maintained mcp/requirements.in states.
        lock = pins('mcp/requirements.txt')
        direct = direct_requirements((ROOT / 'mcp/requirements.in').read_text())
        self.assertTrue(direct, 'mcp/requirements.in has no requirements')
        for name, specifier in direct:
            with self.subTest(package=name, specifier=specifier):
                self.assertIn(name, lock, 'mcp/requirements.txt does not pin ' + name)
                self.assertTrue(satisfies(lock[name], specifier),
                                'mcp/requirements.txt pins {}=={}, outside {}'.format(name, lock[name], specifier))

    def test_satisfies(self):
        self.assertTrue(satisfies('2.2.0', '>=2.2,<3'))
        self.assertFalse(satisfies('3.0.0', '>=2.2,<3'))
        self.assertFalse(satisfies('2.1.9', '>=2.2,<3'))
        self.assertTrue(satisfies('2.2', '>=2.2.0'))
        self.assertTrue(satisfies('0.28.1', '>=0.28,<0.29'))
        self.assertFalse(satisfies('1.0', '!=1'))
        self.assertTrue(satisfies('1.2.3', ''))
        with self.assertRaises(ValueError):
            satisfies('1', '~=1.0')
        with self.assertRaises(ValueError):
            direct_requirements('mcp @ https://example.invalid/mcp.whl\n')


if __name__ == '__main__':
    unittest.main()
