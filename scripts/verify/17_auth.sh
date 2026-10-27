#!/usr/bin/env bash
# Authentication and the endpoint/role matrix (#25): SPECIFICATIONS.md §3.1.8
# and §10.6.
#
# The unit checks run in the API image with no network. The stack checks run
# on the verify project through auth_stack.py, one check each; those that create
# throwaway vfy-t-<random> accounts or age a session use the operator command,
# so they run only on the verify project as stack.sh sets it up. The restart
# check is opt-in (RAG_ALLOW_RESTART=1) like the other restart checks.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
require_stack

operator=""
[ -n "$(volume_removal_refusal_reason)" ] || operator=--operator
stack() { _rag_require_lock_owner; python3 ./auth_stack.py "$@" $operator; }

section "Auth units: passwords, store, policy, middleware, routes, operator command"
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_auth.py)
check "test_auth.py registered regression checks" $?
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_auth_enforcement.py)
check "test_auth_enforcement.py: every protected route refuses a request without a session" $?
python3 "$REPO_ROOT/scripts/tests/test_verify_auth.py"
check "test_verify_auth.py: the harness keeps its credentials private" $?

section "Endpoint/role matrix on the stack (H1)"
stack public
check "/health and /auth/status answer without a session" $?
stack matrix anonymous
check "without a session every protected route, downloads and job polling included, answers 401" $?
stack matrix end_user
check "End User: 403 exactly on the routes above its role" $?
stack matrix developer
check "Developer: 403 exactly on the routes above its role" $?
stack matrix engineer
check "Engineer: no 401 or 403 on any route" $?

section "Sessions (H2)"
stack login-logout
check "login, GET /auth/me and logout; a logged-out session answers 401" $?
stack cookie
check "the session cookie is HttpOnly, SameSite=Strict, Path=/, Max-Age 7 days, not Secure on loopback" $?
stack reset
check "a password reset ends the user's sessions and forces a change" $?
stack disable
check "disabling a user ends their session and refuses their login; enable restores it" $?
stack role-change
check "a role change applies to an existing session on its next request" $?
stack own-password
check "the own password change: rules, current password, other sessions ended" $?
stack idle-expiry
check "a session idle for more than 12 hours answers 401" $?

section "Request protection and login throttling (H4)"
stack no-cors
check "no Access-Control-* headers, preflight included" $?
stack origin
check "a cookie-authenticated change from another origin is refused; same-origin passes" $?
stack generic-failure
check "a wrong password and an unknown user get the same 401 LOGIN_FAILED" $?
stack throttle
check "five failed logins lock the username and address for 15 minutes" $?
stack bootstrap-refused
check "bootstrap is refused once set up; an invalid username is a usage error" $?
stack no-secrets-logged
check "the API log names logins and holds no harness password or session token" $?

section "Restart persistence (opt-in: it restarts the api)"
if [ "${RAG_ALLOW_RESTART:-0}" = "1" ]; then
  restart_refusal=$(restart_refusal_reason)
  if [ -n "$restart_refusal" ]; then
    check "a session and a lockout survive an API restart" 1 "$restart_refusal"
  else
    stack restart-prepare
    prepared=$?
    project="$COMPOSE_PROJECT_NAME"
    started=$(python3 -c "import time;print(time.time())")
    (cd "$REPO_ROOT" && docker compose -p "$project" restart api >/dev/null 2>&1)
    elapsed=$(wait_healthy_timed "$started" 300)
    [ "$prepared" = 0 ] && stack restart-verify
    check "a session and a lockout survive an API restart" $? "healthy after ${elapsed:-never}s"
  fi
else
  skip "a session and a lockout survive an API restart" "set RAG_ALLOW_RESTART=1 to include it"
fi

summary
