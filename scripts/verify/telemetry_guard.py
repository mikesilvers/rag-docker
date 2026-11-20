"""Additional service-specific restrictions for the optional disposable collector."""
import json
from pathlib import Path
import re

KEYS = {'profiles', 'mem_limit', 'cpus', 'stop_grace_period', 'read_only', 'logging'}
SERVICES = {'otel-collector', 'otel-capture'}


def repository(image):
    """The repository of <repository>:<tag>@sha256:<digest>, or None (#532)."""
    match = re.fullmatch(r'(.+):([\w][\w.-]{0,127})@sha256:[0-9a-f]{64}', image)
    return match.group(1) if match else None


def collector_image(root, pin, problems):
    """The collector image to expect: the harness's own pin, or a reviewed pin
    the evaluator supplied with stack.sh --telemetry-pin (#532). A reviewed
    pin may change only the tag and digest, never the repository."""
    harness = json.loads((Path(root) / 'telemetry/image.json').read_text())['image']
    if not pin:
        return harness
    try:
        reviewed = json.loads(Path(pin).read_text())
    except (OSError, ValueError):
        reviewed = None
    if not isinstance(reviewed, dict) or not isinstance(reviewed.get('image'), str):
        problems.append("otel-collector: the reviewed telemetry pin can't be read")
        return harness
    image = reviewed['image']
    if repository(image) is None:
        problems.append('otel-collector: the reviewed telemetry pin is not <repository>:<tag>@sha256:<digest>')
        return harness
    if repository(image) != repository(harness):
        problems.append("otel-collector: the reviewed telemetry pin's repository differs from the harness pin")
        return harness
    return image


def check(config, root, pin=None):
    problems = []
    services = config['services']
    collector = collector_image(root, pin, problems)
    for name in SERVICES & services.keys():
        svc = services[name]
        expected_image = collector if name == 'otel-collector' else 'rag-verify-api:latest'
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
