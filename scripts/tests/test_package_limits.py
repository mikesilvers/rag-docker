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

    def package(self, fillers, encrypted=False):
        data = tar_bytes(fillers)
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
