"""Passphrase encryption of export packages in the age v1 format (#27).

An encrypted package is an ordinary age file with one `scrypt` (passphrase)
recipient, so `age -d` opens it as well as import does. The format is the C2SP
age specification (https://c2sp.org/age); this module implements only the parts
a passphrase file uses, on the `cryptography` package the API already locks:

    age-encryption.org/v1
    -> scrypt <salt, base64> <work factor>
    <wrapped file key, base64>
    --- <header MAC, base64>
    <16-byte nonce><STREAM payload: 64 KiB chunks, ChaCha20-Poly1305>

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
from cryptography.hazmat.primitives import hashes
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
# A passphrase file's header is about 150 bytes. Reading stops well before a
# hostile file could make the parser hold a large one.
HEADER_MAX = 64 * 1024

_B64 = re.compile(rb"^[A-Za-z0-9+/]*$")
# One or two digits, no sign or leading zero: every accepted value (1 to 20) fits,
# and a longer field is refused before int(), which raises on a very long one
# (Python's int string-length limit), so no field can reach an unhandled error.
_WORK_FACTOR_TEXT = re.compile(r"^[1-9][0-9]?$")
# One scrypt derivation at a time in this process: a hostile file at the work
# factor limit needs about 1 GiB, and concurrent imports must not multiply that.
_scrypt_lock = threading.Lock()


class AgeError(Exception):
    """A file that can't be decrypted. `kind` says why, without any secret:

    header      not a valid age header (or not an age file at all)
    recipient   a valid age header, but not for a passphrase
    work_factor the scrypt work factor is above MAX_WORK_FACTOR, or is not one or
                two decimal digits without a sign or leading zero
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
    otherwise change the file without the passphrase. The work factor is
    returned unchecked against MAX_WORK_FACTOR; `unwrap_file_key` checks it.
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
    if not _WORK_FACTOR_TEXT.match(stanza.args[2]):
        raise AgeError("work_factor", "the scrypt work factor is not one or two decimal "
                       "digits without a leading zero")
    if len(stanza.body) != _FILE_KEY + _TAG:
        raise AgeError("header", "malformed scrypt stanza body")
    return salt, int(stanza.args[2]), stanza.body


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


def check_header(fh: BinaryIO) -> bool:
    """True when `fh` starts with a valid age passphrase header. No scrypt runs,
    and the work factor is not held against the limit; for the listing.

    Any failure reading or parsing the file is False, never an exception: one
    bad file must not break the listing of the others (#27 review).
    """
    try:
        scrypt_stanza(read_header(fh))
    except Exception:                                 # noqa: BLE001 - see above
        return False
    return True


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


def open_decrypt(fh: BinaryIO, passphrase: str) -> Decryptor:
    """Parse the header, unwrap the key, check the MAC; then stream the payload."""
    header = read_header(fh)
    file_key = unwrap_file_key(header, passphrase)
    verify_mac(header, file_key)
    return Decryptor(fh, file_key)


class Encryptor(io.RawIOBase):
    """Writes an age passphrase file to `fh` from the plaintext written to it.

    The last chunk carries the final flag, so a full chunk is held back until
    either more data or `close()` shows whether it is the last. `close()` writes
    it and leaves `fh` open: the caller owns that file.
    """

    def __init__(self, fh: BinaryIO, passphrase: str, work_factor: int | None = None):
        super().__init__()
        self._fh = fh
        work_factor = WORK_FACTOR if work_factor is None else work_factor
        file_key = os.urandom(_FILE_KEY)
        salt = os.urandom(16)
        key = _scrypt(passphrase.encode("utf-8"), salt, work_factor)
        wrapped = ChaCha20Poly1305(key).encrypt(b"\x00" * 12, file_key, None)
        mac_input = (VERSION_LINE + b"\n"
                     + b"-> scrypt " + _b64encode(salt) + b" " + str(work_factor).encode() + b"\n"
                     + _b64encode(wrapped) + b"\n"   # 43 characters: one short line
                     + b"---")
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
