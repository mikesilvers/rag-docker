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

    def test_an_unreadable_record_is_taken_as_empty(self):
        (self.root / "uploads" / "exported_packages.json").write_text("{not json")
        with self.assertLogs("services.package_retention", "ERROR"):
            self.assertEqual(package_retention.expiries(), {})

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
                          download["details"]["complete"]), ("dev", "success", "ragpkg-mine.tar.gz", True))
        deleted = self.records("package.delete")[-1]
        self.assertEqual((deleted["actor"], deleted["actor_kind"], deleted["result"], deleted["details"]),
                         ("dev", "user", "success", {"file": "ragpkg-mine.tar.gz", "reason": "downloaded"}))

    def test_a_package_not_exported_here_is_served_and_kept(self):
        path = self.write("ragpkg-dropped-in.tar.gz")
        answer = self.get("ragpkg-dropped-in.tar.gz")
        self.assertEqual((answer.status_code, answer.content), (200, path.read_bytes()))
        self.assertTrue(path.exists())
        self.assertEqual(self.records("package.delete"), [])
        self.assertTrue(self.records("package.download")[-1]["details"]["complete"])

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
        self.assertIs(ctx["details"]["complete"], False)


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


class AuditActionTests(unittest.TestCase):
    def test_both_actions_are_listed(self):
        self.assertIn("package.download", audit.ALL_ACTIONS)
        self.assertIn("package.delete", audit.ALL_ACTIONS)


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main(verbosity=2)
