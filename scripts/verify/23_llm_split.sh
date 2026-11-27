#!/usr/bin/env bash
# Build and answer models (#671): preparation and gold standard generation use
# the build model, query uses the answer model.
#
# Slow only (it pulls a small model and makes real LLM calls), so a run with
# RAG_SKIP_SLOW=1 skips it; every other check stays on one model. It runs
# llm_split.py in the api container with BUILD_LLM_MODEL set to a small model
# for that process only, leaving the answer model as compose sets it: the API
# itself and the compose settings are not changed. Its collection is
# VfyLlmSplit, removed at the end; it creates no preparation document and no
# gold standard session. Its model pull is retried on a 5xx; a pull that fails
# anyway is one failed check, with the model checks skipped (#922).
# ../tests/test_llm_split.py checks both on the host, in every run but an
# external-Ollama one.
#
# External-Ollama mode (#673) skips it: the Ollama there is the host's own,
# shared with other apps, and rag-docker never pulls or deletes a model in it.
cd "$(dirname "$0")" && . ./lib.sh
if [ "${RAG_VERIFY_EXTERNAL_OLLAMA:-0}" = 1 ]; then
  section "Build and answer models (#671)"
  skip "preparation and gold standard on the build model, query on the answer model" \
    "external-Ollama mode: this suite pulls and deletes qwen2.5:0.5b, and an external Ollama is shared, so rag-docker never writes models to it (#673)"
  summary; exit $?
fi
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -d "$FIX" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack
SMALL=qwen2.5:0.5b
C="${PREFIX}LlmSplit"
OUT="$(mktemp)"
trap 'rm -f "$OUT" /tmp/vfy_llm_split_ingest.json' EXIT

section "Build and answer models (#671)"

python3 "$REPO_ROOT/scripts/tests/test_llm_split.py"
check "llm_split.py retries its model pull on a 5xx and reports a failed pull as one setup error (#922)" $?

if [ "$SKIP_SLOW" = "1" ]; then
  skip "preparation and gold standard on the build model, query on the answer model" \
    "set RAG_SKIP_SLOW=0 to include it"
  summary; exit $?
fi

drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_llm_split_ingest.json
job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_llm_split_ingest.json'))['job_id'])" 2>/dev/null)
check_eq "the collection for the query is ingested" "$(wait_for_job "/ingest/job/$job" 900)" "completed"

(cd "$REPO_ROOT" && docker compose exec -T -e BUILD_LLM_MODEL="$SMALL" -e RAG_LLM_SPLIT_COLLECTION="$C" \
  api python - < scripts/verify/llm_split.py) > "$OUT"
ran=$?

f() { python3 -c "import json,sys;print(json.loads(open(sys.argv[1]).read().strip().splitlines()[-1]).get(sys.argv[2]))" "$OUT" "$1" 2>/dev/null; }
# A pull that failed is one setup error, not a failure of every check below (#922).
setup_error="$(f setup_error)"
if [ -n "$setup_error" ] && [ "$setup_error" != "None" ]; then
  check "the build model is pulled into the verify project's Ollama" 1 "$setup_error"
  skip "the build and answer model checks" "they need the build model pulled"
  drop_collection "$C"
  summary; exit $?
fi
check "llm_split.py ran to the end" "$ran" "$(tail -c 400 "$OUT")"
check_eq "with BUILD_LLM_MODEL set, the API resolves it as the build model" "$(f build_model)" "$SMALL"
answer="$(f answer_model)"
[ -n "$answer" ] && [ "$answer" != "None" ] && [ "$answer" != "$SMALL" ]
check "the answer model stays as compose sets it" $? "got '$answer'"
# Ollama names a model `name:tag`, so compare with the tagged names. A task that
# raised still counts the calls it made; its error is shown with the check.
build_tagged="$(f build_tagged)"; answer_tagged="$(f answer_tagged)"
models_check() {   # models_check <name> <task> <want>
  local got; got="$(f "$2")"
  [ "$got" = "['$3']" ]
  check "$1" $? "got '$got', wanted '['$3']'; error: $(f "$2_error")"
}
models_check "preparation's LLM calls were answered by the build model only" prepare "$build_tagged"
models_check "gold standard generation's LLM calls were answered by the build model only" goldstandard "$build_tagged"
models_check "query's LLM calls were answered by the answer model only" query "$answer_tagged"

drop_collection "$C"
summary
