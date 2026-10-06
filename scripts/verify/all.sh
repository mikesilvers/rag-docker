#!/usr/bin/env bash
#
# Run every verification suite against a running stack.
#
#   bash scripts/verify/all.sh              # everything (~20 min, LLM-bound)
#   RAG_SKIP_SLOW=1 bash scripts/verify/all.sh   # skip LLM work (~8 min)
#   RAG_ALLOW_RESTART=1 bash scripts/verify/all.sh  # also restart the stack
#   bash scripts/verify/all.sh 02 04        # only the named suites
#
# Normally started by scripts/verify/stack.sh run, on the disposable verify
# project. It refuses the live rag-docker stack (#152).
#
# Exits non-zero if any check fails, so it can gate a commit or a release.
#
# These are integration tests: they need the stack up, because the defects this
# project actually produced -- a missing parser dependency, a silent 200 where a
# 422 belonged, vectors that survive a vectorizer -- are all invisible to unit
# tests of the same code.
set -uo pipefail
cd "$(dirname "$0")"
REPO_ROOT="$(cd ../.. && pwd)"
# One run at a time: the fixtures rebuilt below are shared (#95). lib.sh takes
# the lock (lock.sh) and holds the live-stack guard (#152).
. ./lib.sh
live_guard

API="${RAG_API:-http://localhost:8080/api}"
FIX="${RAG_FIXTURES:-/tmp/rag-verify-fixtures}"

code=$(curl -s -o /dev/null -m 10 -w '%{http_code}' "$API/health" 2>/dev/null)
if [ "$code" != "200" ]; then
  printf '\nNo healthy API at %s (HTTP %s).\n' "$API" "$code"
  printf 'Start the verify project first:  bash scripts/verify/stack.sh up\n\n'
  exit 2
fi

# A degraded Ollama runner keeps /health ok while every answer is garbage, and
# then every LLM check fails for reasons unrelated to the code (#119). Ask one
# question with a known answer first, unless LLM work is being skipped.
if [ "${RAG_SKIP_SLOW:-0}" != "1" ]; then
  if ! sanity=$(cd "$REPO_ROOT" && docker compose exec -T api python - < scripts/verify/llm_sanity.py 2>&1); then
    printf '\nThe LLM is not giving usable answers:\n  %s\n' "$sanity"
    printf 'Restart it and run again:  docker compose restart ollama\n'
    printf '(with the verify environment that scripts/verify/stack.sh up prints)\n\n'
    exit 2
  fi
  printf '\n%s\n' "$sanity"
fi

printf '\nBuilding fixtures in %s\n' "$FIX"
rm -rf "$FIX"; python3 ./fixtures.py "$FIX" >/dev/null
export RAG_FIXTURES="$FIX"

ALL=(01_infrastructure 02_ingest 03_query 04_goldstandard 05_transfer 06_ui 07_settings)
if [ "$#" -gt 0 ]; then
  SUITES=()
  for want in "$@"; do
    for s in "${ALL[@]}"; do
      case "$s" in "$want"*) SUITES+=("$s") ;; esac
    done
  done
else
  SUITES=("${ALL[@]}")
fi
[ "${#SUITES[@]}" -gt 0 ] || { printf 'No suite matched: %s\n' "$*"; exit 2; }

started=$(python3 -c "import time;print(time.time())")
declare -a RESULTS
overall=0
for suite in "${SUITES[@]}"; do
  printf '\n──────────────────────────────────────────────────────────────\n'
  printf '  %s\n' "$suite"
  printf '──────────────────────────────────────────────────────────────\n'
  if bash "./$suite.sh"; then
    RESULTS+=("  ok    $suite")
  else
    RESULTS+=("  FAIL  $suite")
    overall=1
  fi
done
elapsed=$(python3 -c "import time;print(int(time.time()-$started))")

printf '\n══════════════════════════════════════════════════════════════\n'
printf '  Summary  (%dm%02ds)\n' "$((elapsed/60))" "$((elapsed%60))"
printf '══════════════════════════════════════════════════════════════\n'
printf '%s\n' "${RESULTS[@]}"
if [ "$overall" -eq 0 ]; then
  printf '\n  All suites passed.\n\n'
else
  printf '\n  At least one suite failed.\n\n'
fi
exit "$overall"
