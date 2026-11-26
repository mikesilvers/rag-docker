#!/usr/bin/env bash
# External-Ollama mode (#673): SPECIFICATIONS.md §2.7 and §10.12.
#
# Every run runs test_external_ollama.py in the API image with no network. The
# external-mode checks need `stack.sh run --external-ollama`, on a host with a
# native Ollama set up as README "External Ollama (native)" says (its models
# pulled): no ollama service runs, the api container's check passes,
# /api/health/details reports the version and what the shared Ollama has
# loaded, ingest proves Weaviate reaches the host's Ollama through
# host-gateway, export with models is refused and one without them imports
# back, and a reindex is accepted. Outside RAG_SKIP_SLOW, hnsw and semantic
# questions return the fixture. Anywhere else they are skipped. Nothing here
# pulls, loads or unloads a model in the host's Ollama.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -d "$FIX" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack
C="${PREFIX}ExtOllama"
OUT="$(mktemp)"
trap 'rm -f "$OUT" /tmp/vfy_eo.json' EXIT

section "External-Ollama units: settings, requests, health, import, export, helper"
API_IMAGE="$(cd "$REPO_ROOT" && docker compose images -q api)"
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$API_IMAGE" python scripts/tests/test_external_ollama.py)
check "test_external_ollama.py registered regression checks" $?

if [ "${RAG_VERIFY_EXTERNAL_OLLAMA:-0}" != 1 ]; then
  skip "external-Ollama mode on a native Ollama: helper, health, ingest, export, import, reindex" \
    "run stack.sh run --external-ollama"
  summary
  exit $?
fi

section "The stack runs no Ollama of its own"
services=$(cd "$REPO_ROOT" && docker compose ps --services 2>/dev/null | tr '\n' ' ')
case " $services " in
  *" ollama "*) false ;;
  *) true ;;
esac
check "no ollama service runs in the project" $? "services: $services"
_rag_require_lock_owner
(cd "$REPO_ROOT" && docker compose exec -T api python -m ollama_check </dev/null) > "$OUT" 2>&1
check "the api container's check passes (python -m ollama_check)" $? "$(cat "$OUT")"
grep -q "^ollama-check: ok: reached Ollama " "$OUT"
check "... and it reached the host's Ollama through ollama:host-gateway" $? "$(head -3 "$OUT")"

section "Health in external mode"
check_eq "GET /api/health is 200" "$(api_code "$API/health")" "200"
api_get "/health/details" > /tmp/vfy_eo.json
python3 - /tmp/vfy_eo.json <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
version = d["services"]["ollama"].get("version")
r = d.get("resources") or {}
ok = (isinstance(version, str) and version
      and r.get("memory", {}).get("status") == "external"
      and "recommended_minimum_gb" not in r.get("memory", {})
      and isinstance(r.get("ollama_loaded"), list))
print(version)
sys.exit(0 if ok else 1)
PY
check "/health/details reports the Ollama version, memory 'external' with no advice, and what Ollama has loaded" $? \
  "$(head -c 400 /tmp/vfy_eo.json)"

section "Ingest: Weaviate embeds through the host's Ollama"
drop_collection "$C"; drop_collection "${C}Back"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_eo.json
job=$(jfield "['job_id']" < /tmp/vfy_eo.json)
check_eq "the fixture is ingested (Weaviate's text2vec-ollama embedded it)" "$(wait_for_job "/ingest/job/$job" 900)" "completed"
count=$(api_get "/collections" | python3 -c "
import json, sys
print([c['object_count'] for c in json.load(sys.stdin)['collections'] if c['name'] == sys.argv[1]][0])" "$C" 2>/dev/null)
[ -n "$count" ] && [ "$count" -gt 0 ]
check "the collection holds the fixture's chunks" $? "count '$count'"

section "Export and import"
code=$(curl -s -m 120 -o /tmp/vfy_eo.json -w '%{http_code}' -X POST "$API/export" \
  -H 'Content-Type: application/json' -d "{\"encryption\":\"none\",\"collection\":\"$C\",\"include_models\":true}")
check_eq "an export with models is refused (400 MODELS_NOT_EXPORTABLE)" \
  "$code $(jfield "['error']['code']" < /tmp/vfy_eo.json)" "400 MODELS_NOT_EXPORTABLE"
api_post "/export" "{\"encryption\":\"none\",\"collection\":\"$C\",\"include_models\":false}" > /tmp/vfy_eo.json
ejob=$(jfield "['job_id']" < /tmp/vfy_eo.json)
check_eq "an export without models completes" "$(wait_for_job "/export/job/$ejob" 1800)" "completed"
pkg=$(api_get "/export/job/$ejob" | jfield "['filename']")
api_post "/import" "{\"filename\":\"$pkg\",\"on_conflict\":\"rename\"}" > /tmp/vfy_eo.json
ijob=$(jfield "['job_id']" < /tmp/vfy_eo.json)
check_eq "the package imports back under a new name" "$(wait_for_job "/import/job/$ijob" 1800)" "completed"
api_get "/import/job/$ijob" > /tmp/vfy_eo.json
python3 -c "
import json, sys
notes = json.load(open('/tmp/vfy_eo.json')).get('notes') or []
sys.exit(0 if any('reported by the external Ollama' in n for n in notes) else 1)"
check "the import checked the embedding model through the external Ollama" $? "$(head -c 400 /tmp/vfy_eo.json)"
imported=$(jfield "['collection']" < /tmp/vfy_eo.json)

section "Reindex keeps the stored endpoint (http://ollama:11434)"
api_post "/tune/reindex" "{\"collection\":\"$C\"}" > /tmp/vfy_eo.json
rjob=$(jfield "['job_id']" < /tmp/vfy_eo.json)
check_eq "a reindex of the collection completes" "$([ -n "$rjob" ] && wait_for_job "/tune/job/$rjob" 1800 || echo "refused: $(head -c 200 /tmp/vfy_eo.json)")" "completed"

if [ "$SKIP_SLOW" = "1" ]; then
  skip "hnsw and semantic questions through the host's Ollama" "set RAG_SKIP_SLOW=0 to include model calls"
else
  section "Questions through the host's Ollama"
  for mode in hnsw semantic; do
    api_post "/query" "{\"question\":\"who approves overtime?\",\"collection\":\"$C\",\"retrieval_mode\":\"$mode\",\"top_k\":3,\"alpha\":0.5,\"include_citations\":true,\"response_format\":\"end_user\"}" > /tmp/vfy_eo.json
    python3 -c "
import json, sys
d = json.load(open('/tmp/vfy_eo.json'))
sys.exit(0 if d.get('answer', '').strip() and any(c.get('source_file') == 'policies.txt' for c in d.get('citations') or []) else 1)"
    check "a $mode question is answered and cites the fixture" $? "$(head -c 300 /tmp/vfy_eo.json)"
  done
fi

drop_collection "$C"
[ -z "${imported:-}" ] || drop_collection "$imported"
[ -z "${pkg:-}" ] || rm -f "${RAG_EXPORTS_DIR:?}/$pkg"
summary
exit $?
