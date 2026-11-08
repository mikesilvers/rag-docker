"""Exact embedded sources; behavior runs through registered14_reindex.sh."""
import unittest
from pathlib import Path
class DocumentationTests(unittest.TestCase):
    def test_changed_sources_match_runtime(self):
        root=Path(__file__).resolve().parents[2];text=(root/'IMPLEMENTATION.md').read_text()
        names=['api/services/tuning.py', 'scripts/verify/05_transfer.sh', 'scripts/verify/14_reindex.sh', 'scripts/verify/reindex_cases.py', 'scripts/verify/reindex.py', 'scripts/verify/compose_target.py', 'scripts/verify/README.md', 'api/main.py', 'api/services/weaviate_client.py', 'api/services/ingest_pipeline.py', 'api/services/importer.py', 'api/services/collection_writes.py', 'api/services/collection_recovery.py', 'scripts/tests/test_collection_writes.py', 'scripts/verify/reindex_verifier_cases.py']
        for name in names:
            lang='bash' if name.endswith('.sh') else 'markdown' if name.endswith('.md') else 'python';fence='````' if name.endswith('.md') else '```';header='### '+name+'\n\n'+fence+lang+'\n';a=text.index(header)+len(header);b=text.index('\n'+fence+'\n',a)
            with self.subTest(file=name):self.assertEqual(text[a:b],(root/name).read_text().rstrip('\n'))


if __name__ == "__main__":
    unittest.main()
