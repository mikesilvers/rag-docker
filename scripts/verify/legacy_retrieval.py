"""Live E28 acceptance for retrieval settings saved under an older contract (#173).

Writes the collection's saved settings file directly in the API container, as
an API from before PR #108 could have, then checks through the real API:
- a legacy integer ef exports as null with a warning, and the saved file is
  left alone;
- a package carrying a legacy ef imports with ef null and a note;
- other invalid saved settings fail the export before any package is
  published, with an error naming the fields and the Retrieval page.
The collection's saved settings are restored on exit.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request

WARNING = ("saved retrieval setting ef={} is outside 16-512 and was exported as null; "
           "ef is no longer used. Save this collection's settings on the Retrieval "
           "page to clear it.")
NOTE = ("the package's retrieval setting ef={} is outside 16-512 and was restored "
        "as null; ef is no longer used.")


def run(api, collection, exports, repo_root):
    exports = Path(exports)
    failures = []

    def request(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(api.rstrip('/') + path, data=data,
                                     headers={'Content-Type': 'application/json'})
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

    def write_saved(value):
        """Bypass the save contract, as an older API's saved file would."""
        code = ('import json, sys\n'
                'from services import retrieval_config\n'
                'retrieval_config._path(sys.argv[1]).write_text(sys.argv[2])\n')
        subprocess.run(['docker', 'compose', 'exec', '-T', 'api', 'python', '-c', code,
                        collection, json.dumps(value)], cwd=repo_root, check=True)

    def expect(name, ok, detail=''):
        print(('PASS ' if ok else 'FAIL ') + name + ('' if ok else f'  -- {detail}'))
        if not ok:
            failures.append(name)

    def listing():
        return sorted(p.name for p in exports.iterdir())

    saved = request('/retrieval/config/' + collection)
    renamed = None
    try:
        # 1. Export of a legacy ef: null in the package, a warning, file left alone.
        legacy = {k: saved[k] for k in ('retrieval_mode', 'alpha', 'response_format')}
        write_saved({**legacy, 'top_k': 7, 'ef': 10000})
        loaded = request('/retrieval/config/' + collection)
        expect('legacy ef: the Retrieval page can still load the saved settings',
               loaded['ef'] == 10000 and loaded['top_k'] == 7, loaded)
        job = wait('/export/job/' + request('/export', {'collection': collection,
                                                        'include_models': False})['job_id'])
        expect('legacy ef: export completes', job['status'] == 'completed', job)
        package = exports / job['filename']
        with tarfile.open(package) as archive:
            members = {Path(m.name).name: m for m in archive.getmembers() if m.isfile()}
            cfg = json.load(archive.extractfile(members['retrieval_config.json']))
            manifest = json.load(archive.extractfile(members['manifest.json']))
            script = archive.extractfile(members['retrieve.py']).read().decode()
        warning = WARNING.format(10000)
        expect('legacy ef: the package carries ef null and the other settings',
               cfg['ef'] is None and cfg['top_k'] == 7, cfg)
        expect('legacy ef: the export job warns once', job['warnings'].count(warning) == 1,
               job['warnings'])
        expect('legacy ef: the manifest records the warning',
               manifest['warnings'].count(warning) == 1, manifest['warnings'])
        expect('legacy ef: retrieve.py carries the saved top_k', 'DEFAULT_TOP_K = 7' in script,
               [l for l in script.splitlines() if 'TOP_K =' in l])
        after = request('/retrieval/config/' + collection)
        expect('legacy ef: export leaves the saved settings alone', after['ef'] == 10000, after)

        # 2. Import of a package with a legacy ef: restored as null, with a note.
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            with tarfile.open(package) as archive:
                archive.extractall(work, filter='data')
            root = next(p for p in work.iterdir() if p.is_dir())
            side = root / 'retrieval_config.json'
            side.write_text(json.dumps({**json.loads(side.read_text()), 'ef': 513}))
            manifest_path = root / 'manifest.json'
            manifest = json.loads(manifest_path.read_text())
            manifest['files'][side.name] = 'sha256:' + hashlib.sha256(side.read_bytes()).hexdigest()
            manifest_path.write_text(json.dumps(manifest))
            crafted = exports / (package.name.replace('.tar.gz', '') + '-legacyef.tar.gz')
            # Write under another name, then rename: on Docker Desktop, the API
            # container can read a freshly written bind-mounted file as empty.
            partial = crafted.with_name('.' + crafted.name + '.part')
            with tarfile.open(partial, 'w:gz') as archive:
                archive.add(root, arcname=root.name)
            partial.replace(crafted)
        try:
            job = wait('/import/job/' + request('/import', {'filename': crafted.name,
                                                            'on_conflict': 'rename'})['job_id'])
        finally:
            crafted.unlink(missing_ok=True)
            package.unlink(missing_ok=True)
        expect('legacy ef: a package with ef=513 imports', job['status'] == 'completed', job)
        if job['status'] == 'completed':
            renamed = job['collection']
            expect('legacy ef: the import notes the cleared ef once',
                   job['notes'].count(NOTE.format(513)) == 1, job['notes'])
            restored = request('/retrieval/config/' + renamed)
            expect('legacy ef: the imported collection has ef null and the package top_k',
                   restored['ef'] is None and restored['top_k'] == 7
                   and restored['is_default'] is False, restored)

        # 3. Other invalid saved settings fail early, actionably, publishing nothing.
        for value, needles in [({**legacy, 'top_k': 0, 'alpha': 2}, ['top_k', 'alpha']),
                               ({**legacy, 'ef': True}, ['ef']),
                               ([], ['not a JSON object'])]:
            write_saved(value)
            before = listing()
            job = wait('/export/job/' + request('/export', {'collection': collection,
                                                            'include_models': False})['job_id'])
            error = job.get('error') or ''
            expect(f'invalid saved settings {value!r}: export fails', job['status'] == 'failed', job)
            expect(f'invalid saved settings {value!r}: the error is actionable',
                   all(n in error for n in needles) and 'Retrieval page' in error
                   and f"'{collection}'" in error, error)
            expect(f'invalid saved settings {value!r}: no package or staging is left',
                   listing() == before, sorted(set(listing()) ^ set(before)))
    finally:
        if renamed:
            urllib.request.urlopen(urllib.request.Request(
                api.rstrip('/') + f'/collections/{renamed}?confirm=true', method='DELETE'),
                timeout=120).read()
        request('/retrieval/config', saved)
    print(f'{len(failures)} legacy/invalid retrieval settings check(s) failed')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(run(*sys.argv[1:]))
