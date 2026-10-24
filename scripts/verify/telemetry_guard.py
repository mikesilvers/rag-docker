"""Additional service-specific restrictions for the optional disposable collector."""
import json
from pathlib import Path

KEYS = {'profiles', 'mem_limit', 'cpus', 'stop_grace_period', 'read_only', 'logging'}
SERVICES = {'otel-collector', 'otel-capture'}


def check(config, root):
    problems = []
    services = config['services']
    pin = json.loads((Path(root) / 'telemetry/image.json').read_text())['image']
    for name in SERVICES & services.keys():
        svc = services[name]
        expected_image = pin if name == 'otel-collector' else 'rag-verify-api:latest'
        if svc.get('image') != expected_image or 'build' in svc:
            problems.append(f'{name}: unexpected telemetry image/build')
        if (svc.get('profiles') != ['telemetry'] or str(svc.get('mem_limit')) != '268435456'
                or svc.get('cpus') != 0.5 or svc.get('stop_grace_period') != '10s'
                or svc.get('read_only') is not True):
            problems.append(f'{name}: telemetry limits differ from approved bounds')
        if svc.get('logging') != {'driver': 'json-file', 'options': {'max-size': '5m', 'max-file': '2'}}:
            problems.append(f'{name}: unbounded telemetry logs')
        expected_command = (['--config=/etc/otelcol/config.yaml'] if name == 'otel-collector'
                            else ['python', '/verify/telemetry_capture.py'])
        if svc.get('command') != expected_command or svc.get('entrypoint') is not None:
            problems.append(f'{name}: telemetry command differs from harness contract')
        expected_target = '/etc/otelcol/config.yaml' if name == 'otel-collector' else '/verify'
        expected_source = Path(root) / 'scripts/verify'
        if name == 'otel-collector':
            expected_source /= 'collector.yaml'
        mounts = [m for m in svc.get('volumes', []) if m.get('type') == 'bind']
        if (len(mounts) != 1 or mounts[0].get('target') != expected_target
                or mounts[0].get('source') != str(expected_source)
                or Path(mounts[0].get('source', '/')).resolve() != expected_source
                or mounts[0].get('read_only') is not True):
            problems.append(f'{name}: expected exact read-only harness asset')
        if svc.get('ports') or svc.get('depends_on'):
            problems.append(f'{name}: telemetry has published ports/dependencies')
    for name, svc in services.items():
        if name not in SERVICES and SERVICES & set(svc.get('depends_on') or {}):
            problems.append(f'{name}: collector is in the application dependency chain')
    return problems
