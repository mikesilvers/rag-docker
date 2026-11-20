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

BuiltCssTests copies ui/ into a scratch directory and, in throwaway, named
containers of the image ui/Dockerfile builds with (its `builder` stage), installs
what ui/package-lock.json pins (`npm ci --ignore-scripts`), runs `vite build`
and reads the stylesheet. A step that times out has its container force-removed
(`docker rm -f`) rather than left building. Nothing of the UI toolchain runs on
the host, so a lock file edited by a fork is installed only inside the
container. It needs the docker CLI (and the network unless the image and the npm
cache are present); it is skipped, with a message, only when the docker CLI is
missing, and a docker or build failure fails it. SourceTests read
ui/src/index.css, NavBar.tsx and ui/Dockerfile, and check that the builder image
is an image reference with a tag or a digest (a `--platform` flag, a digest pin
and a registry with a port are accepted; a token starting with `-` is refused
before it reaches `docker run`); they need nothing. ContainerRunTests check the
timeout clean-up with a stub in place of docker. Standard library only; no stack
needed, and nothing under the checkout is changed.

Run: python3 scripts/tests/test_tailwind_output.py
"""
from pathlib import Path
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / 'ui'

# v3's `content` globs (ui/tailwind.config.js before RD-157), relative to ui/src/.
V3_SOURCES = ['../index.html', './**/*.{js,ts,jsx,tsx}']


# An image reference: an optional registry `host[:port]/`, lowercase path components, then a tag,
# a digest or both. It starts with a letter or digit, so it can never be read as a docker option.
_LABEL = r'[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?'
_COMPONENT = r'[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*'
IMAGE_REFERENCE = re.compile(
    r'(?:{label}(?:\.{label})*(?::[0-9]+)?/)?{component}(?:/{component})*(?=[:@])'
    r'(?::[A-Za-z0-9_][A-Za-z0-9_.-]{{0,127}})?(?:@sha256:[0-9a-f]{{64}})?'.format(label=_LABEL, component=_COMPONENT))


def builder_image(dockerfile=None):
    """The image ui/Dockerfile builds in (`FROM [--platform=...] <image> AS builder`), so the test follows it.

    The token is checked against IMAGE_REFERENCE before anything uses it. `dockerfile` is the text to
    read; it defaults to ui/Dockerfile.
    """
    text = (UI / 'Dockerfile').read_text() if dockerfile is None else dockerfile
    found = re.search(r'^FROM\s+(?:--platform=\S+\s+)?(\S+)\s+AS\s+builder\s*$', text, re.M | re.I)
    if not found:
        raise AssertionError('ui/Dockerfile has no `FROM <image> AS builder` line')
    image = found.group(1)
    if not IMAGE_REFERENCE.fullmatch(image):
        raise AssertionError('the builder image {!r} is not an image reference with a tag or a digest'.format(image))
    return image


class SourceTests(unittest.TestCase):
    def test_builder_image_is_read_from_the_dockerfile(self):
        image = builder_image()
        self.assertIn(image, (UI / 'Dockerfile').read_text())

    def test_builder_image_reference_forms(self):
        digest = 'sha256:' + 'a' * 64
        for text, expected in (
            ('FROM node:26-alpine AS builder', 'node:26-alpine'),
            ('FROM node:26-alpine@{} AS builder'.format(digest), 'node:26-alpine@' + digest),
            ('FROM node@{} AS builder'.format(digest), 'node@' + digest),
            ('FROM registry.example:5000/team/node:26-alpine AS builder', 'registry.example:5000/team/node:26-alpine'),
            ('FROM docker.io/library/node:26-alpine AS builder', 'docker.io/library/node:26-alpine'),
            ('FROM --platform=linux/amd64 node:26-alpine AS builder', 'node:26-alpine'),
            ('from --platform=$BUILDPLATFORM node:26-alpine as builder', 'node:26-alpine'),
        ):
            with self.subTest(text=text):
                self.assertEqual(builder_image(text + '\n'), expected)

    def test_builder_image_refuses_what_is_not_a_reference(self):
        for text in (
            'FROM -v AS builder',
            'FROM --rm AS builder',
            'FROM --platform=linux/amd64 -it AS builder',
            'FROM node AS builder',  # no tag or digest
            'FROM Node:26 AS builder',  # upper case
            'FROM node:26@sha256:abc AS builder',  # short digest
            'FROM node:-x AS builder',  # bad tag
        ):
            with self.subTest(text=text):
                with self.assertRaises(AssertionError):
                    builder_image(text + '\n')

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


def run_container(options, image, command, cwd, timeout):
    """Run `command` in a new, named container of `image`, removed when it ends or times out."""
    name = 'rag-tailwind-' + uuid.uuid4().hex[:12]
    argv = ['docker', 'run', '--rm', '--name', name] + options + [image] + command
    try:
        result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        # A timeout kills only the docker CLI; the container would keep building, so remove it. The
        # timeout can also fire while the image is still being pulled, before any container exists.
        try:
            removal = subprocess.run(['docker', 'rm', '-f', name], capture_output=True, text=True, timeout=60)
            if removal.returncode == 0:
                outcome = 'removed the container'
            elif 'no such container' in (removal.stderr or '').lower():
                outcome = 'found no container to remove'
            else:
                outcome = 'failed'
        except (OSError, subprocess.SubprocessError):
            outcome = 'failed'
        raise AssertionError('{} timed out after {} s; docker rm -f {} {}'.format(
            ' '.join(command), timeout, name, outcome)) from None
    if result.returncode != 0:
        raise AssertionError('{} exited {}:\n{}'.format(' '.join(argv), result.returncode,
                                                       (result.stdout + result.stderr)[-3000:]))


class ContainerRunTests(unittest.TestCase):
    def test_a_timed_out_build_removes_its_container(self):
        calls = []

        def fake(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(args, kwargs['timeout'])
            return subprocess.CompletedProcess(args, 0, '', '')

        with mock.patch.object(subprocess, 'run', side_effect=fake):
            with self.assertRaisesRegex(AssertionError, 'timed out'):
                run_container(['-w', '/app'], 'node:26-alpine', ['npm', 'ci'], '.', 300)
        name = calls[0][calls[0].index('--name') + 1]
        self.assertTrue(name.startswith('rag-tailwind-'), name)
        self.assertEqual(calls[1], ['docker', 'rm', '-f', name])

    def _timed_out_message(self, removal):
        calls = []

        def fake(args, **kwargs):
            calls.append(args)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(args, kwargs['timeout'])
            return removal(args)

        with mock.patch.object(subprocess, 'run', side_effect=fake):
            with self.assertRaisesRegex(AssertionError, 'timed out') as caught:
                run_container([], 'node:26-alpine', ['npm', 'ci'], '.', 300)
        return str(caught.exception)

    def test_a_timeout_before_the_container_exists_says_there_is_nothing_to_remove(self):
        message = self._timed_out_message(lambda args: subprocess.CompletedProcess(
            args, 1, '', 'Error response from daemon: No such container: ' + args[-1]))
        self.assertIn('no container to remove', message)
        self.assertNotIn('failed', message)

    def test_a_failed_removal_still_says_failed(self):
        message = self._timed_out_message(lambda args: subprocess.CompletedProcess(
            args, 1, '', 'Error response from daemon: permission denied'))
        self.assertTrue(message.endswith('failed'), message)

    def test_each_run_gets_its_own_name(self):
        calls = []

        def fake(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, '', '')

        with mock.patch.object(subprocess, 'run', side_effect=fake):
            run_container([], 'node:26-alpine', ['npm', 'ci'], '.', 300)
            run_container([], 'node:26-alpine', ['npx', 'vite', 'build'], '.', 180)
        for argv in calls:
            self.assertEqual(argv[:4], ['docker', 'run', '--rm', '--name'])
        self.assertNotEqual(calls[0][4], calls[1][4])


class BuiltCssTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which('docker'):
            raise unittest.SkipTest('the docker CLI is needed to build the UI CSS in a container')
        image = builder_image()
        scratch = Path(tempfile.mkdtemp(prefix='rag-tailwind-'))
        cls.addClassCleanup(shutil.rmtree, scratch, ignore_errors=True)
        ui = scratch / 'ui'
        shutil.copytree(UI, ui, ignore=shutil.ignore_patterns('node_modules', 'dist'))
        # One file the globs match and one they do not, each with a class nothing else uses.
        (ui / 'src/zz_sentinel_in.jsx').write_text("export const sentinel = 'bg-fuchsia-950'\n")
        (ui / 'src/zz_sentinel_out.md').write_text('bg-lime-950\n')
        # The build runs in a container, as the image build does, not on the host. The invoking
        # user owns what it writes (so the clean-up works), and npm gets a cache under /tmp.
        options = []
        if hasattr(os, 'getuid'):
            options += ['--user', '{}:{}'.format(os.getuid(), os.getgid())]
        options += ['-e', 'HOME=/tmp', '-e', 'npm_config_cache=/tmp/.npm', '-v', '{}:/app'.format(ui), '-w', '/app']
        run_container(options, image, ['npm', 'ci', '--ignore-scripts', '--no-audit', '--no-fund'], ui, 300)
        run_container(options, image, ['npx', '--no-install', 'vite', 'build'], ui, 180)
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
