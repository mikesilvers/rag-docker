"""Host-only check that IMPLEMENTATION.md's copy of ui/package.json is the file (#532).

IMPLEMENTATION.md embeds ui/package.json under `### ui/package.json`.
Dependabot updates the file but can't update the copy, so a UI dependency
bump that misses the copy fails here (it made #525's first head NOT READY).
The check compares the copy with the file exactly and names no package or
version, so a bump that updates both needs no test change.

Standard library only; no Docker or stack needed.
Run: python3 scripts/tests/test_ui_dependencies.py
"""
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
HEADING = '### ui/package.json\n'
FENCE = '```'


def embedded_copy(document):
    """The text of the first ```json block after HEADING, without its fences."""
    opening = document.index(FENCE + 'json', document.index(HEADING) + len(HEADING))
    start = document.index('\n', opening) + 1
    end = document.index('\n' + FENCE + '\n', start)
    return document[start:end]


class EmbeddedPackageJsonTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.document = (ROOT / 'IMPLEMENTATION.md').read_text()

    def test_one_embedded_copy(self):
        found = self.document.count(HEADING)
        self.assertEqual(found, 1, 'IMPLEMENTATION.md should hold exactly one "### ui/package.json" copy, '
                                   'found {}'.format(found))

    def test_embedded_copy_matches_file(self):
        self.assertEqual(embedded_copy(self.document), (ROOT / 'ui/package.json').read_text().rstrip('\n'),
                         'the copy under "### ui/package.json" in IMPLEMENTATION.md differs from '
                         'ui/package.json; update the copy with the file')


if __name__ == '__main__':
    unittest.main()
