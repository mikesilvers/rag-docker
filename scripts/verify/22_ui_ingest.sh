#!/usr/bin/env bash
# Ingest through the web UI, end to end (#641): SPECIFICATIONS.md §10.9.
#
# A real browser (browser/ui_ingest.js), logged in as the harness Engineer, with
# nothing stubbed, ingests on the Import page every supported type into a new
# collection each, a ZIP of mixed types with a .jsonl.gz, and a file over the
# part limit; prepares two files on the Prepare page with the local LLM and
# imports them into a new collection (full runs only); and sends a broken PDF,
# an unsupported type and an over-limit ZIP, each into a new collection. Then
# ui_ingest_check.py checks each collection: the job ended with no error and the
# page showed none, the Collections page's count, and the document names and a
# known phrase per document read from the verify project's Weaviate
# (ui_ingest_weaviate.py, in the api container); the refusals: a clear message,
# nothing in the collection and no job left running. The cases and phrases are
# the fixtures' ui_ingest/manifest.json (fixtures.py). Its collections are
# VfyUiIngest*, its prepared documents vfy-ui-*; both are removed at the end.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -f "$FIX/ui_ingest/manifest.json" ] || python3 ./fixtures.py "$FIX" >/dev/null
MANIFEST="$FIX/ui_ingest/manifest.json"
require_stack

WORK=$(mktemp -d "${TMPDIR:-/tmp}/rag-verify-ui-ingest.XXXXXX")
# Also release the verify lock: this trap replaces the one lock.sh set.
trap 'rm -rf "$WORK"; _rag_lock_release 2>/dev/null || true' EXIT
RESULTS="$WORK/results.json"
DUMP="$WORK/weaviate.json"

IMAGE="rag-verify-browser:latest"
# The browser runs in the proxy container's network namespace and opens
# http://localhost, as in 06_ui.sh (#664).
PROXY=$( (cd "$REPO_ROOT" && docker compose ps -q proxy) )
if [ -z "$PROXY" ]; then
  printf '    could not find the proxy container\n'
  exit 2
fi
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  printf '    building the browser image (first run only)...\n'
  docker build -q -t "$IMAGE" ./browser >/dev/null || { printf '    image build failed\n'; exit 2; }
fi

uicheck() { _rag_require_lock_owner; python3 ./ui_ingest_check.py "$@"; }
uicheck cleanup >/dev/null 2>&1

section "The flows in the browser (Import and Prepare pages, no stubs)"
# The credentials go in on standard input (#25), as in 06_ui.sh.
_rag_require_lock_owner
started=$(date +%s)
docker run --rm -i --network "container:$PROXY" \
  -e RAG_UI_BASE="${RAG_UI_BASE:-http://localhost}" \
  -e RAG_SKIP_SLOW="$SKIP_SLOW" -e RAG_AUTH_STDIN=1 \
  -v "$PWD/browser":/w:ro -v "$FIX/ui_ingest":/fixtures:ro -w /w "$IMAGE" \
  node ui_ingest.js < "$RAG_AUTH_DIR/credentials" > "$WORK/browser.out"
rc=$?
grep -v '^UI_INGEST_RESULT ' "$WORK/browser.out"
grep '^UI_INGEST_RESULT ' "$WORK/browser.out" | tail -n 1 | sed 's/^UI_INGEST_RESULT //' > "$RESULTS"
printf '    the browser took %ds\n' "$(( $(date +%s) - started ))"
[ "$rc" = 0 ] && [ -s "$RESULTS" ] && uicheck browser "$MANIFEST" "$RESULTS"
check "the browser ran every case and read the Collections page" $?

(cd "$REPO_ROOT" && docker compose exec -T api python - < scripts/verify/ui_ingest_weaviate.py > "$DUMP")
check "the suite's collections were read from the verify project's Weaviate" $?

while IFS=$'\t' read -r kind slow name; do
  section "$name"
  if [ "$slow" = slow ] && [ "$SKIP_SLOW" = "1" ]; then
    skip "$name: prepare, import and check the collection" "RAG_SKIP_SLOW=1"
    continue
  fi
  run() { uicheck "$1" "$name" "$MANIFEST" "$RESULTS" "$DUMP"; }
  if [ "$kind" = refused ]; then
    run message
    check "$name: the page shows a clear message naming the file" $?
    run nothing-left
    check "$name: nothing is left in the collection and no job is left running" $?
    continue
  fi
  if [ "$kind" = prepare ]; then
    run prepared
    check "$name: the preparation completed with no error shown, every document prepared" $?
  fi
  run job
  check "$name: the job completed with no file error, and the page showed none" $?
  run contents
  check "$name: the Collections page shows at least the expected chunks" $?
  run names
  check "$name: Weaviate holds exactly the expected documents" $?
  run retrieval
  check "$name: each document's known phrase is in one of its chunks" $?
  if [ "$kind" = split ]; then
    run split
    check "$name: the page and the job show it split, Weaviate holds parts 1 to N" $?
  fi
done < <(python3 ./ui_ingest_check.py cases "$MANIFEST")

section "Cleanup"
uicheck cleanup
uicheck cleanup-check
check "the suite's collections and prepared documents are removed" $?

summary
