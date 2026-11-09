"""Host-only check of the README's 1.1.0 → 1.2.0 backup and rollback commands (#679, #693).

Reads the command blocks of "Upgrading from 1.1.0 to 1.2.0" in README.md and checks
that the helper image is pinned by version and digest, that no block holds a `#`
comment line (interactive zsh, macOS's default shell, doesn't treat `#` as a comment
unless `interactivecomments` is set), and that git ignores `backups/`. Then it runs
the blocks as written, under bash, `zsh -c` and interactive `zsh -i` with comments
off, with stub `docker`, `git` and `curl` first on PATH and no rc files read. The
stub's named volumes are folders, and `docker run` runs the container's command
with the host's own tar and find, after mapping the mounts to the test's temp
directory. It accepts only mounts inside that directory and only the README's two
container commands (the backup's `tar czf` and the restore's `find -delete && tar
xzf`), and refuses anything else. So the checks the blocks make before they write
are exercised for real:

- the backup skips a volume that doesn't exist (a wrong prefix) instead of creating
  it, and reports each archive's entry count only for a volume that exists; each
  copy goes to a `.tmp` file that replaces the archive only once `gzip -t` passes,
  so a failed or corrupt copy prints `backup FAILED` and leaves an earlier archive;
- the rollback is gated on `git checkout v1.1.0` and checks every archive and every
  volume before it empties any, and empties nothing, nor starts the stack, if the
  checkout or one check fails; a failed restore stops before the stack is started.

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

def refuse(reason):
    sys.stderr.write('stub docker: refused, %s\n' % reason)
    log({'refused': reason})
    sys.exit(REFUSED_CODE)

def inside(path):
    return path.startswith(stub + '/')

# The container may run only the README's two commands, with every mount inside the
# temp directory, so an edited README can never send `find ... -delete` or `tar x`
# to a real host path, whatever its spelling (`..`, `~`, `$HOME`, `-C/etc`, `>`).
FILE = r'[A-Za-z0-9_-][A-Za-z0-9_.-]*'
BACKUP_FILE = re.compile(r'/backup/(' + FILE + ')')
RESTORE = re.compile(r'find /data -mindepth 1 -delete && tar xzf /backup/(' + FILE + r') -C /data')

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
            named = not source.startswith('/')
            path = os.path.realpath(os.path.join(vols, source) if named else source)
            if not inside(path):
                refuse('mount source outside the test directory: ' + rest[i + 1])
            if named:
                names.append(source)
                if not os.path.isdir(path):
                    os.makedirs(path)
                    log({'created': source})
            mounts[target] = path
            i += 2
        else:
            i += 1
    command = rest[i + 1:]
    if '/data' not in mounts or '/backup' not in mounts:
        refuse('/data and /backup must both be mounted: ' + ' '.join(sorted(mounts)))
    backup_file = BACKUP_FILE.fullmatch(command[2]) if len(command) == 6 else None
    restore = RESTORE.fullmatch(command[2]) if len(command) == 3 and command[:2] == ['sh', '-c'] else None
    if backup_file and command[:2] == ['tar', 'czf'] and command[3:] == ['-C', '/data', '.']:
        archive = os.path.join(mounts['/backup'], backup_file.group(1))
        run = ['tar', 'czf', archive, '-C', mounts['/data'], '.']
    elif restore:
        archive = os.path.join(mounts['/backup'], restore.group(1))
        run = ['sh', '-c', 'find "$1" -mindepth 1 -delete && tar xzf "$2" -C "$1"', 'sh', mounts['/data'], archive]
    else:
        refuse('not one of the README command shapes: ' + json.dumps(command))
    if os.environ.get('STUB_FAIL_RUN') in names:
        log({'failed': os.environ['STUB_FAIL_RUN']})
        sys.exit(1)
    status = subprocess.call(run)
    if status == 0 and run[1] == 'czf' and os.environ.get('STUB_CORRUPT_RUN') in names:
        with open(archive, 'r+b') as f:
            data = bytearray(f.read())
            data[len(data) // 2] ^= 0xff
            f.seek(0)
            f.write(bytes(data))
        log({'corrupted': os.environ['STUB_CORRUPT_RUN']})
    sys.exit(status)
sys.exit(0)
'''.replace('REFUSED_CODE', str(REFUSED))

STUB_LOGGER = r'''
import json, os, sys
name = os.path.basename(sys.argv[0])
with open(os.path.join(os.environ['STUB_DIR'], 'calls'), 'a') as f:
    f.write(json.dumps({name: sys.argv[1:]}) + '\n')
if name == 'git' and sys.argv[1:2] == ['checkout'] and os.environ.get('STUB_FAIL_GIT'):
    sys.stderr.write('error: stub git: checkout failed\n')
    sys.exit(1)
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
        self.volume = self.dir / 'vols' / 'rdtest_x'
        self.volume.mkdir(parents=True)
        (self.volume / 'f').write_text('x')
        self.backups = self.dir / 'backups'
        self.backups.mkdir()

    def run_stub(self, *args):
        env = dict(os.environ, STUB_DIR=str(self.dir))
        for name in ('STUB_FAIL_RUN', 'STUB_CORRUPT_RUN'):
            env.pop(name, None)
        return subprocess.run([sys.executable, str(self.stub)] + list(args), env=env,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)

    def run_mounted(self, *command, data='rdtest_x:/data', backup=None):
        backup = backup or str(self.backups) + ':/backup'
        return self.run_stub('run', '--rm', '-v', data, '-v', backup, 'alpine', *command)

    def make_archive(self):
        source = self.dir / 'source'
        source.mkdir()
        (source / 'restored').write_text('from the archive')
        subprocess.check_call(['tar', 'czf', str(self.backups / 'v.tgz'), '-C', str(source), '.'])

    def assert_refused(self, result):
        self.assertEqual(result.returncode, REFUSED, result.stdout + result.stderr)
        self.assertIn('stub docker: refused, ', result.stderr)
        self.assertEqual(tree(str(self.volume)), {'f': b'x'}, 'a refused command changed the volume')

    def test_refuses_each_bypass_of_the_old_path_check(self):
        """The spellings #686's security review found the textual check let through."""
        self.make_archive()
        for command in (['sh', '-c', 'find /data/../../.. -mindepth 1 -delete'],
                        ['sh', '-c', 'find ~ -mindepth 1 -delete'],
                        ['sh', '-c', 'find "$HOME" -delete'],
                        ['sh', '-c', 'find .. -delete'],
                        ['tar', 'xzf', '/backup/v.tgz', '-C/etc'],
                        ['sh', '-c', 'echo x >/etc/foo']):
            with self.subTest(command=command):
                self.assert_refused(self.run_mounted(*command))

    def test_refuses_a_parent_folder_as_the_archive(self):
        self.assert_refused(self.run_mounted(
            'sh', '-c', 'find /data -mindepth 1 -delete && tar xzf /backup/.. -C /data'))

    def test_refuses_a_path_it_did_not_map(self):
        self.assert_refused(self.run_mounted(
            'sh', '-c', 'find /nonexistent-rd679 -mindepth 1 -delete && tar xzf /backup/v.tgz -C /nonexistent-rd679'))

    def test_refuses_a_mount_target_the_command_does_not_use(self):
        self.assert_refused(self.run_mounted(
            'sh', '-c', 'find /data -mindepth 1 -delete && tar xzf /backup/v.tgz -C /data', data='rdtest_x:/volume'))

    def test_refuses_a_bind_mount_from_outside_the_test_directory(self):
        result = self.run_mounted('tar', 'czf', '/backup/v.tgz', '-C', '/data', '.', data='/etc:/data')
        self.assert_refused(result)
        self.assertEqual(os.listdir(str(self.backups)), [])
        self.assertEqual(os.listdir(str(self.dir / 'vols')), ['rdtest_x'])

    def test_refuses_a_volume_name_that_leaves_the_test_directory(self):
        self.assert_refused(self.run_mounted('tar', 'czf', '/backup/v.tgz', '-C', '/data', '.',
                                             data='../../../../../../tmp:/data'))

    def test_runs_the_readme_backup_command(self):
        result = self.run_mounted('tar', 'czf', '/backup/v.tgz', '-C', '/data', '.')
        self.assertEqual(result.returncode, 0, result.stderr)
        listing = subprocess.check_output(['tar', 'tzf', str(self.backups / 'v.tgz')], universal_newlines=True)
        self.assertIn('f', [line.strip('./') for line in listing.splitlines()])

    def test_runs_the_readme_restore_command(self):
        self.make_archive()
        result = self.run_mounted('sh', '-c', 'find /data -mindepth 1 -delete && tar xzf /backup/v.tgz -C /data')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(tree(str(self.volume)), {'restored': b'from the archive'})


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
        for name in ('BASH_ENV', 'ENV', 'STUB_FAIL_RUN', 'STUB_CORRUPT_RUN', 'STUB_FAIL_GIT'):
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

    def assert_no_temporary_file(self):
        left = [p.name for p in (self.work / 'backups').iterdir() if p.name.endswith('.tmp')]
        self.assertEqual(left, [], 'a .tmp file was left in backups/')

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
        self.assertNotIn('backup FAILED', result.stdout)
        self.assert_no_temporary_file()

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
        for v in VOLUMES:
            self.assertIn(v + ': backup FAILED', result.stdout)

    def test_backup_rerun_with_a_wrong_prefix_counts_no_old_archive(self):
        self.backup()
        before = self.archives()
        result, log = self.run_block(backup_block(), prefix='wrongprefix')
        self.assertEqual(self.docker_runs(log), [])
        self.assertNotIn('entries', result.stdout)
        self.assertEqual(self.archives(), before, 'the old archives changed')

    def assert_rerun_keeps_the_archive(self, **env):
        self.backup()
        before = self.archives()
        self.change_volumes()
        result, _log = self.run_block(backup_block(), **env)
        after = self.archives()
        self.assertEqual(after['ingest_uploads-v1.1.0.tgz'], before['ingest_uploads-v1.1.0.tgz'],
                         'a failed copy replaced the earlier archive')
        for v in ('weaviate_data', 'rag_sources'):
            self.assertNotEqual(after[v + '-v1.1.0.tgz'], before[v + '-v1.1.0.tgz'], v + ' was not backed up again')
        self.assertIn('ingest_uploads: backup FAILED', result.stdout)
        self.assertNotIn('weaviate_data: backup FAILED', result.stdout)
        self.assertNotIn('rag_sources: backup FAILED', result.stdout)
        self.assert_no_temporary_file()

    def test_backup_rerun_keeps_the_archive_when_a_copy_fails(self):
        self.assert_rerun_keeps_the_archive(STUB_FAIL_RUN=PREFIX + '_ingest_uploads')

    def test_backup_rerun_keeps_the_archive_when_a_copy_is_corrupt(self):
        self.assert_rerun_keeps_the_archive(STUB_CORRUPT_RUN=PREFIX + '_ingest_uploads')

    def test_backup_with_a_corrupt_first_copy_writes_no_archive(self):
        result, log = self.run_block(backup_block(), STUB_CORRUPT_RUN=PREFIX + '_ingest_uploads')
        self.assertIn({'corrupted': PREFIX + '_ingest_uploads'}, log)
        self.assertEqual(sorted(self.archives()), ['rag_sources-v1.1.0.tgz', 'weaviate_data-v1.1.0.tgz'])
        self.assertIn('ingest_uploads: backup FAILED', result.stdout)
        self.assertNotRegex(result.stdout, r'(?m)^ingest_uploads: \d+ entries$')
        self.assertRegex(result.stdout, r'(?m)^weaviate_data: \d+ entries$')

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
        self.assertIn('git checkout v1.1.0: OK', result.stdout)
        self.assertEqual(self.created(log), [])

    def test_rollback_with_a_failing_checkout_empties_nothing(self):
        self.backup()
        self.change_volumes()
        before = self.snapshot()
        result, log = self.run_block(rollback_block(), STUB_FAIL_GIT='1')
        self.assertIn({'git': ['checkout', 'v1.1.0']}, log)
        self.assert_nothing_restored(result, log, before)
        self.assertIn('git checkout v1.1.0: FAILED', result.stdout)
        for v in VOLUMES:
            self.assertIn(v + ': OK', result.stdout)

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
