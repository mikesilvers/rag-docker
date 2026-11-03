"""A file over the per-file limits is split into parts, not refused (issue #621).

Runs with the API's dependencies, inside the verify project's API container (plain
text is partitioned by Unstructured, which loads its language model):

    docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_split_parts.py

Weaviate is replaced by an in-memory collection that keeps the replacement helpers'
contract, `accept` included; nothing reaches a backend. The first two classes fail
on the code before #621, where these files fail with "exceeds the per-file limit".
"""
import csv
import io
import json
import os
import sys
import tempfile
import unittest
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from services import chunker, goldstandard, ingest_pipeline, packager, sources, tuning
from services import weaviate_client as wc

CAP = chunker.MAX_CHUNKS_PER_FILE
TEXT = chunker.MAX_TEXT_CHARACTERS


def records(count, size, start=0):
    """`count` JSON Lines records whose converted text is `size` characters."""
    return "".join(json.dumps({"t": f"{i:07d} " + "x" * (size - 11)}) + "\n"
                   for i in range(start, start + count))


class FakeCollection:
    """Chunks in a list. `accept`, as the real helpers take it, selects values the
    way _exact_source_objects does; without it, exactly the name."""

    def __init__(self, existing=()):
        self.chunks = [{"source_file": name, "content": c, "created_at": "old", "chunk_index": i}
                       for name, contents in existing for i, c in enumerate(contents)]
        self.fail_insert_at = None
        self.inserts = 0

    @staticmethod
    def keep(name, accept):
        return accept or (lambda value: value == name)

    def names(self):
        return sorted({c["source_file"] for c in self.chunks})

    def of(self, name):
        return [c for c in self.chunks if c["source_file"] == name]

    def of_contents(self, name):
        return [c["content"] for c in sorted(self.of(name), key=lambda c: c["chunk_index"])]

    def insert(self, collection, chunks):
        self.inserts += 1
        if self.fail_insert_at == self.inserts:
            raise RuntimeError("Weaviate rejected the batch")
        self.chunks.extend(dict(c) for c in chunks)

    def has(self, collection, name, accept=None):
        keep = self.keep(name, accept)
        return any(keep(c["source_file"]) for c in self.chunks)

    def delete_earlier(self, collection, name, created_at, expected, accept=None):
        keep, stamp = self.keep(name, accept), created_at.isoformat()
        mine = [c for c in self.chunks if keep(c["source_file"])]
        new = [c for c in mine if c["created_at"] == stamp]
        if len(new) != expected:
            raise wc.ReplaceError(f"found {len(new)} of its {expected} new chunks", 0)
        earlier = [c for c in mine if c["created_at"] != stamp]
        self.chunks = [c for c in self.chunks if not any(c is e for e in earlier)]
        return len(earlier)

    def delete_new(self, collection, name, created_at):
        keep, stamp = self.keep(name, wc._family(name)), created_at.isoformat()
        before = len(self.chunks)
        self.chunks = [c for c in self.chunks
                       if not (keep(c["source_file"]) and c["created_at"] == stamp)]
        return before - len(self.chunks)


class JobCase(unittest.TestCase):
    def run_job(self, files, strategy="fixed", chunk_size=1000, overlap=0, backend=None,
                minimum=0):
        backend = backend or FakeCollection()
        tmp = Path(tempfile.mkdtemp())  # the job removes it when it ends
        paths, names = [], {}
        for name, data in files:
            path = tmp / str(len(paths)) / Path(name).name
            path.parent.mkdir()
            path.write_bytes(data.encode() if isinstance(data, str) else data)
            paths.append(path)
            names[path] = name
        job_id = "s" + uuid.uuid4().hex[:7]
        ingest_pipeline._jobs[job_id] = {
            "job_id": job_id, "status": "queued", "files_total": len(paths),
            "files_completed": 0, "files_failed": 0, "chunks_stored": 0,
            "chunks_submitted": 0, "chunks_accepted": 0, "chunks_replaced": 0,
            "errors": [], "skipped": [], "split": []}
        self.addCleanup(ingest_pipeline._jobs.pop, job_id, None)
        self.retained, self.marks = [], []
        with patch.object(wc, "_insert_chunks_sync", side_effect=backend.insert), \
                patch.object(wc, "_source_has_chunks_sync", side_effect=backend.has), \
                patch.object(wc, "_delete_earlier_chunks_sync", side_effect=backend.delete_earlier), \
                patch.object(wc, "_delete_new_family_sync", side_effect=backend.delete_new, create=True), \
                patch.object(wc, "_collection_exists_sync", return_value=True), \
                patch.object(goldstandard, "mark_stale",
                             side_effect=lambda c, reason, **k: self.marks.append(reason)), \
                patch.object(ingest_pipeline.sources, "store",
                             side_effect=lambda c, name, data, *a, **k: self.retained.append(name)):
            ingest_pipeline._process_job_sync(job_id, paths, tmp, "Coll", strategy, chunk_size,
                                              overlap, 0.85, minimum, None, names)
        return ingest_pipeline._jobs[job_id], backend

    @staticmethod
    def joined(backend, name):
        """Every chunk of `name`'s parts, in part and chunk order, joined."""
        def order(c):
            return int(c["source_file"].rsplit(" ", 1)[-1][:-1]), c["chunk_index"]
        mine = [c for c in backend.chunks if c["source_file"].startswith(name + " (part ")]
        return "".join(c["content"] for c in sorted(mine, key=order))

    def assert_parts(self, backend, name):
        """The stored names are exactly `<name> (part 1)` to `(part N)`, N >= 2, each
        with chunk indexes 0..n-1 inside the cap; returns N."""
        stored = [n for n in backend.names() if n == name or n.startswith(name + " (part ")]
        count = len(stored)
        self.assertGreaterEqual(count, 2, stored)
        self.assertEqual(sorted(stored), sorted(f"{name} (part {n})" for n in range(1, count + 1)))
        for part in stored:
            indexes = [c["chunk_index"] for c in backend.of(part)]
            self.assertEqual(indexes, list(range(len(indexes))), part)
            self.assertLessEqual(len(indexes), CAP, part)
        return count


class ReviewerCaseTests(JobCase):
    """The cases from the issue: they fail before #621."""

    def test_13000_records_of_600_characters_import_fully_as_parts_context_aware(self):
        # Records are packed whole, so 13,000 records give 13,000 chunks: the
        # pre-check (len(text) // chunk_size) let it through and the cap refused it.
        job, backend = self.run_job([("big.jsonl", records(13_000, 600))], "context_aware", 1000)
        self.assertEqual((job["status"], job["files_completed"], job["errors"]),
                         ("completed", 1, []), job)
        parts = self.assert_parts(backend, "big.jsonl")
        bodies = sorted(c["content"][3:10] for c in backend.chunks)
        self.assertEqual(bodies, [f"{i:07d}" for i in range(13_000)])  # every record once
        self.assertEqual(job["chunks_stored"], 13_000)
        self.assertEqual(job["split"], [f"big.jsonl: split into {parts} parts, stored as "
                                        f"'big.jsonl (part 1)' to 'big.jsonl (part {parts})'"])
        self.assertEqual(self.retained, ["big.jsonl"])  # the original, once, under its name

    def test_a_jsonl_file_over_10_million_characters_imports_as_parts(self):
        data = records(10_500, 1000)
        job, backend = self.run_job([("text.jsonl", data)], "fixed", 6000)
        self.assertEqual((job["status"], job["errors"]), ("completed", []), job)
        self.assert_parts(backend, "text.jsonl")
        ids = __import__("re").findall(r"\d{7}", self.joined(backend, "text.jsonl"))
        self.assertEqual(ids, [f"{i:07d}" for i in range(10_500)])

    def test_a_text_file_over_10_million_characters_imports_as_parts(self):
        paragraph = "word " * 199 + "end."
        data = "\n\n".join([paragraph] * 10_500)  # 10,519,499 characters
        self.assertGreater(len(data), TEXT)
        job, backend = self.run_job([("notes/long.txt", data)], "fixed", 6000)
        self.assertEqual((job["status"], job["errors"]), ("completed", []), job)
        self.assert_parts(backend, "notes/long.txt")
        self.assertEqual(self.joined(backend, "notes/long.txt").count("end."), 10_500)


class WithinLimitsTests(JobCase):
    def test_a_file_within_the_limits_is_stored_as_before(self):
        for name, data, strategy in [("a.jsonl", records(50, 300), "context_aware"),
                                     ("b.txt", "One paragraph.\n\nAnother one here.", "language"),
                                     ("c.csv", "h1,h2\n1,2\n3,4\n", "fixed")]:
            with self.subTest(name=name):
                job, backend = self.run_job([(name, data)], strategy, 200)
                path = Path(tempfile.mkdtemp()) / name
                path.write_text(data)
                text, elements = ingest_pipeline._parse_file(path)
                expected = chunker.chunk(text, strategy, chunk_size=200, chunk_overlap_size=0,
                                         min_chunk_size=0,
                                         elements=elements if strategy == "context_aware" else None)
                self.assertEqual(backend.names(), [name])
                self.assertEqual([c["content"] for c in backend.chunks], expected)
                self.assertEqual(job["split"], [])


class ReplacementTests(JobCase):
    NEIGHBOURS = [("a/data.jsonl", ["n1"]), ("Data.jsonl", ["n2"]), ("data.jsonl.bak (part 1)", ["n3"])]

    def test_a_smaller_upload_replaces_every_earlier_part(self):
        backend = FakeCollection([("data.jsonl (part 1)", ["o1", "o2"]), ("data.jsonl (part 2)", ["o3"]),
                                  ("data.jsonl (part 3)", ["o4"])] + self.NEIGHBOURS)
        job, backend = self.run_job([("data.jsonl", records(3, 100))], backend=backend)
        self.assertEqual((job["status"], job["chunks_replaced"]), ("completed", 4), job)
        self.assertEqual(backend.names(), sorted(["data.jsonl", "a/data.jsonl", "Data.jsonl",
                                                  "data.jsonl.bak (part 1)"]))
        self.assertEqual(len(self.marks), 1)
        self.assertIn("data.jsonl", self.marks[0])

    def test_a_newly_split_file_replaces_its_unsplit_chunks(self):
        backend = FakeCollection([("data.jsonl", ["o1", "o2"])] + self.NEIGHBOURS)
        job, backend = self.run_job([("data.jsonl", records(13_000, 600))], "context_aware",
                                    backend=backend)
        self.assertEqual((job["status"], job["chunks_replaced"]), ("completed", 2), job)
        self.assertNotIn("data.jsonl", backend.names())
        self.assert_parts(backend, "data.jsonl")
        for name, contents in self.NEIGHBOURS:
            self.assertEqual([c["content"] for c in backend.of(name)], contents)

    def test_a_split_file_uploaded_again_as_fewer_parts_leaves_no_stale_part(self):
        backend = FakeCollection([(f"data.jsonl (part {n})", ["o"]) for n in range(1, 6)])
        job, backend = self.run_job([("data.jsonl", records(13_000, 600))], "context_aware",
                                    backend=backend)
        self.assertEqual(job["status"], "completed", job)
        self.assertEqual(self.assert_parts(backend, "data.jsonl"), 2)
        self.assertFalse([c for c in backend.chunks if c["created_at"] == "old"])


class AllOrNothingTests(JobCase):
    def test_a_bad_line_after_the_first_part_stores_nothing_and_keeps_the_earlier_version(self):
        # 17,000 records are over the text limit, so parts are stored before the bad
        # line is read (PR #630: a file within the limits is no longer split, so
        # 10,500 records now meet the bad line before anything is stored).
        data = records(17_000, 600) + "{not json\n"
        backend = FakeCollection([("data.jsonl", ["earlier"])])
        job, backend = self.run_job([("data.jsonl", data), ("ok.jsonl", records(2, 50))],
                                    "context_aware", backend=backend)
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"]),
                         ("partial", 1, 1), job)
        self.assertEqual(len(job["errors"]), 1)
        self.assertRegex(job["errors"][0], r"^data\.jsonl: part \d+: line 17001 is not valid JSON: "
                                           r".*; nothing from it was stored$")
        self.assertEqual([c["content"] for c in backend.of("data.jsonl")], ["earlier"])
        self.assertEqual(backend.names(), ["data.jsonl", "ok.jsonl"])
        # The earlier parts' chunks were counted, then removed again.
        self.assertEqual(job["chunks_stored"], len(backend.of("ok.jsonl")))
        self.assertEqual(job["split"], [])
        self.assertEqual(self.retained, ["ok.jsonl"])

    def test_a_failed_insert_of_a_later_part_stores_nothing(self):
        backend = FakeCollection()
        backend.fail_insert_at = 2
        job, backend = self.run_job([("data.jsonl", records(13_000, 600))], "context_aware",
                                    backend=backend)
        self.assertEqual(job["status"], "failed", job)
        self.assertEqual(job["errors"], ["data.jsonl: part 2: Weaviate rejected the batch; "
                                         "nothing from it was stored"])
        self.assertEqual(backend.chunks, [])
        self.assertEqual(job["chunks_stored"], 0)

    def test_a_removal_that_fails_says_how_many_new_chunks_remain(self):
        backend = FakeCollection()
        backend.fail_insert_at = 2

        def broken(collection, name, created_at):
            raise wc.ReplaceError("Weaviate failed to delete 10000 new chunk(s)", 0)

        backend.delete_new = broken
        with self.assertLogs("services.ingest_pipeline", "ERROR"):
            job, _ = self.run_job([("data.jsonl", records(13_000, 600))], "context_aware",
                                  backend=backend)
        # PR #630: 13,000 records fit by text, so they are chunked whole first and
        # refused, then packed into parts of 10,000 and 3,000 chunks.
        self.assertEqual(job["errors"], [
            "data.jsonl: part 2: Weaviate rejected the batch; 10000 new chunks could not be "
            "removed (Weaviate failed to delete 10000 new chunk(s)); upload it again to retry"])

    def test_the_semantic_sentence_limit_fails_a_part_and_is_not_split_further(self):
        from services import splitter
        calls = []

        def chunk(text, strategy, **kwargs):
            calls.append(len(text))
            if len(calls) == 2:
                raise ValueError("semantic chunking exceeds the per-file limit: 100001 sentences")
            return [text[:10]]

        path = Path(tempfile.mkdtemp()) / "s.jsonl"
        path.write_text(records(120, 100_000))  # 12,000,000 characters: two parts
        settings = splitter.Settings("semantic", 1000, 0, 0.85, 0)
        produced = []
        with self.assertRaises(splitter.PartError) as caught:
            for kind, chunks in splitter.split_file(path, settings, chunk=chunk):
                produced.append(kind)
        self.assertEqual((produced, caught.exception.number), (["part"], 2))
        self.assertIn("100001 sentences", str(caught.exception))
        self.assertEqual(len(calls), 2)


class RecordTests(JobCase):
    def test_a_json_array_over_10_million_characters_imports_as_parts(self):
        doc = json.dumps([{"t": "x" * 1000} for _ in range(10_500)])
        job, backend = self.run_job([("array.json", doc)], "fixed", 6000)
        self.assertEqual((job["status"], job["errors"]), ("completed", []), job)
        self.assert_parts(backend, "array.json")

    def test_a_json_record_over_the_chunk_cap_fails_naming_it(self):
        doc = json.dumps([{"t": "small"}, {"t": "word " * 120_000}])
        job, backend = self.run_job([("one.json", doc)], "fixed", 50)
        self.assertEqual(job["errors"], [
            "one.json: part 2: record 2 gives more than 10,000 chunks with these chunk settings, "
            "the most one part can hold; a record is never split, so use a larger chunk size; "
            "nothing from it was stored"])
        self.assertEqual(backend.chunks, [])


class UnitTests(unittest.TestCase):
    def setUp(self):
        from services import splitter
        self.splitter = splitter
        self.dir = Path(tempfile.mkdtemp())

    def split(self, name, data, strategy="fixed", size=1000, overlap=0, chunk=None):
        path = self.dir / name
        path.write_bytes(data if isinstance(data, bytes) else data.encode())
        settings = self.splitter.Settings(strategy, size, overlap, 0.85, 0)
        return list(self.splitter.split_file(path, settings, chunk=chunk))

    def test_cuts_prefer_a_line_break_then_whitespace_then_a_hard_cut(self):
        cut = self.splitter._cut_text
        self.assertEqual(cut("aaaa bb\ncc dd", 9), ["aaaa bb", "cc dd"])
        self.assertEqual(cut("aaaa bbbb cc", 9), ["aaaa bbbb", "cc"])
        self.assertEqual(cut("aaaa bbbbb cc", 9), ["aaaa", "bbbbb cc"])
        self.assertEqual(cut("x" * 20, 8), ["x" * 8, "x" * 8, "x" * 4])

    def test_a_paragraph_with_no_break_is_cut_and_kept_whole(self):
        # One 600,000-character run, no whitespace: over one part (10,000 chunks of 50).
        chunks = self.split("run.txt", "y" * 600_000, "fixed", 50)
        self.assertGreaterEqual(len(chunks), 2)
        self.assertEqual("".join(c for _, part in chunks for c in part), "y" * 600_000)

    def test_csv_parts_each_repeat_the_header(self):
        rows = [["name", "city"]] + [[f"n{i:06d}", "x" * 80] for i in range(6_000)]
        out = io.StringIO()
        csv.writer(out, lineterminator="\n").writerows(rows)
        parts = self.split("people.csv", out.getvalue(), "fixed", 50)
        self.assertGreaterEqual(len(parts), 2)
        for kind, chunks in parts:
            self.assertEqual(kind, "part")
            self.assertTrue(chunks[0].startswith("name city"), chunks[0][:40])
        seen = "".join("".join(chunks) for _, chunks in parts)
        for i in (0, 2_999, 5_999):
            self.assertIn(f"n{i:06d}", seen)

    def test_markdown_is_not_cut_inside_a_fenced_block(self):
        source = self.splitter
        stream = io.StringIO("intro\n\n```\ncode one\n\ncode two\n```\n\nafter\n")
        units = [u.text for u in source._text_units(stream, True, 1000)]
        self.assertEqual(units, ["intro", "```\ncode one\n\ncode two\n```", "after"])
        stream = io.StringIO("intro\n\n```\ncode one\n\ncode two\n```\n")
        self.assertEqual(len(list(source._text_units(stream, False, 1000))), 3)

    def test_a_streamed_latin_1_file_is_read_in_its_encoding(self):
        data = ("Café crème brûlée. " * 40 + "\n\n") * 900
        path = self.dir / "menu.txt"
        path.write_bytes(data.encode("latin-1"))
        self.assertNotEqual(self.splitter.text_encoding(path), "utf-8-sig")
        parts = self.split("menu.txt", data.encode("latin-1"), "fixed", 50)
        self.assertGreaterEqual(len(parts), 2)
        self.assertIn("Café", parts[0][1][0])

    def test_a_file_no_encoding_reads_fails_before_anything_is_stored(self):
        path = self.dir / "odd.txt"
        path.write_bytes(b"\xff\xfe\xfa" * 10)
        with patch("unstructured.file_utils.encoding.detect_file_encoding",
                   return_value=("ascii", "")):
            with self.assertRaisesRegex(ValueError, "^is not in an encoding that can be read in parts$"):
                self.splitter.text_encoding(path)

    def test_overlap_parts_stay_inside_both_window_limits(self):
        for size, overlap in ((1000, 999), (6000, 5000), (1000, 200)):
            with self.subTest(size=size, overlap=overlap):
                settings = self.splitter.Settings("overlap", size, overlap, 0.85, 0)
                limit, _ = self.splitter.part_limits(settings)
                chunker.chunk_overlap("z" * limit, size, overlap, 0)
                with self.assertRaises(chunker.ChunkLimitError):
                    chunker.chunk_overlap("z" * (limit + 1), size, overlap, 0)

    def test_a_part_still_over_after_chunking_is_halved(self):
        def chunk(text, strategy, **kwargs):
            if text.count("\n\n") >= 3:
                raise chunker.ChunkLimitError("too many", "chunks")
            return [text]

        parts = self.split("eight.jsonl", records(8, 50), "language", 1000, chunk=chunk)
        self.assertEqual([k for k, _ in parts], ["part"] * 4)
        self.assertEqual(sum(c[0].count("t: ") for _, c in parts), 8)

    def test_the_in_scope_limits_raise_chunk_limit_error_and_the_sentence_limit_does_not(self):
        with self.assertRaises(chunker.ChunkLimitError):
            chunker.chunk("q" * (TEXT + 1), "fixed")
        with self.assertRaises(chunker.ChunkLimitError):
            chunker.chunk("q" * 60 * CAP, "fixed", chunk_size=50)
        with self.assertRaises(chunker.ChunkLimitError):
            chunker.chunk_overlap("q" * 20_000, 1000, 999, 0)
        with patch.object(chunker, "MAX_SEMANTIC_SENTENCES", 2), \
                self.assertRaises(ValueError) as caught:
            chunker.chunk("a. b. c. d.", "semantic")
        self.assertNotIsInstance(caught.exception, chunker.ChunkLimitError)


class PartsMapToTheirOriginalTests(unittest.TestCase):
    def test_part_names(self):
        from services import parts
        self.assertEqual(parts.part_name("a.txt", 3), "a.txt (part 3)")
        self.assertEqual(parts.base_of("a.txt (part 12)"), "a.txt")
        self.assertEqual(parts.base_of("a.txt (part 2) (part 3)"), "a.txt (part 2)")
        for name in ("a.txt", "a.txt (part 0)", "a.txt (part 01)", "a.txt(part 1)", "a.txt (part x)"):
            self.assertIsNone(parts.base_of(name), name)
        self.assertTrue(parts.in_family("a.txt (part 4)", "a.txt"))
        self.assertFalse(parts.in_family("b/a.txt (part 4)", "a.txt"))
        self.assertEqual(parts.resolve("a.txt (part 4)", {"a.txt": 1}), "a.txt")
        self.assertEqual(parts.resolve("a.txt (part 4)", {"a.txt (part 4)": 1, "a.txt": 1}),
                         "a.txt (part 4)")
        self.assertIsNone(parts.resolve("a.txt (part 4)", {}))

    def objects(self, names):
        return [SimpleNamespace(properties={"source_file": n, "chunk_index": i}, uuid=uuid.uuid4(),
                                vector=[0.0])
                for n, count in names for i in range(count)]

    def test_rechunk_coverage_counts_parts_for_their_original(self):
        documents = {"d" * 64: {"filenames": ["big.jsonl"]}, "e" * 64: {"filenames": ["x.txt"]}}
        objects = self.objects([("big.jsonl (part 1)", 2), ("big.jsonl (part 2)", 1), ("x.txt", 1),
                                ("orphan.txt (part 1)", 1)])
        with patch.object(tuning.wc, "get_client") as client:
            client.return_value.collections.get.return_value.iterator.return_value = objects
            uncovered, stored = tuning._source_coverage("Coll", documents)
        self.assertEqual(uncovered, ["orphan.txt (part 1)"])
        self.assertTrue({"big.jsonl", "x.txt"} <= stored)

    def test_export_links_a_part_to_its_original(self):
        documents = {"d" * 64: {"filenames": ["big.jsonl"]}}
        objects = self.objects([("big.jsonl (part 2)", 1), ("other.txt", 1)])
        with patch.object(packager.wc, "get_client") as client:
            client.return_value.collections.get.return_value.iterator.return_value = objects
            linked = [r["source_sha256"] for r in packager.read_chunks("Coll", documents)]
        self.assertEqual(linked, ["d" * 64, None])

    def test_rechunk_rebuilds_a_split_original_as_parts(self):
        data = records(13_000, 600).encode()
        work = Path(tempfile.mkdtemp())
        digest = __import__("hashlib").sha256(data).hexdigest()
        (work / digest).write_bytes(data)
        documents = {digest: {"filenames": ["big.jsonl"]}}
        with patch.object(tuning.settings, "upload_dir", str(work)), \
                patch.object(tuning.sources, "load_index", return_value={"documents": documents}), \
                patch.object(tuning.sources, "collection_dir", return_value=work), \
                patch.object(tuning.wc, "get_client") as client:
            client.return_value.collections.get.return_value.iterator.return_value = \
                self.objects([("big.jsonl", 3)])
            out = tuning._chunks_from_sources("Coll", "context_aware", 1000, 0, 0.85, 0)
        names = sorted({c["source_file"] for c in out})
        self.assertEqual(names, ["big.jsonl (part 1)", "big.jsonl (part 2)"])
        self.assertEqual(len(out), 13_000)


class ReviewerAddedTests(JobCase):
    """Added by the testing review of PR #630: confirmed interpretations and
    branches the PR's own tests leave out."""

    def test_13000_records_import_fully_as_parts_language(self):
        # The issue names `language` as well as `context_aware`: records of 600
        # characters with chunk_size 1000 give one chunk each.
        job, backend = self.run_job([("lang.jsonl", records(13_000, 600))], "language", 1000)
        self.assertEqual((job["status"], job["files_completed"], job["errors"]),
                         ("completed", 1, []), job)
        self.assert_parts(backend, "lang.jsonl")
        ids = __import__("re").findall(r"\d{7}", self.joined(backend, "lang.jsonl"))
        self.assertEqual(ids, [f"{i:07d}" for i in range(13_000)])

    def test_a_small_text_file_over_the_chunk_cap_imports_as_parts(self):
        # A .txt well under 10,000,000 characters whose paragraphs pack one to a
        # chunk under context_aware: 13,000 chunks, so the whole-file attempt
        # fails on the cap and the file is halved from its paragraphs.
        data = "\n\n".join(f"p{i:07d} " + "y" * 590 for i in range(13_000))
        self.assertLess(len(data), TEXT)
        job, backend = self.run_job([("paras.txt", data)], "context_aware", 1000)
        self.assertEqual((job["status"], job["files_completed"], job["errors"]),
                         ("completed", 1, []), job)
        self.assert_parts(backend, "paras.txt")
        ids = __import__("re").findall(r"p(\d{7})", self.joined(backend, "paras.txt"))
        self.assertEqual(ids, [f"{i:07d}" for i in range(13_000)])

    def test_a_rollback_keeps_the_submitted_and_accepted_counts(self):
        # Confirmed interpretation 4: chunks_stored goes back down by what was
        # removed; the submitted and accepted counts keep what was sent.
        # Adjusted for PR #630 (17,000 records, over the text limit, so parts are
        # stored first): the first part is packed to the chunk cap, 10,000
        # records of one chunk each, all sent before the bad line.
        data = records(17_000, 600) + "{not json\n"
        job, backend = self.run_job([("data.jsonl", data), ("ok.jsonl", records(2, 50))],
                                    "context_aware")
        ok = len(backend.of("ok.jsonl"))
        sent = CAP
        self.assertEqual(job["chunks_stored"], ok, job)
        self.assertEqual(job["chunks_submitted"], sent + ok, job)
        self.assertEqual(job["chunks_accepted"], sent + ok, job)

    def test_a_jsonl_record_over_10_million_characters_fails_naming_it(self):
        # Decision 3: a single record over 10,000,000 characters still fails with
        # a clear error; nothing of the file is stored.
        big = json.dumps({"k" * 200: {f"v{i:06d}": 1 for i in range(50_000)}})
        data = json.dumps({"t": "small"}) + "\n" + big + "\n"
        backend = FakeCollection([("rec.jsonl", ["earlier"])])
        job, backend = self.run_job([("rec.jsonl", data)], "fixed", 6000, backend=backend)
        self.assertEqual(job["status"], "failed", job)
        self.assertEqual(job["errors"], [
            "rec.jsonl: line 2 converts to more than 10,000,000 characters of text, the most "
            "one record can hold; a record is never split"])
        self.assertEqual([c["content"] for c in backend.of("rec.jsonl")], ["earlier"])

    def test_a_csv_row_over_a_part_on_its_own_is_cut_into_one_cell_rows(self):
        # Confirmed interpretation 1: line breaks, then whitespace, then a hard
        # cut; each piece a one-cell row, so the part stays valid CSV.
        from services import splitter
        cells = ["id7", "word " * 30, "z" * 25]
        pieces = splitter.cut_unit(splitter.Unit(splitter._csv_row(cells, csv.excel), cells=cells),
                                   40, csv.excel)
        self.assertGreaterEqual(len(pieces), 2)
        for piece in pieces:
            rows = list(csv.reader(io.StringIO(piece.text)))
            self.assertEqual(len(rows), 1, piece.text)
            self.assertEqual(len(rows[0]), 1, piece.text)
            self.assertLessEqual(len(rows[0][0]), 40)
        self.assertEqual(" ".join(p.cells[0] for p in pieces).split(), " ".join(cells).split())

    def test_a_csv_with_one_row_over_a_part_keeps_the_header_and_every_word(self):
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["name", "notes"])
        writer.writerow(["small", "first row"])
        writer.writerow(["huge", " ".join(f"w{i:06d}" for i in range(80_000))])  # 640,000 characters
        writer.writerow(["last", "final row"])
        path = Path(tempfile.mkdtemp()) / "rows.csv"
        path.write_text(out.getvalue())
        from services import splitter
        parts = list(splitter.split_file(path, splitter.Settings("fixed", 50, 0, 0.85, 0)))
        self.assertGreaterEqual(len(parts), 2)
        for kind, chunks in parts:
            self.assertEqual(kind, "part")
            self.assertTrue(chunks[0].startswith("name notes"), chunks[0][:40])
        seen = "".join("".join(chunks) for _, chunks in parts)
        words = __import__("re").findall(r"w\d{6}", seen)
        self.assertEqual(sorted(set(words)), [f"w{i:06d}" for i in range(80_000)])
        self.assertIn("first row", seen)
        self.assertIn("final row", seen)

    def test_a_pdf_over_the_text_limit_is_split_between_elements(self):
        # `.pdf` and `.docx` are parsed whole and split between elements.
        from services import splitter
        from unstructured.documents.elements import NarrativeText
        elements = [NarrativeText(text=f"e{i:05d} " + "q" * 3993) for i in range(2_600)]  # 10,400,000

        def parse(path):
            return "\n".join(str(e) for e in elements), elements

        path = Path(tempfile.mkdtemp()) / "report.pdf"
        path.write_bytes(b"%PDF-1.4 stand-in")
        parts = list(splitter.split_file(path, splitter.Settings("fixed", 6000, 0, 0.85, 0),
                                         parse=parse))
        self.assertGreaterEqual(len(parts), 2)
        self.assertTrue(all(kind == "part" for kind, _ in parts))
        seen = "".join("".join(chunks) for _, chunks in parts)
        ids = __import__("re").findall(r"e(\d{5})", seen)
        self.assertEqual(ids, [f"{i:05d}" for i in range(2_600)])

    def test_a_text_file_is_read_part_by_part(self):
        # Decision 2: text is read in parts (streamed), so memory stays bounded
        # by a part. With the limits made small, a 16 MB file must never be held
        # whole. Unstructured's partitioner is replaced by a plain one: its own
        # tokenizer caches grow with the text it has seen, whatever the splitter does.
        import tracemalloc
        from services import splitter
        from unstructured.documents.elements import Text
        import unstructured.partition.text as partition

        def write(path, count):
            with path.open("w") as fh:
                for i in range(count):
                    fh.write(f"para {i:06d} " + "r" * 480 + "\n\n")
            return path

        count = [0]

        def chunk(text, strategy, **kwargs):
            count[0] += 1
            return [text[:12]]

        settings = splitter.Settings("fixed", 6000, 0, 0.85, 0)
        work = Path(tempfile.mkdtemp())
        path = write(work / "stream.txt", 32_000)
        self.assertGreater(path.stat().st_size, 15_000_000)
        with patch.object(chunker, "MAX_TEXT_CHARACTERS", 50_000), \
                patch.object(partition, "partition_text", lambda text: [Text(text=text)]):
            list(splitter.split_file(write(work / "warm.txt", 500), settings, chunk=chunk))
            count[0] = 0
            tracemalloc.start()
            try:
                for _ in splitter.split_file(path, settings, chunk=chunk):
                    pass
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertLess(peak, 4 * 1024 * 1024, f"peak {peak} bytes")
        self.assertGreaterEqual(count[0], 300)

class CsvHeaderTests(JobCase):
    """A CSV's header is repeated at the start of every part (PR #630 security
    review): parts are sized with it, and a header over half a part fails the
    file before it is split (maintainer's decision)."""

    # fixed, chunk_size 50: a part holds at most 10,000 x 50 = 500,000 characters.
    SIZE = 50
    LIMIT = CAP * 50

    @staticmethod
    def csv_text(header_cell, rows, row_cell=1000):
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["h" * header_cell, "g" * header_cell])
        writer.writerows([f"r{i:06d}" + "x" * row_cell, "y"] for i in range(rows))
        return out.getvalue()

    def split(self, data, chunk):
        from services import splitter
        path = Path(tempfile.mkdtemp()) / "table.csv"
        path.write_text(data)
        settings = splitter.Settings("fixed", self.SIZE, 0, 0.85, 0)
        return list(splitter.split_file(path, settings, chunk=chunk))

    def test_a_large_header_makes_every_part_smaller_so_none_goes_over(self):
        # A 200,000-character header and 1,000,000 characters of rows. The file is
        # first chunked whole and refused (PR #630: split only on a refusal); after
        # that, every part handed to the chunker, its header included, is inside one
        # part's limit: the header is counted when parts are sized.
        seen = []

        def chunk(text, strategy, **kwargs):
            seen.append(len(text))
            return chunker.chunk(text, strategy, **kwargs)

        parts = self.split(self.csv_text(100_000, 1000), chunk)
        self.assertGreaterEqual(len(parts), 2)
        self.assertEqual({k for k, _ in parts}, {"part"})
        self.assertGreater(seen[0], self.LIMIT, seen)  # the whole file, refused
        self.assertLessEqual(max(seen[1:]), self.LIMIT, seen)
        for _, chunks in parts:
            self.assertTrue(chunks[0].startswith("h" * self.SIZE), chunks[0][:60])
        rows = __import__("re").findall(r"r(\d{6})", "".join("".join(c) for _, c in parts))
        self.assertEqual(rows, [f"{i:06d}" for i in range(1000)])

    def test_a_header_over_half_a_part_fails_the_file_before_it_is_split(self):
        from services import splitter
        data = self.csv_text(130_000, 1000)  # a 260,002-character header row
        header = 2 * 130_000 + 2
        message = (f"its header row is {header:,} characters, more than half of what one part "
                   f"can hold with these chunk settings ({self.LIMIT:,} characters); the header is "
                   f"repeated in every part, so shorten the header or split the file")
        calls = []

        def refuse(text, strategy, **kw):
            calls.append(len(text))
            raise chunker.ChunkLimitError("over", "chunks")

        with self.assertRaises(ValueError) as caught:
            self.split(data, refuse)
        self.assertNotIsInstance(caught.exception, splitter.PartError)
        self.assertEqual(str(caught.exception), message)
        # Only the whole file was chunked, and refused (PR #630); no part was
        # chunked, so nothing was stored.
        self.assertEqual(len(calls), 1, calls)
        job, backend = self.run_job([("data/table.csv", data)], "fixed", self.SIZE)
        self.assertEqual((job["status"], job["errors"], job["split"]),
                         ("failed", [f"data/table.csv: {message}"], []))
        self.assertEqual((backend.chunks, job["chunks_stored"]), ([], 0))

    def test_a_csv_in_one_part_keeps_any_header(self):
        # 260,002 characters of header and three short rows: one part, not split,
        # so the header rule doesn't apply.
        data = self.csv_text(130_000, 3, row_cell=10)
        job, backend = self.run_job([("table.csv", data)], "fixed", self.SIZE)
        self.assertEqual((job["status"], job["errors"], job["split"]), ("completed", [], []), job)
        self.assertEqual(backend.names(), ["table.csv"])

class ReviewerCsvHeaderTests(JobCase):
    """PR #630 testing review (a764852): the header rule's boundary, a CSV row over
    what a part has left beside a large header, the rule on the path where a CSV
    fits by size but its chunks go over, and the limit of another strategy."""

    SIZE = CsvHeaderTests.SIZE
    LIMIT = CsvHeaderTests.LIMIT
    split = CsvHeaderTests.split

    @staticmethod
    def header_of(length):
        """Two header cells giving a header row of exactly `length` characters."""
        first = (length - 2) // 2
        return ["h" * first, "g" * (length - 2 - first)]

    def csv_with(self, header, rows):
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(header)
        writer.writerows(rows)
        return out.getvalue()

    def seen_chunk(self):
        seen = []

        def chunk(text, strategy, **kwargs):
            seen.append(len(text))
            return chunker.chunk(text, strategy, **kwargs)
        return seen, chunk

    def test_a_header_of_exactly_half_a_part_is_split_and_one_more_character_fails(self):
        rows = [[f"r{i:06d}" + "x" * 1000, "y"] for i in range(1000)]
        seen, chunk = self.seen_chunk()
        parts = self.split(self.csv_with(self.header_of(self.LIMIT // 2), rows), chunk)
        self.assertGreaterEqual(len(parts), 2)
        self.assertEqual({k for k, _ in parts}, {"part"})
        # After the whole file, chunked first and refused (PR #630), every part
        # handed to the chunker is inside one part's limit.
        self.assertLessEqual(max(seen[1:]), self.LIMIT, seen)
        found = __import__("re").findall(r"r(\d{6})", "".join("".join(c) for _, c in parts))
        self.assertEqual(found, [f"{i:06d}" for i in range(1000)])
        over = self.LIMIT // 2 + 1
        with self.assertRaises(ValueError) as caught:
            self.split(self.csv_with(self.header_of(over), rows), chunk)
        self.assertIn(f"its header row is {over:,} characters", str(caught.exception))
        self.assertIn(f"({self.LIMIT:,} characters)", str(caught.exception))

    def test_a_row_over_what_is_left_beside_a_large_header_is_cut_and_kept(self):
        # A 200,000-character header leaves 300,000 for rows; one row of 450,000
        # characters (words) is cut into one-cell rows, every word kept, in order,
        # and every part stays inside the cap.
        words = [f"w{i:06d}" for i in range(450_000 // 8)]
        data = self.csv_with(self.header_of(200_000), [["a", "b"], [" ".join(words)], ["c", "d"]])
        job, backend = self.run_job([("wide.csv", data)], "fixed", self.SIZE)
        self.assertEqual((job["status"], job["errors"]), ("completed", []), job["errors"])
        self.assert_parts(backend, "wide.csv")
        found = __import__("re").findall(r"w\d{6}", self.joined(backend, "wide.csv"))
        self.assertEqual(found, words)

    def test_the_header_rule_applies_when_a_csv_fits_by_size_but_its_chunks_go_over(self):
        # 400,000 bytes, inside one part's text, but chunking it whole goes over the
        # cap (a fake chunker): it has to be split, so a header over half a part
        # fails the file, and one under half is split with the header in each part.
        from services import splitter

        def chunk(text, strategy, **kwargs):
            if len(text) > 150_000:
                raise chunker.ChunkLimitError("over the cap", "chunks")
            return [text]
        rows = [[f"r{i:06d}" + "x" * 100, "y"] for i in range(1000)]
        big = self.csv_with(self.header_of(260_000), rows)
        self.assertLess(len(big), self.LIMIT)
        with self.assertRaises(ValueError) as caught:
            self.split(big, chunk)
        self.assertNotIsInstance(caught.exception, splitter.PartError)
        self.assertIn("its header row is 260,000 characters", str(caught.exception))
        parts = self.split(self.csv_with(self.header_of(100_000), rows), chunk)
        self.assertGreaterEqual(len(parts), 2)
        for _, chunks in parts:
            self.assertTrue(chunks[0].startswith("h" * 50), chunks[0][:60])

    def test_the_limit_is_that_of_the_active_strategy(self):
        # language at chunk size 50 and overlap 25 holds half of what fixed does
        # in a part: a header fine under fixed fails a split language file, and
        # the message names language's limit.
        from services import splitter
        settings = splitter.Settings("language", self.SIZE, 25, 0.85, 0)
        limit, _ = splitter.part_limits(settings)
        self.assertEqual(limit, self.LIMIT // 2)
        header = self.header_of(limit // 2 + 1)
        rows = [[f"r{i:06d}" + "x" * 1000, "y"] for i in range(600)]
        path = Path(tempfile.mkdtemp()) / "table.csv"
        path.write_text(self.csv_with(header, rows))
        with self.assertRaises(ValueError) as caught:
            list(splitter.split_file(path, settings))
        self.assertIn(f"its header row is {limit // 2 + 1:,} characters", str(caught.exception))
        self.assertIn(f"({limit:,} characters)", str(caught.exception))
        self.assertLessEqual(2 * (limit // 2 + 1), self.LIMIT)  # fine under fixed


class ReviewerRechunkRefusalTests(unittest.TestCase):
    """PR #630 testing review: the re-chunk refusal (#328) counts a part for its
    original, so a collection holding only parts has something to rebuild."""

    def test_only_parts_stored_still_counts_as_stored(self):
        documents = {"d" * 64: {"filenames": ["big.jsonl"]}}
        objects = [SimpleNamespace(properties={"source_file": "big.jsonl (part 1)"}),
                   SimpleNamespace(properties={"source_file": "other.txt (part 1)"})]
        with patch.object(tuning.wc, "get_client") as client:
            client.return_value.collections.get.return_value.iterator.return_value = objects
            self.assertTrue(tuning._has_stored_listed_name("Coll", documents))
            client.return_value.collections.get.return_value.iterator.return_value = objects[1:]
            self.assertFalse(tuning._has_stored_listed_name("Coll", documents))


class WithinLimitsUnderContextAwareTests(JobCase):
    """PR #630 coding review: a file within both limits is stored whole, whatever
    its number of records; context_aware no longer splits at 10,000 units."""

    def test_more_than_10000_records_under_10000_chunks_are_stored_whole(self):
        data = "".join(json.dumps({"t": f"{i:07d} short"}) + "\n" for i in range(20_000))
        job, backend = self.run_job([("many.jsonl", data)], "context_aware", 1000)
        self.assertEqual((job["status"], job["errors"], job["split"]), ("completed", [], []), job)
        self.assertEqual(backend.names(), ["many.jsonl"])
        self.assertLessEqual(len(backend.of("many.jsonl")), CAP)
        ids = __import__("re").findall(r"\d{7}", "".join(backend.of_contents("many.jsonl")))
        self.assertEqual(ids, [f"{i:07d}" for i in range(20_000)])

    def test_parts_are_sized_by_replaying_the_packing(self):
        from services import splitter
        # 600-character records, one chunk each at chunk_size 1000, 17,000 of them
        # (over the text limit): each part's chunk count is predicted, not refused.
        refused = []

        def chunk(text, strategy, **kwargs):
            try:
                return chunker.chunk(text, strategy, **kwargs)
            except chunker.ChunkLimitError:
                refused.append(len(text))
                raise

        path = Path(tempfile.mkdtemp()) / "big.jsonl"
        path.write_text(records(17_000, 600))
        settings = splitter.Settings("context_aware", 1000, 0, 0.85, 0)
        parts = list(splitter.split_file(path, settings, chunk=chunk))
        self.assertEqual([len(c) for _, c in parts], [CAP, 7_000])
        self.assertEqual(refused, [])


class StoredTextCapTests(JobCase):
    """PR #630 security review, maintainer's decision: a file split into parts
    fails, through the all-or-nothing rollback, when its chunks would store more
    than ten times its own size. A file stored whole is never refused by it."""

    @staticmethod
    def message(stored, own):
        return (f"it would store {stored:,} characters of chunks, more than 10 times its own "
                f"size ({own:,} characters); use less chunk_overlap, shorten repeated headers "
                f"or keys, or split the file")

    def test_a_split_jsonl_whose_overlap_stores_over_ten_times_its_size_fails_whole(self):
        # overlap 1000/990: every character is stored about 100 times. 250 records
        # of 1,000 characters are over one part (about 101,000 characters).
        data = records(250, 1000)
        job, backend = self.run_job([("windows.jsonl", data)], "overlap", 1000, overlap=990)
        self.assertEqual((job["status"], job["split"]), ("failed", []), job)
        self.assertEqual(len(job["errors"]), 1)
        self.assertRegex(job["errors"][0], r"^windows\.jsonl: it would store [\d,]+ characters of "
                         r"chunks, more than 10 times its own size \(" + f"{len(data):,}" + r" characters\)")
        self.assertNotIn("part ", job["errors"][0])
        self.assertEqual((backend.chunks, job["chunks_stored"]), ([], 0))

    def test_a_split_csv_whose_parts_store_over_ten_times_its_size_fails_whole(self):
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(["name", "notes"])
        writer.writerows([f"n{i:06d}", "word " * 40] for i in range(2_000))
        job, backend = self.run_job([("people.csv", out.getvalue())], "overlap", 1000, overlap=990)
        self.assertEqual(job["status"], "failed", job)
        self.assertTrue(job["errors"][0].startswith("people.csv: it would store "), job["errors"])
        self.assertEqual(backend.chunks, [])

    def test_a_file_stored_whole_is_never_refused_by_the_cap(self):
        # 20,000 characters at 1000/990 store about 2,000,000: 100 times, but one part.
        data = records(20, 1000)
        job, backend = self.run_job([("small.jsonl", data)], "overlap", 1000, overlap=990)
        self.assertEqual((job["status"], job["errors"]), ("completed", []), job)
        self.assertEqual(backend.names(), ["small.jsonl"])
        self.assertGreater(sum(len(c["content"]) for c in backend.chunks), 10 * len(data))

    def test_a_rechunk_over_the_cap_fails_whole(self):
        data = records(250, 1000).encode()
        work = Path(tempfile.mkdtemp())
        digest = __import__("hashlib").sha256(data).hexdigest()
        (work / digest).write_bytes(data)
        documents = {digest: {"filenames": ["windows.jsonl"]}}
        with patch.object(tuning.settings, "upload_dir", str(work)), \
                patch.object(tuning.sources, "load_index", return_value={"documents": documents}), \
                patch.object(tuning.sources, "collection_dir", return_value=work), \
                patch.object(tuning.wc, "get_client") as client:
            client.return_value.collections.get.return_value.iterator.return_value = [
                SimpleNamespace(properties={"source_file": "windows.jsonl", "chunk_index": 0})]
            with self.assertRaisesRegex(ValueError, r"^windows\.jsonl: it would store [\d,]+ "
                                                    r"characters of chunks, more than 10 times"):
                tuning._chunks_from_sources("Coll", "overlap", 1000, 990, 0.85, 0)


class SplitOnlyWhenRefusedTests(JobCase):
    """PR #630 coding review (cc99010): whether a file is split is decided by the
    chunker on the whole file, never by the part-size estimate or the raw size."""

    def kinds(self, name, data, strategy, size):
        from services import splitter
        path = Path(tempfile.mkdtemp()) / name
        path.write_text(data)
        settings = splitter.Settings(strategy, size, 0, 0.85, 100)
        return [kind for kind, _ in splitter.split_file(path, settings)]

    @staticmethod
    def sparse_csv(rows, cols=40):
        return (",".join(f"c{i}" for i in range(cols)) + "\n"
                + "".join(f"{r}" + "," * (cols - 1) + "\n" for r in range(rows)))

    def test_a_sparse_csv_within_the_limits_is_stored_whole(self):
        # The reviewer's probe: 2.69 MB of CSV, about 349,000 characters of text.
        data = self.sparse_csv(60_000)
        for strategy in ("context_aware", "fixed"):
            with self.subTest(strategy=strategy):
                self.assertEqual(self.kinds("s.csv", data, strategy, 200), ["whole"])

    def test_a_csv_over_the_text_limit_in_bytes_but_not_in_text_is_stored_whole(self):
        data = self.sparse_csv(250_000)
        self.assertGreater(len(data), TEXT)
        job, backend = self.run_job([("wide.csv", data)], "fixed", 1000)
        self.assertEqual((job["status"], job["errors"], job["split"]), ("completed", [], []), job)
        self.assertEqual(backend.names(), ["wide.csv"])

    def test_text_between_one_part_estimate_and_the_text_limit_is_stored_whole(self):
        # The band case: 5,000,200 characters at context_aware/500 is over
        # 10,000 x 500 but chunks whole into 2,000 chunks.
        bodies = ["r " * 1247 + "r"] * 2000  # records of 2,498 characters with "t: "
        bodies[0] += "s" * 202
        data = "".join(json.dumps({"t": b}) + "\n" for b in bodies)
        text, _ = ingest_pipeline._parse_file(self.write("band.jsonl", data))
        self.assertEqual(len(text), 5_000_200)
        self.assertEqual(self.kinds("band.jsonl", data, "context_aware", 500), ["whole"])

    def write(self, name, data):
        path = Path(tempfile.mkdtemp()) / name
        path.write_text(data)
        return path

    def test_a_rechunk_part_error_names_the_file(self):
        data = records(13_000, 600).encode()
        work = Path(tempfile.mkdtemp())
        digest = __import__("hashlib").sha256(data).hexdigest()
        (work / digest).write_bytes(data)
        documents = {digest: {"filenames": ["big.jsonl"]}}
        calls = []

        def chunk(**kwargs):
            calls.append(1)
            if len(calls) == 3:  # the whole file, part 1, then part 2
                raise ValueError("owned failure")
            return chunker.chunk(**kwargs)

        with patch.object(tuning.settings, "upload_dir", str(work)), \
                patch.object(tuning.sources, "load_index", return_value={"documents": documents}), \
                patch.object(tuning.sources, "collection_dir", return_value=work), \
                patch.object(tuning, "do_chunk", side_effect=chunk), \
                patch.object(tuning.wc, "get_client") as client:
            client.return_value.collections.get.return_value.iterator.return_value = [
                SimpleNamespace(properties={"source_file": "big.jsonl", "chunk_index": 0})]
            with self.assertRaisesRegex(ValueError, r"^big\.jsonl: part 2: owned failure$"):
                tuning._chunks_from_sources("Coll", "context_aware", 1000, 0, 0.85, 0)


if __name__ == "__main__":
    unittest.main()
