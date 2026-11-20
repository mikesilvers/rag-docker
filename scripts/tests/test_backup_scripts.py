"""The encrypted backup and restore scripts (#848), on throwaway volumes.

scripts/backup.sh streams each data volume through tar and age in a container
of the age image (scripts/age), encrypted to a new X25519 key pair per volume,
and scripts/restore.sh refuses a missing or wrong key, or a damaged archive,
before it stops or empties anything. This runs both for real, with the docker
CLI, on volumes it creates under its own random project name (rd848t<hex>,
with compose's labels) and removes afterwards, never a stack's. The scripts run
from a scratch git checkout holding copies of them, with HOME set to a
temporary folder, so nothing is written into this checkout or the real home.

It checks: a backup then a restore gives back every file, folder, link, mode
and owner; a missing volume is skipped; the project's running containers are
stopped, not removed; every key differs, within a run and between runs; the
backup folder holds only the encrypted archives and their keys, 0700 and 0600,
with no unencrypted archive and no key in the output; the refusals (a wrong
key, a missing key file, a damaged or cut archive) leave the volume and the
running container as they were; the prefix and location rules; and, without
Docker, the image pins and `*.key` in .gitignore.

Needs the docker CLI (skipped, with a message, only when it is missing); the
age image is built once if this machine doesn't have it (network). A docker or
build failure fails it.
Run: python3 scripts/tests/test_backup_scripts.py
"""
from pathlib import Path
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ('scripts/backup.sh', 'scripts/restore.sh', 'scripts/backup-common.sh', 'scripts/age/Dockerfile')
VOLUMES = ('weaviate_data', 'ingest_uploads', 'rag_sources', 'rag_preparation', 'auth_data', 'lan_certs')
PIN = re.compile(r'\balpine:(\d+\.\d+\.\d+)@sha256:[0-9a-f]{64}(?![0-9a-f])')
SECRET = 'AGE-SECRET-KEY-'
NOTHING = 'Nothing was stopped, emptied or restored.'
DOCKER = shutil.which('docker')
# A listing of a volume: every entry's path, mode, owner, size and type, link
# targets and file digests, made in a container so the host's tools don't matter.
LISTING = ('cd /data && find . -mindepth 1 | sort | while read -r f; do '
           'stat -c "%n %a %u %g %s %F" "$f"; [ -L "$f" ] && echo "-> $(readlink "$f")"; done; '
           'find . -type f | sort | xargs -r sha256sum')
FILL = ('mkdir -p /data/docs/sub /data/empty && echo "note of $1" > /data/docs/sub/note.txt && '
        'echo "hidden of $1" > /data/.hidden && chmod 640 /data/.hidden && chown 1234:1234 /data/.hidden && '
        'head -c 200000 /dev/urandom > /data/docs/random.bin && ln -s docs/sub/note.txt /data/link')


def docker(*args, **kwargs):
    return subprocess.run([DOCKER] + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          universal_newlines=True, **kwargs)


def dockerfile():
    return (ROOT / 'scripts/age/Dockerfile').read_text()


def age_image():
    version = re.search(r'^RUN apk add --no-cache age=(\S+)$', dockerfile(), re.M).group(1)
    return 'rag-docker-age:' + version


class StaticTests(unittest.TestCase):
    def test_the_age_image_uses_the_readmes_pinned_alpine(self):
        froms = re.findall(r'^FROM (\S+)$', dockerfile(), re.M)
        self.assertEqual(len(froms), 1, froms)
        self.assertTrue(PIN.fullmatch(froms[0]), 'not pinned by version and digest: ' + froms[0])
        readme_pins = set(m.group(0) for m in PIN.finditer((ROOT / 'README.md').read_text()))
        self.assertEqual(readme_pins, {froms[0]})

    def test_age_is_pinned_to_an_exact_package_version(self):
        self.assertRegex(dockerfile(), r'(?m)^RUN apk add --no-cache age=\d+\.\d+\.\d+-r\d+$')

    def test_gitignore_lists_key_files(self):
        self.assertIn('*.key', (ROOT / '.gitignore').read_text().splitlines())

    def test_git_ignores_a_key_file(self):
        if not shutil.which('git'):
            self.skipTest('git is not on PATH')
        inside = subprocess.run(['git', '-C', str(ROOT), 'rev-parse', '--is-inside-work-tree'],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True)
        if inside.stdout.strip() != 'true':
            self.skipTest(str(ROOT) + ' is not a git work tree')
        for name in ('weaviate_data-20261120T000000Z.key', 'backups/x/auth_data-20261120T000000Z.key'):
            result = subprocess.run(['git', '-C', str(ROOT), 'check-ignore', '-q', name])
            self.assertEqual(result.returncode, 0, 'git does not ignore ' + name)


@unittest.skipUnless(DOCKER, 'the docker CLI is not on PATH')
class ScriptTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = age_image()
        if docker('image', 'inspect', cls.image).returncode != 0:
            built = docker('build', '-q', '-t', cls.image, str(ROOT / 'scripts/age'))
            assert built.returncode == 0, built.stderr

    def setUp(self):
        self.prefix = 'rd848t' + secrets.token_hex(4)
        self.tmp = Path(tempfile.mkdtemp(prefix='rd848-')).resolve()
        self.addCleanup(shutil.rmtree, str(self.tmp), True)
        self.home = self.tmp / 'home'
        self.home.mkdir()
        self.checkout = self.tmp / 'checkout'
        for name in SCRIPTS:
            target = self.checkout / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(ROOT / name), str(target))
        (self.checkout / 'backups').mkdir()
        subprocess.run(['git', 'init', '-q', str(self.checkout)], check=True)
        self.volumes = []
        self.containers = []
        self.addCleanup(self.cleanup)
        self.outputs = []

    def cleanup(self):
        for container in self.containers:
            docker('rm', '-f', container)
        for volume in self.volumes:
            docker('volume', 'rm', '-f', volume)

    def create_volume(self, name, fill=True):
        volume = self.prefix + '_' + name
        created = docker('volume', 'create', '--label', 'com.docker.compose.project=' + self.prefix,
                         '--label', 'com.docker.compose.volume=' + name, volume)
        self.assertEqual(created.returncode, 0, created.stderr)
        self.volumes.append(volume)
        if fill:
            self.in_volume(name, FILL, name)
        return volume

    def in_volume(self, name, script, *args, readonly=False):
        mount = self.prefix + '_' + name + ':/data' + (':ro' if readonly else '')
        result = docker('run', '--rm', '--network', 'none', '-v', mount, self.image, 'sh', '-c', script, 'sh', *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def listing(self, name):
        return self.in_volume(name, LISTING, readonly=True)

    def start_container(self):
        started = docker('run', '-d', '--network', 'none', '--label', 'com.docker.compose.project=' + self.prefix,
                         self.image, 'sleep', '600')
        self.assertEqual(started.returncode, 0, started.stderr)
        self.containers.append(started.stdout.strip())
        return started.stdout.strip()

    def running(self, container):
        state = docker('inspect', '-f', '{{.State.Running}}', container)
        self.assertEqual(state.returncode, 0, 'the container was removed: ' + state.stderr)
        return state.stdout.strip() == 'true'

    def script(self, name, *args, env=None):
        # HOME is the test's own; the docker CLI keeps finding its context
        # (Docker Desktop's socket is named in ~/.docker) through DOCKER_CONFIG.
        environment = dict(os.environ, HOME=str(self.home),
                           DOCKER_CONFIG=os.environ.get('DOCKER_CONFIG', os.path.expanduser('~/.docker')))
        environment.pop('COMPOSE_PROJECT_NAME', None)
        environment.update(env or {})
        result = subprocess.run(['bash', str(self.checkout / 'scripts' / name)] + list(args), cwd=str(self.tmp),
                                env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, universal_newlines=True)
        self.outputs.append(result.stdout + result.stderr)
        self.assertNotIn(SECRET, result.stdout + result.stderr, 'a script printed a secret key')
        return result

    def backup(self, *args):
        result = self.script('backup.sh', '--prefix', self.prefix, '--yes', *args)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        runs = re.findall(r'^Backup folder: (.+)$', result.stdout, re.M)
        self.assertEqual(len(runs), 1, result.stdout)
        return result, Path(runs[0])

    @staticmethod
    def pairs(run, names):
        found = []
        for name in names:
            archives = list(run.glob(name + '-*.tgz.age'))
            keys = list(run.glob(name + '-*.key'))
            assert len(archives) == 1 and len(keys) == 1, (name, archives, keys)
            found += [str(archives[0]), str(keys[0])]
        return found

    def restore(self, *paths):
        return self.script('restore.sh', '--prefix', self.prefix, '--yes', *paths)

    def assert_refused_untouched(self, result, name, before, container):
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(NOTHING, result.stderr)
        self.assertEqual(self.listing(name), before, 'a refused restore changed the volume')
        self.assertTrue(self.running(container), 'a refused restore stopped the stack')

    # The round trip

    def test_backup_then_restore_gives_back_every_volume(self):
        main = VOLUMES[:5]
        for name in main:
            self.create_volume(name)
        before = {name: self.listing(name) for name in main}
        container = self.start_container()
        result, run = self.backup()
        self.assertIn('lan_certs: no volume {}_lan_certs, skipped'.format(self.prefix), result.stdout)
        self.assertFalse(self.running(container), 'the backup did not stop the running container')
        self.assertEqual(run.parent, self.home / 'rag-docker-backups')
        self.assertEqual(run.parent.stat().st_mode & 0o777, 0o700)
        for name in main:
            self.in_volume(name, 'rm -r /data/docs && echo changed > /data/added-later')
            self.assertNotEqual(self.listing(name), before[name])
        restored = self.restore(*self.pairs(run, main))
        self.assertEqual(restored.returncode, 0, restored.stdout + restored.stderr)
        for name in main:
            self.assertEqual(self.listing(name), before[name], name + ' did not come back the same')
            self.assertIn(name + ': restored', restored.stdout)

    def test_the_backup_folder_holds_only_encrypted_archives_and_keys(self):
        for name in ('weaviate_data', 'auth_data'):
            self.create_volume(name)
        _, run = self.backup()
        self.assertEqual(run.stat().st_mode & 0o777, 0o700)
        names = sorted(p.name for p in run.iterdir())
        stamp = run.name[len(self.prefix) + 1:]
        self.assertEqual(names, sorted(n + '-' + stamp + suffix for n in ('weaviate_data', 'auth_data')
                                       for suffix in ('.tgz.age', '.key')))
        for path in run.iterdir():
            data = path.read_bytes()
            self.assertEqual(path.stat().st_mode & 0o777, 0o600, path.name)
            self.assertFalse(data.startswith(b'\x1f\x8b'), path.name + ' is a gzip stream')
            self.assertNotEqual(data[257:262], b'ustar', path.name + ' is a tar archive')
            if path.name.endswith('.tgz.age'):
                self.assertTrue(data.startswith(b'age-encryption.org/v1\n-> X25519 '), path.name)
            else:
                self.assertIn(SECRET.encode(), data)

    def test_every_key_differs_within_and_between_runs(self):
        for name in ('weaviate_data', 'ingest_uploads', 'rag_sources'):
            self.create_volume(name)
        _, first = self.backup()
        _, second = self.backup('--dir', str(self.tmp / 'elsewhere'))
        keys, stanzas = [], []
        for run in (first, second):
            for path in run.glob('*.key'):
                keys += [line for line in path.read_text().splitlines() if line.startswith(SECRET)]
            for path in run.glob('*.tgz.age'):
                stanzas.append(path.read_bytes().split(b'\n')[1])
        self.assertEqual(len(keys), 6)
        self.assertEqual(len(set(keys)), 6, 'two archives share a key')
        self.assertEqual(len(set(stanzas)), 6)

    # Refusals before anything is stopped or emptied

    def test_a_wrong_or_missing_key_or_a_damaged_archive_changes_nothing(self):
        for name in ('weaviate_data', 'auth_data'):
            self.create_volume(name)
        _, run = self.backup()
        archive, key, _, other_key = self.pairs(run, ('weaviate_data', 'auth_data'))
        self.in_volume('weaviate_data', 'echo changed > /data/added-later')
        before = self.listing('weaviate_data')
        container = self.start_container()
        damaged = self.tmp / 'damaged' / Path(archive).name
        damaged.parent.mkdir()
        data = bytearray(Path(archive).read_bytes())
        data[len(data) // 2] ^= 0xff
        damaged.write_bytes(bytes(data))
        cut = self.tmp / 'cut' / Path(archive).name
        cut.parent.mkdir()
        cut.write_bytes(Path(archive).read_bytes()[:-20])
        cases = {
            'wrong key': (archive, other_key),
            'missing key file': (archive, str(run / 'no-such.key')),
            'damaged archive': (str(damaged), key),
            'cut archive': (str(cut), key),
        }
        for case, paths in cases.items():
            with self.subTest(case=case):
                self.assert_refused_untouched(self.restore(*paths), 'weaviate_data', before, container)

    def test_a_missing_key_argument_is_refused(self):
        self.create_volume('weaviate_data')
        _, run = self.backup()
        archive = self.pairs(run, ('weaviate_data',))[0]
        result = self.restore(archive)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('give each archive followed by its key file', result.stderr)

    def test_restore_refuses_a_volume_that_does_not_exist(self):
        self.create_volume('weaviate_data')
        _, run = self.backup()
        archive, key = self.pairs(run, ('weaviate_data',))
        renamed = self.tmp / ('rag_sources-' + Path(archive).name.split('-', 1)[1])
        shutil.copy2(archive, str(renamed))
        result = self.restore(str(renamed), key)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('no such volume: {}_rag_sources'.format(self.prefix), result.stderr)
        self.assertIn(NOTHING, result.stderr)

    # The prefix and the location

    def test_a_prefix_with_no_volumes_is_refused_and_lists_the_projects(self):
        self.create_volume('weaviate_data', fill=False)
        result = self.script('backup.sh', '--prefix', self.prefix + 'x', '--yes')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("no volume of project '{}x'".format(self.prefix), result.stderr)
        self.assertIn(self.prefix, result.stderr.split('Projects with rag-docker volumes:')[1].split('\n')[0].split())
        self.assertFalse((self.home / 'rag-docker-backups').exists())

    def test_an_invalid_prefix_is_refused(self):
        result = self.script('backup.sh', '--prefix', 'Bad/Prefix', '--yes')
        self.assertEqual(result.returncode, 1)
        self.assertIn('is not a compose project name', result.stderr)

    def test_yes_needs_a_prefix(self):
        for name in ('backup.sh', 'restore.sh'):
            with self.subTest(script=name):
                args = ['--yes'] + (['a.tgz.age', 'a.key'] if name == 'restore.sh' else [])
                result = self.script(name, *args)
                self.assertEqual(result.returncode, 1)
                self.assertIn('--yes needs --prefix', result.stderr)

    def test_without_a_terminal_the_detected_prefix_is_shown_and_nothing_is_done(self):
        self.create_volume('weaviate_data')
        container = self.start_container()
        result = self.script('backup.sh', env={'COMPOSE_PROJECT_NAME': self.prefix})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("Back up project '{}'".format(self.prefix), result.stdout)
        self.assertIn('no terminal to confirm on', result.stderr)
        self.assertFalse((self.home / 'rag-docker-backups').exists())
        self.assertTrue(self.running(container))

    def test_the_checkout_folder_name_is_the_default_prefix(self):
        named = self.tmp / self.prefix.upper()
        self.checkout.rename(named)
        self.checkout = named
        self.create_volume('weaviate_data', fill=False)
        result = self.script('backup.sh')
        self.assertIn("Back up project '{}'".format(self.prefix), result.stdout)
        self.assertEqual(result.returncode, 1)

    def test_a_folder_inside_the_checkout_is_refused_unless_under_backups(self):
        self.create_volume('weaviate_data', fill=False)
        for inside in (self.checkout, self.checkout / 'scripts', self.checkout / 'new' / 'folder',
                       self.checkout / 'backups' / '..' / 'scripts'):
            with self.subTest(folder=str(inside)):
                result = self.script('backup.sh', '--prefix', self.prefix, '--yes', '--dir', str(inside))
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn('inside the git checkout', result.stderr)
        result = self.script('backup.sh', '--prefix', self.prefix, '--yes', '--dir', 'checkout/backups/nested/../x')
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse((self.checkout / 'backups' / 'nested').exists())
        _, run = self.backup('--dir', str(self.checkout / 'backups' / 'mine'))
        self.assertEqual(run.parent, self.checkout / 'backups' / 'mine')
        self.assertEqual(run.parent.stat().st_mode & 0o777, 0o700)


if __name__ == '__main__':
    unittest.main()
