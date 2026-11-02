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
        data = records(10_500, 600) + "{not json\n"
        backend = FakeCollection([("data.jsonl", ["earlier"])])
        job, backend = self.run_job([("data.jsonl", data), ("ok.jsonl", records(2, 50))],
                                    "context_aware", backend=backend)
        self.assertEqual((job["status"], job["files_completed"], job["files_failed"]),
                         ("partial", 1, 1), job)
        self.assertEqual(len(job["errors"]), 1)
        self.assertRegex(job["errors"][0], r"^data\.jsonl: part 2: line 10501 is not valid JSON: "
                                           r".*; nothing from it was stored$")
        self.assertEqual([c["content"] for c in backend.of("data.jsonl")], ["earlier"])
        self.assertEqual(backend.names(), ["data.jsonl", "ok.jsonl"])
        # Part 1's 10,000 chunks were counted, then removed again.
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


if __name__ == "__main__":
    unittest.main()
