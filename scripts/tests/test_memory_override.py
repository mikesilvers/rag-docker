"""Host-only check that suite 01's memory override always flips the status (#536).

Suite 01 raised the recommendation to a fixed 13 GB, which reports 'ok' on any
host giving Docker 12.35 GB or more. This runs the real system_info.memory_info
with the /proc/meminfo reading replaced, at allocations below and above that,
with the override scripts/verify/memory_override.py derives from the reported
allocation. Standard library only; no Docker or stack needed.

Run: python3 scripts/tests/test_memory_override.py
"""
from pathlib import Path
from unittest import mock
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def load(name, path):
    # By file path: importing the api `services` package would need the API's dependencies.
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


system_info = load('system_info', 'api/services/system_info.py')
memory_override = load('memory_override', 'scripts/verify/memory_override.py')

# Docker allocations in GB, below and above the 12.35 GB a fixed 13 could flip.
ALLOCATIONS = (4, 7.75, 11.67, 12.35, 12.4, 13, 15.6, 16, 31.3, 64, 512)


class OverrideTests(unittest.TestCase):
    def test_the_override_reports_below_recommended_with_a_note(self):
        for true_gb in ALLOCATIONS:
            # The second figure is rounded away in the report but still counts in the comparison.
            for allocated in (true_gb, true_gb + 0.0049):
                with self.subTest(allocated=allocated), \
                        mock.patch.object(system_info, '_read_mem_total_gb', return_value=allocated):
                    reported = system_info.memory_info(12.0)['allocated_gb']
                    override = memory_override.override_gb(reported)
                    self.assertIsInstance(override, int)
                    self.assertGreater(override, reported)
                    info = system_info.memory_info(float(override))
                    self.assertEqual(info['status'], 'below_recommended',
                                     'override {} at {} GB'.format(override, allocated))
                    self.assertIn('recommended', info.get('note', ''))

    def test_no_override_without_a_usable_allocation(self):
        for allocated in (None, 0, -1, True, '16', float('nan'), float('inf')):
            with self.subTest(allocated=allocated):
                self.assertIsNone(memory_override.override_gb(allocated))


class MainTests(unittest.TestCase):
    def run_main(self, health):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'health.json'
            path.write_text(json.dumps(health))
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = memory_override.main(['memory_override.py', str(path)])
        return code, out.getvalue()

    def test_prints_the_override_for_the_reported_allocation(self):
        health = {'resources': {'memory': {'status': 'ok', 'allocated_gb': 15.6,
                                           'recommended_minimum_gb': 12.0}}}
        self.assertEqual(self.run_main(health), (0, '{}\n'.format(memory_override.override_gb(15.6))))

    def test_prints_nothing_and_fails_without_an_allocation(self):
        unknown = {'resources': {'memory': {'status': 'unknown', 'allocated_gb': None,
                                            'recommended_minimum_gb': 12.0}}}
        for health in (unknown, {'status': 'ok'}):
            with self.subTest(health=health):
                self.assertEqual(self.run_main(health), (1, ''))


class EmbeddedCopyTests(unittest.TestCase):
    def test_embedded_copies_match(self):
        text = (ROOT / 'IMPLEMENTATION.md').read_text()
        for name in ('scripts/verify/memory_override.py', 'scripts/tests/test_memory_override.py'):
            with self.subTest(file=name):
                header = '### {}\n\n'.format(name)
                start = text.index(header) + len(header)
                start = text.index('```python\n', start) + len('```python\n')
                end = text.index('\n```\n', start)
                self.assertEqual(text[start:end], (ROOT / name).read_text().rstrip('\n'))


if __name__ == '__main__':
    unittest.main()
