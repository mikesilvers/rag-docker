#!/usr/bin/env bash
# LAN mode (#26): SPECIFICATIONS.md §2.5 and §10.5.
#
# Every run checks the real docker-compose.lan.yml as `docker compose config`
# resolves it (nothing is started) and runs the LAN unit tests in the API image
# with no network. The checks of a running LAN project need
# `stack.sh run --lan`, which switches the verify project to LAN mode with TLS
# on 127.0.0.1:<verify port> only; anywhere else they are skipped.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
require_stack

section "LAN override, resolved without starting it"
python3 ./lan_config.py "$REPO_ROOT"
check "docker-compose.lan.yml publishes only the proxy on every interface at 443 (RAG_LAN_PORT changes it), in LAN mode with a Secure cookie, after tls-init" $?

section "LAN units: setting, startup refusal, certificate step, nginx configuration"
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_lan_mode.py)
check "test_lan_mode.py registered regression checks" $?

if [ "${RAG_VERIFY_LAN:-0}" != 1 ]; then
  skip "LAN mode on the running verify project" "run stack.sh run --lan"
  summary
  exit $?
fi

lan() { _rag_require_lock_owner; python3 ./lan_stack.py "$1"; }

section "LAN mode on the verify project (TLS on loopback only)"
lan https
check "HTTPS answers with the generated certificate: localhost and the test names, 825 days" $?
lan tls
check "TLS 1.2 and 1.3 connect, TLS 1.1 is refused, ALPN picks HTTP/2" $?
lan bindings
check "the only published port is the proxy's 127.0.0.1 TLS port; plain HTTP gets no API answer" $?
lan cookie
check "a login over HTTPS sets the session cookie Secure, HttpOnly and SameSite=Strict" $?
lan origin
check "a same-origin change over HTTPS is accepted, another origin's refused 403 CROSS_ORIGIN_REFUSED" $?
lan refusals
check "the API refuses LAN mode with one line when AUTH_COOKIE_SECURE is false or no Engineer exists" $?
lan address

summary
