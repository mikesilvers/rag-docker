"""Embedded runnable examples must match the completed-write boundary."""
from pathlib import Path
import os
import subprocess
import tempfile
import unittest
ROOT = Path(__file__).resolve().parents[2]


class ImplementationTests(unittest.TestCase):
    def test_examples_match_runtime_and_verification(self):
        implementation = (ROOT / 'IMPLEMENTATION.md').read_text()
        files = [(name, '```', 'python') for name in ('api/main.py', 'api/services/weaviate_client.py',
                 'api/services/batch_write.py', 'api/services/collection_recovery.py',
                 'api/services/importer.py', 'api/services/tuning.py')]
        files += [(name, '```', 'bash') for name in ('scripts/verify/01_infrastructure.sh', 'scripts/verify/05_transfer.sh')]
        files += [('scripts/verify/README.md', '````', 'markdown'), ('scripts/verify/batch_recovery.py', '```', 'python')]
        for name, fence, language in files:
            with self.subTest(file=name):
                header = '### ' + name + '\n\n' + fence + language + '\n'
                start = implementation.index(header) + len(header)
                end = implementation.index('\n' + fence + '\n', start)
                self.assertEqual(implementation[start:end], (ROOT / name).read_text().rstrip('\n'))


class TransferPrefixTests(unittest.TestCase):
    """05_transfer.sh skips batch recovery for prefixes batch_recovery.py refuses."""

    STUBS = ('check(){ echo "CHECK $1"; }\n'
             'skip(){ echo "SKIP $1 | $2"; }\n'
             'docker(){ echo "DOCKER $*" >> "$TRACE"; }\n'
             'api_code(){ echo 200; }\n'
             'sleep(){ :; }\n')

    def run_block(self, prefix, allow_restart, refusal=''):
        script = (ROOT / 'scripts/verify/05_transfer.sh').read_text()
        start = script.index('# ── verified recovery across an API restart')
        end = script.index('rm -f "$EXPORTS/$PKG"', start)
        with tempfile.TemporaryDirectory() as work:
            # The block writes its phase logs to /tmp; keep them in the test's directory.
            block = script[start:end].replace('/tmp/', work + '/')
            trace = Path(work) / 'trace'
            env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'REPO_ROOT': str(ROOT), 'TRACE': str(trace),
                   'API': 'http://127.0.0.1:9/api', 'PREFIX': prefix, 'RAG_ALLOW_RESTART': allow_restart}
            # lib.sh's guard, stubbed so the block never depends on it being undefined.
            stubs = self.STUBS + "restart_refusal_reason(){ printf '%s' \"$REFUSAL\"; }\n"
            env['REFUSAL'] = refusal
            result = subprocess.run(['bash', '-c', stubs + block], env=env,
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('command not found', result.stderr)
            calls = trace.read_text().splitlines() if trace.exists() else []
        return result.stdout.splitlines() + calls

    def test_custom_prefix_skips_with_reason_and_never_restarts(self):
        lines = self.run_block('Custom', '1')
        self.assertEqual(len(lines), 1, lines)
        self.assertTrue(lines[0].startswith('SKIP batch recovery across an API restart | '), lines)
        self.assertIn('Vfy', lines[0])
        self.assertIn("RAG_TEST_PREFIX='Custom'", lines[0])

    def test_vfy_prefix_runs_every_phase(self):
        lines = self.run_block('Vfy', '1')
        self.assertFalse([line for line in lines if line.startswith('SKIP')], lines)
        docker = [line for line in lines if line.startswith('DOCKER')]
        self.assertEqual(docker, ['DOCKER compose exec -T api python - prepare --prefix VfyBatchRecovery',
                                  'DOCKER compose restart api',
                                  'DOCKER compose exec -T api python - check --prefix VfyBatchRecovery',
                                  'DOCKER compose exec -T api python - cleanup --prefix VfyBatchRecovery'])

    def test_restart_not_allowed_keeps_the_existing_skip(self):
        self.assertEqual(self.run_block('Vfy', '0'), [
            'SKIP batch recovery across an API restart | set RAG_ALLOW_RESTART=1 to include it'])

    def test_restart_refusal_fails_the_check_and_never_restarts(self):
        # Reviewer-added: the #152 guard still wins over the prefix checks.
        for prefix in ('Vfy', 'Custom'):
            with self.subTest(prefix=prefix):
                lines = self.run_block(prefix, '1', refusal='restarts never run against the live rag-docker project (#152)')
                self.assertEqual(lines, ['CHECK batch recovery across an API restart'], lines)


if __name__ == '__main__':
    unittest.main()
