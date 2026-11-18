"""Downloading a package deletes it; undownloaded exports expire (#677).

`GET /packages/{filename}` streams a package from the exports folder to a
Developer or above, with the listing's #606 access rule. After a complete
download a package this instance exported (recorded in
`<UPLOAD_DIR>/exported_packages.json` when its export finished) is deleted; an
interrupted download, HEAD, and any package not recorded keep it. A download is
refused 409 PACKAGE_BUSY while an import of the file runs or another download
of it is in progress. The sweep deletes recorded packages older than
EXPORT_RETENTION_HOURS, skipping busy ones. Downloads and deletions are audited
as `package.download` and `package.delete`.

Runs with the API's dependencies, in the built API image with no network
(`05_transfer.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_package_download.py

or on the host with them installed. Every test has its own account store,
upload, exports and audit folder. Requests go through `main.app` with FastAPI's
TestClient, without the lifespan; Weaviate is a fake set of collection names.
On the code before #677 every route, record and sweep case fails.
"""
import asyncio
import io
import json
import logging
import os
import sys
import tarfile
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

API_DIR = os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api")
sys.path.insert(0, API_DIR)

from fastapi.testclient import TestClient
from pydantic import ValidationError

import config
from config import settings
from main import app
from auth import passwords, store
from routers import transfer
from services import audit, exporter, importer, package_retention
from services import weaviate_client as wc

ORIGIN = "http://testserver"
SECRET = "Secret"
NOW = datetime(2026, 11, 17, 12, 0, 0, tzinfo=timezone.utc)


def package_bytes(collection, size=0):
    """A readable .tar.gz package of `collection`, padded with `size` random bytes."""
    stem = "ragpkg-x"
    manifest = {"package_format": 1, "created_at": "2026-11-17T12:00:00Z",
                "collection": {"name": collection, "chunk_count": 1, "source_document_count": 0},
                "fidelity": "chunks-only", "files": {}}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in ((f"{stem}/manifest.json", json.dumps(manifest).encode()),
                           (f"{stem}/chunks.jsonl", os.urandom(size))):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


class DownloadTestCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        for name, value in (("audit_dir", str(self.root / "audit")), ("auth_dir", str(self.root / "auth")),
                            ("upload_dir", str(self.root / "uploads")), ("exports_dir", str(self.root / "exports")),
                            ("export_retention_hours", 24)):
            target = patch.object(settings, name, value)
            target.start()
            self.addCleanup(target.stop)
        self.exports = self.root / "exports"
        self.exports.mkdir()
        (self.root / "uploads").mkdir()
        self.collections = {SECRET, "Open1"}

        async def exists(name):
            return name[:1].upper() + name[1:] in self.collections
        target = patch.object(wc, "collection_exists", exists)
        target.start()
        self.addCleanup(target.stop)
        for username, role in (("eng", "engineer"), ("dev", "developer"), ("dev2", "developer"),
                               ("user", "end_user")):
            store.create_user(username, role, passwords.generate_password(), bootstrap=not store.is_set_up(),
                              must_change_password=False)
        self.client = TestClient(app, raise_server_exceptions=False)
        self.addCleanup(self.client.close)
        self.addCleanup(package_retention._downloading.clear)
        self.addCleanup(importer._active.clear)

    def as_(self, who="dev"):
        token = store.create_session(store.get_user(who).id)
        return {"Cookie": f"{store.COOKIE}={token}", "Origin": ORIGIN}

    def write(self, name, collection="Open1", size=0, recorded_hours_ago=None):
        path = self.exports / name
        path.write_bytes(package_bytes(collection, size))
        if recorded_hours_ago is not None:
            package_retention.record_export(name, datetime.now(timezone.utc) - timedelta(hours=recorded_hours_ago))
        return path

    def records(self, action=None):
        return [r for r in reversed(list(audit.newest_first())) if action is None or r["action"] == action]

    def get(self, name, who="dev", method="GET"):
        return self.client.request(method, f"/packages/{name}", headers=self.as_(who))


class SettingTests(unittest.TestCase):
    def test_retention_hours_must_be_1_to_720(self):
        for good in ("1", "24", "720"):
            with self.subTest(value=good), patch.dict(os.environ, {"EXPORT_RETENTION_HOURS": good}):
                self.assertEqual(config.Settings().export_retention_hours, int(good))
        for bad in ("0", "-1", "721", "abc", "1.5"):
            with self.subTest(value=bad), patch.dict(os.environ, {"EXPORT_RETENTION_HOURS": bad}):
                with self.assertRaises(ValidationError) as caught:
                    config.Settings()
                self.assertTrue(any("EXPORT_RETENTION_HOURS" in line for line in config.refusal_lines(caught.exception)))

    def test_the_default_is_24(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("EXPORT_RETENTION_HOURS", None)
            self.assertEqual(config.Settings().export_retention_hours, 24)


class RecordTests(DownloadTestCase):
    def test_a_recorded_package_expires_after_the_setting(self):
        package_retention.record_export("ragpkg-a-1.tar.gz", NOW)
        self.assertEqual(package_retention.expires_at("ragpkg-a-1.tar.gz"), "2026-11-18T12:00:00Z")
        with patch.object(settings, "export_retention_hours", 2):
            self.assertEqual(package_retention.expires_at("ragpkg-a-1.tar.gz"), "2026-11-17T14:00:00Z")
        self.assertIsNone(package_retention.expires_at("ragpkg-other.tar.gz"))
        self.assertTrue((self.root / "uploads" / "exported_packages.json").is_file())
        self.assertFalse(list(self.exports.iterdir()), "the record is never written in ./exports")

    def test_an_unreadable_record_is_moved_aside_never_overwritten(self):
        record = self.root / "uploads" / "exported_packages.json"
        record.write_text("{not json")
        with self.assertLogs("services.package_retention", "ERROR") as logs:
            self.assertEqual(package_retention.expiries(), {})
        aside = [p for p in record.parent.iterdir() if p.name.startswith("exported_packages.json.corrupt-")]
        self.assertEqual(len(aside), 1)
        self.assertEqual(aside[0].read_text(), "{not json")
        self.assertIn(aside[0].name, "\n".join(logs.output))
        self.assertFalse(record.exists())
        package_retention.record_export("ragpkg-new.tar.gz", NOW)      # a new record starts
        self.assertTrue(package_retention.recorded("ragpkg-new.tar.gz"))
        self.assertEqual(aside[0].read_text(), "{not json")

    def test_a_record_that_cant_be_moved_aside_is_not_overwritten(self):
        record = self.root / "uploads" / "exported_packages.json"
        record.write_text('{"packages": "wrong shape"}')
        self.addCleanup(setattr, package_retention, "_stuck", False)
        real_replace = os.replace

        def replace(src, dst):
            if Path(src) == record:                   # the record can't be moved aside
                raise OSError("read-only")
            return real_replace(src, dst)
        with patch.object(package_retention.os, "replace", replace), \
                self.assertLogs("services.package_retention", "ERROR"):
            self.assertEqual(package_retention.expiries(), {})
            with self.assertRaises(RuntimeError):
                package_retention.record_export("ragpkg-new.tar.gz", NOW)
        self.assertEqual(record.read_text(), '{"packages": "wrong shape"}')

    def run_export(self, record=None):
        result = {"filename": "ragpkg-open1-x.tar.gz", "size_bytes": 1, "chunk_count": 1,
                  "source_document_count": 0, "fidelity": "chunks-only", "models_bundled": False,
                  "retrieve_script": True, "warnings": []}
        exporter._jobs["j677"] = {"job_id": "j677", "status": "queued", "collection": "Open1",
                                  "chunks_written": 0, "filename": None, "warnings": [], "error": None}
        self.addCleanup(exporter._jobs.pop, "j677", None)
        patches = [patch.object(exporter.packager, "build", lambda *a, **k: result)]
        if record is not None:
            patches.append(patch.object(package_retention, "record_export", record))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        exporter._run("j677", "Open1", False)
        return exporter._jobs["j677"]

    def test_a_finished_export_is_recorded_and_its_status_says_when_it_expires(self):
        job = self.run_export()
        self.assertEqual(job["status"], "completed")
        self.assertTrue(package_retention.recorded("ragpkg-open1-x.tar.gz"))
        answer = self.client.get("/export/job/j677", headers=self.as_("eng"))
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.json()["expires_at"], package_retention.expires_at("ragpkg-open1-x.tar.gz"))

    def test_the_export_status_drops_expires_at_once_the_package_is_downloaded(self):
        # SPECIFICATIONS.md GET /packages: the job status carries expires_at only
        # while its package is still recorded (#677).
        self.run_export()
        (self.exports / "ragpkg-open1-x.tar.gz").write_bytes(package_bytes("Open1"))
        self.assertIsNotNone(self.client.get("/export/job/j677", headers=self.as_("eng")).json()["expires_at"])
        self.assertEqual(self.get("ragpkg-open1-x.tar.gz").status_code, 200)
        self.assertFalse((self.exports / "ragpkg-open1-x.tar.gz").exists())
        answer = self.client.get("/export/job/j677", headers=self.as_("eng"))
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertIsNone(answer.json()["expires_at"])

    def test_a_finished_export_records_its_collection(self):
        # #794: an encrypted package exported here follows this collection's access.
        self.run_export()
        self.assertEqual(package_retention.recorded_collections(), {"ragpkg-open1-x.tar.gz": "Open1"})
        entry = json.loads((self.root / "uploads" / "exported_packages.json").read_text())["packages"]
        self.assertEqual(entry["ragpkg-open1-x.tar.gz"]["collection"], "Open1")

    def test_an_entry_written_before_794_is_read_and_rewritten_with_no_collection(self):
        record = self.root / "uploads" / "exported_packages.json"
        record.write_text(json.dumps({"packages": {"ragpkg-old.tar.gz": "2026-11-17T12:00:00Z"}}))
        self.assertEqual(package_retention.expires_at("ragpkg-old.tar.gz"), "2026-11-18T12:00:00Z")
        self.assertEqual(package_retention.recorded_collections(), {"ragpkg-old.tar.gz": None})
        package_retention.record_export("ragpkg-new.tar.gz", NOW, "Open1")
        self.assertEqual(json.loads(record.read_text())["packages"], {
            "ragpkg-old.tar.gz": {"finished_at": "2026-11-17T12:00:00Z", "collection": None},
            "ragpkg-new.tar.gz": {"finished_at": "2026-11-17T12:00:00Z", "collection": "Open1"}})

    def test_an_entry_of_another_shape_moves_the_record_aside(self):
        record = self.root / "uploads" / "exported_packages.json"
        record.write_text(json.dumps({"packages": {"ragpkg-a.tar.gz": {"collection": "Open1"}}}))
        with self.assertLogs("services.package_retention", "ERROR"):
            self.assertEqual(package_retention.expiries(), {})
        self.assertFalse(record.exists())

    def test_a_recorded_name_outside_the_rule_is_ignored_and_never_deleted(self):
        # #794 item 4: a tampered record can't name a file outside the exports folder.
        outside = self.root / "outside.tar.gz"
        outside.write_bytes(b"not a package")
        old = self.write("ragpkg-old.tar.gz", recorded_hours_ago=25)
        record = self.root / "uploads" / "exported_packages.json"
        data = json.loads(record.read_text())
        data["packages"]["../outside.tar.gz"] = "2026-01-01T00:00:00Z"
        record.write_text(json.dumps(data))
        with self.assertLogs("services.package_retention", "WARNING") as logs:
            self.assertEqual(package_retention.sweep(), ["ragpkg-old.tar.gz"])
        self.assertIn("'../outside.tar.gz'", "\n".join(logs.output))
        self.assertTrue(outside.exists())
        self.assertFalse(old.exists())
        self.assertNotIn("../outside.tar.gz", package_retention.expiries())

    def test_an_export_whose_record_fails_still_completes_and_says_so(self):
        def broken(*_):
            raise OSError("disk full")
        with self.assertLogs("services.exporter", "ERROR"):
            job = self.run_export(record=broken)
        self.assertEqual(job["status"], "completed")
        self.assertTrue(any("expiry could not be recorded" in w for w in job["warnings"]))
        answer = self.client.get("/export/job/j677", headers=self.as_("eng"))
        self.assertIsNone(answer.json()["expires_at"])


class ListingTests(DownloadTestCase):
    def test_the_listing_says_when_recorded_packages_expire(self):
        self.write("ragpkg-mine.tar.gz", recorded_hours_ago=1)
        self.write("ragpkg-dropped-in.tar.gz")
        answer = self.client.get("/packages", headers=self.as_())
        packages = {p["filename"]: p for p in answer.json()["packages"]}
        self.assertEqual(packages["ragpkg-mine.tar.gz"]["expires_at"],
                         package_retention.expires_at("ragpkg-mine.tar.gz"))
        self.assertIsNone(packages["ragpkg-dropped-in.tar.gz"]["expires_at"])


class DownloadTests(DownloadTestCase):
    def test_a_complete_download_deletes_a_recorded_package(self):
        path = self.write("ragpkg-mine.tar.gz", size=3 * 1024 * 1024, recorded_hours_ago=0)
        data = path.read_bytes()
        answer = self.get("ragpkg-mine.tar.gz")
        self.assertEqual(answer.status_code, 200, answer.text)
        self.assertEqual(answer.content, data)
        self.assertEqual(answer.headers["content-type"], "application/octet-stream")
        self.assertEqual(answer.headers["content-length"], str(len(data)))
        self.assertEqual(answer.headers["content-disposition"], 'attachment; filename="ragpkg-mine.tar.gz"')
        self.assertEqual(answer.headers["cache-control"], "no-store")
        self.assertEqual(answer.headers["x-accel-buffering"], "no")
        self.assertFalse(path.exists())
        self.assertFalse(package_retention.recorded("ragpkg-mine.tar.gz"))
        self.assertFalse(package_retention._downloading)
        download = self.records("package.download")[-1]
        self.assertEqual((download["actor"], download["result"], download["details"]["file"],
                          download["details"]["completed"]), ("dev", "success", "ragpkg-mine.tar.gz", True))
        deleted = self.records("package.delete")[-1]
        self.assertEqual((deleted["actor"], deleted["actor_kind"], deleted["result"], deleted["details"]),
                         ("dev", "user", "success", {"file": "ragpkg-mine.tar.gz", "reason": "downloaded"}))

    def test_a_package_not_exported_here_is_served_and_kept(self):
        path = self.write("ragpkg-dropped-in.tar.gz")
        answer = self.get("ragpkg-dropped-in.tar.gz")
        self.assertEqual((answer.status_code, answer.content), (200, path.read_bytes()))
        self.assertTrue(path.exists())
        self.assertEqual(self.records("package.delete"), [])
        self.assertTrue(self.records("package.download")[-1]["details"]["completed"])

    def test_head_answers_the_headers_and_keeps_the_package(self):
        path = self.write("ragpkg-mine.tar.gz", recorded_hours_ago=0)
        answer = self.get("ragpkg-mine.tar.gz", method="HEAD")
        self.assertEqual(answer.status_code, 200)
        self.assertEqual(answer.content, b"")
        self.assertEqual(answer.headers["content-length"], str(path.stat().st_size))
        self.assertTrue(path.exists())
        self.assertEqual(self.records("package.delete"), [])

    def test_bad_names_missing_files_and_links_are_not_found(self):
        (self.exports / "ragpkg-dir.tar.gz").mkdir()
        target = self.write("ragpkg-target.tar.gz", recorded_hours_ago=0)
        os.symlink(target, self.exports / "ragpkg-link.tar.gz")
        (self.exports / ".ragpkg-hidden.tar.gz").write_bytes(b"x")
        (self.exports / "notes.txt").write_bytes(b"x")
        for name in ("ragpkg-missing.tar.gz", "ragpkg-dir.tar.gz", "ragpkg-link.tar.gz", ".ragpkg-hidden.tar.gz",
                     "notes.txt", "ragpkg-a%20b.tar.gz"):
            with self.subTest(name=name):
                answer = self.get(name)
                self.assertEqual(answer.status_code, 404, answer.text)
                self.assertEqual(answer.json()["error"]["code"], "PACKAGE_NOT_FOUND")
        # A path with a separator never reaches the route.
        self.assertEqual(self.get("..%2Fetc%2Fpasswd").status_code, 404)
        self.assertTrue(target.exists(), "a link to a package never deletes it")

    def test_a_role_below_developer_is_refused(self):
        path = self.write("ragpkg-mine.tar.gz", recorded_hours_ago=0)
        self.assertEqual(self.get("ragpkg-mine.tar.gz", who="user").status_code, 403)
        self.assertTrue(path.exists())

    def test_a_restricted_collections_package_answers_as_missing_and_is_audited(self):
        status = self.client.put(f"/collections/{SECRET}/access", headers=self.as_("eng"),
                                 json={"restricted": True, "users": ["dev"], "service_principals": []}).status_code
        self.assertEqual(status, 200)
        path = self.write("ragpkg-secret.tar.gz", collection=SECRET, recorded_hours_ago=0)
        answer = self.get("ragpkg-secret.tar.gz", who="dev2")
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (404, "PACKAGE_NOT_FOUND"))
        self.assertEqual(answer.json()["error"]["message"],
                         "No package named 'ragpkg-secret.tar.gz' in the exports directory.")
        self.assertTrue(path.exists())
        refused = [r for r in self.records("package.download") if r["actor"] == "dev2"][0]
        self.assertEqual((refused["collection"], refused["details"]["reason"], refused["details"]["code"]),
                         (SECRET, "collection_access", "PACKAGE_NOT_FOUND"))
        self.assertEqual(self.get("ragpkg-secret.tar.gz", who="dev").status_code, 200)
        self.assertFalse(path.exists())

    def test_a_package_being_imported_or_downloaded_is_busy(self):
        path = self.write("ragpkg-mine.tar.gz", recorded_hours_ago=0)
        importer._active.add("ragpkg-mine.tar.gz")
        answer = self.get("ragpkg-mine.tar.gz")
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (409, "PACKAGE_BUSY"))
        self.assertEqual(self.get("ragpkg-mine.tar.gz", method="HEAD").status_code, 409)
        importer._active.clear()
        self.assertTrue(package_retention.claim("ragpkg-mine.tar.gz"))
        answer = self.get("ragpkg-mine.tar.gz")
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (409, "PACKAGE_BUSY"))
        package_retention.release("ragpkg-mine.tar.gz")
        self.assertTrue(path.exists())
        self.assertEqual(self.get("ragpkg-mine.tar.gz").status_code, 200)

    def test_an_import_started_during_the_download_keeps_the_package(self):
        path = self.write("ragpkg-mine.tar.gz", recorded_hours_ago=0)
        real_claim = package_retention.claim

        def claim_then_import(name):
            claimed = real_claim(name)
            importer._active.add(name)              # an import starts once the download has begun
            return claimed
        with patch.object(package_retention, "claim", claim_then_import):
            self.assertEqual(self.get("ragpkg-mine.tar.gz").status_code, 200)
        self.assertTrue(path.exists())
        self.assertTrue(package_retention.recorded("ragpkg-mine.tar.gz"))


class ImportClaimTests(DownloadTestCase):
    def test_an_import_claim_waits_while_a_sweep_holds_the_lock(self):
        # #677: the sweep checks busy and deletes under package_retention's lock;
        # an import claims its package under the same lock, so it can't slip between.
        import threading
        holding, release = threading.Event(), threading.Event()

        def sweep_like():
            with package_retention.lock():
                holding.set()
                release.wait(5)
        worker = threading.Thread(target=sweep_like)
        worker.start()
        holding.wait(5)
        claimed = threading.Event()
        claimer = threading.Thread(target=lambda: (importer._claim_package("ragpkg-x.tar.gz", "ragpkg-x.tar.gz"),
                                                   claimed.set()))
        claimer.start()
        self.assertFalse(claimed.wait(0.3), "the import claimed its package while the lock was held")
        release.set()
        self.assertTrue(claimed.wait(5))
        worker.join(5)
        claimer.join(5)
        self.assertTrue(importer.package_busy("ragpkg-x.tar.gz"))
        with self.assertRaises(importer.ImportInProgressError):
            importer._claim_package("ragpkg-x.tar.gz", "ragpkg-x.tar.gz")


class InterruptedDownloadTests(DownloadTestCase):
    def drive(self, path, disconnect_after):
        """Run the response with a client that goes away after `disconnect_after` body messages."""
        sent, gone = [], asyncio.Event()

        async def receive():
            await gone.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)
            if message["type"] == "http.response.body" and len(sent) - 1 >= disconnect_after:
                gone.set()
                await asyncio.sleep(0)
        response = transfer._PackageStream(path, path.name, head=False)
        asyncio.run(response({"type": "http", "method": "GET", "headers": []}, receive, send))
        return sent

    def test_a_client_that_goes_away_keeps_the_package(self):
        path = self.write("ragpkg-mine.tar.gz", size=3 * 1024 * 1024, recorded_hours_ago=0)
        sent = self.drive(path, disconnect_after=1)
        self.assertFalse(any(m.get("more_body") is False for m in sent if m["type"] == "http.response.body"),
                         "an interrupted download never sends the final message")
        self.assertTrue(path.exists())
        self.assertTrue(package_retention.recorded(path.name))
        self.assertFalse(package_retention._downloading, "the slot is released")
        self.assertEqual(self.records("package.delete"), [])

    def test_the_audit_record_says_an_interrupted_download_was_incomplete(self):
        path = self.write("ragpkg-mine.tar.gz", size=3 * 1024 * 1024, recorded_hours_ago=0)
        ctx = {"details": {}}
        token = audit._current.set(ctx)
        try:
            self.drive(path, disconnect_after=1)
        finally:
            audit._current.reset(token)
        self.assertIs(ctx["details"]["completed"], False)


class EncryptedAccessTests(DownloadTestCase):
    """#794 item 2 (RD-194 Q1, Q2): an encrypted package exported here follows
    the collection recorded at its export, in the listing and the download."""

    def setUp(self):
        super().setUp()
        status = self.client.put(f"/collections/{SECRET}/access", headers=self.as_("eng"),
                                 json={"restricted": True, "users": ["dev"], "service_principals": []}).status_code
        self.assertEqual(status, 200)

    def write_encrypted(self, name, collection=SECRET, recorded=True):
        path = self.exports / name
        path.write_bytes(b"age-encryption.org/v1\n" + os.urandom(64))
        if recorded:
            package_retention.record_export(name, datetime.now(timezone.utc), collection)
        return path

    def listed(self, who):
        answer = self.client.get("/packages", headers=self.as_(who))
        self.assertEqual(answer.status_code, 200, answer.text)
        return {p["filename"]: p for p in answer.json()["packages"]}

    def test_a_restricted_collections_encrypted_package_is_hidden_from_a_developer_without_access(self):
        path = self.write_encrypted("ragpkg-secret.tar.gz.age")
        self.assertNotIn(path.name, self.listed("dev2"))
        answer = self.get(path.name, who="dev2")
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (404, "PACKAGE_NOT_FOUND"))
        self.assertEqual(answer.json()["error"]["message"],
                         "No package named 'ragpkg-secret.tar.gz.age' in the exports directory.")
        self.assertTrue(path.exists())
        refused = [r for r in self.records("package.download") if r["actor"] == "dev2"][0]
        self.assertEqual((refused["collection"], refused["details"]["reason"], refused["details"]["code"]),
                         (SECRET, "collection_access", "PACKAGE_NOT_FOUND"))
        for who in ("dev", "eng"):
            entry = self.listed(who)[path.name]
            self.assertIsNone(entry["collection"], "the recorded collection isn't revealed")
            self.assertTrue(entry["encrypted"])
        self.assertEqual(self.get(path.name, who="dev").status_code, 200)
        self.assertFalse(path.exists())

    def test_an_open_collections_encrypted_package_is_open(self):
        path = self.write_encrypted("ragpkg-open.tar.gz.age", collection="Open1")
        self.assertIn(path.name, self.listed("dev2"))
        self.assertEqual(self.get(path.name, who="dev2").status_code, 200)

    def test_one_recorded_without_a_collection_is_for_engineers_only(self):
        path = self.write_encrypted("ragpkg-before.tar.gz.age", collection=None)
        self.assertNotIn(path.name, self.listed("dev"))
        answer = self.get(path.name, who="dev")
        self.assertEqual((answer.status_code, answer.json()["error"]["code"]), (404, "PACKAGE_NOT_FOUND"))
        refused = [r for r in self.records("package.download") if r["actor"] == "dev"][0]
        self.assertEqual((refused["collection"], refused["details"]["reason"]), (None, "collection_access"))
        self.assertTrue(path.exists())
        self.assertIn(path.name, self.listed("eng"))
        self.assertEqual(self.get(path.name, who="eng").status_code, 200)
        self.assertFalse(path.exists())

    def test_one_copied_in_is_open_to_any_developer(self):
        path = self.write_encrypted("ragpkg-copied.tar.gz.age", recorded=False)
        self.assertIn(path.name, self.listed("dev2"))
        self.assertEqual(self.get(path.name, who="dev2").status_code, 200)
        self.assertTrue(path.exists(), "a package not exported here is kept")


class DeletionRecordTests(DownloadTestCase):
    def test_a_deletion_is_audited_and_the_sweep_goes_on_when_the_record_cant_be_rewritten(self):
        # #794 item 6: the file is gone, so its package.delete record is written whatever the record does.
        first = self.write("ragpkg-a.tar.gz", recorded_hours_ago=25)
        second = self.write("ragpkg-b.tar.gz", recorded_hours_ago=25)
        with patch.object(package_retention, "_write", side_effect=OSError("disk full")), \
                self.assertLogs("services.package_retention", "ERROR") as logs:
            self.assertEqual(package_retention.sweep(), ["ragpkg-a.tar.gz", "ragpkg-b.tar.gz"])
        self.assertFalse(first.exists())
        self.assertFalse(second.exists())
        self.assertEqual([r["details"]["file"] for r in self.records("package.delete")],
                         ["ragpkg-a.tar.gz", "ragpkg-b.tar.gz"])
        self.assertIn("ragpkg-a.tar.gz", "\n".join(logs.output))
        self.assertEqual(package_retention.sweep(), [])          # the leftover entries are dropped
        self.assertEqual(package_retention.recorded_collections(), {})


class SameOriginTests(DownloadTestCase):
    """#794 item 5 (RD-194 Q4): the deleting GET checks Sec-Fetch-Site."""

    def fetch(self, site, who="dev", method="GET", headers=None):
        headers = dict(headers or self.as_(who))
        if site is not None:
            headers["Sec-Fetch-Site"] = site
        return self.client.request(method, "/packages/ragpkg-mine.tar.gz", headers=headers)

    def test_a_download_from_another_origin_of_the_site_is_refused(self):
        path = self.write("ragpkg-mine.tar.gz", recorded_hours_ago=0)
        for site in ("same-site", "cross-site"):
            with self.subTest(site=site):
                answer = self.fetch(site)
                self.assertEqual((answer.status_code, answer.json()["error"]["code"]),
                                 (403, "CROSS_ORIGIN_REFUSED"))
                self.assertTrue(path.exists())
        refused = self.records("package.download")
        self.assertEqual([(r["actor"], r["result"]) for r in refused], [("dev", "refused")] * 2)

    def test_same_origin_a_typed_url_and_no_header_download(self):
        for site in ("same-origin", "none", None):
            with self.subTest(site=site):
                path = self.write("ragpkg-mine.tar.gz", recorded_hours_ago=0)
                self.assertEqual(self.fetch(site).status_code, 200)
                self.assertFalse(path.exists())

    def test_head_and_a_service_key_are_not_checked(self):
        path = self.write("ragpkg-mine.tar.gz", recorded_hours_ago=0)
        self.assertEqual(self.fetch("cross-site", method="HEAD").status_code, 200)
        store.create_principal("svc", "developer")
        key = store.issue_key("svc")[1]
        self.assertEqual(self.fetch("cross-site", headers={"Authorization": "Bearer " + key}).status_code, 200)
        self.assertFalse(path.exists())


class HeadRecordTests(DownloadTestCase):
    def test_head_is_audited_as_a_header_check(self):
        # #794 item 7 (RD-194 Q5): head true, no completed.
        self.write("ragpkg-mine.tar.gz", recorded_hours_ago=0)
        self.assertEqual(self.get("ragpkg-mine.tar.gz", method="HEAD").status_code, 200)
        details = self.records("package.download")[-1]["details"]
        self.assertIs(details["head"], True)
        self.assertNotIn("completed", details)
        self.assertEqual(self.get("ragpkg-mine.tar.gz").status_code, 200)
        details = self.records("package.download")[-1]["details"]
        self.assertIs(details["completed"], True)
        self.assertNotIn("head", details)


class CancelledClaimTests(DownloadTestCase):
    """#794 item 8: a claim whose request is cancelled while its thread runs is
    released once the thread ends."""

    def blocked(self, real):
        import threading
        entered, proceed, self.claimed = threading.Event(), threading.Event(), threading.Event()

        def claim(*args):
            entered.set()
            proceed.wait(5)
            try:
                return real(*args)
            finally:
                self.claimed.set()
        return claim, entered, proceed

    def cancel_during_claim(self, start, entered, proceed, released):
        def settled():
            return self.claimed.is_set() and released()

        async def scenario():
            task = asyncio.ensure_future(start())
            await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            proceed.set()
            for _ in range(500):
                if settled():
                    return True
                await asyncio.sleep(0.01)
            return False
        return asyncio.run(scenario())

    def test_a_cancelled_import_start_releases_its_package(self):
        claim, entered, proceed = self.blocked(importer._claim_package)
        with patch.object(importer, "_claim_package", claim):
            settled = self.cancel_during_claim(
                lambda: importer.start_import_job("ragpkg-x.tar.gz", "abort"), entered, proceed,
                lambda: not importer._active and not package_retention._settling)
        self.assertTrue(settled, "the import claim was never released")
        self.assertFalse(importer.package_busy("ragpkg-x.tar.gz"))

    def test_a_cancelled_download_releases_its_slot(self):
        path = self.write("ragpkg-mine.tar.gz", recorded_hours_ago=0)
        claim, entered, proceed = self.blocked(package_retention.claim)
        response = transfer._PackageStream(path, path.name, head=False)

        async def receive():
            await asyncio.sleep(3600)

        async def send(message):
            pass
        with patch.object(package_retention, "claim", claim):
            settled = self.cancel_during_claim(
                lambda: response({"type": "http", "method": "GET", "headers": []}, receive, send),
                entered, proceed,
                lambda: not package_retention._downloading and not package_retention._settling)
        self.assertTrue(settled, "the download slot was never released")
        self.assertFalse(package_retention.busy(path.name))
        self.assertTrue(path.exists())


class SweepTests(DownloadTestCase):
    def test_an_expired_package_is_swept_and_a_fresh_one_kept(self):
        old = self.write("ragpkg-old.tar.gz", recorded_hours_ago=25)
        fresh = self.write("ragpkg-fresh.tar.gz", recorded_hours_ago=1)
        dropped = self.write("ragpkg-dropped-in.tar.gz")
        os.utime(dropped, (0, 0))                    # an old file that isn't recorded
        self.assertEqual(package_retention.sweep(), ["ragpkg-old.tar.gz"])
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(dropped.exists())
        self.assertFalse(package_retention.recorded("ragpkg-old.tar.gz"))
        deleted = self.records("package.delete")[-1]
        self.assertEqual((deleted["actor"], deleted["actor_kind"], deleted["details"]),
                         ("system", "system", {"file": "ragpkg-old.tar.gz", "reason": "expired"}))

    def test_a_busy_package_waits_for_the_next_run(self):
        old = self.write("ragpkg-old.tar.gz", recorded_hours_ago=25)
        importer._active.add("ragpkg-old.tar.gz")
        self.assertEqual(package_retention.sweep(), [])
        importer._active.clear()
        self.assertTrue(package_retention.claim("ragpkg-old.tar.gz"))
        self.assertEqual(package_retention.sweep(), [])
        package_retention.release("ragpkg-old.tar.gz")
        self.assertTrue(old.exists())
        self.assertEqual(package_retention.sweep(), ["ragpkg-old.tar.gz"])

    def test_an_entry_whose_file_is_gone_is_dropped(self):
        old = self.write("ragpkg-old.tar.gz", recorded_hours_ago=25)
        old.unlink()
        self.assertEqual(package_retention.sweep(), [])
        self.assertFalse(package_retention.recorded("ragpkg-old.tar.gz"))
        self.assertEqual(self.records("package.delete"), [])

    def test_a_recorded_name_that_is_now_a_link_is_left_and_dropped(self):
        target = self.root / "elsewhere.tar.gz"
        target.write_bytes(b"keep me")
        package_retention.record_export("ragpkg-old.tar.gz", datetime.now(timezone.utc) - timedelta(hours=25))
        os.symlink(target, self.exports / "ragpkg-old.tar.gz")
        with self.assertLogs("services.package_retention", "WARNING"):
            self.assertEqual(package_retention.sweep(), [])
        self.assertTrue((self.exports / "ragpkg-old.tar.gz").is_symlink())
        self.assertEqual(target.read_bytes(), b"keep me")
        self.assertFalse(package_retention.recorded("ragpkg-old.tar.gz"))

    def test_the_setting_decides_when_a_package_has_expired(self):
        self.write("ragpkg-mine.tar.gz", recorded_hours_ago=3)
        self.assertEqual(package_retention.sweep(), [])
        with patch.object(settings, "export_retention_hours", 2):
            self.assertEqual(package_retention.sweep(), ["ragpkg-mine.tar.gz"])


class SweeperTests(DownloadTestCase):
    """Q3 (RD-145): the sweep runs at start-up and then every 15 minutes, fixed in code."""

    def test_the_sweeper_sweeps_at_once_then_every_15_minutes_and_survives_a_failure(self):
        self.assertEqual(package_retention.SWEEP_INTERVAL_S, 15 * 60)
        calls, sleeps = [], []

        def sweep():
            calls.append(len(sleeps))
            if len(calls) == 2:
                raise RuntimeError("synthetic sweep failure")
            return []

        async def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 3:
                raise asyncio.CancelledError

        with patch.object(package_retention, "sweep", sweep), \
                patch.object(package_retention.asyncio, "sleep", sleep), \
                self.assertLogs("services.package_retention", "ERROR"):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(package_retention.sweeper())
        # The first sweep comes before any wait; a failed sweep doesn't stop the next one.
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual(sleeps, [900, 900, 900])

    def test_the_api_start_sweeps_an_expired_package_and_keeps_others(self):
        # The real lifespan, in another process (as test_audit's lifespan test).
        old = self.write("ragpkg-old.tar.gz", recorded_hours_ago=25)
        fresh = self.write("ragpkg-fresh.tar.gz", recorded_hours_ago=1)
        dropped = self.write("ragpkg-dropped-in.tar.gz")
        os.utime(dropped, (0, 0))
        script = """
import asyncio, sys
from unittest.mock import AsyncMock, patch
sys.path.insert(0, sys.argv[1])
from main import app, lifespan
from services import importer, packager, weaviate_client as wc
async def run():
    with patch.object(wc, 'sweep_staging', new=AsyncMock(return_value=[])), \\
         patch.object(importer, 'sweep_interrupted_imports', return_value=[]), \\
         patch.object(importer, 'sweep_stale_workdirs', return_value=[]), \\
         patch.object(packager, 'sweep_build_leftovers', return_value=[]), \\
         patch.object(wc, 'close_client'):
        async with lifespan(app):
            await asyncio.sleep(2)
asyncio.run(run())
"""
        import subprocess
        env = {**os.environ, "AUDIT_DIR": str(self.root / "audit"), "AUTH_DIR": str(self.root / "auth"),
               "UPLOAD_DIR": str(self.root / "uploads"), "EXPORTS_DIR": str(self.exports),
               "EXPORT_RETENTION_HOURS": "24"}
        result = subprocess.run([sys.executable, "-c", script, API_DIR], capture_output=True, text=True,
                                timeout=120, cwd=API_DIR, env=env)
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertFalse(old.exists(), "the start-up sweep did not delete the expired package")
        self.assertTrue(fresh.exists())
        self.assertTrue(dropped.exists())
        deleted = [r for r in self.records("package.delete") if r["details"].get("file") == "ragpkg-old.tar.gz"]
        self.assertEqual([(r["actor"], r["details"]["reason"]) for r in deleted], [("system", "expired")])

    def test_an_invalid_retention_stops_the_api_at_start_up(self):
        import subprocess
        for bad in ("0", "721", "off"):
            env = {**os.environ, "EXPORT_RETENTION_HOURS": bad, "AUDIT_DIR": str(self.root / "audit"),
                   "AUTH_DIR": str(self.root / "auth"), "UPLOAD_DIR": str(self.root / "uploads"),
                   "EXPORTS_DIR": str(self.exports)}
            result = subprocess.run([sys.executable, "-c", "import sys; sys.path.insert(0, sys.argv[1]); import main",
                                     API_DIR], capture_output=True, text=True, timeout=120, cwd=API_DIR, env=env)
            self.assertNotEqual(result.returncode, 0, f"the API imported with EXPORT_RETENTION_HOURS={bad}")
            self.assertIn("EXPORT_RETENTION_HOURS", result.stderr, result.stderr[-1000:])


class AuditActionTests(unittest.TestCase):
    def test_both_actions_are_listed(self):
        self.assertIn("package.download", audit.ALL_ACTIONS)
        self.assertIn("package.delete", audit.ALL_ACTIONS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
