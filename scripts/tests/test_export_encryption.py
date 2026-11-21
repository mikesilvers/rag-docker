"""Passphrase encryption of export packages in the age format (issue #27).

Covers `services/age_stream.py` against the C2SP CCTV age testkit (vendored in
scripts/tests/fixtures/age-testkit) and against an independent decryption
written here from the age specification; the encrypted export, import, listing
and passphrase refusals; and that a passphrase never reaches a job record, a log
line or an error response.

Runs with the API's dependencies, in the built API image with no network
(`05_transfer.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_export_encryption.py

Exports and uploads go to temporary folders; Weaviate is mocked and nothing
reaches a backend. scrypt runs at a low work factor except where a test checks
the default, so the file runs in seconds.
"""
import asyncio
import base64
import errno
import hashlib
import io
import json
import logging
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import uuid
import zlib
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from cryptography.hazmat.primitives import hashes, hmac as crypto_hmac
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
import anyio.from_thread
from fastapi.testclient import TestClient

from config import settings
from main import app
from auth import testing as auth_testing  # a real session in a temporary store (#25)
from services import age_stream, exporter, importer, packager, retrieval_config
from services import weaviate_client as wc

ROOT = Path(__file__).resolve().parents[2]
VECTORS = ROOT / "scripts" / "tests" / "fixtures" / "age-testkit"
PASSPHRASE = "correct horse battery staple"
LOW = 10            # work factor for tests: 1 MiB of scrypt memory
CHUNK = age_stream.CHUNK
STEM = "ragpkg-corpus-20261027T000000Z-0123abcd"
# The testkit's outcomes, and the AgeError kinds that mean each.
OUTCOME = {"header": "header failure", "work_factor": "header failure",
           "recipient": "no match", "passphrase": "no match", "key": "no match",
           "identity": "no match", "nonce": "header failure",
           "hmac": "HMAC failure", "payload": "payload failure"}


def encrypt(data, passphrase=PASSPHRASE, work_factor=LOW, pieces=None):
    out = io.BytesIO()
    enc = age_stream.Encryptor(out, passphrase, work_factor)
    if pieces is None:
        enc.write(data)
    else:
        for start in range(0, len(data), pieces):
            enc.write(data[start:start + pieces])
    enc.close()
    return out.getvalue()


def decrypt(blob, passphrase=PASSPHRASE):
    dec = age_stream.open_decrypt(io.BytesIO(blob), passphrase)
    return dec.read()


def independent_decrypt(blob, passphrase):
    """The age v1 passphrase format, decrypted straight from the specification
    with no code from age_stream, so the encryptor is checked against the
    format rather than against its own reader."""
    header_end = blob.index(b"\n---") + 4
    lines = blob[:header_end].split(b"\n")
    assert lines[0] == b"age-encryption.org/v1", lines[0]
    args = lines[1].split(b" ")
    assert args[:2] == [b"->", b"scrypt"] and len(args) == 4, args
    salt = base64.b64decode(args[2] + b"==")
    work_factor = int(args[3])
    body = base64.b64decode(lines[2] + b"=" * (-len(lines[2]) % 4))
    assert lines[3] == b"---"
    mac_end = blob.index(b"\n", header_end)
    mac = base64.b64decode(blob[header_end + 1:mac_end] + b"=")
    key = Scrypt(salt=b"age-encryption.org/v1/scrypt" + salt, length=32,
                 n=2 ** work_factor, r=8, p=1).derive(passphrase.encode())
    file_key = ChaCha20Poly1305(key).decrypt(bytes(12), body, None)
    hmac_key = HKDF(hashes.SHA256(), 32, None, b"header").derive(file_key)
    check = crypto_hmac.HMAC(hmac_key, hashes.SHA256())
    check.update(blob[:header_end])
    check.verify(mac)
    nonce = blob[mac_end + 1:mac_end + 17]
    aead = ChaCha20Poly1305(HKDF(hashes.SHA256(), 32, nonce, b"payload").derive(file_key))
    payload = blob[mac_end + 17:]
    chunks = [payload[i:i + CHUNK + 16] for i in range(0, len(payload), CHUNK + 16)] or [b""]
    plain = b""
    for counter, chunk in enumerate(chunks):
        last = counter == len(chunks) - 1
        plain += aead.decrypt(counter.to_bytes(11, "big") + (b"\x01" if last else b"\x00"), chunk, None)
    return plain, work_factor


def vectors():
    for path in sorted(VECTORS.iterdir()):
        if path.name in ("SOURCE", ".gitattributes"):
            continue
        raw = path.read_bytes()
        head, _, body = raw.partition(b"\n\n")
        meta, passphrases = {}, []
        for line in head.decode().splitlines():
            key, _, value = line.partition(": ")
            if key == "passphrase":
                passphrases.append(value)
            else:
                meta[key] = value
        if meta.get("compressed") == "zlib":
            body = zlib.decompress(body)
        yield path.name, meta, passphrases, body


# Audit hooks can't be removed once added: one is installed for the whole run and
# hands each event to the hooks a test has registered here.
_AUDIT_HOOKS = []
sys.addaudithook(lambda event, args: [hook(event, args) for hook in list(_AUDIT_HOOKS)])


def read_all(dec, released):
    while True:
        block = dec.read(1000)
        if not block:
            return
        released += block


class TestkitVectors(unittest.TestCase):
    """Every applicable CCTV vector decrypts, or fails, as it says."""

    def test_the_vendored_vectors_are_the_unarmored_set(self):
        names = [name for name, *_ in vectors()]
        self.assertEqual(len(names), 110)
        self.assertIn("scrypt", names)
        self.assertIn("stream_two_final_chunks", names)

    def test_passphrase_vectors(self):
        seen = 0
        for name, meta, passphrases, body in vectors():
            if not passphrases:
                continue
            seen += 1
            with self.subTest(vector=name):
                released, outcome = bytearray(), "success"
                try:
                    read_all(age_stream.open_decrypt(io.BytesIO(body), passphrases[0]), released)
                except age_stream.AgeError as exc:
                    outcome = OUTCOME[exc.kind]
                self.assertEqual(outcome, meta["expect"])
                if "payload" in meta:
                    self.assertEqual(hashlib.sha256(released).hexdigest(), meta["payload"])
        self.assertEqual(seen, 25)

    def test_header_mac_and_payload_vectors_with_their_file_key(self):
        """The header, MAC and STREAM layers, below the recipient, on every
        vector that tests them (the testkit's own suggestion)."""
        seen = 0
        for name, meta, passphrases, body in vectors():
            recipient_case = meta["expect"] == "no match" or (
                meta["expect"] == "header failure" and name.startswith(("x25519", "hybrid")))
            if passphrases or recipient_case:
                continue
            seen += 1
            with self.subTest(vector=name):
                released, outcome = bytearray(), "success"
                fh = io.BytesIO(body)
                try:
                    header = age_stream.read_header(fh)
                    file_key = bytes.fromhex(meta["file key"])
                    age_stream.verify_mac(header, file_key)
                    read_all(age_stream.Decryptor(fh, file_key), released)
                except age_stream.AgeError as exc:
                    outcome = OUTCOME[exc.kind]
                self.assertEqual(outcome, meta["expect"])
                if "payload" in meta:
                    self.assertEqual(hashlib.sha256(released).hexdigest(), meta["payload"])
        self.assertEqual(seen, 60)

    def test_files_for_other_recipients_are_refused_with_a_passphrase(self):
        for name, meta, passphrases, body in vectors():
            if passphrases:
                continue
            with self.subTest(vector=name), self.assertRaises(age_stream.AgeError) as caught:
                age_stream.open_decrypt(io.BytesIO(body), PASSPHRASE)
            self.assertIn(caught.exception.kind, ("header", "recipient"))


class RoundTrip(unittest.TestCase):
    SIZES = (0, 1, CHUNK - 1, CHUNK, CHUNK + 1, 2 * CHUNK, 3 * CHUNK + 5)

    def test_sizes_round_trip_through_both_decryptions(self):
        for size in self.SIZES:
            data = os.urandom(size)
            for pieces in (None, 1000, CHUNK + 7):
                with self.subTest(size=size, pieces=pieces):
                    blob = encrypt(data, pieces=pieces)
                    self.assertEqual(decrypt(blob), data)
                    self.assertEqual(independent_decrypt(blob, PASSPHRASE), (data, LOW))
                    chunks = max(1, -(-size // CHUNK))
                    self.assertEqual(len(blob) - blob.index(b"\n", blob.index(b"\n---") + 1) - 1,
                                     16 + size + 16 * chunks)

    def test_any_unicode_passphrase(self):
        phrase = "pass phrase \u00e9\u4e2d\u6587 \U0001F512 long enough"
        self.assertEqual(decrypt(encrypt(b"x", passphrase=phrase), phrase), b"x")
        self.assertEqual(independent_decrypt(encrypt(b"y", passphrase=phrase), phrase)[0], b"y")

    def test_each_file_has_fresh_randomness(self):
        self.assertNotEqual(encrypt(b"same"), encrypt(b"same"))

    def test_export_uses_age_default_work_factor_18(self):
        derived = []
        real = age_stream._scrypt

        def spy(passphrase, salt, work_factor):
            derived.append(work_factor)
            return real(passphrase, salt, LOW)
        out = io.BytesIO()
        with patch.object(age_stream, "_scrypt", spy):
            enc = age_stream.Encryptor(out, PASSPHRASE)
            enc.close()
        self.assertEqual(derived, [18])
        self.assertEqual(out.getvalue().split(b"\n")[1].split(b" ")[-1], b"18")

    def test_damage_is_refused_by_kind(self):
        data = os.urandom(2 * CHUNK + 100)
        blob = encrypt(data)
        payload_at = blob.index(b"\n", blob.index(b"\n---") + 1) + 1 + 16
        cases = {
            "wrong passphrase": (blob, "passphrase", "not the passphrase"),
            "flipped payload byte": (blob[:payload_at + 5] + bytes([blob[payload_at + 5] ^ 1]) + blob[payload_at + 6:], "payload", PASSPHRASE),
            "last byte cut": (blob[:-1], "payload", PASSPHRASE),
            "final chunk dropped": (blob[:payload_at + 2 * (CHUNK + 16)], "payload", PASSPHRASE),
            "trailing byte": (blob + b"\x00", "payload", PASSPHRASE),
            "changed MAC": (self.changed_mac(blob), "hmac", PASSPHRASE),
            "not age": (b"\x1f\x8b" + blob[2:], "header", PASSPHRASE),
            "empty file": (b"", "header", PASSPHRASE),
        }
        for name, (damaged, kind, phrase) in cases.items():
            with self.subTest(case=name), self.assertRaises(age_stream.AgeError) as caught:
                decrypt(damaged, phrase)
            self.assertEqual(caught.exception.kind, kind)

    @staticmethod
    def changed_mac(blob):
        start = blob.index(b"\n--- ") + 5
        mac = base64.b64decode(blob[start:start + 43] + b"=")
        other = base64.b64encode(bytes([mac[0] ^ 1]) + mac[1:]).rstrip(b"=")
        return blob[:start] + other + blob[start + 43:]

    def test_the_import_work_factor_limit_is_20(self):
        self.assertEqual(age_stream.MAX_WORK_FACTOR, 20)

    def test_a_work_factor_above_20_is_refused_before_any_derivation(self):
        blob = encrypt(b"data")
        line = blob.split(b"\n")[1]
        for factor in (b"21", b"22", b"23", b"64", b"99999999999999999999"):
            hostile = blob.replace(line, line.rsplit(b" ", 1)[0] + b" " + factor, 1)
            with self.subTest(factor=factor), \
                 patch.object(age_stream, "_scrypt", side_effect=AssertionError("scrypt ran")), \
                 self.assertRaises(age_stream.AgeError) as caught:
                decrypt(hostile)
            self.assertEqual(caught.exception.kind, "work_factor")

    def test_a_malformed_or_overlong_work_factor_is_refused_before_int(self):
        """A field of thousands of digits once made int() raise ValueError, which
        broke the listing and turned an import into IMPORT_FAILED (#27 review).
        A field that is not a decimal without a sign or leading zero is a malformed
        header; a longer decimal is above the limit (#549)."""
        blob = encrypt(b"data")
        line = blob.split(b"\n")[1]
        for factor, kind in ((b"9" * 5000, "work_factor"), (b"100", "work_factor"),
                             (b"018", "header"), (b"+18", "header"), (b"0x12", "header"),
                             (b"1e1", "header"), (b"0", "header")):
            hostile = blob.replace(line, line.rsplit(b" ", 1)[0] + b" " + factor, 1)
            with self.subTest(factor=factor[:12]), \
                 patch.object(age_stream, "_scrypt", side_effect=AssertionError("scrypt ran")), \
                 self.assertRaises(age_stream.AgeError) as caught:
                decrypt(hostile)
            self.assertEqual(caught.exception.kind, kind)
            self.assertFalse(age_stream.check_header(io.BytesIO(hostile)))

    def test_check_header_never_raises(self):
        with patch.object(age_stream, "read_header", side_effect=RuntimeError("unexpected")):
            self.assertFalse(age_stream.check_header(io.BytesIO(encrypt(b"x"))))

    def test_a_work_factor_of_20_still_reaches_derivation(self):
        blob = encrypt(b"data")
        line = blob.split(b"\n")[1]
        at_limit = blob.replace(line, line.rsplit(b" ", 1)[0] + b" 20", 1)
        derived = []
        with patch.object(age_stream, "_scrypt",
                          side_effect=lambda p, salt, wf: derived.append(wf) or bytes(32)), \
             self.assertRaises(age_stream.AgeError) as caught:
            decrypt(at_limit)
        # A stand-in key, so the stanza doesn't unwrap; what matters is that it ran.
        self.assertEqual((derived, caught.exception.kind), ([20], "passphrase"))

    def test_scrypt_derivations_run_one_at_a_time(self):
        active, peak, lock = [0], [0], threading.Lock()

        class Slow:
            def __init__(self, **_kwargs):
                pass

            def derive(self, _passphrase):
                with lock:
                    active[0] += 1
                    peak[0] = max(peak[0], active[0])
                time.sleep(0.05)
                with lock:
                    active[0] -= 1
                return bytes(32)
        with patch.object(age_stream, "Scrypt", Slow):
            threads = [threading.Thread(target=age_stream._scrypt, args=(b"p", bytes(16), 22))
                       for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(peak[0], 1)

    def test_listing_header_check_runs_no_scrypt(self):
        blob = encrypt(b"data")
        with patch.object(age_stream, "_scrypt", side_effect=AssertionError("scrypt ran")):
            self.assertTrue(age_stream.check_header(io.BytesIO(blob)))
            self.assertFalse(age_stream.check_header(io.BytesIO(b"age-encryption.org/v1\n-> X25519 abc\n")))
            self.assertFalse(age_stream.check_header(io.BytesIO(b"not age")))


def records(count=3, width=4):
    return [{"id": str(uuid.UUID(int=i + 1)), "vector": [0.1 * (i + 1)] * width,
             "properties": {"content": f"chunk {i}", "source_file": "doc.txt", "chunk_index": i},
             "source_sha256": None} for i in range(count)]


_BECH32_TEST = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def bech32_data(text):
    """Bech32 (BIP 173) data bytes of `text`, written here from the BIP, with no
    code from age_stream; the checksum is verified."""
    text = text.lower()
    hrp, rest = text[:text.rfind("1")], text[text.rfind("1") + 1:]
    values = [_BECH32_TEST.index(c) for c in rest]
    chk = 1
    for v in [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + values:
        top, chk = chk >> 25, (chk & 0x1FFFFFF) << 5 ^ v
        for i, g in enumerate((0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)):
            chk ^= g if (top >> i) & 1 else 0
    assert chk == 1, "bad checksum"
    bits = "".join(format(v, "05b") for v in values[:-6])
    return hrp, bytes(int(bits[i:i + 8], 2) for i in range(0, len(bits) - len(bits) % 8, 8))


def independent_x25519_decrypt(blob, key_line):
    """The age v1 X25519 format, decrypted straight from the specification with
    no code from age_stream (#676)."""
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
    from cryptography.hazmat.primitives import serialization
    hrp, raw = bech32_data(key_line)
    assert hrp == "age-secret-key-" and len(raw) == 32
    private = X25519PrivateKey.from_private_bytes(raw)
    ours = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    header_end = blob.index(b"\n---") + 4
    lines = blob[:header_end].split(b"\n")
    assert lines[0] == b"age-encryption.org/v1", lines[0]
    args = lines[1].split(b" ")
    assert args[:2] == [b"->", b"X25519"] and len(args) == 3, args
    share = base64.b64decode(args[2] + b"=")
    body = base64.b64decode(lines[2] + b"=")
    secret = private.exchange(X25519PublicKey.from_public_bytes(share))
    wrap = HKDF(hashes.SHA256(), 32, share + ours, b"age-encryption.org/v1/X25519").derive(secret)
    file_key = ChaCha20Poly1305(wrap).decrypt(bytes(12), body, None)
    hmac_key = HKDF(hashes.SHA256(), 32, None, b"header").derive(file_key)
    mac_end = blob.index(b"\n", header_end)
    check = crypto_hmac.HMAC(hmac_key, hashes.SHA256())
    check.update(blob[:header_end])
    check.verify(base64.b64decode(blob[header_end + 1:mac_end] + b"="))
    nonce = blob[mac_end + 1:mac_end + 17]
    aead = ChaCha20Poly1305(HKDF(hashes.SHA256(), 32, nonce, b"payload").derive(file_key))
    payload = blob[mac_end + 17:]
    chunks = [payload[i:i + CHUNK + 16] for i in range(0, len(payload), CHUNK + 16)] or [b""]
    return b"".join(aead.decrypt(n.to_bytes(11, "big") + (b"\x01" if n == len(chunks) - 1 else b"\x00"), c, None)
                    for n, c in enumerate(chunks))


def x25519_identities(path):
    """The `identity:` lines of a vector that are X25519 keys (a vector can
    list a post-quantum one too)."""
    head = path.read_bytes().partition(b"\n\n")[0].decode()
    return [line[len("identity: "):] for line in head.splitlines()
            if line.startswith("identity: AGE-SECRET-KEY-1")]


def key_encrypt(data, identity):
    out = io.BytesIO()
    enc = age_stream.Encryptor(out, recipient=identity.public_bytes)
    enc.write(data)
    enc.close()
    return out.getvalue()


class X25519Keys(unittest.TestCase):
    """A new X25519 key per package (#676): the testkit, an independent
    decryption, the key file and its parser."""

    def test_x25519_vectors(self):
        seen = 0
        for name, meta, _p, body in vectors():
            keys = x25519_identities(VECTORS / name)
            if not keys:
                continue
            seen += 1
            with self.subTest(vector=name):
                released, outcome = bytearray(), "success"
                try:
                    read_all(age_stream.open_decrypt(io.BytesIO(body),
                                                     identity=age_stream.parse_identity(keys[0])), released)
                except age_stream.AgeError as exc:
                    outcome = OUTCOME[exc.kind]
                self.assertEqual(outcome, meta["expect"])
                if "payload" in meta:
                    self.assertEqual(hashlib.sha256(released).hexdigest(), meta["payload"])
        self.assertEqual(seen, 68)

    def test_round_trips_through_both_decryptions(self):
        identity = age_stream.generate_identity()
        line = identity.encode()
        for size in RoundTrip.SIZES:
            data = os.urandom(size)
            with self.subTest(size=size):
                blob = key_encrypt(data, identity)
                self.assertEqual(age_stream.open_decrypt(io.BytesIO(blob), identity=identity).read(), data)
                self.assertEqual(independent_x25519_decrypt(blob, line), data)

    def test_each_identity_is_new_and_each_file_fresh(self):
        keys = {age_stream.generate_identity().encode() for _ in range(20)}
        self.assertEqual(len(keys), 20)
        identity = age_stream.generate_identity()
        self.assertNotEqual(key_encrypt(b"same", identity), key_encrypt(b"same", identity))

    def test_a_wrong_key_is_kind_key_and_a_passphrase_is_refused(self):
        blob = key_encrypt(b"data", age_stream.generate_identity())
        with self.assertRaises(age_stream.AgeError) as caught:
            age_stream.open_decrypt(io.BytesIO(blob), identity=age_stream.generate_identity())
        self.assertEqual(caught.exception.kind, "key")
        with self.assertRaises(age_stream.AgeError) as caught:
            age_stream.open_decrypt(io.BytesIO(blob), PASSPHRASE)
        self.assertEqual(caught.exception.kind, "recipient")
        with self.assertRaises(age_stream.AgeError) as caught:
            age_stream.open_decrypt(io.BytesIO(encrypt(b"data")), identity=age_stream.generate_identity())
        self.assertEqual(caught.exception.kind, "header")

    def test_header_kind(self):
        self.assertEqual(age_stream.header_kind(io.BytesIO(encrypt(b"x"))), "passphrase")
        self.assertEqual(age_stream.header_kind(io.BytesIO(key_encrypt(b"x", age_stream.generate_identity()))), "key")
        for junk in (b"", b"not age\n", b"age-encryption.org/v1\n-> X25519 abc\n"):
            self.assertIsNone(age_stream.header_kind(io.BytesIO(junk)))
        self.assertTrue(age_stream.check_header(io.BytesIO(key_encrypt(b"x", age_stream.generate_identity()))))

    def test_the_key_file_is_ages_layout_and_parses_back(self):
        from datetime import datetime, timezone
        identity = age_stream.generate_identity()
        text = age_stream.identity_file(identity, datetime(2026, 11, 20, 12, 0, tzinfo=timezone.utc), STEM + ".tar.gz.age")
        lines = text.splitlines()
        self.assertEqual(lines[0], "# created: 2026-11-20T12:00:00Z")
        self.assertEqual(lines[1], "# public key: " + identity.recipient)
        self.assertIn(STEM + ".tar.gz.age", lines[2])
        self.assertIn("Store it apart from the package", lines[2])
        self.assertTrue(lines[3].startswith("AGE-SECRET-KEY-1") and lines[3] == lines[3].upper())
        self.assertTrue(text.endswith("\n"))
        self.assertEqual(bech32_data(identity.recipient)[0], "age")
        self.assertEqual(age_stream.parse_identity(text).public_bytes, identity.public_bytes)
        self.assertEqual(age_stream.parse_identity(text.replace("\n", "\r\n")).public_bytes, identity.public_bytes)
        self.assertEqual(age_stream.parse_identity("\n\n" + lines[3]).public_bytes, identity.public_bytes)

    def test_the_key_never_shows_in_a_repr_or_an_error(self):
        identity = age_stream.generate_identity()
        line = identity.encode()
        self.assertNotIn(line, repr(identity))
        self.assertNotIn(line.lower(), repr(identity).lower())
        broken = line[:-1] + ("Q" if line[-1] != "Q" else "P")
        other = age_stream.generate_identity().encode()
        pq = "AGE-SECRET-KEY-PQ-1HZLGZUPT4ETPKDEV8HSGFDCYZ4E522W0A7PU2LHT8EH9W6YLNC3SW78XKG"
        for text in ("", "# only a comment\n", broken, line + "\n" + other, line.lower()[:20] + line[20:],
                     pq, "AGE-SECRET-KEY-1", "x" * (age_stream.KEY_FILE_MAX + 1), line + " ",
                     identity.recipient):
            with self.subTest(text=text[:24]), self.assertRaises(age_stream.AgeError) as caught:
                age_stream.parse_identity(text)
            self.assertEqual(caught.exception.kind, "identity")
            self.assertNotIn(line, str(caught.exception))
            self.assertNotIn(text.strip() or "\x00", str(caught.exception))

    def test_the_encryptor_and_decryptor_take_exactly_one_secret(self):
        identity = age_stream.generate_identity()
        with self.assertRaises(ValueError):
            age_stream.Encryptor(io.BytesIO())
        with self.assertRaises(ValueError):
            age_stream.Encryptor(io.BytesIO(), PASSPHRASE, LOW, recipient=identity.public_bytes)
        with self.assertRaises(ValueError):
            age_stream.open_decrypt(io.BytesIO(b""), PASSPHRASE, identity=identity)


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.exports = self.root / "exports"
        self.uploads = self.root / "uploads"
        self.exports.mkdir()
        self.uploads.mkdir()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for key, value in (("exports_dir", self.exports), ("upload_dir", self.uploads),
                           ("sources_dir", self.root / "sources")):
            self.stack.enter_context(patch.object(settings, key, str(value)))
        self.stack.enter_context(patch.object(retrieval_config, "_DIR", None))
        self.stack.enter_context(patch.object(age_stream, "WORK_FACTOR", LOW))
        self.stack.enter_context(patch.dict(importer._jobs, {}, clear=True))
        self.stack.enter_context(patch.dict(exporter._jobs, {}, clear=True))

    def stage(self, extra=None):
        stage = self.root / ("stage-" + uuid.uuid4().hex[:8])
        stage.mkdir()
        (stage / "chunks.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records()))
        manifest = {"package_format": 1, "created_at": "2026-10-27T00:00:00Z",
                    "collection": {"name": "Corpus", "chunk_count": 3, "source_document_count": 0},
                    "embedding": {"model": settings.embed_model, "dimensions": 4},
                    "fidelity": "chunks-only", "models_bundled": False, "bundled_models": [],
                    "files": {"chunks.jsonl": "sha256:" + packager.sha256_file(stage / "chunks.jsonl")},
                    "warnings": []}
        (stage / "manifest.json").write_text(json.dumps(manifest))
        for name, data in (extra or {}).items():
            (stage / name).write_bytes(data)
        return stage

    def sealed(self, name=STEM + ".tar.gz.age", passphrase=PASSPHRASE, stage=None):
        path = self.exports / name
        packager._write_archive(stage or self.stage(), STEM, path, passphrase)
        return path

    def build(self, passphrase=None, key=False):
        extra = {"key": True} if key else {}
        with patch.object(packager, "read_chunks", return_value=[]), \
             patch.object(packager.wc, "_collection_config_sync", return_value={"embedding_model": settings.embed_model}), \
             patch.object(packager, "_ingest_config", return_value=None), \
             patch.object(packager, "_goldstandard_sessions", return_value=[]), \
             patch.object(packager.sources, "load_index", return_value={"documents": {}}), \
             patch.object(packager.wc, "_meta_sync", return_value={"version": "test"}):
            return packager.build("Corpus", passphrase=passphrase, **extra)

    def keyed(self, name=STEM + ".tar.gz.age", stage=None):
        """A key package in ./exports and its identity (#676)."""
        identity = age_stream.generate_identity()
        packager._write_archive(stage or self.stage(), STEM, self.exports / name,
                                recipient=identity.public_bytes)
        return self.exports / name, identity


class Export(Fixture):
    def test_the_encrypted_archive_holds_the_same_members_in_the_same_order(self):
        stage = self.stage(extra={"README.md": b"readme"})
        plain = self.exports / (STEM + ".tar.gz")
        packager._write_archive(stage, STEM, plain)
        sealed = self.sealed(stage=stage)
        with plain.open("rb") as fh:
            expected = [(m.name, m.size) for m in tarfile.open(fileobj=fh, mode="r:gz")]
        with sealed.open("rb") as fh, \
             tarfile.open(fileobj=age_stream.open_decrypt(fh, PASSPHRASE), mode="r|gz") as tar:
            got = [(m.name, m.size) for m in tar]
        self.assertEqual(got, expected)
        self.assertEqual(got[1][0], STEM + "/manifest.json")
        self.assertEqual(independent_decrypt(sealed.read_bytes(), PASSPHRASE)[1], LOW)

    def test_an_encrypted_build_names_the_delivered_file_and_leaves_nothing_else(self):
        retrieval_config.save({"collection": "Corpus", "top_k": 7})
        result = self.build(PASSPHRASE)
        self.assertTrue(result["filename"].endswith(".tar.gz.age"), result["filename"])
        self.assertTrue(result["encrypted"])
        self.assertEqual(sorted(p.name for p in self.exports.iterdir()), [result["filename"]])
        work = self.root / "open"
        work.mkdir()
        pkg, manifest = packager.open_package(self.exports / result["filename"], work, PASSPHRASE)
        packager.verify_digests(pkg, manifest)
        self.assertEqual(pkg.name, result["filename"][:-len(".tar.gz.age")])
        readme = (pkg / "README.md").read_text()
        self.assertIn(result["filename"], readme)
        self.assertIn('"on_conflict": "abort", "passphrase": "<the passphrase>"}', readme)
        self.assertIn("## Encryption", readme)
        self.assertNotIn(PASSPHRASE, readme)
        self.assertIn(repr(result["filename"]), (pkg / "retrieve.py").read_text())
        self.assertNotIn(PASSPHRASE.encode(), (self.exports / result["filename"]).read_bytes())

    def test_an_unencrypted_build_is_unchanged(self):
        result = self.build()
        self.assertTrue(result["filename"].endswith(".tar.gz") and not result["filename"].endswith(".age"))
        self.assertFalse(result["encrypted"])
        with tarfile.open(self.exports / result["filename"], "r:gz") as tar:
            readme = tar.extractfile(f"{result['filename'][:-7]}/README.md").read().decode()
        self.assertIn('"on_conflict": "abort"}', readme)
        self.assertNotIn("passphrase\": ", readme)
        self.assertIn("not encrypted", readme)

    def test_a_failed_encrypted_build_leaves_no_partial_file(self):
        with patch.object(packager, "_add_members", side_effect=OSError("disk full")), \
             self.assertRaises(OSError):
            self.build(PASSPHRASE)
        self.assertEqual(list(self.exports.iterdir()), [])

    def test_staging_is_in_upload_dir_and_removed_after_every_build(self):
        """The unencrypted package files are staged under UPLOAD_DIR, never in
        EXPORTS_DIR, and the staging is gone after a build that succeeds or
        fails, plain or encrypted (#549)."""
        real = packager._add_members
        for passphrase in (None, PASSPHRASE):
            for fail in (False, True):
                seen = []

                def add_members(tar, stage, stem):
                    seen.append((stage.parent, stage.name,
                                 sorted(p.name for p in self.exports.iterdir()),
                                 (stage / "chunks.jsonl").exists()))
                    if fail:
                        raise OSError("disk full")
                    return real(tar, stage, stem)
                for p in self.exports.iterdir():
                    p.unlink()
                with self.subTest(encrypted=bool(passphrase), fail=fail), \
                     patch.object(packager, "_add_members", add_members):
                    if fail:
                        with self.assertRaises(OSError):
                            self.build(passphrase)
                    else:
                        result = self.build(passphrase)
                    self.assertEqual(len(seen), 1)
                    parent, name, exports_then, staged = seen[0]
                    self.assertEqual(parent, self.uploads)
                    self.assertTrue(name.startswith("export-"), name)
                    self.assertTrue(staged, "chunks.jsonl not staged")
                    self.assertFalse([n for n in exports_then if not n.endswith(".partial")],
                                     f"EXPORTS_DIR held more than the partial archive: {exports_then}")
                    self.assertEqual([p.name for p in self.uploads.iterdir() if p.name.startswith("export-")],
                                     [], "staging left in UPLOAD_DIR")
                    left = sorted(p.name for p in self.exports.iterdir())
                    self.assertEqual(left, [] if fail else [result["filename"]])

    def test_the_startup_sweep_removes_encrypted_partials_only(self):
        for name in (".a.tar.gz.partial", ".b.tar.gz.age.partial", "c.tar.gz.age", "d.tar.gz"):
            (self.exports / name).write_bytes(b"x")
        packager.sweep_build_leftovers()
        self.assertEqual(sorted(p.name for p in self.exports.iterdir()), ["c.tar.gz.age", "d.tar.gz"])


class OpenEncrypted(Fixture):
    def open(self, archive, passphrase=PASSPHRASE):
        dest = self.root / ("work-" + uuid.uuid4().hex[:6])
        dest.mkdir()
        return packager.open_package(archive, dest, passphrase), dest

    def refused(self, archive, code, passphrase=PASSPHRASE):
        before = archive.read_bytes() if archive.exists() else None
        with self.assertRaises(packager.PackageError) as caught:
            self.open(archive, passphrase)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        if before is not None:
            self.assertEqual(archive.read_bytes(), before, "the package file changed")
        self.assertNotIn(PASSPHRASE, json.dumps(caught.exception.detail) + caught.exception.message)
        return caught.exception

    def test_the_right_passphrase_opens_and_verifies(self):
        (pkg, manifest), _dest = self.open(self.sealed())
        packager.verify_digests(pkg, manifest)
        self.assertEqual(manifest["collection"]["name"], "Corpus")

    def test_a_wrong_passphrase_is_decrypt_failed_with_nothing_extracted(self):
        archive = self.sealed()
        dest = self.root / "w"
        dest.mkdir()
        with self.assertRaises(packager.PackageError) as caught:
            packager.open_package(archive, dest, "not the passphrase")
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ("DECRYPT_FAILED", {"reason": "passphrase"}))
        self.assertEqual(caught.exception.message,
                         f"'{archive.name}' could not be decrypted: the passphrase is wrong or the file is damaged.")
        self.assertEqual(list(dest.iterdir()), [])

    def test_damage_is_decrypt_failed_naming_the_reason(self):
        good = self.sealed().read_bytes()
        payload_at = good.index(b"\n", good.index(b"\n---") + 1) + 1 + 16
        cases = {"payload": good[:payload_at + 40] + bytes([good[payload_at + 40] ^ 1]) + good[payload_at + 41:],
                 "header": RoundTrip.changed_mac(good)}
        for reason, data in cases.items():
            archive = self.exports / f"ragpkg-{reason}.tar.gz.age"
            archive.write_bytes(data)
            with self.subTest(reason=reason):
                self.assertEqual(self.refused(archive, "DECRYPT_FAILED").detail, {"reason": reason})

    def test_a_missing_last_chunk_after_a_complete_tar_is_caught_at_the_end(self):
        """tarfile stops at its end-of-archive blocks; the rest of the file is
        still authenticated, so a cut that only removes padding is refused."""
        stage = self.stage()
        plain = self.root / "plain.tar.gz"
        packager._write_archive(stage, STEM, plain)
        data = plain.read_bytes() + bytes(2 * CHUNK)   # zero padding past the gzip end
        blob = encrypt(data)
        archive = self.exports / "ragpkg-cut.tar.gz.age"
        chunks = -(-len(data) // CHUNK)
        archive.write_bytes(blob[:-(len(data) - CHUNK * (chunks - 1) + 16)])
        self.assertEqual(self.refused(archive, "DECRYPT_FAILED").detail, {"reason": "payload"})
        archive.write_bytes(blob + b"trailing")
        self.assertEqual(self.refused(archive, "DECRYPT_FAILED").detail, {"reason": "payload"})
        archive.write_bytes(blob)
        (pkg, _manifest), _dest = self.open(archive)
        self.assertEqual(pkg.name, STEM)

    def test_a_work_factor_above_the_limit_is_decrypt_failed(self):
        good = self.sealed().read_bytes()
        line = good.split(b"\n")[1]
        archive = self.exports / "ragpkg-wf.tar.gz.age"
        archive.write_bytes(good.replace(line, line.rsplit(b" ", 1)[0] + b" 21", 1))
        with patch.object(age_stream, "_scrypt", side_effect=AssertionError("scrypt ran")):
            self.assertEqual(self.refused(archive, "DECRYPT_FAILED").detail, {"reason": "work_factor"})

    def test_a_malformed_work_factor_is_package_unreadable(self):
        """Not a decimal without a sign or leading zero: a malformed age header,
        PACKAGE_UNREADABLE as #27 decision 7 says, not DECRYPT_FAILED (#549)."""
        good = self.sealed().read_bytes()
        line = good.split(b"\n")[1]
        for factor in (b"0x12", b"018", b"+18", b"0"):
            archive = self.exports / "ragpkg-wf-bad.tar.gz.age"
            archive.write_bytes(good.replace(line, line.rsplit(b" ", 1)[0] + b" " + factor, 1))
            with self.subTest(factor=factor), \
                 patch.object(age_stream, "_scrypt", side_effect=AssertionError("scrypt ran")):
                detail = self.refused(archive, "PACKAGE_UNREADABLE").detail
                self.assertIn("work factor", detail["reason"])

    def test_an_overlong_work_factor_is_decrypt_failed_not_import_failed(self):
        good = self.sealed().read_bytes()
        line = good.split(b"\n")[1]
        archive = self.exports / "ragpkg-wf-long.tar.gz.age"
        archive.write_bytes(good.replace(line, line.rsplit(b" ", 1)[0] + b" " + b"9" * 5000, 1))
        with patch.object(age_stream, "_scrypt", side_effect=AssertionError("scrypt ran")):
            self.assertEqual(self.refused(archive, "DECRYPT_FAILED").detail, {"reason": "work_factor"})

    def test_a_file_cut_inside_the_nonce_is_decrypt_failed(self):
        good = self.sealed().read_bytes()
        header_end = good.index(b"\n", good.index(b"\n---") + 1) + 1
        archive = self.exports / "ragpkg-nonce.tar.gz.age"
        for cut in (0, 5, 15):
            archive.write_bytes(good[:header_end + cut])
            with self.subTest(cut=cut):
                self.assertEqual(self.refused(archive, "DECRYPT_FAILED").detail, {"reason": "payload"})

    def test_a_file_that_is_not_a_passphrase_age_file_is_unreadable(self):
        x25519 = next(body for name, _m, _p, body in vectors() if name == "x25519")
        plain = self.root / "plain.tar.gz"
        packager._write_archive(self.stage(), STEM, plain)
        for name, data in (("garbage", b"not an age file at all\n"), ("x25519", x25519),
                           ("plain", plain.read_bytes())):
            archive = self.exports / f"ragpkg-{name}.tar.gz.age"
            archive.write_bytes(data)
            with self.subTest(case=name):
                error = self.refused(archive, "PACKAGE_UNREADABLE")
                self.assertIn("is not a passphrase-encrypted age file", error.message)

    def test_encrypted_bytes_that_are_not_a_tar_gz_are_unreadable(self):
        archive = self.exports / "ragpkg-text.tar.gz.age"
        archive.write_bytes(encrypt(b"plain text, not gzip\n"))
        error = self.refused(archive, "PACKAGE_UNREADABLE")
        self.assertEqual(error.message, "'ragpkg-text.tar.gz.age' is not a readable encrypted .tar.gz archive.")

    def test_import_writes_no_decrypted_copy_of_the_archive(self):
        """Decision 9 on #27: the package is decrypted as a stream while it is
        unpacked, so the only files written are the package's own members,
        inside the work directory. Every file opened for writing is recorded
        through the interpreter's audit hook, whichever API opened it."""
        stage = self.stage(extra={"README.md": b"readme"})
        archive = self.sealed(stage=stage)
        dest = self.root / "work-nocopy"
        dest.mkdir()
        written, watching = [], [False]
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND

        def hook(event, args):
            if not watching[0] or event != "open":
                return
            path, mode, flags = (tuple(args) + (None, None))[:3]
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                written.append(str(path))
            elif mode is None and isinstance(flags, int) and flags & write_flags:
                written.append(str(path))
        _AUDIT_HOOKS.append(hook)
        watching[0] = True
        try:
            pkg, _manifest = packager.open_package(archive, dest, PASSPHRASE)
        finally:
            watching[0] = False
            _AUDIT_HOOKS.remove(hook)
        root = (dest / STEM).resolve()
        members = sorted(p.name for p in stage.iterdir())
        self.assertEqual(pkg.resolve(), root)
        self.assertTrue(written, "the audit hook saw no write at all")
        for path in written:
            resolved = Path(path).resolve()
            self.assertEqual(resolved.parent, root, f"a file was written outside the package: {path}")
            self.assertIn(resolved.name, members, f"a file that is not a package member was written: {path}")
        self.assertEqual(sorted(Path(p).name for p in written), members)

    def test_an_empty_passphrase_is_tried_and_refused_as_decrypt_failed(self):
        """Import takes any passphrase (decision 4); an empty one unwraps nothing."""
        self.assertEqual(self.refused(self.sealed(), "DECRYPT_FAILED", passphrase="").detail,
                         {"reason": "passphrase"})

    def test_unsafe_members_are_refused_before_extraction(self):
        outside = self.root / "escape-27.txt"
        for kind in ("symlink", "outside"):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                info = tarfile.TarInfo(STEM)
                info.type = tarfile.DIRTYPE
                tar.addfile(info)
                if kind == "symlink":
                    info = tarfile.TarInfo(STEM + "/link")
                    info.type, info.linkname = tarfile.SYMTYPE, str(outside)
                    tar.addfile(info)
                else:
                    info = tarfile.TarInfo("../escape-27.txt")
                    info.size = 3
                    tar.addfile(info, io.BytesIO(b"bad"))
            archive = self.exports / f"ragpkg-{kind}.tar.gz.age"
            archive.write_bytes(encrypt(buf.getvalue()))
            with self.subTest(kind=kind):
                self.refused(archive, "PACKAGE_UNREADABLE")
                self.assertFalse(outside.exists())


class ImportJob(Fixture):
    """The import job: a wrong passphrase touches nothing, and no record or log
    line holds the passphrase."""

    def setUp(self):
        super().setUp()
        self.create = MagicMock(side_effect=AssertionError("reached collection creation"))
        self.exists = MagicMock(side_effect=AssertionError("reached the conflict check"))
        self.client = MagicMock(side_effect=AssertionError("no Weaviate client may be opened"))
        self.stack.enter_context(patch.object(wc, "_create_collection_sync", self.create))
        self.stack.enter_context(patch.object(wc, "_collection_exists_sync", self.exists))
        self.stack.enter_context(patch.object(wc, "get_client", self.client))
        self.markers = importer._markers_dir()

    def run_job(self, archive, passphrase):
        job_id = "job" + uuid.uuid4().hex[:5]
        importer._jobs[job_id] = {"job_id": job_id, "status": "queued", "filename": archive.name,
                                  "on_conflict": "replace", "collection": None,
                                  "original_collection": None, "chunks_written": 0,
                                  "fidelity": None, "renamed": False, "notes": [],
                                  "restored_sessions": [], "error": None, "error_code": None,
                                  "error_detail": None, "encrypted": True}
        with importer._lock:
            importer._active.add(archive.name)
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        try:
            importer._run(job_id, archive.name, "replace", passphrase)
        finally:
            logging.getLogger().removeHandler(handler)
        return importer._jobs[job_id], logs.getvalue()

    def test_a_wrong_passphrase_fails_the_job_and_touches_nothing(self):
        archive = self.sealed()
        before = archive.read_bytes()
        job, logs = self.run_job(archive, "wrong passphrase!")
        self.assertEqual((job["status"], job["error_code"], job["error_detail"]),
                         ("failed", "DECRYPT_FAILED", {"reason": "passphrase"}))
        self.create.assert_not_called()
        self.exists.assert_not_called()
        self.client.assert_not_called()
        self.assertEqual(archive.read_bytes(), before)
        self.assertFalse(self.markers.exists() and any(self.markers.iterdir()))
        self.assertEqual([p.name for p in self.uploads.iterdir() if p.name.startswith("import-")], [])
        self.assertNotIn("wrong passphrase!", json.dumps(job) + logs)

    def test_the_right_passphrase_reaches_the_conflict_check(self):
        archive = self.sealed()
        with patch.object(importer, "_check_embedding"), \
             patch.object(importer, "_ensure_models", return_value=[]):
            job, logs = self.run_job(archive, PASSPHRASE)
        # The decrypted package passed checks 1 to 4; the mocked backend stops it there.
        self.exists.assert_called()
        self.assertEqual(job["collection"], "Corpus")
        self.assertNotIn(PASSPHRASE, json.dumps(job) + logs)


class KeyExports(Fixture):
    """A new key per package (#676): the build, the key file, opening it."""

    def test_a_key_build_writes_the_package_and_a_0600_key_file(self):
        result = self.build(key=True)
        name, key_name = result["filename"], result["key_filename"]
        self.assertTrue(name.endswith(".tar.gz.age"), name)
        self.assertEqual(key_name, name + ".key")
        self.assertEqual((result["encrypted"], result["encryption"]), (True, "key"))
        self.assertEqual(sorted(p.name for p in self.exports.iterdir()), sorted([name, key_name]))
        self.assertEqual(os.stat(self.exports / key_name).st_mode & 0o777, 0o600)
        text = (self.exports / key_name).read_text()
        identity = age_stream.parse_identity(text)
        self.assertIn(name, text)
        self.assertNotIn(identity.encode(), json.dumps(result))
        with (self.exports / name).open("rb") as fh:
            self.assertEqual(age_stream.header_kind(fh), "key")
        work = self.root / "open"
        work.mkdir()
        pkg, manifest = packager.open_package(self.exports / name, work, identity=identity)
        packager.verify_digests(pkg, manifest)
        readme = (pkg / "README.md").read_text()
        self.assertIn(f"age -d -i {name}.key {name}", readme)
        self.assertIn('"on_conflict": "abort", "key": "<the key file\'s text>"}', readme)
        self.assertIn("Store the key apart from the package", readme)
        self.assertNotIn(identity.encode(), readme)

    def test_each_key_export_has_its_own_key(self):
        keys = set()
        for _ in range(3):
            result = self.build(key=True)
            keys.add(age_stream.parse_identity((self.exports / result["key_filename"]).read_text()).recipient)
            for p in self.exports.iterdir():
                p.unlink()
        self.assertEqual(len(keys), 3)

    def test_the_umask_cant_widen_the_key_file(self):
        old = os.umask(0)
        try:
            result = self.build(key=True)
        finally:
            os.umask(old)
        self.assertEqual(os.stat(self.exports / result["key_filename"]).st_mode & 0o777, 0o600)

    def test_a_failed_key_build_leaves_neither_file(self):
        for target in ("_add_members", "_write_key_file"):
            with self.subTest(failing=target), \
                 patch.object(packager, target, side_effect=OSError("disk full")), \
                 self.assertRaises(OSError):
                self.build(key=True)
            self.assertEqual(list(self.exports.iterdir()), [])
        # The package's placement fails after its key file was placed (#860).
        real = os.link
        def fail_package(src, dst, *args, **kwargs):
            if str(dst).endswith(".tar.gz.age"):
                raise OSError(errno.EIO, "disk full")
            return real(src, dst, *args, **kwargs)
        with patch.object(packager.os, "link", fail_package), self.assertRaises(OSError):
            self.build(key=True)
        self.assertEqual(list(self.exports.iterdir()), [])

    def test_a_key_and_a_passphrase_are_never_both_used(self):
        with self.assertRaises(ValueError):
            packager.build("Corpus", passphrase=PASSPHRASE, key=True)

    def test_the_startup_sweep_removes_key_partials(self):
        for name in (".a.tar.gz.age.key.partial", "b.tar.gz.age.key"):
            (self.exports / name).write_bytes(b"x")
        packager.sweep_build_leftovers()
        self.assertEqual(sorted(p.name for p in self.exports.iterdir()), ["b.tar.gz.age.key"])

    def test_opening_with_the_wrong_key_or_the_wrong_kind(self):
        archive, identity = self.keyed()
        for secret, code, reason in ((dict(identity=age_stream.generate_identity()), "DECRYPT_FAILED", "key"),
                                     (dict(passphrase=PASSPHRASE), "PACKAGE_UNREADABLE", None)):
            work = self.root / ("w" + uuid.uuid4().hex[:6])
            work.mkdir()
            with self.subTest(secret=list(secret)), self.assertRaises(packager.PackageError) as caught:
                packager.open_package(archive, work, **secret)
            self.assertEqual(caught.exception.code, code)
            if reason:
                self.assertEqual(caught.exception.detail, {"reason": reason})
            self.assertEqual(list(work.iterdir()), [])
        work = self.root / "right"
        work.mkdir()
        packager.open_package(archive, work, identity=identity)


class SameSecondNames(Fixture):
    """Two exports of one collection in the same second get distinct names and
    no export replaces a file in ./exports (#860, spec §4.1)."""

    WHEN = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)

    def setUp(self):
        super().setUp()
        self.stack.enter_context(patch.object(packager, "_given", set()))
        self.stack.enter_context(patch.object(packager, "_now", return_value=self.WHEN))

    def files(self):
        return {p.name: p.read_bytes() for p in self.exports.iterdir()}

    def stem(self, result):
        return result["filename"].split(".tar.gz")[0]

    def test_same_second_exports_get_distinct_names_and_keep_every_file(self):
        first = self.build(key=True)
        kept = self.files()
        second = self.build(key=True)
        third = self.build(passphrase=PASSPHRASE)
        stem = self.stem(first)
        self.assertTrue(stem.endswith("-20261121T120000Z-" + first["id8"]), stem)
        self.assertEqual([self.stem(second), self.stem(third)], [stem + "-2", stem + "-3"])
        self.assertEqual(second["id8"], first["id8"])
        names = [first["filename"], first["key_filename"], second["filename"], second["key_filename"],
                 third["filename"]]
        files = self.files()
        self.assertEqual(sorted(files), sorted(names))
        for name, data in kept.items():
            self.assertEqual(files[name], data, name)
        self.assertIsNone(third["key_filename"])
        for result in (first, second):
            work = self.root / ("open-" + self.stem(result))
            work.mkdir()
            identity = age_stream.parse_identity((self.exports / result["key_filename"]).read_text())
            pkg, manifest = packager.open_package(self.exports / result["filename"], work, identity=identity)
            packager.verify_digests(pkg, manifest)
            self.assertEqual(pkg.name, self.stem(result))
            self.assertIn(result["filename"], (pkg / "README.md").read_text())
        with self.assertRaises(packager.PackageError) as caught:
            packager.open_package(self.exports / second["filename"], self.root / "wrong",
                                  identity=age_stream.parse_identity(
                                      (self.exports / first["key_filename"]).read_text()))
        self.assertEqual(caught.exception.code, "DECRYPT_FAILED")

    def test_a_leftover_key_file_takes_the_name(self):
        first = self.build(key=True)
        (self.exports / first["filename"]).unlink()           # the package was downloaded
        key = (self.exports / first["key_filename"]).read_bytes()
        packager._given.clear()                               # a new process: only the folder counts
        second = self.build(key=True)
        self.assertEqual(self.stem(second), self.stem(first) + "-2")
        self.assertEqual((self.exports / first["key_filename"]).read_bytes(), key)

    def test_a_name_given_in_this_process_is_not_given_again(self):
        first = self.build()
        (self.exports / first["filename"]).unlink()           # downloaded and deleted
        second = self.build()
        self.assertEqual(second["filename"], self.stem(first) + "-2.tar.gz")

    def plant_during_build(self, suffix):
        """Wrap `_write_archive` so a file appears at the chosen name's `suffix`
        after the name was chosen, as if something else wrote it."""
        real = packager._write_archive
        planted = []

        def write(stage, stem, dest, *args, **kwargs):
            path = self.exports / (stem + suffix)
            path.write_bytes(b"someone else's file")
            planted.append(path)
            return real(stage, stem, dest, *args, **kwargs)
        return patch.object(packager, "_write_archive", write), planted

    def test_a_name_taken_after_it_was_chosen_fails_and_replaces_nothing(self):
        for suffix in (".tar.gz.age", ".tar.gz.age.key"):
            with self.subTest(planted=suffix):
                for p in self.exports.iterdir():
                    p.unlink()
                wrapper, planted = self.plant_during_build(suffix)
                with wrapper, self.assertRaises(FileExistsError) as caught:
                    self.build(key=True)
                self.assertIn(planted[0].name, str(caught.exception))
                self.assertEqual(self.files(), {planted[0].name: b"someone else's file"})

    def test_without_hard_links_the_fallback_places_and_refuses(self):
        no_links = patch.object(packager.os, "link", side_effect=PermissionError(errno.EPERM, "no links"))
        with no_links:
            result = self.build(key=True)
        self.assertEqual(sorted(self.files()), sorted([result["filename"], result["key_filename"]]))
        self.assertEqual(os.stat(self.exports / result["key_filename"]).st_mode & 0o777, 0o600)
        for p in self.exports.iterdir():
            p.unlink()
        wrapper, planted = self.plant_during_build(".tar.gz.age")
        with no_links, wrapper, self.assertRaises(FileExistsError):
            self.build(key=True)
        self.assertEqual(self.files(), {planted[0].name: b"someone else's file"})

    def test_no_free_name_fails_and_writes_nothing(self):
        first = self.build()
        (self.exports / (self.stem(first) + "-2.tar.gz.age.key")).write_bytes(b"x")
        before = self.files()
        with patch.object(packager, "MAX_NAME_SUFFIX", 2), self.assertRaises(RuntimeError):
            self.build()
        self.assertEqual(self.files(), before)

    # Reviewer tests (rag-pr-review, #860).
    def test_a_failed_removal_of_the_temporary_takes_the_placed_file_back(self):
        # The link placed the file but the temporary can't be removed: the
        # placed name is removed again, so the export leaves no half-placed pair.
        tmp, final = self.exports / ".x.tar.gz.partial", self.exports / "x.tar.gz"
        tmp.write_bytes(b"data")
        real = os.unlink

        def unlink(path, *args, **kwargs):
            if str(path) == str(tmp):
                raise OSError(errno.EIO, "busy")
            return real(path, *args, **kwargs)
        with patch.object(packager.os, "unlink", unlink), self.assertRaises(OSError):
            packager._place(tmp, final)
        self.assertFalse(final.exists())
        self.assertEqual(tmp.read_bytes(), b"data")

    def test_validate_package_accepts_a_suffixed_name_and_refuses_a_malformed_one(self):
        first = self.build()
        second = self.build()
        self.assertEqual(second["filename"], self.stem(first) + "-2.tar.gz")
        script = ROOT / "scripts" / "verify" / "validate_package.py"
        # Only the name clauses are checked here: this fixture's mocked build
        # leaves embedding.dimensions empty, which suite 05 checks on a real one.
        ran = subprocess.run([sys.executable, str(script), str(self.exports / second["filename"])],
                             capture_output=True, text=True, timeout=120)
        self.assertIn("PASS  E5 filename matches the §4.1 pattern", ran.stdout, ran.stdout + ran.stderr)
        self.assertIn("PASS  E19 README names the package file as delivered", ran.stdout)
        for bad in ("-01", "-0", "-x"):
            with self.subTest(suffix=bad):
                renamed = self.exports / (self.stem(first) + bad + ".tar.gz")
                renamed.write_bytes((self.exports / first["filename"]).read_bytes())
                ran = subprocess.run([sys.executable, str(script), str(renamed)],
                                     capture_output=True, text=True, timeout=120)
                self.assertNotEqual(ran.returncode, 0)
                self.assertIn("FAIL  E5 filename matches the §4.1 pattern", ran.stdout)


class KeyApi(Fixture):
    """POST /export's `encryption`, LAN mode, POST /import's `key` and the
    listing (#676). No key text reaches a response, record or log."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(app, headers=auth_testing.headers(), raise_server_exceptions=False)
        self.stack.enter_context(patch.object(importer, "_active", set()))

    def started(self, body):
        seen = []

        async def start(collection, include_models, passphrase, owner=None, encryption=None):
            seen.append((passphrase, encryption))
            return "job12345"
        with patch.object(wc, "collection_exists", return_value=True), \
             patch.object(exporter, "start_export_job", side_effect=start):
            response = self.client.post("/export", json={"collection": "Corpus", **body})
        return response, seen

    def test_the_default_export_is_a_key_export(self):
        for body, expected in (({}, (None, "key")), ({"encryption": "key"}, (None, "key")),
                               ({"encryption": "none"}, (None, "none")),
                               ({"passphrase": PASSPHRASE}, (PASSPHRASE, "passphrase")),
                               ({"encryption": "passphrase", "passphrase": PASSPHRASE}, (PASSPHRASE, "passphrase"))):
            with self.subTest(body=list(body)):
                response, seen = self.started(body)
                self.assertEqual(response.status_code, 202, response.text)
                self.assertEqual(seen, [expected])

    def test_mismatched_choices_are_422_without_the_passphrase(self):
        for body in ({"encryption": "passphrase"}, {"encryption": "key", "passphrase": PASSPHRASE},
                     {"encryption": "none", "passphrase": PASSPHRASE}, {"encryption": "plain"}):
            with self.subTest(body=body):
                response, seen = self.started(body)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(seen, [])
                self.assertNotIn(PASSPHRASE, response.text)

    def test_lan_mode_refuses_an_unencrypted_export_only(self):
        with patch.object(settings, "rag_lan_mode", True):
            response, seen = self.started({"encryption": "none"})
            self.assertEqual(response.status_code, 400, response.text)
            self.assertEqual(response.json()["error"]["code"], "ENCRYPTION_REQUIRED")
            self.assertEqual(seen, [])
            for body in ({}, {"passphrase": PASSPHRASE}):
                response, seen = self.started(body)
                self.assertEqual(response.status_code, 202, response.text)
        response, seen = self.started({"encryption": "none"})
        self.assertEqual(response.status_code, 202, response.text)

    def test_a_key_export_job_names_its_key_file_and_never_the_key(self):
        logs = io.StringIO()
        handler = logging.StreamHandler(logs)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        with patch.object(wc, "collection_exists", return_value=True), \
             patch.object(packager, "read_chunks", return_value=[]), \
             patch.object(packager.wc, "_collection_config_sync", return_value={"embedding_model": settings.embed_model}), \
             patch.object(packager, "_ingest_config", return_value=None), \
             patch.object(packager, "_goldstandard_sessions", return_value=[]), \
             patch.object(packager.sources, "load_index", return_value={"documents": {}}), \
             patch.object(packager.wc, "_meta_sync", return_value={"version": "test"}):
            self.client.portal = self.stack.enter_context(anyio.from_thread.start_blocking_portal())
            response = self.client.post("/export", json={"collection": "Corpus"})
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["job_id"]
            for _ in range(300):
                job = self.client.get(f"/export/job/{job_id}").json()
                if job["status"] in ("completed", "failed"):
                    break
                time.sleep(0.05)
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual((job["encryption"], job["key_filename"], job["key_on_server"]),
                         ("key", job["filename"] + ".key", True))
        key_text = (self.exports / job["key_filename"]).read_text()
        secret = age_stream.parse_identity(key_text).encode()
        listed = {p["filename"]: p for p in self.client.get("/packages").json()["packages"]}
        self.assertEqual(sorted(listed), [job["filename"]])
        self.assertEqual((listed[job["filename"]]["encryption"], listed[job["filename"]]["key_on_server"],
                          listed[job["filename"]]["readable"]), ("key", True, True))
        # GET /export/jobs adds the same fields to each listed record.
        from routers import transfer
        self.assertEqual(transfer._export_extras(exporter.get_job(job_id)),
                         {"encryption": "key", "key_on_server": True})
        self.assertEqual(transfer._export_extras({"encrypted": True}), {"encryption": "passphrase", "key_on_server": False})
        self.assertEqual(transfer._export_extras({}), {"encryption": "none", "key_on_server": False})
        self.assertEqual(transfer._export_extras({"encryption": "key", "key_filename": "../x.key"}),
                         {"encryption": "key", "key_on_server": False})
        (self.exports / job["key_filename"]).unlink()
        self.assertFalse(self.client.get(f"/export/job/{job_id}").json()["key_on_server"])
        self.assertFalse(self.client.get("/packages").json()["packages"][0]["key_on_server"])
        for text in (json.dumps(dict(exporter._jobs)), logs.getvalue(), json.dumps(job)):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret.lower(), text)

    def import_body(self, filename, **secret):
        return {"filename": filename, "on_conflict": "abort", **secret}

    def test_import_refusals_before_any_job(self):
        archive, identity = self.keyed()
        line = identity.encode()
        pp = self.sealed(name="ragpkg-pp-20261027T000000Z-0123abcd.tar.gz.age")
        plain = self.exports / (STEM + ".tar.gz")
        packager._write_archive(self.stage(), STEM, plain)
        cases = ((archive.name, {}, "KEY_FILE_REQUIRED"),
                 (archive.name, {"passphrase": PASSPHRASE}, "KEY_FILE_REQUIRED"),
                 (archive.name, {"key": "not a key"}, "KEY_FILE_INVALID"),
                 (archive.name, {"key": "AGE-SECRET-KEY-PQ-1HZLGZUPT4ETPKDEV8HSGFDCYZ4E522W0A7PU2LHT8EH9W6YLNC3SW78XKG"},
                  "KEY_FILE_INVALID"),
                 (pp.name, {"key": line}, "PASSPHRASE_REQUIRED"),
                 (pp.name, {}, "PASSPHRASE_REQUIRED"),
                 (plain.name, {"key": line}, "KEY_FILE_NOT_EXPECTED"),
                 ("ragpkg-missing.tar.gz.age", {}, "PASSPHRASE_REQUIRED"))
        for filename, secret, code in cases:
            with self.subTest(code=code, secret=list(secret)), \
                 patch.object(importer, "_run", side_effect=AssertionError("a job ran")):
                response = self.client.post("/import", json=self.import_body(filename, **secret))
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], code)
                self.assertNotIn(line, response.text)
                self.assertEqual(dict(importer._jobs), {})
        for body in (self.import_body(archive.name, key=line, passphrase=PASSPHRASE),
                     self.import_body(archive.name, key="x" * (age_stream.KEY_FILE_MAX + 1))):
            response = self.client.post("/import", json=body)
            self.assertEqual(response.status_code, 422, response.text)
            self.assertNotIn(line, response.text)

    def test_import_with_the_key_hands_the_worker_an_identity_only(self):
        archive, identity = self.keyed()
        line = identity.encode()
        self.client.portal = self.stack.enter_context(anyio.from_thread.start_blocking_portal())
        seen, ran = [], threading.Event()

        def run(*args, **kwargs):
            seen.append((args, kwargs))
            ran.set()
        key_text = age_stream.identity_file(identity, __import__("datetime").datetime.now(), archive.name)
        with patch.object(importer, "_run", side_effect=run):
            response = self.client.post("/import", json=self.import_body(archive.name, key=key_text))
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["job_id"]
            self.assertTrue(ran.wait(30), "the import worker did not run within 30 s")
        args, kwargs = seen[0]
        self.assertEqual(args[1:4], (archive.name, "abort", None))
        handed = kwargs.get("identity") or args[4]
        self.assertEqual(handed.public_bytes, identity.public_bytes)
        job = self.client.get(f"/import/job/{job_id}").json()
        self.assertEqual((job["encrypted"], job["encryption"]), (True, "key"))
        self.assertNotIn(line, json.dumps(dict(importer._jobs)))

    def test_an_unreadable_header_takes_either_secret(self):
        bad = self.exports / "ragpkg-bad-20261027T000000Z-0123abcd.tar.gz.age"
        bad.write_bytes(b"not an age file\n")
        line = age_stream.generate_identity().encode()
        self.client.portal = self.stack.enter_context(anyio.from_thread.start_blocking_portal())
        for secret in ({"key": line}, {"passphrase": PASSPHRASE}):
            ran = threading.Event()
            with self.subTest(secret=list(secret)), \
                 patch.object(importer, "_run", side_effect=lambda *a, **k: ran.set()):
                response = self.client.post("/import", json=self.import_body(bad.name, **secret))
                self.assertEqual(response.status_code, 202, response.text)
                self.assertTrue(ran.wait(30))
            importer._active.clear()


class KeyImportJob(ImportJob):
    """The import job with a key (#676): a wrong key touches nothing."""

    def run_key_job(self, archive, identity):
        job_id = "job" + uuid.uuid4().hex[:5]
        importer._jobs[job_id] = {"job_id": job_id, "status": "queued", "filename": archive.name,
                                  "on_conflict": "replace", "collection": None,
                                  "original_collection": None, "chunks_written": 0,
                                  "fidelity": None, "renamed": False, "notes": [],
                                  "restored_sessions": [], "error": None, "error_code": None,
                                  "error_detail": None, "encrypted": True, "encryption": "key"}
        importer._run(job_id, archive.name, "replace", None, identity=identity)
        return importer._jobs[job_id]

    def test_a_wrong_key_fails_the_job_and_touches_nothing(self):
        archive, _identity = self.keyed()
        before = archive.read_bytes()
        job = self.run_key_job(archive, age_stream.generate_identity())
        self.assertEqual((job["status"], job["error_code"], job["error_detail"]),
                         ("failed", "DECRYPT_FAILED", {"reason": "key"}))
        self.create.assert_not_called()
        self.exists.assert_not_called()
        self.assertEqual(archive.read_bytes(), before)
        self.assertEqual([p.name for p in self.uploads.iterdir() if p.name.startswith("import-")], [])

    def test_the_right_key_reaches_the_conflict_check(self):
        archive, identity = self.keyed()
        with patch.object(importer, "_check_embedding"), \
             patch.object(importer, "_ensure_models", return_value=[]):
            job = self.run_key_job(archive, identity)
        self.exists.assert_called()
        self.assertEqual(job["collection"], "Corpus")


class Api(Fixture):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app, headers=auth_testing.headers(), raise_server_exceptions=False)
        self.stack.enter_context(patch.object(importer, "_active", set()))

    def one_event_loop(self):
        """Send this test's requests on one event loop (#567, as #389 did in test_reingest.py).

        TestClient otherwise opens a loop per request and closes it with the response,
        which can cancel a started import's task before it reaches `_run`."""
        self.client.portal = self.stack.enter_context(anyio.from_thread.start_blocking_portal())

    def test_import_refuses_a_missing_or_unexpected_passphrase_before_any_job(self):
        cases = ((STEM + ".tar.gz.age", None, "PASSPHRASE_REQUIRED"),
                 (STEM + ".tar.gz", PASSPHRASE, "PASSPHRASE_NOT_EXPECTED"),
                 ("sub/../" + STEM + ".tar.gz.age", None, "PASSPHRASE_REQUIRED"))
        for filename, passphrase, code in cases:
            body = {"filename": filename, "on_conflict": "abort"}
            if passphrase is not None:
                body["passphrase"] = passphrase
            with self.subTest(code=code, filename=filename), \
                 patch.object(importer, "_run", side_effect=AssertionError("a job ran")):
                response = self.client.post("/import", json=body)
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json()["error"]["code"], code)
                self.assertNotIn(PASSPHRASE, response.text)
                self.assertEqual(dict(importer._jobs), {})
                self.assertEqual(importer._active, set())

    def test_an_empty_passphrase_counts_as_given(self):
        """`""` is a passphrase, not an absent one: it is refused for a plain
        package, and for an encrypted one it starts a job that will fail."""
        self.one_event_loop()
        with patch.object(importer, "_run", side_effect=AssertionError("a job ran")):
            response = self.client.post("/import", json={"filename": STEM + ".tar.gz",
                                                         "on_conflict": "abort", "passphrase": ""})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "PASSPHRASE_NOT_EXPECTED")
        self.assertEqual(dict(importer._jobs), {})
        seen, ran = [], threading.Event()

        def run(*args):
            seen.append(args)
            ran.set()
        with patch.object(importer, "_run", side_effect=run):
            response = self.client.post("/import", json={"filename": STEM + ".tar.gz.age",
                                                         "on_conflict": "abort", "passphrase": ""})
            self.assertEqual(response.status_code, 202, response.text)
            self.assertTrue(ran.wait(30), "the import worker did not run within 30 s")
        self.assertEqual(seen[0][1:], (STEM + ".tar.gz.age", "abort", ""))

    def test_import_with_a_passphrase_starts_a_job_that_records_only_encrypted(self):
        self.one_event_loop()
        seen, ran = [], threading.Event()

        def run(*args):
            seen.append(args)
            ran.set()
        with patch.object(importer, "_run", side_effect=run):
            response = self.client.post("/import", json={"filename": STEM + ".tar.gz.age",
                                                         "on_conflict": "abort", "passphrase": PASSPHRASE})
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["job_id"]
            self.assertTrue(ran.wait(30), "the import worker did not run within 30 s")
        self.assertEqual(seen[0][1:], (STEM + ".tar.gz.age", "abort", PASSPHRASE))
        job = self.client.get(f"/import/job/{job_id}").json()
        self.assertTrue(job["encrypted"])
        self.assertNotIn(PASSPHRASE, json.dumps(dict(importer._jobs)))

    def test_export_refuses_a_passphrase_out_of_bounds_without_echoing_it(self):
        for phrase in ("x" * 11, "\U0001F512" * 11, "y" * 1025):
            with self.subTest(length=len(phrase)), \
                 patch.object(exporter, "start_export_job", side_effect=AssertionError("a job started")):
                response = self.client.post("/export", json={"collection": "Corpus", "passphrase": phrase})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["error"]["code"], "INVALID_PARAMETER")
                self.assertNotIn(phrase, response.text)
                self.assertIn("12 to 1024 characters", response.text)

    def test_a_lone_surrogate_passphrase_is_refused_by_validation(self):
        bodies = (("/export", '{"collection": "Corpus", "passphrase": "\\ud800aaaaaaaaaaaaaa"}'),
                  ("/import", '{"filename": "%s.tar.gz.age", "on_conflict": "abort", '
                              '"passphrase": "abc\\udfffdef"}' % STEM))
        for path, body in bodies:
            with self.subTest(path=path), \
                 patch.object(exporter, "start_export_job", side_effect=AssertionError("a job started")), \
                 patch.object(importer, "_run", side_effect=AssertionError("a job ran")):
                response = self.client.post(path, content=body, headers={"Content-Type": "application/json"})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["error"]["code"], "INVALID_PARAMETER")
                self.assertIn("lone surrogates", response.text)
                self.assertNotIn("aaaaaaaa", response.text)
        self.assertEqual(dict(importer._jobs), {})

    def test_one_bad_encrypted_file_never_breaks_the_listing(self):
        good = self.sealed()
        line = good.read_bytes().split(b"\n")[1]
        (self.exports / "ragpkg-long.tar.gz.age").write_bytes(
            good.read_bytes().replace(line, line.rsplit(b" ", 1)[0] + b" " + b"9" * 5000, 1))
        packager._write_archive(self.stage(), STEM, self.exports / (STEM + ".tar.gz"))
        response = self.client.get("/packages")
        self.assertEqual(response.status_code, 200, response.text)
        listed = {p["filename"]: p for p in response.json()["packages"]}
        self.assertEqual((listed["ragpkg-long.tar.gz.age"]["readable"], listed[good.name]["readable"],
                          listed[STEM + ".tar.gz"]["readable"]), (False, True, True))
        with patch.object(age_stream, "header_kind", side_effect=RuntimeError("unexpected")):
            response = self.client.get("/packages")
        self.assertEqual(response.status_code, 200, response.text)
        listed = {p["filename"]: p for p in response.json()["packages"]}
        self.assertFalse(listed[good.name]["readable"])
        self.assertTrue(listed[STEM + ".tar.gz"]["readable"])

    def test_export_bounds_count_characters(self):
        seen = []

        async def start(collection, include_models, passphrase, owner=None, encryption=None):   # #617, #676
            seen.append(passphrase)
            return "job12345"
        with patch.object(wc, "collection_exists", return_value=True), \
             patch.object(exporter, "start_export_job", side_effect=start):
            for phrase in ("\U0001F512" * 12, "z" * 1024):
                response = self.client.post("/export", json={"collection": "Corpus", "passphrase": phrase})
                self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(seen, ["\U0001F512" * 12, "z" * 1024])

    def test_an_encrypted_export_job_records_encrypted_and_never_the_passphrase(self):
        built = []

        def build(collection, include_models=False, progress=None, passphrase=None):
            built.append(passphrase)
            return {"filename": STEM + ".tar.gz.age", "size_bytes": 1, "chunk_count": 0,
                    "source_document_count": 0, "fidelity": "chunks-only", "models_bundled": False,
                    "retrieve_script": False, "id8": "0123abcd", "encrypted": True, "warnings": []}
        with patch.object(wc, "collection_exists", return_value=True), \
             patch.object(packager, "build", side_effect=build), \
             patch.object(exporter.collection_writes, "guard", MagicMock()):
            response = self.client.post("/export", json={"collection": "Corpus", "passphrase": PASSPHRASE})
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["job_id"]
            for _ in range(200):
                job = self.client.get(f"/export/job/{job_id}").json()
                if job["status"] in ("completed", "failed"):
                    break
                time.sleep(0.02)
        self.assertEqual(job["status"], "completed", job)
        self.assertTrue(job["encrypted"])
        self.assertEqual(job["filename"], STEM + ".tar.gz.age")
        self.assertEqual(built, [PASSPHRASE])
        self.assertNotIn(PASSPHRASE, json.dumps(dict(exporter._jobs)))

    def test_the_listing_shows_encrypted_packages_without_metadata_or_scrypt(self):
        self.sealed()
        (self.exports / "ragpkg-junk.tar.gz.age").write_bytes(b"junk")
        packager._write_archive(self.stage(), STEM, self.exports / (STEM + ".tar.gz"))
        with patch.object(age_stream, "_scrypt", side_effect=AssertionError("scrypt ran")):
            response = self.client.get("/packages")
        self.assertEqual(response.status_code, 200, response.text)
        listed = {p["filename"]: p for p in response.json()["packages"]}
        nulls = {"collection": None, "chunk_count": None, "fidelity": None, "created_at": None}
        sealed = listed[STEM + ".tar.gz.age"]
        self.assertEqual({k: sealed[k] for k in nulls}, nulls)
        self.assertEqual((sealed["encrypted"], sealed["readable"]), (True, True))
        junk = listed["ragpkg-junk.tar.gz.age"]
        self.assertEqual((junk["encrypted"], junk["readable"]), (True, False))
        plain = listed[STEM + ".tar.gz"]
        self.assertEqual((plain["encrypted"], plain["readable"], plain["collection"]), (False, True, "Corpus"))
        self.assertEqual(sorted(p.name for p in self.exports.iterdir()),
                         sorted(listed), "the listing wrote something beside the packages")


class Telemetry(Fixture):
    """Decision 5 on #27: the passphrase never reaches telemetry. An encrypted
    export, an import with a wrong passphrase and one with the right one run
    through the router with tracing on; no span (its name, attributes, events or
    status, serialized as it would be sent) and no telemetry log record holds it."""

    def test_the_passphrase_never_reaches_telemetry(self):
        import httpx
        from fastapi import FastAPI
        from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
        from routers import transfer
        from services import telemetry as ot

        memory = InMemorySpanExporter()
        provider = TracerProvider(sampler=TraceIdRatioBased(1), shutdown_on_exit=False)
        provider.add_span_processor(SimpleSpanProcessor(memory))
        self.addCleanup(provider.shutdown)
        runtime = ot.Runtime(ot.Config())
        runtime.tracer = provider.get_tracer("test")
        runtime.logger = MagicMock()
        api = FastAPI()
        api.state.telemetry = runtime
        api.add_middleware(ot.RequestTracing)
        api.include_router(transfer.router)
        wrong = "a wrong passphrase, also secret"

        async def until(predicate):
            for _ in range(2000):
                if predicate():
                    return
                await asyncio.sleep(0.005)
            self.fail("the job did not finish")

        async def scenario():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
                started = await client.post("/export", json={"collection": "Corpus", "passphrase": PASSPHRASE})
                self.assertEqual(started.status_code, 202, started.text)
                export_id = started.json()["job_id"]
                await until(lambda: exporter.get_job(export_id)["status"] in ("completed", "failed"))
                job = exporter.get_job(export_id)
                self.assertEqual(job["status"], "completed", job)
                ids = []
                for phrase in (wrong, PASSPHRASE):
                    response = await client.post("/import", json={"filename": job["filename"], "on_conflict": "abort",
                                                                   "passphrase": phrase})
                    self.assertEqual(response.status_code, 202, response.text)
                    ids.append(response.json()["job_id"])
                    await until(lambda: importer.get_job(ids[-1])["status"] in ("completed", "failed"))
                return [importer.get_job(i) for i in ids]

        with patch.object(wc, "collection_exists", return_value=True), \
             patch.object(packager, "read_chunks", return_value=[]), \
             patch.object(packager.wc, "_collection_config_sync", return_value={"embedding_model": settings.embed_model}), \
             patch.object(packager, "_ingest_config", return_value=None), \
             patch.object(packager, "_goldstandard_sessions", return_value=[]), \
             patch.object(packager.sources, "load_index", return_value={"documents": {}}), \
             patch.object(packager.wc, "_meta_sync", return_value={"version": "test"}), \
             patch.object(importer, "_check_embedding"), \
             patch.object(importer, "_ensure_models", return_value=[]), \
             patch.object(wc, "_collection_exists_sync", side_effect=RuntimeError("stop before the backend")), \
             patch.object(importer, "_active", set()):
            failed, stopped = asyncio.run(scenario())
        self.assertEqual(failed["error_code"], "DECRYPT_FAILED")
        self.assertEqual(stopped["error_code"], "IMPORT_FAILED")   # past decryption, at the mocked backend
        spans = memory.get_finished_spans()
        names = {span.name for span in spans}
        self.assertTrue({"rag.export", "rag.import"} <= names, names)
        wire = encode_spans(spans).SerializeToString()
        self.assertTrue(runtime.logger.emit.called, "no telemetry log record was emitted")
        records = repr(runtime.logger.mock_calls).encode()
        for phrase in (PASSPHRASE, wrong):
            with self.subTest(phrase=phrase[:8]):
                self.assertNotIn(phrase.encode(), wire)
                self.assertNotIn(phrase.encode(), records)
                for span in spans:
                    self.assertNotIn(phrase, repr(dict(span.attributes or {})) + repr(span.events) + repr(span.status))


class EmbeddedCopies(unittest.TestCase):
    def test_embedded_copies_match(self):
        document = (ROOT / "IMPLEMENTATION.md").read_text()
        for name in ("api/services/age_stream.py", "api/services/packager.py", "api/services/exporter.py",
                     "api/services/importer.py", "api/routers/transfer.py", "api/models/schemas.py",
                     "api/requirements.in", "api/templates/partials/encryption.md",
                     "api/templates/partials/naming.md", "api/templates/package_readme.md.tmpl",
                     "api/templates/help_transfer.md.tmpl", "ui/src/api/client.ts",
                     "ui/src/pages/TransferPage.tsx", "scripts/verify/validate_package.py",
                     "scripts/verify/browser/transfer_page.js", "scripts/verify/05_transfer.sh"):
            with self.subTest(file=name):
                header = "### " + name + "\n\n"
                self.assertEqual(document.count(header), 1)
                opening = document.index(header) + len(header)
                # Markdown files are fenced with four backticks, as they hold three.
                fence = "````" if document.startswith("````", opening) else "```"
                start = document.index("\n", opening) + 1
                end = document.index("\n" + fence + "\n", start)
                self.assertEqual(document[start:end], (ROOT / name).read_text().rstrip("\n"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
