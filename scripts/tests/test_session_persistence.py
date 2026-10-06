"""Exact embedded-source contract; behavioral cases run through12_persistence.sh."""
import unittest
from pathlib import Path

class DocumentationTests(unittest.TestCase):
    def test_changed_embedded_sources_match_runtime(self):
        root=Path(__file__).resolve().parents[2];text=(root/'IMPLEMENTATION.md').read_text()
        names=['api/services/goldstandard.py', 'api/services/importer.py', 'api/routers/goldstandard.py', 'ui/src/api/client.ts', 'ui/src/pages/HealthPage.tsx', 'scripts/verify/04_goldstandard.sh', 'scripts/verify/12_persistence.sh', 'scripts/verify/session_persistence.py', 'scripts/verify/session_persistence_cases.py', 'scripts/verify/compose_target.py', 'scripts/verify/README.md', 'scripts/verify/browser/ui_criteria.js']
        for name in names:
            lang='typescript' if name.endswith(('.ts','.tsx')) else 'bash' if name.endswith('.sh') else 'markdown' if name.endswith('.md') else 'javascript' if name.endswith('.js') else 'python';fence='````' if name.endswith('.md') else '```';header='### '+name+'\n\n'+fence+lang+'\n'
            a=text.index(header)+len(header);b=text.index('\n'+fence+'\n',a)
            with self.subTest(file=name):self.assertEqual(text[a:b],(root/name).read_text().rstrip('\n'))

if __name__=='__main__':unittest.main()
