#!/usr/bin/env bash
# SPECIFICATIONS.md §10.3 — Gold Standard
#
# Every check here corresponds to a defect that was live in the codebase:
# lost pairs, counters that reported attempts as successes, a 500 where a 409
# belonged, and a PATCH that silently rewrote approved content.
cd "$(dirname "$0")" && . ./lib.sh
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"
[ -d "$FIX" ] || python3 ./fixtures.py "$FIX" >/dev/null
require_stack
bash ./12_persistence.sh
check "durable session acceptance suite" $?
C="${PREFIX}Gold"

section "§10.3 Gold Standard"

# Selection needs the backend but no LLM work; include it even in slow-skip runs.
bash ./09_sampling.sh
check "UUID sampling acceptance" $?

if [ "$SKIP_SLOW" = "1" ]; then
  skip "LLM generation/review/export" "set RAG_SKIP_SLOW=0 to include them"
  summary; exit $?
fi

drop_collection "$C"; make_collection "$C"
curl -s -m 600 -X POST "$API/ingest/upload" -F "collection=$C" -F "strategy=fixed" \
  -F "chunk_size=150" -F "min_chunk_size=40" -F "files=@$FIX/policies.txt" > /tmp/vfy_g.json
job=$(python3 -c "import json;print(json.load(open('/tmp/vfy_g.json'))['job_id'])")
wait_for_job "/ingest/job/$job" 900 >/dev/null

SAMPLE="${RAG_GS_SAMPLE:-3}"
api_post "/goldstandard/generate" "{\"collection\":\"$C\",\"sample_size\":$SAMPLE}" > /tmp/vfy_gen.json
SID=$(python3 -c "import json;print(json.load(open('/tmp/vfy_gen.json'))['session_id'])")

# ── 409 while still generating ───────────────────────────────────────────────
# Regenerating mid-flight raced with the generation loop and returned 500.
overlapped=0
for _ in $(seq 1 60); do
  read -r st n <<<"$(api_get "/goldstandard/session/$SID" | python3 -c "
import json,sys; d=json.load(sys.stdin); print(d['status'], len(d['pairs']))")"
  if [ "$st" = generating ] && [ "$n" -ge 1 ]; then
    pid=$(api_get "/goldstandard/session/$SID" | jfield "['pairs'][0]['pair_id']")
    code=$(api_post_code "/goldstandard/regenerate" "{\"session_id\":\"$SID\",\"pair_id\":\"$pid\"}")
    check_eq "regenerate during generation returns 409" "$code" "409"
    overlapped=1; break
  fi
  [ "$st" != generating ] && break
  sleep 1
done
[ "$overlapped" = 1 ] || skip "409-while-generating" "generation finished before a regenerate could overlap"

wait_for_job "/goldstandard/session/$SID" 1800 >/dev/null
api_get "/goldstandard/session/$SID" > /tmp/vfy_sess.json

# ── every requested pair exists, and the counters agree ──────────────────────
read -r total attempted completed failed actual <<<"$(python3 -c "
import json; d=json.load(open('/tmp/vfy_sess.json'))
print(d['pairs_total'], d.get('pairs_attempted','?'), d['pairs_completed'],
      d.get('pairs_failed','?'), len(d['pairs']))")"
[ "$completed" = "$total" ] && [ "$actual" = "$total" ]
check "generate returns sample_size pairs" $? \
  "total=$total completed=$completed actual=$actual failed=$failed"
[ "$completed" = "$actual" ]
check "pairs_completed matches the pairs that exist" $? \
  "completed=$completed actual=$actual (this counted attempts before)"
[ "$attempted" = "$total" ]
check "pairs_attempted reaches the total so progress can finish" $? "attempted=$attempted/$total"

python3 -c "
import json,sys; d=json.load(open('/tmp/vfy_sess.json'))
need = ('question','answer','ground_truth','contexts')
sys.exit(0 if d['pairs'] and all(all(p.get(f) for f in need) for p in d['pairs']) else 1)"
check "every pair has question, answer, ground_truth and contexts" $?

# ── the PATCH audit rule ─────────────────────────────────────────────────────
PID=$(python3 -c "import json;print(json.load(open('/tmp/vfy_sess.json'))['pairs'][0]['pair_id'])")
code=$(curl -s -o /dev/null -m 120 -w '%{http_code}' -X PATCH \
  "$API/goldstandard/session/$SID/pair/$PID" -H 'Content-Type: application/json' \
  -d '{"question":"rewritten without declaring an edit"}')
check_eq "editing content without status='edited' is refused" "$code" "422"
code=$(curl -s -o /dev/null -m 120 -w '%{http_code}' -X PATCH \
  "$API/goldstandard/session/$SID/pair/$PID" -H 'Content-Type: application/json' \
  -d '{"status":"edited","question":"a properly declared edit"}')
check_eq "editing content with status='edited' is accepted" "$code" "200"
code=$(curl -s -o /dev/null -m 120 -w '%{http_code}' -X PATCH \
  "$API/goldstandard/session/$SID/pair/$PID" -H 'Content-Type: application/json' \
  -d '{"status":"approved"}')
check_eq "a status-only change is accepted" "$code" "200"

# ── regenerate replaces exactly one pair ─────────────────────────────────────
if [ "$actual" -ge 2 ]; then
  TARGET=$(python3 -c "import json;print(json.load(open('/tmp/vfy_sess.json'))['pairs'][1]['pair_id'])")
  api_post "/goldstandard/regenerate" "{\"session_id\":\"$SID\",\"pair_id\":\"$TARGET\"}" > /tmp/vfy_regen.json
  api_get "/goldstandard/session/$SID" > /tmp/vfy_after.json
  python3 - "$TARGET" <<'ENDPY'
import json, sys
target = sys.argv[1]
before = {p['pair_id']: p for p in json.load(open('/tmp/vfy_sess.json'))['pairs']}
after = {p['pair_id']: p for p in json.load(open('/tmp/vfy_after.json'))['pairs']}
changed = [k for k in before if k in after and before[k] != after[k]]
# The first pair was edited above, so it is expected to differ too.
unexpected = [k for k in changed if k != target and k != list(before)[0]]
sys.exit(0 if set(before) == set(after) and target in changed and not unexpected else 1)
ENDPY
  check "regenerate replaces only the targeted pair, ids stable" $?
else
  skip "regenerate-replaces-one" "needs at least two pairs"
fi

# ── export filtering, schema and filename ────────────────────────────────────
python3 - "$SID" "$API" <<'ENDPY'
import json, sys, urllib.request
sid, api = sys.argv[1], sys.argv[2]
pairs = json.load(urllib.request.urlopen(f"{api}/goldstandard/session/{sid}"))['pairs']
plan = ["approved", "edited", "rejected", "pending", "approved"]
for pair, status in zip(pairs, plan):
    body = {"status": status}
    if status == "edited":
        body["question"] = "edited for export"
    req = urllib.request.Request(
        f"{api}/goldstandard/session/{sid}/pair/{pair['pair_id']}",
        data=json.dumps(body).encode(), method="PATCH",
        headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req)
ENDPY
api_post "/goldstandard/save" "{\"session_id\":\"$SID\"}" > /tmp/vfy_save.json
SAVE_INFO=$(python3 - "$C" <<'ENDPY'
import json, re, sys
collection = sys.argv[1]
d = json.load(open('/tmp/vfy_save.json'))
kept = json.load(open('/tmp/vfy_sess.json'))['pairs']
expected = sum(1 for p, s in zip(kept, ["approved","edited","rejected","pending","approved"])
               if s in ("approved", "edited"))
print(d['pairs_saved'], d['pairs_excluded'], expected, d['filename'],
      1 if re.match(rf'^{collection}_\d{{8}}_\d{{6}}\.json$', d['filename']) else 0)
ENDPY
)
read -r saved excluded expected fname name_ok <<<"$SAVE_INFO"
[ "$saved" = "$expected" ]
check "export keeps only approved and edited pairs" $? "saved=$saved expected=$expected excluded=$excluded"
[ "$((saved + excluded))" = "$actual" ]
check "saved + excluded accounts for every pair" $? "$saved + $excluded vs $actual"
check_eq "default filename is {collection}_{YYYYMMDD_HHMMSS}.json" "$name_ok" "1"

curl -s -m 120 "$API/goldstandard/download/$fname" -o /tmp/vfy_export.json
python3 -c "
import json,sys
d = json.load(open('/tmp/vfy_export.json'))
RAGAS = {'question','answer','contexts','ground_truth'}
ok = isinstance(d, list) and d and all(
    RAGAS <= set(r) and isinstance(r['contexts'], list) and r['contexts']
    and all(isinstance(x,str) and x for x in r['contexts'])
    and all(isinstance(r[k],str) and r[k].strip() for k in ('question','answer','ground_truth'))
    for r in d)
sys.exit(0 if ok else 1)"
check "exported file is valid JSON in the RAGAS schema" $?

code=$(api_code "$API/goldstandard/download/definitely_not_here.json")
check_eq "download of an unknown filename returns 404" "$code" "404"

# ── sessions survive a restart ───────────────────────────────────────────────
# Never the live rag-docker project, whatever RAG_VERIFY_LIVE says (#152).
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then restart_refusal=$(restart_refusal_reason); fi
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ] && [ -n "$restart_refusal" ]; then
  check "sessions survive an API restart" 1 "$restart_refusal"
elif [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then
  (cd "$(git rev-parse --show-toplevel 2>/dev/null || echo ../..)" && docker compose restart api >/dev/null 2>&1)
  for _ in $(seq 1 60); do [ "$(api_code "$API/health")" = "200" ] && break; sleep 3; done
  api_get "/goldstandard/session/$SID" > /tmp/vfy_post.json
  python3 -c "
import json,sys
a=json.load(open('/tmp/vfy_after.json')); b=json.load(open('/tmp/vfy_post.json'))
sys.exit(0 if [p['pair_id'] for p in a['pairs']] == [p['pair_id'] for p in b['pairs']] else 1)"
  check "sessions survive an API restart" $?
else
  skip "session survives a restart" "set RAG_ALLOW_RESTART=1 to include it"
fi

drop_collection "$C"
cleanup_prefixed
summary
