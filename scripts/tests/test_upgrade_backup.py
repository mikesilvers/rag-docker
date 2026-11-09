"""Host-only check of the README's 1.1.0 → 1.2.0 backup and rollback commands (#679).

Reads the command blocks of "Upgrading from 1.1.0 to 1.2.0" in README.md and checks
that the helper image is pinned by version and digest, that no block holds a `#`
comment line (interactive zsh, macOS's default shell, doesn't treat `#` as a comment
unless `interactivecomments` is set), and that git ignores `backups/`. Then it runs
the blocks as written, under bash, `zsh -c` and interactive `zsh -i` with comments
off, with stub `docker`, `git` and `curl` first on PATH and no rc files read. The
stub's named volumes are folders, and `docker run` runs the container's command
with the host's own tar and find, after mapping the mounts to the test's temp
directory; it refuses any other absolute path. So the checks the blocks make before
they write are exercised for real:

- the backup skips a volume that doesn't exist (a wrong prefix) instead of creating
  it, and reports each archive's entry count only for a volume that exists;
- the rollback checks every archive and every volume before it empties any, and
  empties nothing, nor starts the stack, if one check fails; a failed restore stops
  before the stack is started.

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
SHELL_ERRORS = re.compile(r'\b(zsh|bash)(: line \d+)?: |command not found|no matches found|unmatched|parse error')
REFUSED = 97

STUB_DOCKER = r'''
import json, os, re, subprocess, sys
stub = os.path.realpath(os.environ['STUB_DIR'])
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
    rest, mounts, names, i = args[1:], {}, [], 0
    while rest[i].startswith('-'):
        if rest[i] == '-v':
            spec = rest[i + 1].split(':')
            source, target = spec[0], spec[1]
            if not source.startswith('/'):
                names.append(source)
                path = os.path.join(vols, source)
                if not os.path.isdir(path):
                    os.makedirs(path)
                    log({'created': source})
                source = path
            mounts[target] = os.path.realpath(source)
            i += 2
        else:
            i += 1
    command = rest[i + 1:]
    if mounts:
        pattern = re.compile(r'(?<![^\s"\'])(' + '|'.join(re.escape(t) for t in mounts) + r')(?=[/\s"\']|$)')
        command = [pattern.sub(lambda m: mounts[m.group(1)], part) for part in command]
    # Refuse any absolute path the mounts didn't map into the temp directory, so an
    # edited README can never send `find ... -delete` to a real host path.
    for part in command:
        for path in re.findall(r'(?<![^\s"\'=])/[^\s"\';&|]*', part):
            if not (path == stub or path.startswith(stub + '/')):
                sys.stderr.write('stub docker: refused, unmapped path %s\n' % path)
                log({'refused': path})
                sys.exit(REFUSED_CODE)
    if os.environ.get('STUB_FAIL_RUN') in names:
        log({'failed': os.environ['STUB_FAIL_RUN']})
        sys.exit(1)
    sys.exit(subprocess.call(command))
sys.exit(0)
'''.replace('REFUSED_CODE', str(REFUSED))

STUB_LOGGER = r'''
import json, os, sys
with open(os.path.join(os.environ['STUB_DIR'], 'calls'), 'a') as f:
    f.write(json.dumps({os.path.basename(sys.argv[0]): sys.argv[1:]}) + '\n')
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


class StaticTests(unittest.TestCase):
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

    def test_no_block_holds_a_comment_line(self):
        self.assertGreaterEqual(len(blocks()), 8)
        for text in blocks():
            for line in text.splitlines():
                self.assertFalse(line.lstrip().startswith('#'), 'comment line in a pasted block: ' + line)

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


class StubTests(unittest.TestCase):
    """The stub itself: what keeps the block runs off the real host."""

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix='rd679-')).resolve()
        self.addCleanup(shutil.rmtree, str(self.dir))
        self.stub = self.dir / 'docker'
        self.stub.write_text(STUB_DOCKER)
        (self.dir / 'vols').mkdir()

    def run_stub(self, *args):
        env = dict(os.environ, STUB_DIR=str(self.dir))
        return subprocess.run([sys.executable, str(self.stub)] + list(args), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def test_refuses_a_path_it_did_not_map(self):
        result = self.run_stub('run', '--rm', '-v', 'rdtest_x:/data', 'alpine', 'sh', '-c',
                               'find /nonexistent-rd679 -mindepth 1 -delete')
        self.assertEqual(result.returncode, REFUSED, result.stderr)
        self.assertIn('refused, unmapped path /nonexistent-rd679', result.stderr)

    def test_refuses_a_mount_target_the_command_does_not_use(self):
        result = self.run_stub('run', '--rm', '-v', 'rdtest_x:/volume', 'alpine', 'sh', '-c',
                               'find /data -mindepth 1 -delete')
        self.assertEqual(result.returncode, REFUSED, result.stderr)

    def test_runs_a_mapped_command(self):
        (self.dir / 'vols' / 'rdtest_x').mkdir()
        (self.dir / 'vols' / 'rdtest_x' / 'f').write_text('x')
        result = self.run_stub('run', '--rm', '-v', 'rdtest_x:/data', 'alpine', 'sh', '-c',
                               'find /data -mindepth 1 -delete')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(os.listdir(str(self.dir / 'vols' / 'rdtest_x')), [])


class CommandTests(unittest.TestCase):
    """Runs the README's own blocks against the stubs, under bash."""

    shell = 'bash'

    def argv(self, script):
        """(argv, stdin) that run script in this shell, reading no rc file."""
        return ['bash', '--norc', '--noprofile', '-c', script], None

    def setUp(self):
        if not shutil.which(self.shell):
            self.skipTest(self.shell + ' is not on PATH')
        self.dir = Path(tempfile.mkdtemp(prefix='rd679-')).resolve()
        self.addCleanup(shutil.rmtree, str(self.dir))
        bin_dir = self.dir / 'bin'
        bin_dir.mkdir()
        for name, source in (('docker', STUB_DOCKER), ('git', STUB_LOGGER), ('curl', STUB_LOGGER)):
            stub = bin_dir / name
            stub.write_text('#!' + sys.executable + '\n' + source)
            stub.chmod(0o755)
        (self.dir / 'vols').mkdir()
        self.work = self.dir / 'work'
        self.work.mkdir()
        self.env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'],
                        STUB_DIR=str(self.dir), PWD=str(self.work))
        for name in ('BASH_ENV', 'ENV', 'STUB_FAIL_RUN'):
            self.env.pop(name, None)
        rng = random.Random(679)
        for v in VOLUMES:
            volume = self.volume(v)
            (volume / 'sub').mkdir(parents=True)
            (volume / 'data.bin').write_bytes(bytes(rng.getrandbits(8) for _ in range(40000)))
            (volume / '.hidden').write_text('dot file of ' + v)
            (volume / 'sub' / 'note.txt').write_text('note of ' + v)

    def volume(self, name, prefix=PREFIX):
        return self.dir / 'vols' / (prefix + '_' + name)

    def run_script(self, script, **env):
        calls = self.dir / 'calls'
        if calls.exists():
            calls.unlink()
        argv, stdin = self.argv(script)
        result = subprocess.run(argv, input=stdin, cwd=str(self.work), env=dict(self.env, **env),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        log = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        self.assertNotRegex(result.stderr, SHELL_ERRORS, 'the shell could not run the block')
        self.assertFalse([c for c in log if 'refused' in c], 'the stub refused a path: ' + result.stderr)
        return result, log

    def run_block(self, text, prefix=PREFIX, **env):
        script, count = re.subn(r'^P=rag-docker$', 'P=' + prefix, text, flags=re.M)
        self.assertEqual(count, 1, 'the block sets P once')
        return self.run_script(script, **env)

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

    def archives(self):
        return {p.name: p.read_bytes() for p in (self.work / 'backups').iterdir()}

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

    def test_backup_rerun_with_a_wrong_prefix_counts_no_old_archive(self):
        self.backup()
        before = self.archives()
        result, log = self.run_block(backup_block(), prefix='wrongprefix')
        self.assertEqual(self.docker_runs(log), [])
        self.assertNotIn('entries', result.stdout)
        self.assertEqual(self.archives(), before, 'the old archives changed')

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

    def test_rollback_stops_before_starting_when_a_restore_fails(self):
        self.backup()
        restored = self.snapshot()
        archives = self.archives()
        self.change_volumes()
        changed = self.snapshot()
        result, log = self.run_block(rollback_block(), STUB_FAIL_RUN=PREFIX + '_ingest_uploads')
        self.assertIn('ingest_uploads: restore FAILED', result.stdout)
        self.assertEqual(self.compose_up(log), [], 'compose up ran after a failed restore')
        self.assertEqual(len(self.docker_runs(log)), 2, 'the restore went on after the failure')
        after = self.snapshot()
        self.assertEqual(after['weaviate_data'], restored['weaviate_data'])
        self.assertEqual(after['rag_sources'], changed['rag_sources'])
        self.assertEqual(self.archives(), archives, 'the backups changed')


class ZshCommandTests(CommandTests):
    """The main cases again under `zsh -f -c`, macOS's default shell."""

    shell = 'zsh'

    def argv(self, script):
        return ['zsh', '-f', '-c', script], None

    test_backup_of_an_empty_volume_counts_one_entry = None
    test_backup_with_a_wrong_prefix_creates_and_writes_nothing = None
    test_rollback_with_a_missing_archive_empties_nothing = None
    test_rollback_with_a_wrong_prefix_empties_and_creates_nothing = None


class InteractiveZshCommandTests(CommandTests):
    """Every case as pasted into an interactive zsh with comments off, its default."""

    shell = 'zsh'

    def argv(self, script):
        return ['zsh', '-f', '-i', '+o', 'interactivecomments'], script

    def test_every_block_of_the_section_runs(self):
        for number, text in enumerate(blocks(), 1):
            with self.subTest(block=number, first_line=text.splitlines()[0]):
                script = text.replace('<your-username>', 'alice')
                _result, log = self.run_script(script)
                self.assertTrue(log, 'no command of the block reached the stubs')

    def test_a_comment_line_would_break_the_paste(self):
        """The check above can see the failure #686's review found."""
        calls = self.dir / 'calls'
        argv, stdin = self.argv("# A volume that doesn't exist\ndocker volume ls\n")
        result = subprocess.run(argv, input=stdin, cwd=str(self.work), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        self.assertRegex(result.stderr, SHELL_ERRORS)
        self.assertFalse(calls.exists(), 'the quoted text after the comment still ran')


if __name__ == '__main__':
    unittest.main()
