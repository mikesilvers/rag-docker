"""Username and password rules, and password hashing (#25).

Hashing is the standard library's scrypt, so no new dependency. The parameters
are stored with each hash, so they can be raised later without breaking the
hashes already stored.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets

# n = 2^15, r = 8, p = 1: about 32 MiB and a few tens of milliseconds per hash.
SCRYPT_LOG2_N = 15
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
HASH_BYTES = 32
# hashlib's default limit is 32 MiB, exactly what n=2^15, r=8 needs, and scrypt
# needs a little more than that, so the default refuses these parameters.
_MAXMEM = 64 * 1024 * 1024

USERNAME_RULE = "3-32 characters: lowercase letters, digits, '.', '_' or '-'"
_USERNAME = re.compile(r"[a-z0-9._-]{3,32}")
PASSWORD_MIN = 12
PASSWORD_MAX = 256


def normalize_username(username: str) -> str | None:
    """The stored form of a username (lowercased), or None when it breaks the rule."""
    if not isinstance(username, str):
        return None
    name = username.lower()
    return name if _USERNAME.fullmatch(name) else None


def password_problem(password: str, username: str) -> str | None:
    """Why a new password is refused, or None when it is acceptable."""
    if len(password) < PASSWORD_MIN:
        return f"The password must be at least {PASSWORD_MIN} characters."
    if len(password) > PASSWORD_MAX:
        return f"The password must be at most {PASSWORD_MAX} characters."
    if password.lower() == username.lower():
        return "The password must not be the username."
    return None


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _scrypt(password: str, salt: bytes, log2_n: int, r: int, p: int, length: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2 ** log2_n, r=r, p=p,
                          maxmem=_MAXMEM, dklen=length)


def hash_password(password: str) -> str:
    """`scrypt$<log2 n>$<r>$<p>$<salt>$<hash>`, salt and hash in base64."""
    salt = os.urandom(SALT_BYTES)
    digest = _scrypt(password, salt, SCRYPT_LOG2_N, SCRYPT_R, SCRYPT_P, HASH_BYTES)
    return "$".join(("scrypt", str(SCRYPT_LOG2_N), str(SCRYPT_R), str(SCRYPT_P),
                     _b64(salt), _b64(digest)))


def verify_password(password: str, stored: str) -> bool:
    """Whether `password` matches `stored`. A malformed hash never matches."""
    try:
        scheme, log2_n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        log2_n, r, p = int(log2_n), int(r), int(p)
        # Bounds, so a corrupted row can't ask for gigabytes of memory.
        if not (1 <= log2_n <= 20 and 1 <= r <= 16 and 1 <= p <= 4):
            return False
        salt, digest = base64.b64decode(salt, validate=True), base64.b64decode(digest, validate=True)
        if not salt or not digest:
            return False
        candidate = _scrypt(password, salt, log2_n, r, p, len(digest))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, digest)


def generate_password() -> str:
    """A one-time password: 24 URL-safe characters, 144 random bits."""
    return secrets.token_urlsafe(18)


_dummy_hash: str | None = None


def dummy_hash() -> str:
    """A hash to verify against when the username is unknown, so a failed login
    takes as long whether the account exists or not."""
    global _dummy_hash
    if _dummy_hash is None:
        _dummy_hash = hash_password(secrets.token_urlsafe(18))
    return _dummy_hash
