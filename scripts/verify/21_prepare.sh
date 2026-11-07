#!/usr/bin/env bash
# Document preparation (#638): SPECIFICATIONS.md §3.1.11 and §10.8.
#
# The unit checks (U1-U14) run in the API image with no network and a stub LLM.
# The stack checks (P1-P11) run on the verify project through prepare_stack.py,
# one check each, as the harness Developer. P9-P11 prepare three small files with
# the local LLM, one job at a time, so they run only in a full run
# (RAG_SKIP_SLOW unset). Documents this suite uploads are vfy-prepare-*, its
# collections VfyPrepare*; both are removed at the end.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -f "$FIX/zip_high_ratio.zip" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack

stack() { _rag_require_lock_owner; python3 ./prepare_stack.py "$1" "$FIX"; }

section "Preparation units: windows, fidelity, LLM limits, parts, JSON, storage, jobs, import, routes"
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_prepare.py)
check "test_prepare.py: the preparation area, pipeline, jobs, import and routes (U1-U14)" $?

stack cleanup >/dev/null 2>&1

section "Access and the area (P1-P8)"
stack access
check "P1 every /prepare route refuses no session (401) and an End User (403) and admits a Developer" $?
stack upload
check "P2 a .txt, a .csv and a ZIP's .jsonl become raw documents; the unsupported member is skipped" $?
stack archives
check "P3 a ZIP over the ratio limit and a damaged ZIP are refused with ingest's codes, nothing kept" $?
stack download
check "P4 the raw download is the uploaded bytes; the list reports the space used and the limit" $?
stack delete
check "P5 deleting a document removes it and lowers the space used; a second delete is 404" $?
stack import-refusals
check "P6 import refuses a raw document (409), an unknown one (404), no selection (400), bad settings (422)" $?
stack cancel-unknown
check "P7 cancelling an unknown job is 404" $?
stack audit
check "P8 the audit log records upload, download and delete with names and no document text" $?

section "Preparation with the local LLM (P9-P11)"
if [ "$SKIP_SLOW" = "1" ]; then
  skip "P9-P11 prepare, import and download with the LLM" "RAG_SKIP_SLOW=1"
else
  stack prepare
  check "P9 the .txt becomes cleaned Markdown of source lines in order with its header; the .csv and .jsonl are unchanged and labelled from their data" $?
  stack import
  check "P10 an unknown collection is 404; the import into a new collection completes and is recorded" $?
  p10_collection=$(python3 -c 'import prepare_stack as p; print(p.state().get("collection", ""))')
  p10_sources=$(cd "$REPO_ROOT" && docker compose exec -T api python - "$p10_collection" <<'ENDPY'
import json, sys
from services import weaviate_client as wc
try:
    coll = wc.get_client().collections.get(sys.argv[1])
    print(json.dumps(sorted({o.properties["source_file"] for o in coll.iterator()})))
finally:
    wc.close_client()
ENDPY
)
  python3 - "$p10_sources" <<'ENDPY'
import json, sys
import prepare_stack as p
from auth_stack import role_token
token = role_token("developer")
want = sorted(part["name"] for i in p.state()["ids"].values()
              for part in p.send("GET", "/prepare/documents/" + i, token)[1]["parts"])
got = json.loads(sys.argv[1] or "[]")
if want != got:
    print("    the collection holds {}, the parts are {}".format(got, want), file=sys.stderr)
sys.exit(0 if want and want == got else 1)
ENDPY
  check "P10 the collection's documents are named as the prepared parts, folder path included" $?
  stack prepared-download
  check "P11 the prepared download is the part; deleting the prepared output returns the document to raw" $?
  python3 - "$FIX" <<'ENDPY'
import sys
from pathlib import Path
import prepare_stack as p
from auth_stack import role_token
token = role_token("developer")
fix = Path(sys.argv[1])
status, body, _ = p.send("POST", "/prepare/upload", token, files=[
    ("vfy-prepare-policies.pdf", (fix / "policies.pdf").read_bytes()),
    ("vfy-prepare-policies.docx", (fix / "policies.docx").read_bytes())])
if status != 201:
    sys.exit("    upload answered {} {}".format(status, body))
ids = {d["name"]: d["id"] for d in body["documents"]}
status, body, _ = p.send("POST", "/prepare/jobs", token, body={"document_ids": list(ids.values())})
job = p.wait_job(token, "/prepare/job/" + body["job_id"])
if job["status"] != "completed":
    sys.exit("    job ended {}: {}".format(job["status"], job["errors"]))
for name, document_id in ids.items():
    record = p.send("GET", "/prepare/documents/" + document_id, token)[1]
    want = name.rsplit(".", 1)[0] + ".md"
    if record["status"] != "prepared" or [x["name"] for x in record["parts"]] != [want]:
        sys.exit("    {}: {} {}".format(name, record["status"], [x["name"] for x in record["parts"]]))
    status, content, _ = p.send("GET", "/prepare/documents/{}/prepared".format(document_id), token, raw=True)
    if status != 200 or "Overtime Approval" not in content.decode("utf-8"):
        sys.exit("    {}: prepared text {} {!r}".format(name, status, content[:200]))
ENDPY
  check "PDF and DOCX documents are extracted with ingest's parsers and prepared as Markdown (S4.3, S4.5)" $?
fi

stack cleanup
check "the suite's documents and collections are removed" $?

summary
