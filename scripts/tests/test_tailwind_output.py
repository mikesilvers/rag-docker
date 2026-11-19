"""Host-only guard on the Tailwind CSS 4 output of the web UI (#773).

The Tailwind 4 migration (RD-157, #763) relies on four things in the built
stylesheet, and nothing else checked them, so a later toolchain change could
drop one silently:

  * the `prose` / `prose-sm` rules of the typography plugin;
  * `relative` for the NavBar dropdown wrapper (Tailwind 4's scanner missed the
    old `relative${...}` form);
  * the upgrade guide's `hover` variant, which applies on every device, with no
    `@media (hover: hover)` wrapping;
  * the `@source` globs, which are the files v3's `content` globs named, with
    automatic source detection off.

BuiltCssTests copies ui/ into a scratch directory, installs what
ui/package-lock.json pins (`npm ci`), runs `vite build` and reads the stylesheet.
It needs node and npm (and the network unless the npm cache holds the packages);
it is skipped, with a message, only when node or npm is missing. SourceTests read
ui/src/index.css and NavBar.tsx and need nothing. Standard library only; no
Docker or stack needed, and nothing under the checkout is changed.

Run: python3 scripts/tests/test_tailwind_output.py
"""
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / 'ui'

# v3's `content` globs (ui/tailwind.config.js before RD-157), relative to ui/src/.
V3_SOURCES = ['../index.html', './**/*.{js,ts,jsx,tsx}']


class SourceTests(unittest.TestCase):
    def test_index_css_configuration(self):
        css = (UI / 'src/index.css').read_text()
        for line, why in (
            ("@import 'tailwindcss' source(none);", 'automatic source detection is off, so only the @source files are scanned'),
            ("@plugin '@tailwindcss/typography';", 'the typography plugin gives the `prose` classes their rules'),
            ('@custom-variant hover (&:hover);', 'hover applies on every device, as in Tailwind 3'),
        ):
            with self.subTest(line=line):
                self.assertIn(line, css, why)
        self.assertEqual(re.findall(r"^@source '([^']*)';", css, re.M), V3_SOURCES,
                         "the @source lines must be exactly v3's content globs")

    def test_navbar_writes_relative_as_a_whole_token(self):
        text = (UI / 'src/components/NavBar.tsx').read_text()
        self.assertRegex(text, r"""['"]relative['"]""", 'the dropdown wrapper writes `relative` as a whole token')
        self.assertNotIn('`relative${', text, "Tailwind 4's scanner misses `relative` built inside a template literal")


def run(command, cwd, timeout):
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise AssertionError('{} exited {}:\n{}'.format(' '.join(command), result.returncode,
                                                       (result.stdout + result.stderr)[-3000:]))


class BuiltCssTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not (shutil.which('node') and shutil.which('npm')):
            raise unittest.SkipTest('node and npm are needed to build the UI CSS')
        scratch = Path(tempfile.mkdtemp(prefix='rag-tailwind-'))
        cls.addClassCleanup(shutil.rmtree, scratch, ignore_errors=True)
        ui = scratch / 'ui'
        shutil.copytree(UI, ui, ignore=shutil.ignore_patterns('node_modules', 'dist'))
        # One file the globs match and one they do not, each with a class nothing else uses.
        (ui / 'src/zz_sentinel_in.jsx').write_text("export const sentinel = 'bg-fuchsia-950'\n")
        (ui / 'src/zz_sentinel_out.md').write_text('bg-lime-950\n')
        run(['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], ui, 300)
        run(['npx', '--no-install', 'vite', 'build'], ui, 180)
        sheets = list((ui / 'dist/assets').glob('*.css'))
        assert len(sheets) == 1, 'expected one built stylesheet, found {}'.format(sheets)
        # Whitespace out, so the checks don't depend on the minifier's spacing.
        cls.css = re.sub(r'\s+', '', sheets[0].read_text())

    def test_prose_rules(self):
        for rule in ('.prose{', '.prose-sm{', '.prose:where(p)'):
            with self.subTest(rule=rule):
                self.assertIn(rule, self.css, 'the typography plugin or the prose classes are gone from the built CSS')

    def test_relative_utility(self):
        self.assertIn('.relative{position:relative}', self.css, 'the NavBar dropdown wrapper lost its `relative` rule')

    def test_hover_applies_on_every_device(self):
        self.assertIn('.hover\\:bg-gray-100:hover{', self.css, 'a hover class the sources use has no :hover rule')
        self.assertNotIn('@media(hover:hover)', self.css, 'hover styles are wrapped in @media (hover: hover) again')

    def test_source_globs(self):
        self.assertIn('.bg-fuchsia-950{', self.css, 'a .jsx file under src/ is no longer scanned')
        self.assertNotIn('.bg-lime-950{', self.css, 'a file outside the @source globs was scanned: automatic detection is on')


if __name__ == '__main__':
    unittest.main()
