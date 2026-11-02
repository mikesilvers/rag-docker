"""Running and recent job lists on the verify project (#617). Host-side,
standard library, Python 3.9+. Run by 02_ingest.sh:

    python3 job_lists.py --operator

Exit 0 when every check holds, 1 when one doesn't (the reason on stderr).
The harness Developer and a throwaway vfy-s-<random> Developer-role service
principal each ingest a small file into a throwaway VfyJobs<random>
collection; the Developer then exports it and imports the package under a new
name. GET /ingest/jobs, /export/jobs and /import/jobs must show each caller
only its own jobs, and the harness Engineer everyone's with their owners.
Needs --operator (02_ingest.sh passes it only on the verify project as
stack.sh sets it up). The key is held in memory only and never printed.
"""
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import auth_setup
from auth_setup import request
from auth_stack import CheckFailed, bearer, code, error_code, expect, new_principal, role_token

TERMINAL = ("completed", "failed", "partial", "cancelled")
DEVELOPER = auth_setup.ROLES["developer"]


def upload(collection, name, content, token=None, headers=None):
    """POST /ingest/upload of one text file, with a session or a key's headers."""
    boundary = "vfy" + secrets.token_hex(8)
    body = ('--{b}\r\nContent-Disposition: form-data; name="collection"\r\n\r\n{c}\r\n'
            '--{b}\r\nContent-Disposition: form-data; name="files"; filename="{n}"\r\n'
            'Content-Type: text/plain\r\n\r\n').format(b=boundary, c=collection, n=name).encode()
    body += content + "\r\n--{}--\r\n".format(boundary).encode()
    sent = dict(headers) if headers else {"Origin": auth_setup.origin(), "Cookie": "rag_session=" + token}
    sent["Content-Type"] = "multipart/form-data; boundary=" + boundary
    req = urllib.request.Request(auth_setup.api_base() + "/ingest/upload", data=body, method="POST", headers=sent)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def get(path, token=None, headers=None):
    status, raw, _ = code("GET", path, token=token, headers=headers)
    expect(status == 200, "GET {} answered {} {}".format(path, status, error_code(raw)))
    return json.loads(raw)


def wait_job(path, token=None, headers=None, timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = get(path, token, headers)
        if job["status"] in TERMINAL:
            return job
        time.sleep(1)
    raise CheckFailed("{} didn't end within {}s".format(path, timeout))


def listed(path, token=None, headers=None):
    """The entries of a job list, by job ID."""
    return {job["job_id"]: job for job in get(path, token, headers)["jobs"]}


def main(argv):
    if "--operator" not in argv:
        print("creates a collection and a service principal, so it runs only on the verify project",
              file=sys.stderr)
        return 1
    dev = role_token("developer")
    admin = role_token("engineer")
    name = "VfyJobs" + secrets.token_hex(4)
    imported = package = principal = None
    try:
        principal, _, key = new_principal(admin, "developer")
        as_key = bearer(key)
        status, raw, _ = code("POST", "/collections", {"name": name}, token=dev)
        expect(status == 201, "creating {} answered {} {}".format(name, status, error_code(raw)))
        name = json.loads(raw)["name"]

        status, mine = upload(name, "vfy-jobs-dev.txt", b"A note the Developer imports.\n" * 20, token=dev)
        expect(status == 202, "the Developer's upload answered {} {}".format(status, mine))
        status, keys = upload(name, "vfy-jobs-key.txt", b"A note the key imports.\n" * 20, headers=as_key)
        expect(status == 202, "the key's upload answered {} {}".format(status, keys))
        dev_id, key_id = mine["job_id"], keys["job_id"]

        # While they run, or once they have ended: each sees its own job only.
        seen = listed("/ingest/jobs", token=dev)
        expect(dev_id in seen and key_id not in seen, "the Developer's list holds {}".format(sorted(seen)))
        entry = seen[dev_id]
        expect(entry["owner"] == {"name": DEVELOPER, "kind": "user"}, "the owner is {}".format(entry["owner"]))
        expect(entry["collection"] == name and entry["files"] == ["vfy-jobs-dev.txt"],
               "the entry names {} {}".format(entry["collection"], entry["files"]))
        expect(entry["created_at"], "the entry has no created_at")
        seen = listed("/ingest/jobs", headers=as_key)
        expect(key_id in seen and dev_id not in seen, "the key's list holds {}".format(sorted(seen)))
        expect(seen[key_id]["owner"] == {"name": principal, "kind": "service"},
               "the key's job owner is {}".format(seen[key_id]["owner"]))

        for job_id, who in ((dev_id, {"token": dev}), (key_id, {"headers": as_key})):
            job = wait_job("/ingest/job/" + job_id, **who)
            expect(job["status"] == "completed", "ingest {} ended {}".format(job_id, job["status"]))
            expect("owner" not in job and "created_at" not in job, "the lookup by ID gained the new fields")
        seen = listed("/ingest/jobs", token=dev)
        expect(seen.get(dev_id, {}).get("finished_at"), "the finished job has no finished_at in the list")
        everyone = listed("/ingest/jobs", token=admin)
        expect(dev_id in everyone and key_id in everyone, "the Engineer's list lacks a job")
        expect(everyone[key_id]["owner"] == {"name": principal, "kind": "service"},
               "the Engineer sees the key's job owned by {}".format(everyone[key_id]["owner"]))

        status, raw, _ = code("POST", "/export", {"collection": name}, token=dev)
        expect(status == 202, "the export answered {} {}".format(status, error_code(raw)))
        export_id = json.loads(raw)["job_id"]
        expect(export_id in listed("/export/jobs", token=dev), "the running or ended export isn't listed")
        export = wait_job("/export/job/" + export_id, token=dev)
        package = export.get("filename")
        expect(export["status"] == "completed", "the export ended {}".format(export["status"]))
        status, raw, _ = code("POST", "/import", {"filename": package, "on_conflict": "rename"}, token=dev)
        expect(status == 202, "the import answered {} {}".format(status, error_code(raw)))
        import_id = json.loads(raw)["job_id"]
        job = wait_job("/import/job/" + import_id, token=dev)
        imported = job.get("collection")
        expect(job["status"] == "completed", "the import ended {} {}".format(job["status"], job.get("error")))

        for path, job_id in (("/export/jobs", export_id), ("/import/jobs", import_id)):
            seen = listed(path, token=dev)
            expect(job_id in seen and seen[job_id]["owner"] == {"name": DEVELOPER, "kind": "user"}
                   and seen[job_id]["created_at"] and seen[job_id]["finished_at"],
                   "{} for the Developer: {}".format(path, seen.get(job_id)))
            expect(job_id in listed(path, token=admin), "{} for the Engineer lacks {}".format(path, job_id))
            expect(job_id not in listed(path, headers=as_key), "{} for the key holds {}".format(path, job_id))
        return 0
    except CheckFailed as failure:
        print(str(failure), file=sys.stderr)
        return 1
    finally:
        for collection in (name, imported):
            if collection:
                code("DELETE", "/collections/{}?confirm=true".format(collection), token=admin)
        if package:
            exports = Path(os.environ.get("RAG_EXPORTS_DIR") or auth_setup.REPO / "exports")
            (exports / package).unlink(missing_ok=True)
        if principal:
            code("POST", "/auth/service-principals/{}/disable".format(principal), token=admin)
        request("POST", "/auth/logout", token=dev)
        request("POST", "/auth/logout", token=admin)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
