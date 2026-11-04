#!/usr/bin/env bash
# Document preparation (#638): SPECIFICATIONS.md §3.1.11 and §10.8.
#
# The unit checks (U1-U13) run in the API image with no network and a stub LLM.
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
check "test_prepare.py: the preparation area, pipeline, jobs, import and routes (U1-U13)" $?

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
  check "P9 the .txt becomes cleaned Markdown of source lines in order with its header; the .csv and .jsonl are unchanged" $?
  stack import
  check "P10 an unknown collection is 404; the import into a new collection completes and is recorded" $?
  stack prepared-download
  check "P11 the prepared download is the part; deleting the prepared output returns the document to raw" $?
fi

stack cleanup
check "the suite's documents and collections are removed" $?

summary
