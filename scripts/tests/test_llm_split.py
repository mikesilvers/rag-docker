"""Host-only check of suite 23's model pull (#922).

`scripts/verify/llm_split.py` pulls the build model into the verify project's
Ollama before its three tasks. A 5xx is tried again (3 attempts, waiting 5 s
and then 10 s); a 4xx or a request error is not; a pull that fails runs no
task and is reported as `setup_error`. `23_llm_split.sh` turns that into one
failed check instead of a failure of every check that reads the run.

In external-Ollama mode (#673) the script refuses, exit 2, before any request.

The script runs in this process with stand-ins for httpx, config, services and
time.sleep, and the suite's part that reads the script's output runs under bash
with stand-in check helpers and docker. Standard library only; no Docker or
stack needed. Registered by 23_llm_split.sh.

Run: python3 scripts/tests/test_llm_split.py
"""
import contextlib
import io
import json
import os
import runpy
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/verify/llm_split.py'
SUITE = ROOT / 'scripts/verify/23_llm_split.sh'
BUILD = 'qwen2.5:0.5b'
ANSWER = 'llama3.1:8b'


class FakeResponse:
    def __init__(self, status, text=''):
        self.status_code = status
        self.text = text
        self.is_success = 200 <= status < 300


def fake_modules(answers, record, external=False):
    """httpx, config and services stand-ins; `answers` are what the pulls get."""
    httpx = types.ModuleType('httpx')

    class HTTPError(Exception):
        pass

    class ConnectError(HTTPError):
        pass

    class AsyncClient:
        async def post(self, url, *args, **kwargs):
            raise AssertionError('no LLM call is made by the stand-in tasks')

    def post(url, json=None, timeout=None):
        record['pulls'].append((url, json, timeout))
        answer = answers.pop(0)
        if answer == 'connect-error':
            raise ConnectError('connection refused')
        return FakeResponse(answer, '{"error":"pull failed"}' if answer >= 400 else '{"status":"success"}')

    def request(method, url, json=None, timeout=None):
        record['deletes'].append((method, url, json))
        return FakeResponse(200)

    httpx.HTTPError, httpx.ConnectError, httpx.AsyncClient = HTTPError, ConnectError, AsyncClient
    httpx.post, httpx.request = post, request

    config = types.ModuleType('config')
    config.settings = types.SimpleNamespace(ollama_host='ollama', ollama_port=11434,
                                            build_model=BUILD, answer_model=ANSWER,
                                            rag_external_ollama=external)

    def ran(name):
        async def run(*args, **kwargs):
            record['tasks'].append(name)
        return run

    services = types.ModuleType('services')
    services.preparation_jobs = types.SimpleNamespace(_llm=lambda: (None, None))
    services.preparation = types.SimpleNamespace(prepare_document=ran('prepare'))
    services.goldstandard = types.SimpleNamespace(_generate_pair=ran('goldstandard'))
    services.rag_pipeline = types.SimpleNamespace(run_query=ran('query'))
    return {'httpx': httpx, 'config': config, 'services': services}


def run_script(answers):
    record = {'pulls': [], 'deletes': [], 'tasks': []}
    stdout, stderr = io.StringIO(), io.StringIO()
    with mock.patch.dict(sys.modules, fake_modules(list(answers), record)), \
            mock.patch.dict(os.environ, {'RAG_LLM_SPLIT_COLLECTION': 'VfyLlmSplit'}), \
            mock.patch('time.sleep') as sleep, \
            contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        runpy.run_path(str(SCRIPT), run_name='__main__')
    record['waits'] = [call.args[0] for call in sleep.call_args_list]
    record['out'] = json.loads(stdout.getvalue().strip().splitlines()[-1])
    record['stderr'] = stderr.getvalue()
    return record


class ExternalOllamaRefusalTests(unittest.TestCase):
    """In external-Ollama mode (#673) the script refuses before any request: that
    Ollama is the host's own and shared, so rag-docker never pulls or deletes a
    model in it. (Suite 23 skips in that mode before it runs the script.)"""

    def test_it_exits_2_before_any_pull_or_delete(self):
        record = {'pulls': [], 'deletes': [], 'tasks': []}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, fake_modules([200], record, external=True)), \
                mock.patch.dict(os.environ, {'RAG_LLM_SPLIT_COLLECTION': 'VfyLlmSplit'}), \
                mock.patch('time.sleep'), \
                contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                runpy.run_path(str(SCRIPT), run_name='__main__')
        self.assertEqual(caught.exception.code, 2)
        self.assertEqual(record, {'pulls': [], 'deletes': [], 'tasks': []})
        self.assertEqual(stdout.getvalue(), '')
        self.assertIn('refused in external-Ollama mode (RAG_EXTERNAL_OLLAMA=true)', stderr.getvalue())
        self.assertIn('rag-docker never writes models to it (#673)', stderr.getvalue())


class PullTests(unittest.TestCase):
    def assert_pulled_once_each_attempt(self, record, attempts):
        self.assertEqual(len(record['pulls']), attempts)
        for url, body, timeout in record['pulls']:
            self.assertEqual(url, 'http://ollama:11434/api/pull')
            self.assertEqual(body, {'name': BUILD, 'stream': False})
            self.assertEqual(timeout, 1800)

    def test_two_5xx_then_success(self):
        record = run_script([500, 500, 200])
        self.assert_pulled_once_each_attempt(record, 3)
        self.assertEqual(record['waits'], [5, 10])
        self.assertNotIn('setup_error', record['out'])
        self.assertEqual(record['tasks'], ['prepare', 'goldstandard', 'query'])
        retries = [line for line in record['stderr'].splitlines() if 'trying again' in line]
        self.assertEqual(len(retries), 2, record['stderr'])
        self.assertIn('attempt 1 of 3 answered HTTP 500; trying again in 5 s', retries[0])
        self.assertIn('attempt 2 of 3 answered HTTP 500; trying again in 10 s', retries[1])

    def test_5xx_every_time(self):
        record = run_script([500, 500, 500])
        self.assert_pulled_once_each_attempt(record, 3)
        self.assertEqual(record['waits'], [5, 10])
        error = record['out']['setup_error']
        for part in (BUILD, "verify project's Ollama", 'HTTP 500', '3 attempts', 'pull failed'):
            self.assertIn(part, error)
        self.assertEqual(record['tasks'], [])
        self.assertEqual(len(record['deletes']), 1, 'the build model is still deleted')
        self.assertEqual(record['out']['build_model'], BUILD)
        self.assertEqual(record['out']['answer_model'], ANSWER)

    def test_4xx_is_not_retried(self):
        record = run_script([404])
        self.assert_pulled_once_each_attempt(record, 1)
        self.assertEqual(record['waits'], [])
        self.assertIn('HTTP 404 after 1 attempt:', record['out']['setup_error'])
        self.assertEqual(record['tasks'], [])

    def test_request_error_is_not_retried(self):
        record = run_script(['connect-error'])
        self.assert_pulled_once_each_attempt(record, 1)
        self.assertEqual(record['waits'], [])
        self.assertIn('ConnectError: connection refused', record['out']['setup_error'])
        self.assertEqual(record['tasks'], [])

    def test_success_first_time(self):
        record = run_script([200])
        self.assert_pulled_once_each_attempt(record, 1)
        self.assertEqual(record['waits'], [])
        self.assertNotIn('setup_error', record['out'])
        self.assertEqual(record['tasks'], ['prepare', 'goldstandard', 'query'])


SIX_CHECKS = (
    'llm_split.py ran to the end',
    'with BUILD_LLM_MODEL set, the API resolves it as the build model',
    'the answer model stays as compose sets it',
    "preparation's LLM calls were answered by the build model only",
    "gold standard generation's LLM calls were answered by the build model only",
    "query's LLM calls were answered by the answer model only",
)
PULL_CHECK = "the build model is pulled into the verify project's Ollama"

STUBS = '''check() { if [ "$2" = "0" ]; then echo "PASS $1"; else echo "FAIL $1 | ${3:-}"; fi; }
check_eq() { [ "$2" = "$3" ]; check "$1" $? "got '$2', wanted '$3'"; }
skip() { echo "SKIP $1 | ${2:-}"; }
drop_collection() { echo "DROP $1"; }
summary() { echo SUMMARY; }
docker() { cat "$CANNED"; }
'''


class SuiteTests(unittest.TestCase):
    def run_block(self, line):
        script = SUITE.read_text()
        start = script.index('(cd "$REPO_ROOT" && docker compose exec')
        end = script.rindex('summary') + len('summary')
        with tempfile.TemporaryDirectory() as work:
            canned = Path(work) / 'canned.json'
            canned.write_text(json.dumps(line) + '\n')
            env = dict(os.environ, CANNED=str(canned), REPO_ROOT=str(ROOT), SMALL=BUILD,
                       C='VfyLlmSplit', OUT=str(Path(work) / 'out'))
            result = subprocess.run(['bash', '-c', STUBS + script[start:end]], env=env,
                                    capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_setup_error_is_one_failure(self):
        message = "could not pull qwen2.5:0.5b into the verify project's Ollama: HTTP 500 after 3 attempts: x"
        lines = self.run_block({'build_model': BUILD, 'answer_model': ANSWER, 'build_tagged': BUILD,
                                'answer_tagged': ANSWER + ':latest', 'setup_error': message})
        failures = [line for line in lines if line.startswith('FAIL ')]
        self.assertEqual(failures, ['FAIL {} | {}'.format(PULL_CHECK, message)], lines)
        self.assertEqual(len([line for line in lines if line.startswith('SKIP ')]), 1, lines)
        for name in SIX_CHECKS:
            self.assertFalse(any(name in line for line in lines), (name, lines))
        self.assertIn('DROP VfyLlmSplit', lines)
        self.assertEqual(lines[-1], 'SUMMARY')

    def test_good_line_passes_the_six_checks(self):
        lines = self.run_block({'build_model': BUILD, 'answer_model': ANSWER, 'build_tagged': BUILD,
                                'answer_tagged': ANSWER + ':latest', 'prepare': [BUILD],
                                'goldstandard': [BUILD], 'query': [ANSWER + ':latest']})
        self.assertEqual([line for line in lines if line.startswith(('PASS ', 'FAIL '))],
                         ['PASS ' + name for name in SIX_CHECKS], lines)
        self.assertFalse(any(PULL_CHECK in line for line in lines), lines)


if __name__ == '__main__':
    unittest.main()
