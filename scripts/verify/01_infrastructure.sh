#!/usr/bin/env bash
# SPECIFICATIONS.md §10.5 — Infrastructure
#
# The restart and first-run timing checks are disruptive, so they are opt-in.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
require_stack
C="${PREFIX}Infra"

section "§10.5 Infrastructure"
RAG_INFRA_TMP=$(mktemp -d "${TMPDIR:-/tmp}/rag-infra.XXXXXX") || exit 2
export RAG_INFRA_TMP
# Also release the verify lock: this trap replaces the one lock.sh set.
trap 'rm -rf "$RAG_INFRA_TMP"; _rag_lock_release 2>/dev/null || true' EXIT
EXPECTED_PORT="${RAG_EXPECTED_PROXY_PORT:-8080}"
export EXPECTED_PORT
engine=$(docker version --format '{{.Server.Version}}')
python3 - "$engine" <<'ENDPY'
import re, sys
version = sys.argv[1]
match = re.match(r'^(\d+)\.(\d+)\.(\d+)', version)
ok = bool(match and tuple(map(int, match.groups())) >= (28, 0, 0)
          and not (version.startswith('28.0.0-') and any(x in version for x in ('alpha', 'beta', 'rc'))))
sys.exit(0 if ok else 1)
ENDPY
check "Docker Engine is 28.0.0 or newer for localhost port isolation" $? "$engine"

# ── resolved configuration and live bindings ─────────────────────────────────
# Inspect structured ports, including their host addresses. Matching only
# 0.0.0.0 made a loopback deployment look as though it published no ports.
(cd "$REPO_ROOT" && docker compose config --format json) > "$RAG_INFRA_TMP/vfy_compose.json"
python3 - <<'ENDPY'
import json, sys, os
services = json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_compose.json'))['services']
ports = [(name, port) for name, service in services.items() for port in service.get('ports', [])]
ok = (len(ports) == 1 and ports[0][0] == 'proxy'
      and ports[0][1].get('host_ip') == '127.0.0.1'
      and str(ports[0][1]['published']) == os.environ['EXPECTED_PORT']
      and ports[0][1]['target'] == 80 and ports[0][1].get('protocol', 'tcp') == 'tcp')
sys.exit(0 if ok else 1)
ENDPY
check "resolved Compose publishes only the proxy on host loopback" $?

(cd "$REPO_ROOT" && python3 - <<'ENDPY'
import json, subprocess
ids = subprocess.check_output(['docker', 'compose', 'ps', '-q'], text=True).split()
containers = json.loads(subprocess.check_output(['docker', 'inspect', *ids], text=True)) if ids else []
bindings = [{'service': container['Config']['Labels']['com.docker.compose.service'],
             'container_port': port, **binding}
            for container in containers
            for port, published in container['NetworkSettings']['Ports'].items()
            for binding in (published or [])]
print(json.dumps(bindings))
ENDPY
) > "$RAG_INFRA_TMP/vfy_bindings.json"
count=$(python3 -c "import json, os; print(len(json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_bindings.json'))))")
check_eq "only one port is published to the host" "$count" "1"
python3 - <<'ENDPY'
import json, sys, os
bindings = json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_bindings.json'))
ok = (len(bindings) == 1 and bindings[0]['service'] == 'proxy'
      and bindings[0]['container_port'] == '80/tcp' and bindings[0]['HostIp'] == '127.0.0.1'
      and bindings[0]['HostPort'] == os.environ['EXPECTED_PORT'])
sys.exit(0 if ok else 1)
ENDPY
check "the live proxy port is bound only to host loopback" $?

for svc in api weaviate; do
  published=$( (cd "$REPO_ROOT" && docker compose ps --format "{{.Service}}|{{.Ports}}") \
    | grep "^$svc|" | grep -c -- '->[0-9]*/tcp' || true)
  check_eq "$svc publishes nothing to the host" "$published" "0"
done

# ── five services, all reporting healthy where a healthcheck exists ──────────
running=$( (cd "$REPO_ROOT" && docker compose ps --services --filter status=running) | grep -c .)
check_eq "five services are running" "$running" "5"
unhealthy=$( (cd "$REPO_ROOT" && docker compose ps --format '{{.Status}}') | grep -c 'unhealthy' || true)
check_eq "no service reports unhealthy" "$unhealthy" "0"

# ── health endpoint reports each dependency ──────────────────────────────────
api_get "/health" > "$RAG_INFRA_TMP/vfy_health.json"
check_eq "health status is ok" "$(jfield "['status']" < "$RAG_INFRA_TMP/vfy_health.json")" "ok"
python3 -c "
import json,sys,os; d=json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_health.json'))
s=d['services']
ok = (s['weaviate']['status']=='ok' and s['ollama']['llm']['status']=='ok'
      and s['ollama']['embed']['status']=='ok'
      and all(v['latency_ms'] >= 0 for v in (s['weaviate'], s['ollama']['llm'], s['ollama']['embed'])))
sys.exit(0 if ok else 1)"
check "per-service status and latency are reported" $?

# ── memory resource reporting (Issue #98) ────────────────────────────────────
# Advisory only (see the comment in api/routers/health.py), so it never
# affects overall_ok, but the report itself must be right: the configured
# recommendation, and a flip to below_recommended -- with a note -- once the
# recommendation exceeds what's actually allocated.
check_eq "the recommended minimum defaults to 12.0 GB" \
  "$(jfield "['resources']['memory']['recommended_minimum_gb']" < "$RAG_INFRA_TMP/vfy_health.json")" "12.0"
# Whether this machine meets it depends on its Docker allocation, so check the
# status agrees with the allocation /health reports, not that it is 'ok'.
python3 -c "
import json, os
m = json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_health.json'))['resources']['memory']
want = 'ok' if m['allocated_gb'] >= m['recommended_minimum_gb'] * 0.95 else 'below_recommended'
raise SystemExit(0 if m['status'] == want else 1)"
check "the memory status agrees with the reported allocation" $?

# Raise the recommendation past the actual allocation (in-process only --
# neither the running server nor docker-compose.yml is touched) and confirm
# the status flips and a note is attached.
(cd "$REPO_ROOT" && docker compose exec -T api env RECOMMENDED_MEMORY_GB=13 python3 -c "
import json
from config import Settings
from services import system_info
print(json.dumps(system_info.memory_info(Settings().recommended_memory_gb)))
") > "$RAG_INFRA_TMP/vfy_mem_override.json"
check_eq "a recommendation above the allocation reports below_recommended" \
  "$(jfield "['status']" < "$RAG_INFRA_TMP/vfy_mem_override.json")" "below_recommended"
python3 -c "
import json, os
d = json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_mem_override.json'))
raise SystemExit(0 if d.get('note') and 'recommended' in d['note'] else 1)"
check "the below-recommended report includes an explanatory note" $?

# ── ingest config defaults ───────────────────────────────────────────────────
drop_collection "$C"; make_collection "$C"
check_eq "a fresh collection reports is_default true" \
  "$(api_get "/ingest/config/$C" | jfield "['is_default']")" "True"
api_post "/ingest/config" "{\"collection\":\"$C\",\"chunking_strategy\":\"semantic\",\"chunk_size\":800,\"chunk_overlap\":150,\"similarity_threshold\":0.9,\"min_chunk_size\":80}" >/dev/null
api_get "/ingest/config/$C" > "$RAG_INFRA_TMP/vfy_cfg.json"
check_eq "after saving, is_default is false" "$(jfield "['is_default']" < "$RAG_INFRA_TMP/vfy_cfg.json")" "False"
check_eq "the saved strategy is returned" "$(jfield "['chunking_strategy']" < "$RAG_INFRA_TMP/vfy_cfg.json")" "semantic"

# ── deleting a collection must take its configs with it ──────────────────────
# Spec §8 rule 1. This was not happening for the ingest config, so a recreated
# collection silently inherited chunking settings the user never chose.
drop_collection "$C"
make_collection "$C"
api_post "/ingest/config" "{\"collection\":\"$C\",\"chunking_strategy\":\"semantic\",\"chunk_size\":900,\"chunk_overlap\":100,\"similarity_threshold\":0.9,\"min_chunk_size\":70}" >/dev/null
api_post "/retrieval/config" "{\"collection\":\"$C\",\"retrieval_mode\":\"hybrid\",\"top_k\":9,\"alpha\":0.4,\"ef\":null,\"response_format\":\"engineer\"}" >/dev/null
drop_collection "$C"
make_collection "$C"
check_eq "a recreated collection does not inherit the ingest config" \
  "$(api_get "/ingest/config/$C" | jfield "['is_default']")" "True"
check_eq "a recreated collection does not inherit the retrieval config" \
  "$(api_get "/retrieval/config/$C" | jfield "['is_default']")" "True"

# ── persistence across a restart (opt-in: it stops the stack) ────────────────
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then
  api_get "/collections" > "$RAG_INFRA_TMP/vfy_before.json"
  started=$(python3 -c "import time;print(time.time())")
  (cd "$REPO_ROOT" && docker compose down >/dev/null 2>&1 && docker compose up -d >/dev/null 2>&1)
  for _ in $(seq 1 120); do [ "$(api_code "$API/health")" = "200" ] && break; sleep 2; done
  elapsed=$(python3 -c "import time;print(int(time.time()-$started))")
  [ "$elapsed" -le 120 ]
  check "restart reaches healthy within 120s" $? "took ${elapsed}s"
  api_get "/collections" > "$RAG_INFRA_TMP/vfy_after.json"
  python3 -c "
import json,sys,os
b={c['name']:c for c in json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_before.json'))['collections']}
a={c['name']:c for c in json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_after.json'))['collections']}
ok = set(b)==set(a) and all(b[k]['object_count']==a[k]['object_count'] for k in b)
sys.exit(0 if ok else 1)"
  check "Weaviate data survives down/up" $?
  python3 -c "
import json,sys,os
b={c['name']:c for c in json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_before.json'))['collections']}
a={c['name']:c for c in json.load(open(os.environ['RAG_INFRA_TMP'] + '/vfy_after.json'))['collections']}
sys.exit(0 if all(b[k]['created_at']==a[k]['created_at'] for k in b if k in a) else 1)"
  check "created_at is preserved across restart" $?
  check_eq "saved ingest config survives restart" \
    "$(api_get "/ingest/config/$C" | jfield "['chunking_strategy']")" "semantic"
else
  skip "restart, persistence and timing" "set RAG_ALLOW_RESTART=1 to include them"
fi

# ── startup sweeps leave a clean instance alone ──────────────────────────────
leftover=$( (cd "$REPO_ROOT" && docker compose exec -T api sh -c \
  'ls -d /app/uploads/import-* /app/uploads/rechunk-* 2>/dev/null | wc -l') | tr -d ' ')
check_eq "no abandoned extraction directories" "${leftover:-0}" "0"
staging=$(api_get "/collections" | python3 -c "
import json,sys
print(sum(1 for c in json.load(sys.stdin)['collections']
          if '__importing_' in c['name'] or '__tuning_' in c['name']))")
check_eq "no abandoned staging collections" "$staging" "0"

drop_collection "$C"
cleanup_prefixed
summary
