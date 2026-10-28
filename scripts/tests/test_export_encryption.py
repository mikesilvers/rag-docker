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
import hashlib
import io
import json
import logging
import os
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import uuid
import zlib
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from cryptography.hazmat.primitives import hashes, hmac as crypto_hmac
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
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
           "recipient": "no match", "passphrase": "no match", "nonce": "header failure",
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
        broke the listing and turned an import into IMPORT_FAILED (#27 review)."""
        blob = encrypt(b"data")
        line = blob.split(b"\n")[1]
        for factor in (b"9" * 5000, b"100", b"018", b"+18", b"0x12", b"1e1", b"0"):
            hostile = blob.replace(line, line.rsplit(b" ", 1)[0] + b" " + factor, 1)
            with self.subTest(factor=factor[:12]), \
                 patch.object(age_stream, "_scrypt", side_effect=AssertionError("scrypt ran")), \
                 self.assertRaises(age_stream.AgeError) as caught:
                decrypt(hostile)
            self.assertEqual(caught.exception.kind, "work_factor")
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

    def build(self, passphrase=None):
        with patch.object(packager, "read_chunks", return_value=[]), \
             patch.object(packager.wc, "_collection_config_sync", return_value={"embedding_model": settings.embed_model}), \
             patch.object(packager, "_ingest_config", return_value=None), \
             patch.object(packager, "_goldstandard_sessions", return_value=[]), \
             patch.object(packager.sources, "load_index", return_value={"documents": {}}), \
             patch.object(packager.wc, "_meta_sync", return_value={"version": "test"}):
            return packager.build("Corpus", passphrase=passphrase)


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


class Api(Fixture):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app, headers=auth_testing.headers(), raise_server_exceptions=False)
        self.stack.enter_context(patch.object(importer, "_active", set()))

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
        with patch.object(importer, "_run", side_effect=AssertionError("a job ran")):
            response = self.client.post("/import", json={"filename": STEM + ".tar.gz",
                                                         "on_conflict": "abort", "passphrase": ""})
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"]["code"], "PASSPHRASE_NOT_EXPECTED")
        self.assertEqual(dict(importer._jobs), {})
        seen = []
        with patch.object(importer, "_run", side_effect=lambda *args: seen.append(args)):
            response = self.client.post("/import", json={"filename": STEM + ".tar.gz.age",
                                                         "on_conflict": "abort", "passphrase": ""})
            self.assertEqual(response.status_code, 202, response.text)
            for _ in range(100):
                if seen:
                    break
                time.sleep(0.02)
        self.assertEqual(seen[0][1:], (STEM + ".tar.gz.age", "abort", ""))

    def test_import_with_a_passphrase_starts_a_job_that_records_only_encrypted(self):
        seen = []
        with patch.object(importer, "_run", side_effect=lambda *args: seen.append(args)):
            response = self.client.post("/import", json={"filename": STEM + ".tar.gz.age",
                                                         "on_conflict": "abort", "passphrase": PASSPHRASE})
            self.assertEqual(response.status_code, 202, response.text)
            job_id = response.json()["job_id"]
            for _ in range(100):
                if seen:
                    break
                time.sleep(0.02)
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
        with patch.object(age_stream, "check_header", side_effect=RuntimeError("unexpected")):
            response = self.client.get("/packages")
        self.assertEqual(response.status_code, 200, response.text)
        listed = {p["filename"]: p for p in response.json()["packages"]}
        self.assertFalse(listed[good.name]["readable"])
        self.assertTrue(listed[STEM + ".tar.gz"]["readable"])

    def test_export_bounds_count_characters(self):
        seen = []

        async def start(collection, include_models, passphrase):
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
