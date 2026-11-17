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

Two-step verification (#607, schema 3): a user's sealed TOTP secret and last
accepted step (`mfa`), a set-up not yet confirmed (`mfa_pending`), recovery
codes as SHA-256 (`recovery_codes`), login challenges between the password and
the code (`mfa_challenges`, token as SHA-256) and the "require for everyone"
switch (`settings`). The codes and the sealing are in mfa.py.

Per-collection access (#606, schema 4): a collection named in
`restricted_collections` is restricted, and only the accounts in its
`collection_grants` (people or service principals) and Engineers may use it;
any other collection is open to every account its role allows. Names are the
canonical (stored) collection names. The rule itself is in access.py.
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

from . import keys, mfa, passwords
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
SCHEMA_VERSION = 4
MFA_PENDING_S = 10 * 60          # a started set-up must be confirmed within 10 minutes
MFA_CHALLENGE_S = 5 * 60         # the code must follow the password within 5 minutes ...
MFA_CHALLENGE_TRIES = 5          # ... with at most 5 codes per challenge
MFA_COOKIE = "rag_mfa"

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
CREATE TABLE IF NOT EXISTS mfa (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    secret TEXT NOT NULL,
    last_step INTEGER NOT NULL,
    enrolled_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS mfa_pending (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    secret TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recovery_codes (
    user_id INTEGER NOT NULL REFERENCES users(id),
    code_hash TEXT NOT NULL,
    used_at REAL
);
CREATE INDEX IF NOT EXISTS recovery_codes_user ON recovery_codes(user_id);
CREATE TABLE IF NOT EXISTS mfa_challenges (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    login_key TEXT NOT NULL,
    created_at REAL NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS mfa_challenges_user ON mfa_challenges(user_id);
CREATE TABLE IF NOT EXISTS settings (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS restricted_collections (
    collection TEXT PRIMARY KEY,
    restricted_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS collection_grants (
    collection TEXT NOT NULL,
    user_id INTEGER NOT NULL REFERENCES users(id),
    granted_at REAL NOT NULL,
    PRIMARY KEY (collection, user_id)
);
"""

_ready: set[str] = set()
_set_up: set[str] = set()


class Refused(Exception):
    """An account change the rules don't allow; the message says why, in one line.
    `code` names the reason for the API: USER_EXISTS, USER_NOT_FOUND,
    LAST_ENGINEER, PRINCIPAL_EXISTS, PRINCIPAL_NOT_FOUND, KEY_NOT_FOUND,
    NOT_A_USER, REFUSED for the bootstrap rules, or one of the MFA codes
    (MFA_ALREADY_ENROLLED, MFA_ENROLLMENT_NOT_STARTED, MFA_CODE_INCORRECT,
    MFA_NOT_ENROLLED, MFA_REQUIRED, MFA_ENROLL_FIRST), or UNKNOWN_ACCOUNT for a
    collection grant naming no such account (#606)."""

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
    mfa_enrolled: bool = False
    mfa_due: bool = False        # MFA is required for everyone and this user hasn't enrolled (#607)


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
    mfa_enrolled: bool = False


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
    """Version 2 (PR 3): users.kind. Versions 3 (#607) and 4 (#606) only add
    tables, which _SCHEMA creates; version 4 leaves every existing collection
    open, since none is named in restricted_collections. In a write transaction, so the API and the operator
    command can't both add the column."""
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
                bool(row["enabled"]), bool(row["must_change_password"]), row["created_at"], row["kind"],
                bool(row["mfa_enrolled"]))


# Every users query that builds a User selects through this, so the MFA state comes along.
_USERS = "SELECT u.*, (m.user_id IS NOT NULL) AS mfa_enrolled FROM users u LEFT JOIN mfa m ON m.user_id = u.id"


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


def has_enabled_engineer() -> bool:
    """True when an enabled human Engineer exists: LAN mode refuses to start
    without one (#26). A service principal can't log in, so it doesn't count."""
    with _connect() as conn:
        return conn.execute("SELECT 1 FROM users WHERE role = 'engineer' AND enabled = 1 AND kind = 'user' "
                            "LIMIT 1").fetchone() is not None


# ── accounts (the operator command and the admin routes) ─────────────────────

def get_user(username: str) -> User | None:
    with _connect() as conn:
        return _user(conn.execute(_USERS + " WHERE u.username = ?", (username,)).fetchone())


def list_users(kind: str | None = "user") -> list[User]:
    """Human accounts by default; kind=None lists principals too (the operator's `list`)."""
    with _connect() as conn:
        if kind is None:
            rows = conn.execute(_USERS + " ORDER BY u.username")
        else:
            rows = conn.execute(_USERS + " WHERE u.kind = ? ORDER BY u.username", (kind,))
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
        row = conn.execute(_USERS + " WHERE u.username = ?", (username,)).fetchone()
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
        # A login half-way through, started with the old password, ends too (#607).
        conn.execute("DELETE FROM mfa_challenges WHERE user_id = ?", (row["id"],))


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
            # Pending login challenges end too, so enabling again doesn't revive one (#607).
            conn.execute("DELETE FROM mfa_challenges WHERE user_id = ?", (row["id"],))


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
    """A new session; returns the token for the cookie. Only its SHA-256 is stored.
    Every session that has ended by time, anyone's, is deleted in the same
    transaction, so sessions abandoned without a logout don't pile up (#546)."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with _connect(write=True) as conn:
        conn.execute("DELETE FROM sessions WHERE last_seen < ? OR created_at < ?", (now - IDLE_S, now - ABSOLUTE_S))
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
            "SELECT s.created_at, s.last_seen, u.id, u.username, u.role, u.enabled, u.must_change_password, "
            "(m.user_id IS NOT NULL) AS mfa_enrolled FROM sessions s JOIN users u ON u.id = s.user_id "
            "LEFT JOIN mfa m ON m.user_id = u.id WHERE s.token_hash = ?", (digest,)).fetchone()
        required = _mfa_required(conn) if row is not None else False
    if row is None:
        return None
    if now - row["last_seen"] > IDLE_S or now - row["created_at"] > ABSOLUTE_S or not row["enabled"]:
        delete_session(digest)
        return None
    if now - row["last_seen"] > _TOUCH_S:
        with _connect(write=True) as conn:
            conn.execute("UPDATE sessions SET last_seen = ? WHERE token_hash = ?", (now, digest))
    enrolled = bool(row["mfa_enrolled"])
    return Principal(row["id"], row["username"], row["role"], bool(row["must_change_password"]), digest,
                     mfa_enrolled=enrolled, mfa_due=required and not enrolled)


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
        # A login half-way through, started with the old password, ends too (#607).
        conn.execute("DELETE FROM mfa_challenges WHERE user_id = ?", (principal.user_id,))


# ── login throttle ───────────────────────────────────────────────────────────

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


def release_attempt(username: str, address: str) -> None:
    """Withdraw a reserved attempt that wasn't a failure without clearing the
    pair's count: a right password for an enrolled user (#607), whose earlier
    failures stay counted until the second factor passes."""
    with _connect(write=True) as conn:
        conn.execute("DELETE FROM login_failures WHERE rowid = (SELECT MAX(rowid) FROM login_failures "
                     "WHERE username = ? AND address = ?)", (username, address))


# ── two-step verification (#607) ─────────────────────────────────────────────

def _mfa_required(conn) -> bool:
    return conn.execute("SELECT 1 FROM settings WHERE name = 'mfa_required' AND value = '1'").fetchone() is not None


def mfa_required() -> bool:
    """The "require two-step verification for everyone" switch."""
    with _connect() as conn:
        return _mfa_required(conn)


def set_mfa_required(user_id: int, required: bool) -> None:
    """Set the switch. Turning it on needs the engineer turning it on to be
    enrolled, so nobody switches on a gate they couldn't pass themselves."""
    with _connect(write=True) as conn:
        if required:
            if conn.execute("SELECT 1 FROM mfa WHERE user_id = ?", (user_id,)).fetchone() is None:
                raise Refused("set up two-step verification for your own account first", "MFA_ENROLL_FIRST")
            conn.execute("INSERT OR REPLACE INTO settings (name, value) VALUES ('mfa_required', '1')")
        else:
            conn.execute("DELETE FROM settings WHERE name = 'mfa_required'")


def _status(conn, user_id: int) -> dict:
    row = conn.execute("SELECT enrolled_at FROM mfa WHERE user_id = ?", (user_id,)).fetchone()
    remaining = conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ? AND used_at IS NULL",
                             (user_id,)).fetchone()[0]
    return {"enrolled": row is not None, "enrolled_at": row["enrolled_at"] if row else None,
            "required": _mfa_required(conn), "recovery_codes_remaining": remaining if row else 0}


def mfa_status(user_id: int) -> dict:
    """{"enrolled", "enrolled_at" (epoch or None), "required", "recovery_codes_remaining"}."""
    with _connect() as conn:
        return _status(conn, user_id)


def start_enrollment(user_id: int, username: str) -> tuple[str, str]:
    """A new pending secret (replacing an earlier one); returns it and its
    otpauth URI. The secret is stored sealed; this answer is the only copy."""
    secret = mfa.new_secret()
    sealed = mfa.seal(user_id, secret)
    with _connect(write=True) as conn:
        if conn.execute("SELECT 1 FROM mfa WHERE user_id = ?", (user_id,)).fetchone():
            raise Refused("two-step verification is already set up", "MFA_ALREADY_ENROLLED")
        conn.execute("INSERT OR REPLACE INTO mfa_pending (user_id, secret, created_at) VALUES (?, ?, ?)",
                     (user_id, sealed, _now()))
    return secret, mfa.otpauth_uri(username, secret)


def _new_codes(conn, user_id: int) -> list[str]:
    codes = mfa.new_recovery_codes()
    conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
    conn.executemany("INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)",
                     [(user_id, mfa.recovery_hash(code)) for code in codes])
    return codes


def confirm_enrollment(principal: Principal, code: str) -> list[str]:
    """Confirm the pending set-up with a code from the app: the secret becomes
    the user's, a recovery-code set is made, and every other session of the
    user ends (it was opened with the password alone). Returns the codes."""
    now = _now()
    with _connect(write=True) as conn:
        if conn.execute("SELECT 1 FROM mfa WHERE user_id = ?", (principal.user_id,)).fetchone():
            raise Refused("two-step verification is already set up", "MFA_ALREADY_ENROLLED")
        row = conn.execute("SELECT secret, created_at FROM mfa_pending WHERE user_id = ?",
                           (principal.user_id,)).fetchone()
        if row is None or now - row["created_at"] > MFA_PENDING_S:
            raise Refused("no set-up is in progress; start it again", "MFA_ENROLLMENT_NOT_STARTED")
        secret = mfa.unseal(principal.user_id, row["secret"])
        step = mfa.matching_step(secret, code, now, -1) if secret else None
        if step is None:
            raise Refused("the code is incorrect", "MFA_CODE_INCORRECT")
        conn.execute("INSERT INTO mfa (user_id, secret, last_step, enrolled_at) VALUES (?, ?, ?, ?)",
                     (principal.user_id, row["secret"], step, now))
        conn.execute("DELETE FROM mfa_pending WHERE user_id = ?", (principal.user_id,))
        codes = _new_codes(conn, principal.user_id)
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
                     (principal.user_id, principal.token_hash))
    return codes


def _verify_factor(conn, user_id: int, code: str, allow_recovery: bool) -> str | None:
    """Inside the caller's write transaction: "totp" or "recovery" when `code`
    is a valid second factor of the user (and spends it), else None. A TOTP
    code's step becomes the last accepted one; a recovery code is marked used."""
    now = _now()
    if mfa.is_totp(code):
        row = conn.execute("SELECT secret, last_step FROM mfa WHERE user_id = ?", (user_id,)).fetchone()
        secret = mfa.unseal(user_id, row["secret"]) if row else None
        step = mfa.matching_step(secret, code, now, row["last_step"]) if secret else None
        if step is None:
            return None
        conn.execute("UPDATE mfa SET last_step = ? WHERE user_id = ?", (step, user_id))
        return "totp"
    if not allow_recovery:
        return None
    digest = mfa.recovery_hash(code)
    if digest is None:
        return None
    found = conn.execute("SELECT rowid FROM recovery_codes WHERE user_id = ? AND code_hash = ? AND used_at IS NULL",
                         (user_id, digest)).fetchone()
    if found is None:
        return None
    conn.execute("UPDATE recovery_codes SET used_at = ? WHERE rowid = ?", (now, found[0]))
    return "recovery"


def factor_method(code: str) -> str:
    """How a typed code is checked: "totp" for 6 digits, else "recovery"."""
    return "totp" if mfa.is_totp(code) else "recovery"


def create_challenge(user_id: int, login_key: str) -> str:
    """A login challenge after a right password; returns the token for the
    `rag_mfa` cookie, of which only the SHA-256 is stored. The user's earlier
    challenges and every expired one are deleted."""
    token = secrets.token_urlsafe(32)
    now = _now()
    with _connect(write=True) as conn:
        conn.execute("DELETE FROM mfa_challenges WHERE user_id = ? OR created_at < ?",
                     (user_id, now - MFA_CHALLENGE_S))
        conn.execute("INSERT INTO mfa_challenges (token_hash, user_id, login_key, created_at, attempts) "
                     "VALUES (?, ?, ?, ?, 0)", (_token_hash(token), user_id, login_key, now))
    return token


def _live_challenge(conn, token: str | None):
    if not token or len(token) > 256:
        return None
    row = conn.execute(
        "SELECT c.token_hash, c.user_id, c.login_key, c.created_at, c.attempts, u.username, u.role, u.enabled, "
        "u.kind, (m.user_id IS NOT NULL) AS enrolled FROM mfa_challenges c JOIN users u ON u.id = c.user_id "
        "LEFT JOIN mfa m ON m.user_id = u.id WHERE c.token_hash = ?", (_token_hash(token),)).fetchone()
    if (row is None or _now() - row["created_at"] > MFA_CHALLENGE_S or row["attempts"] >= MFA_CHALLENGE_TRIES
            or not row["enabled"] or not row["enrolled"] or row["kind"] != "user"):
        return None
    return row


def challenge(token: str | None) -> tuple[str, str] | None:
    """(username, login_key) of a live challenge, or None when it is missing,
    expired, used up, or its user is disabled or no longer enrolled."""
    with _connect() as conn:
        row = _live_challenge(conn, token)
    return None if row is None else (row["username"], row["login_key"])


def answer_challenge(token: str | None, code: str) -> tuple[str, User | None, str | None]:
    """Check a second factor against a challenge, in one write transaction:
    ("ok", user, method) deletes the challenge; ("failed", user, method) counts
    an attempt and deletes the challenge at the fifth; ("invalid", None, None)
    when the challenge isn't live (any more)."""
    with _connect(write=True) as conn:
        row = _live_challenge(conn, token)
        if row is None:
            return "invalid", None, None
        method = _verify_factor(conn, row["user_id"], code, allow_recovery=True)
        if method is None:
            conn.execute("UPDATE mfa_challenges SET attempts = attempts + 1 WHERE token_hash = ?", (row["token_hash"],))
            conn.execute("DELETE FROM mfa_challenges WHERE token_hash = ? AND attempts >= ?",
                         (row["token_hash"], MFA_CHALLENGE_TRIES))
            outcome = "failed"
        else:
            conn.execute("DELETE FROM mfa_challenges WHERE token_hash = ?", (row["token_hash"],))
            outcome = "ok"
        user = _user(conn.execute(_USERS + " WHERE u.id = ?", (row["user_id"],)).fetchone())
    return outcome, user, method or factor_method(code)


def regenerate_codes(principal: Principal, code: str) -> list[str] | None:
    """A new recovery-code set, replacing the old one, for a current TOTP code
    (a recovery code doesn't count). None when the code is wrong."""
    with _connect(write=True) as conn:
        if conn.execute("SELECT 1 FROM mfa WHERE user_id = ?", (principal.user_id,)).fetchone() is None:
            raise Refused("two-step verification isn't set up", "MFA_NOT_ENROLLED")
        if _verify_factor(conn, principal.user_id, code, allow_recovery=False) is None:
            return None
        return _new_codes(conn, principal.user_id)


def disable_mfa(principal: Principal, password: str, code: str) -> bool:
    """The user's own turn-off, only while MFA isn't required: needs the
    current password and a TOTP or recovery code. False when either is wrong.
    Sessions stay."""
    with _connect() as conn:
        if _mfa_required(conn):
            raise Refused("two-step verification is required for everyone", "MFA_REQUIRED")
        row = conn.execute("SELECT password_hash FROM users WHERE id = ?", (principal.user_id,)).fetchone()
        if conn.execute("SELECT 1 FROM mfa WHERE user_id = ?", (principal.user_id,)).fetchone() is None:
            raise Refused("two-step verification isn't set up", "MFA_NOT_ENROLLED")
    # scrypt outside the write lock, as a login does.
    right_password = row is not None and passwords.verify_password(password, row["password_hash"])
    with _connect(write=True) as conn:
        if _mfa_required(conn):
            raise Refused("two-step verification is required for everyone", "MFA_REQUIRED")
        if not right_password:
            return False
        if _verify_factor(conn, principal.user_id, code, allow_recovery=True) is None:
            return False
        _forget_mfa(conn, principal.user_id)
    return True


def _forget_mfa(conn, user_id: int) -> None:
    for table in ("mfa", "mfa_pending", "recovery_codes", "mfa_challenges"):
        conn.execute(f"DELETE FROM {table} WHERE user_id = ?", (user_id,))


def reset_mfa(username: str, kind: str | None = None) -> None:
    """An engineer's or the operator's reset: the user's secret, pending set-up,
    recovery codes and challenges are deleted and every session of theirs ends.
    Their next login asks for no code (and for a set-up, while MFA is required).
    A service principal has no second factor, so it is refused."""
    with _connect(write=True) as conn:
        row = _existing(conn, username, kind)
        if row["kind"] != "user":
            raise Refused(f"{username!r} is a service principal; it has no two-step verification", "NOT_A_USER")
        _forget_mfa(conn, row["id"])
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (row["id"],))


# ── per-collection access (#606) ─────────────────────────────────────────────

def _canonical(collection: str) -> str:
    # collection_writes.canonical, inlined so auth imports nothing from services.
    return collection[:1].upper() + collection[1:]


@dataclass(frozen=True)
class CollectionAccess:
    restricted: bool
    users: tuple[str, ...] = ()
    service_principals: tuple[str, ...] = ()


def _access(conn, collection: str) -> CollectionAccess:
    if conn.execute("SELECT 1 FROM restricted_collections WHERE collection = ?", (collection,)).fetchone() is None:
        return CollectionAccess(False)
    rows = conn.execute("SELECT u.username, u.kind FROM collection_grants g JOIN users u ON u.id = g.user_id "
                        "WHERE g.collection = ? ORDER BY u.username", (collection,)).fetchall()
    return CollectionAccess(True, tuple(r["username"] for r in rows if r["kind"] == "user"),
                            tuple(r["username"] for r in rows if r["kind"] == "service"))


def collection_access(collection: str) -> CollectionAccess:
    """Whether the collection is restricted, and who is granted it (names sorted)."""
    with _connect() as conn:
        return _access(conn, _canonical(collection))


def restricted_collections() -> dict[str, frozenset[int]]:
    """Every restricted collection and the ids of the accounts granted it."""
    with _connect() as conn:
        found = {row["collection"]: set() for row in conn.execute("SELECT collection FROM restricted_collections")}
        for row in conn.execute("SELECT collection, user_id FROM collection_grants"):
            if row["collection"] in found:
                found[row["collection"]].add(row["user_id"])
    return {name: frozenset(ids) for name, ids in found.items()}


def has_access(user_id: int, role: str, collection: str) -> bool:
    """An Engineer reaches every collection; anyone else an open one, or a
    restricted one they are granted. Read on every call, so a change applies to
    the next request."""
    if role == "engineer":
        return True
    collection = _canonical(collection)
    with _connect() as conn:
        if conn.execute("SELECT 1 FROM restricted_collections WHERE collection = ?", (collection,)).fetchone() is None:
            return True
        return conn.execute("SELECT 1 FROM collection_grants WHERE collection = ? AND user_id = ?",
                            (collection, user_id)).fetchone() is not None


def _owner_row(conn, owner):
    """The account a job record's owner ({"name", "kind"}) names, or None."""
    if not isinstance(owner, dict):
        return None
    kind = "service" if owner.get("kind") == "service" else "user"
    return conn.execute("SELECT * FROM users WHERE username = ? AND kind = ?", (owner.get("name"), kind)).fetchone()


def owner_has_access(owner, collection: str) -> bool:
    """has_access for a job's owner as the account is now. A job with no owner
    (a direct call in tests) isn't checked; an owner that names no account has
    no access."""
    if owner is None:
        return True
    with _connect() as conn:
        row = _owner_row(conn, owner)
    return row is not None and has_access(row["id"], row["role"], collection)


def user_id_for(owner) -> int | None:
    """The id of the account a job's owner names, or None."""
    with _connect() as conn:
        row = _owner_row(conn, owner)
    return row["id"] if row is not None else None


def _grant_ids(conn, names, kind: str) -> list[int]:
    ids = []
    for name in names:
        row = conn.execute("SELECT id FROM users WHERE username = ? AND kind = ?",
                           (str(name).strip().lower(), kind)).fetchone()
        if row is None:
            what = "person" if kind == "user" else "service principal"
            raise Refused(f"no {what} is named {name!r}", "UNKNOWN_ACCOUNT")
        ids.append(row["id"])
    return ids


def _restrict(conn, collection: str, user_ids) -> None:
    now = _now()
    conn.execute("INSERT OR IGNORE INTO restricted_collections (collection, restricted_at) VALUES (?, ?)",
                 (collection, now))
    conn.execute("DELETE FROM collection_grants WHERE collection = ?", (collection,))
    conn.executemany("INSERT INTO collection_grants (collection, user_id, granted_at) VALUES (?, ?, ?)",
                     [(collection, user_id, now) for user_id in dict.fromkeys(user_ids)])


def _open(conn, collection: str) -> None:
    conn.execute("DELETE FROM collection_grants WHERE collection = ?", (collection,))
    conn.execute("DELETE FROM restricted_collections WHERE collection = ?", (collection,))


def set_collection_access(collection: str, restricted: bool, users=(), service_principals=()):
    """An Engineer's change (PUT /collections/{name}/access): the whole state
    at once. Returns (before, after). An unknown name refuses the whole change
    (UNKNOWN_ACCOUNT); disabled accounts may be granted."""
    collection = _canonical(collection)
    with _connect(write=True) as conn:
        before = _access(conn, collection)
        if restricted:
            ids = _grant_ids(conn, users, "user") + _grant_ids(conn, service_principals, "service")
            _restrict(conn, collection, ids)
        else:
            _open(conn, collection)
        return before, _access(conn, collection)


def restrict_to(collection: str, user_id: int | None) -> None:
    """A new collection: restricted, granted only to its creator (none when None)."""
    with _connect(write=True) as conn:
        _restrict(conn, _canonical(collection), [] if user_id is None else [user_id])


def copy_collection_access(source: str, target: str) -> None:
    """The target takes the source's state: open stays open, restricted copies its grants."""
    source, target = _canonical(source), _canonical(target)
    with _connect(write=True) as conn:
        if conn.execute("SELECT 1 FROM restricted_collections WHERE collection = ?", (source,)).fetchone() is None:
            _open(conn, target)
            return
        ids = [row["user_id"] for row in
               conn.execute("SELECT user_id FROM collection_grants WHERE collection = ?", (source,))]
        _restrict(conn, target, ids)


def drop_collection_access(collection: str) -> None:
    """A deleted collection's restriction and grants, so a later one of the same name starts afresh."""
    with _connect(write=True) as conn:
        _open(conn, _canonical(collection))
