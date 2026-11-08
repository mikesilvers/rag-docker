"""Host-only check of the README's 1.1.0 → 1.2.0 backup and rollback commands (#679).

Reads the two command blocks of "Upgrading from 1.1.0 to 1.2.0" in README.md and
checks that the helper image is pinned by version and digest and that git ignores
`backups/`. Then it runs both blocks as written, under bash (and zsh when present),
with a stub `docker` and `git` first on PATH. The stub's named volumes are folders,
and `docker run` runs the container's command with the host's own tar and find, so
the checks the blocks make before they write are exercised for real:

- the backup skips a volume that doesn't exist (a wrong prefix) instead of creating
  it, and reports each archive's entry count;
- the rollback checks every archive and every volume before it empties any, and
  empties nothing, nor starts the stack, if one check fails.

Standard library only; no Docker or stack needed.
Run: python3 scripts/tests/test_upgrade_backup.py
"""
from pathlib import Path
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
PIN = re.compile(r'\balpine:(\d+\.\d+\.\d+)@sha256:[0-9a-f]{64}(?![0-9a-f])')
VOLUMES = ('weaviate_data', 'ingest_uploads', 'rag_sources')
PREFIX = 'rdtest'

STUB_DOCKER = r'''
import json, os, re, subprocess, sys
stub = os.environ['STUB_DIR']
vols = os.path.join(stub, 'vols')

def log(entry):
    with open(os.path.join(stub, 'calls'), 'a') as f:
        f.write(json.dumps(entry) + '\n')

args = sys.argv[1:]
log({'argv': args})
if args[:2] == ['volume', 'inspect']:
    if os.path.isdir(os.path.join(vols, args[2])):
        print('[{"Name": "%s"}]' % args[2])
        sys.exit(0)
    sys.stderr.write('Error response from daemon: get %s: no such volume\n' % args[2])
    sys.exit(1)
if args[:1] == ['run']:
    rest, mounts, i = args[1:], {}, 0
    while rest[i].startswith('-'):
        if rest[i] == '-v':
            spec = rest[i + 1].split(':')
            source, target = spec[0], spec[1]
            if not source.startswith('/'):
                path = os.path.join(vols, source)
                if not os.path.isdir(path):
                    os.makedirs(path)
                    log({'created': source})
                source = path
            mounts[target] = source
            i += 2
        else:
            i += 1
    command = rest[i + 1:]
    pattern = re.compile(r'(?<![^\s"\'])(' + '|'.join(re.escape(t) for t in mounts) + r')(?=[/\s"\']|$)')
    command = [pattern.sub(lambda m: mounts[m.group(1)], part) for part in command]
    sys.exit(subprocess.call(command))
sys.exit(0)
'''

STUB_GIT = r'''
import json, os, sys
with open(os.path.join(os.environ['STUB_DIR'], 'calls'), 'a') as f:
    f.write(json.dumps({'git': sys.argv[1:]}) + '\n')
'''


def section():
    text = (ROOT / 'README.md').read_text()
    start = text.index('\n### Upgrading from 1.1.0 to 1.2.0\n')
    end = text.index('\n### ', start + 1)
    return text[start:end]


def blocks():
    """The fenced bash blocks of the section, dedented."""
    found = []
    for indent, body in re.findall(r'^( *)```bash\n(.*?)^\1```$', section(), re.S | re.M):
        found.append('\n'.join(line[len(indent):] for line in body.splitlines()) + '\n')
    return found


def block(marker):
    matches = [b for b in blocks() if marker in b]
    assert len(matches) == 1, '{} blocks of the section contain {!r}'.format(len(matches), marker)
    return matches[0]


def backup_block():
    return block('tar czf')


def rollback_block():
    return block('find /data -mindepth 1 -delete')


def tree(path):
    """{relative path: file bytes, or None for a folder} under path."""
    result = {}
    for folder, dirs, files in os.walk(path):
        for name in dirs:
            result[os.path.relpath(os.path.join(folder, name), path)] = None
        for name in files:
            full = os.path.join(folder, name)
            with open(full, 'rb') as f:
                result[os.path.relpath(full, path)] = f.read()
    return result


class PinAndIgnoreTests(unittest.TestCase):
    def test_every_docker_run_uses_the_same_pinned_alpine(self):
        refs = set()
        for name, text in (('backup', backup_block()), ('rollback', rollback_block())):
            runs = [line for line in text.splitlines() if 'docker run' in line]
            self.assertTrue(runs, name + ' block runs no container')
            for line in runs:
                match = PIN.search(line)
                self.assertTrue(match, name + ': not pinned by version and digest: ' + line)
                refs.add(match.group(0))
        self.assertEqual(len(refs), 1, refs)

    def test_no_unversioned_alpine_in_the_section(self):
        for match in re.finditer(r'\balpine\b', section()):
            following = section()[match.end():match.end() + 2]
            self.assertRegex(following, r'^(:\d| \d)', 'unversioned alpine at: ' +
                             section()[max(0, match.start() - 40):match.end() + 20])

    def test_gitignore_lists_backups(self):
        lines = (ROOT / '.gitignore').read_text().splitlines()
        self.assertIn('backups/', lines)

    def test_git_ignores_a_backup_file(self):
        if not shutil.which('git'):
            self.skipTest('git is not on PATH')
        inside = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--is-inside-work-tree'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        if inside.stdout.strip() != 'true':
            self.skipTest(str(ROOT) + ' is not a git work tree')
        result = subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '-q', 'backups/weaviate_data-v1.1.0.tgz'])
        self.assertEqual(result.returncode, 0, 'git does not ignore backups/weaviate_data-v1.1.0.tgz')


class CommandTests(unittest.TestCase):
    """Runs the README's own blocks against the stub."""

    shell = 'bash'

    def setUp(self):
        if not shutil.which(self.shell):
            self.skipTest(self.shell + ' is not on PATH')
        self.dir = Path(tempfile.mkdtemp(prefix='rd679-'))
        self.addCleanup(shutil.rmtree, str(self.dir))
        bin_dir = self.dir / 'bin'
        bin_dir.mkdir()
        for name, source in (('docker', STUB_DOCKER), ('git', STUB_GIT)):
            stub = bin_dir / name
            stub.write_text('#!' + sys.executable + '\n' + source)
            stub.chmod(0o755)
        (self.dir / 'vols').mkdir()
        self.work = self.dir / 'work'
        self.work.mkdir()
        self.env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'], STUB_DIR=str(self.dir))
        rng = random.Random(679)
        for v in VOLUMES:
            volume = self.volume(v)
            (volume / 'sub').mkdir(parents=True)
            (volume / 'data.bin').write_bytes(bytes(rng.getrandbits(8) for _ in range(40000)))
            (volume / '.hidden').write_text('dot file of ' + v)
            (volume / 'sub' / 'note.txt').write_text('note of ' + v)

    def volume(self, name, prefix=PREFIX):
        return self.dir / 'vols' / (prefix + '_' + name)

    def run_block(self, text, prefix=PREFIX):
        script, count = re.subn(r'^P=rag-docker$', 'P=' + prefix, text, flags=re.M)
        self.assertEqual(count, 1, 'the block sets P once')
        calls = self.dir / 'calls'
        if calls.exists():
            calls.unlink()
        result = subprocess.run([self.shell, '-c', script], cwd=str(self.work), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        log = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        return result, log

    @staticmethod
    def docker_runs(log):
        return [c for c in log if c.get('argv', [None])[0] == 'run']

    @staticmethod
    def created(log):
        return [c['created'] for c in log if 'created' in c]

    @staticmethod
    def compose_up(log):
        return [c for c in log if c.get('argv', [])[:2] == ['compose', 'up']]

    def backup(self):
        result, log = self.run_block(backup_block())
        self.assertEqual(len(self.docker_runs(log)), 3, result.stderr)
        return result, log

    def snapshot(self):
        return {v: tree(str(self.volume(v))) for v in VOLUMES}

    def change_volumes(self):
        for v in VOLUMES:
            (self.volume(v) / 'sub' / 'note.txt').unlink()
            (self.volume(v) / 'added-after-backup').write_text('new')

    def assert_nothing_restored(self, result, log, before):
        self.assertEqual(self.snapshot(), before, 'a volume changed although a check failed')
        self.assertEqual(self.docker_runs(log), [])
        self.assertEqual(self.compose_up(log), [])
        self.assertIn('Nothing was emptied or restored.', result.stdout)

    # Backup

    def test_backup_with_the_right_prefix(self):
        result, log = self.backup()
        self.assertEqual(self.created(log), [])
        for v in VOLUMES:
            self.assertTrue((self.work / 'backups' / (v + '-v1.1.0.tgz')).is_file(), v)
            match = re.search(r'^' + v + r': (\d+) entries$', result.stdout, re.M)
            self.assertTrue(match, result.stdout + result.stderr)
            self.assertGreater(int(match.group(1)), 1)

    def test_backup_of_an_empty_volume_counts_one_entry(self):
        shutil.rmtree(str(self.volume('ingest_uploads')))
        self.volume('ingest_uploads').mkdir()
        result, _log = self.backup()
        self.assertRegex(result.stdout, r'(?m)^ingest_uploads: 1 entries$')

    def test_backup_with_a_wrong_prefix_creates_and_writes_nothing(self):
        result, log = self.run_block(backup_block(), prefix='wrongprefix')
        self.assertEqual(self.docker_runs(log), [])
        self.assertEqual(self.created(log), [])
        self.assertFalse(any(name.startswith('wrongprefix') for name in os.listdir(str(self.dir / 'vols'))))
        self.assertNotIn('entries', result.stdout)
        self.assertIn('no such volume', result.stderr)

    # Rollback

    def test_rollback_restores_the_backup(self):
        self.backup()
        before = self.snapshot()
        self.change_volumes()
        result, log = self.run_block(rollback_block())
        self.assertEqual(self.snapshot(), before, result.stdout + result.stderr)
        for v in VOLUMES:
            self.assertIn(v + ': OK', result.stdout)
        self.assertEqual(len(self.docker_runs(log)), 3)
        self.assertEqual(len(self.compose_up(log)), 1)
        self.assertIn({'git': ['checkout', 'v1.1.0']}, log)
        self.assertEqual(self.created(log), [])

    def test_rollback_with_a_corrupt_archive_empties_nothing(self):
        self.backup()
        self.change_volumes()
        before = self.snapshot()
        archive = self.work / 'backups' / 'ingest_uploads-v1.1.0.tgz'
        data = bytearray(archive.read_bytes())
        data[len(data) // 2] ^= 0xff
        archive.write_bytes(bytes(data))
        result, log = self.run_block(rollback_block())
        self.assert_nothing_restored(result, log, before)
        self.assertIn('ingest_uploads: FAILED', result.stdout)
        self.assertIn('weaviate_data: OK', result.stdout)

    def test_rollback_with_a_missing_archive_empties_nothing(self):
        self.backup()
        self.change_volumes()
        before = self.snapshot()
        (self.work / 'backups' / 'rag_sources-v1.1.0.tgz').unlink()
        result, log = self.run_block(rollback_block())
        self.assert_nothing_restored(result, log, before)
        self.assertIn('rag_sources: FAILED', result.stdout)

    def test_rollback_with_a_wrong_prefix_empties_and_creates_nothing(self):
        self.backup()
        self.change_volumes()
        before = self.snapshot()
        result, log = self.run_block(rollback_block(), prefix='wrongprefix')
        self.assert_nothing_restored(result, log, before)
        self.assertEqual(self.created(log), [])
        for v in VOLUMES:
            self.assertIn(v + ': FAILED', result.stdout)
        self.assertIn('no such volume', result.stderr)


class ZshCommandTests(CommandTests):
    """The main cases again in zsh, macOS's default shell, where operators paste these."""

    shell = 'zsh'

    test_backup_of_an_empty_volume_counts_one_entry = None
    test_backup_with_a_wrong_prefix_creates_and_writes_nothing = None
    test_rollback_with_a_missing_archive_empties_nothing = None
    test_rollback_with_a_wrong_prefix_empties_and_creates_nothing = None


if __name__ == '__main__':
    unittest.main()
