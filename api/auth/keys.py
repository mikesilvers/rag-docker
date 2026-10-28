"""The service API key format (#25, PR 3).

A key is `rag_` + a 12-character key id + `_` + 43 random characters
(secrets.token_urlsafe(32), 256 bits): 60 characters. The id names the key in
listings and logs and finds its row; it doesn't authenticate on its own. The
store keeps only the id and the SHA-256 of the whole key: the key is random
and high-entropy, so a slow hash would add nothing (as for session tokens).

Keys are sent only as `Authorization: Bearer <key>`; nothing reads one from a
query parameter, a cookie or another header.
"""
from __future__ import annotations

import hashlib
import re
import secrets
import string

PREFIX = "rag_"
ID_LENGTH = 12
SECRET_LENGTH = 43
LENGTH = len(PREFIX) + ID_LENGTH + 1 + SECRET_LENGTH
_ID_ALPHABET = string.ascii_lowercase + string.digits
_KEY = re.compile(r"rag_([a-z0-9]{12})_[A-Za-z0-9_-]{43}")


def generate() -> tuple[str, str]:
    """A new (key id, key)."""
    key_id = "".join(secrets.choice(_ID_ALPHABET) for _ in range(ID_LENGTH))
    return key_id, f"{PREFIX}{key_id}_{secrets.token_urlsafe(32)}"


def key_id(key: str) -> str | None:
    """The id of a key of the right shape, or None."""
    if not isinstance(key, str) or len(key) != LENGTH:
        return None
    found = _KEY.fullmatch(key)
    return found.group(1) if found else None


def digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def bearer(value: str | None) -> str | None:
    """The key in an `Authorization` value of the form `Bearer <key>` (scheme
    case-insensitive, one space), or None."""
    if not value:
        return None
    scheme, sep, key = value.partition(" ")
    if not sep or scheme.lower() != "bearer" or key_id(key) is None:
        return None
    return key
