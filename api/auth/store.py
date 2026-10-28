"""Accounts, service principals and their keys, sessions and the login
throttle, in one SQLite file (#25).

The file is `auth.db` in AUTH_DIR, the auth_data volume, so all three survive an
API restart. The API and the operator command (`python -m auth`) open it from
separate processes; SQLite's locking keeps them consistent, and the
last-engineer rule is checked inside a write transaction so two commands
can't both pass it.

Every function opens its own short connection. Nothing here is cached except
the fact that the system is set up, which can't become false again: no
command deletes an account.

A service principal (PR 3) is a `users` row with kind 'service': it shares the
username rule and name pool with people, has no usable password and no
session, and authenticates only with its API keys (`api_keys`, keys.py).
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from config import settings

from . import keys, passwords
from .policy import ROLES

COOKIE = "rag_session"
IDLE_S = 12 * 3600               # a session ends after 12 hours without a request
ABSOLUTE_S = 7 * 24 * 3600       # and 7 days after login, whichever is first
_TOUCH_S = 60                    # last_seen is written at most once a minute
THROTTLE_FAILURES = 5            # failed logins per (username, address) ...
THROTTLE_WINDOW_S = 15 * 60      # ... within 15 minutes ...
LOCKOUT_S = 15 * 60              # ... lock that pair for 15 minutes
KEY_DAYS_DEFAULT = 90            # a key's lifetime when none is given (Q23) ...
KEY_DAYS_MAX = 365               # ... and the longest allowed
_DAY_S = 86400
NO_PASSWORD = "!"                # a principal's password hash: never verifies
SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS login_failures (
    username TEXT NOT NULL,
    address TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS login_failures_pair ON login_failures(username, address);
CREATE TABLE IF NOT EXISTS login_lockouts (
    username TEXT NOT NULL,
    address TEXT NOT NULL,
    until REAL NOT NULL,
    PRIMARY KEY (username, address)
);
CREATE TABLE IF NOT EXISTS api_keys (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    key_hash TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    revoked_at REAL,
    last_used REAL
);
CREATE INDEX IF NOT EXISTS api_keys_user ON api_keys(user_id);
"""

_ready: set[str] = set()
_set_up: set[str] = set()


class Refused(Exception):
    """An account change the rules don't allow; the message says why, in one line.
    `code` names the reason for the API: USER_EXISTS, USER_NOT_FOUND,
    LAST_ENGINEER, PRINCIPAL_EXISTS, PRINCIPAL_NOT_FOUND, KEY_NOT_FOUND,
    NOT_A_USER, or REFUSED for the bootstrap rules."""

    def __init__(self, message: str, code: str = "REFUSED"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    role: str
    must_change_password: bool
    token_hash: str              # the session's; "" for a key
    kind: str = "user"           # "service" when authenticated by a key
    key_id: str | None = None


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    password_hash: str
    enabled: bool
    must_change_password: bool
    created_at: float
    kind: str = "user"


@dataclass(frozen=True)
class Key:
    id: str
    label: str
    created_at: float
    expires_at: float
    revoked_at: float | None
    last_used: float | None

    def status(self, now: float | None = None) -> str:
        if self.revoked_at is not None:
            return "revoked"
        return "expired" if (now if now is not None else _now()) >= self.expires_at else "active"


@dataclass(frozen=True)
class ServicePrincipal:
    id: int
    name: str
    role: str
    enabled: bool
    created_at: float
    keys: tuple[Key, ...]


def _now() -> float:
    return time.time()


def db_path() -> Path:
    return Path(settings.auth_dir) / "auth.db"


@contextmanager
def _connect(write: bool = False):
    path = db_path()
    key = str(path)
    if key not in _ready:
        # Folder 0700 and file 0600: the file holds password hashes and session
        # verifiers, and nobody but the API's own user needs to read it.
        old = os.umask(0o077)
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            conn = sqlite3.connect(path, timeout=10)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.executescript(_SCHEMA)
                _migrate(conn)
            finally:
                conn.close()
        finally:
            os.umask(old)
        _ready.add(key)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def _migrate(conn) -> None:
    """Version 2 (PR 3): users.kind. In a write transaction, so the API and the
    operator command can't both add the column."""
    conn.execute("BEGIN IMMEDIATE")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    if "kind" not in columns:
        conn.execute("ALTER TABLE users ADD COLUMN kind TEXT NOT NULL DEFAULT 'user'")
    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def _user(row) -> User | None:
    if row is None:
        return None
    return User(row["id"], row["username"], row["role"], row["password_hash"],
                bool(row["enabled"]), bool(row["must_change_password"]), row["created_at"], row["kind"])


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ── setup ────────────────────────────────────────────────────────────────────

def is_set_up() -> bool:
    """True once any account exists; until then only public routes answer."""
    key = str(db_path())
    if key in _set_up:
        return True
    with _connect() as conn:
        found = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
    if found:
        _set_up.add(key)
    return found


# ── accounts (the operator command and the admin routes) ─────────────────────

def get_user(username: str) -> User | None:
    with _connect() as conn:
        return _user(conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())


def list_users(kind: str | None = "user") -> list[User]:
    """Human accounts by default; kind=None lists principals too (the operator's `list`)."""
    with _connect() as conn:
        if kind is None:
            rows = conn.execute("SELECT * FROM users ORDER BY username")
        else:
            rows = conn.execute("SELECT * FROM users WHERE kind = ? ORDER BY username", (kind,))
        return [_user(row) for row in rows]


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; use one of {', '.join(ROLES)}")


def create_user(username: str, role: str, password: str, *, bootstrap: bool = False,
                must_change_password: bool = True) -> User:
    """Create an account. `bootstrap` only when none exists, otherwise only after one does."""
    _check_role(role)
    password_hash = passwords.hash_password(password)
    now = _now()
    with _connect(write=True) as conn:
        exists = conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None
        if bootstrap and exists:
            raise Refused("the system is already set up; use create-user or reset-password")
        if not bootstrap and not exists:
            raise Refused("the system is not set up yet; run bootstrap first")
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            # One name pool for people and service principals (S3).
            raise Refused(f"the name {username!r} is already taken", "USER_EXISTS")
        conn.execute("INSERT INTO users (username, role, password_hash, enabled, must_change_password, "
                     "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
                     (username, role, password_hash, int(must_change_password), now, now))
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _user(row)


def _existing(conn, username: str, kind: str | None = None):
    """The row of `username`; with `kind`, only an account of that kind counts."""
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is None or (kind is not None and row["kind"] != kind):
        if kind == "service":
            raise Refused(f"there is no service principal {username!r}", "PRINCIPAL_NOT_FOUND")
        raise Refused(f"there is no user {username!r}", "USER_NOT_FOUND")
    return row


def _is_last_engineer(conn, row) -> bool:
    # Only people count: a service principal can't log in to administer (S4).
    if row["role"] != "engineer" or not row["enabled"] or row["kind"] != "user":
        return False
    others = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'engineer' AND enabled = 1 AND kind = 'user' "
                          "AND id != ?", (row["id"],)).fetchone()[0]
    return others == 0


def reset_password(username: str, password: str, kind: str | None = None) -> None:
    """A new password the user must change at next login; every session of theirs ends.
    A service principal has no password, so it is refused."""
    password_hash = passwords.hash_password(password)
    with _connect(write=True) as conn:
        row = _existing(conn, username, kind)
        if row["kind"] != "user":
            raise Refused(f"{username!r} is a service principal; it has no password", "NOT_A_USER")
        conn.execute("UPDATE users SET password_hash = ?, must_change_password = 1, updated_at = ? "
                     "WHERE id = ?", (password_hash, _now(), row["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))


def set_role(username: str, role: str, kind: str | None = None) -> None:
    """Sessions and keys stay; the next request of each one runs with the new role."""
    _check_role(role)
    with _connect(write=True) as conn:
        row = _existing(conn, username, kind)
        if role != "engineer" and _is_last_engineer(conn, row):
            raise Refused(f"{username!r} is the last enabled engineer; make another engineer first",
                          "LAST_ENGINEER")
        conn.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (role, _now(), row["id"]))


def set_enabled(username: str, enabled: bool, kind: str | None = None) -> None:
    """Disabling ends every session of the user; a disabled principal's keys fail
    until it is enabled again."""
    with _connect(write=True) as conn:
        row = _existing(conn, username, kind)
        if not enabled and _is_last_engineer(conn, row):
            raise Refused(f"{username!r} is the last enabled engineer; make another engineer first",
                          "LAST_ENGINEER")
        conn.execute("UPDATE users SET enabled = ?, updated_at = ? WHERE id = ?",
                     (int(enabled), _now(), row["id"]))
        if not enabled:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))


# ── service principals and their keys (PR 3) ─────────────────────────────────

def _key(row) -> Key:
    return Key(row["id"], row["label"], row["created_at"], row["expires_at"], row["revoked_at"], row["last_used"])


def _principal(conn, row) -> ServicePrincipal:
    rows = conn.execute("SELECT * FROM api_keys WHERE user_id = ? ORDER BY created_at DESC, id",
                        (row["id"],)).fetchall()
    return ServicePrincipal(row["id"], row["username"], row["role"], bool(row["enabled"]), row["created_at"],
                            tuple(_key(k) for k in rows))


def create_principal(name: str, role: str) -> ServicePrincipal:
    """A service principal: no usable password, no session, keys only. Only after bootstrap."""
    _check_role(role)
    now = _now()
    with _connect(write=True) as conn:
        if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is None:
            raise Refused("the system is not set up yet; run bootstrap first")
        if conn.execute("SELECT 1 FROM users WHERE username = ?", (name,)).fetchone():
            raise Refused(f"the name {name!r} is already taken", "PRINCIPAL_EXISTS")
        conn.execute("INSERT INTO users (username, role, password_hash, enabled, must_change_password, "
                     "created_at, updated_at, kind) VALUES (?, ?, ?, 1, 0, ?, ?, 'service')",
                     (name, role, NO_PASSWORD, now, now))
        return _principal(conn, _existing(conn, name, "service"))


def get_principal(name: str) -> ServicePrincipal:
    with _connect() as conn:
        return _principal(conn, _existing(conn, name, "service"))


def list_principals() -> list[ServicePrincipal]:
    with _connect() as conn:
        rows = conn.execute("SELECT * FROM users WHERE kind = 'service' ORDER BY username").fetchall()
        return [_principal(conn, row) for row in rows]


def issue_key(name: str, days: int = KEY_DAYS_DEFAULT, label: str = "") -> tuple[Key, str]:
    """A new key for the principal; returns it and its plaintext, which is stored
    nowhere. Older keys stay valid until revoked or expired (Q24)."""
    if not (isinstance(days, int) and 1 <= days <= KEY_DAYS_MAX):
        raise ValueError(f"days must be a whole number from 1 to {KEY_DAYS_MAX}")
    key_id, key = keys.generate()
    now = _now()
    with _connect(write=True) as conn:
        row = _existing(conn, name, "service")
        conn.execute("INSERT INTO api_keys (id, user_id, key_hash, label, created_at, expires_at) "
                     "VALUES (?, ?, ?, ?, ?, ?)", (key_id, row["id"], keys.digest(key), label, now,
                                                   now + days * _DAY_S))
        stored = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
    return _key(stored), key


def revoke_key(name: str, key_id: str) -> Key:
    """Revoke a key of the principal; a revoked key keeps its first revoked time."""
    with _connect(write=True) as conn:
        row = _existing(conn, name, "service")
        found = conn.execute("SELECT * FROM api_keys WHERE id = ? AND user_id = ?", (key_id, row["id"])).fetchone()
        if found is None:
            raise Refused(f"{name!r} has no key {key_id!r}", "KEY_NOT_FOUND")
        conn.execute("UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL", (_now(), key_id))
        return _key(conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone())


def authenticate_key(key: str | None) -> Principal | None:
    """The key's principal as it is now, or None when the key is malformed,
    unknown, revoked or expired, or its principal is disabled."""
    key_id = keys.key_id(key) if key else None
    if key_id is None:
        return None
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT k.key_hash, k.expires_at, k.revoked_at, k.last_used, u.id, u.username, u.role, u.enabled, "
            "u.kind FROM api_keys k JOIN users u ON u.id = k.user_id WHERE k.id = ?", (key_id,)).fetchone()
    if row is None or not hmac.compare_digest(row["key_hash"], keys.digest(key)):
        return None
    if row["revoked_at"] is not None or now >= row["expires_at"] or not row["enabled"] or row["kind"] != "service":
        return None
    if row["last_used"] is None or now - row["last_used"] > _TOUCH_S:
        with _connect(write=True) as conn:
            conn.execute("UPDATE api_keys SET last_used = ? WHERE id = ?", (now, key_id))
    return Principal(row["id"], row["username"], row["role"], False, "", "service", key_id)


# ── sessions ─────────────────────────────────────────────────────────────────

def create_session(user_id: int) -> str:
    """A new session; returns the token for the cookie. Only its SHA-256 is stored."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with _connect(write=True) as conn:
        conn.execute("INSERT INTO sessions (token_hash, user_id, created_at, last_seen) VALUES (?, ?, ?, ?)",
                     (_token_hash(token), user_id, now, now))
    return token


def authenticate(token: str | None) -> Principal | None:
    """The session's user as they are now, or None. An ended session is deleted."""
    if not token or len(token) > 256:
        return None
    digest = _token_hash(token)
    now = _now()
    with _connect() as conn:
        row = conn.execute(
            "SELECT s.created_at, s.last_seen, u.id, u.username, u.role, u.enabled, u.must_change_password "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?", (digest,)).fetchone()
    if row is None:
        return None
    if now - row["last_seen"] > IDLE_S or now - row["created_at"] > ABSOLUTE_S or not row["enabled"]:
        delete_session(digest)
        return None
    if now - row["last_seen"] > _TOUCH_S:
        with _connect(write=True) as conn:
            conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, digest))
    return Principal(row["id"], row["username"], row["role"], bool(row["must_change_password"]), digest)


def delete_session(token_hash: str) -> None:
    with _connect(write=True) as conn:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (token_hash,))


def change_password(principal: Principal, password: str) -> None:
    """The user's own change: clears the forced change and ends their other sessions."""
    password_hash = passwords.hash_password(password)
    with _connect(write=True) as conn:
        conn.execute("UPDATE users SET password_hash = ?, must_change_password = 0, updated_at = ? "
                     "WHERE id = ?", (password_hash, _now(), principal.user_id))
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                     (principal.user_id, principal.token_hash))


# ── login throttle ───────────────────────────────────────────────────────────

def locked(username: str, address: str) -> bool:
    with _connect() as conn:
        row = conn.execute("SELECT until FROM login_lockouts WHERE username = ? AND address = ?",
                           (username, address)).fetchone()
    return row is not None and row["until"] > _now()


def record_failure(username: str, address: str) -> bool:
    """Count a failed login; True when it locks the pair."""
    now = _now()
    with _connect(write=True) as conn:
        conn.execute("DELETE FROM login_failures WHERE at <= ?", (now - THROTTLE_WINDOW_S,))
        conn.execute("DELETE FROM login_lockouts WHERE until <= ?", (now,))
        conn.execute("INSERT INTO login_failures (username, address, at) VALUES (?, ?, ?)",
                     (username, address, now))
        count = conn.execute("SELECT COUNT(*) FROM login_failures WHERE username = ? AND address = ?",
                             (username, address)).fetchone()[0]
        if count < THROTTLE_FAILURES:
            return False
        conn.execute("INSERT OR REPLACE INTO login_lockouts (username, address, until) VALUES (?, ?, ?)",
                     (username, address, now + LOCKOUT_S))
        conn.execute("DELETE FROM login_failures WHERE username = ? AND address = ?", (username, address))
    return True


def reserve_attempt(username: str, address: str) -> bool:
    """Count an attempt before its password is checked; False when the pair is
    locked or already has its 5 counted attempts in the window (some of them
    possibly still being checked). Check and count are one transaction, so
    parallel attempts can't all pass the check. finish_attempt settles it."""
    now = _now()
    with _connect(write=True) as conn:
        conn.execute("DELETE FROM login_failures WHERE at <= ?", (now - THROTTLE_WINDOW_S,))
        conn.execute("DELETE FROM login_lockouts WHERE until <= ?", (now,))
        if conn.execute("SELECT 1 FROM login_lockouts WHERE username = ? AND address = ?",
                        (username, address)).fetchone():
            return False
        count = conn.execute("SELECT COUNT(*) FROM login_failures WHERE username = ? AND address = ?",
                             (username, address)).fetchone()[0]
        if count >= THROTTLE_FAILURES:
            return False
        conn.execute("INSERT INTO login_failures (username, address, at) VALUES (?, ?, ?)",
                     (username, address, now))
    return True


def finish_attempt(username: str, address: str, succeeded: bool) -> bool:
    """Settle a reserved attempt: a success clears the pair's count; a failure
    keeps its row and, at 5 in the window, locks the pair. True when it locks."""
    now = _now()
    with _connect(write=True) as conn:
        if succeeded:
            conn.execute("DELETE FROM login_failures WHERE username = ? AND address = ?", (username, address))
            return False
        count = conn.execute("SELECT COUNT(*) FROM login_failures WHERE username = ? AND address = ? AND at > ?",
                             (username, address, now - THROTTLE_WINDOW_S)).fetchone()[0]
        if count < THROTTLE_FAILURES:
            return False
        conn.execute("INSERT OR REPLACE INTO login_lockouts (username, address, until) VALUES (?, ?, ?)",
                     (username, address, now + LOCKOUT_S))
        conn.execute("DELETE FROM login_failures WHERE username = ? AND address = ?", (username, address))
    return True


def clear_failures(username: str, address: str) -> None:
    with _connect(write=True) as conn:
        conn.execute("DELETE FROM login_failures WHERE username = ? AND address = ?", (username, address))
