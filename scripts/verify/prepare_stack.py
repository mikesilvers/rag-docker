"""Document preparation checks on the verify project (#638), one per subcommand.
Host-side, standard library, Python 3.9+. Run by 21_prepare.sh, which turns each
exit status into a check:

    python3 prepare_stack.py CHECK FIXTURES_DIR

Exit 0 when the check holds, 1 when it doesn't (the reason on stderr). Requests
are sent as the harness Developer (End User and no session for the role checks).
Documents this suite uploads are named vfy-prepare-*, and `cleanup` deletes them
with the VfyPrepare* collections. State between checks is a small JSON file in
the harness's private folder. SPECIFICATIONS.md §10.8, P1-P11.
"""
import io
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import auth_setup
from auth_setup import request
from auth_stack import CheckFailed, code, error_code, expect, role_token

TERMINAL = ("completed", "failed", "partial", "cancelled")
STATE = Path(auth_setup.private_dir()) / "prepare-state.json"
COLLECTION_PREFIX = "VfyPrepare"

PAGES = "\n".join(
    line for page in range(1, 4) for line in (
        ["ACME CORP CONFIDENTIAL"]
        + [f"Paragraph {page}.{n}: the committee reviewed item {page * 10 + n} and agreed to carry it "
           f"forward to the next session." for n in range(6)]
        + [f"Page {page}", ""])) + "\n"
CSV = "name,team\nAda,core\nBo,docs\nCy,ops\n"
# The cleaning windows and the fidelity rule of api/services/preparation.py
# (WINDOW_CHARACTERS, MAX_REMOVED_SHARE), for P9's live check of the rule (#640).
WINDOW_CHARACTERS = 3000
MAX_REMOVED_SHARE = 0.20
JSONL = '{"id": 1, "text": "first record"}\n{"id": 2, "text": "second record"}\n'


def state():
    try:
        return json.loads(STATE.read_text())
    except (OSError, ValueError):
        return {}


def save(data):
    STATE.write_text(json.dumps(data))
    os.chmod(STATE, 0o600)


def send(method, path, token, body=None, files=None, raw=False):
    """(status, parsed JSON or bytes, headers) for a request as a same-origin browser."""
    headers = {"Origin": auth_setup.origin()}
    if token:
        headers["Cookie"] = "rag_session=" + token
    data = None
    if files is not None:
        boundary = "vfy" + secrets.token_hex(8)
        parts = []
        for name, content in files:
            parts.append('--{}\r\nContent-Disposition: form-data; name="files"; filename="{}"\r\n'
                         'Content-Type: application/octet-stream\r\n\r\n'.format(boundary, name).encode()
                         + content + b"\r\n")
        parts.append("--{}--\r\n".format(boundary).encode())
        data = b"".join(parts)
        headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(auth_setup.api_base() + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as response:
            status, content, got = response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        status, content, got = error.code, error.read(), error.headers
    if raw:
        return status, content, got
    try:
        return status, json.loads(content) if content else None, got
    except ValueError:
        return status, content, got


def documents(token):
    status, body, _ = send("GET", "/prepare/documents", token)
    expect(status == 200, "GET /prepare/documents answered {}".format(status))
    return body


def ours(token):
    return [d for d in documents(token)["documents"] if d["name"].startswith(("vfy-prepare-", "vfy/"))]


def zip_bytes(members):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def wait_job(token, path, timeout=1800):
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, job, _ = send("GET", path, token)
        expect(status == 200, "{} answered {}".format(path, status))
        if job["status"] in TERMINAL:
            return job
        time.sleep(2)
    raise CheckFailed("{} didn't end within {}s".format(path, timeout))


# ── checks ───────────────────────────────────────────────────────────────────

ROUTES = [("POST", "/prepare/upload"), ("GET", "/prepare/documents"),
          ("GET", "/prepare/documents/aaaaaaaaaaaa"), ("GET", "/prepare/documents/aaaaaaaaaaaa/raw"),
          ("GET", "/prepare/documents/aaaaaaaaaaaa/prepared"), ("DELETE", "/prepare/documents/aaaaaaaaaaaa"),
          ("DELETE", "/prepare/documents/aaaaaaaaaaaa/prepared"), ("POST", "/prepare/jobs"),
          ("GET", "/prepare/jobs"), ("GET", "/prepare/job/none"), ("POST", "/prepare/job/none/cancel"),
          ("POST", "/prepare/import")]


def check_access(fix):
    """P1: every route refuses no session (401) and an End User (403), and admits a Developer."""
    wrong = []
    end_user = role_token("end_user")
    developer = role_token("developer")
    for method, path in ROUTES:
        body = {} if method == "POST" and path != "/prepare/upload" else None
        status, raw, _ = code(method, path, body=body)
        if status != 401:
            wrong.append("{} {} without a session -> {}".format(method, path, status))
        status, raw, _ = code(method, path, body=body, token=end_user)
        if status != 403:
            wrong.append("{} {} as End User -> {}".format(method, path, status))
        status, raw, _ = code(method, path, body=body, token=developer)
        if status in (401, 403):
            wrong.append("{} {} as Developer -> {} {}".format(method, path, status, error_code(raw)))
    request("POST", "/auth/logout", token=end_user)
    expect(not wrong, "; ".join(wrong))


def check_upload(fix):
    """P2: a .txt, a .csv and a ZIP with a .jsonl and an unsupported member."""
    token = role_token("developer")
    status, body, _ = send("POST", "/prepare/upload", token, files=[
        ("vfy-prepare-minutes.txt", PAGES.encode()), ("vfy-prepare-people.csv", CSV.encode()),
        ("vfy-prepare-bundle.zip", zip_bytes({"vfy/records.jsonl": JSONL, "vfy/tool.exe": "no"}))])
    expect(status == 201, "upload answered {} {}".format(status, body))
    names = sorted(d["name"] for d in body["documents"])
    expect(names == ["vfy-prepare-minutes.txt", "vfy-prepare-people.csv", "vfy/records.jsonl"],
           "documents {}".format(names))
    expect(all(d["status"] == "raw" for d in body["documents"]), "not all raw")
    expect(any("tool.exe" in s for s in body["skipped"]), "skipped {}".format(body["skipped"]))
    save({"ids": {d["name"]: d["id"] for d in body["documents"]}})


def check_archives(fix):
    """P3: a ZIP over the ratio limit and a damaged ZIP are refused; nothing is kept."""
    token = role_token("developer")
    before = documents(token)["documents"]
    for name, want in (("zip_high_ratio.zip", "ARCHIVE_RATIO_TOO_HIGH"), ("truncated.zip", "ARCHIVE_UNREADABLE")):
        status, body, _ = send("POST", "/prepare/upload", token,
                               files=[("vfy-prepare-" + name, (Path(fix) / name).read_bytes())])
        expect(status == 400 and body["error"]["code"] == want, "{} -> {} {}".format(name, status, body))
    expect(documents(token)["documents"] == before, "the list changed")


def check_download(fix):
    """P4: raw download returns the uploaded bytes; the list reports its usage and limit."""
    token = role_token("developer")
    ids = state()["ids"]
    status, content, headers = send("GET", "/prepare/documents/{}/raw".format(ids["vfy-prepare-people.csv"]),
                                    token, raw=True)
    expect(status == 200 and content == CSV.encode(), "raw download {} {!r}".format(status, content[:60]))
    expect("vfy-prepare-people.csv" in headers.get("Content-Disposition", ""), "no file name")
    listing = documents(token)
    expect(listing["used_bytes"] > 0 and listing["limit_bytes"] >= 1024 * 1024,
           "usage {} of {}".format(listing["used_bytes"], listing["limit_bytes"]))


def check_delete(fix):
    """P5: deleting a document removes it and lowers the usage; an unknown id is 404."""
    token = role_token("developer")
    status, body, _ = send("POST", "/prepare/upload", token, files=[("vfy-prepare-gone.md", b"x" * 4096)])
    expect(status == 201, "upload answered {}".format(status))
    doc = body["documents"][0]
    used = documents(token)["used_bytes"]
    status, _, _ = send("DELETE", "/prepare/documents/" + doc["id"], token)
    expect(status == 204, "delete answered {}".format(status))
    listing = documents(token)
    expect(all(d["id"] != doc["id"] for d in listing["documents"]), "still listed")
    expect(listing["used_bytes"] < used, "usage {} not below {}".format(listing["used_bytes"], used))
    status, body, _ = send("DELETE", "/prepare/documents/" + doc["id"], token)
    expect(status == 404 and body["error"]["code"] == "DOCUMENT_NOT_FOUND", "second delete {}".format(status))


def check_import_refusals(fix):
    """P6: a raw document 409, an unknown document 404, an empty selection 400, invalid settings 422."""
    token = role_token("developer")
    raw_id = state()["ids"]["vfy-prepare-people.csv"]
    cases = [({"collection": "VfyPrepareNone", "document_ids": [raw_id]}, 409, "DOCUMENT_NOT_PREPARED"),
             ({"collection": "VfyPrepareNone", "document_ids": ["bbbbbbbbbbbb"]}, 404, "DOCUMENT_NOT_FOUND"),
             ({"collection": "VfyPrepareNone", "document_ids": []}, 400, "NO_DOCUMENTS"),
             ({"collection": "VfyPrepareNone", "all": True, "chunk_size": 100, "chunk_overlap": 100},
              422, "INVALID_PARAMETER")]
    wrong = []
    for body, status_want, code_want in cases:
        status, answer, _ = send("POST", "/prepare/import", token, body=body)
        got = answer.get("error", {}).get("code") if isinstance(answer, dict) else None
        if (status, got) != (status_want, code_want):
            wrong.append("{} -> {} {}".format(body, status, got))
    expect(not wrong, "; ".join(wrong))


def check_cancel_unknown(fix):
    """P7: cancelling an unknown job is 404."""
    status, body, _ = send("POST", "/prepare/job/nosuchjob/cancel", role_token("developer"))
    expect(status == 404 and body["error"]["code"] == "JOB_NOT_FOUND", "cancel answered {}".format(status))


def check_audit(fix):
    """P8: prepare.upload, prepare.download and prepare.delete records with names, no content."""
    admin = role_token("engineer")
    found = {}
    for action in ("prepare.upload", "prepare.download", "prepare.delete"):
        query = urllib.parse.urlencode({"action": action, "limit": 200})
        status, raw, _ = code("GET", "/audit?" + query, token=admin)
        expect(status == 200, "GET /audit answered {}".format(status))
        found[action] = json.loads(raw)["records"]
    expect(any("vfy-prepare-minutes.txt" in (r["details"].get("files") or []) for r in found["prepare.upload"]),
           "no prepare.upload record naming the upload")
    expect(any(r.get("target") == "vfy-prepare-people.csv" and r["details"].get("kind") == "raw"
               for r in found["prepare.download"]), "no prepare.download record of the raw CSV")
    expect(any(r.get("target") == "vfy-prepare-gone.md" for r in found["prepare.delete"]),
           "no prepare.delete record")
    text = json.dumps(found)
    expect("committee reviewed" not in text and "Ada,core" not in text, "document text in the audit records")


# ── with the LLM (full runs only) ────────────────────────────────────────────

def text_windows(lines):
    """The cleaning windows of a part's lines, as preparation.windows() groups them:
    whole lines in order while their characters (each line's length + 1) fit in
    WINDOW_CHARACTERS; a longer line is a window of its own. Each window is the
    list of its line indexes."""
    out, current, size = [], [], 0
    for index, line in enumerate(lines):
        if len(line) + 1 > WINDOW_CHARACTERS:
            if current:
                out.append(current)
                current, size = [], 0
            out.append([index])
            continue
        if current and size + len(line) + 1 > WINDOW_CHARACTERS:
            out.append(current)
            current, size = [], 0
        current.append(index)
        size += len(line) + 1
    if current:
        out.append(current)
    return out


def removed_too_much(source, kept, fallbacks):
    """The first window without a fallback whose removed lines hold more than
    MAX_REMOVED_SHARE of its characters, as (number, removed, characters), or None.
    `kept` is the set of source line indexes the prepared text kept (#640)."""
    fell_back = {f["window"] for f in fallbacks if f.get("window") is not None}
    for number, window in enumerate(text_windows(source), 1):
        if number in fell_back:
            continue
        characters = sum(len(source[i]) + 1 for i in window)
        removed = sum(len(source[i]) + 1 for i in window if i not in kept)
        if removed > MAX_REMOVED_SHARE * characters:
            return number, removed, characters
    return None


def check_prepare(fix):
    """P9: preparing the .txt, the .csv and the .jsonl; the .txt keeps only source
    lines, in order, and every window without a fallback lost at most 20 % of its
    characters (#640); the .csv and .jsonl are labelled from their data."""
    token = role_token("developer")
    ids = state()["ids"]
    status, body, _ = send("POST", "/prepare/jobs", token, body={"document_ids": list(ids.values())})
    expect(status == 202, "start answered {} {}".format(status, body))
    job = wait_job(token, "/prepare/job/" + body["job_id"])
    expect(job["status"] == "completed", "job ended {}: {}".format(job["status"], job["errors"]))
    txt = send("GET", "/prepare/documents/" + ids["vfy-prepare-minutes.txt"], token)[1]
    expect(txt["status"] == "prepared" and len(txt["parts"]) == 1, "txt {}".format(txt["status"]))
    part = txt["parts"][0]
    expect(part["name"] == "vfy-prepare-minutes.md", "part named {}".format(part["name"]))
    status, content, _ = send("GET", "/prepare/documents/{}/prepared".format(txt["id"]), token, raw=True)
    expect(status == 200, "prepared download {}".format(status))
    text = content.decode("utf-8")
    lines = text.rstrip("\n").split("\n")
    if part["title"]:
        header = "# {}".format(part["title"])
        expect(lines[0] == header and lines[2] == "Summary (generated): {}".format(part["summary"]),
               "no header: {!r}".format(lines[:3]))
        lines = lines[4:]
    # The part's text as preparation reads the .txt: its paragraphs joined by
    # one blank line, then cut into the windows the LLM saw.
    source = "\n\n".join(block.strip("\n") for block in PAGES.split("\n\n")
                          if block.strip()).split("\n")
    position = 0
    kept = set()
    for line in lines:
        try:
            position = source.index(line, position) + 1
        except ValueError:
            raise CheckFailed("a line that isn't a source line, in order: {!r}".format(line))
        kept.add(position - 1)
    lost = removed_too_much(source, kept, part["fallbacks"])
    expect(lost is None, "window {} lost {} of {} characters, more than 20 %".format(*(lost or (0, 0, 0))))
    for reason in (f["reason"] for f in part["fallbacks"]):
        expect(reason in ("removed-too-much", "removed-long-line", "unusable-answer", "llm-timeout",
                          "llm-error", "llm-unavailable", "too-large-for-llm"), "reason {}".format(reason))
    labels = {"vfy-prepare-people.csv": "3 records. Fields: name, team. First: Ada; last: Cy.",
              "vfy/records.jsonl": "2 records. Fields: id, text. First: 1; last: 2."}
    for name, want in (("vfy-prepare-people.csv", CSV), ("vfy/records.jsonl", JSONL)):
        record = send("GET", "/prepare/documents/" + ids[name], token)[1]
        expect(record["status"] == "prepared" and record["code_only_by_type"], "{} {}".format(name, record["status"]))
        status, content, _ = send("GET", "/prepare/documents/{}/prepared".format(record["id"]), token, raw=True)
        expect(content.decode("utf-8") == want, "{} changed: {!r}".format(name, content[:80]))
        # Labelled from the data, with no LLM call (#648).
        [part] = record["parts"]
        got = (part["title"], part["summary"], part["summary_error"])
        expect(got == (name, labels[name], None), "{} labelled {!r}".format(name, got))


def check_import(fix):
    """P10: an unknown collection is 404; an import into a new collection completes."""
    token = role_token("developer")
    ids = state()["ids"]
    status, body, _ = send("POST", "/prepare/import", token,
                           body={"collection": "VfyPrepareMissing", "document_ids": list(ids.values())})
    expect(status == 404 and body["error"]["code"] == "COLLECTION_NOT_FOUND", "missing collection {}".format(status))
    collection = COLLECTION_PREFIX + secrets.token_hex(3)
    status, raw, _ = code("POST", "/collections", body={
        "name": collection, "index_type": "hnsw", "distance_metric": "cosine",
        "hnsw_config": {"efConstruction": 128, "maxConnections": 64, "ef": 64}}, token=token)
    expect(status in (200, 201), "create collection {}".format(status))
    status, body, _ = send("POST", "/prepare/import", token, body={
        "collection": collection, "document_ids": list(ids.values()), "chunk_size": 500, "chunk_overlap": 50})
    expect(status == 202, "import answered {} {}".format(status, body))
    job = wait_job(token, "/ingest/job/" + body["job_id"])
    expect(job["status"] == "completed", "ingest ended {}: {}".format(job["status"], job["errors"]))
    status, raw, _ = code("GET", "/collections", token=token)
    expect(status == 200, "collections {}".format(status))
    for document_id in ids.values():
        record = send("GET", "/prepare/documents/" + document_id, token)[1]
        expect(record["imports"] and record["imports"][-1]["collection"] == collection
               and record["imports"][-1]["job_id"] == body["job_id"], "imports {}".format(record["imports"]))
    data = state()
    data["collection"] = collection
    save(data)


def check_prepared_download(fix):
    """P11: the prepared download is the part; delete prepared returns the document to raw."""
    token = role_token("developer")
    csv_id = state()["ids"]["vfy-prepare-people.csv"]
    status, content, headers = send("GET", "/prepare/documents/{}/prepared".format(csv_id), token, raw=True)
    expect(status == 200 and content == CSV.encode(), "prepared download {}".format(status))
    status, _, _ = send("DELETE", "/prepare/documents/{}/prepared".format(csv_id), token)
    expect(status == 204, "delete prepared {}".format(status))
    record = send("GET", "/prepare/documents/" + csv_id, token)[1]
    expect(record["status"] == "raw" and record["parts"] == [] and record["imports"], "after delete {}".format(record))


def check_cleanup(fix):
    token = role_token("developer")
    for doc in ours(token):
        send("DELETE", "/prepare/documents/" + doc["id"], token)
    admin = role_token("engineer")
    status, raw, _ = code("GET", "/collections", token=admin)
    if status == 200:
        for item in json.loads(raw)["collections"]:
            if item["name"].startswith(COLLECTION_PREFIX):
                code("DELETE", "/collections/{}?confirm=true".format(item["name"]), token=admin)
    STATE.unlink(missing_ok=True)
    expect(not ours(token), "documents left")


CHECKS = {
    "access": check_access, "upload": check_upload, "archives": check_archives,
    "download": check_download, "delete": check_delete, "import-refusals": check_import_refusals,
    "cancel-unknown": check_cancel_unknown, "audit": check_audit, "prepare": check_prepare,
    "import": check_import, "prepared-download": check_prepared_download, "cleanup": check_cleanup,
}


def main(argv):
    if len(argv) != 3 or argv[1] not in CHECKS:
        print("usage: prepare_stack.py {} FIXTURES_DIR".format("|".join(CHECKS)), file=sys.stderr)
        return 2
    try:
        CHECKS[argv[1]](argv[2])
    except CheckFailed as exc:
        print("    {}".format(exc), file=sys.stderr)
        return 1
    except Exception as exc:                                # noqa: BLE001
        print("    {}: {}".format(type(exc).__name__, exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
