"""The Weaviate upgrade path, on throwaway resources of the verify project (#805).

Run by 25_weaviate_upgrade.sh, in full runs only. Host side, standard library, Python 3.9+:

    python3 weaviate_upgrade.py run --checkout DIR --api-image ID --compose-json FILE

FILE is the verify project's `docker compose config --format json`. The check starts the
previous Weaviate tag (RAG_WEAVIATE_FROM, or the tag in origin/main's docker-compose.yml) on a
fresh volume, writes a collection with vectors, stops it, starts the checkout's tag on the same
volume and compares what reads back. It then makes Weaviate take a Raft snapshot, restarts it
and compares again.

Everything it creates, a container, a volume and an internal network, is named
rag-verify-wvup-<hex> and labelled as part of the rag-verify project, so `stack.sh down` removes
whatever a killed run leaves; nothing is published on the host. The requests come from one-off
containers of the verify project's api image, which run this same file in client mode:

    python - client <phase> <host> < weaviate_upgrade.py      (in the api image)

with the weaviate-client the API uses. Images are never removed: the previous tag may be the one
the live stack runs.

Prints one line per check, `PASS<TAB>name` or `FAIL<TAB>name<TAB>detail`, and exits 1 when any
check failed.
"""
import json
import math
import os
import re
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

IMAGE = 'semitechnologies/weaviate'
TAG = re.compile(r'^\d+\.\d+\.\d+$')
COMPOSE_PIN = re.compile(r'^\s+image: ' + re.escape(IMAGE) + r':(\S+)\s*$', re.M)
LABEL_PROJECT = 'com.docker.compose.project=rag-verify'
LABEL_CHECK = 'rag-verify.check=weaviate-upgrade'
# Low enough that a snapshot is taken within the check (compose: 128 and 30).
SNAPSHOT_ENV = {'RAFT_SNAPSHOT_THRESHOLD': '8', 'RAFT_SNAPSHOT_INTERVAL': '2'}
DATA_PATH = '/var/lib/weaviate'
COLLECTION = 'VfyUpgrade'
CHURN_PREFIX = 'VfyUpgradeChurn'
OBJECTS = 12
CHURN = 12
DIMENSIONS = 8
READY_S = 300
SNAPSHOT_S = 180
STOP_GRACE_S = 60
POLL_S = 2
VECTOR_TOLERANCE = 1e-6
DISTANCE_TOLERANCE = 1e-5


class Refusal(Exception):
    """The check can't run as asked; the message says why and what to do."""


class StepFailed(Exception):
    def __init__(self, detail):
        super().__init__(detail)
        self.detail = detail


# ── tags (D2) ────────────────────────────────────────────────────────────────

def _version(tag):
    return tuple(int(part) for part in tag.split('.'))


def git_main_compose(checkout):
    """origin/main's docker-compose.yml, or None when it can't be read. Never fetches."""
    try:
        result = subprocess.run(['git', '-C', str(checkout), 'show', 'origin/main:docker-compose.yml'],
                                capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def resolve_tags(compose, env, main_compose):
    """(from_tag, from_source, to_tag). `main_compose` is a callable giving origin/main's
    docker-compose.yml text, or None."""
    image = str(compose.get('services', {}).get('weaviate', {}).get('image', ''))
    prefix = IMAGE + ':'
    to_tag = image[len(prefix):] if image.startswith(prefix) else ''
    if not TAG.match(to_tag):
        raise Refusal("the checkout's Weaviate image is '{}', not {}:<X.Y.Z>".format(image, IMAGE))
    override = env.get('RAG_WEAVIATE_FROM', '')
    if override:
        from_tag, source = override, 'RAG_WEAVIATE_FROM'
        if not TAG.match(from_tag):
            raise Refusal("RAG_WEAVIATE_FROM must be a plain X.Y.Z Weaviate tag, not '{}'".format(from_tag))
    else:
        text = main_compose()
        match = COMPOSE_PIN.search(text) if text is not None else None
        if match is None:
            raise Refusal("origin/main's docker-compose.yml can't be read or names no {} image; "
                          "set RAG_WEAVIATE_FROM=<X.Y.Z> to the tag to upgrade from".format(IMAGE))
        from_tag, source = match.group(1), 'origin/main'
        if not TAG.match(from_tag):
            raise Refusal("origin/main pins Weaviate '{}', not a plain X.Y.Z tag; "
                          "set RAG_WEAVIATE_FROM=<X.Y.Z>".format(from_tag))
    if _version(from_tag) > _version(to_tag):
        raise Refusal('{} ({}) is newer than the checkout\'s {}: that is not an upgrade'.format(
            from_tag, source, to_tag))
    return from_tag, source, to_tag


def weaviate_env(compose):
    """The verify project's weaviate environment, with the snapshot settings lowered."""
    env = compose.get('services', {}).get('weaviate', {}).get('environment') or {}
    if isinstance(env, list):          # compose normally gives a mapping; accept KEY=VALUE too
        env = dict(item.split('=', 1) if '=' in item else (item, '') for item in env)
    merged = {str(key): '' if value is None else str(value) for key, value in env.items()}
    merged.update(SNAPSHOT_ENV)
    return ['{}={}'.format(key, merged[key]) for key in sorted(merged)]


# ── comparing what reads back (S8) ───────────────────────────────────────────

def compare(baseline, current):
    """None when `current` reads back as `baseline`, else the first difference."""
    if current.get('schema') != baseline.get('schema'):
        return 'schema differs: {} != {}'.format(json.dumps(current.get('schema')),
                                                 json.dumps(baseline.get('schema')))
    if current.get('count') != baseline.get('count'):
        return 'count {} != {}'.format(current.get('count'), baseline.get('count'))
    want, got = baseline.get('objects', {}), current.get('objects', {})
    if sorted(got) != sorted(want):
        return 'object ids differ: {} != {}'.format(sorted(got), sorted(want))
    for key in sorted(want):
        if got[key].get('properties') != want[key].get('properties'):
            return 'object {} properties {} != {}'.format(key, got[key].get('properties'),
                                                          want[key].get('properties'))
        a, b = got[key].get('vector') or [], want[key].get('vector') or []
        if len(a) != len(b) or any(abs(x - y) > VECTOR_TOLERANCE for x, y in zip(a, b)):
            return 'object {} vector {} != {}'.format(key, a, b)
    a, b = current.get('near', []), baseline.get('near', [])
    if [hit[0] for hit in a] != [hit[0] for hit in b]:
        return 'nearVector ids {} != {}'.format([hit[0] for hit in a], [hit[0] for hit in b])
    for (key, x), (_, y) in zip(a, b):
        if abs(x - y) > DISTANCE_TOLERANCE:
            return 'nearVector distance of {} {} != {}'.format(key, x, y)
    return None


# ── Docker ───────────────────────────────────────────────────────────────────

class Docker:
    def run(self, args, input=None, timeout=120):
        try:
            result = subprocess.run(['docker'] + list(args), input=input, capture_output=True,
                                    text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return 124, '', 'docker {} timed out after {}s'.format(' '.join(args[:2]), timeout)
        except OSError as exc:
            return 127, '', str(exc)
        return result.returncode, result.stdout, result.stderr


def _tail(text, lines=5):
    return ' | '.join(line.strip() for line in (text or '').strip().splitlines()[-lines:])


def _one_line(text, limit=800):
    text = re.sub(r'\s+', ' ', str(text)).strip()
    return text if len(text) <= limit else text[:limit] + '...'


class Run:
    def __init__(self, docker, api_image, compose, tags, script, sleep=time.sleep, clock=time.monotonic,
                 name=None, out=sys.stdout):
        self.docker = docker
        self.api_image = api_image
        self.compose = compose
        self.from_tag, self.source, self.to_tag = tags
        self.script = script
        self.sleep = sleep
        self.clock = clock
        self.name = name or 'rag-verify-wvup-' + secrets.token_hex(4)
        self.out = out
        self.failed = False

    # results
    def result(self, name, ok, detail=''):
        if ok:
            self.out.write('PASS\t{}\n'.format(_one_line(name)))
        else:
            self.failed = True
            self.out.write('FAIL\t{}\t{}\n'.format(_one_line(name), _one_line(detail or 'failed')))
        self.out.flush()

    def labels(self):
        return ['--label', LABEL_PROJECT, '--label', LABEL_CHECK]

    # resources
    def cleanup_leftovers(self):
        filters = ['--filter', 'label=' + LABEL_PROJECT, '--filter', 'label=' + LABEL_CHECK]
        for listing, remove in ((['ps', '-aq'], ['rm', '-f']), (['volume', 'ls', '-q'], ['volume', 'rm']),
                                (['network', 'ls', '-q'], ['network', 'rm'])):
            code, out, _ = self.docker.run(listing + filters)
            ids = out.split() if code == 0 else []
            if ids:
                self.docker.run(remove + ids)

    def ensure_image(self, tag):
        image = '{}:{}'.format(IMAGE, tag)
        if self.docker.run(['image', 'inspect', image])[0] == 0:
            return
        code, out, err = self.docker.run(['pull', image], timeout=600)
        if code != 0:
            raise StepFailed('docker pull {} failed: {}'.format(image, _tail(out + '\n' + err)))

    def create(self):
        for args in (['network', 'create', '--internal'] + self.labels() + [self.name],
                     ['volume', 'create'] + self.labels() + [self.name]):
            code, out, err = self.docker.run(args)
            if code != 0:
                raise StepFailed('docker {} failed: {}'.format(' '.join(args[:2]), _tail(out + '\n' + err)))

    def diagnostics(self):
        _, state, _ = self.docker.run(['inspect', '-f', '{{.State.Status}} exit={{.State.ExitCode}}', self.name])
        _, out, err = self.docker.run(['logs', '--tail', '20', self.name])
        return 'state: {}; last log lines: {}'.format(state.strip() or 'unknown', _tail(out + '\n' + err, 20))

    def start(self, tag):
        args = ['run', '-d', '--name', self.name] + self.labels() + ['--network', self.name,
                '-v', '{}:{}'.format(self.name, DATA_PATH)]
        for pair in weaviate_env(self.compose):
            args += ['-e', pair]
        args.append('{}:{}'.format(IMAGE, tag))
        code, out, err = self.docker.run(args, timeout=300)
        if code != 0:
            raise StepFailed('docker run {}:{} failed: {}'.format(IMAGE, tag, _tail(out + '\n' + err)))
        self.wait_ready()

    def wait_ready(self):
        deadline = self.clock() + READY_S
        while True:
            code, _, _ = self.docker.run(['exec', self.name, 'wget', '-q', '--spider',
                                          'http://localhost:8080/v1/.well-known/ready'], timeout=30)
            if code == 0:
                return
            _, running, _ = self.docker.run(['inspect', '-f', '{{.State.Running}}', self.name])
            if running.strip() != 'true':
                raise StepFailed('the container stopped before it was ready; ' + self.diagnostics())
            if self.clock() >= deadline:
                raise StepFailed('not ready within {}s; {}'.format(READY_S, self.diagnostics()))
            self.sleep(POLL_S)

    def stop(self):
        code, out, err = self.docker.run(['stop', '-t', str(STOP_GRACE_S), self.name], timeout=STOP_GRACE_S + 60)
        if code != 0:
            raise StepFailed('docker stop failed: {}'.format(_tail(out + '\n' + err)))

    def client(self, phase):
        args = ['run', '--rm', '-i', '--network', self.name] + self.labels() + [
            self.api_image, 'python', '-', 'client', phase, self.name]
        code, out, err = self.docker.run(args, input=self.script, timeout=600)
        lines = [line for line in out.strip().splitlines() if line.strip()]
        try:
            answer = json.loads(lines[-1]) if lines else None
        except ValueError:
            answer = None
        if code != 0 or not isinstance(answer, dict):
            raise StepFailed('client {} failed (exit {}): {}'.format(phase, code, _tail(err + '\n' + out)))
        return answer

    def version(self, want):
        got = self.client('meta').get('version')
        if got != want:
            raise StepFailed('/v1/meta reports {!r}, wanted {!r}'.format(got, want))

    def snapshots(self):
        code, out, _ = self.docker.run(['exec', self.name, 'find', DATA_PATH, '-path', '*snapshots*',
                                        '-name', 'meta.json'], timeout=60)
        return set(out.split()) if code == 0 else set()

    def raft_listing(self):
        _, out, err = self.docker.run(['exec', self.name, 'find', DATA_PATH + '/raft'], timeout=60)
        return _tail(out + '\n' + err, 40)

    def wait_snapshot(self, before):
        deadline = self.clock() + SNAPSHOT_S
        while True:
            if self.snapshots() - before:
                return
            if self.clock() >= deadline:
                raise StepFailed('no new Raft snapshot within {}s; {} holds: {}'.format(
                    SNAPSHOT_S, DATA_PATH + '/raft', self.raft_listing()))
            self.sleep(POLL_S)

    def remove(self):
        self.docker.run(['rm', '-f', self.name], timeout=120)
        self.docker.run(['volume', 'rm', self.name])
        self.docker.run(['network', 'rm', self.name])
        left = []
        for kind, listing in (('container', ['ps', '-aq']), ('volume', ['volume', 'ls', '-q']),
                              ('network', ['network', 'ls', '-q'])):
            code, out, _ = self.docker.run(listing + ['--filter', 'name=' + self.name,
                                                      '--filter', 'label=' + LABEL_CHECK])
            if code != 0 or out.strip():
                left.append(kind)
        self.result('the throwaway container, volume and network are removed', not left,
                    'left: ' + ', '.join(left) + ' ' + self.name)

    # the steps (S6)
    def steps(self):
        f, t = self.from_tag, self.to_tag

        def step(name, action):
            try:
                action()
            except StepFailed as exc:
                self.result(name, False, exc.detail)
                return False
            self.result(name, True)
            return True

        state = {}

        def first_start():
            self.ensure_image(f)
            self.create()
            self.start(f)
            self.version(f)

        def write():
            state['baseline'] = self.client('write')
            if state['baseline'].get('count') != OBJECTS:
                raise StepFailed('wrote {} objects, {} read back'.format(OBJECTS, state['baseline'].get('count')))

        def upgrade():
            self.stop()
            code, out, err = self.docker.run(['rm', self.name])
            if code != 0:
                raise StepFailed('docker rm failed: {}'.format(_tail(out + '\n' + err)))
            self.start(t)
            self.version(t)

        def same(after):
            def check():
                current = self.client('read')
                difference = compare(state['baseline'], current)
                if difference:
                    raise StepFailed(difference)
                if after:
                    churned = [c for c in current.get('collections', []) if c.startswith(CHURN_PREFIX)]
                    if churned:
                        raise StepFailed('throwaway collections still exist: {}'.format(churned))
            return check

        def snapshot():
            before = self.snapshots()
            answer = self.client('churn')
            if answer.get('churned') != CHURN:
                raise StepFailed('churn answered {}'.format(answer))
            self.wait_snapshot(before)

        def restart():
            self.stop()
            code, out, err = self.docker.run(['start', self.name], timeout=120)
            if code != 0:
                raise StepFailed('docker start failed: {}'.format(_tail(out + '\n' + err)))
            self.wait_ready()
            self.version(t)

        plan = (
            ('{f} starts on a fresh volume and reports {f}', first_start),
            ('data written on {f} reads back ({n} objects with vectors)', write),
            ("{t} starts on {f}'s volume and reports {t}", upgrade),
            ('after the upgrade the schema, count, objects and nearVector match', same(False)),
            ('{t} takes a Raft snapshot after {c} collections are created and deleted', snapshot),
            ('{t} restarts after the snapshot and reports {t}', restart),
            ('after the restart the data matches and the throwaway collections are gone', same(True)),
        )
        for name, action in plan:
            if not step(name.format(f=f, t=t, n=OBJECTS, c=CHURN), action):
                return

    def run(self):
        self.cleanup_leftovers()
        try:
            self.steps()
        finally:
            self.remove()
        return 1 if self.failed else 0


def host_main(argv, docker=None, env=None, main_compose=None, out=sys.stdout):
    import argparse
    parser = argparse.ArgumentParser(prog='weaviate_upgrade.py')
    parser.add_argument('command', choices=['run'])
    parser.add_argument('--checkout', required=True)
    parser.add_argument('--api-image', required=True)
    parser.add_argument('--compose-json', required=True)
    args = parser.parse_args(argv)
    env = os.environ if env is None else env
    compose = json.loads(Path(args.compose_json).read_text())
    if main_compose is None:
        def main_compose():
            return git_main_compose(args.checkout)
    try:
        tags = resolve_tags(compose, env, main_compose)
    except Refusal as exc:
        out.write('FAIL\tthe upgrade path to the checkout\'s Weaviate\t{}\n'.format(_one_line(exc)))
        return 1
    f, source, t = tags
    out.write('PASS\tupgrade path {} ({}) -> {} (checkout){}\n'.format(
        f, source, t, ': same version' if f == t else ''))
    out.flush()
    script = Path(__file__).read_text()
    return Run(docker or Docker(), args.api_image, compose, tags, script, out=out).run()


# ── client mode, in the api image ────────────────────────────────────────────

def _http_json(host, path):
    import urllib.request
    with urllib.request.urlopen('http://{}:8080{}'.format(host, path), timeout=60) as response:
        return json.loads(response.read().decode())


def _connect(host):
    import weaviate
    deadline = time.monotonic() + 60
    while True:
        try:
            return weaviate.connect_to_custom(http_host=host, http_port=8080, http_secure=False,
                                              grpc_host=host, grpc_port=50051, grpc_secure=False)
        except Exception:                          # noqa: BLE001 — gRPC can lag /ready
            if time.monotonic() >= deadline:
                raise
            time.sleep(2)


def _vector(i):
    return [math.sin(i + k) for k in range(DIMENSIONS)]


def _read(host, client):
    from weaviate.classes.query import MetadataQuery
    schema = _http_json(host, '/v1/schema/' + COLLECTION)
    projection = {
        'class': schema.get('class'),
        'properties': sorted([p.get('name'), p.get('dataType')] for p in schema.get('properties', [])),
        'vectorizer': schema.get('vectorizer'),
        'vectorIndexType': schema.get('vectorIndexType'),
        'distance': (schema.get('vectorIndexConfig') or {}).get('distance'),
    }
    coll = client.collections.get(COLLECTION)
    count = coll.aggregate.over_all(total_count=True).total_count
    objects = {}
    for i in range(OBJECTS):
        key = str(uuid.UUID(int=i + 1))
        obj = coll.query.fetch_object_by_id(key, include_vector=True)
        if obj is None:
            objects[key] = {'properties': None, 'vector': None}
            continue
        vector = obj.vector
        if isinstance(vector, dict):
            vector = vector.get('default', next(iter(vector.values()), None))
        objects[key] = {'properties': json.loads(json.dumps(obj.properties, sort_keys=True, default=str)),
                        'vector': list(vector) if vector is not None else None}
    hits = coll.query.near_vector(near_vector=[math.cos(k) for k in range(DIMENSIONS)], limit=5,
                                  return_metadata=MetadataQuery(distance=True)).objects
    near = [[str(hit.uuid), hit.metadata.distance] for hit in hits]
    collections = sorted(c.get('class') for c in _http_json(host, '/v1/schema').get('classes', []))
    return {'schema': projection, 'count': count, 'objects': objects, 'near': near, 'collections': collections}


def client_main(argv):
    phase, host = argv[0], argv[1]
    if phase == 'meta':
        print(json.dumps({'version': _http_json(host, '/v1/meta').get('version')}))
        return 0
    from datetime import datetime, timedelta, timezone
    from weaviate.classes.config import Configure, DataType, Property, VectorDistances
    from weaviate.classes.data import DataObject
    client = _connect(host)
    try:
        if phase == 'write':
            # As weaviate_client._create_collection_sync creates a collection, with its defaults.
            client.collections.create(
                name=COLLECTION,
                vectorizer_config=Configure.Vectorizer.text2vec_ollama(
                    # Never called: every object carries its vector.
                    api_endpoint='http://ollama:11434', model='nomic-embed-text',
                    vectorize_collection_name=False),
                vector_index_config=Configure.VectorIndex.hnsw(distance_metric=VectorDistances.COSINE,
                                                               ef_construction=128, max_connections=64, ef=64),
                properties=[
                    Property(name='content', data_type=DataType.TEXT, index_searchable=True, index_filterable=True),
                    Property(name='source_file', data_type=DataType.TEXT, index_searchable=False, index_filterable=True),
                    Property(name='source_type', data_type=DataType.TEXT, index_searchable=False, index_filterable=True),
                    Property(name='chunk_index', data_type=DataType.INT, index_filterable=True),
                    Property(name='chunk_strategy', data_type=DataType.TEXT, index_searchable=False, index_filterable=True),
                    Property(name='chunk_size', data_type=DataType.INT, index_filterable=True),
                    Property(name='chunk_overlap', data_type=DataType.INT, index_filterable=True),
                    Property(name='created_at', data_type=DataType.DATE, index_filterable=True),
                ])
            start = datetime(2026, 1, 1, tzinfo=timezone.utc)
            objects = [DataObject(uuid=uuid.UUID(int=i + 1), vector=_vector(i), properties={
                'content': 'Upgrade check chunk {} of the Weaviate upgrade path.'.format(i),
                'source_file': 'upgrade-{}.txt'.format(i % 3), 'source_type': 'txt',
                'chunk_index': i, 'chunk_strategy': 'fixed', 'chunk_size': 400, 'chunk_overlap': 40,
                'created_at': start + timedelta(days=i)}) for i in range(OBJECTS)]
            outcome = client.collections.get(COLLECTION).data.insert_many(objects)
            if outcome.has_errors:
                print('insert errors: {}'.format(getattr(outcome, 'errors', outcome)), file=sys.stderr)
                return 1
            print(json.dumps(_read(host, client)))
        elif phase == 'read':
            print(json.dumps(_read(host, client)))
        elif phase == 'churn':
            for i in range(CHURN):
                name = '{}{}'.format(CHURN_PREFIX, i)
                client.collections.create(name=name, vectorizer_config=Configure.Vectorizer.none())
                client.collections.delete(name)
            print(json.dumps({'churned': CHURN}))
        else:
            print('unknown phase {!r}'.format(phase), file=sys.stderr)
            return 2
    finally:
        client.close()
    return 0


if __name__ == '__main__':
    if len(sys.argv) > 1 and sys.argv[1] == 'client':
        sys.exit(client_main(sys.argv[2:]))
    sys.exit(host_main(sys.argv[1:]))
