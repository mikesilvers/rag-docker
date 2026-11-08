"""Host-only check of the CI workflow that runs the verify suite on pull requests (#24).

Reads .github/workflows/verify.yml as text: its trigger, permissions and
action pins, the stack.sh up / all.sh / stack.sh down sequence on the
disposable verify project, the compose logs artifact and the model cache. Also
checks the README badge and the copies embedded in IMPLEMENTATION.md. The
workflow's real run is the pull request's own; this keeps its rules from
drifting. Standard library only; no Docker or stack needed.

Run: python3 scripts/tests/test_ci_workflow.py
"""
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = '.github/workflows/verify.yml'
BADGE = ('[![Verify](https://github.com/mikesilvers/rag-docker/actions/workflows/verify.yml/badge.svg?branch=develop)]'
         '(https://github.com/mikesilvers/rag-docker/actions/workflows/verify.yml)')
# The pattern the workflow's sed uses on ollama/entrypoint.sh, in Python.
MODEL_LINE = re.compile(r'^pull_with_retry ([A-Za-z0-9._:-]*)$', re.M)


def workflow():
    return (ROOT / WORKFLOW).read_text()


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

    def test_one_job_on_ubuntu_latest_with_a_timeout(self):
        text = workflow()
        self.assertEqual(re.findall(r'^    runs-on: (.*)$', text, re.M), ['ubuntu-latest'])
        self.assertRegex(text, r'(?m)^    timeout-minutes: \d+$')

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
        _, step = step_with(workflow(), 'actions/checkout@')
        self.assertIn('persist-credentials: false', step)


class SuiteTests(unittest.TestCase):
    def test_skip_slow_on_the_verify_project_never_the_live_stack(self):
        text = code(workflow())
        self.assertRegex(text, r"(?m)^      RAG_SKIP_SLOW: '1'$")
        for refused in ('RAG_VERIFY_LIVE', 'stack.sh run'):
            with self.subTest(refused=refused):
                self.assertFalse(refused in text, refused + ' is used')
        self.assertIsNone(re.search(r'docker compose[^\n]* up\b', text), 'docker compose up is used')

    def test_up_suite_logs_upload_down_in_order(self):
        text = workflow()
        order = [step_with(text, needle)[0] for needle in (
            'bash scripts/verify/stack.sh up', 'bash scripts/verify/all.sh',
            'docker compose -p rag-verify logs', 'actions/upload-artifact@',
            'bash scripts/verify/stack.sh down')]
        self.assertEqual(order, sorted(order))

    def test_logs_upload_and_down_always_run(self):
        text = workflow()
        for needle in ('docker compose -p rag-verify logs', 'actions/upload-artifact@',
                       'bash scripts/verify/stack.sh down'):
            with self.subTest(step=needle):
                self.assertIn('if: always()', step_with(text, needle)[1])


class ModelCacheTests(unittest.TestCase):
    def test_key_is_the_models_the_entrypoint_pulls(self):
        text = workflow()
        _, step = step_with(text, 'id: models')
        self.assertIn('ollama/entrypoint.sh', step)
        self.assertIn(r"sed -n 's/^pull_with_retry \([A-Za-z0-9._:-]*\)$/\1/p'", step)
        self.assertIn('key=ollama-models-', step)
        self.assertTrue(MODEL_LINE.findall((ROOT / 'ollama/entrypoint.sh').read_text()))

    def test_restored_before_start_and_saved_only_after_a_miss(self):
        text = workflow()
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
