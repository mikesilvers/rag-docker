"""Host-only check of the Weaviate upgrade-path suite's helper (#805).

`scripts/verify/weaviate_upgrade.py` runs in full verify runs only, against Docker, so its first
real run is a full `stack.sh run`. This checks everything that can be checked without Docker:
which tags it upgrades between (RAG_WEAVIATE_FROM, else origin/main's docker-compose.yml, read
from a scratch git repository here) and what it refuses; the comparison of what reads back; and a
whole run against a fake `docker` that records every command: the names and labels that make the
throwaway container, volume and network part of the rag-verify project, no published port, an
internal network, the lowered Raft snapshot settings, the order of the steps, stopping at the first
failure, and removing everything after a pass and after a failure, never an image. It also checks
the copies of the suite and the helper embedded in IMPLEMENTATION.md.

Standard library only; no Docker or stack needed.
Run: python3 scripts/tests/test_weaviate_upgrade.py
"""
import copy
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/verify'))
import weaviate_upgrade as wu  # noqa: E402

NAME = 'rag-verify-wvup-test'
COMPOSE = {'services': {'weaviate': {
    'image': 'semitechnologies/weaviate:1.41.0',
    'environment': {'CLUSTER_HOSTNAME': 'node1', 'RAFT_BOOTSTRAP_EXPECT': '1', 'AUTOSCHEMA_ENABLED': 'false',
                    'RAFT_SNAPSHOT_THRESHOLD': '128', 'RAFT_SNAPSHOT_INTERVAL': '30',
                    'PERSISTENCE_DATA_PATH': '/var/lib/weaviate'}}}}
MAIN = 'services:\n  weaviate:\n    image: semitechnologies/weaviate:1.40.2\n'


def state(**changes):
    base = {
        'schema': {'class': 'VfyUpgrade', 'properties': [['content', ['text']]], 'vectorizer': 'text2vec-ollama',
                   'vectorIndexType': 'hnsw', 'distance': 'cosine'},
        'count': 12,
        'objects': {'a': {'properties': {'content': 'x'}, 'vector': [0.1, 0.2]},
                    'b': {'properties': {'content': 'y'}, 'vector': [0.3, 0.4]}},
        'near': [['a', 0.01], ['b', 0.02]],
        'collections': ['VfyUpgrade'],
    }
    base.update(changes)
    return base


class TagTests(unittest.TestCase):
    def test_override_wins_over_origin_main(self):
        tags = wu.resolve_tags(COMPOSE, {'RAG_WEAVIATE_FROM': '1.39.6'}, lambda: MAIN)
        self.assertEqual(tags, ('1.39.6', 'RAG_WEAVIATE_FROM', '1.41.0'))

    def test_origin_main_from_a_git_repository(self):
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch)
        git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', '-c', 'init.defaultBranch=main',
               '-c', 'commit.gpgsign=false']
        upstream, clone = scratch / 'up', scratch / 'clone'
        subprocess.run(git + ['init', '-q', str(upstream)], check=True)
        (upstream / 'docker-compose.yml').write_text(MAIN)
        subprocess.run(git + ['-C', str(upstream), 'add', '.'], check=True)
        subprocess.run(git + ['-C', str(upstream), 'commit', '-qm', 'pin'], check=True)
        subprocess.run(git + ['clone', '-q', str(upstream), str(clone)], check=True)
        tags = wu.resolve_tags(COMPOSE, {}, lambda: wu.git_main_compose(clone))
        self.assertEqual(tags, ('1.40.2', 'origin/main', '1.41.0'))
        # No origin/main at all: refused, with what to do.
        with self.assertRaises(wu.Refusal) as caught:
            wu.resolve_tags(COMPOSE, {}, lambda: wu.git_main_compose(upstream))
        self.assertIn('RAG_WEAVIATE_FROM=<X.Y.Z>', str(caught.exception))

    def test_unreadable_origin_main_is_refused(self):
        for text in (None, 'services: {}\n'):
            with self.subTest(text=text), self.assertRaises(wu.Refusal) as caught:
                wu.resolve_tags(COMPOSE, {}, lambda: text)
            self.assertIn('RAG_WEAVIATE_FROM', str(caught.exception))

    def test_only_plain_tags(self):
        for bad in ('latest', '1.40.2-9858973.arm64', '1.40', '1.40.2 ', 'x:1.40.2'):
            with self.subTest(tag=bad), self.assertRaises(wu.Refusal):
                wu.resolve_tags(COMPOSE, {'RAG_WEAVIATE_FROM': bad}, lambda: MAIN)
        with self.assertRaises(wu.Refusal):
            wu.resolve_tags(COMPOSE, {}, lambda: MAIN.replace('1.40.2', '1.40.2-9858973.arm64'))
        for image in ('semitechnologies/weaviate:latest', 'other/weaviate:1.41.0', ''):
            compose = copy.deepcopy(COMPOSE)
            compose['services']['weaviate']['image'] = image
            with self.subTest(image=image), self.assertRaises(wu.Refusal):
                wu.resolve_tags(compose, {}, lambda: MAIN)

    def test_a_downgrade_is_refused_and_the_same_version_allowed(self):
        with self.assertRaises(wu.Refusal) as caught:
            wu.resolve_tags(COMPOSE, {'RAG_WEAVIATE_FROM': '1.41.1'}, lambda: MAIN)
        self.assertIn('not an upgrade', str(caught.exception))
        with self.assertRaises(wu.Refusal):     # compared as numbers, not text
            wu.resolve_tags(COMPOSE, {'RAG_WEAVIATE_FROM': '1.100.0'}, lambda: MAIN)
        self.assertEqual(wu.resolve_tags(COMPOSE, {'RAG_WEAVIATE_FROM': '1.41.0'}, lambda: MAIN)[0], '1.41.0')
        self.assertEqual(wu.resolve_tags(COMPOSE, {'RAG_WEAVIATE_FROM': '1.9.9'}, lambda: MAIN)[0], '1.9.9')


class EnvTests(unittest.TestCase):
    def test_compose_environment_with_the_snapshot_settings_lowered(self):
        env = wu.weaviate_env(COMPOSE)
        self.assertIn('CLUSTER_HOSTNAME=node1', env)
        self.assertIn('AUTOSCHEMA_ENABLED=false', env)
        self.assertIn('RAFT_SNAPSHOT_THRESHOLD=8', env)
        self.assertIn('RAFT_SNAPSHOT_INTERVAL=2', env)
        self.assertNotIn('RAFT_SNAPSHOT_THRESHOLD=128', env)
        self.assertEqual(env, sorted(env))


class CompareTests(unittest.TestCase):
    def test_equal_and_within_tolerance(self):
        self.assertIsNone(wu.compare(state(), state()))
        near = state(near=[['a', 0.01 + 5e-6], ['b', 0.02]])
        objects = copy.deepcopy(state()['objects'])
        objects['a']['vector'] = [0.1 + 5e-7, 0.2]
        self.assertIsNone(wu.compare(state(), near))
        self.assertIsNone(wu.compare(state(), state(objects=objects)))

    def test_each_difference_is_named(self):
        objects = copy.deepcopy(state()['objects'])
        objects['a']['properties'] = {'content': 'changed'}
        vector = copy.deepcopy(state()['objects'])
        vector['b']['vector'] = [0.3, 0.4 + 1e-4]
        missing = copy.deepcopy(state()['objects'])
        del missing['b']
        schema = dict(state()['schema'], distance='dot')
        cases = {
            'schema': state(schema=schema),
            'count': state(count=11),
            'properties': state(objects=objects),
            'vector': state(objects=vector),
            'ids': state(objects=missing),
            'nearVector ids': state(near=[['b', 0.02], ['a', 0.01]]),
            'distance': state(near=[['a', 0.02], ['b', 0.02]]),
        }
        for word, current in cases.items():
            with self.subTest(difference=word):
                difference = wu.compare(state(), current)
                self.assertIsNotNone(difference)
                self.assertIn(word, difference)


class FakeDocker:
    """Answers the commands weaviate_upgrade.py sends, and records them."""

    def __init__(self, fail=None, image_present=True):
        self.calls = []
        self.fail = fail or set()
        self.image_present = image_present
        self.ready_after = 2            # readiness polls that fail first
        self.snapshot_files = {'/var/lib/weaviate/raft/snapshots/1-8-1/meta.json'}
        self.running_image = None
        self.client_phases = []
        self.reads = 0

    def run(self, args, input=None, timeout=120):
        args = list(args)
        self.calls.append(args)
        head = args[0]
        if head == 'image' and args[1] == 'inspect':
            return (0 if self.image_present else 1), '', ''
        if head == 'pull':
            return (1, '', 'pull access denied') if 'pull' in self.fail else (0, 'pulled', '')
        if head == 'run' and '-d' in args:
            self.running_image = args[-1]
            self.ready_polls = 0
            return 0, 'container-id\n', ''
        if head == 'exec' and 'wget' in args:
            self.ready_polls += 1
            if 'ready' in self.fail:
                return 1, '', ''
            return (0 if self.ready_polls > self.ready_after else 1), '', ''
        if head == 'exec' and 'find' in args:
            if '-name' in args:
                return 0, '\n'.join(sorted(self.snapshot_files)) + '\n', ''
            return 0, '/var/lib/weaviate/raft\n/var/lib/weaviate/raft/raft.db\n', ''
        if head == 'inspect':
            return 0, ('true\n' if 'Running' in args[2] else 'running exit=0\n'), ''
        if head == 'logs':
            return 0, 'log line\n', ''
        if head == 'run' and '--rm' in args:
            phase = args[args.index('client') + 1]
            self.client_phases.append(phase)
            if phase == 'meta':
                return 0, json.dumps({'version': self.running_image.rsplit(':', 1)[1]}) + '\n', ''
            if phase == 'churn':
                if 'snapshot' not in self.fail:
                    self.snapshot_files.add('/var/lib/weaviate/raft/snapshots/2-40-2/meta.json')
                return 0, json.dumps({'churned': 12}) + '\n', ''
            self.reads += 1
            current = state()
            if 'compare' in self.fail and phase == 'read':
                current['count'] = 11
            return 0, 'a warning\n' + json.dumps(current) + '\n', ''
        if head in ('ps', 'volume', 'network') and args[1] in ('-aq', 'ls'):
            return 0, '', ''
        return 0, '', ''


class RunTests(unittest.TestCase):
    def run_helper(self, docker, tags=('1.40.2', 'origin/main', '1.41.0')):
        out = io.StringIO()
        clock = iter(range(0, 100000, 1))
        code = wu.Run(docker, 'sha256:api', COMPOSE, tags, 'script text', sleep=lambda s: None,
                      clock=lambda: next(clock), name=NAME, out=out).run()
        lines = [line.split('\t') for line in out.getvalue().splitlines()]
        return code, lines

    def assert_removed(self, docker):
        calls = docker.calls
        self.assertIn(['rm', '-f', NAME], calls)
        self.assertIn(['volume', 'rm', NAME], calls)
        self.assertIn(['network', 'rm', NAME], calls)
        for args in calls:
            self.assertFalse(args[0] in ('rmi',) or args[:2] in (['image', 'rm'], ['image', 'prune']),
                             'an image must never be removed: {}'.format(args))

    def test_a_passing_run(self):
        docker = FakeDocker()
        code, lines = self.run_helper(docker)
        self.assertEqual(code, 0, lines)
        self.assertEqual([line[0] for line in lines], ['PASS'] * 8)
        self.assertEqual([line[1] for line in lines], [
            '1.40.2 starts on a fresh volume and reports 1.40.2',
            'data written on 1.40.2 reads back (12 objects with vectors)',
            "1.41.0 starts on 1.40.2's volume and reports 1.41.0",
            'after the upgrade the schema, count, objects and nearVector match',
            '1.41.0 takes a Raft snapshot after 12 collections are created and deleted',
            '1.41.0 restarts after the snapshot and reports 1.41.0',
            'after the restart the data matches and the throwaway collections are gone',
            'the throwaway container, volume and network are removed',
        ])
        self.assertEqual(docker.client_phases, ['meta', 'write', 'meta', 'read', 'churn', 'meta', 'read'])
        starts = [args for args in docker.calls if args[0] == 'run' and '-d' in args]
        self.assertEqual([args[-1] for args in starts],
                         ['semitechnologies/weaviate:1.40.2', 'semitechnologies/weaviate:1.41.0'])
        self.assertIn(['stop', '-t', '60', NAME], docker.calls)
        self.assertIn(['start', NAME], docker.calls)
        self.assert_removed(docker)

    def test_names_labels_no_published_port_internal_network(self):
        docker = FakeDocker()
        self.run_helper(docker)
        created = [args for args in docker.calls
                   if args[:2] in (['network', 'create'], ['volume', 'create']) or args[0] == 'run']
        self.assertTrue(created)
        for args in created:
            with self.subTest(args=args[:3]):
                self.assertIn('com.docker.compose.project=rag-verify', args)
                self.assertIn('rag-verify.check=weaviate-upgrade', args)
                for arg in args:
                    self.assertFalse(arg in ('-p', '-P') or arg.startswith('--publish'), args)
        network = [args for args in docker.calls if args[:2] == ['network', 'create']][0]
        self.assertIn('--internal', network)
        self.assertEqual(network[-1], NAME)
        for args in created:
            if args[0] == 'run':
                self.assertIn(NAME, args[args.index('--network') + 1])
        start = [args for args in docker.calls if args[0] == 'run' and '-d' in args][0]
        self.assertIn('{}:/var/lib/weaviate'.format(NAME), start)
        self.assertIn('RAFT_SNAPSHOT_THRESHOLD=8', start)
        self.assertIn('RAFT_SNAPSHOT_INTERVAL=2', start)
        self.assertIn('CLUSTER_HOSTNAME=node1', start)
        client = [args for args in docker.calls if args[0] == 'run' and '--rm' in args][0]
        self.assertEqual(client[client.index('sha256:api'):][:4], ['sha256:api', 'python', '-', 'client'])

    def test_leftovers_of_an_earlier_run_are_removed_first(self):
        docker = FakeDocker()
        self.run_helper(docker)
        first = docker.calls[0]
        self.assertEqual(first[:2], ['ps', '-aq'])
        self.assertIn('label=rag-verify.check=weaviate-upgrade', first)
        self.assertIn('label=com.docker.compose.project=rag-verify', first)

    def test_a_missing_image_is_pulled_and_a_failed_pull_stops(self):
        docker = FakeDocker(image_present=False)
        code, _ = self.run_helper(docker)
        self.assertEqual(code, 0)
        self.assertIn(['pull', 'semitechnologies/weaviate:1.40.2'], docker.calls)
        docker = FakeDocker(image_present=False, fail={'pull'})
        code, lines = self.run_helper(docker)
        self.assertEqual(code, 1)
        self.assertEqual(lines[0][0], 'FAIL')
        self.assertIn('pull access denied', lines[0][2])
        self.assertEqual(len(lines), 2)            # the failure, then the removal check
        self.assert_removed(docker)

    def test_failures_stop_later_steps_and_everything_is_removed(self):
        for failure, failed_index, detail in (('ready', 0, 'not ready within 300s'),
                                              ('compare', 3, 'count 11 != 12'),
                                              ('snapshot', 4, 'no new Raft snapshot within 180s')):
            with self.subTest(failure=failure):
                docker = FakeDocker(fail={failure})
                code, lines = self.run_helper(docker)
                self.assertEqual(code, 1)
                self.assertEqual([line[0] for line in lines[:failed_index]], ['PASS'] * failed_index)
                self.assertEqual(lines[failed_index][0], 'FAIL')
                self.assertIn(detail, lines[failed_index][2])
                self.assertEqual(lines[failed_index + 1][1], 'the throwaway container, volume and network are removed')
                self.assertEqual(len(lines), failed_index + 2)
                self.assert_removed(docker)
        # The readiness failure names the container's state and its log.
        docker = FakeDocker(fail={'ready'})
        _, lines = self.run_helper(docker)
        self.assertIn('state: running', lines[0][2])
        self.assertIn('log line', lines[0][2])
        docker = FakeDocker(fail={'snapshot'})
        _, lines = self.run_helper(docker)
        self.assertIn('raft.db', lines[4][2])

    def test_host_main_reports_a_refusal_without_docker(self):
        scratch = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, scratch)
        (scratch / 'compose.json').write_text(json.dumps(COMPOSE))
        docker = FakeDocker()
        out = io.StringIO()
        code = wu.host_main(['run', '--checkout', str(scratch), '--api-image', 'x', '--compose-json',
                             str(scratch / 'compose.json')], docker=docker, env={'RAG_WEAVIATE_FROM': 'latest'},
                            main_compose=lambda: MAIN, out=out)
        self.assertEqual(code, 1)
        self.assertTrue(out.getvalue().startswith('FAIL\t'))
        self.assertEqual(docker.calls, [])
        out = io.StringIO()
        code = wu.host_main(['run', '--checkout', str(scratch), '--api-image', 'x', '--compose-json',
                             str(scratch / 'compose.json')], docker=FakeDocker(), env={},
                            main_compose=lambda: MAIN.replace('1.40.2', '1.41.0'), out=out)
        first = out.getvalue().splitlines()[0]
        self.assertEqual(first, 'PASS\tupgrade path 1.41.0 (origin/main) -> 1.41.0 (checkout): same version')


class EmbeddedTests(unittest.TestCase):
    def test_implementation_md_copies_are_the_files(self):
        text = (ROOT / 'IMPLEMENTATION.md').read_text()
        for name in ('scripts/verify/25_weaviate_upgrade.sh', 'scripts/verify/weaviate_upgrade.py'):
            with self.subTest(file=name):
                match = re.search(r'^### ' + re.escape(name) + r'\n\n(`{3,})[a-z]*\n(.*?)\n\1$', text, re.S | re.M)
                self.assertIsNotNone(match, 'IMPLEMENTATION.md embeds no copy of ' + name)
                self.assertEqual(match.group(2) + '\n', (ROOT / name).read_text())


if __name__ == '__main__':
    unittest.main()
