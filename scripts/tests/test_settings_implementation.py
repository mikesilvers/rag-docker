"""Host-side source-copy checks; no API runtime dependencies."""
from pathlib import Path
import unittest

class ImplementationTests(unittest.TestCase):
    def test_embedded_sources_match_runtime(self):
        root = Path(__file__).resolve().parents[2]
        text = (root / 'IMPLEMENTATION.md').read_text()
        for name in ('api/services/settings_store.py', 'api/services/ingest_config.py',
                     'api/services/retrieval_config.py', 'scripts/verify/07_settings.sh',
                     'scripts/verify/README.md', 'scripts/tests/test_settings_persistence.py',
                     'scripts/verify/settings_validation.py', 'api/services/importer.py'):
            with self.subTest(file=name):
                fence = '````' if name.endswith('.md') else '```'
                language = 'markdown' if name.endswith('.md') else 'bash' if name.endswith('.sh') else 'python'
                header = f'### {name}\n\n{fence}{language}\n'
                start = text.index(header) + len(header)
                end = text.index('\n' + fence + '\n', start)
                self.assertEqual(text[start:end], (root / name).read_text().rstrip('\n'))


if __name__ == '__main__':
    unittest.main()
