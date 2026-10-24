"""Host controller for real disposable API -> Collector -> protobuf capture.

Uses only stdlib on the host. The private sink decodes protobuf in the API image.
Every mutation is limited to the explicit guarded rag-verify project.
"""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import os
from pathlib import Path
import statistics
import runpy
import threading
import subprocess
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[2]
API = os.environ.get('RAG_API', '')
SCHEMA = runpy.run_path(str(ROOT / 'api/services/telemetry.py'))
SENTINELS = ['OTEL285_SECRET_SENTINEL', 'OTEL285_CONTENT_SENTINEL', 'OTEL285_FILENAME_SENTINEL', 'OTEL285_PATH_SENTINEL']


def owner():
    subprocess.run(['bash', '-c', 'source "$1/scripts/verify/lock.sh"; _rag_lock_owner_ok',
                    'telemetry-lock', str(ROOT)], check=True, stdout=subprocess.DEVNULL)


def compose(*args, timeout=120, capture_stderr=False):
    owner()
    return subprocess.check_output(['docker', 'compose', '-p', 'rag-verify', *args],
                                   text=True, timeout=timeout, stderr=subprocess.PIPE if capture_stderr else None)


def safe_failure(exc):
    """Only typed numeric diagnostics; never exporter output, URLs, bodies or argv."""
    result = {'type': type(exc).__name__[:64]}
    if isinstance(exc, subprocess.CalledProcessError):
        result['exit_code'] = exc.returncode
    elif isinstance(exc, subprocess.TimeoutExpired):
        result['timeout_seconds'] = exc.timeout
    elif isinstance(exc, urllib.error.HTTPError):
        result['http_status'] = exc.code
    return result


def stats_snapshot(collector_running):
    result = {}
    # Compose stats accepts one SERVICE argument (unlike docker stats).
    # Explicit per-service calls also establish which required sample was absent.
    for service in ('api', 'otel-collector'):
        if service == 'otel-collector' and not collector_running:
            result[service] = {'state': 'stopped_by_test'}
            continue
        raw = compose('stats', '--no-stream', '--format', 'json', service,
                      timeout=8, capture_stderr=True)
        if len(raw) > 8192:
            raise ValueError('oversized resource sample')
        try:
            rows = json.loads(raw)
        except json.JSONDecodeError:
            rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            raise ValueError('missing or ambiguous resource sample')
        # Docker's stats fields contain measurements. Keep only the expected
        # numeric display fields; container names and other metadata are unnecessary.
        fields = ('CPUPerc', 'MemUsage', 'MemPerc', 'PIDs', 'BlockIO', 'NetIO')
        sample = {k: rows[0][k] for k in fields if k in rows[0]}
        if not all(k in sample for k in ('CPUPerc', 'MemUsage')):
            raise ValueError('CPU or memory resource evidence missing')
        if any(not isinstance(v, str) or len(v) > 128 for v in sample.values()):
            raise ValueError('malformed resource measurement')
        result[service] = sample
    return result


@contextmanager
def resource_samples(collector_running):
    measurements, failures = [], []
    stopped = threading.Event()
    def sample_stats():
        while not stopped.is_set():
            try:
                measurements.append({'monotonic': time.monotonic(),
                                     'stats': stats_snapshot(collector_running)})
            except Exception as exc:
                failures.append(safe_failure(exc))
                return
            stopped.wait(5)
    sampler = threading.Thread(target=sample_stats, daemon=True)
    sampler.start()
    primary = None
    try:
        yield measurements
    except BaseException as exc:
        primary = exc
        raise
    finally:
        stopped.set()
        sampler.join(20)
        if sampler.is_alive():
            failures.append({'type': 'SamplerJoinTimeout', 'timeout_seconds': 20})
        if not measurements and not failures:
            failures.append({'type': 'NoResourceSamples'})
        if failures:
            detail = 'resource sampling failed: ' + json.dumps(failures[:4], sort_keys=True)
            if primary is not None:
                primary.add_note(detail)
            else:
                raise AssertionError(detail) from None


def cleanup(actions, primary=None):
    failures = []
    for stage, action in actions:
        try:
            action()
        except Exception as exc:
            failures.append({'stage': stage, **safe_failure(exc)})
    if failures:
        detail = 'telemetry cleanup failed: ' + json.dumps(failures[:4], sort_keys=True)
        if primary is not None:
            primary.add_note(detail)
        else:
            raise AssertionError(detail) from None


def sink(path, post=False):
    code = ('import urllib.request; print(urllib.request.urlopen(urllib.request.Request('
            + repr('http://127.0.0.1:4319' + path) + ',method=' + repr('POST' if post else 'GET')
            + '),timeout=10).read().decode())')
    value = compose('exec', '-T', 'otel-capture', 'python', '-c', code)
    return json.loads(value) if value.strip() else None


def request(path, body=None, headers=None, method=None):
    owner()
    data = json.dumps(body).encode() if body is not None else None
    h = {'Content-Type': 'application/json', 'Authorization': 'Bearer '+SENTINELS[0],
         'Cookie': 'session='+SENTINELS[0], **(headers or {})}
    with urllib.request.urlopen(urllib.request.Request(API+path, data=data, headers=h, method=method),
                                timeout=600) as response:
        return json.load(response)


def trace_headers():
    remote = uuid.uuid4().hex
    return {'traceparent': f'00-{remote}-0123456789abcdef-01'}, base64.b64encode(bytes.fromhex(remote)).decode()


def upload(collection):
    owner()
    h, remote = trace_headers()
    boundary = uuid.uuid4().hex
    fields = {'collection': collection, 'strategy': 'fixed', 'chunk_size': '200',
              'chunk_overlap': '20', 'min_chunk_size': '20'}
    chunks = []
    for name, value in fields.items():
        chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n')
    chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="{SENTINELS[2]}.txt"\r\nContent-Type: text/plain\r\n\r\nManagers approve overtime. {SENTINELS[1]}. Synthetic path /private/{SENTINELS[3]}. The policy requires approval before work.\r\n--{boundary}--\r\n')
    data = ''.join(chunks).encode()
    with urllib.request.urlopen(urllib.request.Request(API+'/ingest/upload', data=data,
            headers={**h, 'Content-Type': 'multipart/form-data; boundary='+boundary,
                     'Authorization': 'Bearer '+SENTINELS[0]}), timeout=120) as r:
        job = json.load(r)['job_id']
    wait_job('/ingest/job/'+job)
    return remote


def wait_job(path):
    deadline = time.monotonic()+900
    while time.monotonic() < deadline:
        data = request(path)
        if data['status'] in ('completed', 'failed', 'cancelled'):
            assert data['status'] == 'completed', 'synthetic job did not complete'
            return
        time.sleep(0.5)
    raise AssertionError('synthetic job exceeded 900s')


def export(collection):
    h, remote = trace_headers()
    job = request('/export', {'collection': collection, 'include_models': False}, h)['job_id']
    wait_job('/export/job/'+job)
    return remote


def query(collection):
    h, remote = trace_headers()
    result = request('/query', {'question': 'Who approves overtime? '+SENTINELS[1],
        'collection': collection, 'retrieval_mode': 'hnsw', 'top_k': 2, 'include_citations': False}, h)
    assert result.get('answer', '').strip(), 'synthetic query has no answer'
    return remote


def records(snapshot, signal):
    resource, scope, field = {'traces': ('resource_spans', 'scope_spans', 'spans'),
        'logs': ('resource_logs', 'scope_logs', 'log_records'),
        'metrics': ('resource_metrics', 'scope_metrics', 'metrics')}[signal]
    assert isinstance(snapshot, dict), 'invalid capture snapshot'
    batches = snapshot.get('batches')
    assert isinstance(batches, list), 'invalid capture batches'
    result = []
    for batch in batches:
        assert isinstance(batch, dict), 'invalid capture batch'
        assert batch.get('signal') in ('traces', 'logs', 'metrics'), 'invalid capture signal'
        data = batch.get('data')
        assert isinstance(data, dict), 'invalid capture data'
        if batch['signal'] != signal:
            continue
        resources = data.get(resource, [])
        assert isinstance(resources, list), 'invalid capture resources'
        for res in resources:
            assert isinstance(res, dict), 'invalid capture resource'
            scopes = res.get(scope, [])
            assert isinstance(scopes, list), 'invalid capture scopes'
            for sc in scopes:
                assert isinstance(sc, dict), 'invalid capture scope'
                rows = sc.get(field, [])
                assert isinstance(rows, list) and all(isinstance(row, dict) for row in rows), 'invalid capture records'
                result.extend(rows)
    return result


def inspect(snapshot, requests):
    try:
        return inspect_records(snapshot, requests)
    except (TypeError, AttributeError, KeyError) as exc:
        raise AssertionError('invalid capture evidence shape') from None


def inspect_records(snapshot, requests):
    assert isinstance(snapshot, dict), 'invalid capture snapshot'
    assert snapshot['overflow'] == 0, 'capture overflow: inspection incomplete'
    wire = json.dumps(snapshot)
    assert all(s not in wire for s in SENTINELS), 'content/credential sentinel exported'
    spans, logs, metrics = (records(snapshot, s) for s in ('traces', 'logs', 'metrics'))
    identities = [(span['trace_id'], span['span_id']) for span in spans]
    assert len(set(identities)) == len(identities), 'duplicate received span identity'
    for item in [*spans, *logs]:
        if 'name' in item:
            assert item['name'] in SCHEMA['SPAN_NAMES'], 'unsafe span name'
        if 'body' in item:
            assert item['body'].get('string_value') in SCHEMA['LOG_BODIES'], 'unsafe log body'
        for attr in item.get('attributes', []):
            key, value = attr['key'], attr['value'].get('string_value')
            if key == 'rag.job_token':
                import re
                assert re.fullmatch('[0-9a-f]{32}', value or '')
            else:
                assert value in SCHEMA['ENUMS'].get(key, ()), 'unsafe trace/log attribute'
    names = {m['name'] for m in metrics}
    assert names <= SCHEMA['METRIC_NAMES'], 'unsafe metric name'
    assert {'rag.api.requests', 'rag.api.duration', 'rag.dependency.calls', 'rag.job.completed'} <= names
    for metric in metrics:
        expected_kind = 'histogram' if SCHEMA['METRICS'][metric['name']][0] == 'create_histogram' else 'sum'
        assert expected_kind in metric, 'metric aggregation schema mismatch'
        assert any(metric.get(k, {}).get('data_points') for k in ('sum', 'histogram')), 'empty metric payload'
        for kind in ('sum', 'histogram'):
            for point in metric.get(kind, {}).get('data_points', []):
                assert not point.get('exemplars'), 'unexpected exemplar identity'
                assert {a['key'] for a in point.get('attributes', [])} == set(SCHEMA['METRICS'][metric['name']][2]), 'metric label schema mismatch'
                for attr in point.get('attributes', []):
                    assert attr['key'] in SCHEMA['METRICS'][metric['name']][2]
                    assert attr['value'].get('string_value') in SCHEMA['metric_values'](metric['name'], attr['key'])
    roots, job_tokens = [], []
    for remote, operation in requests:
        candidates = [s for s in spans if any(l.get('trace_id') == remote for l in s.get('links', []))]
        assert len(candidates) == 1, f'expected one linked request root for {operation}'
        root = candidates[0]
        roots.append(root['trace_id'])
        tree = {s['span_id']: s for s in spans if s['trace_id'] == root['trace_id']}
        target = [s for s in tree.values() if s['name'] == operation]
        assert target, f'missing {operation} worker/operation span'
        for item in target:
            seen = set()
            node = item
            while node['span_id'] != root['span_id']:
                assert node['span_id'] not in seen, 'cyclic span tree'
                seen.add(node['span_id'])
                node = tree[node['parent_span_id']]
            correlated = [l for l in logs if l.get('trace_id') == item['trace_id']
                          and l.get('span_id') == item['span_id']]
            if operation != 'rag.query':
                job_logs = [l for l in correlated if l.get('body', {}).get('string_value') == 'rag.job.completed']
                assert job_logs, 'missing correlated job completion'
                tokens = {a['value']['string_value'] for l in job_logs for a in l.get('attributes', [])
                          if a['key'] == 'rag.job_token'}
                assert len(tokens) == 1, 'job has no stable generated correlation token'
                token = tokens.pop()
                job_tokens.append(token)
                for log in logs:
                    if log.get('trace_id') == item['trace_id']:
                        for attr in log.get('attributes', []):
                            if attr['key'] == 'rag.job_token':
                                assert attr['value']['string_value'] == token, 'job context crossed trace trees'
        dependencies = [s for s in tree.values() if s['name'].startswith(('ollama.', 'weaviate.'))]
        assert dependencies, 'missing dependency spans'
        for operation_span in target:
            connected = False
            for dependency in dependencies:
                cursor, visited = dependency, set()
                while cursor.get('parent_span_id') in tree and cursor['span_id'] not in visited:
                    visited.add(cursor['span_id'])
                    cursor = tree[cursor['parent_span_id']]
                    if cursor['span_id'] == operation_span['span_id']:
                        connected = True
                        break
            assert connected, 'dependency has no parent chain through operation/worker'
        assert any(l.get('trace_id') == root['trace_id'] and l.get('span_id') == root['span_id']
                   and l.get('body', {}).get('string_value') == 'rag.api.completed' for l in logs)
    assert len(roots) == len(set(roots)), 'concurrent requests share a local trace'
    assert len(job_tokens) == len(set(job_tokens)), 'concurrent jobs share a correlation token'


def await_capture(expected):
    deadline = time.monotonic()+30
    last = None
    while time.monotonic() < deadline:
        data = sink('/snapshot')
        try:
            inspect(data, expected)
            return data
        except (AssertionError, KeyError) as exc:
            last = exc
        time.sleep(1)
    raise AssertionError(f'capture incomplete: {last}')


def enabled(value):
    owner()
    harness = os.environ['RAG_VERIFY_HARNESS']
    args = ['bash', harness+'/scripts/verify/stack.sh', 'telemetry-mode', '--telemetry',
            '--checkout', os.environ['RAG_VERIFY_CHECKOUT']]
    if not value:
        args.append('--disabled')
    subprocess.run(args, check=True, timeout=150, stdout=subprocess.DEVNULL)


def transition(mode):
    enabled(mode != 'disabled')
    if mode == 'disabled':
        # API replacement terminates the old producer. Terminate both the old
        # collector queues and capture handlers before opening the zero-traffic
        # window; elapsed quiet time alone cannot prove a drained pipeline.
        compose('stop', '-t', '10', 'otel-collector')
        compose('restart', '-t', '10', 'otel-capture')
        deadline = time.monotonic() + 15
        while True:
            try:
                sink('/reset', True)
                break
            except (OSError, subprocess.SubprocessError):
                if time.monotonic() >= deadline:
                    raise AssertionError('capture unavailable after disabled transition') from None
                time.sleep(0.2)
        compose('start', 'otel-collector')
    else:
        time.sleep(3)
        sink('/reset', True)


def samples(collections):
    for _ in range(5):
        request('/health')
    durations = []
    for _ in range(20):
        start = time.monotonic()
        request('/health')
        durations.append(time.monotonic()-start)
    start = time.monotonic()
    q = query(collections[0])
    query_seconds = time.monotonic()-start
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = list(pool.map(export, collections))
    return {'health_samples_seconds': durations, 'health_median_seconds': statistics.median(durations),
            'health_max_seconds': max(durations), 'query_seconds': query_seconds,
            'two_jobs_seconds': time.monotonic()-start}, [(q, 'rag.query'), *[(x, 'rag.export') for x in jobs]]


def main():
    assert os.environ.get('COMPOSE_PROJECT_NAME') == 'rag-verify'
    assert os.environ.get('RAG_VERIFY_TELEMETRY') == '1'
    assert API.startswith('http://localhost:') and API != 'http://localhost:8080/api'
    owner()
    config = json.loads(compose('config', '--format', 'json'))
    assert config['name'] == 'rag-verify'
    for service in ('otel-collector', 'otel-capture'):
        assert not config['services'][service].get('ports')
        cid = compose('ps', '-q', service).strip()
        assert cid
        runtime = json.loads(subprocess.check_output(['docker', 'inspect', cid]))[0]
        assert not runtime['HostConfig']['PortBindings']
        assert runtime['Config']['Labels']['com.docker.compose.project'] == 'rag-verify'
    actual = compose('exec', '-T', 'api', 'python', '-c',
        'import os; assert os.environ["RAG_OTEL_ENABLED"] == "true"; assert os.environ["RAG_OTEL_ENDPOINT"] == "http://otel-collector:4318"')
    collections = ['VfyOtel'+uuid.uuid4().hex[:12] for _ in range(2)]
    report = {'workload': '5 warm-up +20 timed health requests,1 real query,2 concurrent export jobs per mode',
              'limits': 'small synthetic sample; health timings include host lock checks; no production SLO claim',
              'modes': {}}
    primary = None
    try:
        for c in collections:
            request('/collections', {'name': c, 'index_type': 'hnsw', 'distance_metric': 'cosine',
                    'hnsw_config': {'efConstruction': 128, 'maxConnections': 64, 'ef': 64}})
        sink('/reset', True)
        with ThreadPoolExecutor(max_workers=2) as pool:
            ingest = list(pool.map(upload, collections))
        await_capture([(r, 'rag.ingest') for r in ingest])
        for mode in ('healthy', 'disabled', 'unavailable', 'slow'):
            transition(mode)
            if mode == 'unavailable':
                compose('stop', '-t', '10', 'otel-collector')
            if mode == 'slow':
                sink('/slow', True)
            with resource_samples(mode != 'unavailable') as statistics_samples:
                result, expected = samples(collections)
            result['container_stats_samples'] = statistics_samples
            if mode == 'healthy':
                captured = await_capture(expected)
                result['captured_batches'] = len(captured['batches'])
            elif mode == 'disabled':
                time.sleep(4)
                assert sink('/snapshot')['requests'] == 0, 'disabled application exported telemetry'
            report['modes'][mode] = result
            if mode == 'unavailable':
                compose('start', 'otel-collector')
            if mode == 'slow':
                sink('/healthy', True)
        # Healthy final buffered work, bounded stop and clean fresh request trees.
        time.sleep(12)
        assert all(s not in json.dumps(sink('/snapshot')) for s in SENTINELS), 'outage recovery leaked sentinel'
        sink('/reset', True)
        remote = query(collections[0])
        start = time.monotonic()
        compose('stop', '-t', '10', 'api')
        report['api_stop_seconds'] = time.monotonic()-start
        assert report['api_stop_seconds'] < 15
        await_capture([(remote, 'rag.query')])
        compose('start', 'api')
        compose('restart', '-t', '10', 'otel-collector')
        deadline = time.monotonic()+120
        while True:
            try:
                request('/health')
                break
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)
        time.sleep(3)
        sink('/reset', True)
        _, expected = samples(collections)
        await_capture(expected)
        print(json.dumps(report, indent=2))
    except BaseException as exc:
        primary = exc
        raise
    finally:
        cleanup([('receiver', lambda: sink('/healthy', True)),
                 ('collector', lambda: compose('start', 'otel-collector')),
                 ('api_mode', lambda: enabled(True)),
                 *[('collection', lambda c=c: request('/collections/'+c+'?confirm=true', method='DELETE'))
                   for c in collections]], primary)


if __name__ == '__main__':
    main()
