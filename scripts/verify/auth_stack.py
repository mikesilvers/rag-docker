"""Authentication checks on the verify project (#25), one per subcommand. Host-side,
standard library, Python 3.9+. Run by 17_auth.sh, which turns each exit status
into a check:

    python3 auth_stack.py CHECK [--operator]

Exit 0 when the check holds, 1 when it doesn't (the reason on stderr). Checks
that create throwaway accounts (vfy-t-<random>) or age a session need the
operator command, so they need --operator, which 17_auth.sh passes only on the
verify project as stack.sh sets it up. No password or session token is printed.
"""
import hashlib
import json
import os
import secrets
import subprocess
import sys
import urllib.error
import urllib.request

import auth_headers
import auth_setup
from auth_setup import REPO, request

ROLES = ("end_user", "developer", "engineer")
X = "VfyAuthNoSuch"
# S2.2 of the specifications, written out here rather than read from the API,
# so the API is checked against the agreed matrix and not against itself.
# (method, path, body, required role). Bodies are empty or invalid on purpose:
# an allowed request must fail validation or find nothing, never do work.
MATRIX = (
    ("GET", "/collections", None, "end_user"),
    ("POST", "/query", {}, "end_user"),
    ("GET", "/retrieval/config/" + X, None, "end_user"),
    ("POST", "/collections", {}, "developer"),
    ("POST", "/ingest/upload", {}, "developer"),
    ("GET", "/ingest/job/vfy-no-such-job", None, "developer"),
    ("GET", "/ingest/config/" + X, None, "developer"),
    ("POST", "/ingest/config", {}, "developer"),
    ("POST", "/retrieval/config", {}, "developer"),
    ("POST", "/goldstandard/generate", {}, "developer"),
    ("GET", "/goldstandard/session/gs_vfynosuch", None, "developer"),
    ("PATCH", "/goldstandard/session/gs_vfynosuch/pair/p", {}, "developer"),
    ("POST", "/goldstandard/regenerate", {}, "developer"),
    ("POST", "/goldstandard/save", {}, "developer"),
    ("GET", "/goldstandard/download/vfy_no_such.json", None, "developer"),
    ("POST", "/export", {}, "developer"),
    ("GET", "/export/job/vfy-no-such-job", None, "developer"),
    ("POST", "/import", {}, "developer"),
    ("GET", "/import/job/vfy-no-such-job", None, "developer"),
    ("GET", "/packages", None, "developer"),
    ("GET", "/help/transfer", None, "developer"),
    ("GET", "/tune/" + X, None, "developer"),
    ("DELETE", "/collections/" + X + "?confirm=true", None, "engineer"),
    ("POST", "/tune/rechunk", {}, "engineer"),
    ("POST", "/tune/reembed", {}, "engineer"),
    ("POST", "/tune/reindex", {}, "engineer"),
    ("GET", "/tune/job/vfy-no-such-job", None, "engineer"),
    ("GET", "/metrics/latency", None, "engineer"),
    ("GET", "/goldstandard/diagnostics", None, "engineer"),
    ("GET", "/docs", None, "engineer"),
    ("GET", "/docs/oauth2-redirect", None, "engineer"),
    ("GET", "/redoc", None, "engineer"),
    ("GET", "/openapi.json", None, "engineer"),
)
SESSION_ROUTES = (("GET", "/auth/me", None), ("POST", "/auth/logout", None), ("POST", "/auth/password", {}))


class CheckFailed(Exception):
    pass


def expect(condition, why):
    if not condition:
        raise CheckFailed(why)


def compose(*args, stdin=None):
    return subprocess.run(["docker", "compose"] + list(args), cwd=str(REPO), input=stdin,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)


def role_token(role):
    """A fresh session for the harness account of ROLE."""
    username, password = auth_setup.read_credentials(auth_setup.private_dir())[role]
    status, _, token = request("POST", "/auth/login", {"username": username, "password": password})
    expect(status == 200 and token, "{} could not log in (HTTP {})".format(username, status))
    return token


def code(method, path, body=None, token=None, headers=None):
    """The status of a request sent as a same-origin browser would, or with `headers` instead."""
    api = auth_setup.api_base()
    data = json.dumps(body).encode() if body is not None else None
    sent = {"Origin": auth_setup.origin()} if headers is None else dict(headers)
    if data is not None:
        sent["Content-Type"] = "application/json"
    if token:
        sent["Cookie"] = "rag_session=" + token
    req = urllib.request.Request(api + path, data=data, method=method, headers=sent)
    try:
        with urllib.request.urlopen(req, timeout=120) as response:
            return response.status, response.read(), response.headers
    except urllib.error.HTTPError as error:
        return error.code, error.read(), error.headers


def error_code(raw):
    try:
        return json.loads(raw)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None


def throwaway(role, operator_ok):
    """A new account of ROLE, logged in with its generated password replaced.
    Returns (username, password, token)."""
    expect(operator_ok, "needs the operator command, which runs only on the verify project")
    username = "vfy-t-" + secrets.token_hex(4)
    rc, password = auth_setup.operator("create-user", username, role)
    expect(rc == 0 and password, "create-user {} failed (exit {})".format(username, rc))
    status, body, token = request("POST", "/auth/login", {"username": username, "password": password})
    expect(status == 200 and body.get("must_change_password") is True,
           "a created account must change its password at first login (HTTP {}, {})".format(status, body))
    status, raw, _ = code("GET", "/collections", token=token)
    expect(status == 403 and error_code(raw) == "PASSWORD_CHANGE_REQUIRED",
           "before the change, GET /collections answered {} {}".format(status, error_code(raw)))
    new = secrets.token_urlsafe(18)
    status, _, _ = code("POST", "/auth/password", {"current_password": password, "new_password": new}, token=token)
    expect(status == 200, "the forced change answered {}".format(status))
    return username, new, token


# ── checks ───────────────────────────────────────────────────────────────────

def check_matrix(who, operator_ok):
    wrong = []
    token = None if who == "anonymous" else role_token(who)
    for method, path, body, need in MATRIX:
        status, raw, _ = code(method, path, body, token=token)
        if who == "anonymous":
            ok = status == 401
        elif ROLES.index(who) < ROLES.index(need):
            ok = status == 403 and error_code(raw) == "FORBIDDEN"
        else:
            ok = status not in (401, 403)
        if not ok:
            wrong.append("{} {} -> {} {}".format(method, path, status, error_code(raw)))
    if who == "anonymous":
        for method, path, body in SESSION_ROUTES:
            status, raw, _ = code(method, path, body)
            if status != 401:
                wrong.append("{} {} -> {}".format(method, path, status))
    else:
        request("POST", "/auth/logout", token=token)
    expect(not wrong, "; ".join(wrong))


def check_public(operator_ok):
    status, raw, _ = code("GET", "/health", headers={})
    expect(status in (200, 503), "GET /health without a session answered {}".format(status))
    status, raw, _ = code("GET", "/auth/status", headers={})
    expect(status == 200 and json.loads(raw) == {"setup_required": False},
           "GET /auth/status answered {} {!r}".format(status, raw[:200]))


def check_control_path(operator_ok):
    # Starlette would route /ingest/upload%0A to the upload handler; the API
    # refuses it as an unknown path before reading the body (#25 review).
    body = b"--x\r\nContent-Disposition: form-data; name=\"collection\"\r\n\r\nVfyAuthNoSuch\r\n--x--\r\n"
    req = urllib.request.Request(auth_setup.api_base() + "/ingest/upload%0A", data=body, method="POST",
                                 headers={"Content-Type": "multipart/form-data; boundary=x"})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            status, raw = response.status, response.read()
    except urllib.error.HTTPError as error:
        status, raw = error.code, error.read()
    # The API's own 404, or nginx refusing the path itself (400); anything else
    # (a 403 from the app's fail-closed check, a 422 from the handler) means the
    # request got past the middleware and its body was read.
    expect((status == 404 and error_code(raw) == "NOT_FOUND") or (status == 400 and error_code(raw) is None),
           "POST /api/ingest/upload%0A without a session answered {} {}".format(status, error_code(raw)))


def check_login_logout(operator_ok):
    token = role_token("developer")
    status, raw, _ = code("GET", "/auth/me", token=token)
    body = json.loads(raw)
    expect(status == 200 and body == {"username": "vfy-developer", "role": "developer", "must_change_password": False},
           "GET /auth/me answered {} {}".format(status, body))
    status, _, _ = code("POST", "/auth/logout", token=token)
    expect(status == 200, "logout answered {}".format(status))
    status, raw, _ = code("GET", "/auth/me", token=token)
    expect(status == 401 and error_code(raw) == "AUTH_REQUIRED", "after logout the session answered {}".format(status))


def check_cookie(operator_ok):
    username, password = auth_setup.read_credentials(auth_setup.private_dir())["end_user"]
    status, _, headers = code("POST", "/auth/login", {"username": username, "password": password})
    cookies = headers.get_all("Set-Cookie") or []
    expect(status == 200 and len(cookies) == 1, "login answered {} with {} cookies".format(status, len(cookies)))
    attributes = [a.strip().lower() for a in cookies[0].split(";")[1:]]
    for want in ("httponly", "samesite=strict", "path=/", "max-age=604800"):
        expect(want in attributes, "the session cookie lacks {}".format(want))
    expect("secure" not in attributes, "the loopback profile's cookie is marked Secure")
    token = cookies[0].split(";", 1)[0].split("=", 1)[1]
    request("POST", "/auth/logout", token=token)


def check_no_cors(operator_ok):
    token = role_token("engineer")
    for method, path, headers in (("GET", "/collections", {"Origin": "http://other.example"}),
                                  ("OPTIONS", "/collections", {"Origin": "http://other.example",
                                                               "Access-Control-Request-Method": "DELETE"})):
        _, _, answer = code(method, path, token=token, headers=headers)
        expect(not any(k.lower().startswith("access-control-") for k in answer.keys()),
               "{} {} answered with CORS headers".format(method, path))
    request("POST", "/auth/logout", token=token)


def check_origin(operator_ok):
    token = role_token("developer")
    # index_type 7 is invalid, so an allowed request is a 422 and creates nothing.
    body = {"name": "VfyAuthOrigin" + secrets.token_hex(3), "index_type": 7}
    cases = (("no Origin and no Sec-Fetch-Site", {}, 403),
             ("another origin", {"Origin": "http://evil.example"}, 403),
             ("the right host on another port", {"Origin": auth_setup.origin().rsplit(":", 1)[0] + ":1"}, 403),
             ("Origin null", {"Origin": "null"}, 403),
             ("Sec-Fetch-Site cross-site", {"Sec-Fetch-Site": "cross-site"}, 403),
             ("Sec-Fetch-Site same-origin", {"Sec-Fetch-Site": "same-origin"}, None),
             ("the same origin", {"Origin": auth_setup.origin()}, None))
    for name, headers, want in cases:
        status, raw, _ = code("POST", "/collections", body, token=token, headers=headers)
        if want == 403:
            expect(status == 403 and error_code(raw) == "CROSS_ORIGIN_REFUSED",
                   "{}: answered {} {}".format(name, status, error_code(raw)))
        else:
            expect(status == 422, "{}: answered {} {}, wanted the 422 of an allowed request".format(
                name, status, error_code(raw)))
    status, _, _ = code("GET", "/collections", token=token, headers={"Origin": "http://evil.example"})
    expect(status == 200, "a cross-origin GET answered {} (reads aren't origin-checked)".format(status))
    request("POST", "/auth/logout", token=token)


def check_generic_failure(operator_ok):
    username, password = auth_setup.read_credentials(auth_setup.private_dir())["end_user"]
    answers = []
    for name, secret in ((username, password + "x"), ("vfy-nobody-" + secrets.token_hex(4), password)):
        status, raw, _ = code("POST", "/auth/login", {"username": name, "password": secret})
        answers.append((status, raw))
    expect(answers[0] == answers[1] and answers[0][0] == 401 and error_code(answers[0][1]) == "LOGIN_FAILED",
           "wrong password and unknown user answered differently: {}".format(
               [(s, error_code(r)) for s, r in answers]))


def check_throttle(operator_ok):
    name = "vfy-throttle-" + secrets.token_hex(4)
    seen = []
    for _ in range(5):
        status, raw, _ = code("POST", "/auth/login", {"username": name, "password": "x" * 12})
        seen.append(status)
    status, raw, _ = code("POST", "/auth/login", {"username": name, "password": "x" * 12})
    expect(seen == [401] * 5 and status == 429 and error_code(raw) == "LOGIN_THROTTLED",
           "five failures then {} {} (first five: {})".format(status, error_code(raw), seen))
    if operator_ok:
        username, password, token = throwaway("end_user", operator_ok)
        for _ in range(5):
            code("POST", "/auth/login", {"username": username, "password": "y" * 12})
        status, raw, _ = code("POST", "/auth/login", {"username": username, "password": password})
        expect(status == 429, "the right password during a lockout answered {}".format(status))
        status, _, _ = code("GET", "/auth/me", token=token)
        expect(status == 200, "a lockout ended the user's existing session ({})".format(status))


def check_reset(operator_ok):
    username, password, token = throwaway("developer", operator_ok)
    rc, new = auth_setup.operator("reset-password", username)
    expect(rc == 0 and new, "reset-password failed (exit {})".format(rc))
    status, _, _ = code("GET", "/auth/me", token=token)
    expect(status == 401, "a reset left the old session working ({})".format(status))
    status, _, _ = code("POST", "/auth/login", {"username": username, "password": password})
    expect(status == 401, "the old password still logs in ({})".format(status))
    status, body, token = request("POST", "/auth/login", {"username": username, "password": new})
    expect(status == 200 and body.get("must_change_password") is True, "the reset password must be changed")


def check_disable(operator_ok):
    username, password, token = throwaway("developer", operator_ok)
    rc, _ = auth_setup.operator("disable", username)
    expect(rc == 0, "disable failed (exit {})".format(rc))
    status, _, _ = code("GET", "/collections", token=token)
    expect(status == 401, "a disabled user's session still works ({})".format(status))
    status, raw, _ = code("POST", "/auth/login", {"username": username, "password": password})
    expect(status == 401 and error_code(raw) == "LOGIN_FAILED", "a disabled user can log in ({})".format(status))
    rc, _ = auth_setup.operator("enable", username)
    status, _, _ = code("POST", "/auth/login", {"username": username, "password": password})
    expect(rc == 0 and status == 200, "enable did not restore the login ({}, {})".format(rc, status))


def check_role_change(operator_ok):
    username, password, token = throwaway("developer", operator_ok)
    status, _, _ = code("GET", "/packages", token=token)
    expect(status == 200, "a developer can't list packages ({})".format(status))
    rc, _ = auth_setup.operator("set-role", username, "end_user")
    status, raw, _ = code("GET", "/packages", token=token)
    expect(rc == 0 and status == 403 and error_code(raw) == "FORBIDDEN",
           "after the demotion the same session answered {} {}".format(status, error_code(raw)))
    status, raw, _ = code("GET", "/auth/me", token=token)
    expect(json.loads(raw).get("role") == "end_user", "GET /auth/me still names the old role")
    rc, _ = auth_setup.operator("set-role", username, "engineer")
    status, _, _ = code("GET", "/metrics/latency", token=token)
    expect(rc == 0 and status == 200, "after a promotion the same session answered {}".format(status))


def check_own_password(operator_ok):
    username, password, token = throwaway("end_user", operator_ok)
    status, body, other = request("POST", "/auth/login", {"username": username, "password": password})
    expect(status == 200 and other, "a second login failed ({})".format(status))
    status, raw, _ = code("POST", "/auth/password", {"current_password": "wrong" * 3, "new_password": "z" * 14},
                          token=token)
    expect(status == 403 and error_code(raw) == "PASSWORD_INCORRECT", "a wrong current password answered {}".format(status))
    for bad in ("short", username.upper(), "q" * 257):
        status, raw, _ = code("POST", "/auth/password", {"current_password": password, "new_password": bad},
                              token=token)
        expect(status == 422 and error_code(raw) == "INVALID_PASSWORD",
               "a {}-character new password answered {}".format(len(bad), status))
    status, raw, _ = code("POST", "/auth/password", {"current_password": password, "new_password": password},
                          token=token)
    expect(status == 422 and error_code(raw) == "INVALID_PASSWORD", "the same password again answered {}".format(status))
    new = secrets.token_urlsafe(18)
    status, _, _ = code("POST", "/auth/password", {"current_password": password, "new_password": new}, token=token)
    expect(status == 200, "the change answered {}".format(status))
    expect(code("GET", "/auth/me", token=token)[0] == 200, "the change ended the session that made it")
    expect(code("GET", "/auth/me", token=other)[0] == 401, "the change left another session working")


def check_bootstrap_refused(operator_ok):
    expect(operator_ok, "needs the operator command, which runs only on the verify project")
    rc, out = auth_setup.operator("bootstrap", "vfy-second-" + secrets.token_hex(3))
    expect(rc == 1 and not out, "bootstrap on a set-up project exited {} (printed {} characters)".format(rc, len(out)))
    rc, out = auth_setup.operator("create-user", "x", "engineer")
    expect(rc == 2 and not out, "an invalid username exited {}".format(rc))


def check_idle_expiry(operator_ok):
    username, password, token = throwaway("end_user", operator_ok)
    digest = hashlib.sha256(token.encode()).hexdigest()
    # Ages this one session past the 12-hour idle limit, in the verify project's store.
    script = ("import sqlite3,sys,time\nfrom auth import store\n"
              "c=sqlite3.connect(store.db_path());c.execute('UPDATE sessions SET last_seen=? WHERE token_hash=?',"
              "(time.time()-13*3600,sys.argv[1]));c.commit();print(c.total_changes)\n")
    result = compose("exec", "-T", "api", "python", "-c", script, digest)
    expect(result.returncode == 0 and result.stdout.strip() == "1", "could not age the session: {}".format(
        result.stderr.strip()[-200:]))
    status, raw, _ = code("GET", "/auth/me", token=token)
    expect(status == 401 and error_code(raw) == "AUTH_REQUIRED", "an idle session still works ({})".format(status))


def check_no_secrets_logged(operator_ok):
    secrets_known = [password for _, password in auth_setup.read_credentials(auth_setup.private_dir()).values()]
    token = auth_headers.session_token(os.environ.get("RAG_AUTH_JAR", ""))
    if token:
        secrets_known.append(token)
    result = compose("logs", "--no-color", "api")
    expect(result.returncode == 0, "docker compose logs failed")
    leaked = sum(1 for s in secrets_known if s and s in result.stdout + result.stderr)
    expect(leaked == 0, "{} harness secret(s) appear in the API log".format(leaked))
    expect("Login succeeded for 'vfy-engineer'" in result.stdout + result.stderr,
           "the API log has no login line for vfy-engineer")


STATE = "restart-state.json"


def check_restart_prepare(operator_ok):
    username, password, token = throwaway("end_user", operator_ok)
    name = "vfy-locked-" + secrets.token_hex(4)
    for _ in range(5):
        code("POST", "/auth/login", {"username": name, "password": "x" * 12})
    path = auth_setup.private_dir() / STATE
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump({"token": token, "locked": name}, handle)


def check_restart_verify(operator_ok):
    path = auth_setup.private_dir() / STATE
    state = json.loads(path.read_text())
    path.unlink()
    status, _, _ = code("GET", "/auth/me", token=state["token"])
    expect(status == 200, "a session did not survive the API restart ({})".format(status))
    status, _, _ = code("POST", "/auth/login", {"username": state["locked"], "password": "x" * 12})
    expect(status == 429, "a lockout did not survive the API restart ({})".format(status))


CHECKS = {name[len("check_"):].replace("_", "-"): fn for name, fn in globals().items() if name.startswith("check_")}


def main(argv):
    operator_ok = "--operator" in argv
    names = [a for a in argv if a != "--operator"]
    if len(names) == 2 and names[0] == "matrix" and names[1] in ROLES + ("anonymous",):
        run = lambda: check_matrix(names[1], operator_ok)
    elif len(names) == 1 and names[0] in CHECKS and names[0] != "matrix":
        run = lambda: CHECKS[names[0]](operator_ok)
    else:
        print("usage: auth_stack.py {matrix ROLE|anonymous, " + ", ".join(sorted(CHECKS)) + "} [--operator]",
              file=sys.stderr)
        return 2
    try:
        run()
    except (CheckFailed, auth_setup.SetupError) as failure:
        print("    " + str(failure), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
