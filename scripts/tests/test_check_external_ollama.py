"""Host-only check of scripts/check-external-ollama.sh (#673).

`uname`, `lsof`, `ss`, `ip` and `docker` are stubs on PATH that answer as macOS
or Linux would, so the listener check's decisions and the exit status are
checked without Ollama or Docker: loopback is fine, a wildcard or a LAN address
is a warning (Ollama has no authentication), the docker0 address is fine on
Linux (the line names the README's firewall rule it relies on), nothing
listening is a problem (on macOS with lsof's own-user note), and the api
container's check runs unless --host-only. Standard library only.

Run: python3 scripts/tests/test_check_external_ollama.py
"""
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/check-external-ollama.sh'

LSOF_HEADER = 'COMMAND   PID USER   FD   TYPE             DEVICE SIZE/OFF NODE NAME'


def lsof_line(address):
    return 'ollama  4242 mike    3u  IPv4 0x1234567890abcdef      0t0  TCP {}:11434 (LISTEN)'.format(address)


def ss_line(address):
    return 'LISTEN 0      4096      {}:11434      0.0.0.0:*'.format(address)


class HelperTests(unittest.TestCase):
    def run_helper(self, os_name, listening, *args, docker0='172.17.0.1', tools=('lsof', 'ss', 'ip'),
                   docker_status=0, compose_file=None):
        folder = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__('shutil').rmtree(folder, ignore_errors=True))
        log = folder / 'log'
        stubs = {
            'uname': 'echo {}'.format(os_name),
            'docker': 'echo "docker $*" >> "{}"; echo "COMPOSE_FILE=$COMPOSE_FILE" >> "{}"; '
                      'echo "ollama-check: ok: stub"; exit {}'.format(log, log, docker_status),
        }
        if 'lsof' in tools:
            body = '\n'.join([LSOF_HEADER] + [lsof_line(a) for a in listening]) if listening else ''
            stubs['lsof'] = 'cat <<\'EOF\'\n{}\nEOF'.format(body) if body else 'exit 1'
        if 'ss' in tools:
            body = '\n'.join(ss_line(a) for a in listening)
            stubs['ss'] = 'cat <<\'EOF\'\n{}\nEOF'.format(body) if body else 'exit 0'
        if 'ip' in tools:
            stubs['ip'] = ('echo "4: docker0    inet {}/16 brd 172.17.255.255 scope global docker0"'.format(docker0)
                           if docker0 else 'exit 1')
        bin_dir = folder / 'bin'
        bin_dir.mkdir()
        for name, body in stubs.items():
            path = bin_dir / name
            path.write_text('#!/bin/bash\n' + body + '\n')
            path.chmod(0o755)
        # Only the stubs and the basic tools the script uses (no real lsof/ss/ip).
        base = folder / 'base'
        base.mkdir()
        for tool in ('bash', 'sed', 'awk', 'sort', 'cut', 'head', 'cat', 'dirname', 'printf', 'grep', 'tr'):
            for directory in ('/bin', '/usr/bin'):
                if os.path.exists(os.path.join(directory, tool)):
                    os.symlink(os.path.join(directory, tool), base / tool)
                    break
        env = {'PATH': '{}:{}'.format(bin_dir, base), 'HOME': str(folder)}
        if compose_file:
            env['COMPOSE_FILE'] = compose_file
        out = subprocess.run(['bash', str(SCRIPT), *args], capture_output=True, text=True, timeout=60, env=env)
        calls = log.read_text() if log.exists() else ''
        return out, calls

    def test_loopback_on_macos_passes_and_runs_the_container_check(self):
        out, calls = self.run_helper('Darwin', ['127.0.0.1', '[::1]'])
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn('ok: Ollama listens on 127.0.0.1:11434 (loopback)', out.stdout)
        self.assertIn('ok: Ollama listens on ::1:11434 (loopback)', out.stdout)
        self.assertIn('docker compose exec -T api python -m ollama_check', calls)
        self.assertIn('COMPOSE_FILE=docker-compose.yml:docker-compose.external-ollama.yml', calls)

    def test_a_wildcard_is_a_warning(self):
        for os_name, address in (('Darwin', '*'), ('Linux', '0.0.0.0'), ('Linux', '[::]'), ('Linux', '*')):
            with self.subTest(os=os_name, address=address):
                out, _ = self.run_helper(os_name, [address], '--host-only')
                self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
                self.assertIn('warning: Ollama listens on every address', out.stdout)
                self.assertIn('Ollama has no authentication', out.stdout)

    def test_a_lan_address_is_a_warning(self):
        out, _ = self.run_helper('Darwin', ['192.168.1.20'], '--host-only')
        self.assertEqual(out.returncode, 3)
        self.assertIn('warning: Ollama listens on 192.168.1.20:11434, which other machines', out.stdout)
        self.assertIn('bind it to 127.0.0.1 or ::1', out.stdout)

    def test_docker0_is_allowed_on_linux_only(self):
        out, _ = self.run_helper('Linux', ['172.17.0.1'], '--host-only')
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn('ok: Ollama listens on 172.17.0.1:11434 (docker0)', out.stdout)
        # docker0 alone doesn't keep it off the LAN (#940's evaluation): the line says so.
        self.assertIn('only with the firewall rule in README "External Ollama (native)" step 3 '
                      'that drops port 11434 on other interfaces', out.stdout)
        out, _ = self.run_helper('Darwin', ['172.17.0.1'], '--host-only')
        self.assertEqual(out.returncode, 3, out.stdout)

    def test_the_warning_names_docker0_on_linux(self):
        out, _ = self.run_helper('Linux', ['0.0.0.0'], '--host-only')
        self.assertIn('bind it to 172.17.0.1 (docker0), 127.0.0.1 or ::1', out.stdout)

    def test_nothing_listening_is_a_problem(self):
        for os_name in ('Darwin', 'Linux'):
            with self.subTest(os=os_name):
                out, _ = self.run_helper(os_name, [], '--host-only')
                self.assertEqual(out.returncode, 1)
                self.assertIn('problem: nothing listens on port 11434', out.stdout)
                # macOS lsof without root sees only this user's sockets (#940's evaluation).
                note = "lsof lists only your own processes' sockets"
                if os_name == 'Darwin':
                    self.assertIn(note, out.stdout)
                    self.assertIn('sudo lsof -nP -iTCP:11434 -sTCP:LISTEN', out.stdout)
                else:
                    self.assertNotIn(note, out.stdout)

    def test_a_missing_tool_is_a_warning(self):
        out, _ = self.run_helper('Darwin', ['127.0.0.1'], '--host-only', tools=('ss', 'ip'))
        self.assertEqual(out.returncode, 3)
        self.assertIn("'lsof' isn't installed", out.stdout)

    def test_host_only_skips_docker(self):
        out, calls = self.run_helper('Darwin', ['127.0.0.1'], '--host-only')
        self.assertEqual(out.returncode, 0)
        self.assertEqual(calls, '')

    def test_a_failed_container_check_is_a_problem(self):
        out, _ = self.run_helper('Darwin', ['127.0.0.1'], docker_status=1)
        self.assertEqual(out.returncode, 1)
        self.assertIn("problem: the api container's check failed", out.stdout)

    def test_the_users_compose_file_is_kept(self):
        mine = 'docker-compose.yml:docker-compose.lan.yml:docker-compose.external-ollama.yml'
        out, calls = self.run_helper('Darwin', ['127.0.0.1'], compose_file=mine)
        self.assertEqual(out.returncode, 0, out.stdout)
        self.assertIn('COMPOSE_FILE=' + mine + '\n', calls)

    def test_usage(self):
        out, _ = self.run_helper('Darwin', ['127.0.0.1'], '--nope')
        self.assertEqual(out.returncode, 2)
        self.assertIn('check-external-ollama.sh', out.stderr)

    def test_it_never_writes(self):
        text = SCRIPT.read_text()
        code = '\n'.join(line for line in text.splitlines() if not line.lstrip().startswith('#'))
        for refused in ('ollama pull', 'ollama rm', 'ollama stop', '/api/pull', '/api/delete', 'OLLAMA_HOST=',
                        'launchctl', 'systemctl'):
            with self.subTest(refused=refused):
                self.assertNotIn(refused, code)


if __name__ == '__main__':
    unittest.main()
