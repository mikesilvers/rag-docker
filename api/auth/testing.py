"""A real account and session for in-process tests (#25).

    from auth import testing
    client = TestClient(app, headers=testing.headers("developer"))

Points settings.auth_dir at a temporary folder for the rest of the process
(removed at exit), creates an account of the role with no password change due,
and returns the headers a same-origin browser would send (one account per
role, a new session per call): the session cookie
and an Origin matching base_url. Requests still go through the middleware and
the store; nothing is bypassed. Never used by the API itself.
"""
from __future__ import annotations

import atexit
import shutil
import tempfile

from config import settings

from . import passwords, store

_folder: str | None = None
_count = 0


def use_temporary_store() -> str:
    """Point the store at this process's temporary folder; returns it."""
    global _folder
    if _folder is None:
        _folder = tempfile.mkdtemp(prefix="rag-auth-test-")
        atexit.register(shutil.rmtree, _folder, True)
    settings.auth_dir = _folder
    return _folder


def account(role: str = "engineer") -> tuple[store.User, str]:
    """A new enabled account of `role` in the temporary store, and its password."""
    global _count
    use_temporary_store()
    if not store.is_set_up():
        store.create_user("test-bootstrap", "engineer", passwords.generate_password(), bootstrap=True,
                          must_change_password=False)
    _count += 1
    username = f"test-{role.replace('_', '-')}-{_count}"
    password = passwords.generate_password()
    store.create_user(username, role, password, must_change_password=False)
    return store.get_user(username), password


_accounts: dict[str, store.User] = {}


def headers(role: str = "engineer", base_url: str = "http://testserver") -> dict[str, str]:
    """Cookie and Origin headers for a fresh session of this process's `role` account."""
    user = _accounts.get(role)
    if user is None or store.get_user(user.username) is None:
        user, _ = account(role)
        _accounts[role] = user
    token = store.create_session(user.id)
    return {"Cookie": f"{store.COOKIE}={token}", "Origin": base_url.rstrip("/")}
