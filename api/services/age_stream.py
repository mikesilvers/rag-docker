"""Encryption of export packages in the age v1 format (#27, #676).

An encrypted package is an ordinary age file, so `age -d` opens it as well as
import does. It has one recipient, of one of two kinds:

- a new X25519 key per package (#676, the default): one `X25519` stanza. The
  secret key is written once, to the package's key file, in age's own identity
  file layout (`# created:`, `# public key: age1...`, `AGE-SECRET-KEY-1...`),
  so `age -d -i <package>.key` opens it too;
- a passphrase (#27): one `scrypt` stanza.

The format is the C2SP age specification (https://c2sp.org/age); this module
implements only the parts these files use, on the `cryptography` package the
API already locks. Keys and recipients are Bech32 (BIP 173), as age writes them.
age's post-quantum hybrid recipients (`mlkem768x25519`, `AGE-SECRET-KEY-PQ-1`)
are not supported: export writes X25519 only, and import refuses a PQ key file.
A passphrase file looks like this:

    age-encryption.org/v1
    -> scrypt <salt, base64> <work factor>
    <wrapped file key, base64>
    --- <header MAC, base64>
    <16-byte nonce><STREAM payload: 64 KiB chunks, ChaCha20-Poly1305>

A key file's package has `-> X25519 <ephemeral share, base64>` and the wrapped
file key in place of the scrypt stanza; everything after the stanza is the same.

pyrage was not used: its passphrase API takes and returns whole byte strings,
and a package can be several GB. Both directions here stream: the encryptor is a
writable file object tarfile writes into, and the decryptor a readable one
tarfile reads from, holding one chunk at a time.

Every rule the parser enforces is one the age testkit (C2SP CCTV, vendored in
scripts/tests/fixtures/age-testkit) has a vector for; the tests run all of them.
"""
from __future__ import annotations

import base64
import binascii
import hmac
import io
import os
import re
import threading
from dataclasses import dataclass
from hashlib import sha256
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

ENCRYPTED_SUFFIX = ".tar.gz.age"
VERSION_LINE = b"age-encryption.org/v1"
CHUNK = 64 * 1024
_TAG = 16
_FILE_KEY = 16
_NONCE = 16
# age's default for new passphrase files: about 256 MiB of memory (128 * r * N)
# and about a second of work. Import accepts at most 20 (about 1 GiB; the
# maintainer's limit on #27, below age's own 22, which needs about 4 GiB and over
# a minute), and refuses more without deriving anything.
WORK_FACTOR = 18
MAX_WORK_FACTOR = 20
_SCRYPT_LABEL = b"age-encryption.org/v1/scrypt"
_X25519_LABEL = b"age-encryption.org/v1/X25519"
# The longest key file import reads (POST /import and the loader): age's own is
# about 190 characters.
KEY_FILE_MAX = 4096
# A passphrase file's header is about 150 bytes. Reading stops well before a
# hostile file could make the parser hold a large one.
HEADER_MAX = 64 * 1024

_B64 = re.compile(rb"^[A-Za-z0-9+/]*$")
# A plain decimal, no sign or leading zero; anything else is a malformed header
# (#549). Every accepted value (1 to 20) has one or two digits, so a longer one
# is refused as above the limit before int(), which raises on a very long one
# (Python's int string-length limit): no field can reach an unhandled error.
_WORK_FACTOR_TEXT = re.compile(r"[1-9][0-9]*")
# One scrypt derivation at a time in this process: a hostile file at the work
# factor limit needs about 1 GiB, and concurrent imports must not multiply that.
_scrypt_lock = threading.Lock()


class AgeError(Exception):
    """A file that can't be decrypted. `kind` says why, without any secret:

    header      not a valid age header (or not an age file at all), a scrypt
                work factor that is not a decimal without a sign or leading
                zero included (#549); a malformed X25519 stanza, one whose
                share gives an all-zero secret, or a scrypt stanza next to it
    recipient   a valid age header, but not for a passphrase
    key         an X25519 key that unwraps none of the file's X25519 stanzas
    identity    a key file that isn't one valid age X25519 secret key
    work_factor the scrypt work factor is above MAX_WORK_FACTOR, a decimal of
                more than two digits included
    nonce       the file ends inside the 16-byte payload nonce
    passphrase  the passphrase does not unwrap the file key
    hmac        the header MAC does not match: the header was changed
    payload     a payload chunk failed authentication, or the payload is
                truncated or has data after its final chunk
    """

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def is_encrypted_name(name: str) -> bool:
    """Encrypted packages are recognised by name first (#27, decision 7)."""
    return name.endswith(ENCRYPTED_SUFFIX)


# ── Header ────────────────────────────────────────────────────────────────────

@dataclass
class Stanza:
    args: list[str]
    body: bytes


@dataclass
class Header:
    stanzas: list[Stanza]
    mac_input: bytes      # everything up to and including "---"
    mac: bytes


def _b64decode(text: bytes) -> bytes:
    """Canonical, unpadded standard base64, as the age header requires."""
    if not _B64.match(text) or len(text) % 4 == 1:
        raise AgeError("header", "invalid base64 in the age header")
    try:
        data = base64.b64decode(text + b"=" * (-len(text) % 4), validate=True)
    except binascii.Error:
        raise AgeError("header", "invalid base64 in the age header") from None
    # Unused trailing bits must be zero: re-encoding has to give the same text.
    if base64.b64encode(data).rstrip(b"=") != text:
        raise AgeError("header", "non-canonical base64 in the age header")
    return data


def _b64encode(data: bytes) -> bytes:
    return base64.b64encode(data).rstrip(b"=")


def read_header(fh: BinaryIO) -> Header:
    """Parse an age header from `fh`, leaving it at the first payload byte.

    Lines end in LF only. A stanza is `-> arg...` then its body in lines of
    exactly 64 base64 characters ending with one shorter line (possibly empty).
    The header ends with `--- <MAC>`.
    """
    consumed = 0

    def line() -> bytes:
        nonlocal consumed
        raw = fh.readline(HEADER_MAX - consumed + 1)
        consumed += len(raw)
        if consumed > HEADER_MAX:
            raise AgeError("header", "the age header is too long")
        if not raw.endswith(b"\n"):
            raise AgeError("header", "the age header ends early")
        return raw[:-1]

    first = fh.readline(len(VERSION_LINE) + 1)
    consumed += len(first)
    if first != VERSION_LINE + b"\n":
        raise AgeError("header", "not an age v1 file")
    mac_input = bytearray(first)
    stanzas: list[Stanza] = []
    current = line()
    while True:
        if current.startswith(b"---"):
            if not current.startswith(b"--- "):
                raise AgeError("header", "malformed age header MAC line")
            mac = _b64decode(current[4:])
            if len(mac) != 32:
                raise AgeError("header", "malformed age header MAC")
            mac_input += b"---"
            break
        if not current.startswith(b"->"):
            raise AgeError("header", "malformed age header line")
        parts = current[2:].split(b" ")
        # "->" is followed by one or more " <argument>": the first split part is
        # the empty string before the first space.
        if len(parts) < 2 or parts[0] != b"" or any(
                not p or any(c < 0x21 or c > 0x7E for c in p) for p in parts[1:]):
            raise AgeError("header", "malformed age stanza arguments")
        mac_input += current + b"\n"
        body = bytearray()
        while True:
            body_line = line()
            mac_input += body_line + b"\n"
            if len(body_line) > 64:
                raise AgeError("header", "age stanza body line too long")
            body += _b64decode(body_line)
            if len(body_line) < 64:
                break
        stanzas.append(Stanza([p.decode("ascii") for p in parts[1:]], bytes(body)))
        current = line()
    if not stanzas:
        raise AgeError("header", "the age header has no recipient stanza")
    return Header(stanzas, bytes(mac_input), mac)


def scrypt_stanza(header: Header) -> tuple[bytes, int, bytes]:
    """The salt, work factor and wrapped key of the header's one scrypt stanza.

    A passphrase file has exactly one stanza, of type `scrypt`; age refuses a
    scrypt stanza next to any other, since anyone holding the other key could
    otherwise change the file without the passphrase. A work factor of one or
    two digits is returned unchecked against MAX_WORK_FACTOR; `unwrap_file_key`
    checks it. A longer decimal is refused here, before int().
    """
    types = [s.args[0] for s in header.stanzas]
    if "scrypt" in types and len(types) > 1:
        raise AgeError("header", "a scrypt stanza must be the only stanza")
    if types != ["scrypt"]:
        raise AgeError("recipient", "the age file is not passphrase-encrypted")
    stanza = header.stanzas[0]
    if len(stanza.args) != 3:
        raise AgeError("header", "malformed scrypt stanza")
    salt = _b64decode(stanza.args[1].encode("ascii"))
    if len(salt) != 16:
        raise AgeError("header", "malformed scrypt salt")
    field = stanza.args[2]
    if not _WORK_FACTOR_TEXT.fullmatch(field):
        raise AgeError("header", "malformed scrypt work factor: not a decimal number "
                       "without a sign or leading zero")
    if len(field) > 2:
        raise AgeError("work_factor", "the scrypt work factor is above the limit of "
                       f"{MAX_WORK_FACTOR}")
    if len(stanza.body) != _FILE_KEY + _TAG:
        raise AgeError("header", "malformed scrypt stanza body")
    return salt, int(field), stanza.body


def _scrypt(passphrase: bytes, salt: bytes, work_factor: int) -> bytes:
    with _scrypt_lock:
        return Scrypt(salt=_SCRYPT_LABEL + salt, length=32, n=2 ** work_factor,
                      r=8, p=1).derive(passphrase)


def unwrap_file_key(header: Header, passphrase: str) -> bytes:
    salt, work_factor, body = scrypt_stanza(header)
    if work_factor > MAX_WORK_FACTOR:
        raise AgeError("work_factor",
                       f"the scrypt work factor {work_factor} is above the limit of "
                       f"{MAX_WORK_FACTOR}")
    key = _scrypt(passphrase.encode("utf-8"), salt, work_factor)
    try:
        return ChaCha20Poly1305(key).decrypt(b"\x00" * 12, body, None)
    except InvalidTag:
        raise AgeError("passphrase", "the passphrase does not decrypt this file") from None


def _hkdf(key: bytes, salt: bytes | None, info: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=salt, info=info).derive(key)


def _header_mac(file_key: bytes, mac_input: bytes) -> bytes:
    return hmac.new(_hkdf(file_key, None, b"header"), mac_input, sha256).digest()


def verify_mac(header: Header, file_key: bytes) -> None:
    if not hmac.compare_digest(_header_mac(file_key, header.mac_input), header.mac):
        raise AgeError("hmac", "the age header MAC does not match")


def header_kind(fh: BinaryIO) -> str | None:
    """`"passphrase"` when `fh` starts with a valid age passphrase header, `"key"`
    when it starts with a valid header with an `X25519` stanza and no `scrypt`
    (#676), else None. No scrypt or X25519 operation runs, and the work factor
    is not held against the limit: for the listing and for choosing which
    secret an import needs.

    Any failure reading or parsing the file is None, never an exception: one
    bad file must not break the listing of the others (#27 review).
    """
    try:
        header = read_header(fh)
        types = [s.args[0] for s in header.stanzas]
        if "scrypt" in types:
            scrypt_stanza(header)
            return "passphrase"
        return "key" if "X25519" in types else None
    except Exception:                                 # noqa: BLE001 - see above
        return None


def check_header(fh: BinaryIO) -> bool:
    """True when `fh` starts with a valid passphrase or key header (`header_kind`)."""
    return header_kind(fh) is not None


# ── X25519 keys (#676) ────────────────────────────────────────────────────────

_BECH32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_REV = {c: i for i, c in enumerate(_BECH32)}


def _polymod(values) -> int:
    gen = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
    chk = 1
    for v in values:
        top = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (top >> i) & 1 else 0
    return chk


def _hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def _convert(data, frombits: int, tobits: int, pad: bool) -> list[int]:
    acc = bits = 0
    out = []
    maxv = (1 << tobits) - 1
    for value in data:
        acc = (acc << frombits) | value
        bits += frombits
        while bits >= tobits:
            bits -= tobits
            out.append((acc >> bits) & maxv)
    if pad:
        if bits:
            out.append((acc << (tobits - bits)) & maxv)
    elif bits >= frombits or (acc << (tobits - bits)) & maxv:
        raise ValueError("invalid Bech32 padding")
    return out


def _bech32_encode(hrp: str, data: bytes) -> str:
    """Bech32 (BIP 173), lowercase, with no length limit, as age writes keys."""
    words = _convert(data, 8, 5, True)
    values = _hrp_expand(hrp) + words
    poly = _polymod(values + [0] * 6) ^ 1
    checksum = [(poly >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(_BECH32[w] for w in words + checksum)


def _bech32_decode(text: str) -> tuple[str, bytes]:
    """The HRP (lowercase) and the data of a Bech32 string. Raises ValueError,
    never naming the input, for mixed case, a bad character or checksum."""
    if text.lower() != text and text.upper() != text:
        raise ValueError("mixed-case Bech32")
    text = text.lower()
    pos = text.rfind("1")
    if pos < 1 or pos + 7 > len(text) or any(ord(c) < 33 or ord(c) > 126 for c in text):
        raise ValueError("malformed Bech32")
    hrp, rest = text[:pos], text[pos + 1:]
    try:
        values = [_BECH32_REV[c] for c in rest]
    except KeyError:
        raise ValueError("invalid Bech32 character") from None
    if _polymod(_hrp_expand(hrp) + values) != 1:
        raise ValueError("invalid Bech32 checksum")
    return hrp, bytes(_convert(values[:-6], 5, 8, False))


_RAW = serialization.Encoding.Raw


class Identity:
    """An age X25519 secret key. Its repr never shows the key, and nothing
    returns its text except `encode()`, which only the key file uses."""

    __slots__ = ("_key",)

    def __init__(self, key: X25519PrivateKey):
        self._key = key

    def __repr__(self) -> str:
        return "Identity(<redacted>)"

    @property
    def public_bytes(self) -> bytes:
        return self._key.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw)

    @property
    def recipient(self) -> str:
        """The `age1...` recipient: the public key, which is not secret."""
        return _bech32_encode("age", self.public_bytes)

    def encode(self) -> str:
        """The `AGE-SECRET-KEY-1...` line of the key file."""
        raw = self._key.private_bytes(_RAW, serialization.PrivateFormat.Raw,
                                      serialization.NoEncryption())
        return _bech32_encode("age-secret-key-", raw).upper()

    def exchange(self, share: bytes) -> bytes:
        return self._key.exchange(X25519PublicKey.from_public_bytes(share))


def generate_identity() -> Identity:
    """A new X25519 key pair: one per package (#676)."""
    return Identity(X25519PrivateKey.generate())


def parse_identity(text: str) -> Identity:
    """The key in the contents of an age X25519 identity file (#676).

    Blank lines and `#` comments are skipped, and CRLF line ends are accepted.
    Exactly one other line must remain: one `AGE-SECRET-KEY-1...` key. Anything
    else, a post-quantum `AGE-SECRET-KEY-PQ-1...` key included, raises
    AgeError("identity"), whose message never contains the text.
    """
    bad = AgeError("identity", "the key file is not a valid age X25519 key")
    if not isinstance(text, str) or len(text) > KEY_FILE_MAX:
        raise bad
    lines = [line[:-1] if line.endswith("\r") else line for line in text.split("\n")]
    keys = [line for line in lines if line.strip() and not line.startswith("#")]
    if len(keys) != 1 or not keys[0].startswith("AGE-SECRET-KEY-1"):
        raise bad
    try:
        hrp, data = _bech32_decode(keys[0])
    except ValueError:
        raise bad from None
    if hrp != "age-secret-key-" or len(data) != 32:
        raise bad
    return Identity(X25519PrivateKey.from_private_bytes(data))


def identity_file(identity: Identity, created, package: str) -> str:
    """The key file's text, in age's identity file layout, so `age -d -i` reads
    it. `created` is a UTC datetime."""
    return (f"# created: {created.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
            f"# public key: {identity.recipient}\n"
            f"# The key of {package}. Store it apart from the package: anyone with "
            "both can read it.\n"
            f"{identity.encode()}\n")


def _x25519_stanza(file_key: bytes, recipient: bytes) -> bytes:
    """The `X25519` stanza wrapping `file_key` to the raw public key `recipient`."""
    ephemeral = X25519PrivateKey.generate()
    share = ephemeral.public_key().public_bytes(_RAW, serialization.PublicFormat.Raw)
    secret = ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient))
    if secret == bytes(32):
        raise ValueError("the recipient is a low-order point")
    wrap = _hkdf(secret, share + recipient, _X25519_LABEL)
    body = ChaCha20Poly1305(wrap).encrypt(b"\x00" * 12, file_key, None)
    return b"-> X25519 " + _b64encode(share) + b"\n" + _b64encode(body) + b"\n"


def x25519_unwrap(header: Header, identity: Identity) -> bytes:
    """The file key from the first `X25519` stanza `identity` opens.

    Stanzas are taken in order, as age does: other stanza types are skipped, a
    malformed X25519 stanza met first is a header failure, and one that doesn't
    open is passed over. A scrypt stanza must be alone (`scrypt_stanza`).
    """
    if "scrypt" in [s.args[0] for s in header.stanzas]:
        raise AgeError("header", "a scrypt stanza must be the only stanza")
    ours = identity.public_bytes
    for stanza in header.stanzas:
        if stanza.args[0] != "X25519":
            continue
        if len(stanza.args) != 2:
            raise AgeError("header", "malformed X25519 stanza")
        share = _b64decode(stanza.args[1].encode("ascii"))
        if len(share) != 32 or len(stanza.body) != _FILE_KEY + _TAG:
            raise AgeError("header", "malformed X25519 stanza")
        try:
            secret = identity.exchange(share)
        except ValueError:
            secret = bytes(32)
        if secret == bytes(32):
            raise AgeError("header", "the X25519 share gives an all-zero secret")
        try:
            return ChaCha20Poly1305(_hkdf(secret, share + ours, _X25519_LABEL)).decrypt(
                b"\x00" * 12, stanza.body, None)
        except InvalidTag:
            continue
    raise AgeError("key", "the key does not decrypt this file")


# ── Payload (STREAM) ──────────────────────────────────────────────────────────

def _chunk_nonce(counter: int, last: bool) -> bytes:
    return counter.to_bytes(11, "big") + (b"\x01" if last else b"\x00")


class Decryptor(io.RawIOBase):
    """The authenticated plaintext of an age payload, as a readable stream.

    Only plaintext from chunks that passed authentication is ever returned. The
    rules follow the reference implementation, which the testkit encodes: a
    short chunk is the last; a full chunk is tried as a middle chunk, then as
    the last; the last chunk may be empty only when it is the only one; nothing
    may follow it. A payload that breaks any of these raises
    `AgeError("payload")` once the plaintext authenticated before it is read.
    """

    def __init__(self, fh: BinaryIO, file_key: bytes):
        super().__init__()
        self._fh = fh
        nonce = self._read(_NONCE)
        if len(nonce) != _NONCE:
            # The testkit counts the nonce as part of the header; for import it
            # is a truncated file, after a valid header (#27 review).
            raise AgeError("nonce", "the age payload nonce is missing")
        self._aead = ChaCha20Poly1305(_hkdf(file_key, nonce, b"payload"))
        self._counter = 0
        self._plain = memoryview(b"")
        self._done = False
        self._error: AgeError | None = None

    def _read(self, n: int) -> bytes:
        data = bytearray()
        while len(data) < n:
            block = self._fh.read(n - len(data))
            if not block:
                break
            data += block
        return bytes(data)

    def _next_chunk(self) -> bytes:
        data = self._read(CHUNK + _TAG)
        if not data:
            raise AgeError("payload", "the age payload is truncated: its last chunk is missing")
        last = len(data) < CHUNK + _TAG
        if last and len(data) == _TAG and self._counter > 0:
            raise AgeError("payload", "the age payload ends with an empty chunk")
        try:
            plain = self._aead.decrypt(_chunk_nonce(self._counter, last), data, None)
        except InvalidTag:
            plain = None
        if plain is None and not last:
            # A full-length chunk can be the last one.
            last = True
            try:
                plain = self._aead.decrypt(_chunk_nonce(self._counter, True), data, None)
            except InvalidTag:
                pass
        if plain is None:
            raise AgeError("payload", "an age payload chunk failed authentication")
        self._counter += 1
        if last:
            self._done = True
            if self._fh.read(1):
                # Released first: the chunk itself was authentic.
                self._error = AgeError("payload", "the age file has data after its last chunk")
        return plain

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        while not self._plain:
            if self._error is not None:
                raise self._error
            if self._done:
                return 0
            self._plain = memoryview(self._next_chunk())
        n = min(len(buffer), len(self._plain))
        buffer[:n] = self._plain[:n]
        self._plain = self._plain[n:]
        return n

    def drain(self) -> None:
        """Read and authenticate the rest of the payload, to its last chunk."""
        self._plain = memoryview(b"")
        while not self._done:
            self._next_chunk()
        if self._error is not None:
            raise self._error


def open_decrypt(fh: BinaryIO, passphrase: str | None = None, *,
                 identity: Identity | None = None) -> Decryptor:
    """Parse the header, unwrap the key with the passphrase or the X25519
    identity (exactly one), check the MAC; then stream the payload."""
    if (passphrase is None) == (identity is None):
        raise ValueError("open_decrypt takes a passphrase or an identity")
    header = read_header(fh)
    if identity is not None:
        file_key = x25519_unwrap(header, identity)
    else:
        file_key = unwrap_file_key(header, passphrase)
    verify_mac(header, file_key)
    return Decryptor(fh, file_key)


class Encryptor(io.RawIOBase):
    """Writes an age file to `fh` from the plaintext written to it, for a
    passphrase or for an X25519 recipient (`recipient`, the raw 32-byte public
    key; #676): exactly one of them.

    The last chunk carries the final flag, so a full chunk is held back until
    either more data or `close()` shows whether it is the last. `close()` writes
    it and leaves `fh` open: the caller owns that file.
    """

    def __init__(self, fh: BinaryIO, passphrase: str | None = None, work_factor: int | None = None,
                 *, recipient: bytes | None = None):
        super().__init__()
        if (passphrase is None) == (recipient is None):
            raise ValueError("Encryptor takes a passphrase or a recipient")
        self._fh = fh
        file_key = os.urandom(_FILE_KEY)
        if recipient is not None:
            stanza = _x25519_stanza(file_key, recipient)
        else:
            work_factor = WORK_FACTOR if work_factor is None else work_factor
            salt = os.urandom(16)
            key = _scrypt(passphrase.encode("utf-8"), salt, work_factor)
            wrapped = ChaCha20Poly1305(key).encrypt(b"\x00" * 12, file_key, None)
            stanza = (b"-> scrypt " + _b64encode(salt) + b" " + str(work_factor).encode() + b"\n"
                      + _b64encode(wrapped) + b"\n")   # 43 characters: one short line
        mac_input = VERSION_LINE + b"\n" + stanza + b"---"
        nonce = os.urandom(_NONCE)
        fh.write(mac_input + b" " + _b64encode(_header_mac(file_key, mac_input)) + b"\n" + nonce)
        self._aead = ChaCha20Poly1305(_hkdf(file_key, nonce, b"payload"))
        self._counter = 0
        self._buf = bytearray()

    def _emit(self, plain: bytes, last: bool) -> None:
        self._fh.write(self._aead.encrypt(_chunk_nonce(self._counter, last), plain, None))
        self._counter += 1

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        if self.closed:
            raise ValueError("write to a closed Encryptor")
        view = memoryview(data).cast("B")
        self._buf += view
        # Strictly more than a chunk: a chunk that may be the last is kept.
        while len(self._buf) > CHUNK:
            self._emit(bytes(self._buf[:CHUNK]), last=False)
            del self._buf[:CHUNK]
        return len(view)

    def close(self) -> None:
        if not self.closed:
            self._emit(bytes(self._buf), last=True)
            self._buf = bytearray()
        super().close()
