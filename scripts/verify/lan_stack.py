"""LAN mode on the verify project (#26). Host-side, standard library, Python 3.9+.

    python3 lan_stack.py https|tls|bindings|cookie|origin|refusals|rate-limit|address

Run by 18_lan.sh in a `stack.sh run --lan`, which brings the verify project up
in LAN mode with TLS on 127.0.0.1:<verify port> only, copies the certificate
the tls-init step generated to RAG_VERIFY_LAN_CERT and points RAG_API at
https://localhost:<port>/api. Each subcommand is one check and exits 0 or 1,
printing what failed:

  https     /api/health over TLS, verified against that certificate and the
            name localhost; the certificate names exactly localhost and the
            harness's test names and is valid for 825 days
  tls       TLS 1.2 and 1.3 connect, TLS 1.1 is refused, ALPN picks HTTP/2
  bindings  the project's only published port is the proxy's
            127.0.0.1:<port>->443/tcp, and plain HTTP to it gets no API answer
  cookie    a login over TLS sets the session cookie Secure, HttpOnly and
            SameSite=Strict
  origin    a cookie-authenticated change from another origin is refused 403
            CROSS_ORIGIN_REFUSED, and from https://localhost:<port> accepted
  refusals  a one-off api container of the LAN configuration (no ports, the
            running project untouched) refuses to start, with one line, when
            AUTH_COOKIE_SECURE is false and when its account store is empty
  rate-limit
            logins for unknown usernames, sent without waiting for a slot,
            until nginx refuses one with 429 LOGIN_RATE_LIMITED (#577)
  address   prints the client address nginx recorded for these requests
            (always exits 0; information for the README)

The harness Engineer's password comes from auth_setup.py's credentials file and
travels only in request bodies; nothing here prints it.
"""
import http.client
import json
import os
import re
import secrets
import socket
import ssl
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import auth_setup  # noqa: E402

NAMES = {("DNS", "localhost"), ("DNS", "vfy-lan.invalid"), ("IP Address", "192.0.2.10")}
VALID_S = 825 * 86400
REFUSED = "LAN mode refused:"


def port():
    return int(os.environ["RAG_VERIFY_PORT"])


def cert():
    return os.environ["RAG_VERIFY_LAN_CERT"]


def origin():
    return "https://localhost:{}".format(port())


def context():
    return ssl.create_default_context(cafile=cert())


def request(method, path, body=None, headers=None):
    """(status, parsed JSON or None, Set-Cookie values) over verified TLS."""
    conn = http.client.HTTPSConnection("localhost", port(), context=context(), timeout=60)
    try:
        data = json.dumps(body).encode() if body is not None else None
        sent = dict(headers or {})
        if data is not None:
            sent["Content-Type"] = "application/json"
        conn.request(method, "/api" + path, body=data, headers=sent)
        response = conn.getresponse()
        raw = response.read()
        cookies = [value for name, value in response.getheaders() if name.lower() == "set-cookie"]
        try:
            parsed = json.loads(raw) if raw else None
        except ValueError:
            parsed = None
        return response.status, parsed, cookies
    finally:
        conn.close()


def login():
    """(session token, Set-Cookie value) for the harness Engineer."""
    stored = auth_setup.read_credentials(auth_setup.private_dir()).get("engineer")
    if not stored:
        raise AssertionError("no stored credentials for the harness Engineer")
    # nginx's login rate limit (#577): wait for a slot as auth_setup.paced does.
    status, body, cookies = auth_setup.paced(auth_setup.LOGIN_PATH, lambda: request(
        "POST", "/auth/login", {"username": stored[0], "password": stored[1]}, {"Origin": origin()}))
    session = [c for c in cookies if c.split("=", 1)[0].strip() == auth_setup.COOKIE]
    if status != 200 or len(session) != 1:
        raise AssertionError("login answered HTTP {} with {} session cookie(s)".format(status, len(session)))
    token = session[0].split("=", 1)[1].split(";", 1)[0]
    return token, session[0]


def check_https():
    conn = http.client.HTTPSConnection("localhost", port(), context=context(), timeout=60)
    try:
        conn.request("GET", "/api/health")
        response = conn.getresponse()
        response.read()
        peer = conn.sock.getpeercert()
    finally:
        conn.close()
    assert response.status == 200, "GET /api/health answered {}".format(response.status)
    names = set(peer.get("subjectAltName", ()))
    assert names == NAMES, "the certificate names {}, not {}".format(sorted(names), sorted(NAMES))
    valid = ssl.cert_time_to_seconds(peer["notAfter"]) - ssl.cert_time_to_seconds(peer["notBefore"])
    assert valid == VALID_S, "the certificate is valid for {} s, not {} (825 days)".format(valid, VALID_S)


def handshake(ctx, alpn=None):
    with socket.create_connection(("localhost", port()), timeout=30) as raw:
        with ctx.wrap_socket(raw, server_hostname="localhost") as tls:
            return tls.version(), tls.selected_alpn_protocol()


def check_tls():
    for version, name in ((ssl.TLSVersion.TLSv1_2, "TLSv1.2"), (ssl.TLSVersion.TLSv1_3, "TLSv1.3")):
        ctx = context()
        ctx.minimum_version = ctx.maximum_version = version
        got, _ = handshake(ctx)
        assert got == name, "asked for {}, got {}".format(name, got)
    old = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    old.load_verify_locations(cert())
    # Security level 0, so this client really offers TLS 1.1 and only the
    # server can refuse it.
    old.set_ciphers("DEFAULT:@SECLEVEL=0")
    old.minimum_version = old.maximum_version = ssl.TLSVersion.TLSv1_1
    try:
        got, _ = handshake(old)
    except (ssl.SSLError, OSError):
        got = None
    assert got is None, "a TLS 1.1 connection was accepted ({})".format(got)
    ctx = context()
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    _, alpn = handshake(ctx)
    assert alpn == "h2", "ALPN picked {!r}, not h2".format(alpn)


def docker(*args):
    result = subprocess.run(["docker"] + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, universal_newlines=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError("docker {} failed: {}".format(" ".join(args[:2]), result.stderr.strip()))
    return result.stdout


def check_bindings():
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    assert project == "rag-verify", "COMPOSE_PROJECT_NAME is {!r}, not rag-verify".format(project)
    ids = docker("ps", "-q", "--filter", "label=com.docker.compose.project=rag-verify").split()
    bindings = []
    for container in json.loads(docker("inspect", *ids)) if ids else []:
        service = container["Config"]["Labels"].get("com.docker.compose.service")
        for target, published in (container["NetworkSettings"]["Ports"] or {}).items():
            for binding in published or []:
                bindings.append((service, target, binding.get("HostIp"), binding.get("HostPort")))
    want = [("proxy", "443/tcp", "127.0.0.1", str(port()))]
    assert bindings == want, "published bindings are {}, not {}".format(bindings, want)
    with socket.create_connection(("127.0.0.1", port()), timeout=30) as raw:
        raw.sendall(b"GET /api/health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
        answer = b""
        while len(answer) < 65536:
            chunk = raw.recv(4096)
            if not chunk:
                break
            answer += chunk
    first = answer.split(b"\r\n", 1)[0]
    assert not re.match(rb"HTTP/1\.[01] 200", first), "plain HTTP got {!r}".format(first)
    assert b'"status"' not in answer, "plain HTTP got an API answer"


def check_cookie():
    token, header = login()
    try:
        attributes = {part.strip().lower() for part in header.split(";")[1:]}
        for wanted in ("secure", "httponly", "samesite=strict"):
            assert wanted in attributes, "the session cookie lacks {} ({})".format(
                wanted, sorted(a for a in attributes if not a.startswith("expires")))
    finally:
        request("POST", "/auth/logout", headers={"Cookie": "{}={}".format(auth_setup.COOKIE, token),
                                                 "Origin": origin()})


def check_origin():
    token, _ = login()
    cookie = "{}={}".format(auth_setup.COOKIE, token)
    status, body, _ = request("POST", "/auth/logout", headers={"Cookie": cookie, "Origin": "https://vfy-lan.invalid"})
    code = ((body or {}).get("error") or {}).get("code")
    assert (status, code) == (403, "CROSS_ORIGIN_REFUSED"), "another origin got {} {}".format(status, code)
    status, _, _ = request("GET", "/auth/me", headers={"Cookie": cookie})
    assert status == 200, "the session didn't survive the refused change ({})".format(status)
    status, _, _ = request("POST", "/auth/logout", headers={"Cookie": cookie, "Origin": origin()})
    assert status == 200, "the same-origin change answered {}".format(status)
    status, _, _ = request("GET", "/auth/me", headers={"Cookie": cookie})
    assert status == 401, "the session still works after logout ({})".format(status)


def one_off(*env):
    """A one-off api container of the LAN configuration: (exit status, its output lines)."""
    args = ["docker", "compose", "run", "--rm", "--no-deps", "-T"]
    for item in env:
        args += ["-e", item]
    result = subprocess.run(args + ["api"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, universal_newlines=True, timeout=300)
    return result.returncode, result.stdout.splitlines()


def check_refusals():
    project = os.environ.get("COMPOSE_PROJECT_NAME", "")
    assert project == "rag-verify", "COMPOSE_PROJECT_NAME is {!r}, not rag-verify".format(project)
    cases = [(("AUTH_COOKIE_SECURE=false",), "AUTH_COOKIE_SECURE"),
             (("AUTH_DIR=/tmp/vfy-lan-empty-" + secrets.token_hex(4),), "no enabled AI Engineer")]
    for env, cause in cases:
        status, lines = one_off(*env)
        refused = [line for line in lines if REFUSED in line]
        assert status != 0, "{}: the API started (exit 0)".format(env[0])
        assert len(refused) == 1 and cause in refused[0], "{}: refusal lines {}".format(env[0], refused or lines[-5:])
        tracebacks = [line for line in lines if line.startswith("Traceback")]
        assert not tracebacks, "{}: the refusal printed a traceback".format(env[0])
    status, _, _ = request("GET", "/health")
    assert status == 200, "the running API isn't healthy after the one-off containers ({})".format(status)


def check_rate_limit():
    """#577: nginx's login rate limit on the LAN project. The attempts are sent
    without waiting for a slot (no auth_setup.paced) until one is refused."""
    seen = []
    for n in range(12):
        name = "vfy-lan-rate-{}-{}".format(n, secrets.token_hex(3))
        status, body, _ = request("POST", "/auth/login", {"username": name, "password": "x" * 12}, {"Origin": origin()})
        code = ((body or {}).get("error") or {}).get("code")
        seen.append("{} {}".format(status, code))
        if status == 429:
            assert code == "LOGIN_RATE_LIMITED", "the refusal was {} {}, not nginx's".format(status, code)
            return
    raise AssertionError("12 logins in a row were all let through: {}".format(", ".join(seen)))


def check_address():
    logs = subprocess.run(["docker", "compose", "logs", "--no-color", "--tail", "500", "proxy"],
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          universal_newlines=True, timeout=120).stdout
    seen = re.findall(r"\|\s+(\S+) - \S+ \[[^\]]*\] \"GET /api/health ", logs)
    print("    nginx recorded the client address {} for this host's loopback requests (in X-Real-IP for the "
          "API)".format(seen[-1] if seen else "(none found)"))


CHECKS = {"https": check_https, "tls": check_tls, "bindings": check_bindings, "cookie": check_cookie,
          "origin": check_origin, "refusals": check_refusals, "rate-limit": check_rate_limit,
          "address": check_address}


def main(argv):
    if len(argv) != 1 or argv[0] not in CHECKS:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    try:
        CHECKS[argv[0]]()
    except (AssertionError, OSError, ssl.SSLError, subprocess.SubprocessError, KeyError, auth_setup.SetupError) as error:
        print("    lan_stack {}: {}".format(argv[0], error or type(error).__name__), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
