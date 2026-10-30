"""The member-count and expanded-size caps on an imported package's TAR (issue #317).

Checks `packager.open_package` for unencrypted `.tar.gz` and encrypted `.tar.gz.age`
packages, and the import job through `importer._run`, with small caps set through the
environment. Runs with the API's dependencies, inside the verify project's API container
(`15_transfer_paths.sh`):

    docker compose exec -T -e RAG_TEST_API_DIR=/app -e MAX_PACKAGE_EXPANDED_BYTES=1048576 \
        -e MAX_PACKAGE_MEMBERS=20 api python - < scripts/tests/test_package_limits.py

Without those variables the test sets the same caps (1 MiB, 20 entries) itself. Weaviate,
Ollama and the model store are mocked; nothing reaches a backend.
"""
import gzip
import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))
# Before config is imported: the settings read the environment once.
os.environ.setdefault("MAX_PACKAGE_EXPANDED_BYTES", "1048576")
os.environ.setdefault("MAX_PACKAGE_MEMBERS", "20")

from config import settings
from services import age_stream, importer, packager
from services import weaviate_client as wc

CAP_BYTES = 1048576
CAP_MEMBERS = 20
TOP = "ragpkg-corpus"
PASSPHRASE = "correct horse battery staple"
LOW = 10            # scrypt work factor for tests: 1 MiB of memory
MANIFEST = json.dumps({"package_format": 1, "created_at": "2026-10-29T00:00:00Z",
                       "collection": {"name": "Corpus", "chunk_count": 0},
                       "fidelity": "chunks-only", "files": {}}).encode()


def tar_bytes(fillers):
    """A .tar.gz of the top directory, a valid manifest and filler files.

    `fillers` is a list of sizes, one file each, in archive order.
    """
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        top = tarfile.TarInfo(TOP)
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tar.addfile(top)
        for name, data in [("manifest.json", MANIFEST)] + [
                (f"fill-{i:03d}.bin", b"\0" * size) for i, size in enumerate(fillers)]:
            info = tarfile.TarInfo(f"{TOP}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def negative_dir_header(name, size):
    """A directory header whose size field holds `size` < 0 in base-256 (first byte 0xff).

    tarfile decodes such a field as a negative number; written by hand because tarfile
    won't write a directory with a size.
    """
    info = tarfile.TarInfo(name)
    info.type = tarfile.DIRTYPE
    info.mode = 0o755
    block = bytearray(info.tobuf(format=tarfile.GNU_FORMAT))
    block[124:136] = bytes([0xff]) + (256 ** 11 + size).to_bytes(11, "big")
    block[148:156] = b" " * 8
    block[148:156] = b"%06o\0 " % sum(block)
    return bytes(block)


def negative_size_tar():
    """The top directory and manifest, a directory of size -10 MiB, then a 2 MiB file in it."""
    def member(name, data):
        info = tarfile.TarInfo(name)
        info.size = len(data)
        return info.tobuf(format=tarfile.GNU_FORMAT) + data + b"\0" * (-len(data) % 512)
    top = tarfile.TarInfo(TOP)
    top.type = tarfile.DIRTYPE
    top.mode = 0o755
    raw = (top.tobuf(format=tarfile.GNU_FORMAT)
           + member(f"{TOP}/manifest.json", MANIFEST)
           + negative_dir_header(f"{TOP}/hole", -10 * CAP_BYTES)
           + member(f"{TOP}/hole/big.bin", b"\0" * (2 * CAP_BYTES))
           + b"\0" * 1024)
    return gzip.compress(raw)


def block(data):
    """Data padded to whole 512-byte TAR blocks."""
    return data + b"\0" * (-len(data) % 512)


def gnu_header(name, size, kind=tarfile.REGTYPE):
    info = tarfile.TarInfo(name)
    info.size = size
    info.type = kind
    info.mode = 0o755 if kind == tarfile.DIRTYPE else 0o644
    return info.tobuf(format=tarfile.GNU_FORMAT)


def pax_header(records):
    """A PAX extended header holding `records` in order, repeated keywords kept."""
    body = b""
    for key, value in records:
        record = f" {key}={value}\n".encode()
        length = len(record) + 1
        while len(str(length)) + len(record) != length:
            length = len(str(length)) + len(record)
        body += str(length).encode() + record
    info = tarfile.TarInfo("././@PaxHeader")
    info.size = len(body)
    info.type = tarfile.XHDTYPE
    return info.tobuf(format=tarfile.USTAR_FORMAT) + block(body)


def old_gnu_sparse_header(name, realsize, chunks, data_size):
    """An old GNU type 'S' header: chunks at offset 386, the real size at 483."""
    raw = bytearray(gnu_header(name, data_size))
    raw[156:157] = tarfile.GNUTYPE_SPARSE
    for i, (offset, numbytes) in enumerate(chunks):
        at = 386 + 24 * i
        raw[at:at + 12] = b"%011o\0" % offset
        raw[at + 12:at + 24] = b"%011o\0" % numbytes
    raw[482] = 0
    raw[483:495] = b"%011o\0" % realsize
    raw[148:156] = b" " * 8
    raw[148:156] = b"%06o\0 " % sum(raw)
    return bytes(raw)


SPARSE = f"{TOP}/sparse.bin"
SPARSE_KINDS = ("GNU S", "PAX 0.0", "PAX 0.1", "PAX 1.0")


def sparse_tar(kind):
    """The top directory, the manifest and a sparse file of real size 0 whose map writes 4 MiB.

    tarfile counts such a file at its real size, 0, and writes the whole map before
    truncating it to that size (#317).
    """
    data = b"\0" * (4 * CAP_BYTES)
    if kind == "GNU S":
        member = old_gnu_sparse_header(SPARSE, 0, [(0, len(data))], len(data)) + block(data)
    elif kind == "PAX 0.0":
        member = (pax_header([("GNU.sparse.size", 0), ("GNU.sparse.numblocks", 1),
                              ("GNU.sparse.offset", 0), ("GNU.sparse.numbytes", len(data))])
                  + gnu_header(SPARSE, len(data)) + block(data))
    elif kind == "PAX 0.1":
        member = (pax_header([("GNU.sparse.size", 0), ("GNU.sparse.map", f"0,{len(data)}")])
                  + gnu_header(SPARSE, len(data)) + block(data))
    else:
        sparse_map = block(b"1\n0\n%d\n" % len(data))
        member = (pax_header([("GNU.sparse.major", 1), ("GNU.sparse.minor", 0),
                              ("GNU.sparse.realsize", 0), ("GNU.sparse.name", SPARSE)])
                  + gnu_header(f"{TOP}/GNUSparseFile.0/sparse.bin", len(sparse_map) + len(data))
                  + sparse_map + block(data))
    return gzip.compress(gnu_header(TOP, 0, tarfile.DIRTYPE)
                         + gnu_header(f"{TOP}/manifest.json", len(MANIFEST)) + block(MANIFEST)
                         + member + b"\0" * 1024)


def folder_tar(names):
    """The top directory, the manifest, then an empty file per name (a directory for a name
    ending in '/'), in order, written in PAX format so long paths keep every component."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        top = tarfile.TarInfo(TOP)
        top.type = tarfile.DIRTYPE
        top.mode = 0o755
        tar.addfile(top)
        info = tarfile.TarInfo(f"{TOP}/manifest.json")
        info.size = len(MANIFEST)
        tar.addfile(info, io.BytesIO(MANIFEST))
        for name in names:
            info = tarfile.TarInfo(name.rstrip("/"))
            if name.endswith("/"):
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
            tar.addfile(info)
    return out.getvalue()


def under(folders):
    """A file under `folders` new nested folders inside the top directory."""
    return f"{TOP}/" + "".join(f"d{i:02d}/" for i in range(folders)) + "f"


def encrypt(data):
    out = io.BytesIO()
    enc = age_stream.Encryptor(out, PASSPHRASE, LOW)
    enc.write(data)
    enc.close()
    return out.getvalue()


def at_count(count):
    """Fillers giving `count` entries in all: the directory and the manifest count too."""
    return [1] * (count - 2)


def at_size(total):
    """One filler that brings the members' sizes to exactly `total` bytes."""
    return [total - len(MANIFEST)]


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.exports = self.root / "exports"
        self.uploads = self.root / "uploads"
        self.exports.mkdir()
        self.uploads.mkdir()
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(settings, "exports_dir", str(self.exports)))
        stack.enter_context(patch.object(settings, "upload_dir", str(self.uploads)))
        stack.enter_context(patch.object(settings, "sources_dir", str(self.root / "sources")))
        stack.enter_context(patch.dict(importer._jobs, {}, clear=True))
        self.stack = stack

    def package(self, fillers, encrypted=False, data=None):
        data = tar_bytes(fillers) if data is None else data
        name = f"ragpkg-{uuid.uuid4().hex[:8]}.tar.gz" + (".age" if encrypted else "")
        archive = self.exports / name
        archive.write_bytes(encrypt(data) if encrypted else data)
        return archive

    def too_many_message(self, archive):
        return (f"'{archive.name}' holds more than 20 entries, the most a package may hold "
                f"(MAX_PACKAGE_MEMBERS), so nothing from it was imported.")

    def too_large_message(self, archive):
        return (f"'{archive.name}' expands to more than 1 MB when unpacked, the most a package "
                f"may expand to (MAX_PACKAGE_EXPANDED_BYTES), so nothing from it was imported.")


class SettingsTests(unittest.TestCase):
    def test_the_caps_come_from_the_environment(self):
        self.assertEqual(settings.max_package_expanded_bytes, CAP_BYTES)
        self.assertEqual(settings.max_package_members, CAP_MEMBERS)

    def test_the_cap_text(self):
        self.assertEqual(packager._limit_text(16 * 1024 ** 3), "16,384 MB")
        self.assertEqual(packager._limit_text(1048576), "1 MB")
        self.assertEqual(packager._limit_text(1000), "1,000 bytes")


class OpenPackageTests(Fixture):
    def open(self, archive):
        dest = self.root / ("work-" + uuid.uuid4().hex[:6])
        dest.mkdir()
        passphrase = PASSPHRASE if archive.name.endswith(".age") else None
        return dest, (lambda: packager.open_package(archive, dest, passphrase))

    def refused(self, archive, code):
        dest, run = self.open(archive)
        with self.assertRaises(packager.PackageError) as caught:
            run()
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        return caught.exception, dest

    def written(self, dest):
        return sum(p.stat().st_size for p in dest.rglob("*") if p.is_file())

    def test_a_package_at_each_cap_opens(self):
        for encrypted in (False, True):
            for label, fillers, entries in (("members", at_count(CAP_MEMBERS), CAP_MEMBERS),
                                            ("bytes", at_size(CAP_BYTES), 3)):
                with self.subTest(encrypted=encrypted, cap=label):
                    dest, run = self.open(self.package(fillers, encrypted))
                    pkg, manifest = run()
                    self.assertEqual(pkg.name, TOP)
                    self.assertEqual(manifest["package_format"], 1)
                    # The directory entry is one of the entries counted.
                    self.assertEqual(len(list(dest.rglob("*"))), entries)
                    if label == "bytes":
                        self.assertEqual(self.written(dest), CAP_BYTES)

    def test_one_entry_over_is_refused(self):
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                archive = self.package(at_count(CAP_MEMBERS + 1), encrypted)
                error, dest = self.refused(archive, "PACKAGE_TOO_MANY_MEMBERS")
                self.assertEqual(error.message, self.too_many_message(archive))
                self.assertEqual(error.detail, {"limit": CAP_MEMBERS})
                if not encrypted:
                    self.assertEqual(list(dest.iterdir()), [])
                else:
                    # Extracted one at a time: the 20 entries before the refusal at most.
                    self.assertLessEqual(len(list(dest.rglob("*"))), CAP_MEMBERS)

    def test_one_byte_over_is_refused(self):
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                archive = self.package(at_size(CAP_BYTES + 1), encrypted)
                error, dest = self.refused(archive, "PACKAGE_TOO_LARGE")
                self.assertEqual(error.message, self.too_large_message(archive))
                self.assertEqual(error.detail, {"limit": CAP_BYTES, "member": f"{TOP}/fill-000.bin"})
                self.assertFalse((dest / TOP / "fill-000.bin").exists())
                if not encrypted:
                    self.assertEqual(list(dest.iterdir()), [])

    def test_an_encrypted_refusal_leaves_no_more_than_the_cap(self):
        # Two fillers that fit and a third that goes over: the first two are extracted.
        half = (CAP_BYTES - len(MANIFEST)) // 2
        archive = self.package([half, half, 4096], encrypted=True)
        error, dest = self.refused(archive, "PACKAGE_TOO_LARGE")
        self.assertEqual(error.detail["member"], f"{TOP}/fill-002.bin")
        self.assertLessEqual(self.written(dest), CAP_BYTES)
        self.assertTrue((dest / TOP / "fill-001.bin").is_file())
        self.assertFalse((dest / TOP / "fill-002.bin").exists())

    def test_headers_past_the_refusing_entry_are_not_read(self):
        archive = self.package(at_count(30))
        real = tarfile.TarInfo.fromtarfile
        with patch.object(tarfile.TarInfo, "fromtarfile", side_effect=real) as headers:
            self.refused(archive, "PACKAGE_TOO_MANY_MEMBERS")
        # fromtarfile reads one header per call; the 21st is the one refused.
        self.assertEqual(headers.call_count, CAP_MEMBERS + 1)

    def test_a_negative_size_is_refused_before_it_is_counted(self):
        # Counted, a size of -10 MiB would let the 2 MiB file after it pass the 1 MiB cap.
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                data = negative_size_tar()
                archive = self.exports / ("ragpkg-negative.tar.gz" + (".age" if encrypted else ""))
                archive.write_bytes(encrypt(data) if encrypted else data)
                error, dest = self.refused(archive, "PACKAGE_UNREADABLE")
                self.assertEqual(error.message, f"Package contains an entry with a negative size "
                                                f"('{TOP}/hole').")
                self.assertEqual(error.detail, {"member": f"{TOP}/hole"})
                self.assertFalse((dest / TOP / "hole" / "big.bin").exists())
                if not encrypted:
                    self.assertEqual(list(dest.iterdir()), [])

    def test_a_sparse_file_is_refused(self):
        # Counted at its real size, 0, a sparse file's map would write 4 MiB under the 1 MiB cap.
        for kind in SPARSE_KINDS:
            for encrypted in (False, True):
                with self.subTest(kind=kind, encrypted=encrypted):
                    archive = self.package(None, encrypted, sparse_tar(kind))
                    error, dest = self.refused(archive, "PACKAGE_UNREADABLE")
                    self.assertEqual(error.message, f"Package contains a sparse file ('{SPARSE}'), "
                                                    f"which is not allowed.")
                    self.assertEqual(error.detail, {"member": SPARSE})
                    self.assertFalse((dest / SPARSE).exists())
                    self.assertLessEqual(self.written(dest), len(MANIFEST))
                    if not encrypted:
                        self.assertEqual(list(dest.iterdir()), [])

    def test_implied_folders_over_the_cap_are_refused(self):
        # The top directory, the manifest and the file are 3 entries; 18 new folders make 21.
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                archive = self.package(None, encrypted, folder_tar([under(18)]))
                error, dest = self.refused(archive, "PACKAGE_TOO_MANY_MEMBERS")
                self.assertEqual(error.message, self.too_many_message(archive))
                self.assertEqual(error.detail, {"limit": CAP_MEMBERS})
                self.assertFalse((dest / TOP / "d00").exists())
                if not encrypted:
                    self.assertEqual(list(dest.iterdir()), [])

    def test_implied_folders_at_the_cap_open(self):
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                dest, run = self.open(self.package(None, encrypted, folder_tar([under(17)])))
                run()
                self.assertTrue((dest / under(17)).is_file())
                self.assertEqual(len(list(dest.rglob("*"))), CAP_MEMBERS)

    def test_a_named_folder_counts_once(self):
        # A directory entry and 17 files in it: 20 entries, the folder counted by its entry.
        names = [f"{TOP}/d/"] + [f"{TOP}/d/f{i:02d}" for i in range(17)]
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                dest, run = self.open(self.package(None, encrypted, folder_tar(names)))
                run()
                self.assertEqual(len(list(dest.rglob("*"))), CAP_MEMBERS)

    def test_folders_named_before_dotdot_count(self):
        # Resolved, the path is TOP/new/f: one folder. os.makedirs still makes x00 to x16
        # on the way, so 17 + 1 folders and 3 entries are 21.
        name = (f"{TOP}/" + "".join(f"x{i:02d}/" for i in range(17)) + "../" * 17 + "new/f")
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                archive = self.package(None, encrypted, folder_tar([name]))
                error, dest = self.refused(archive, "PACKAGE_TOO_MANY_MEMBERS")
                self.assertEqual(error.detail, {"limit": CAP_MEMBERS})
                self.assertFalse((dest / TOP / "x00").exists())
                if not encrypted:
                    self.assertEqual(list(dest.iterdir()), [])

    def test_a_link_before_the_caps_still_refuses_as_unreadable(self):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode="w:gz") as tar:
            link = tarfile.TarInfo(f"{TOP}/link")
            link.type = tarfile.SYMTYPE
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        archive = self.exports / "ragpkg-link.tar.gz"
        archive.write_bytes(out.getvalue())
        error, _dest = self.refused(archive, "PACKAGE_UNREADABLE")
        self.assertEqual(error.detail, {"member": f"{TOP}/link"})


class RunTests(Fixture):
    """Through the import job: the refusal is its result and nothing else changes."""

    def setUp(self):
        super().setUp()
        self.create = MagicMock(side_effect=RuntimeError("reached collection creation"))
        for target, value in ((importer, "_check_embedding"), (importer, "_ensure_models")):
            self.stack.enter_context(patch.object(target, value, return_value=[]))
        self.stack.enter_context(patch.object(wc, "_create_collection_sync", self.create))
        self.stack.enter_context(patch.object(wc, "_collection_exists_sync", MagicMock(return_value=False)))
        self.stack.enter_context(patch.object(wc, "get_client", MagicMock(
            side_effect=AssertionError("no Weaviate client may be opened"))))
        self.stack.enter_context(patch.object(importer, "_log"))

    def run_import(self, archive):
        job_id = "job" + uuid.uuid4().hex[:5]
        importer._jobs[job_id] = {"job_id": job_id, "status": "queued", "filename": archive.name,
                                  "on_conflict": "rename", "collection": None,
                                  "original_collection": None, "chunks_written": 0,
                                  "fidelity": None, "renamed": False, "notes": [],
                                  "restored_sessions": [], "error": None, "error_code": None,
                                  "error_detail": None}
        with importer._lock:
            importer._active.add(archive.name)
        importer._run(job_id, archive.name, "rename")
        return importer._jobs[job_id]

    def test_each_refusal_fails_the_job_and_leaves_nothing(self):
        cases = (("PACKAGE_TOO_MANY_MEMBERS", at_count(CAP_MEMBERS + 1), self.too_many_message,
                  lambda a: {"limit": CAP_MEMBERS}),
                 ("PACKAGE_TOO_LARGE", at_size(CAP_BYTES + 1), self.too_large_message,
                  lambda a: {"limit": CAP_BYTES, "member": f"{TOP}/fill-000.bin"}))
        for code, fillers, message, detail in cases:
            with self.subTest(code=code):
                archive = self.package(fillers)
                job = self.run_import(archive)
                self.assertEqual((job["status"], job["error_code"]), ("failed", code), job["error"])
                self.assertEqual(job["error"], message(archive))
                self.assertEqual(job["error_detail"], detail(archive))
                self.create.assert_not_called()
                self.assertEqual(sorted(p.name for p in self.uploads.glob("import-*")), [])
                markers = self.uploads / "imports_in_progress"
                self.assertEqual(sorted(p.name for p in markers.iterdir()) if markers.exists() else [], [])


if __name__ == "__main__":
    unittest.main()
