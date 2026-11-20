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

# #555 (reviewer): the fail-closed chain checks report a broken chain.
python3 - "$REPO_ROOT" <<'ENDPY'
import copy, sys
sys.path.insert(0, ".")
import lan_config
checkout = sys.argv[1]
config = lan_config.resolve(checkout)
bad_ui = copy.deepcopy(config)
bad_ui["services"]["ui"]["depends_on"]["api"]["condition"] = "service_started"
bad_proxy = copy.deepcopy(config)
del bad_proxy["services"]["proxy"]["depends_on"]["ui"]
failed = False
for label, cfg, expected in (("ui condition service_started", bad_ui, "the ui doesn't wait for a healthy api"),
                             ("no ui in the proxy's depends_on", bad_proxy, "the proxy doesn't wait for the ui")):
    found = lan_config.problems(cfg, checkout, "443")
    if not any(expected in problem for problem in found):
        failed = True
        print("    {}: not reported; problems were {}".format(label, found), file=sys.stderr)
sys.exit(1 if failed else 0)
ENDPY
check "lan_config reports a ui that doesn't wait for a healthy api, and a proxy that doesn't wait for the ui" $?

section "LAN units: setting, startup refusal, certificate step, nginx configuration"
(cd "$REPO_ROOT" && docker run --rm --network none -v "$REPO_ROOT:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api "$(docker compose images -q api)" python scripts/tests/test_lan_mode.py)
check "test_lan_mode.py registered regression checks" $?

section "LAN refusal in the built API image, on the running verify project (default or LAN configuration)"
# One-off api containers (no dependencies, removed afterwards) with LAN mode
# switched on by environment: the real image and its settings parsing refuse to
# start, with one line and no traceback, in every run, not only with --lan.
_rag_require_lock_owner
(cd "$REPO_ROOT" && python3 - <<'ENDPY'
import secrets, subprocess, sys
cases = [(["RAG_LAN_MODE=true", "AUTH_COOKIE_SECURE=false"], "LAN mode refused: AUTH_COOKIE_SECURE"),
         (["RAG_LAN_MODE=true", "AUTH_COOKIE_SECURE=true", "AUTH_DIR=/tmp/vfy-lan-empty-" + secrets.token_hex(4)],
          "LAN mode refused: there is no enabled AI Engineer"),
         (["RAG_LAN_MODE=yes"], "RAG_LAN_MODE must be exactly true or false")]
failed = False
for env, expected in cases:
    args = ["docker", "compose", "run", "--rm", "--no-deps", "-T"]
    for item in env:
        args += ["-e", item]
    try:
        result = subprocess.run(args + ["api"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, universal_newlines=True, timeout=300)
    except subprocess.TimeoutExpired:
        print("    {}: the API kept running".format(" ".join(env)), file=sys.stderr)
        failed = True
        continue
    lines = result.stdout.splitlines()
    hits = [line for line in lines if expected in line]
    problems = []
    if result.returncode == 0:
        problems.append("exit 0")
    if not hits:
        problems.append("no line with {!r}".format(expected))
    if len(hits) != 1 or any(line.startswith("Traceback") for line in lines):
        problems.append("not exactly one refusal line without a traceback")
    if problems:
        failed = True
        print("    {}: {}; last lines {}".format(" ".join(env), ", ".join(problems), lines[-5:]), file=sys.stderr)
sys.exit(1 if failed else 0)
ENDPY
)
check "the API image refuses LAN mode with one line and no traceback: a non-Secure cookie, no Engineer, a non-boolean RAG_LAN_MODE" $?

if [ "${RAG_VERIFY_LAN:-0}" != 1 ]; then
  skip "LAN mode on the running verify project" "run stack.sh run --lan"
  summary
  exit $?
fi

lan() { _rag_require_lock_owner; python3 ./lan_stack.py "$1"; }

section "LAN mode on the verify project (TLS on loopback only)"
lan https
check "HTTPS answers with the generated certificate: localhost and the test names, 825 days" $?
lan hosts
check "only localhost, 127.0.0.1 and the certificate's names are served; another Host gets 421 (#664)" $?
lan tls
check "TLS 1.2 and 1.3 connect, TLS 1.1 is refused, ALPN picks HTTP/2" $?
lan bindings
check "the only published port is the proxy's 127.0.0.1 TLS port; plain HTTP gets no API answer" $?
lan cookie
check "a login over HTTPS sets the session cookie Secure, HttpOnly and SameSite=Strict" $?
lan origin
check "a same-origin change over HTTPS is accepted, another origin's refused 403 CROSS_ORIGIN_REFUSED" $?
lan server-tokens
check "nginx sends no version: the Server header is plain nginx on an API answer and on its own 421 page (#555)" $?
lan refusals
check "the API refuses LAN mode with one line when AUTH_COOKIE_SECURE is false or no Engineer exists" $?
lan rate-limit
check "nginx refuses logins beyond its rate over HTTPS with 429 LOGIN_RATE_LIMITED" $?
lan address

summary
