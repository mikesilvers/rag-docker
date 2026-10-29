#!/usr/bin/env bash
# Shared helpers for the verification suites.
#
# Sourced, never executed. Every suite expects a running stack and leaves the
# instance as it found it.
#
# A note on JSON and the shell: never round-trip an API response through `echo`.
# zsh and bash interpret backslash escapes differently, and an LLM-generated
# answer containing \n will be silently corrupted into invalid JSON. Pipe curl
# straight into python3, or capture to a file. This cost real debugging time
# more than once.

set -uo pipefail

# One verify run at a time; see lock.sh.
. "$(dirname "${BASH_SOURCE[0]}")/lock.sh"

API="${RAG_API:-http://localhost:8080/api}"
# Collections and packages created by the suites all carry this prefix so
# cleanup can find them without guessing.
PREFIX="${RAG_TEST_PREFIX:-Vfy}"
# Suites that need an LLM call are slow (20-60s each). Set RAG_SKIP_SLOW=1 for
# a structural-only run.
SKIP_SLOW="${RAG_SKIP_SLOW:-0}"

PASS=0; FAIL=0; SKIP=0
FAILED_NAMES=()

_c_pass=$'\033[32m'; _c_fail=$'\033[31m'; _c_skip=$'\033[33m'; _c_off=$'\033[0m'
[ -t 1 ] || { _c_pass=""; _c_fail=""; _c_skip=""; _c_off=""; }

section() { printf '\n  %s\n' "$1"; }

# check <name> <condition-exit-status> [detail]
check() {
  local name="$1" ok="$2" detail="${3:-}"
  if [ "$ok" = "0" ]; then
    PASS=$((PASS+1)); printf '    %sPASS%s  %s\n' "$_c_pass" "$_c_off" "$name"
  else
    FAIL=$((FAIL+1)); FAILED_NAMES+=("$name")
    printf '    %sFAIL%s  %s%s\n' "$_c_fail" "$_c_off" "$name" "${detail:+  — $detail}"
  fi
}

# check_eq <name> <actual> <expected>
check_eq() {
  local name="$1" actual="$2" expected="$3"
  [ "$actual" = "$expected" ]
  check "$name" $? "got '$actual', wanted '$expected'"
}

skip() {
  SKIP=$((SKIP+1)); printf '    %sSKIP%s  %s%s\n' "$_c_skip" "$_c_off" "$1" "${2:+  — $2}"
}

# jq-free JSON field read: api_get <path> | jfield <expr>
# `expr` is python indexing against the parsed document, e.g. ['status']
jfield() { python3 -c "import json,sys; d=json.load(sys.stdin); print(d$1)" 2>/dev/null; }

# Every helper below that reaches the API first checks that this suite's verify
# lock is still held by it or an ancestor (#184). A suite orphaned by a run that
# died after it passed lock.sh stops at its next helper call, before any
# request, instead of acting on a later run's verify project. The suite ends
# even when the helper runs inside $(...) or a pipeline, and its EXIT trap
# still runs. Not re-checked: direct curl and `docker compose exec` calls in the
# suites, and the Python helpers, which end on their own timeouts (up to 900 s).
_rag_require_lock_owner() {
  _rag_lock_owner_ok && return 0
  _rag_lock_leftover
  # In a subshell, exit alone would end only the subshell.
  [ "${BASHPID:-}" = "$$" ] || kill -TERM "$$" 2>/dev/null
  exit 3
}

api_get()  { _rag_require_lock_owner; curl -s -m 120 "$API$1"; }
api_code() { _rag_require_lock_owner; curl -s -o /dev/null -m 120 -w '%{http_code}' "$@"; }
api_post() { _rag_require_lock_owner; curl -s -m 600 -X POST "$API$1" -H 'Content-Type: application/json' -d "$2"; }
api_post_code() { _rag_require_lock_owner; curl -s -o /dev/null -m 600 -w '%{http_code}' -X POST "$API$1" -H 'Content-Type: application/json' -d "$2"; }
# Any other request, headers and body included: pass curl's own arguments and a full URL.
api_request() { _rag_require_lock_owner; curl -s -m 120 "$@"; }

require_stack() {
  local code
  code=$(api_code "$API/health")
  if [ "$code" != "200" ]; then
    printf '\n  Cannot reach a healthy API at %s (HTTP %s).\n' "$API" "$code"
    printf '  Start the verify project first:  bash scripts/verify/stack.sh up\n\n'
    exit 2
  fi
  auth_ensure
}

# ── test accounts (#25) ──────────────────────────────────────────────────────
# Every API route but /health and the login needs a session. auth_setup.py keeps
# one account per role (vfy-engineer, vfy-developer, vfy-end-user) with its
# password in $RAG_AUTH_DIR/credentials (mode 0600, folder 0700, outside the
# checkout), never printed, on a command line or in the environment. It runs
# the operator command (`python -m auth`) only when the target is the verify
# project as stack.sh sets it up, the same condition as removing its volumes;
# anywhere else a password that doesn't work is a failure, never a reset.
#
# The suites then send the engineer's session with every request: the curl
# function below adds the cookie jar and a same-origin Origin (the API refuses a
# cookie-authenticated change without one) to every curl call in a suite,
# api_* helpers included. A check that needs no session, another role or another
# origin calls `command curl` and passes its own. Python helpers read the same
# jar through auth_headers.py.
RAG_AUTH_DIR="${RAG_AUTH_DIR:-$(printf '%s' "${TMPDIR:-/tmp}" | sed 's:/*$::')/rag-verify-auth-$(id -u)}"
# The script that does it; test_verify_stack.py's sandboxes, which stub docker
# and curl, put a stand-in here. The --operator condition below stays lib.sh's.
RAG_AUTH_SETUP="${RAG_AUTH_SETUP:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/auth_setup.py}"
RAG_AUTH_ORIGIN=$(python3 -c 'import sys; from urllib.parse import urlsplit; u = urlsplit(sys.argv[1]); print(u.scheme + "://" + u.netloc)' "$API")
export RAG_AUTH_DIR RAG_AUTH_ORIGIN

curl() {
  if [ -n "${RAG_AUTH_JAR:-}" ]; then
    command curl -b "$RAG_AUTH_JAR" -H "Origin: $RAG_AUTH_ORIGIN" "$@"
  else
    command curl "$@"
  fi
}

# auth_jar <role>: logs the role's test account in and prints its cookie jar.
auth_jar() {
  local jar="$RAG_AUTH_DIR/$1.jar"
  _rag_require_lock_owner
  python3 "$RAG_AUTH_SETUP" login "$1" "$jar" || return 1
  printf '%s' "$jar"
}

auth_ensure() {
  local operator="" jar
  _rag_require_lock_owner
  [ -n "$(volume_removal_refusal_reason)" ] || operator=--operator
  if ! python3 "$RAG_AUTH_SETUP" ensure $operator; then
    printf '\n  Could not set up the test accounts on %s (see above).\n\n' "$API"
    exit 2
  fi
  jar=$(auth_jar engineer) || { printf '\n  The test engineer could not log in.\n\n'; exit 2; }
  RAG_AUTH_JAR="$jar"
  export RAG_AUTH_JAR
}

# wait_for_job <url-path> [timeout-seconds] — polls until status is terminal.
# Echoes the final status.
wait_for_job() {
  local path="$1" limit="${2:-600}" waited=0 status=""
  while [ "$waited" -lt "$limit" ]; do
    _rag_require_lock_owner
    status=$(api_get "$path" | jfield "['status']")
    case "$status" in
      completed|failed|partial|cancelled) printf '%s' "$status"; return 0 ;;
    esac
    sleep 2; waited=$((waited+2))
  done
  printf 'timeout'; return 1
}

make_collection() {
  api_post "/collections" "{\"name\":\"$1\",\"index_type\":\"${2:-hnsw}\",\"distance_metric\":\"${3:-cosine}\",\"hnsw_config\":{\"efConstruction\":128,\"maxConnections\":64,\"ef\":64}}" >/dev/null
}

drop_collection() { _rag_require_lock_owner; curl -s -o /dev/null -m 120 -X DELETE "$API/collections/$1?confirm=true"; }

# Remove every collection and package this run created.
cleanup_prefixed() {
  local names
  names=$(api_get "/collections" | python3 -c "
import json,sys
for c in json.load(sys.stdin)['collections']:
    if c['name'].startswith('$PREFIX'): print(c['name'])" 2>/dev/null)
  for n in $names; do drop_collection "$n"; done
  rm -f "${RAG_EXPORTS_DIR:-${REPO_ROOT:-.}/exports}"/ragpkg-"$(echo "$PREFIX" | tr '[:upper:]' '[:lower:]')"*.tar.gz 2>/dev/null || true
}

summary() {
  printf '\n  %d passed, %d failed, %d skipped\n' "$PASS" "$FAIL" "$SKIP"
  if [ "$FAIL" -gt 0 ]; then
    printf '  failed:\n'
    printf '    - %s\n' "${FAILED_NAMES[@]}"
    return 1
  fi
  return 0
}

# ── the live stack is off limits (#152) ──────────────────────────────────────
# Verification runs on the disposable `rag-verify` project that
# scripts/verify/stack.sh brings up, never on the live `rag-docker` stack that
# holds real data. A target is live when the compose project these scripts
# would act on is rag-docker, or when RAG_API uses the live port 8080.

# Prints why the current target is the live stack, or nothing.
live_target_reason() {
  local project="${COMPOSE_PROJECT_NAME:-}" root port
  if [ -z "$project" ]; then
    # What compose would use: COMPOSE_PROJECT_NAME from the checkout's .env
    # (optionally after `export `; the last one wins; quotes removed), else
    # a `name:` in the compose file, else the folder name (#154).
    root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    project=$(sed -n 's/^\(export[[:space:]]\{1,\}\)\{0,1\}COMPOSE_PROJECT_NAME=//p' "$root/.env" 2>/dev/null | tail -1 \
      | sed -e 's/^"\(.*\)"$/\1/' -e t -e "s/^'\(.*\)'\$/\1/")
    [ -n "$project" ] || project=$(sed -n 's/^name:[[:space:]]*["'"'"']\{0,1\}\([^"'"'"'[:space:]]*\).*/\1/p' "$root/docker-compose.yml" 2>/dev/null | head -1)
    [ -n "$project" ] || project=$(basename "$root")
  fi
  # Lowercased, everything outside [a-z0-9_-] dropped, and leading `-` and
  # `_` stripped, as compose normalises a name.
  project=$(printf '%s' "$project" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9_-' | sed 's/^[-_]*//')
  port=$(python3 -c '
import sys
from urllib.parse import urlsplit
url = urlsplit(sys.argv[1])
try:
    port = url.port
except ValueError:
    port = None
print(port if port is not None else (443 if url.scheme == "https" else 80))' "$API" 2>/dev/null)
  if [ "$project" = "rag-docker" ]; then
    printf 'the compose project is rag-docker, the live rag-docker stack'
  elif [ "$port" = "8080" ]; then
    printf 'RAG_API (%s) uses port 8080, the live rag-docker stack' "$API"
  fi
}

# Refuses a live target unless RAG_VERIFY_LIVE=1, which only warns.
live_guard() {
  local reason
  reason=$(live_target_reason)
  [ -n "$reason" ] || return 0
  if [ "${RAG_VERIFY_LIVE:-0}" = "1" ]; then
    printf '\n  WARNING: RAG_VERIFY_LIVE=1, so this run targets the live rag-docker stack:\n  %s.\n\n' "$reason" >&2
    return 0
  fi
  printf '\n  Refusing to verify against the live rag-docker stack: %s.\n' "$reason" >&2
  printf '  Run it on the disposable verify project:  bash scripts/verify/stack.sh run\n' >&2
  printf '  (RAG_VERIFY_LIVE=1 overrides this; see scripts/verify/README.md.)\n\n' >&2
  exit 2
}

# Prints why a restart must not run here, or nothing. Restarts never reach the
# live project, whatever RAG_VERIFY_LIVE says.
restart_refusal_reason() {
  local project
  project=$(printf '%s' "${COMPOSE_PROJECT_NAME:-}" | tr '[:upper:]' '[:lower:]')
  if [ -z "$project" ]; then
    printf 'COMPOSE_PROJECT_NAME is not set, so a restart could reach the live rag-docker stack; run it through scripts/verify/stack.sh'
  elif [ "$project" = "rag-docker" ]; then
    printf 'restarts never run against the live rag-docker project (#152)'
  fi
}

# Prints why removing the project's volumes (`docker compose down -v`) must not
# run here, or nothing (#465). Only the verify project as stack.sh sets it up:
# COMPOSE_PROJECT_NAME rag-verify and a COMPOSE_FILE that includes the overlay
# docker-compose.verify.yml, whose model volume is external and so kept. Any
# other project would lose all its volumes, models included.
volume_removal_refusal_reason() {
  local reason sep="${COMPOSE_PATH_SEPARATOR:-:}" rest entry
  reason=$(restart_refusal_reason)
  if [ -n "$reason" ]; then printf '%s' "$reason"; return 0; fi
  if [ "$COMPOSE_PROJECT_NAME" != rag-verify ]; then
    printf "volumes are removed only on the verify project rag-verify, not '%s'; run it through scripts/verify/stack.sh" "$COMPOSE_PROJECT_NAME"
    return 0
  fi
  # Split as compose splits COMPOSE_FILE; only that exact file name counts.
  rest="${COMPOSE_FILE:-}"
  while [ -n "$rest" ]; do
    entry="${rest%%"$sep"*}"
    [ "${entry##*/}" = docker-compose.verify.yml ] && return 0
    [ "$entry" = "$rest" ] && break
    rest="${rest#*"$sep"}"
  done
  printf 'COMPOSE_FILE does not include docker-compose.verify.yml, whose model volume is kept; run it through scripts/verify/stack.sh'
}

# Default for RAG_RESTART_LIMIT_S (#130): Weaviate's start_period in
# docker-compose.yml plus 60s for `down` and the api's own start.
RESTART_LIMIT_DEFAULT_S=240

# Prints the restart limit in seconds, or, with status 1, why the value of
# RAG_RESTART_LIMIT_S is refused. Empty means unset.
restart_limit() {
  local value="${RAG_RESTART_LIMIT_S:-$RESTART_LIMIT_DEFAULT_S}"
  case "$value" in
    ''|*[!0-9]*) ;;
    *) if [ "${#value}" -le 6 ] && [ "$((10#$value))" -gt 0 ]; then printf '%s' "$((10#$value))"; return 0; fi ;;
  esac
  printf "RAG_RESTART_LIMIT_S must be a positive whole number of seconds (at most 6 digits), not '%s'" "$value"
  return 1
}

# wait_healthy_timed <started-epoch> <cap-seconds>: polls /health every 2s.
# Prints the whole seconds since <started> once it returns 200, or nothing
# once <cap> seconds have passed.
wait_healthy_timed() {
  local started="$1" cap="$2" code now
  while :; do
    code=$(api_code "$API/health")
    now=$(python3 -c "import time;print(int(time.time()-$started))")
    if [ "$code" = "200" ]; then printf '%s' "$now"; return 0; fi
    [ "$now" -lt "$cap" ] || return 0
    sleep 2
  done
}

# restart_timing_check <limit> <elapsed, or empty if never healthy>: the
# restart timing result, with the measured time on a pass and on a fail.
restart_timing_check() {
  local limit="$1" elapsed="$2"
  if [ -n "$elapsed" ] && [ "$elapsed" -le "$limit" ]; then
    check "restart reaches healthy within ${limit}s (took ${elapsed}s)" 0
  elif [ -n "$elapsed" ]; then
    check "restart reaches healthy within ${limit}s" 1 "took ${elapsed}s"
  else
    check "restart reaches healthy within ${limit}s" 1 "not healthy after $((limit * 2))s"
  fi
}

# Suites (NN_*.sh) are guarded as soon as they source this file. Other scripts
# that borrow these helpers are not suites and are left alone.
case "$(basename "$0")" in
  [0-9][0-9]_*.sh) live_guard ;;
esac
