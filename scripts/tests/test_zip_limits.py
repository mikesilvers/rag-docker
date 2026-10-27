"""The member-count and compression-ratio caps on an upload's ZIPs, members
whose path leaves the archive, and the bound on a refusal's detail (issue #28).

Runs with the API's dependencies, inside the verify project's API container,
with a small member cap set through the environment:

    docker compose exec -T -e RAG_TEST_API_DIR=/app -e MAX_ZIP_MEMBERS=20 \
        api python - < scripts/tests/test_zip_limits.py

Without MAX_ZIP_MEMBERS the test sets the same cap of 20 itself. The ratio cap
and the total expanded-size cap keep their defaults (200, 2 GiB). Weaviate is
mocked and the ingest worker never runs; nothing reaches a backend.
"""
import asyncio
import gzip
import io
import json
import logging
import os
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))
# Before config is imported: the settings read the environment once.
os.environ.setdefault("MAX_ZIP_MEMBERS", "20")

from fastapi import UploadFile

import config
from config import settings
from routers import ingest
from services import ingest_pipeline
from services import weaviate_client as wc

MEMBERS = settings.max_zip_members
RATIO = settings.max_zip_member_ratio
MIB = 1024 * 1024
LEAVES = "(its path leaves the archive, so it was not extracted)"


def zip_bytes(members, method=zipfile.ZIP_DEFLATED):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", method) as z:
        for name, data in members:
            z.writestr(name, data)
    return buf.getvalue()


def upload(name, data):
    return UploadFile(file=io.BytesIO(data), filename=name)


def bad_crc(data, payload):
    """Flip one byte of a stored member's data: its CRC no longer matches."""
    data = bytearray(data)
    data[data.index(payload)] ^= 0x01
    return bytes(data)


def too_many(name, count, limit):
    return (f"'{name}' holds {count:,} entries, more than {limit:,}, the most one zip may hold, "
            f"so nothing from it was imported. Split it into smaller zips and upload them in "
            f"separate imports.")


def ratio_message(name, member):
    return (f"'{name}' can't be imported: its member '{member}' expands to more than "
            f"{RATIO:,} times its compressed size, which is refused as a possible zip bomb, so "
            f"nothing from it was imported. Remove that file from the zip, or upload it on its "
            f"own, and upload again.")


class SettingTests(unittest.TestCase):
    def test_the_member_cap_comes_from_the_environment(self):
        self.assertEqual(MEMBERS, int(os.environ["MAX_ZIP_MEMBERS"]))

    def test_the_defaults_are_5000_members_and_a_ratio_of_200(self):
        with patch.dict(os.environ):
            os.environ.pop("MAX_ZIP_MEMBERS", None)
            os.environ.pop("MAX_ZIP_MEMBER_RATIO", None)
            defaults = config.Settings(_env_file=None)
        self.assertEqual((defaults.max_zip_members, defaults.max_zip_member_ratio), (5000, 200))

    def test_the_ratio_cap_comes_from_the_environment(self):
        with patch.dict(os.environ, {"MAX_ZIP_MEMBER_RATIO": "50"}):
            self.assertEqual(config.Settings(_env_file=None).max_zip_member_ratio, 50)


class RatioRuleTests(unittest.TestCase):
    """Who is over the ratio cap: members under 1 MiB are exempt, and a member
    exactly at the ratio is accepted."""

    def test_under_one_mib_is_exempt(self):
        self.assertFalse(ingest_pipeline._over_ratio(MIB - 1, 1))
        self.assertFalse(ingest_pipeline._over_ratio(MIB - 1, 0))

    def test_one_mib_and_more_is_checked(self):
        self.assertTrue(ingest_pipeline._over_ratio(MIB, 1))
        self.assertTrue(ingest_pipeline._over_ratio(MIB, 0))

    def test_exactly_the_ratio_is_accepted_and_one_byte_more_refused(self):
        compressed = 8192
        at = RATIO * compressed
        self.assertGreaterEqual(at, MIB)
        self.assertFalse(ingest_pipeline._over_ratio(at, compressed))
        self.assertTrue(ingest_pipeline._over_ratio(at + 1, compressed))

    def test_the_ratio_comes_from_the_setting(self):
        with patch.object(settings, "max_zip_member_ratio", 1000):
            self.assertFalse(ingest_pipeline._over_ratio(1000 * 2048, 2048))
            self.assertTrue(ingest_pipeline._over_ratio(1000 * 2048 + 1, 2048))


class Harness(unittest.TestCase):
    """Posts uploads through the router; no worker runs."""

    def setUp(self):
        self.assertEqual(MEMBERS, 20, "these cases are sized for a member cap of 20")
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.uploads = Path(self.temp.name)
        p = patch.object(settings, "upload_dir", str(self.uploads))
        p.start(); self.addCleanup(p.stop)
        p = patch.object(wc, "collection_exists", AsyncMock(return_value=True))
        p.start(); self.addCleanup(p.stop)
        # An accepted upload queues a job; its worker never runs here.
        self.started = []
        p = patch.object(ingest_pipeline, "_process_job_sync",
                         lambda *args: self.started.append(args))
        p.start(); self.addCleanup(p.stop)

    def post(self, *files):
        self.jobs_before = set(ingest_pipeline._jobs)
        self.uploads_before = set(self.uploads.iterdir())
        self.started_before = len(self.started)
        response = asyncio.run(ingest.ingest_upload(
            collection="Coll", strategy="fixed", chunk_size="200", chunk_overlap="0",
            similarity_threshold="0.85", min_chunk_size="0", files=list(files)))
        if hasattr(response, "status_code"):
            return response.status_code, json.loads(response.body)
        body = response.model_dump()
        self.addCleanup(ingest_pipeline._jobs.pop, body["job_id"], None)
        return 202, body

    def assert_refused(self, status, body, code, message, detail):
        self.assertEqual(status, 400, body)
        self.assertEqual(body["error"], {"code": code, "message": message, "detail": detail})
        # Nothing queued and nothing left behind.
        self.assertEqual(set(ingest_pipeline._jobs), self.jobs_before)
        self.assertEqual(set(self.uploads.iterdir()), self.uploads_before)
        self.assertEqual(len(self.started), self.started_before)

    def assert_ratio_refused(self, status, body, name, member):
        self.assert_refused(status, body, "ARCHIVE_RATIO_TOO_HIGH", ratio_message(name, member), {
            "file": name,
            "members": [{"member": member,
                         "reason": f"expands to more than {RATIO:,} times its compressed size"}],
            "members_omitted": 0})

    def job(self):
        self.assertEqual(len(self.started), 1)
        return ingest_pipeline._jobs[self.started[0][0]]

    def queued_names(self):
        self.assertEqual(len(self.started), 1)
        return sorted(self.started[0][-1].values())

    def reading(self):
        """Records which members validation reads."""
        read = []
        real = ingest_pipeline._member_problem
        return read, patch.object(ingest_pipeline, "_member_problem",
                                  lambda *a, **k: read.append(a[1].filename) or real(*a, **k))


class MemberCapTests(Harness):
    def test_a_zip_at_the_cap_is_accepted(self):
        status, body = self.post(upload("full.zip", zip_bytes(
            [(f"m{i:02}.txt", f"text {i}") for i in range(MEMBERS)])))
        self.assertEqual(status, 202, body)
        self.assertEqual(len(self.queued_names()), MEMBERS)

    def test_one_entry_over_the_cap_is_refused_before_anything_is_read(self):
        read, reading = self.reading()
        with reading, self.assertLogs("services.ingest_pipeline", "WARNING") as logs:
            status, body = self.post(
                upload("over.zip", zip_bytes([(f"m{i:02}.txt", "t") for i in range(MEMBERS + 1)])),
                upload("other.txt", b"A separate, valid upload."))
        self.assert_refused(status, body, "ARCHIVE_TOO_MANY_MEMBERS",
                            too_many("over.zip", MEMBERS + 1, MEMBERS),
                            {"file": "over.zip", "members": [], "members_omitted": 0})
        self.assertEqual(read, [])
        self.assertIn("ARCHIVE_TOO_MANY_MEMBERS", logs.output[0])

    def test_every_entry_counts(self):
        # Folders, an unsupported file, macOS metadata and members that leave
        # the archive are entries too: 21 in all, though 16 are documents.
        names = ([f"d{i:02}.txt" for i in range(16)]
                 + ["folder/", "tool.exe", "__MACOSX/._d00.txt", "../out.txt", "/abs.txt"])
        self.assertEqual(len(names), MEMBERS + 1)
        status, body = self.post(upload("mixed.zip", zip_bytes([(n, "x") for n in names])))
        self.assert_refused(status, body, "ARCHIVE_TOO_MANY_MEMBERS",
                            too_many("mixed.zip", MEMBERS + 1, MEMBERS),
                            {"file": "mixed.zip", "members": [], "members_omitted": 0})

    def test_the_cap_is_per_zip(self):
        first = zip_bytes([(f"a{i:02}.txt", "a") for i in range(MEMBERS)])
        second = zip_bytes([(f"b{i:02}.txt", "b") for i in range(MEMBERS)])
        status, body = self.post(upload("first.zip", first), upload("second.zip", second))
        self.assertEqual(status, 202, body)
        self.assertEqual(len(self.queued_names()), 2 * MEMBERS)

    def test_too_many_entries_wins_over_damage(self):
        payload = b"y" * 100
        data = zip_bytes([("bad.txt", payload)] + [(f"m{i:02}.txt", "t") for i in range(MEMBERS)],
                         zipfile.ZIP_STORED)
        status, body = self.post(upload("both.zip", bad_crc(data, payload)))
        self.assertEqual((status, body["error"]["code"]), (400, "ARCHIVE_TOO_MANY_MEMBERS"))

    def test_the_default_cap_refuses_5001_entries(self):
        entries = [(f"m{i:04}.txt", "") for i in range(5001)]
        with patch.object(settings, "max_zip_members", 5000):
            status, body = self.post(upload("big.zip", zip_bytes(entries[:5000])))
            self.assertEqual(status, 202, body)
            status, body = self.post(upload("bigger.zip", zip_bytes(entries)))
        self.assert_refused(status, body, "ARCHIVE_TOO_MANY_MEMBERS",
                            too_many("bigger.zip", 5001, 5000),
                            {"file": "bigger.zip", "members": [], "members_omitted": 0})
        self.assertIn("holds 5,001 entries, more than 5,000,", body["error"]["message"])

    def test_the_count_wins_over_the_declared_total(self):
        # Reviewer test: the count is checked before the declared total, so a
        # ZIP over both is ARCHIVE_TOO_MANY_MEMBERS (SPECIFICATIONS.md §3.1.3).
        read, reading = self.reading()
        data = zip_bytes([("big.txt", bytes(3 * MIB))]
                         + [(f"m{i:02}.txt", "t") for i in range(MEMBERS)])
        with reading, patch.object(settings, "max_zip_expanded_bytes", 2 * MIB):
            status, body = self.post(upload("both.zip", data))
        self.assert_refused(status, body, "ARCHIVE_TOO_MANY_MEMBERS",
                            too_many("both.zip", MEMBERS + 1, MEMBERS),
                            {"file": "both.zip", "members": [], "members_omitted": 0})
        self.assertEqual(read, [])


class RatioCapTests(Harness):
    def test_a_member_over_the_ratio_is_refused_before_anything_is_read(self):
        read, reading = self.reading()
        data = zip_bytes([("a.txt", "first"), ("docs/zeros.txt", bytes(MIB))])
        self.assertLess(len(data), MIB // RATIO)   # about 1,000:1
        with reading:
            status, body = self.post(upload("bomb.zip", data))
        self.assert_ratio_refused(status, body, "bomb.zip", "docs/zeros.txt")
        self.assertEqual(read, [])

    def test_a_member_under_one_mib_is_exempt(self):
        status, body = self.post(upload("small.zip", zip_bytes([("zeros.txt", bytes(MIB - 1))])))
        self.assertEqual(status, 202, body)
        self.assertEqual(self.queued_names(), ["zeros.txt"])

    def test_a_stored_member_is_never_over(self):
        status, body = self.post(upload("stored.zip", zip_bytes([("zeros.txt", bytes(4 * MIB))],
                                                                zipfile.ZIP_STORED)))
        self.assertEqual(status, 202, body)

    def test_only_the_zip_layer_counts(self):
        # 8 MiB of gzip output from a few KB of gzip, stored in the zip: the
        # ZIP layer is 1:1, so it is accepted; its output still counts toward
        # the total, which it is far under.
        data = zip_bytes([("big.json.gz", gzip.compress(b" " * (8 * MIB)))], zipfile.ZIP_STORED)
        status, body = self.post(upload("gz.zip", data))
        self.assertEqual(status, 202, body)
        self.assertEqual(self.queued_names(), ["big.json.gz"])

    def test_the_bytes_read_are_checked_when_the_header_understates(self):
        # With the declared check out of the way, reading still refuses the
        # member, within one block of passing the ratio.
        most = []
        real = ingest_pipeline._CountedMember.read

        def read(member, size=-1):
            block = real(member, size)
            most.append(member.read_bytes)
            return block

        data = zip_bytes([("zeros.txt", bytes(64 * MIB))])
        with patch.object(ingest_pipeline, "_declared_ratio_member", lambda _members: None), \
                patch.object(ingest_pipeline._CountedMember, "read", read):
            status, body = self.post(upload("lies.zip", data))
        self.assert_ratio_refused(status, body, "lies.zip", "zeros.txt")
        # Stopped within one block of passing the ratio, long before its end.
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            compressed = z.infolist()[0].compress_size
        self.assertLessEqual(max(most), RATIO * compressed + ingest_pipeline._BLOCK)
        self.assertLess(max(most), 64 * MIB // 4)

    def test_the_total_cap_wins_over_the_ratio(self):
        # Over both by its declared sizes, like the verify fixture
        # zip_over_declared.zip: it stays ARCHIVE_TOO_LARGE.
        with patch.object(settings, "max_zip_expanded_bytes", 2 * MIB):
            status, body = self.post(upload("both.zip", zip_bytes([("z.txt", bytes(3 * MIB))])))
        self.assertEqual((status, body["error"]["code"]), (400, "ARCHIVE_TOO_LARGE"))

    def test_the_ratio_wins_over_an_earlier_unreadable_member(self):
        # A damaged stored member read first, then a Deflate member that passes
        # the ratio while it is read: refused for the ratio, like the total cap.
        payload = b"y" * 100
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr(zipfile.ZipInfo("bad.txt"), payload)
            z.writestr("zeros.txt", bytes(2 * MIB), zipfile.ZIP_DEFLATED)
        data = bad_crc(buf.getvalue(), payload)
        with patch.object(ingest_pipeline, "_declared_ratio_member", lambda _members: None):
            status, body = self.post(upload("mixed.zip", data))
        self.assert_ratio_refused(status, body, "mixed.zip", "zeros.txt")

    def test_while_reading_the_total_wins_over_the_ratio(self):
        # Reviewer test: with both declared checks out of the way, the first
        # block read passes both the total and the ratio; the total is checked
        # first, so it is ARCHIVE_TOO_LARGE (SPECIFICATIONS.md §3.1.3).
        data = zip_bytes([("zeros.txt", bytes(3 * MIB))])
        with patch.object(settings, "max_zip_expanded_bytes", MIB // 2), \
                patch.object(ingest_pipeline, "_declared_size", lambda _members: 0), \
                patch.object(ingest_pipeline, "_declared_ratio_member", lambda _members: None):
            status, body = self.post(upload("both.zip", data))
        self.assertEqual((status, body["error"]["code"]), (400, "ARCHIVE_TOO_LARGE"), body)


class EscapingMemberTests(Harness):
    def test_a_damaged_member_leaving_the_archive_refuses_the_zip(self):
        payload = b"hello world" * 10
        data = bad_crc(zip_bytes([("../bad.txt", payload), ("good.txt", "ok")], zipfile.ZIP_STORED),
                       payload)
        status, body = self.post(upload("trav.zip", data))
        self.assertEqual((status, body["error"]["code"]), (400, "ARCHIVE_UNREADABLE"), body)
        self.assertEqual([m["member"] for m in body["error"]["detail"]["members"]], ["../bad.txt"])
        self.assertIn("../bad.txt (damaged: Bad CRC-32", body["error"]["message"])
        self.assertEqual(set(ingest_pipeline._jobs), self.jobs_before)
        self.assertEqual(set(self.uploads.iterdir()), self.uploads_before)

    def test_members_leaving_the_archive_are_listed_and_not_extracted(self):
        status, body = self.post(upload("trav.zip", zip_bytes(
            [("good.txt", "ok"), ("../out.txt", "x"), ("a/../../up.json.gz", gzip.compress(b"{}"))])))
        self.assertEqual(status, 202, body)
        self.assertEqual(self.queued_names(), ["good.txt"])
        self.assertEqual(self.job()["skipped"], [f"trav.zip:../out.txt {LEAVES}",
                                                 f"trav.zip:a/../../up.json.gz {LEAVES}"])
        # Only the one queued file is staged: nothing was written for the others.
        staged = [p for p in self.uploads.rglob("*") if p.is_file()]
        self.assertEqual([p.name for p in staged], ["good.txt"])

    def test_their_bytes_count_toward_the_total_but_not_their_gzip_output(self):
        gz = gzip.compress(b" " * 100_000)
        data = zip_bytes([("../a.bin", b"a" * 5000), ("../b.json.gz", gz)], zipfile.ZIP_STORED)
        path = self.uploads / "t.zip"
        path.write_bytes(data)
        budget = ingest_pipeline._ExpansionBudget(10 * MIB)
        queue = ingest_pipeline._Queue(Path(tempfile.mkdtemp(dir=self.uploads)), [])
        ingest_pipeline._extract_zip(path, "t.zip", queue, budget)
        self.assertEqual(budget.used, 5000 + len(gz))
        self.assertEqual(queue.entries, {})
        # Over a total that only their own bytes pass: refused.
        with self.assertRaises(ingest_pipeline.UploadRejected) as refused:
            ingest_pipeline._extract_zip(path, "t.zip", queue, ingest_pipeline._ExpansionBudget(5000))
        self.assertEqual(refused.exception.code, "ARCHIVE_TOO_LARGE")

    def test_their_ratio_is_checked(self):
        status, body = self.post(upload("trav.zip", zip_bytes([("../zeros.txt", bytes(MIB)),
                                                               ("good.txt", "ok")])))
        self.assert_ratio_refused(status, body, "trav.zip", "../zeros.txt")

    def test_an_encrypted_member_leaving_the_archive_is_unsupported(self):
        # Reviewer test: validated like any member, so an encrypted one refuses
        # the zip as ARCHIVE_UNSUPPORTED (SPECIFICATIONS.md §3.1.3).
        data = bytearray(zip_bytes([("../locked.txt", "text"), ("good.txt", "ok")]))
        central = data.index(b"PK\x01\x02")
        for pos in (6, central + 8):   # the first member's flag bits, in both headers
            data[pos] |= 0x01
        status, body = self.post(upload("trav.zip", bytes(data)))
        self.assert_refused(
            status, body, "ARCHIVE_UNSUPPORTED", body["error"]["message"],
            {"file": "trav.zip", "members": [{"member": "../locked.txt",
                                              "reason": "password-protected"}],
             "members_omitted": 0})
        self.assertIn("../locked.txt (password-protected)", body["error"]["message"])

    def test_a_zip_of_only_such_members_has_no_supported_files(self):
        status, body = self.post(upload("only.zip", zip_bytes([("../out.txt", "x")])))
        self.assertEqual((status, body["error"]["code"]), (400, "NO_SUPPORTED_FILES"))
        self.assertIn(f"only.zip:../out.txt {LEAVES}", body["error"]["message"])


class DetailBoundTests(Harness):
    def test_detail_keeps_the_ten_members_the_message_names(self):
        payloads = [f"payload {i:02} ".encode() * 10 for i in range(15)]
        data = zip_bytes([(f"m{i:02}.txt", p) for i, p in enumerate(payloads)], zipfile.ZIP_STORED)
        for p in payloads:
            data = bad_crc(data, p)
        status, body = self.post(upload("many.zip", data))
        self.assertEqual((status, body["error"]["code"]), (400, "ARCHIVE_UNREADABLE"))
        detail = body["error"]["detail"]
        self.assertEqual([m["member"] for m in detail["members"]], [f"m{i:02}.txt" for i in range(10)])
        self.assertEqual(detail["members_omitted"], 5)
        self.assertIn("m09.txt (damaged", body["error"]["message"])
        self.assertIn("; and 5 more.", body["error"]["message"])
        self.assertNotIn("m10.txt", body["error"]["message"])

    def test_exactly_ten_members_omit_none(self):
        # Reviewer test: the boundary of the bound, ten named and none omitted.
        payloads = [f"payload {i:02} ".encode() * 10 for i in range(10)]
        data = zip_bytes([(f"m{i:02}.txt", p) for i, p in enumerate(payloads)], zipfile.ZIP_STORED)
        for p in payloads:
            data = bad_crc(data, p)
        status, body = self.post(upload("ten.zip", data))
        self.assertEqual((status, body["error"]["code"]), (400, "ARCHIVE_UNREADABLE"))
        detail = body["error"]["detail"]
        self.assertEqual([m["member"] for m in detail["members"]], [f"m{i:02}.txt" for i in range(10)])
        self.assertEqual(detail["members_omitted"], 0)
        self.assertNotIn("more.", body["error"]["message"])

    def test_a_refusal_with_few_members_omits_none(self):
        payload = b"y" * 100
        data = bad_crc(zip_bytes([("bad.txt", payload)], zipfile.ZIP_STORED), payload)
        status, body = self.post(upload("one.zip", data))
        self.assertEqual(body["error"]["detail"]["members_omitted"], 0)
        self.assertEqual(len(body["error"]["detail"]["members"]), 1)

    def test_the_bound_is_in_the_exception_itself(self):
        members = [{"member": f"m{i}", "reason": "r"} for i in range(11)]
        rejected = ingest_pipeline.UploadRejected("ARCHIVE_UNREADABLE", "m", "f.zip", members)
        self.assertEqual(rejected.detail, {"file": "f.zip", "members": members[:10],
                                           "members_omitted": 1})


if __name__ == "__main__":
    logging.basicConfig(level=logging.CRITICAL)
    unittest.main()
