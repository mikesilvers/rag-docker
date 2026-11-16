"""Two-step verification (MFA, #607): TOTP codes, recovery codes and the
encryption of TOTP secrets at rest. Pure functions; the store (store.py) keeps
the rows and decides when a code counts.

- TOTP is RFC 6238 with HMAC-SHA1, 6 digits and a 30-second step, what every
  common authenticator app supports, over a 160-bit random secret. A code is
  accepted for the step before, the current one and the one after (clock drift)
  and only for a step later than the last one accepted, so no code works twice.
- Recovery codes are 80 random bits each, shown as `xxxx-xxxx-xxxx-xxxx` and
  stored only as the SHA-256 of their normalized form: with that much entropy
  and the login throttle, a slow hash would add nothing.
- A TOTP secret has to be read back, so it can't be hashed. It is sealed with
  AES-256-GCM under a key kept in its own file, `AUTH_DIR/mfa.key` (0600),
  with the user id as associated data: a copied `auth.db` alone (a bug report,
  a partial backup) reveals no secret, and a sealed secret can't be moved to
  another user. A secret that can't be unsealed (the key file lost or
  replaced) never verifies; its user signs in with a recovery code or is reset.

Nothing here logs a secret, code or key.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
from pathlib import Path
from urllib.parse import quote

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from config import settings

STEP_S = 30           # the TOTP time step
DIGITS = 6
WINDOW = 1            # steps accepted either side of the current one
SECRET_BYTES = 20     # 160 bits, RFC 4226's recommended length
RECOVERY_COUNT = 10
RECOVERY_BYTES = 10   # 80 bits: 16 base32 characters
ISSUER = "RAG Platform"
KEY_FILE = "mfa.key"
_NONCE = 12
_TOTP = re.compile(r"\d{6}")
_RECOVERY = re.compile(r"[a-z2-7]{16}")


def _b32(raw: bytes) -> str:
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _unb32(text: str) -> bytes:
    text = text.upper()
    return base64.b32decode(text + "=" * (-len(text) % 8))


# ── TOTP ─────────────────────────────────────────────────────────────────────

def new_secret() -> str:
    """A new TOTP secret as unpadded base32, as authenticator apps take it."""
    return _b32(secrets.token_bytes(SECRET_BYTES))


def code_at(secret: str, step: int) -> str:
    """The RFC 4226 HOTP value of `step` (RFC 6238's time counter)."""
    digest = hmac.new(_unb32(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def clean(code: str) -> str:
    return "".join(code.split())


def is_totp(code: str) -> bool:
    return bool(_TOTP.fullmatch(clean(code)))


def matching_step(secret: str, code: str, now: float, last_step: int) -> int | None:
    """The step in now's window, later than `last_step`, whose code is `code`;
    None when there is none. Every candidate is compared in constant time."""
    code = clean(code)
    if not _TOTP.fullmatch(code):
        return None
    current = int(now // STEP_S)
    found = None
    for step in range(current - WINDOW, current + WINDOW + 1):
        if hmac.compare_digest(code_at(secret, step), code) and step > last_step and found is None:
            found = step
    return found


def otpauth_uri(username: str, secret: str) -> str:
    """The `otpauth://` URI an authenticator app reads from the QR code."""
    label = quote(f"{ISSUER}:{username}", safe=":")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP_S}")


# ── recovery codes ───────────────────────────────────────────────────────────

def normalize_recovery(code: str) -> str:
    return "".join(code.split()).replace("-", "").lower()


def new_recovery_codes() -> list[str]:
    codes = []
    for _ in range(RECOVERY_COUNT):
        raw = _b32(secrets.token_bytes(RECOVERY_BYTES)).lower()
        codes.append("-".join(raw[i:i + 4] for i in range(0, 16, 4)))
    return codes


def recovery_hash(code: str) -> str | None:
    """The stored form of a recovery code, or None when it can't be one."""
    normal = normalize_recovery(code)
    if not _RECOVERY.fullmatch(normal):
        return None
    return hashlib.sha256(normal.encode("ascii")).hexdigest()


# ── secrets at rest ──────────────────────────────────────────────────────────

def key_path() -> Path:
    return Path(settings.auth_dir) / KEY_FILE


def _key(create: bool) -> bytes | None:
    """The sealing key; created on first need (0600, exclusive create, so the
    API and the operator command can't both write one). None when it doesn't
    exist and `create` is false, or the file isn't a 32-byte key."""
    path = key_path()
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        if not create:
            return None
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        key = secrets.token_bytes(32)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raw = path.read_bytes()        # another process created it first
        else:
            with os.fdopen(fd, "wb") as out:
                out.write(key)
                out.flush()
                os.fsync(out.fileno())
            return key
    return raw if len(raw) == 32 else None


def _aad(user_id: int) -> bytes:
    return b"rag-mfa:%d" % user_id


def seal(user_id: int, secret: str) -> str:
    key = _key(create=True)
    if key is None:
        raise RuntimeError(f"{key_path()} is not a 32-byte key")
    nonce = secrets.token_bytes(_NONCE)
    sealed = AESGCM(key).encrypt(nonce, secret.encode("ascii"), _aad(user_id))
    return base64.b64encode(nonce + sealed).decode("ascii")


def unseal(user_id: int, sealed: str) -> str | None:
    """The secret, or None when it can't be read (no key, another key, tampered)."""
    key = _key(create=False)
    if key is None:
        return None
    try:
        raw = base64.b64decode(sealed, validate=True)
        return AESGCM(key).decrypt(raw[:_NONCE], raw[_NONCE:], _aad(user_id)).decode("ascii")
    except Exception:                                  # noqa: BLE001 - any failure means "can't verify"
        return None
