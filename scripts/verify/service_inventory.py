"""Exact service inventory for the default and guarded telemetry verify modes."""
import argparse
from collections import Counter
import json
from pathlib import Path

BASE = frozenset(('api', 'ollama', 'proxy', 'ui', 'weaviate'))
TELEMETRY = frozenset(('otel-collector', 'otel-capture'))


def check(config, running, all_services, mode='0', project='', profiles=''):
    if mode not in ('0', '1'):
        return ['unknown telemetry verification mode']
    if mode == '1' and (project != 'rag-verify' or config.get('name') != 'rag-verify'
                        or 'telemetry' not in profiles.split(',')):
        return ['telemetry inventory requires the guarded rag-verify project and profile']
    expected = BASE | (TELEMETRY if mode == '1' else frozenset())
    problems = []
    if set(config.get('services', {})) != expected:
        problems.append('configured service inventory differs from the selected verification mode')
    # Counter equality rejects replacements, unexpected services and duplicate
    # containers rather than merely accepting any five/seven running names.
    wanted = Counter({name: 1 for name in expected})
    if Counter(all_services) != wanted:
        problems.append('project service inventory has missing, duplicate or unexpected services')
    if Counter(running) != wanted:
        problems.append('running service inventory differs from the required exact set')
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('config', type=Path)
    parser.add_argument('running', type=Path)
    parser.add_argument('all_services', type=Path)
    parser.add_argument('--mode', default='0')
    parser.add_argument('--project', default='')
    parser.add_argument('--profiles', default='')
    args = parser.parse_args()
    problems = check(json.loads(args.config.read_text()), args.running.read_text().splitlines(),
                     args.all_services.read_text().splitlines(), args.mode, args.project, args.profiles)
    for problem in problems:
        print(problem)
    raise SystemExit(bool(problems))


if __name__ == '__main__':
    main()
