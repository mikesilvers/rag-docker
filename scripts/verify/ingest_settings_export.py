"""Live E52 acceptance: invalid saved ingest settings fail the export (#515, #919).

Writes the collection's saved ingest settings file directly in the API
container, as an older API or a hand edit could leave it (the API's own save
refuses these values), then checks through the real export job that each
invalid file fails the export with an error naming the collection, the failing
field and the Chunking Configuration page, that no package is published, and
that the refused export leaves the saved file as it was written (#932). The
collection's saved settings file is restored on exit.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

# The suite's session (#25); found beside this file however the file is loaded.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth_headers  # noqa: E402

# Each invalid saved file and what its error must name: JSON that isn't an
# object, a field of the wrong type and an invalid value, as E52 lists them.
CASES = [([], 'not a JSON object'),
         ({'chunk_size': 'invalid'}, 'chunk_size'),
         ({'chunk_size': 0}, 'chunk_size')]

READ = ('import json, sys\n'
        'from services import ingest_config\n'
        'p = ingest_config._path(sys.argv[1])\n'
        'print(json.dumps(p.read_text() if p.exists() else None))\n')
WRITE = ('import sys\n'
         'from services import ingest_config\n'
         'ingest_config._path(sys.argv[1]).write_text(sys.argv[2])\n')
REMOVE = ('import sys\n'
          'from services import ingest_config\n'
          'ingest_config._path(sys.argv[1]).unlink(missing_ok=True)\n')


def run(api, collection, exports, repo_root):
    exports = Path(exports)
    failures = []

    def request(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(api.rstrip('/') + path, data=data,
                                     headers={'Content-Type': 'application/json', **auth_headers.headers()})
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)

    def wait(path, timeout=900):
        deadline = time.monotonic() + timeout
        while True:
            job = request(path)
            if job['status'] in ('failed', 'completed'):
                return job
            if time.monotonic() >= deadline:
                raise AssertionError(f'{path} timed out: {job}')
            time.sleep(0.5)

    def in_api(code, *args):
        """Runs `code` in the API container, where the saved settings live."""
        return subprocess.run(['docker', 'compose', 'exec', '-T', 'api', 'python', '-c', code,
                               collection, *args], cwd=repo_root, check=True,
                              capture_output=True, text=True).stdout

    def expect(name, ok, detail=''):
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else f'  -- {detail}'))
        if not ok:
            failures.append(name)

    def listing():
        return sorted(p.name for p in exports.iterdir())

    def saved_file():
        """The saved settings file's text, or None when there is none."""
        return json.loads(in_api(READ).strip().splitlines()[-1])

    saved = saved_file()
    try:
        for value, needle in CASES:
            # Bypasses the save contract, which refuses these values.
            in_api(WRITE, json.dumps(value))
            written = saved_file()
            before = listing()
            job = wait('/export/job/' + request('/export', {'collection': collection, 'encryption': 'none',
                                                            'include_models': False})['job_id'])
            error = job.get('error') or ''
            expect(f'invalid saved ingest settings {value!r}: export fails', job['status'] == 'failed', job)
            expect(f'invalid saved ingest settings {value!r}: the error names the collection, '
                   f'{needle!r} and the Chunking Configuration page',
                   f"'{collection}'" in error and needle in error
                   and 'Chunking Configuration page' in error, error)
            expect(f'invalid saved ingest settings {value!r}: no package or staging is left',
                   listing() == before, sorted(set(listing()) ^ set(before)))
            after = saved_file()
            expect(f'invalid saved ingest settings {value!r}: the saved settings file is unchanged',
                   after == written, {'written': written, 'after': after})
    finally:
        if saved is None:
            in_api(REMOVE)
        else:
            in_api(WRITE, saved)
    print(f'{len(failures)} invalid saved ingest settings check(s) failed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(run(*sys.argv[1:]))
