"""Live transfer regression: digest-valid invalid settings cannot replace a corpus."""
import hashlib
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import time
import urllib.request


def run(api, collection, package):
    def request(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(api.rstrip('/') + path, data=data,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as response:
            return json.load(response)

    def collections():
        return sorted((c['name'], c['object_count'])
                      for c in request('/collections')['collections'])

    package = Path(package)
    saved = request('/retrieval/config/' + collection)
    baseline = collections()
    marker = {**saved, 'top_k': 9 if saved['top_k'] != 9 else 10}
    try:
        request('/retrieval/config', marker)
        expected = request('/retrieval/config/' + collection)
        invalid = [[], {'top_k': '5; raise RuntimeError("injected")'},
                   {'retrieval_mode': 'hybrid"; raise RuntimeError("injected") #'},
                   {'alpha': 2}, {'ef': True}]
        for value in invalid:
            with tempfile.TemporaryDirectory() as directory:
                work = Path(directory)
                with tarfile.open(package) as archive:
                    archive.extractall(work, filter='data')
                root = next(p for p in work.iterdir() if p.is_dir())
                side = root / 'retrieval_config.json'
                side.write_text(json.dumps(value))
                manifest_path = root / 'manifest.json'
                manifest = json.loads(manifest_path.read_text())
                manifest['files'][side.name] = 'sha256:' + hashlib.sha256(side.read_bytes()).hexdigest()
                manifest_path.write_text(json.dumps(manifest))
                with tempfile.NamedTemporaryFile(prefix='vfy-retrieval-', suffix='.tar.gz',
                                                 dir=package.parent) as output:
                    with tarfile.open(output.name, 'w:gz') as archive:
                        archive.add(root, arcname=root.name)
                    for conflict in ('abort', 'rename', 'replace'):
                        job_id = request('/import', {'filename': Path(output.name).name,
                                                     'on_conflict': conflict})['job_id']
                        deadline = time.monotonic() + 120
                        while True:
                            job = request('/import/job/' + job_id)
                            if job['status'] in ('failed', 'completed'):
                                break
                            if time.monotonic() >= deadline:
                                raise AssertionError('Retrieval settings import timed out')
                            time.sleep(0.2)
                        assert job['status'] == 'failed', job
                        assert job['error_code'] == 'PACKAGE_CORRUPT', job
                        assert job['error_detail'] == {'file': 'retrieval_config.json'}, job
                        assert request('/retrieval/config/' + collection) == expected
                        assert collections() == baseline
        print('15 digest-valid malformed retrieval imports rejected; live collections/settings preserved')
    finally:
        request('/retrieval/config', saved)


if __name__ == '__main__':
    run(*sys.argv[1:])
