#!/usr/bin/env bash
# Query-only mode (#670): SPECIFICATIONS.md §2.6 and §10.11.
#
# Every run checks the real docker-compose.query-only.yml as `docker compose
# config` resolves it (nothing is started), runs test_query_only.py in the API
# image with no network, checks that the API image refuses a non-boolean
# RAG_QUERY_ONLY at start-up, and that the loader refuses on the running
# default instance. The two-instance checks need `stack.sh run --query-only`:
# a collection is built and exported on the default instance, then
# `stack.sh query-only-mode` starts the same project again in query-only mode
# with empty volumes and only that package in its exports folder, as another
# machine would have it, and the package is loaded and queried there. Anywhere
# else they are skipped. Nothing here needs cleaning up afterwards: a
# --query-only run ends with stack.sh removing the project.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -d "$FIX" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack
C="${PREFIX}QueryOnly"
OUT="$(mktemp)"
trap 'rm -f "$OUT" /tmp/vfy_qo.json' EXIT

# loader [docker compose exec options...] -- [loader arguments...]: runs the
# loader in the api container, its lines in $OUT, and returns its exit status.
loader() {
  local opts=()
  while [ "$#" -gt 0 ] && [ "$1" != -- ]; do opts+=("$1"); shift; done
  shift
  _rag_require_lock_owner
  (cd "$REPO_ROOT" && docker compose exec -T ${opts[@]+"${opts[@]}"} api python -m loader "$@") \
    </dev/null >"$OUT" 2>&1
}

# The newest operator.load record's result and code, as "result code".
last_load() {
  api_get "/audit?action=operator.load&limit=1" | python3 -c "
import json, sys
r = json.load(sys.stdin)['records'][0]
print(r['result'], (r.get('details') or {}).get('code') or '-', r.get('target'))" 2>/dev/null
}

section "Query-only overlay, resolved without starting it"
python3 ./query_only_config.py "$REPO_ROOT"
check "docker-compose.query-only.yml changes only the api's RAG_QUERY_ONLY, from false to true" $?

section "Query-only units: setting, policy, refusal, flag, loader"
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \
  "$(docker compose images -q api)" python scripts/tests/test_query_only.py)
check "test_query_only.py registered regression checks" $?

section "Query-only refusal in the built API image"
_rag_require_lock_owner
(cd "$REPO_ROOT" && docker compose run --rm --no-deps -T -e RAG_QUERY_ONLY=yes api </dev/null 2>&1) > "$OUT"
hits=$(grep -c "Settings refused: RAG_QUERY_ONLY must be exactly true or false" "$OUT")
! grep -q '^Traceback' "$OUT" && [ "$hits" = 1 ]
check "the API image refuses RAG_QUERY_ONLY=yes at start-up with one line and no traceback" $? "$(tail -3 "$OUT")"

section "The loader on a full instance"
loader -- ragpkg-vfy-not-there.tar.gz
rc=$?
[ "$rc" = 1 ] && grep -q "NOT_QUERY_ONLY" "$OUT"
check "the loader refuses on an instance that isn't query-only (exit 1, NOT_QUERY_ONLY)" $? "exit $rc: $(tail -2 "$OUT")"
check_eq "the refusal is an operator.load audit record" "$(last_load)" "refused NOT_QUERY_ONLY ragpkg-vfy-not-there.tar.gz"
loader -- ../etc/passwd
check_eq "a path instead of a package name is a usage error (exit 2)" "$?" "2"

if [ "${RAG_VERIFY_QUERY_ONLY:-0}" != 1 ]; then
  skip "query-only mode on a second instance: build, export, load, refusals, Q&A" "run stack.sh run --query-only"
  summary
  exit $?
fi

# ── the build instance ───────────────────────────────────────────────────────
section "Build and export on the default instance"
drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_qo.json
job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_qo.json'))['job_id'])" 2>/dev/null)
check_eq "the collection is ingested" "$(wait_for_job "/ingest/job/$job" 900)" "completed"
api_post "/export" "{\"encryption\":\"none\",\"collection\":\"$C\",\"include_models\":false}" > /tmp/vfy_qo.json
ejob=$(jfield "['job_id']" < /tmp/vfy_qo.json)
check_eq "the collection is exported" "$(wait_for_job "/export/job/$ejob" 1800)" "completed"
pkg=$(api_get "/export/job/$ejob" | jfield "['filename']")
chunks=$(api_get "/packages" | python3 -c "
import json, sys
print([p['chunk_count'] for p in json.load(sys.stdin)['packages'] if p['filename'] == sys.argv[1]][0])" "$pkg" 2>/dev/null)
[ -n "$pkg" ] && [ -f "${RAG_EXPORTS_DIR:?}/$pkg" ] && [ -n "$chunks" ]
check "the package is in the exports folder, with its chunk count" $? "package '$pkg', chunks '$chunks'"
if [ "$FAIL" -gt 0 ]; then
  summary
  exit $?
fi

# ── the query-only instance ──────────────────────────────────────────────────
section "Switch to a query-only instance with its own volumes"
_rag_require_lock_owner
bash "$RAG_VERIFY_HARNESS/scripts/verify/stack.sh" query-only-mode --package "$pkg" \
  --checkout "$RAG_VERIFY_CHECKOUT"
check "stack.sh query-only-mode started the project again in query-only mode with only the package" $?
if [ "$FAIL" -gt 0 ]; then
  summary
  exit $?
fi
# The same project, now with the overlay: docker compose here resolves it so.
export COMPOSE_FILE="$RAG_VERIFY_CHECKOUT/docker-compose.yml:$RAG_VERIFY_CHECKOUT/docker-compose.query-only.yml:$RAG_VERIFY_HARNESS/docker-compose.verify.yml"
# A new, empty account store: the test accounts are made again.
auth_ensure
names=$(ls "${RAG_EXPORTS_DIR:?}")
check_eq "the exports folder holds only the copied package" "$names" "$pkg"
code=$(api_get "/collections" | python3 -c "import json,sys; print(len(json.load(sys.stdin)['collections']))" 2>/dev/null)
check_eq "the query-only instance starts with no collection" "$code" "0"
check_eq "GET /auth/me says query_only" "$(api_get /auth/me | jfield "['query_only']")" "True"

section "Load the package"
loader -e EMBED_MODEL=vfy-other-embed -- "$pkg"
rc=$?
[ "$rc" = 1 ] && grep -q "EMBEDDING_MISMATCH" "$OUT"
check "a package from another embedding model is refused (exit 1, EMBEDDING_MISMATCH)" $? "exit $rc: $(tail -2 "$OUT")"
exists=$(api_get "/collections" | python3 -c "import json,sys; print(any(c['name']=='$C' for c in json.load(sys.stdin)['collections']))" 2>/dev/null)
check_eq "the refused load created no collection" "$exists" "False"
loader -- "$pkg"
rc=$?
check_eq "the loader loads the package (exit 0)" "$rc" "0"
grep -q "loaded into collection $C: $chunks chunks" "$OUT"
check "the loader reports the collection and its $chunks chunks" $? "$(tail -3 "$OUT")"
count=$(api_get "/collections" | python3 -c "
import json, sys
print([c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name'] == sys.argv[1]][0])" "$C" 2>/dev/null)
check_eq "the loaded collection holds the exported chunks" "$count" "$chunks"
check_eq "the load is an operator.load audit record" "$(last_load)" "success - $pkg"
loader -- "$pkg"
rc=$?
[ "$rc" = 1 ] && grep -q "COLLECTION_EXISTS" "$OUT"
check "loading it again with the default abort is refused (exit 1, COLLECTION_EXISTS)" $? "exit $rc: $(tail -2 "$OUT")"

section "Who sees the loaded collection"
user=$(auth_jar end_user) || user=""
seen() { command curl -s -m 60 -b "$1" "$API/collections" | python3 -c "import json,sys; print(any(c['name']=='$C' for c in json.load(sys.stdin)['collections']))" 2>/dev/null; }
check_eq "the End User doesn't see it: a loaded collection starts restricted with no grants" "$(seen "$user")" "False"
check_eq "the Engineer sees it" "$(seen "$RAG_AUTH_JAR")" "True"

section "Refused and kept routes"
refused() {   # refused <method> <path>: the Engineer's answer, as "status code"
  curl -s -m 60 -o /tmp/vfy_qo.json -w '%{http_code}' -X "$1" "$API$2" -H 'Content-Type: application/json' -d '{}' \
    >/tmp/vfy_qo.code
  printf '%s %s' "$(cat /tmp/vfy_qo.code)" "$(jfield "['error']['code']" < /tmp/vfy_qo.json)"
}
bad=""
for route in "POST /collections" "DELETE /collections/$C" "POST /ingest/upload" "POST /ingest/config" \
             "POST /retrieval/config" "POST /goldstandard/generate" "POST /goldstandard/regenerate" \
             "PATCH /goldstandard/session/vfy-s/pair/vfy-p" "POST /goldstandard/save" "POST /import" \
             "POST /prepare/upload" "DELETE /prepare/documents/vfy-d" "DELETE /prepare/documents/vfy-d/prepared" \
             "POST /prepare/jobs" "POST /prepare/job/vfy-j/cancel" "POST /prepare/import" \
             "POST /tune/rechunk" "POST /tune/reembed" "POST /tune/reindex"; do
  got=$(refused ${route% *} "${route#* }")
  [ "$got" = "403 QUERY_ONLY" ] || bad="$bad; $route: $got"
done
[ -z "$bad" ]
check "each of the 19 data-changing routes answers the Engineer 403 QUERY_ONLY" $? "${bad#; }"
dev=$(auth_jar developer) || dev=""
got=$(command curl -s -m 60 -b "$dev" -H "Origin: $RAG_AUTH_ORIGIN" -X DELETE "$API/collections/$C?confirm=true" | jfield "['error']['code']")
check_eq "a Developer deleting a collection is still refused FORBIDDEN" "$got" "FORBIDDEN"
check_eq "no session is still 401" "$(command curl -s -o /dev/null -m 60 -w '%{http_code}' -X POST "$API/import" -H 'Content-Type: application/json' -d '{}')" "401"
check_eq "the Engineer opens the loaded collection (PUT access is kept)" \
  "$(curl -s -o /dev/null -m 60 -w '%{http_code}' -X PUT "$API/collections/$C/access" -H 'Content-Type: application/json' -d '{"restricted":false}')" "200"
check_eq "the End User then sees it" "$(seen "$user")" "True"

if [ "$SKIP_SLOW" = "1" ]; then
  skip "an End User's question on the loaded collection" "set RAG_SKIP_SLOW=0 to include it"
else
  command curl -s -m 600 -b "$user" -H "Origin: $RAG_AUTH_ORIGIN" -X POST "$API/query" -H 'Content-Type: application/json' \
    -d "{\"question\":\"who approves overtime?\",\"collection\":\"$C\",\"top_k\":3,\"include_citations\":true}" > /tmp/vfy_qo.json
  python3 -c "
import json, sys
r = json.load(open('/tmp/vfy_qo.json'))
sys.exit(0 if r.get('answer', '').strip() and r.get('citations') else 1)"
  check "an End User asks a question on the loaded collection and gets an answer with citations" $? "$(head -c 300 /tmp/vfy_qo.json)"
fi

# The default export, encrypted to a new key (#676).
api_post "/export" "{\"collection\":\"$C\",\"include_models\":false}" > /tmp/vfy_qo.json
ejob=$(jfield "['job_id']" < /tmp/vfy_qo.json)
check_eq "export keeps working in query-only mode" "$(wait_for_job "/export/job/$ejob" 1800)" "completed"
check_eq "the package list keeps working" "$(api_code "$API/packages")" "200"

section "Load a key package with its key file (#676)"
kpkg=$(api_get "/export/job/$ejob" | jfield "['filename']")
kkey=$(api_get "/export/job/$ejob" | jfield "['key_filename']")
check_eq "the default export is encrypted with a new key" "$(api_get "/export/job/$ejob" | jfield "['encryption']")" "key"
loader -- "$kpkg"
rc=$?
[ "$rc" = 1 ] && grep -q "KEY_FILE_REQUIRED" "$OUT"
check "the loader refuses a key package with no key on standard input (exit 1, KEY_FILE_REQUIRED)" $? "exit $rc: $(tail -2 "$OUT")"
check_eq "the refusal is an operator.load audit record" "$(last_load)" "refused KEY_FILE_REQUIRED $kpkg"
check_eq "the key file downloads" "$(curl -s -o /tmp/vfy_qo_key.txt -m 120 -w '%{http_code}' "$API/packages/$kkey")" "200"
_rag_require_lock_owner
(cd "$REPO_ROOT" && docker compose exec -T api python -m loader "$kpkg" --on-conflict rename) \
  </tmp/vfy_qo_key.txt >"$OUT" 2>&1
rc=$?
check_eq "the loader loads the key package with its key file on standard input (exit 0)" "$rc" "0"
grep -q "AGE-SECRET-KEY" "$OUT"
[ "$?" = 1 ]
check "the loader never prints the key" $?
rm -f /tmp/vfy_qo_key.txt
curl -s -o /dev/null -m 600 "$API/packages/$kpkg"

summary
