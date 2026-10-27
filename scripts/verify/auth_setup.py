"""Test accounts for the verify suites (#25). Host-side, standard library, Python 3.9+.

    python3 auth_setup.py ensure [--operator]
    python3 auth_setup.py login ROLE JAR
    python3 auth_setup.py password ROLE | <a command reading it on stdin>

`ensure` makes sure the verify project has one account per role
(vfy-engineer, vfy-developer, vfy-end-user) whose password is in the
credentials file, and that none of them has a password change due. It sets the
project up (bootstrap) when it isn't yet, creates missing accounts and resets
unknown passwords, all with the operator command `python -m auth` run in the
api container: only with --operator, which lib.sh passes only when the target
is the verify project as stack.sh sets it up. Without it, a stored password that
doesn't work is a failure, never a reset.

`login` logs ROLE in and writes the session to JAR, a curl cookie jar.
`password` writes ROLE's password into a pipe, for a command that reads it on
standard input (the packaged retrieve.py); it refuses to write it to a terminal.

Secrets: the credentials file is mode 0600 in a folder of mode 0700 owned by
this user, outside the checkout ($RAG_AUTH_DIR, default
${TMPDIR:-/tmp}/rag-verify-auth-<uid>). Passwords travel only in request
bodies and in the operator command's captured output: never on a command
line, in the environment, or in anything this prints.
"""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

ROLES = {"engineer": "vfy-engineer", "developer": "vfy-developer", "end_user": "vfy-end-user"}
REPO = Path(__file__).resolve().parents[2]
COOKIE = "rag_session"


class SetupError(Exception):
    pass


def api_base():
    return os.environ.get("RAG_API", "http://localhost:8080/api").rstrip("/")


def origin(api=None):
    parts = urlsplit(api or api_base())
    return "{}://{}".format(parts.scheme, parts.netloc)


def auth_dir():
    configured = os.environ.get("RAG_AUTH_DIR")
    if configured:
        return Path(configured)
    base = (os.environ.get("TMPDIR") or "/tmp").rstrip("/") or "/tmp"
    return Path(base) / "rag-verify-auth-{}".format(os.getuid())


def private_dir():
    """The credentials folder, created 0700; refused if it is a symlink, not a
    folder, or someone else's, so nobody can plant or read the file."""
    folder = auth_dir()
    if folder.is_symlink():
        raise SetupError("{} is a symlink; refusing to use it".format(folder))
    if folder.exists() and not folder.is_dir():
        raise SetupError("{} exists and is not a folder; refusing to use it".format(folder))
    if not folder.exists():
        folder.mkdir(mode=0o700)
    if folder.stat().st_uid != os.getuid():
        raise SetupError("{} belongs to another user; refusing to use it".format(folder))
    os.chmod(folder, 0o700)
    return folder


def read_credentials(folder):
    path = folder / "credentials"
    found = {}
    if not path.exists():
        return found
    if path.is_symlink() or not path.is_file():
        raise SetupError("{} is not a regular file; refusing to use it".format(path))
    for line in path.read_text().splitlines():
        parts = line.split(" ", 2)
        if len(parts) == 3 and parts[0] in ROLES:
            found[parts[0]] = (parts[1], parts[2])
    return found


def write_credentials(folder, credentials):
    """Atomically, mode 0600 from the start."""
    fd, tmp = tempfile.mkstemp(prefix=".credentials.", dir=str(folder))
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            for role in ROLES:
                if role in credentials:
                    username, password = credentials[role]
                    handle.write("{} {} {}\n".format(role, username, password))
        os.replace(tmp, str(folder / "credentials"))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def request(method, path, body=None, token=None, api=None):
    """(status, parsed JSON or None, session token from Set-Cookie or None)."""
    api = api or api_base()
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Origin": origin(api)}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Cookie"] = "{}={}".format(COOKIE, token)
    req = urllib.request.Request(api + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            status, raw, cookies = response.status, response.read(), response.headers.get_all("Set-Cookie") or []
    except urllib.error.HTTPError as error:
        status, raw, cookies = error.code, error.read(), error.headers.get_all("Set-Cookie") or []
    try:
        parsed = json.loads(raw) if raw else None
    except ValueError:
        parsed = None
    token_out = None
    for cookie in cookies:
        name, _, rest = cookie.partition("=")
        if name.strip() == COOKIE:
            token_out = rest.split(";", 1)[0] or None
    return status, parsed, token_out


def operator(*args):
    """Run `python -m auth ARGS` in the verify project's api container.
    Returns (exit status, stdout). stdout (a generated password) is captured,
    never shown; stderr, one line naming the action, passes through."""
    result = subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-m", "auth"] + list(args),
                            cwd=str(REPO), stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                            universal_newlines=True)
    return result.returncode, result.stdout.strip()


def fresh_password():
    return secrets.token_urlsafe(24)


def settle(role, username, password):
    """Log in; replace a generated password with a fresh one. Returns the
    working password, or None when the login fails."""
    status, body, token = request("POST", "/auth/login", {"username": username, "password": password})
    if status != 200 or not token:
        return None
    if body.get("must_change_password"):
        new = fresh_password()
        status, _, _ = request("POST", "/auth/password", {"current_password": password, "new_password": new},
                               token=token)
        if status != 200:
            raise SetupError("could not replace the generated password of {} (HTTP {})".format(username, status))
        password = new
    request("POST", "/auth/logout", token=token)
    return password


def ensure(allow_operator):
    folder = private_dir()
    credentials = read_credentials(folder)
    status, body, _ = request("GET", "/auth/status")
    if status != 200 or not isinstance(body, dict):
        raise SetupError("GET /auth/status answered HTTP {}".format(status))
    if body.get("setup_required"):
        if not allow_operator:
            raise SetupError("the verify project is not set up, and this target is not the verify project")
        code, password = operator("bootstrap", ROLES["engineer"])
        if code != 0 or not password:
            raise SetupError("bootstrap failed (exit {})".format(code))
        credentials["engineer"] = (ROLES["engineer"], password)
    for role, username in ROLES.items():
        stored = credentials.get(role)
        working = settle(role, username, stored[1]) if stored and stored[0] == username else None
        if working is None:
            if not allow_operator:
                raise SetupError("the stored password of {} doesn't work, and this target is not the verify "
                                 "project, so it isn't reset".format(username))
            code, password = operator("create-user", username, role)
            if code == 1:
                code, password = operator("reset-password", username)
            if code != 0 or not password:
                raise SetupError("could not create or reset {} (exit {})".format(username, code))
            working = settle(role, username, password)
            if working is None:
                raise SetupError("{} can't log in with its new password".format(username))
        credentials[role] = (username, working)
        write_credentials(folder, credentials)


def write_jar(jar, token, api=None):
    """A curl cookie jar holding the session, mode 0600."""
    host = urlsplit(api or api_base()).hostname
    fd = os.open(str(jar), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write("# Netscape HTTP Cookie File\n")
        handle.write("#HttpOnly_{}\tFALSE\t/\tFALSE\t0\t{}\t{}\n".format(host, COOKIE, token))


def login(role, jar):
    if role not in ROLES:
        raise SetupError("unknown role {!r}".format(role))
    folder = private_dir()
    stored = read_credentials(folder).get(role)
    if not stored:
        raise SetupError("no stored password for {}; run ensure first".format(ROLES[role]))
    status, body, token = request("POST", "/auth/login", {"username": stored[0], "password": stored[1]})
    if status != 200 or not token or body.get("must_change_password"):
        raise SetupError("{} could not log in (HTTP {})".format(stored[0], status))
    write_jar(Path(jar), token)


def password_to_pipe(role):
    if role not in ROLES:
        raise SetupError("unknown role {!r}".format(role))
    if os.isatty(1):
        raise SetupError("refusing to print a password to a terminal; pipe it into the command that needs it")
    stored = read_credentials(private_dir()).get(role)
    if not stored:
        raise SetupError("no stored password for {}; run ensure first".format(ROLES[role]))
    sys.stdout.write(stored[1] + "\n")


def main(argv):
    try:
        if len(argv) >= 1 and argv[0] == "ensure" and argv[1:] in ([], ["--operator"]):
            ensure(argv[1:] == ["--operator"])
        elif len(argv) == 3 and argv[0] == "login":
            login(argv[1], argv[2])
        elif len(argv) == 2 and argv[0] == "password":
            password_to_pipe(argv[1])
        else:
            print(__doc__.split("\n\n")[1], file=sys.stderr)
            return 2
    except SetupError as error:
        print("auth_setup: {}".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
