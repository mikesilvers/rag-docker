"""Accounts, sessions and the login throttle, in one SQLite file (#25).

The file is `auth.db` in AUTH_DIR, the auth_data volume, so all three survive an
API restart. The API and the operator command (`python -m auth`) open it from
separate processes; SQLite's locking keeps them consistent, and the
last-engineer rule is checked inside a write transaction so two commands
can't both pass it.

Every function opens its own short connection. Nothing here is cached except
the fact that the system is set up, which can't become false again: no
command deletes an account.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from config import settings

from . import passwords
from .policy import ROLES

COOKIE = "rag_session"
IDLE_S = 12 * 3600               # a session ends after 12 hours without a request
ABSOLUTE_S = 7 * 24 * 3600       # and 7 days after login, whichever is first
_TOUCH_S = 60                    # last_seen is written at most once a minute
THROTTLE_FAILURES = 5            # failed logins per (username, address) ...
THROTTLE_WINDOW_S = 15 * 60      # ... within 15 minutes ...
LOCKOUT_S = 15 * 60              # ... lock that pair for 15 minutes

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
"""

_ready: set[str] = set()
_set_up: set[str] = set()


class Refused(Exception):
    """An account change the rules don't allow; the message says why, in one line."""


@dataclass(frozen=True)
class Principal:
    user_id: int
    username: str
    role: str
    must_change_password: bool
    token_hash: str


@dataclass(frozen=True)
class User:
    id: int
    username: str
    role: str
    password_hash: str
    enabled: bool
    must_change_password: bool
    created_at: float


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
                conn.execute("PRAGMA user_version = 1")
                conn.commit()
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


def _user(row) -> User | None:
    if row is None:
        return None
    return User(row["id"], row["username"], row["role"], row["password_hash"],
                bool(row["enabled"]), bool(row["must_change_password"]), row["created_at"])


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


# ── accounts (the operator command) ──────────────────────────────────────────

def get_user(username: str) -> User | None:
    with _connect() as conn:
        return _user(conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())


def list_users() -> list[User]:
    with _connect() as conn:
        return [_user(row) for row in conn.execute("SELECT * FROM users ORDER BY username")]


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
            raise Refused(f"the user {username!r} already exists")
        conn.execute("INSERT INTO users (username, role, password_hash, enabled, must_change_password, "
                     "created_at, updated_at) VALUES (?, ?, ?, 1, ?, ?, ?)",
                     (username, role, password_hash, int(must_change_password), now, now))
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    return _user(row)


def _existing(conn, username: str):
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if row is None:
        raise Refused(f"there is no user {username!r}")
    return row


def _is_last_engineer(conn, row) -> bool:
    if row["role"] != "engineer" or not row["enabled"]:
        return False
    others = conn.execute("SELECT COUNT(*) FROM users WHERE role = 'engineer' AND enabled = 1 AND id != ?",
                          (row["id"],)).fetchone()[0]
    return others == 0


def reset_password(username: str, password: str) -> None:
    """A new password the user must change at next login; every session of theirs ends."""
    password_hash = passwords.hash_password(password)
    with _connect(write=True) as conn:
        row = _existing(conn, username)
        conn.execute("UPDATE users SET password_hash = ?, must_change_password = 1, updated_at = ? "
                     "WHERE id = ?", (password_hash, _now(), row["id"]))
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))


def set_role(username: str, role: str) -> None:
    """Sessions stay; the next request of each one runs with the new role."""
    _check_role(role)
    with _connect(write=True) as conn:
        row = _existing(conn, username)
        if role != "engineer" and _is_last_engineer(conn, row):
            raise Refused(f"{username!r} is the last enabled engineer; make another engineer first")
        conn.execute("UPDATE users SET role = ?, updated_at = ? WHERE id = ?", (role, _now(), row["id"]))


def set_enabled(username: str, enabled: bool) -> None:
    """Disabling ends every session of the user."""
    with _connect(write=True) as conn:
        row = _existing(conn, username)
        if not enabled and _is_last_engineer(conn, row):
            raise Refused(f"{username!r} is the last enabled engineer; make another engineer first")
        conn.execute("UPDATE users SET enabled = ?, updated_at = ? WHERE id = ?",
                     (int(enabled), _now(), row["id"]))
        if not enabled:
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))


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


def clear_failures(username: str, address: str) -> None:
    with _connect(write=True) as conn:
        conn.execute("DELETE FROM login_failures WHERE username = ? AND address = ?", (username, address))
