"""Host-only check of the CI workflow that runs the verify suite on pull requests (#24).

Reads .github/workflows/verify.yml as text: its trigger, permissions and
action pins, the stack.sh up / all.sh / stack.sh down sequence on the
disposable verify project, the compose logs artifact and the model cache, in
the `verify` job; and the `external-ollama` job (#673), which installs the
pinned native Ollama from its release's install script checked by sha256, binds
it to the docker0 address and runs suite 25 in `stack.sh up --external-ollama`. Also
checks the README badge and the copies embedded in IMPLEMENTATION.md. The
workflow's real run is the pull request's own; this keeps its rules from
drifting. Standard library only; no Docker or stack needed.

Run: python3 scripts/tests/test_ci_workflow.py
"""
from pathlib import Path
import os
import re
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = '.github/workflows/verify.yml'
BADGE = ('[![Verify](https://github.com/mikesilvers/rag-docker/actions/workflows/verify.yml/badge.svg?branch=develop)]'
         '(https://github.com/mikesilvers/rag-docker/actions/workflows/verify.yml)')
# A `docker` that answers `docker compose config` with the ollama service's LLM
# settings as docker-compose.yml sets them (#671).
STUB_DOCKER = """#!/bin/bash
[ "$1 $2" = "compose config" ] || { echo "stub docker: unexpected call: $*" >&2; exit 99; }
echo '{"name": "rag-docker", "services": {"ollama": {"environment": {"LLM_MODEL": "llama3.1:8b", "BUILD_LLM_MODEL": "", "ANSWER_LLM_MODEL": ""}}}}'
"""


def workflow():
    return (ROOT / WORKFLOW).read_text()


def job(text, name):
    """One job's lines, from its `  <name>:` line to the next job or the end."""
    lines = text.splitlines()
    start = lines.index('  {}:'.format(name))
    block = [lines[start]]
    for line in lines[start + 1:]:
        if re.match(r'^  [A-Za-z0-9_-]+:\s*$', line) or (line and not line.startswith((' ', '#'))):
            break
        block.append(line)
    while block and (not block[-1].strip() or block[-1].lstrip().startswith('#')):
        block.pop()
    return '\n'.join(block) + '\n'


def verify_job():
    return job(workflow(), 'verify')


def external_job():
    return job(workflow(), 'external-ollama')


def code(text):
    """The workflow without its comment lines."""
    return '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))


def top_block(text, key):
    """The lines of a top-level key's block, the key's own line included."""
    lines = text.splitlines()
    start = lines.index(key + ':')
    block = [lines[start]]
    for line in lines[start + 1:]:
        if line and not line.startswith((' ', '#')):
            break
        block.append(line)
    return [line for line in block if line.strip() and not line.lstrip().startswith('#')]


def steps(text):
    """Each step's text, in order, comments left out."""
    lines = text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == 'steps:')
    found = []
    for line in lines[start + 1:]:
        if line.lstrip().startswith('#'):
            continue
        if line.startswith('      - '):
            found.append([])
        if found:
            found[-1].append(line)
    return ['\n'.join(step) for step in found]


def step_with(text, needle):
    matches = [i for i, step in enumerate(steps(text)) if needle in step]
    if len(matches) != 1:
        raise AssertionError('{} steps contain {!r}, wanted 1'.format(len(matches), needle))
    return matches[0], steps(text)[matches[0]]


class TriggerAndPermissionTests(unittest.TestCase):
    def test_runs_on_every_pull_request_to_develop_and_main_only(self):
        text = workflow()
        self.assertEqual(top_block(text, 'on'),
                         ['on:', '  pull_request:', '    branches: [develop, main]'])
        self.assertNotIn('pull_request_target', text)

    def test_two_jobs_on_ubuntu_latest_with_a_timeout(self):
        text = workflow()
        self.assertEqual(re.findall(r'^  ([A-Za-z0-9_-]+):\s*$', text.split('\njobs:\n', 1)[1], re.M),
                         ['verify', 'external-ollama'])
        self.assertEqual(re.findall(r'^    runs-on: (.*)$', text, re.M), ['ubuntu-latest', 'ubuntu-latest'])
        for name in ('verify', 'external-ollama'):
            with self.subTest(job=name):
                self.assertRegex(job(text, name), r'(?m)^    timeout-minutes: \d+$')

    def test_least_privilege_and_no_secrets(self):
        text = workflow()
        self.assertEqual(top_block(text, 'permissions'), ['permissions:', '  contents: read'])
        self.assertEqual(len(re.findall(r'(?m)^\s*permissions:', text)), 1)
        for refused in ('secrets.', 'GITHUB_TOKEN'):
            with self.subTest(refused=refused):
                self.assertFalse(refused in text, refused + ' is used')

    def test_every_action_is_github_owned_and_pinned_to_a_full_sha(self):
        uses = re.findall(r'(?m)^\s*(?:- )?uses: (.*)$', workflow())
        self.assertTrue(uses)
        for value in uses:
            with self.subTest(uses=value):
                self.assertRegex(value, r'^actions/[a-z-]+(/[a-z-]+)?@[0-9a-f]{40} # v\d+\.\d+\.\d+$')

    def test_checkout_leaves_no_credentials(self):
        for text in (verify_job(), external_job()):
            _, step = step_with(text, 'actions/checkout@')
            self.assertIn('persist-credentials: false', step)


class SuiteTests(unittest.TestCase):
    def test_skip_slow_on_the_verify_project_never_the_live_stack(self):
        text = code(verify_job())
        self.assertRegex(text, r"(?m)^      RAG_SKIP_SLOW: '1'$")
        for refused in ('RAG_VERIFY_LIVE', 'stack.sh run'):
            with self.subTest(refused=refused):
                self.assertFalse(refused in text, refused + ' is used')
        self.assertIsNone(re.search(r'docker compose[^\n]* up\b', text), 'docker compose up is used')

    def test_up_suite_logs_upload_down_in_order(self):
        text = verify_job()
        order = [step_with(text, needle)[0] for needle in (
            'bash scripts/verify/stack.sh up', 'bash scripts/verify/all.sh',
            'docker compose -p rag-verify logs', 'actions/upload-artifact@',
            'bash scripts/verify/stack.sh down')]
        self.assertEqual(order, sorted(order))

    def test_logs_upload_and_down_always_run(self):
        text = verify_job()
        for needle in ('docker compose -p rag-verify logs', 'actions/upload-artifact@',
                       'bash scripts/verify/stack.sh down'):
            with self.subTest(step=needle):
                self.assertIn('if: always()', step_with(text, needle)[1])


class ModelCacheTests(unittest.TestCase):
    def test_key_is_the_models_the_entrypoint_pulls(self):
        # The names ollama/compose-models.sh resolves from the ollama service's LLM
        # settings, which the entrypoint pulls (#671).
        text = verify_job()
        _, step = step_with(text, 'id: models')
        self.assertIn('names=$(bash ollama/compose-models.sh) \\\n', step)
        self.assertIn('|| { echo "could not read the model names (ollama/compose-models.sh)" >&2; exit 1; }', step)
        self.assertIn('echo "key=ollama-models-${names// /-}"', step)
        self.assertIn('for model in $(models); do', (ROOT / 'ollama/entrypoint.sh').read_text())
        with tempfile.TemporaryDirectory() as folder:
            docker = Path(folder) / 'docker'
            docker.write_text(STUB_DOCKER)
            docker.chmod(0o755)
            out = subprocess.run(['bash', str(ROOT / 'ollama/compose-models.sh')], capture_output=True,
                                 text=True, timeout=60,
                                 env=dict(os.environ, PATH=folder + os.pathsep + os.environ['PATH']))
        self.assertEqual(out.returncode, 0, out.stderr)
        # With the default settings the key is what it was before the split.
        self.assertEqual('ollama-models-' + '-'.join(out.stdout.split()),
                         'ollama-models-llama3.1:8b-nomic-embed-text')

    def test_restored_before_start_and_saved_only_after_a_miss(self):
        text = verify_job()
        restore, restore_step = step_with(text, 'actions/cache/restore@')
        start, _ = step_with(text, 'bash scripts/verify/stack.sh up')
        export, export_step = step_with(text, 'id: export')
        save, save_step = step_with(text, 'actions/cache/save@')
        down, _ = step_with(text, 'bash scripts/verify/stack.sh down')
        self.assertLess(restore, start)
        self.assertLess(down, export)
        self.assertLess(export, save)
        for step in (restore_step, save_step):
            self.assertIn('key: ${{ steps.models.outputs.key }}', step)
        self.assertIn("steps.cache.outputs.cache-hit != 'true'", export_step)
        self.assertIn('/copy/models', export_step)
        self.assertIn("steps.export.outcome == 'success'", save_step)


class ExternalOllamaJobTests(unittest.TestCase):
    """The external-Ollama job (#673): README "External Ollama (native)" on Linux."""

    def test_pins_the_tested_release_and_its_install_script(self):
        text = external_job()
        self.assertRegex(text, r"(?m)^      OLLAMA_VERSION: 0\.40\.18$")
        self.assertRegex(text, r"(?m)^      OLLAMA_INSTALL_SHA256: [0-9a-f]{64}$")
        _, step = step_with(text, 'name: Install native Ollama')
        self.assertIn('https://github.com/ollama/ollama/releases/download/v$OLLAMA_VERSION/install.sh', step)
        self.assertIn('echo "$OLLAMA_INSTALL_SHA256  $RUNNER_TEMP/install.sh" | sha256sum -c -', step)
        # The check comes before the script runs, and nothing is piped into a shell.
        self.assertLess(step.index('sha256sum -c -'), step.index('sh "$RUNNER_TEMP/install.sh"'))
        self.assertNotRegex(code(text), r'\|\s*(sudo\s+)?(ba)?sh\b')
        self.assertIn('0.40.18', (ROOT / 'README.md').read_text())

    def test_binds_ollama_to_docker0_never_every_address(self):
        text = code(external_job())
        _, step = step_with(text, 'name: Bind Ollama to the docker0 address')
        self.assertIn('ip -4 -o addr show docker0', step)
        self.assertIn('Environment="OLLAMA_HOST=%s:11434"', step)
        self.assertNotIn('0.0.0.0', text)

    def test_up_external_suite_logs_upload_down_in_order(self):
        text = external_job()
        order = [step_with(text, needle)[0] for needle in (
            'name: Install native Ollama', 'name: Bind Ollama', 'name: Pull the models',
            'bash scripts/verify/stack.sh up --external-ollama',
            'bash scripts/verify/all.sh 25_external_ollama',
            'docker compose -p rag-verify logs', 'actions/upload-artifact@',
            'bash scripts/verify/stack.sh down')]
        self.assertEqual(order, sorted(order))
        for needle in ('docker compose -p rag-verify logs', 'actions/upload-artifact@',
                       'bash scripts/verify/stack.sh down'):
            with self.subTest(step=needle):
                self.assertIn('if: always()', step_with(text, needle)[1])
        self.assertRegex(code(text), r"(?m)^      RAG_SKIP_SLOW: '1'$")
        for refused in ('RAG_VERIFY_LIVE', 'stack.sh run'):
            with self.subTest(refused=refused):
                self.assertNotIn(refused, code(text))

    def test_native_model_cache_by_release_and_names(self):
        text = external_job()
        _, names = step_with(text, 'id: models')
        self.assertIn('names=$(bash ollama/compose-models.sh)', names)
        self.assertIn('echo "key=native-ollama-$OLLAMA_VERSION-${names// /-}"', names)
        restore, restore_step = step_with(text, 'actions/cache/restore@')
        pull, _ = step_with(text, 'name: Pull the models')
        export, export_step = step_with(text, 'id: export')
        save, save_step = step_with(text, 'actions/cache/save@')
        down, _ = step_with(text, 'bash scripts/verify/stack.sh down')
        self.assertLess(restore, pull)
        self.assertLess(down, export)
        self.assertLess(export, save)
        for step in (restore_step, save_step):
            self.assertIn('key: ${{ steps.models.outputs.key }}', step)
        self.assertIn("steps.cache.outputs.cache-hit != 'true'", export_step)
        self.assertIn("steps.export.outcome == 'success'", save_step)


class DocumentationTests(unittest.TestCase):
    def test_readme_shows_the_badge(self):
        self.assertIn(BADGE, (ROOT / 'README.md').read_text())

    def test_embedded_copies_match(self):
        text = (ROOT / 'IMPLEMENTATION.md').read_text()
        for name, language in ((WORKFLOW, 'yaml'), ('scripts/tests/test_ci_workflow.py', 'python')):
            with self.subTest(file=name):
                header = '### {}\n\n'.format(name)
                start = text.index(header) + len(header)
                start = text.index('```{}\n'.format(language), start) + len(language) + 4
                end = text.index('\n```\n', start)
                self.assertEqual(text[start:end], (ROOT / name).read_text().rstrip('\n'))


if __name__ == '__main__':
    unittest.main()
