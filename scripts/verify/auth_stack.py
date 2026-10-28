"""Authentication checks on the verify project (#25), one per subcommand. Host-side,
standard library, Python 3.9+. Run by 17_auth.sh, which turns each exit status
into a check:

    python3 auth_stack.py CHECK [--operator]

Exit 0 when the check holds, 1 when it doesn't (the reason on stderr). Checks
that create throwaway accounts (vfy-t-<random>), service principals
(vfy-s-<random>) or age a session need the operator command, so they need
--operator, which 17_auth.sh passes only on the verify project as stack.sh sets
it up. No password, session token or API key is printed; keys are held in
memory only.
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
    # Account administration (PR 2 of #25): an allowed request lists, or is a
    # 422 or a 404 for an account that doesn't exist; it changes nothing.
    ("GET", "/auth/users", None, "engineer"),
    ("POST", "/auth/users", {}, "engineer"),
    ("POST", "/auth/users/vfy-no-such-user/reset-password", None, "engineer"),
    ("POST", "/auth/users/vfy-no-such-user/role", {}, "engineer"),
    ("POST", "/auth/users/vfy-no-such-user/disable", None, "engineer"),
    ("POST", "/auth/users/vfy-no-such-user/enable", None, "engineer"),
    # Service principals and keys (PR 3 of #25): likewise a list, a 422 or a 404.
    ("GET", "/auth/service-principals", None, "engineer"),
    ("POST", "/auth/service-principals", {}, "engineer"),
    ("POST", "/auth/service-principals/vfy-no-such-svc/role", {}, "engineer"),
    ("POST", "/auth/service-principals/vfy-no-such-svc/disable", None, "engineer"),
    ("POST", "/auth/service-principals/vfy-no-such-svc/enable", None, "engineer"),
    ("POST", "/auth/service-principals/vfy-no-such-svc/keys", {}, "engineer"),
    ("POST", "/auth/service-principals/vfy-no-such-svc/keys/nosuchkey123/revoke", None, "engineer"),
)
# Routes an API key can't use whatever its role (S5 of PR 3): every account,
# principal and key route, logout and the password change.
KEY_REFUSED = ("/auth/users", "/auth/service-principals", "/auth/logout", "/auth/password")
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


def bearer(key):
    """The headers of a service sending its key: no Origin, no cookie."""
    return {"Authorization": "Bearer " + key}


def new_principal(admin, role):
    """A throwaway vfy-s-<random> principal of ROLE with one key, through the
    API as the harness Engineer (session token `admin`). Returns (name, key id, key)."""
    name = "vfy-s-" + secrets.token_hex(4)
    status, raw, _ = code("POST", "/auth/service-principals", {"name": name, "role": role}, token=admin)
    expect(status == 201, "creating principal {} answered {} {}".format(name, status, error_code(raw)))
    key_id, key = issue(admin, name)
    return name, key_id, key


def issue(admin, name, body=None):
    status, raw, headers = code("POST", "/auth/service-principals/{}/keys".format(name), body or {}, token=admin)
    expect(status == 201, "issuing a key for {} answered {} {}".format(name, status, error_code(raw)))
    expect(headers.get("Cache-Control") == "no-store", "the issued key's answer may be cached")
    answer = json.loads(raw)
    return answer["key"]["id"], answer["secret"]


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


def check_key_matrix(role, operator_ok):
    """The matrix with an API key of ROLE, sent without Origin (S8.2 of PR 3)."""
    expect(operator_ok, "creates a service principal, so it runs only on the verify project")
    admin = role_token("engineer")
    name, key_id, key = new_principal(admin, role)
    wrong = []
    try:
        for method, path, body, need in MATRIX:
            status, raw, _ = code(method, path, body, headers=bearer(key))
            if path.startswith(KEY_REFUSED):
                ok = status == 403 and error_code(raw) == "KEY_NOT_ALLOWED"
            elif ROLES.index(role) < ROLES.index(need):
                ok = status == 403 and error_code(raw) == "FORBIDDEN"
            else:
                ok = status not in (401, 403)
            if not ok:
                wrong.append("{} {} -> {} {}".format(method, path, status, error_code(raw)))
        for method, path, body in SESSION_ROUTES:
            status, raw, _ = code(method, path, body, headers=bearer(key))
            if path == "/auth/me":
                me = json.loads(raw) if status == 200 else {}
                if (me.get("username"), me.get("role"), me.get("kind")) != (name, role, "service"):
                    wrong.append("GET /auth/me -> {} {}".format(status, me))
            elif not (status == 403 and error_code(raw) == "KEY_NOT_ALLOWED"):
                wrong.append("{} {} -> {} {}".format(method, path, status, error_code(raw)))
    finally:
        code("POST", "/auth/service-principals/{}/keys/{}/revoke".format(name, key_id), token=admin)
        code("POST", "/auth/service-principals/{}/disable".format(name), token=admin)
        request("POST", "/auth/logout", token=admin)
    expect(not wrong, "; ".join(wrong))


def check_service_keys(operator_ok):
    """A service principal's keys through their life (S8.2 of PR 3), as the
    harness Engineer through the API."""
    expect(operator_ok, "creates a service principal, so it runs only on the verify project")
    admin = role_token("engineer")
    me = auth_setup.read_credentials(auth_setup.private_dir())["engineer"][0]
    name, first_id, first = new_principal(admin, "developer")
    issued = [first]

    def works(key, path="/packages"):
        return code("GET", path, headers=bearer(key))[0]

    def invalid(key_or_header, why):
        status, raw, _ = code("GET", "/collections", headers={"Authorization": key_or_header})
        expect(status == 401 and error_code(raw) == "KEY_INVALID", "{}: answered {} {}".format(
            why, status, error_code(raw)))

    def admin_post(path, body=None):
        status, raw, _ = code("POST", "/auth/service-principals/{}{}".format(name, path), body, token=admin)
        expect(status == 200, "POST {} answered {} {}".format(path, status, error_code(raw)))
        return json.loads(raw)["principal"]

    # The principal can't log in (Q21).
    status, raw, _ = code("POST", "/auth/login", {"username": name, "password": "x" * 12})
    expect(status == 401 and error_code(raw) == "LOGIN_FAILED", "the principal's login answered {}".format(status))
    # A change without Origin is accepted (Q26): index_type 7 is invalid, so it's a 422 and creates nothing.
    status, raw, _ = code("POST", "/collections", {"name": "VfyKey" + secrets.token_hex(3), "index_type": 7},
                          headers=bearer(first))
    expect(status == 422, "a key's change without Origin answered {} {}".format(status, error_code(raw)))
    # Rotation: a second key; both work (Q24).
    second_id, second = issue(admin, name, {"label": "rotated", "days": 1})
    issued.append(second)
    expect(works(first) == 200 and works(second) == 200, "after rotation both keys must work")
    # A role change applies to the keys at once.
    admin_post("/role", {"role": "end_user"})
    status, raw, _ = code("GET", "/packages", headers=bearer(first))
    expect(status == 403 and error_code(raw) == "FORBIDDEN", "after the demotion the key answered {}".format(status))
    admin_post("/role", {"role": "developer"})
    # Disabling the principal stops its keys; enabling restores them.
    admin_post("/disable")
    invalid("Bearer " + first, "a disabled principal's key")
    admin_post("/enable")
    expect(works(first) == 200, "enabling the principal did not restore its key")
    # Revocation stops one key; the other keeps working.
    principal = admin_post("/keys/{}/revoke".format(first_id))
    invalid("Bearer " + first, "a revoked key")
    expect(works(second) == 200, "revoking one key stopped the other")
    expect([k["status"] for k in principal["keys"] if k["id"] == first_id] == ["revoked"], "the revoked key isn't listed as revoked")
    # Expiry: the second key, aged past its end in the verify project's store.
    script = ("import sqlite3,sys,time\nfrom auth import store\n"
              "c=sqlite3.connect(store.db_path());c.execute('UPDATE api_keys SET expires_at=? WHERE id=?',"
              "(time.time()-1,sys.argv[1]));c.commit();print(c.total_changes)\n")
    result = compose("exec", "-T", "api", "python", "-c", script, second_id)
    expect(result.returncode == 0 and result.stdout.strip() == "1", "could not age the key: {}".format(
        result.stderr.strip()[-200:]))
    invalid("Bearer " + second, "an expired key")
    # Unknown, malformed and non-Bearer keys; a bad key never falls back to a valid cookie.
    third_id, third = issue(admin, name)
    issued.append(third)
    invalid("Bearer rag_" + secrets.token_hex(6) + "_" + secrets.token_urlsafe(32), "an unknown key")
    invalid("Bearer " + third[:-1], "a malformed key")
    invalid("Basic " + third, "a Basic header")
    status, raw, _ = code("GET", "/metrics/latency", token=admin, headers={"Authorization": "Bearer nonsense"})
    expect(status == 401 and error_code(raw) == "KEY_INVALID", "a bad key with a valid cookie answered {} {}".format(
        status, error_code(raw)))
    # A key in the query string is no credential. It reaches the access log there,
    # which is why keys are never accepted from URLs: revoked at once, and left
    # out of the log check below.
    query_id, query_key = issue(admin, name)
    status, raw, _ = code("GET", "/collections?api_key=" + query_key, headers={})
    expect(status == 401 and error_code(raw) == "AUTH_REQUIRED", "a key in the query string answered {} {}".format(
        status, error_code(raw)))
    admin_post("/keys/{}/revoke".format(query_id))
    # Keys appear in no listing and not in the API log, which has the admin lines.
    status, raw, _ = code("GET", "/auth/service-principals", token=admin)
    expect(status == 200 and not [k for k in issued + [query_key] if k in raw.decode()], "a listing holds a key")
    admin_post("/keys/{}/revoke".format(third_id))
    admin_post("/disable")
    result = compose("logs", "--no-color", "api")
    log = result.stdout + result.stderr
    expect(result.returncode == 0, "docker compose logs failed")
    expect(not [k for k in issued if k in log], "an API key appears in the API log")
    for line in ("created service principal '{}' (developer)".format(name),
                 "issued key {} for service principal '{}'".format(second_id, name),
                 "revoked key {} of service principal '{}'".format(first_id, name),
                 "disabled service principal '{}'".format(name)):
        expect("Admin '{}' {}".format(me, line) in log, "the API log has no line \"Admin ... {}\"".format(line))
    # The operator command marks the principal in its list and won't reset its password.
    rc, listed = auth_setup.operator("list")
    expect(rc == 0 and "{}\tdeveloper\tdisabled\tservice account\t".format(name) in listed,
           "the operator's list doesn't mark the principal")
    rc, out = auth_setup.operator("reset-password", name)
    expect(rc == 1 and not out, "reset-password of a principal exited {}".format(rc))
    request("POST", "/auth/logout", token=admin)


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
    expect(status == 200 and body == {"username": "vfy-developer", "role": "developer", "must_change_password": False,
                                      "kind": "user"},
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


def check_admin_accounts(operator_ok):
    """The Engineer's account administration through the API (PR 2 of #25), on a
    throwaway vfy-t-<random> account."""
    expect(operator_ok, "creates accounts, so it runs only on the verify project")
    admin = role_token("engineer")
    username = "vfy-t-" + secrets.token_hex(4)
    generated = []

    def call(path, body=None, method="POST"):
        status, raw, headers = code(method, "/auth/users" + path, body, token=admin)
        return status, (json.loads(raw) if raw else {}), headers

    status, body, headers = call("", {"username": username.upper(), "role": "developer"})
    expect(status == 201 and body["user"]["username"] == username and body["user"]["must_change_password"] is True,
           "create answered {} {}".format(status, error_code(json.dumps(body).encode())))
    expect(headers.get("Cache-Control") == "no-store", "the created password's answer may be cached")
    generated.append(body["password"])
    status, listed, _ = call("", method="GET")
    expect(status == 200 and username in [u["username"] for u in listed["users"]], "the new account isn't listed")
    expect(generated[0] not in json.dumps(listed), "the list holds the generated password")
    status, body, token = request("POST", "/auth/login", {"username": username, "password": generated[0]})
    expect(status == 200 and body.get("must_change_password") is True, "the generated password must be changed first")
    new = secrets.token_urlsafe(18)
    status, _, _ = code("POST", "/auth/password", {"current_password": generated[0], "new_password": new}, token=token)
    expect(status == 200, "the forced change answered {}".format(status))

    status, body, _ = call("/{}/role".format(username), {"role": "end_user"})
    expect(status == 200 and body["user"]["role"] == "end_user", "the role change answered {}".format(status))
    status, raw, _ = code("GET", "/packages", token=token)
    expect(status == 403 and error_code(raw) == "FORBIDDEN", "the demoted session answered {}".format(status))

    status, body, _ = call("/{}/reset-password".format(username))
    expect(status == 200 and body["user"]["must_change_password"] is True, "the reset answered {}".format(status))
    generated.append(body["password"])
    expect(code("GET", "/auth/me", token=token)[0] == 401, "a reset left the user's session working")
    status, _, token = request("POST", "/auth/login", {"username": username, "password": generated[1]})
    expect(status == 200 and token, "the reset password doesn't log in ({})".format(status))

    status, body, _ = call("/{}/disable".format(username))
    expect(status == 200 and body["user"]["enabled"] is False, "disable answered {}".format(status))
    expect(code("GET", "/auth/me", token=token)[0] == 401, "a disabled user's session still works")
    status, raw, _ = code("POST", "/auth/login", {"username": username, "password": generated[1]})
    expect(status == 401 and error_code(raw) == "LOGIN_FAILED", "a disabled user can log in ({})".format(status))
    status, body, _ = call("/{}/enable".format(username))
    expect(status == 200 and body["user"]["enabled"] is True, "enable answered {}".format(status))
    status, _, _ = code("POST", "/auth/login", {"username": username, "password": generated[1]})
    expect(status == 200, "enable did not restore the login ({})".format(status))

    me = auth_setup.read_credentials(auth_setup.private_dir())["engineer"][0]
    status, body, _ = call("/{}/reset-password".format(me))
    expect(status == 409 and body["error"]["code"] == "SELF_RESET_REFUSED",
           "the Engineer's own reset answered {}".format(status))
    expect(code("GET", "/auth/me", token=admin)[0] == 200, "the refused own reset ended the session")
    request("POST", "/auth/logout", token=admin)

    result = compose("logs", "--no-color", "api")
    log = result.stdout + result.stderr
    expect(result.returncode == 0, "docker compose logs failed")
    expect(not [p for p in generated if p in log], "a generated password appears in the API log")
    for line in ("created developer '{}'".format(username), "reset the password of '{}'".format(username),
                 "disabled '{}'".format(username), "enabled '{}'".format(username)):
        expect("Admin '{}' {}".format(me, line) in log, "the API log has no line \"Admin ... {}\"".format(line))


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
    elif len(names) == 2 and names[0] == "key-matrix" and names[1] in ROLES:
        run = lambda: check_key_matrix(names[1], operator_ok)
    elif len(names) == 1 and names[0] in CHECKS and names[0] not in ("matrix", "key-matrix"):
        run = lambda: CHECKS[names[0]](operator_ok)
    else:
        print("usage: auth_stack.py {matrix ROLE|anonymous, key-matrix ROLE, "
              + ", ".join(sorted(set(CHECKS) - {"matrix", "key-matrix"})) + "} [--operator]",
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
