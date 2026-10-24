"""Default/optional infrastructure inventory acceptance without Docker."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location('inventory', ROOT/'scripts/verify/service_inventory.py')
INVENTORY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INVENTORY)


class InventoryTests(unittest.TestCase):
    def fixture(self, telemetry=False):
        names = sorted(INVENTORY.BASE | (INVENTORY.TELEMETRY if telemetry else frozenset()))
        config = {'name':'rag-verify', 'services':{name:{} for name in names}}
        return config,names

    def test_default_exact_five(self):
        config,names = self.fixture()
        self.assertEqual(INVENTORY.check(config,names,names),[])

    def test_guarded_telemetry_exact_seven(self):
        config,names = self.fixture(True)
        self.assertEqual(INVENTORY.check(config,names,names,'1','rag-verify','telemetry'),[])

    def test_same_count_replacement_does_not_pass(self):
        for mode in (False,True):
            with self.subTest(telemetry=mode):
                config,names = self.fixture(mode)
                replaced = ['unexpected' if n=='api' else n for n in names]
                self.assertTrue(INVENTORY.check(config,replaced,replaced,str(int(mode)),'rag-verify','telemetry'))

    def test_missing_stopped_duplicate_or_extra_service_is_refused(self):
        config,names = self.fixture(True)
        for running,all_services in [(names[:-1],names),(names,names+['orphan']),
                                     (names+['api'],names+['api']), (names[:-1],names[:-1])]:
            with self.subTest(running=running,all_services=all_services):
                self.assertTrue(INVENTORY.check(config,running,all_services,'1','rag-verify','telemetry'))

    def test_default_cannot_silently_accept_optional_services(self):
        config,names = self.fixture(True)
        self.assertTrue(INVENTORY.check(config,names,names))

    def test_mode_requires_project_and_profile(self):
        config,names = self.fixture(True)
        for mode,project,profiles in [('1','rag-docker','telemetry'),('1','rag-verify',''),('anything','rag-verify','telemetry')]:
            with self.subTest(mode=mode,project=project,profiles=profiles):
                self.assertTrue(INVENTORY.check(config,names,names,mode,project,profiles))

    def test_unexpected_configured_service_refused_even_when_not_running(self):
        config,names = self.fixture(True)
        config['services']['surprise'] = {}
        self.assertTrue(INVENTORY.check(config,names,names,'1','rag-verify','telemetry'))


if __name__ == '__main__':
    unittest.main()
