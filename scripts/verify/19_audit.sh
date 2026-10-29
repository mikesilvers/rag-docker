#!/usr/bin/env bash
# The audit log (#29): SPECIFICATIONS.md §3.1.9 and §10.7.
#
# The unit checks run in the API image with no network. The stack checks run
# on the verify project through audit_stack.py, one check each, reading the
# records through GET /audit as the harness Engineer; those that create a
# throwaway VfyAudit<random> collection or vfy-s-<random> service principal run
# only on the verify project as stack.sh sets it up. The restart check is
# opt-in (RAG_ALLOW_RESTART=1) like the other restart checks.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
require_stack

operator=""
[ -n "$(volume_removal_refusal_reason)" ] || operator=--operator
stack() { _rag_require_lock_owner; python3 ./audit_stack.py "$@" $operator; }

section "Audit units: records, rotation, writers, settings, jobs, middleware, API, CSV, operator command"
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_audit.py)
check "test_audit.py registered regression checks" $?

section "Who may read it"
stack access
check "End User and Developer get 403 on /audit and /audit.csv; an engineer-role key gets KEY_NOT_ALLOWED (and is recorded)" $?

section "What is recorded"
stack start-record
check "the API's start wrote system.start, saying question text isn't logged" $?
stack sessions
check "a login, a failed login (anonymous, the typed username) and a logout, each with actor, role and address" $?
stack query
check "a query is recorded with its parameters and without its question text" $?
stack jobs
check "collection create and delete; an ingest and an encrypted export, each accepted and ended with the same job id" $?
stack refused
check "a Developer's refused delete is recorded: 403 FORBIDDEN, actor and role" $?
stack admin
check "an Engineer's principal and key administration is recorded with the target, never the key" $?

section "Reading it"
stack filters
check "filters, a one-record page and its cursor, and the CSV (header, a row, an escaped formula cell)" $?
stack no-secrets
check "no harness password, session token, API key or password typed in a change appears in the audit files" $?

section "Concurrent writers"
stack concurrent
check "parallel failed logins (nginx's burst of 5) and the operator command append together: each recorded once, every line whole" $?
stack coalesced-logins
check "repeated failed logins for one username: one record at once, then one with the count and last attempt (#577)" $?

section "Restart persistence (opt-in: it restarts the api)"
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then
  restart_refusal=$(restart_refusal_reason)
  if [ -n "$restart_refusal" ]; then
    check "records survive an API restart" 1 "$restart_refusal"
  else
    stack restart-prepare
    prepared=$?
    project="$COMPOSE_PROJECT_NAME"
    started=$(python3 -c "import time;print(time.time())")
    (cd "$REPO_ROOT" && docker compose -p "$project" restart api >/dev/null 2>&1)
    elapsed=$(wait_healthy_timed "$started" 300)
    [ "$prepared" = 0 ] && stack restart-verify
    check "records survive an API restart, which writes a new system.start" $? "healthy after ${elapsed:-never}s"
  fi
else
  skip "records survive an API restart" "set RAG_ALLOW_RESTART=1 to include it"
fi

summary
