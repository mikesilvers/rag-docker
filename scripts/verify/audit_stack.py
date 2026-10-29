"""Audit log checks on the verify project (#29), one per subcommand. Host-side,
standard library, Python 3.9+. Run by 19_audit.sh, which turns each exit status
into a check:

    python3 audit_stack.py CHECK [--operator]

Exit 0 when the check holds, 1 when it doesn't (the reason on stderr). Records
are read through GET /audit as the harness Engineer. Checks that create
throwaway collections (VfyAudit<random>) or service principals (vfy-s-<random>)
need --operator, which 19_audit.sh passes only on the verify project as
stack.sh sets it up. No password, session token, API key or passphrase is
printed; keys and passphrases are held in memory only.
"""
import csv
import io
import ipaddress
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import auth_setup
from auth_setup import request
from auth_stack import CheckFailed, bearer, code, compose, error_code, expect, new_principal, role_token

ENGINEER = auth_setup.ROLES["engineer"]
DEVELOPER = auth_setup.ROLES["developer"]
END_USER = auth_setup.ROLES["end_user"]
STATE = "audit-restart-state.json"
TERMINAL = ("completed", "failed", "partial", "cancelled")


def records(admin, **filters):
    """A page of records through GET /audit, newest first."""
    query = urllib.parse.urlencode({k: v for k, v in filters.items() if v is not None})
    status, raw, _ = code("GET", "/audit" + ("?" + query if query else ""), token=admin)
    expect(status == 200, "GET /audit answered {} {}".format(status, error_code(raw)))
    return json.loads(raw)


def find(admin, action, **match):
    """The newest record of ACTION whose fields equal MATCH."""
    for item in records(admin, action=action, limit=1000)["records"]:
        if all(item.get(k) == v for k, v in match.items()):
            return item
    raise CheckFailed("no {} record with {}".format(action, match))


def valid_address(item):
    try:
        ipaddress.ip_address(item.get("client_ip") or "")
        return True
    except ValueError:
        return False


def expect_actor(item, actor, role):
    expect((item["actor"], item["role"], item["actor_kind"]) == (actor, role, "user"),
           "{} names {} {} {}, not {} {}".format(item["action"], item["actor"], item["role"], item["actor_kind"],
                                                 actor, role))
    expect(valid_address(item), "{} has no valid client address ({!r})".format(item["action"], item["client_ip"]))


def audit_files():
    """The raw text of every audit file in the verify project's api container."""
    result = compose("exec", "-T", "api", "sh", "-c", "cat /app/uploads/audit/audit.jsonl*")
    expect(result.returncode == 0, "could not read the audit files")
    return result.stdout


def upload(token, collection, name, content):
    """POST /ingest/upload of one small text file, as a same-origin browser."""
    boundary = "vfy" + secrets.token_hex(8)
    parts = []
    for field, value in (("collection", collection), ("chunk_size", "500"), ("chunk_overlap", "50")):
        parts.append('--{}\r\nContent-Disposition: form-data; name="{}"\r\n\r\n{}\r\n'.format(boundary, field, value)
                     .encode())
    parts.append('--{}\r\nContent-Disposition: form-data; name="files"; filename="{}"\r\nContent-Type: text/plain'
                 '\r\n\r\n'.format(boundary, name).encode() + content + b"\r\n")
    parts.append("--{}--\r\n".format(boundary).encode())
    req = urllib.request.Request(auth_setup.api_base() + "/ingest/upload", data=b"".join(parts), method="POST",
                                 headers={"Origin": auth_setup.origin(), "Cookie": "rag_session=" + token,
                                          "Content-Type": "multipart/form-data; boundary=" + boundary})
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def wait_job(token, path, timeout=900):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, raw, _ = code("GET", path, token=token)
        expect(status == 200, "{} answered {}".format(path, status))
        job = json.loads(raw)
        if job["status"] in TERMINAL:
            return job
        time.sleep(1)
    raise CheckFailed("{} didn't end within {}s".format(path, timeout))


# ── checks ───────────────────────────────────────────────────────────────────

def check_access(operator_ok):
    expect(operator_ok, "creates a service principal, so it runs only on the verify project")
    wrong = []
    for role in ("end_user", "developer"):
        token = role_token(role)
        for path in ("/audit", "/audit.csv"):
            status, raw, _ = code("GET", path, token=token)
            if (status, error_code(raw)) != (403, "FORBIDDEN"):
                wrong.append("{} {} -> {} {}".format(role, path, status, error_code(raw)))
        request("POST", "/auth/logout", token=token)
    admin = role_token("engineer")
    name, key_id, key = new_principal(admin, "engineer")
    try:
        for path in ("/audit", "/audit.csv"):
            status, raw, _ = code("GET", path, headers=bearer(key))
            if (status, error_code(raw)) != (403, "KEY_NOT_ALLOWED"):
                wrong.append("engineer key {} -> {} {}".format(path, status, error_code(raw)))
        refused = find(admin, "audit.view", actor=name)
        if (refused["actor_kind"], refused["key_id"], refused["result"]) != ("service", key_id, "refused"):
            wrong.append("the key's refusal was recorded as {}".format(refused))
        status, raw, _ = code("GET", "/audit?limit=1", token=admin)
        if status != 200:
            wrong.append("engineer session /audit -> {}".format(status))
    finally:
        code("POST", "/auth/service-principals/{}/keys/{}/revoke".format(name, key_id), token=admin)
        code("POST", "/auth/service-principals/{}/disable".format(name), token=admin)
        request("POST", "/auth/logout", token=admin)
    expect(not wrong, "; ".join(wrong))


def check_sessions(operator_ok):
    token = role_token("end_user")
    request("POST", "/auth/logout", token=token)
    typed = "vfy-nobody-" + secrets.token_hex(4)
    request("POST", "/auth/login", {"username": typed, "password": "not-a-password-" + secrets.token_hex(4)})
    admin = role_token("engineer")
    try:
        login = find(admin, "auth.login", actor=END_USER)
        expect_actor(login, END_USER, "end_user")
        expect((login["result"], login["status_code"]) == ("success", 200), "login recorded as {}".format(login))
        logout = find(admin, "auth.logout", actor=END_USER)
        expect_actor(logout, END_USER, "end_user")
        failed = find(admin, "auth.login", target=typed)
        expect((failed["actor"], failed["actor_kind"], failed["result"], failed["status_code"],
                failed["details"].get("code")) == (None, "anonymous", "refused", 401, "LOGIN_FAILED"),
               "the failed login was recorded as {}".format(failed))
        expect(valid_address(failed), "the failed login has no valid address")
    finally:
        request("POST", "/auth/logout", token=admin)


def check_query(operator_ok):
    token = role_token("end_user")
    question = "VfyAuditQuestion " + secrets.token_hex(6)
    missing = "VfyAuditNoSuch" + secrets.token_hex(3)
    status, raw, _ = code("POST", "/query", {"question": question, "collection": missing, "top_k": 2}, token=token)
    request("POST", "/auth/logout", token=token)
    expect(status == 404, "the query answered {} {}".format(status, error_code(raw)))
    admin = role_token("engineer")
    try:
        item = find(admin, "query", collection=missing)
        expect_actor(item, END_USER, "end_user")
        expect(item["details"].get("top_k") == 2 and "question" not in item["details"],
               "the query's details are {}".format(item["details"]))
        expect(question not in audit_files(), "the question text is in the audit files")
        expect(records(admin, limit=1)["question_text_logged"] is False, "question text is said to be logged")
    finally:
        request("POST", "/auth/logout", token=admin)


def check_jobs(operator_ok):
    """A collection created, ingested into, exported (encrypted) and deleted."""
    expect(operator_ok, "creates a collection, so it runs only on the verify project")
    dev = role_token("developer")
    admin = role_token("engineer")
    name = "VfyAudit" + secrets.token_hex(4)
    passphrase = "vfy audit " + secrets.token_hex(12)
    package = None
    try:
        status, raw, _ = code("POST", "/collections", {"name": name}, token=dev)
        expect(status == 201, "creating {} answered {} {}".format(name, status, error_code(raw)))
        name = json.loads(raw)["name"]
        status, started = upload(dev, name, "vfy-audit-note.txt",
                                 b"The audit log records who imported this note and when.\n" * 20)
        expect(status == 202, "the upload answered {} {}".format(status, started))
        ingest = wait_job(dev, "/ingest/job/" + started["job_id"])
        expect(ingest["status"] == "completed", "the ingest ended {}".format(ingest["status"]))
        status, raw, _ = code("POST", "/export", {"collection": name, "passphrase": passphrase}, token=dev)
        expect(status == 202, "the export answered {} {}".format(status, error_code(raw)))
        export_id = json.loads(raw)["job_id"]
        export = wait_job(dev, "/export/job/" + export_id)
        package = export.get("filename")
        expect(export["status"] == "completed", "the export ended {}".format(export["status"]))
        status, raw, _ = code("DELETE", "/collections/{}?confirm=true".format(name), token=admin)
        expect(status == 200, "deleting {} answered {} {}".format(name, status, error_code(raw)))

        created = find(admin, "collection.create", collection=name)
        expect_actor(created, DEVELOPER, "developer")
        for action, job_id, ended in (("ingest", started["job_id"], "succeeded"), ("export", export_id, "succeeded")):
            pair = [r for r in records(admin, action=action, limit=1000)["records"] if r["job_id"] == job_id]
            results = sorted(r["result"] for r in pair)
            expect(results == sorted(["accepted", ended]), "{} {} has records {}".format(action, job_id, results))
            for item in pair:
                expect_actor(item, DEVELOPER, "developer")
                expect(item["collection"] == name, "{} record names collection {}".format(action, item["collection"]))
        accepted = find(admin, "ingest", job_id=started["job_id"], result="accepted")
        expect(accepted["details"].get("files") == ["vfy-audit-note.txt"] and accepted["status_code"] == 202,
               "the ingest record's details are {}".format(accepted["details"]))
        exported = find(admin, "export", job_id=export_id, result="succeeded")
        expect(exported["details"].get("encrypted") is True and exported["details"].get("package") == package,
               "the export's end details are {}".format(exported["details"]))
        deleted = find(admin, "collection.delete", collection=name)
        expect_actor(deleted, ENGINEER, "engineer")
        expect(deleted["result"] == "success", "the delete was recorded as {}".format(deleted["result"]))
        expect(passphrase not in audit_files(), "the export passphrase is in the audit files")
    finally:
        code("DELETE", "/collections/{}?confirm=true".format(name), token=admin)
        if package:
            exports = Path(os.environ.get("RAG_EXPORTS_DIR") or auth_setup.REPO / "exports")
            (exports / package).unlink(missing_ok=True)
        request("POST", "/auth/logout", token=dev)
        request("POST", "/auth/logout", token=admin)


def check_refused(operator_ok):
    dev = role_token("developer")
    missing = "VfyAuditRefused" + secrets.token_hex(3)
    status, raw, _ = code("DELETE", "/collections/{}?confirm=true".format(missing), token=dev)
    request("POST", "/auth/logout", token=dev)
    expect((status, error_code(raw)) == (403, "FORBIDDEN"), "the delete answered {} {}".format(status, error_code(raw)))
    admin = role_token("engineer")
    try:
        item = find(admin, "collection.delete", collection=missing)
        expect_actor(item, DEVELOPER, "developer")
        expect((item["result"], item["status_code"], item["details"].get("code")) == ("refused", 403, "FORBIDDEN"),
               "the refusal was recorded as {}".format(item))
    finally:
        request("POST", "/auth/logout", token=admin)


def check_admin(operator_ok):
    expect(operator_ok, "creates a service principal, so it runs only on the verify project")
    admin = role_token("engineer")
    try:
        name, key_id, key = new_principal(admin, "end_user")
        code("POST", "/auth/service-principals/{}/keys/{}/revoke".format(name, key_id), token=admin)
        code("POST", "/auth/service-principals/{}/disable".format(name), token=admin)
        created = find(admin, "admin.principal.create", target=name)
        expect_actor(created, ENGINEER, "engineer")
        expect(created["details"].get("role") == "end_user", "the create's details are {}".format(created["details"]))
        issued = find(admin, "admin.key.issue", target=name)
        expect(issued["details"].get("key_id") == key_id, "the issue names key {}".format(issued["details"]))
        find(admin, "admin.key.revoke", target=name)
        find(admin, "admin.principal.disable", target=name)
        expect(key not in audit_files(), "an issued key is in the audit files")
    finally:
        request("POST", "/auth/logout", token=admin)


def check_filters(operator_ok):
    typed = "=vfy" + secrets.token_hex(4)
    request("POST", "/auth/login", {"username": typed, "password": "not-a-password-" + secrets.token_hex(4)})
    request("POST", "/auth/login", {"username": typed, "password": "not-a-password-" + secrets.token_hex(4)})
    admin = role_token("engineer")
    try:
        page = records(admin, action="auth.login", result="refused", limit=1)
        expect(len(page["records"]) == 1 and page["next_before"], "a one-record page has no cursor: {}".format(page))
        first = page["records"][0]
        expect(first["target"] == typed, "the newest refused login is {}".format(first["target"]))
        older = records(admin, action="auth.login", result="refused", limit=1, before=page["next_before"])
        expect(older["records"] and older["records"][0]["id"] < first["id"] and older["records"][0]["target"] == typed,
               "the next page doesn't hold the next older record")
        expect(all(r["action"] == "audit.view" for r in records(admin, action="audit.view", limit=50)["records"]),
               "the action filter let other actions through")
        status, raw, headers = code("GET", "/audit.csv?" + urllib.parse.urlencode({"action": "auth.login"}),
                                    token=admin)
        expect(status == 200 and headers.get("Content-Type", "").startswith("text/csv"),
               "GET /audit.csv answered {} {}".format(status, headers.get("Content-Type")))
        expect("attachment" in headers.get("Content-Disposition", ""), "the CSV isn't an attachment")
        rows = list(csv.reader(io.StringIO(raw.decode("utf-8"))))
        expect(rows[0] == ["time", "actor", "actor_kind", "role", "key_id", "client_ip", "action", "collection",
                           "target", "result", "status_code", "job_id", "details"],
               "the CSV header is {}".format(rows[0]))
        targets = [dict(zip(rows[0], row)).get("target") for row in rows[1:]]
        expect("'" + typed in targets, "the CSV has no row with the escaped target")
        expect(typed not in targets, "a formula cell is not escaped")
    finally:
        request("POST", "/auth/logout", token=admin)


def check_start_record(operator_ok):
    admin = role_token("engineer")
    try:
        item = records(admin, action="system.start", limit=1)["records"]
        expect(item, "no system.start record")
        expect(item[0]["details"].get("question_text_logged") is False and item[0]["actor"] == "system",
               "the start record is {}".format(item[0]))
    finally:
        request("POST", "/auth/logout", token=admin)


def check_no_secrets(operator_ok):
    expect(operator_ok, "creates a service principal, so it runs only on the verify project")
    known = [password for _, password in auth_setup.read_credentials(auth_setup.private_dir()).values()]
    tokens = [role_token(role) for role in ("end_user", "developer", "engineer")]
    admin = tokens[-1]
    name, key_id, key = new_principal(admin, "end_user")
    try:
        code("GET", "/collections", headers=bearer(key))
        wrong = "vfy-wrong-" + secrets.token_hex(8)
        code("POST", "/auth/password", {"current_password": wrong, "new_password": wrong + "-new"}, token=tokens[0])
        text = audit_files()
        leaked = sum(1 for s in known + tokens + [key, key.split("_", 2)[2], wrong] if s and s in text)
        expect(leaked == 0, "{} secret(s) appear in the audit files".format(leaked))
    finally:
        code("POST", "/auth/service-principals/{}/keys/{}/revoke".format(name, key_id), token=admin)
        code("POST", "/auth/service-principals/{}/disable".format(name), token=admin)
        for token in tokens:
            request("POST", "/auth/logout", token=token)


def check_restart_prepare(operator_ok):
    typed = "vfy-restart-" + secrets.token_hex(4)
    request("POST", "/auth/login", {"username": typed, "password": "not-a-password-" + secrets.token_hex(4)})
    admin = role_token("engineer")
    try:
        item = find(admin, "auth.login", target=typed)
        start = records(admin, action="system.start", limit=1)["records"]
    finally:
        request("POST", "/auth/logout", token=admin)
    path = auth_setup.private_dir() / STATE
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"id": item["id"], "target": typed, "start": start[0]["id"] if start else ""}, handle)


def check_restart_verify(operator_ok):
    path = auth_setup.private_dir() / STATE
    state = json.loads(path.read_text())
    path.unlink()
    admin = role_token("engineer")
    try:
        item = find(admin, "auth.login", target=state["target"])
        expect(item["id"] == state["id"], "the record written before the restart changed")
        start = records(admin, action="system.start", limit=1)["records"]
        expect(start and start[0]["id"] > state["start"], "the restart wrote no new system.start record")
    finally:
        request("POST", "/auth/logout", token=admin)


CHECKS = {name[len("check_"):].replace("_", "-"): fn for name, fn in globals().items() if name.startswith("check_")}


def main(argv):
    operator_ok = "--operator" in argv
    names = [a for a in argv if a != "--operator"]
    if len(names) != 1 or names[0] not in CHECKS:
        print("usage: audit_stack.py {" + ", ".join(sorted(CHECKS)) + "} [--operator]", file=sys.stderr)
        return 2
    try:
        CHECKS[names[0]](operator_ok)
    except (CheckFailed, auth_setup.SetupError) as failure:
        print("    " + str(failure), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
