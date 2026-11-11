"""Document preparation (#638): the area, the pipeline, jobs, import and routes.

SPECIFICATIONS.md §3.1.11 and §10.8 (U1-U14). Runs with the API's dependencies,
in the built API image with no network (`21_prepare.sh`):

    docker run --rm --network none -v "$PWD:/repo:ro" -w /repo -e RAG_TEST_API_DIR=/repo/api \\
      "$(docker compose images -q api)" python scripts/tests/test_prepare.py

The LLM is a stub; nothing reaches Ollama or Weaviate. The preparation area and
the upload folder are temporary. Routes go through `main.app` with real sessions
in a temporary account store (`auth.testing`), without the lifespan.
"""
import asyncio
import gzip
import io
import json
import os
import re
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

sys.path.insert(0, os.environ.get("RAG_TEST_API_DIR") or str(Path(__file__).resolve().parents[2] / "api"))

from fastapi.testclient import TestClient

from config import settings
from main import app
from auth import policy
from auth import testing as auth_testing
from services import audit, ingest_pipeline, preparation
from services import preparation_jobs as jobs
from services import preparation_store as store

ROOT = Path(__file__).resolve().parents[2]
OWNER = {"name": "dev", "kind": "user"}


def page_text(pages=3, lines_per_page=6):
    """Text whose pages repeat a header and a page number line."""
    out = []
    for page in range(1, pages + 1):
        out.append("ACME CORP CONFIDENTIAL")
        for line in range(lines_per_page):
            out.append(f"Paragraph {page}.{line}: the committee reviewed item {page * 10 + line} "
                       f"and agreed to carry it forward to the next session.")
        out.append(f"Page {page}")
        out.append("")
    return "\n".join(out)


class StubLLM:
    """Answers the cleaning prompt by naming header and page-number lines, and the
    title prompt with a fixed title. Behaviour can be switched per test."""

    def __init__(self, clean=None, title=None):
        self.calls = []
        self.model_calls = 0
        self.clean = clean
        self.title = title

    async def chat(self, system, user):
        self.calls.append((system, user))
        if system.startswith(preparation.CLEAN_SYSTEM):
            if self.clean is not None:
                return await self._answer(self.clean, system, user)
            numbers = []
            for line in user.split("\n")[1:]:
                n, _, text = line.partition(": ")
                if text == "ACME CORP CONFIDENTIAL" or re.fullmatch(r"Page \d+", text):
                    numbers.append(int(n))
            return json.dumps({"remove": numbers})
        if self.title is not None:
            return await self._answer(self.title, system, user)
        return '```json\n{"title": "Committee minutes", "summary": "Items the committee carried forward."}\n```'

    async def _answer(self, behaviour, system, user):
        if callable(behaviour):
            result = behaviour(system, user)
            if asyncio.iscoroutine(result):
                result = await result
            return result
        return behaviour

    async def models(self):
        self.model_calls += 1
        return {f"{settings.llm_model}:latest", "nomic-embed-text:latest"}


class AreaCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / "uploads").mkdir()
        for name, value in (("preparation_dir", str(self.root / "area")),
                            ("upload_dir", str(self.root / "uploads")),
                            ("max_preparation_bytes", 10 * 1024 * 1024),
                            ("audit_dir", str(self.root / "audit"))):
            change = patch.object(settings, name, value)
            change.start()
            self.addCleanup(change.stop)
        # Unstructured's Markdown parser fetches a spaCy model on first use, and
        # these tests run with no network: the part check reads a written .md part
        # as its plain text here. P9 (21_prepare.sh) checks the real parser.
        real_parse = ingest_pipeline._parse_file

        def parse(path):
            if Path(path).suffix.lower() == ".md":
                return Path(path).read_text(encoding="utf-8"), []
            return real_parse(path)
        change = patch.object(ingest_pipeline, "_parse_file", parse)
        change.start()
        self.addCleanup(change.stop)
        store._busy.clear()
        jobs._queue = None
        jobs._worker = None
        jobs._loop = None
        jobs._cancel_requested.clear()
        jobs._jobs._records.clear()

    def write(self, name, data):
        path = self.root / "uploads" / name.replace("/", "_")
        path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
        return path

    def document(self, name, data):
        return store.create(name, self.write(name, data), OWNER)

    def prepare(self, record, llm=None, should_stop=lambda: False, write_part=None):
        llm = llm or StubLLM()
        folder = self.root / "out" / record["id"]
        folder.mkdir(parents=True, exist_ok=True)

        async def default_write(index, suffix, data):
            path = folder / f"{index + 1:03d}{suffix}"
            path.write_bytes(data)
            return path

        return asyncio.run(preparation.prepare_document(
            store.raw_path(record["id"]), record["name"], record["raw_type"], llm.chat,
            llm.models, should_stop, write_part or default_write))

    def part_text(self, record_id, entry):
        return (self.root / "out" / record_id / entry["file"]).read_text(encoding="utf-8")


# ── U1, U2: windows and the fidelity check ──────────────────────────────────

class WindowTests(unittest.TestCase):
    def test_windows_hold_whole_lines_up_to_3000_characters(self):
        text = "\n".join("x" * 99 for _ in range(100))
        ws = preparation.windows(text)
        self.assertTrue(all(w.characters <= 3000 for w in ws))
        self.assertEqual(sum(len(w.lines) for w in ws), 100)
        self.assertEqual([line for w in ws for line in w.lines], text.split("\n"))

    def test_a_line_over_3000_characters_is_alone_and_not_sent(self):
        text = "short\n" + "y" * 3500 + "\nafter"
        ws = preparation.windows(text)
        self.assertEqual([w.send for w in ws], [True, False, True])
        self.assertEqual(ws[1].lines, ["y" * 3500])

    def clean(self, lines, answers):
        answers = list(answers)

        async def chat(system, user):
            answer = answers.pop(0)
            if isinstance(answer, BaseException):
                raise answer
            return answer
        llm = preparation.LLM(chat, lambda: False, seconds=5)
        return asyncio.run(preparation.clean_window(preparation.Window(lines), llm)), llm

    def test_numbered_lines_are_sent_and_the_named_ones_removed(self):
        seen = []

        async def chat(system, user):
            seen.append(user)
            return '{"remove": [1, 3]}'
        llm = preparation.LLM(chat, lambda: False)
        lines = ["HEADER", "a long line of real content " * 3, "7"]
        cleaned = asyncio.run(preparation.clean_window(preparation.Window(lines + ["more content " * 10]), llm))
        self.assertIsNone(cleaned.reason)
        self.assertEqual(cleaned.lines, [lines[1], "more content " * 10])
        self.assertTrue(seen[0].startswith("Lines:\n1: HEADER\n2: "))

    def test_a_long_line_named_falls_back(self):
        lines = ["H", "z" * 201] + ["content " * 30] * 3
        cleaned, _ = self.clean(lines, ['{"remove": [2]}'])
        self.assertEqual(cleaned.reason, "removed-long-line")
        self.assertEqual(cleaned.lines, lines)

    def test_removing_more_than_a_fifth_falls_back(self):
        lines = ["header line"] * 5 + ["content"] * 3
        cleaned, _ = self.clean(lines, ['{"remove": [1, 2, 3]}'])
        self.assertEqual(cleaned.reason, "removed-too-much")
        self.assertEqual(cleaned.lines, lines)

    def test_an_unusable_answer_is_asked_again_once(self):
        lines = ["H", "content " * 30]
        cleaned, _ = self.clean(lines, ["no json here", '{"remove": [1]}'])
        self.assertIsNone(cleaned.reason)
        self.assertEqual(cleaned.lines, [lines[1]])
        cleaned, _ = self.clean(lines, ["nope", '{"remove": [9]}'])
        self.assertEqual(cleaned.reason, "unusable-answer")
        cleaned, _ = self.clean(lines, ['{"remove": "1"}', '{"remove": [true]}'])
        self.assertEqual(cleaned.reason, "unusable-answer")

    def test_timeouts_and_errors(self):
        lines = ["H", "content " * 30]
        cleaned, _ = self.clean(lines, [asyncio.TimeoutError(), asyncio.TimeoutError()])
        self.assertEqual(cleaned.reason, "llm-timeout")
        cleaned, _ = self.clean(lines, [RuntimeError("x"), RuntimeError("y")])
        self.assertEqual(cleaned.reason, "llm-error")
        cleaned, _ = self.clean(lines, [asyncio.TimeoutError(), RuntimeError("y")])
        self.assertEqual(cleaned.reason, "llm-error")
        cleaned, _ = self.clean(lines, [asyncio.TimeoutError(), '{"remove": []}'])
        self.assertIsNone(cleaned.reason)

    def test_a_valid_small_removal_is_not_a_fallback(self):
        lines = ["Page 3"] + ["content line"] * 20
        cleaned, _ = self.clean(lines, ['{"remove": [1]}'])
        self.assertIsNone(cleaned.reason)
        self.assertEqual(len(cleaned.lines), 20)


# ── U3, U4: LLM unavailable, size cap, title and summary ────────────────────

class LlmDocumentTests(AreaCase):
    def test_a_text_document_is_cleaned_titled_and_written_as_markdown(self):
        record = self.document("minutes.txt", page_text())
        result = self.prepare(record)
        [part] = result.parts
        text = self.part_text(record["id"], part)
        self.assertEqual(part["name"], "minutes.md")
        self.assertTrue(text.startswith("# Committee minutes\n\nSummary (generated): Items the "
                                        "committee carried forward.\n\n"))
        self.assertNotIn("ACME CORP CONFIDENTIAL", text)
        self.assertNotIn("Page 2", text)
        for line in page_text().split("\n"):
            if line.startswith("Paragraph"):
                self.assertIn(line, text)
        self.assertFalse(part["fallback"])
        self.assertEqual(part["fallbacks"], [])

    def test_three_failed_calls_in_a_row_make_the_rest_code_only(self):
        async def failing(system, user):
            raise RuntimeError("down")
        llm = StubLLM(clean=failing, title=failing)
        record = self.document("minutes.txt", page_text(pages=40))
        result = self.prepare(record, llm)
        [part] = result.parts
        reasons = [f["reason"] for f in part["fallbacks"]]
        self.assertEqual(reasons[0], "llm-error")
        self.assertIn("llm-unavailable", reasons)
        self.assertEqual(len(llm.calls), 3)
        self.assertEqual(part["summary_error"], "llm-unavailable")
        self.assertIsNone(part["title"])
        text = self.part_text(record["id"], part)
        self.assertFalse(text.startswith("# "))
        self.assertIn("ACME CORP CONFIDENTIAL", text)

    def test_a_missing_model_makes_the_document_code_only(self):
        llm = StubLLM()

        async def no_model():
            return {"nomic-embed-text:latest"}
        llm.models = no_model
        record = self.document("a.md", page_text())
        result = self.prepare(record, llm)
        self.assertEqual(result.parts[0]["fallbacks"], [{"window": None, "reason": "llm-unavailable"}])
        self.assertEqual(llm.calls, [])
        self.assertTrue(result.fallback)

    def test_over_1000_windows_is_too_large_for_the_llm_but_titled(self):
        with patch.object(preparation, "MAX_WINDOWS", 2):
            llm = StubLLM()
            record = self.document("big.txt", page_text(pages=20))
            result = self.prepare(record, llm)
        [part] = result.parts
        self.assertEqual(part["fallbacks"], [{"window": None, "reason": "too-large-for-llm"}])
        self.assertTrue(part["fallback"])
        self.assertEqual(part["title"], "Committee minutes")
        self.assertTrue(all(not s.startswith(preparation.CLEAN_SYSTEM) for s, _ in llm.calls))

    def test_title_retried_then_left_out(self):
        answers = ['{"title": "", "summary": "x"}', '{"title": "Ok", "summary": "Fine."}']
        llm = StubLLM(title=lambda s, u: answers.pop(0))
        record = self.document("a.md", "Some text.\n")
        part = self.prepare(record, llm).parts[0]
        self.assertEqual((part["title"], part["summary"], part["summary_error"]), ("Ok", "Fine.", None))
        long_summary = " ".join(["word"] * 81)
        llm = StubLLM(title=json.dumps({"title": "T", "summary": long_summary}))
        record = self.document("b.md", "Some text.\n")
        part = self.prepare(record, llm).parts[0]
        self.assertEqual((part["title"], part["summary_error"]), (None, "unusable-answer"))
        self.assertFalse(part["fallback"])
        llm = StubLLM(title=json.dumps({"title": "Two\nlines", "summary": "s"}))
        part = self.prepare(self.document("c.md", "Text.\n"), llm).parts[0]
        self.assertEqual(part["summary_error"], "unusable-answer")

    def test_csv_and_jsonl_are_labelled_from_their_data_in_the_record_only(self):
        csv_data = "name,team\nAda,core\nBo,docs\n"
        record = self.document("people.csv", csv_data)
        part = self.prepare(record).parts[0]
        self.assertEqual(self.part_text(record["id"], part), csv_data)
        self.assertEqual((part["title"], part["summary"], part["summary_error"]),
                         ("people.csv", "2 records. Fields: name, team. First: Ada; last: Bo.", None))
        self.assertEqual(part["records"], 2)
        jsonl = '{"a": 1}\n{"b": "x"}\n'
        record = self.document("d.jsonl", jsonl)
        part = self.prepare(record).parts[0]
        self.assertEqual(self.part_text(record["id"], part), jsonl)
        self.assertEqual(part["name"], "d.jsonl")
        self.assertEqual(part["records"], 2)
        self.assertEqual((part["title"], part["summary"], part["summary_error"]),
                         ("d.jsonl", "2 records. Fields: a, b. First: 1; last: x.", None))


# ── U14: record labels from the data (#648) ─────────────────────────────────

class RecordLabelTests(AreaCase):
    def label(self, name, data, llm=None):
        result = self.prepare(self.document(name, data), llm)
        return [(p["title"], p["summary"]) for p in result.parts]

    def test_titles_name_the_file_and_the_part(self):
        rows = "".join(f"{i},v{i}\n" for i in range(1, 301))
        with patch.object(preparation, "PART_LIMIT", 1000):
            result = self.prepare(self.document("a/t.csv", "id,value\n" + rows))
        count = len(result.parts)
        self.assertGreater(count, 2)
        self.assertEqual([p["title"] for p in result.parts],
                         [f"a/t.csv, part {n} of {count}" for n in range(1, count + 1)])
        # Keys are document positions' rows, so each part starts where the last ended.
        firsts = [p["summary"].split("First: ")[1].split(";")[0] for p in result.parts]
        lasts = [p["summary"].split("last: ")[1].rstrip(".") for p in result.parts]
        self.assertEqual(firsts[0], "1")
        self.assertEqual(lasts[-1], "300")
        for previous, first in zip(lasts, firsts[1:]):
            self.assertEqual(int(first), int(previous) + 1)
        self.assertEqual(sum(p["records"] for p in result.parts), 300)
        self.assertEqual(preparation.record_title("x.csv", 1, 1), "x.csv")

    def test_a_long_name_is_cut_to_keep_the_title_in_150_characters(self):
        name = "n" * 200 + ".csv"
        one = preparation.record_title(name, 1, 1)
        self.assertEqual(len(one), 150)
        self.assertTrue(one.endswith("…"))
        several = preparation.record_title(name, 3, 12)
        self.assertEqual(len(several), 150)
        self.assertTrue(several.endswith("…, part 3 of 12"))
        self.assertEqual(preparation.record_title("a  b\tc.csv", 1, 2), "a b c.csv, part 1 of 2")

    def test_csv_fields_are_the_header_cells(self):
        [(title, summary)] = self.label("p.csv", 'id,,"long  name"\n7,x,y\n')
        self.assertEqual(summary, "1 record. Fields: id, (blank), long name. First: 7; last: 7.")

    def test_json_lines_fields_are_top_level_keys_first_seen_and_a_nested_object_is_one(self):
        data = ('{"id": "b1", "meta": {"x": 1, "y": 2}}\n'
                '[1, 2]\n'
                '{"title": "t", "id": "b2"}\n')
        [(title, summary)] = self.label("bills.jsonl", data)
        self.assertEqual(title, "bills.jsonl")
        self.assertEqual(summary, "3 records. Fields: id, meta, title. First: b1; last: t.")

    def test_a_part_with_no_object_records_has_no_fields(self):
        [(title, summary)] = self.label("s.json", "[1, 2, 3]")
        self.assertEqual(summary, "1 record. Fields: none. First: record 1; last: record 1.")

    def test_more_than_20_fields(self):
        header = ",".join(f"f{i}" for i in range(1, 26))
        [(title, summary)] = self.label("w.csv", header + "\n" + ",".join("1" * 25) + "\n")
        listed = ", ".join(f"f{i}" for i in range(1, 21))
        self.assertEqual(summary, f"1 record. Fields: {listed}, and 5 more. First: 1; last: 1.")

    def test_key_kinds(self):
        key = preparation._json_key
        number = ingest_pipeline._JsonNumber
        self.assertEqual(key({"a": "x  y\nz"}, 1), "x y z")
        self.assertEqual(key({"a": number("1.50")}, 1), "1.50")
        self.assertEqual(key({"a": True}, 1), "true")
        self.assertEqual(key({"a": False}, 1), "false")
        self.assertEqual(key({"a": {"b": number("2")}}, 1), '{"b":2}')
        self.assertEqual(key({"a": [number("1"), "x"]}, 1), '[1,"x"]')
        self.assertEqual(key({"a": {}}, 1), "{}")
        data = '{"n": 1.50}\n{"n": true}\n'
        [(title, summary)] = self.label("k.jsonl", data)
        self.assertEqual(summary, "2 records. Fields: n. First: 1.50; last: true.")

    def test_fallbacks_use_the_record_position_in_the_document(self):
        key = preparation._json_key
        for value in ([1], "s", None, {}, {"a": None}, {"a": ""}, {"a": "  \t"}):
            self.assertEqual(key(value, 1234), "record 1,234", value)
        self.assertEqual(preparation._csv_key(["  ", "x"], 5), "record 5")
        self.assertEqual(preparation._csv_key([], 5), "record 5")
        lines = "".join('{"id": %s, "pad": "%s"}\n' % ('null' if i % 2 else i, "p" * 20)
                        for i in range(1, 101))
        with patch.object(preparation, "PART_LIMIT", 300):
            result = self.prepare(self.document("f.jsonl", lines))
        self.assertGreater(len(result.parts), 2)
        position = 0
        for part in result.parts:
            first, last = position + 1, position + part["records"]
            want_first = f"record {first:,}" if first % 2 else str(first)
            want_last = f"record {last:,}" if last % 2 else str(last)
            self.assertTrue(part["summary"].endswith(f"First: {want_first}; last: {want_last}."),
                            part["summary"])
            position = last
        rows = "id,v\n1,a\n,b\n"
        [(title, summary)] = self.label("c.csv", rows)
        self.assertEqual(summary, "2 records. Fields: id, v. First: 1; last: record 2.")

    def test_values_and_names_are_cut_at_40_characters(self):
        long = "x" * 60
        cut = "x" * 39 + "…"
        self.assertEqual(preparation._json_key({"a": long}, 1), cut)
        [(title, summary)] = self.label("l.csv", f"{long},b\n{long},1\n")
        self.assertEqual(summary, f"1 record. Fields: {cut}, b. First: {cut}; last: {cut}.")

    def test_the_summary_stays_in_80_words(self):
        name = " ".join("w" for _ in range(20))         # 39 characters, 20 words
        fields = [name] * 20
        key = " ".join("k" for _ in range(20))
        summary = preparation.record_summary(5000, fields, key, key)
        self.assertLessEqual(len(summary.split()), preparation.MAX_SUMMARY_WORDS)
        self.assertIn(f"Fields: {name}, and 19 more.", summary)
        self.assertTrue(summary.startswith("5,000 records. "))
        short = preparation.record_summary(3, ["a", "b"], "x", "y")
        self.assertEqual(short, "3 records. Fields: a, b. First: x; last: y.")
        self.assertEqual(preparation.record_summary(1, ["a"], "x", "x"),
                         "1 record. Fields: a. First: x; last: x.")
        self.assertEqual(preparation.record_summary(0, ["a", "b"], "", ""), "0 records. Fields: a, b.")
        [(title, summary)] = self.label("h.csv", "a,b\n")
        self.assertEqual(summary, "0 records. Fields: a, b.")

    def test_record_documents_make_no_llm_call_and_skip_the_model_check(self):
        async def failing(system, user):
            raise RuntimeError("down")

        async def no_model():
            return set()
        samples = (("r.csv", b"id,v\n1,a\n"), ("r.jsonl", b'{"id": 1}\n'),
                   ("r.jsonl.gz", gzip.compress(b'{"id": 1}\n')), ("r.json", b'[{"id": 1}]'),
                   ("r.json.gz", gzip.compress(b'[{"id": 1}]')))
        for name, data in samples:
            for broken in (False, True):
                llm = StubLLM(clean=failing if broken else None, title=failing if broken else None)
                if broken:
                    llm.models = no_model
                result = self.prepare(self.document(name, data), llm)
                [part] = result.parts
                self.assertEqual(llm.calls, [], name)
                self.assertEqual(llm.model_calls, 0, name)
                self.assertEqual(part["title"], name)
                self.assertEqual(part["summary"], "1 record. Fields: id, v. First: 1; last: 1."
                                 if name.endswith(".csv") else
                                 "1 record. Fields: id. First: 1; last: 1.")
                self.assertIsNone(part["summary_error"])
                self.assertFalse(result.fallback)

    def test_text_documents_still_check_the_model_and_use_the_llm_title(self):
        llm = StubLLM()
        part = self.prepare(self.document("m.txt", page_text()), llm).parts[0]
        self.assertEqual(llm.model_calls, 1)
        self.assertEqual(part["title"], "Committee minutes")


# ── U5: normalisation ───────────────────────────────────────────────────────

class NormaliseTests(unittest.TestCase):
    def test_bom_line_endings_nfc_and_control_characters(self):
        raw = "﻿a\r\nb\rc\x00d\x07e\tf\x7fg\x0cÉ"
        self.assertEqual(preparation.normalise_text(raw), "a\nb\ncde\tfgÉ")


# ── U6: parts, names and checks ─────────────────────────────────────────────

class PartTests(AreaCase):
    def test_text_parts_stay_under_the_limit_and_are_named(self):
        paragraphs = "\n\n".join(f"Paragraph {i} " + "word " * 40 for i in range(200))
        with patch.object(preparation, "PART_LIMIT", 4000), patch.object(preparation, "HEADER_ROOM", 500):
            record = self.document("dir/report.txt", paragraphs)
            result = self.prepare(record)
        self.assertGreater(len(result.parts), 1)
        self.assertEqual(result.parts[0]["name"], "dir/report-part-001.md")
        self.assertEqual(result.parts[-1]["name"], f"dir/report-part-{len(result.parts):03d}.md")
        for part in result.parts:
            self.assertLessEqual(len(self.part_text(record["id"], part)), 4000 + 1)

    def test_a_large_text_file_is_prepared_part_by_part_in_bounded_memory(self):
        # #621's streaming, kept by preparation (PR #639 review): a .txt many parts
        # long is read part by part, its windows counted and built one part at a
        # time, so the memory used stays near one part, far below the file's size.
        import tracemalloc
        paragraph = "word " * 199 + "\n"
        path = self.root / "uploads" / "big.txt"
        with path.open("w", encoding="utf-8") as fh:
            for i in range(8000):
                fh.write(f"Paragraph {i} " + paragraph + "\n")
        size = path.stat().st_size
        self.assertGreater(size, 7_000_000)
        record = store.create("big.txt", path, OWNER)
        produced, written = [0], []
        real_iter = preparation.iter_parts

        def counting(path, raw_type):
            for part in real_iter(path, raw_type):
                produced[0] += 1
                yield part

        async def write_part(index, suffix, data):
            written.append(len(data))
            return self.write(f"out{index}{suffix}", data)
        with patch.object(preparation, "PART_LIMIT", 200_000), \
                patch.object(preparation, "iter_parts", counting):
            tracemalloc.start()
            try:
                result = self.prepare(record, write_part=write_part)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        # One part is 200,000 characters; the file is 40 times that.
        self.assertLess(peak, size / 3, f"peak {peak:,} bytes for a {size:,}-byte file")
        parts = len(result.parts)
        self.assertGreater(parts, 30)
        # Two streaming passes: one to count windows, one to prepare.
        self.assertEqual(produced[0], 2 * parts)
        self.assertEqual(result.parts[0]["fallbacks"], [{"window": None, "reason": "too-large-for-llm"}])
        self.assertEqual(result.parts[-1]["name"], f"big-part-{parts:03d}.md")

    def test_csv_parts_repeat_the_header(self):
        rows = "".join(f"{i},value {i}\n" for i in range(300))
        with patch.object(preparation, "PART_LIMIT", 1000):
            record = self.document("t.csv", "id,value\n" + rows)
            result = self.prepare(record)
        self.assertGreater(len(result.parts), 1)
        joined = ""
        for part in result.parts:
            text = self.part_text(record["id"], part)
            self.assertTrue(text.startswith("id,value\n"))
            joined += text[len("id,value\n"):]
        self.assertEqual(joined, rows)

    def test_a_quoted_csv_field_keeps_its_line_break(self):
        data = 'id,note\n1,"two\nlines"\n2,plain\n'
        record = self.document("q.csv", data)
        part = self.prepare(record).parts[0]
        self.assertEqual(self.part_text(record["id"], part), data)
        self.assertEqual(part["records"], 2)

    def test_a_stray_quote_in_a_csv_is_text_and_its_rows_stay_bounded(self):
        # PR #639 review: a quote inside an unquoted field (5" screen) is text, as
        # csv.reader reads it for ingest; it must not join the following rows.
        rows = ["1,5\" screen,x\n"] + [f"{i},plain {i},y\n" for i in range(2, 400)]
        with patch.object(preparation, "PART_LIMIT", 2000):
            record = self.document("stray.csv", "id,item,z\n" + "".join(rows))
            result = self.prepare(record)
        self.assertGreater(len(result.parts), 2)
        joined = ""
        for part in result.parts:
            text = self.part_text(record["id"], part)
            self.assertLessEqual(len(text), 2000)
            self.assertTrue(text.startswith("id,item,z\n"))
            joined += text[len("id,item,z\n"):]
        self.assertEqual(joined, "".join(rows))
        self.assertEqual(sum(p["records"] for p in result.parts), len(rows))

    def test_an_unclosed_quote_fails_cleanly_without_holding_the_file(self):
        # A quote opened and never closed would swallow every following line;
        # the row is refused once it is longer than a part, without holding more.
        # (The encoding check reads 1 MiB blocks, as ingest's does, so the peak is
        # flat in the file's size; the file is large enough to show that.)
        import tracemalloc
        lines = "".join(f"{i},line {i} " + "w" * 80 + "\n" for i in range(120_000))
        record = self.document("open.csv", 'id,note\n1,"never closed\n' + lines)
        size = store.raw_path(record["id"]).stat().st_size
        with patch.object(preparation, "PART_LIMIT", 20_000):
            tracemalloc.start()
            try:
                with self.assertRaisesRegex(ValueError, "CSV row longer than"):
                    self.prepare(record)
                _, peak = tracemalloc.get_traced_memory()
            finally:
                tracemalloc.stop()
        self.assertLess(peak, size / 4, f"peak {peak:,} bytes for a {size:,}-byte file")

    def test_one_huge_line_fails_cleanly(self):
        # One line with no line break, longer than a part: as a data row, and as
        # the header (the header rule's message).
        with patch.object(preparation, "PART_LIMIT", 5000):
            record = self.document("long.csv", "a,b\n1," + "x" * 50_000)
            with self.assertRaisesRegex(ValueError, "CSV row longer than"):
                self.prepare(record)
            record = self.document("nohead.csv", "h" * 50_000)
            with self.assertRaisesRegex(ValueError, "header row"):
                self.prepare(record)

    def test_a_field_over_the_csv_field_limit_fails_cleanly(self):
        # Ingest's csv.reader refuses a field over Unstructured's CSV_FIELD_LIMIT;
        # so does preparation, as a reason, not a crash.
        from unstructured.partition.csv import CSV_FIELD_LIMIT
        record = self.document("field.csv", 'a,b\n1,"' + "f" * (CSV_FIELD_LIMIT + 10) + '"\n')
        with patch.object(preparation, "PART_LIMIT", 4 * CSV_FIELD_LIMIT):
            with self.assertRaisesRegex(ValueError, "is not a CSV that can be read"):
                self.prepare(record)

    def test_a_csv_header_over_half_a_part_fails(self):
        with patch.object(preparation, "PART_LIMIT", 100):
            record = self.document("h.csv", "x" * 60 + "\n1\n")
            with self.assertRaisesRegex(ValueError, "header row"):
                self.prepare(record)

    def test_a_record_is_never_cut(self):
        records = [json.dumps({"text": "r" * 500, "n": i}) for i in range(10)]
        with patch.object(preparation, "PART_LIMIT", 1200):
            record = self.document("r.jsonl", "\n".join(records) + "\n")
            result = self.prepare(record)
        lines = []
        for part in result.parts:
            lines += self.part_text(record["id"], part).splitlines()
        self.assertEqual(lines, records)
        self.assertTrue(all(p["name"].startswith("r-part-") for p in result.parts))

    def test_names(self):
        self.assertEqual(preparation.part_names("a/b.jsonl.gz", "jsonl", 1), ["a/b.jsonl"])
        self.assertEqual(preparation.part_names("x.json.gz", "jsonl", 2),
                         ["x-part-001.jsonl", "x-part-002.jsonl"])
        self.assertEqual(preparation.part_names("report.pdf", "text", 1), ["report.md"])
        self.assertEqual(preparation.part_names("notes", "text", 1), ["notes.md"])

    def test_a_part_over_the_chunk_limit_fails_the_check(self):
        path = self.root / "big.jsonl"
        path.write_text(json.dumps({"t": "w " * 100}) + "\n")
        with patch.object(preparation, "MAX_CHUNKS_PER_FILE", 0):
            with self.assertRaisesRegex(ValueError, "chunks"):
                preparation.check_part(path, "jsonl")
        self.assertEqual(preparation.overlap_estimate(1000), 1)
        self.assertEqual(preparation.overlap_estimate(1801), 3)

    def test_a_file_ingest_refuses_fails_with_its_reason(self):
        record = self.document("bad.jsonl", '{"a": 1}\n\n{"b": 2}\n')
        with self.assertRaisesRegex(ValueError, "line 2 is blank"):
            self.prepare(record)
        record = self.document("empty.txt", "\n\n")
        with self.assertRaisesRegex(ValueError, "no text"):
            self.prepare(record)


# ── U7: JSON to JSON Lines ──────────────────────────────────────────────────

class JsonTests(AreaCase):
    def test_records_as_ingest_numbers_as_written_empty_kept_no_nfc(self):
        decomposed = "É"
        data = '[{"n": 1.50, "s": "%s"}, {}, {"deep": [1, 2]}]' % decomposed
        record = self.document("d.json", data)
        part = self.prepare(record).parts[0]
        lines = self.part_text(record["id"], part).splitlines()
        self.assertEqual(lines, ['{"n":1.50,"s":"%s"}' % decomposed, "{}", '{"deep":[1,2]}'])
        self.assertEqual(part["name"], "d.jsonl")
        self.assertEqual(part["records"], 3)

    def test_a_scalar_array_is_one_record(self):
        record = self.document("s.json", "[1, 2, 3]")
        part = self.prepare(record).parts[0]
        self.assertEqual(self.part_text(record["id"], part), "[1,2,3]\n")

    def test_element_files_are_written_as_records(self):
        data = json.dumps([{"type": "Title", "element_id": "a", "text": "Hello", "metadata": {}}])
        record = self.document("e.json", data)
        part = self.prepare(record).parts[0]
        self.assertEqual(json.loads(self.part_text(record["id"], part)), json.loads(data)[0])

    def test_gzipped_json_lines_are_decompressed(self):
        record = self.document("z.jsonl.gz", gzip.compress(b'{"a": 1}\r\n{"b": 2}\n'))
        part = self.prepare(record).parts[0]
        self.assertEqual(self.part_text(record["id"], part), '{"a": 1}\n{"b": 2}\n')
        self.assertEqual(part["name"], "z.jsonl")

    def test_blank_lines_after_the_last_record_are_dropped(self):
        record = self.document("t.jsonl", '{"a": 1}\n\n\n')
        part = self.prepare(record).parts[0]
        self.assertEqual(self.part_text(record["id"], part), '{"a": 1}\n')

    def test_ingest_reads_json_lines_as_before(self):
        records = list(ingest_pipeline._iter_jsonl_records(io.BytesIO(b'{"a": 1}\n{}\n{"b": [1, 2]}\n')))
        self.assertEqual(records, [("line 1", "a: 1"), ("line 3", "b: 1, 2")])


# ── jobs: U8-U11 ────────────────────────────────────────────────────────────

class JobCase(AreaCase):
    def setUp(self):
        super().setUp()
        self.llm = StubLLM()
        for name, value in (("chat", self.llm.chat), ("list_models", self.llm.models)):
            change = patch.object(jobs.ollama, name, value)
            change.start()
            self.addCleanup(change.stop)

    async def run_job(self, ids):
        answer = await jobs.start(ids, OWNER)
        await self.finish(answer["job_id"])
        return jobs.get_job(answer["job_id"])

    async def finish(self, job_id, timeout=30):
        for _ in range(int(timeout / 0.02)):
            job = jobs.get_job(job_id)
            if job and job["status"] in ("completed", "failed", "partial", "cancelled"):
                return job
            await asyncio.sleep(0.02)
        self.fail(f"job {job_id} did not end")


class JobTests(JobCase):
    def test_a_job_prepares_its_documents(self):
        a = self.document("a.txt", page_text())
        b = self.document("b.csv", "x,y\n1,2\n")
        job = asyncio.run(self.run_job([a["id"], b["id"]]))
        self.assertEqual((job["status"], job["documents_completed"], job["documents_failed"]),
                         ("completed", 2, 0))
        self.assertEqual(store.get(a["id"])["status"], "prepared")
        self.assertTrue(store.get(b["id"])["code_only_by_type"])
        self.assertEqual(store.get(b["id"])["parts"][0]["name"], "b.csv")

    def test_a_failed_first_run_leaves_the_document_failed(self):
        bad = self.document("bad.jsonl", "not json\n")
        good = self.document("good.md", "Fine text.\n")
        job = asyncio.run(self.run_job([bad["id"], good["id"]]))
        self.assertEqual(job["status"], "partial")
        record = store.get(bad["id"])
        self.assertEqual(record["status"], "failed")
        self.assertIn("not valid JSON", record["last_error"])
        self.assertEqual(record["parts"], [])

    def test_a_failed_rerun_keeps_the_earlier_output(self):
        doc = self.document("a.md", "Fine text.\n")
        asyncio.run(self.run_job([doc["id"]]))
        earlier = store.get(doc["id"])["parts"]
        with patch.object(preparation, "check_part", side_effect=ValueError("broken part")):
            job = asyncio.run(self.run_job([doc["id"]]))
        self.assertEqual(job["status"], "failed")
        record = store.get(doc["id"])
        self.assertEqual((record["status"], record["last_error"]), ("prepared", "broken part"))
        self.assertEqual(record["parts"], earlier)
        self.assertEqual(len(store.part_paths(doc["id"])), 1)
        self.assertTrue(store.part_paths(doc["id"])[0][1].is_file())
        # A later successful run clears the error and replaces the output.
        asyncio.run(self.run_job([doc["id"]]))
        self.assertIsNone(store.get(doc["id"])["last_error"])

    def test_the_storage_limit_refuses_a_part_and_keeps_nothing(self):
        doc = self.document("a.md", "x " * 5000)
        with patch.object(settings, "max_preparation_bytes", store.usage() + 100):
            job = asyncio.run(self.run_job([doc["id"]]))
        record = store.get(doc["id"])
        self.assertEqual(record["status"], "failed")
        self.assertIn("can't hold this prepared part", record["last_error"])
        self.assertFalse((Path(settings.preparation_dir) / "docs" / doc["id"] / "prepared").exists())
        self.assertEqual(job["status"], "failed")
        with patch.object(settings, "max_preparation_bytes", store.usage()):
            with self.assertRaises(store.StorageFull):
                asyncio.run(jobs.start([doc["id"]], OWNER))

    def test_cancel_returns_documents_to_their_earlier_state(self):
        started = None

        async def scenario():
            nonlocal started
            gate = asyncio.Event()

            async def slow(system, user):
                await gate.wait()
                return '{"remove": []}'
            self.llm.clean = slow
            prepared = self.document("p.md", "Earlier.\n")
            self.llm.clean = None
            await self.run_job([prepared["id"]])
            self.llm.clean = slow
            first = self.document("a.md", page_text())
            answer = await jobs.start([first["id"], prepared["id"]], OWNER)
            for _ in range(200):
                if store.get(first["id"])["status"] == "preparing":
                    break
                await asyncio.sleep(0.01)
            await jobs.cancel(answer["job_id"])
            gate.set()
            job = await self.finish(answer["job_id"])
            started = (first, prepared, job)
        asyncio.run(scenario())
        first, prepared, job = started
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(store.get(first["id"])["status"], "raw")
        self.assertEqual(store.get(prepared["id"])["status"], "prepared")
        self.assertFalse(store.is_busy(first["id"]))
        with self.assertRaises(jobs.JobNotRunning):
            asyncio.run(jobs.cancel(job["job_id"]))
        with self.assertRaises(jobs.JobNotFound):
            asyncio.run(jobs.cancel("nope"))

    def test_one_job_at_a_time_and_busy_documents(self):
        async def scenario():
            gate = asyncio.Event()

            async def slow(system, user):
                await gate.wait()
                return '{"remove": []}'
            self.llm.clean = slow
            a = self.document("a.md", "A text.\n")
            b = self.document("b.md", "B text.\n")
            first = await jobs.start([a["id"]], OWNER)
            second = await jobs.start([b["id"]], OWNER)
            for _ in range(200):
                if jobs.get_job(first["job_id"])["status"] == "running":
                    break
                await asyncio.sleep(0.01)
            self.assertEqual(jobs.get_job(second["job_id"])["status"], "queued")
            with self.assertRaises(store.DocumentBusy):
                await jobs.start([a["id"]], OWNER)
            with self.assertRaises(store.DocumentBusy):
                store.delete(a["id"])
            with self.assertRaises(store.DocumentBusy):
                await jobs.import_documents(SimpleNamespace(document_ids=[a["id"]]), OWNER)
            gate.set()
            await self.finish(first["job_id"])
            await self.finish(second["job_id"])
            self.assertEqual(jobs.get_job(second["job_id"])["status"], "completed")
        asyncio.run(scenario())

    def test_an_interrupted_job_reads_failed_with_the_reason(self):
        folder = Path(settings.upload_dir) / "jobs" / "prepare"
        folder.mkdir(parents=True)
        (folder / "p1.json").write_text(json.dumps({
            "job_id": "p1", "status": "running", "documents_total": 2, "documents_completed": 1,
            "documents_failed": 0, "document_ids": [], "names": [], "errors": [],
            "current": {"document_id": "x", "name": "a.md", "windows_done": 1, "windows_total": 2}}))
        bound = jobs.jobstore.JobStore("prepare")
        self.assertEqual(bound.load(), ["p1"])
        record = bound["p1"]
        self.assertEqual((record["status"], record["current"]), ("partial", None))
        self.assertIn("Start the preparation again.", record["errors"][-1])

    def test_restart_recovery(self):
        doc = self.document("a.md", "Text.\n")
        store.queue_for_job([doc["id"]], "j1")
        store.begin(doc["id"], "j1")
        store._busy.clear()
        self.assertEqual(store.recover(), ["a.md"])
        self.assertEqual(store.get(doc["id"])["status"], "raw")
        self.assertFalse((Path(settings.preparation_dir) / "docs" / doc["id"] / "prepared.new").exists())


# ── routes: upload, list, download, delete, import (U8, U11-U13) ─────────────

class RouteTests(JobCase):
    def setUp(self):
        super().setUp()
        self.client = TestClient(app, headers=auth_testing.headers("developer"))

    def upload(self, files):
        return self.client.post("/prepare/upload", files=[("files", f) for f in files])

    def zip_bytes(self, members):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            for name, data in members.items():
                z.writestr(name, data)
        return buf.getvalue()

    def test_upload_list_download_and_delete(self):
        answer = self.upload([("a.txt", b"Hello.\n"), ("b.zip", self.zip_bytes(
            {"dir/c.jsonl": '{"a": 1}\n', "x.exe": "no", "__MACOSX/._c.jsonl": "meta"}))])
        self.assertEqual(answer.status_code, 201, answer.text)
        body = answer.json()
        self.assertEqual(sorted(d["name"] for d in body["documents"]), ["a.txt", "dir/c.jsonl"])
        self.assertTrue(any("x.exe" in s for s in body["skipped"]))
        listing = self.client.get("/prepare/documents").json()
        self.assertEqual(len(listing["documents"]), 2)
        self.assertEqual(listing["limit_bytes"], settings.max_preparation_bytes)
        self.assertGreater(listing["used_bytes"], 0)
        doc = next(d for d in body["documents"] if d["name"] == "a.txt")
        raw = self.client.get(f"/prepare/documents/{doc['id']}/raw")
        self.assertEqual(raw.content, b"Hello.\n")
        self.assertIn("a.txt", raw.headers["content-disposition"])
        self.assertEqual(self.client.get(f"/prepare/documents/{doc['id']}/prepared").status_code, 409)
        self.assertEqual(self.client.delete(f"/prepare/documents/{doc['id']}/prepared").status_code, 409)
        used = listing["used_bytes"]
        self.assertEqual(self.client.delete(f"/prepare/documents/{doc['id']}").status_code, 204)
        listing = self.client.get("/prepare/documents").json()
        self.assertEqual(len(listing["documents"]), 1)
        self.assertLess(listing["used_bytes"], used)
        self.assertEqual(self.client.delete(f"/prepare/documents/{doc['id']}").status_code, 404)
        self.assertEqual(self.client.get("/prepare/documents/zzz").status_code, 404)

    def test_upload_refusals_keep_nothing(self):
        self.assertEqual(self.upload([("x.exe", b"no")]).json()["error"]["code"], "NO_SUPPORTED_FILES")
        damaged = self.zip_bytes({"a.txt": "hello"})[:-30]
        answer = self.upload([("d.zip", damaged)])
        self.assertEqual(answer.json()["error"]["code"], "ARCHIVE_UNREADABLE")
        with patch.object(settings, "max_preparation_bytes", 1024 * 1024):
            answer = self.upload([("big.txt", b"x" * (1024 * 1024 + 1))])
        self.assertEqual(answer.status_code, 409)
        self.assertEqual(answer.json()["error"]["code"], "PREPARATION_STORAGE_FULL")
        self.assertEqual(self.client.get("/prepare/documents").json()["documents"], [])
        self.assertEqual(list((self.root / "uploads").glob("prepare-*")), [])

    def test_job_routes_refusals(self):
        self.assertEqual(self.client.post("/prepare/jobs", json={}).status_code, 422)
        self.assertEqual(self.client.post("/prepare/jobs", json={"document_ids": []}).json()["error"]["code"],
                         "NO_DOCUMENTS")
        self.assertEqual(self.client.post("/prepare/jobs", json={"document_ids": ["aaaaaaaaaaaa"]}).status_code, 404)
        self.assertEqual(self.client.get("/prepare/job/nope").status_code, 404)
        self.assertEqual(self.client.post("/prepare/job/nope/cancel").status_code, 404)
        self.assertEqual(self.client.get("/prepare/jobs").json(), {"jobs": []})

    def test_import_refusals(self):
        doc = self.upload([("a.txt", b"Hello.\n")]).json()["documents"][0]
        answer = self.client.post("/prepare/import", json={"collection": "Docs", "document_ids": [doc["id"]]})
        self.assertEqual(answer.json()["error"]["code"], "DOCUMENT_NOT_PREPARED")
        answer = self.client.post("/prepare/import", json={"collection": "Docs", "document_ids": []})
        self.assertEqual(answer.json()["error"]["code"], "NO_DOCUMENTS")
        answer = self.client.post("/prepare/import", json={"collection": "Docs", "document_ids": ["bbbbbbbbbbbb"]})
        self.assertEqual(answer.status_code, 404)
        answer = self.client.post("/prepare/import", json={"collection": "Docs", "all": True,
                                                            "chunk_size": 100, "chunk_overlap": 100})
        self.assertEqual(answer.json()["error"]["code"], "INVALID_PARAMETER")

    def prepared(self, files):
        docs = self.upload(files).json()["documents"]
        job = asyncio.run(self.run_job([d["id"] for d in docs]))
        self.assertEqual(job["status"], "completed", job)
        return docs

    def test_import_starts_one_ingest_job_with_the_part_names(self):
        docs = self.prepared([("b.zip", self.zip_bytes({"dir/one.txt": "One.\n", "two.csv": "a,b\n1,2\n"}))])
        seen = {}

        async def fake_start(files, collection, **kwargs):
            seen["files"] = [(f.filename, f.file.read()) for f in files]
            seen["names"] = kwargs["names"]
            seen["collection"] = collection
            return "ing00001"
        with patch.object(jobs.wc, "collection_exists", AsyncMock(return_value=False)):
            answer = self.client.post("/prepare/import", json={"collection": "Docs", "all": True})
        self.assertEqual(answer.json()["error"]["code"], "COLLECTION_NOT_FOUND")
        with patch.object(jobs.wc, "collection_exists", AsyncMock(return_value=True)), \
                patch.object(jobs.ingest_pipeline, "start_ingest_job", fake_start):
            answer = self.client.post("/prepare/import", json={"collection": "docs", "all": True})
        self.assertEqual(answer.status_code, 202, answer.text)
        self.assertEqual(answer.json()["job_id"], "ing00001")
        self.assertEqual(seen["collection"], "Docs")
        self.assertEqual(sorted(seen["names"]), ["dir/one.md", "two.csv"])
        self.assertIn(("two.csv", b"a,b\n1,2\n"), seen["files"])
        for doc in docs:
            record = self.client.get(f"/prepare/documents/{doc['id']}").json()
            self.assertEqual(record["imports"][0]["collection"], "Docs")
            self.assertEqual(record["imports"][0]["job_id"], "ing00001")
            self.assertFalse(store.is_busy(doc["id"]))
            self.assertTrue(record["parts"][0]["preview"])

    def test_duplicate_part_names_are_refused(self):
        self.prepared([("a.txt", b"One.\n"), ("a.md", b"Two.\n")])
        with patch.object(jobs.wc, "collection_exists", AsyncMock(return_value=True)):
            answer = self.client.post("/prepare/import", json={"collection": "Docs", "all": True})
            self.assertEqual(answer.json()["error"]["code"], "DUPLICATE_NAMES")
            # The size is checked after the names (#640).
            with patch.object(jobs, "MAX_IMPORT_BYTES", 0):
                answer = self.client.post("/prepare/import", json={"collection": "Docs", "all": True})
            self.assertEqual(answer.json()["error"]["code"], "DUPLICATE_NAMES")

    def test_an_import_over_the_cap_is_refused_before_anything_is_staged(self):
        # #640: one import's parts hold at most one upload's 512 MB, checked with
        # the selection, before the collection, and nothing is staged.
        docs = self.prepared([("one.txt", b"One.\n"), ("two.csv", b"a,b\n1,2\n")])
        ids = [d["id"] for d in docs]
        total = sum(part["bytes"] for i in ids for part in store.get(i)["parts"])
        exists = AsyncMock(return_value=True)
        start = AsyncMock(return_value="ing00009")
        busy = []
        real_mark = store.mark_busy
        with patch.object(jobs.wc, "collection_exists", exists), \
                patch.object(jobs.ingest_pipeline, "start_ingest_job", start), \
                patch.object(jobs.store, "mark_busy", lambda *a: busy.append(a) or real_mark(*a)), \
                patch.object(jobs, "MAX_IMPORT_BYTES", total - 1):
            for body in ({"all": True}, {"document_ids": ids}):
                with self.subTest(body=body):
                    answer = self.client.post("/prepare/import", json={"collection": "Docs", **body})
                    self.assertEqual(answer.status_code, 409, answer.text)
                    error = answer.json()["error"]
                    self.assertEqual(error["code"], "IMPORT_TOO_LARGE")
                    self.assertEqual(error["detail"], {"selected_bytes": total, "limit_bytes": total - 1})
                    self.assertIn("Import them in smaller selections.", error["message"])
            exists.assert_not_called()
            start.assert_not_called()
            self.assertEqual(busy, [])
            for i in ids:
                self.assertEqual(store.get(i)["imports"], [])
                self.assertFalse(store.is_busy(i))
            with patch.object(jobs, "MAX_IMPORT_BYTES", total):
                answer = self.client.post("/prepare/import", json={"collection": "Docs", "all": True})
            self.assertEqual(answer.status_code, 202, answer.text)
            start.assert_called_once()

    def test_the_import_cap_is_512_mb_and_its_message(self):
        self.assertEqual(jobs.MAX_IMPORT_BYTES, 536_870_912)
        self.assertEqual(
            jobs._import_too_large(536_870_913).message,
            "The selected documents' prepared parts total 512.1 MB; one import can be at most "
            "512 MB, the same as one upload. Import them in smaller selections.")
        self.assertIn("total 20,480.0 MB;", jobs._import_too_large(20 * 1024 ** 3).message)

    def test_prepared_download_is_one_file_or_a_zip_of_parts(self):
        [single] = self.prepared([("s.csv", b"a\n1\n")])
        answer = self.client.get(f"/prepare/documents/{single['id']}/prepared")
        self.assertEqual(answer.content, b"a\n1\n")
        rows = "".join(f"{i}\n" for i in range(400))
        with patch.object(preparation, "PART_LIMIT", 500):
            [multi] = self.prepared([("m.csv", ("n\n" + rows).encode())])
        answer = self.client.get(f"/prepare/documents/{multi['id']}/prepared")
        self.assertIn("m-prepared.zip", answer.headers["content-disposition"])
        with zipfile.ZipFile(io.BytesIO(answer.content)) as z:
            names = z.namelist()
            self.assertGreater(len(names), 1)
            self.assertEqual(names[0], "m-part-001.csv")
            self.assertEqual("".join(z.read(n).decode()[2:] for n in names), rows)
        self.assertEqual(self.client.delete(f"/prepare/documents/{multi['id']}/prepared").status_code, 204)
        self.assertEqual(self.client.get(f"/prepare/documents/{multi['id']}").json()["status"], "raw")

    def test_roles(self):
        anonymous = TestClient(app)
        self.assertEqual(anonymous.get("/prepare/documents").status_code, 401)
        end_user = TestClient(app, headers=auth_testing.headers("end_user"))
        self.assertEqual(end_user.get("/prepare/documents").status_code, 403)


class WiringTests(unittest.TestCase):
    ROUTES = [("POST", "/prepare/upload"), ("GET", "/prepare/documents"),
              ("GET", "/prepare/documents/{document_id}"),
              ("GET", "/prepare/documents/{document_id}/raw"),
              ("GET", "/prepare/documents/{document_id}/prepared"),
              ("DELETE", "/prepare/documents/{document_id}"),
              ("DELETE", "/prepare/documents/{document_id}/prepared"),
              ("POST", "/prepare/jobs"), ("GET", "/prepare/jobs"), ("GET", "/prepare/job/{job_id}"),
              ("POST", "/prepare/job/{job_id}/cancel"), ("POST", "/prepare/import")]

    def test_every_route_is_developer_and_the_acting_ones_audited(self):
        for key in self.ROUTES:
            self.assertEqual(policy.ACCESS[key], "developer", key)
        self.assertEqual({k: v for k, v in audit.ACTIONS.items() if k[1].startswith("/prepare")}, {
            ("POST", "/prepare/upload"): "prepare.upload",
            ("POST", "/prepare/jobs"): "prepare.start",
            ("POST", "/prepare/job/{job_id}/cancel"): "prepare.cancel",
            ("POST", "/prepare/import"): "prepare.import",
            ("GET", "/prepare/documents/{document_id}/raw"): "prepare.download",
            ("GET", "/prepare/documents/{document_id}/prepared"): "prepare.download",
            ("DELETE", "/prepare/documents/{document_id}"): "prepare.delete",
            ("DELETE", "/prepare/documents/{document_id}/prepared"): "prepare.delete"})

    def test_the_audit_page_lists_the_new_actions(self):
        page = (ROOT / "ui/src/pages/AuditPage.tsx").read_text()
        for action in ("prepare.upload", "prepare.start", "prepare.cancel", "prepare.import",
                       "prepare.download", "prepare.delete"):
            self.assertIn(f"'{action}'", page)


# ── reviewer's additions (#638 testing review) ──────────────────────────────

class ReviewerTests(JobCase):
    """Considerations of the specification that the tests above don't reach."""

    def setUp(self):
        super().setUp()
        self.client = TestClient(app, headers=auth_testing.headers("developer"))

    def test_every_prepare_route_is_in_the_telemetry_route_list(self):
        # S2, U12: a route missing from the vocabulary is labelled "unmatched".
        from services import telemetry
        routes = [f"{m} {p}" for m, p in policy.ACCESS if p.startswith("/prepare/")]
        self.assertEqual(len(routes), 12)
        self.assertEqual(sorted(r for r in routes if r not in telemetry.ROUTES), [])

    def test_a_preparation_cap_under_1_mib_refuses_to_start(self):
        # S1.2: the API refuses to start, naming the setting.
        from config import Settings
        with self.assertRaisesRegex(ValueError, "MAX_PREPARATION_BYTES"):
            Settings(max_preparation_bytes=1024 * 1024 - 1)
        self.assertEqual(Settings(max_preparation_bytes=1024 * 1024).max_preparation_bytes, 1024 * 1024)

    def test_cancelling_a_queued_job_ends_it_at_once_and_its_documents_stay_raw(self):
        # S4.2.2: a queued job ends cancelled at once; its documents return to raw,
        # and the worker never prepares them afterwards.
        async def scenario():
            gate = asyncio.Event()

            async def slow(system, user):
                await gate.wait()
                return '{"remove": []}'
            self.llm.clean = slow
            a = self.document("a.md", "A text.\n")
            b = self.document("b.md", "B text.\n")
            first = await jobs.start([a["id"]], OWNER)
            second = await jobs.start([b["id"]], OWNER)
            self.assertEqual(store.get(b["id"])["status"], "queued")
            answer = await jobs.cancel(second["job_id"])
            self.assertEqual(answer["status"], "cancelled")
            self.assertEqual(jobs.get_job(second["job_id"])["status"], "cancelled")
            self.assertEqual(store.get(b["id"])["status"], "raw")
            self.assertFalse(store.is_busy(b["id"]))
            gate.set()
            await self.finish(first["job_id"])
            for _ in range(20):
                await asyncio.sleep(0.02)
            self.assertEqual(jobs.get_job(second["job_id"])["status"], "cancelled")
            self.assertEqual(store.get(b["id"])["status"], "raw")
            self.assertEqual(store.get(a["id"])["status"], "prepared")
        asyncio.run(scenario())

    def test_busy_documents_are_refused_by_both_deletes_and_a_job_with_409(self):
        # S4.1.1, S5.3: the routes answer DOCUMENT_BUSY, naming the document.
        doc = self.upload_one("busy.txt", b"Busy.\n")
        store.mark_busy([doc["id"]], "import")
        try:
            for answer in (self.client.delete(f"/prepare/documents/{doc['id']}"),
                           self.client.delete(f"/prepare/documents/{doc['id']}/prepared"),
                           self.client.post("/prepare/jobs", json={"document_ids": [doc["id"]]})):
                self.assertEqual(answer.status_code, 409, answer.text)
                self.assertEqual(answer.json()["error"]["code"], "DOCUMENT_BUSY")
                self.assertIn("busy.txt", answer.json()["error"]["message"])
        finally:
            store.release([doc["id"]])
        self.assertEqual(store.get(doc["id"])["status"], "raw")
        self.assertEqual(self.client.delete(f"/prepare/documents/{doc['id']}").status_code, 204)

    def test_a_zip_is_counted_as_expanded_against_the_limit(self):
        # S1.4: a ZIP's members count as expanded; over the limit nothing is kept.
        import random
        rng = random.Random(638)
        member = "".join(rng.choice("0123456789abcdef") for _ in range(1536 * 1024))
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("big.txt", member)
        data = buf.getvalue()
        limit = 1024 * 1024 + 1
        self.assertLess(len(data), limit - store.usage())     # the ZIP itself would fit
        with patch.object(settings, "max_preparation_bytes", limit):
            answer = self.client.post("/prepare/upload", files=[("files", ("b.zip", data))])
        self.assertEqual(answer.status_code, 409, answer.text)
        self.assertEqual(answer.json()["error"]["code"], "PREPARATION_STORAGE_FULL")
        self.assertEqual(self.client.get("/prepare/documents").json()["documents"], [])

    def test_raw_download_types(self):
        # S5.4: application/gzip for a gzipped file, the name's last segment.
        gz = self.upload_one("logs.jsonl.gz", gzip.compress(b'{"a": 1}\n'))
        answer = self.client.get(f"/prepare/documents/{gz['id']}/raw")
        self.assertEqual(answer.headers["content-type"].split(";")[0], "application/gzip")
        self.assertIn("logs.jsonl.gz", answer.headers["content-disposition"])
        self.assertEqual(gzip.decompress(answer.content), b'{"a": 1}\n')

    def test_prepare_all_takes_every_raw_prepared_and_failed_document_oldest_first(self):
        # S4.1.1, S4.1.4: "all" is every raw, prepared and failed document, by created_at.
        ids = [self.upload_one(name, b"Text.\n")["id"] for name in ("one.md", "two.md", "three.md")]
        self.assertEqual(self.client.post("/prepare/jobs", json={"document_ids": ids, "all": True}).status_code, 422)

        async def scenario():
            answer = await jobs.start(None, OWNER)
            self.assertEqual(answer["documents_queued"], 3)
            return await self.finish(answer["job_id"])
        job = asyncio.run(scenario())
        self.assertEqual(job["status"], "completed")
        created = {d["id"]: d["created_at"] for d in self.client.get("/prepare/documents").json()["documents"]}
        self.assertEqual(job["document_ids"], sorted(ids, key=lambda i: created[i]))

    def test_pdf_and_docx_elements_are_joined_by_a_blank_line_into_markdown(self):
        # S4.3.1, S4.5: PDF and DOCX through ingest's parser (stubbed here: the real
        # one downloads NLTK data, and these tests run with no network), each
        # element's text separated by a blank line, written as .md.
        class Element:
            def __init__(self, text):
                self.text = text

            def __str__(self):
                return self.text
        elements = [Element("Overtime Approval. Requests go to the manager."), Element("  "),
                    Element("Approval is granted by the department head.")]
        def parse(path):
            if Path(path).suffix == ".md":                   # the written part, as AreaCase reads it
                return Path(path).read_text(encoding="utf-8"), []
            return "", elements
        with patch.object(ingest_pipeline, "_parse_file", parse):
            for name in ("policies.pdf", "policies.docx"):
                record = self.document(name, b"stub")
                [part] = self.prepare(record).parts
                self.assertEqual(part["name"], "policies.md")
                self.assertEqual(self.part_text(record["id"], part),
                                 "# Committee minutes\n\nSummary (generated): Items the committee carried "
                                 "forward.\n\nOvertime Approval. Requests go to the manager.\n\n"
                                 "Approval is granted by the department head.\n")

    def upload_one(self, name, data):
        answer = self.client.post("/prepare/upload", files=[("files", (name, data))])
        self.assertEqual(answer.status_code, 201, answer.text)
        return answer.json()["documents"][0]



# ── testing review of 183ae0f: .txt and .md prepared part by part ──────────

class StreamingReviewTests(JobCase):
    """The streaming rewrite of _prepare_text (183ae0f): the window count, the two
    passes, a file that changes between them, cancelling while counting, progress
    and memory on the LLM path, and PDF/DOCX parsed once."""

    def test_count_windows_matches_the_windows_built(self):
        import random
        w = preparation.WINDOW_CHARACTERS
        texts = ["", "\n", "\n\n", "a", "a\n", "\na", "x" * (w - 1), "x" * w, "x" * (w + 5),
                 "x" * (w - 1) + "\n" + "y", "a\n" + "x" * w + "\nb", "x" * w + "\n" + "x" * w,
                 "\n".join("z" * 999 for _ in range(7)), "\n".join("q" * 1499 for _ in range(4)),
                 "short\n" + "y" * 3500 + "\nafter"]
        rng = random.Random(183)
        for _ in range(300):
            lines = [rng.choice("abc") * rng.choice([0, 1, 50, 998, 1499, 1500, 2998, 2999, 3000, 4000])
                     for _ in range(rng.randint(0, 12))]
            texts.append("\n".join(lines) + rng.choice(["", "\n"]))
        for text in texts:
            self.assertEqual(preparation.count_windows(text), len(preparation.windows(text)),
                             f"lines {[len(l) for l in text.split(chr(10))]}")
            self.assertEqual(list(preparation._lines(text)), text.split("\n"))

    def changing_iter_parts(self, first, second):
        """iter_parts that yields `first` parts on its first call, `second` after."""
        calls = [0]

        def fake(path, raw_type):
            calls[0] += 1
            count = first if calls[0] == 1 else second
            for i in range(count):
                yield preparation.Part(body=f"Part {i} text.\n")
        return fake, calls

    def test_a_text_file_that_changes_between_the_passes_fails_and_keeps_nothing(self):
        # Data mutation: the count pass and the writing pass disagree.
        for first, second in ((3, 2), (2, 3)):
            fake, calls = self.changing_iter_parts(first, second)
            record = self.document(f"moving-{first}{second}.txt", "Part text.\n")
            with patch.object(preparation, "iter_parts", fake):
                with self.assertRaisesRegex(ValueError, "changed while it was being prepared"):
                    self.prepare(record)
            self.assertEqual(calls[0], 2)
            # Through a job: a first run fails, nothing prepared is left behind.
            with patch.object(preparation, "iter_parts", self.changing_iter_parts(first, second)[0]):
                job = asyncio.run(self.run_job([record["id"]]))
            self.assertEqual(job["status"], "failed", job)
            after = store.get(record["id"])
            self.assertEqual(after["status"], "failed")
            self.assertIn("changed while it was being prepared", after["last_error"])
            self.assertEqual(after["parts"], [])
            folder = Path(settings.preparation_dir) / "docs" / record["id"]
            self.assertFalse((folder / "prepared.new").exists())
            self.assertFalse(store.is_busy(record["id"]))

    def test_a_changed_file_keeps_the_earlier_prepared_output(self):
        record = self.document("kept.md", "Kept text.\n")
        job = asyncio.run(self.run_job([record["id"]]))
        self.assertEqual(job["status"], "completed")
        before = store.get(record["id"])
        with patch.object(preparation, "iter_parts", self.changing_iter_parts(2, 1)[0]):
            job = asyncio.run(self.run_job([record["id"]]))
        after = store.get(record["id"])
        self.assertEqual(after["status"], "prepared")
        self.assertEqual(after["parts"], before["parts"])
        self.assertIn("changed while it was being prepared", after["last_error"])

    def test_cancelling_while_windows_are_counted_writes_nothing(self):
        stops = [0]

        def should_stop():
            stops[0] += 1
            return stops[0] >= 3         # the check before the text, then during the count
        written = []

        async def write_part(index, suffix, data):
            written.append(index)
            return self.write(f"c{index}{suffix}", data)
        paragraphs = "\n\n".join(f"Paragraph {i} " + "word " * 40 for i in range(200))
        with patch.object(preparation, "PART_LIMIT", 4000), patch.object(preparation, "HEADER_ROOM", 500):
            record = self.document("cancel.txt", paragraphs)
            with self.assertRaises(preparation.Cancelled):
                self.prepare(record, should_stop=should_stop, write_part=write_part)
        self.assertEqual(written, [])

    def test_progress_counts_every_window_once_on_both_paths(self):
        paragraphs = "\n\n".join(f"Paragraph {i} " + "word " * 40 for i in range(200))
        for max_windows in (1_000, 2):          # the LLM path, then too large for it
            seen = []
            with patch.object(preparation, "PART_LIMIT", 4000), \
                    patch.object(preparation, "HEADER_ROOM", 500), \
                    patch.object(preparation, "MAX_WINDOWS", max_windows):
                record = self.document(f"progress{max_windows}.txt", paragraphs)
                llm = StubLLM()
                result = asyncio.run(preparation.prepare_document(
                    store.raw_path(record["id"]), record["name"], record["raw_type"], llm.chat,
                    llm.models, lambda: False, self._writer(record), lambda d, t: seen.append((d, t))))
                expected = sum(len(preparation.windows(p.body))
                               for p in preparation.iter_parts(store.raw_path(record["id"]), ".txt"))
            self.assertGreater(len(result.parts), 1)
            totals = {t for _, t in seen}
            self.assertEqual(len(totals), 1, totals)
            total = totals.pop()
            self.assertEqual(total, expected)
            done = [d for d, _ in seen]
            self.assertEqual(done, sorted(done))
            self.assertEqual((done[0], done[-1]), (0, total))
            cleaning = [s for s, _ in llm.calls if s.startswith(preparation.CLEAN_SYSTEM)]
            self.assertEqual(len(cleaning), total if max_windows == 1_000 else 0)

    def _writer(self, record):
        folder = self.root / "out" / record["id"]
        folder.mkdir(parents=True, exist_ok=True)

        async def write(index, suffix, data):
            path = folder / f"{index + 1:03d}{suffix}"
            path.write_bytes(data)
            return path
        return write

    def test_the_llm_path_is_bounded_and_keeps_every_line_in_order(self):
        # Bounded memory with the LLM cleaning every window, not only the
        # too-large path the PR's own test takes: a file four times longer
        # needs about the same peak, and far less than its size.
        import tracemalloc
        cleaned = [0]

        class LeanLLM:
            """Keeps no prompts, so the measure is the pipeline's memory."""
            async def chat(self, system, user):
                if system.startswith(preparation.CLEAN_SYSTEM):
                    cleaned[0] += 1
                    return '{"remove": []}'
                return '{"title": "Long text", "summary": "Numbered paragraphs."}'

            async def models(self):
                return {f"{settings.llm_model}:latest"}

        def run(name, paragraphs):
            path = self.root / "uploads" / name
            with path.open("w", encoding="utf-8") as fh:
                for i in range(paragraphs):
                    fh.write(f"Paragraph {i} " + "word " * 199 + "\n\n")
            record = store.create(name, path, OWNER)
            cleaned[0] = 0
            with patch.object(preparation, "PART_LIMIT", 200_000), \
                    patch.object(preparation, "MAX_WINDOWS", 100_000):
                tracemalloc.start()
                try:
                    result = self.prepare(record, LeanLLM())
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
            return record, result, peak, path.stat().st_size
        _, _, small_peak, small_size = run("short.md", 2000)
        record, result, peak, size = run("long.md", 8000)
        print(f"\n    LLM path peaks: {small_peak:,} B for {small_size:,} B, {peak:,} B for {size:,} B",
              file=sys.stderr)
        self.assertLess(peak, size / 3, f"peak {peak:,} bytes for a {size:,}-byte file")
        self.assertLess(peak, small_peak + small_size / 2,
                        f"peak grew from {small_peak:,} to {peak:,} bytes with a file 4 times longer")
        self.assertGreater(len(result.parts), 30)
        self.assertFalse(result.fallback)
        self.assertGreater(cleaned[0], 2000)
        numbers = []
        for i, part in enumerate(result.parts, 1):
            self.assertEqual(part["name"], f"long-part-{i:03d}.md")
            text = self.part_text(record["id"], part)
            numbers += [int(m) for m in re.findall(r"^Paragraph (\d+) ", text, re.M)]
        self.assertEqual(numbers, list(range(8000)))

    def test_pdf_and_docx_are_parsed_once_and_text_files_read_twice(self):
        class Element:
            def __init__(self, text):
                self.text = text

            def __str__(self):
                return self.text
        parsed = []

        def parse(path):
            if Path(path).suffix == ".md":
                return Path(path).read_text(encoding="utf-8"), []
            parsed.append(Path(path).suffix)
            return "", [Element("Overtime Approval. Requests go to the manager.")]
        real = preparation.iter_parts
        calls = []

        def counting(path, raw_type):
            calls.append(raw_type)
            return real(path, raw_type)
        with patch.object(ingest_pipeline, "_parse_file", parse), \
                patch.object(preparation, "iter_parts", counting):
            for name in ("once.pdf", "once.docx", "twice.txt", "twice.md"):
                self.prepare(self.document(name, b"stub" if name.endswith(("pdf", "docx")) else "Text.\n"))
        self.assertEqual(calls, [".pdf", ".docx", ".txt", ".txt", ".md", ".md"])
        self.assertEqual(sorted(parsed), [".docx", ".pdf"])


# ── testing review of 0fe2620: CSV rows found by csv.reader, bounded ────────

class CsvReviewTests(JobCase):
    """_csv_rows at 0fe2620: rows as ingest's csv.reader finds them, the row and
    header limits at their boundaries, CRLF, memory with no line break, files
    closed on every path, and a refused CSV through a job."""

    def parts_of(self, name, data, limit):
        with patch.object(preparation, "PART_LIMIT", limit):
            record = self.document(name, data)
            result = self.prepare(record)
        return record, [self.part_text(record["id"], p) for p in result.parts], result

    def test_a_row_that_exactly_fills_a_part_beside_the_header_is_kept(self):
        # Spec §(prepare) step 3: only a row *longer* than a part can hold beside
        # the header fails. header (4) + row (96) == PART_LIMIT (100) fits.
        header = "a,b\n"
        row = "1," + "x" * (100 - len(header) - 3) + "\n"
        self.assertEqual(len(header) + len(row), 100)
        record, texts, _ = self.parts_of("fill.csv", header + row, 100)
        self.assertEqual(texts, [header + row])

    def test_a_row_one_longer_than_a_part_holds_is_refused(self):
        header = "a,b\n"
        row = "1," + "x" * (100 - len(header) - 2) + "\n"
        with patch.object(preparation, "PART_LIMIT", 100):
            record = self.document("over.csv", header + row)
            with self.assertRaisesRegex(ValueError, "CSV row longer than"):
                self.prepare(record)

    def test_a_crlf_row_that_fits_once_normalised_is_kept(self):
        # A CRLF row is a character shorter once normalised; a row that fits a part
        # after normalisation (what is written) should be kept.
        header = "a,b\r\n"
        row = "1," + "x" * (100 - 4 - 4) + "\r\n"          # normalised: 95 chars
        record, texts, _ = self.parts_of("crlf.csv", header + row, 100)
        self.assertEqual(texts, ["a,b\n" + row.replace("\r\n", "\n")])

    def test_the_header_limit_is_half_a_part(self):
        ok = "h" * 49 + "\n"                                   # 50 == 100 // 2
        record, texts, _ = self.parts_of("half.csv", ok + "1\n", 100)
        self.assertEqual(texts, [ok + "1\n"])
        with patch.object(preparation, "PART_LIMIT", 100):
            record = self.document("half1.csv", "h" * 50 + "\n1\n")
            with self.assertRaisesRegex(ValueError, "header row is 51 characters or more"):
                self.prepare(record)

    def test_rows_are_the_rows_ingest_reads(self):
        # Differential: for each dialect and quoting case, the prepared rows,
        # read back with csv.reader, are ingest's rows (splitter._csv_units), in
        # order, with nothing lost or joined, and each part under the limit.
        import csv
        import random
        from services import splitter
        rng = random.Random(639)
        cells = ['plain', '5" screen', '"quoted, comma"', '"two\nlines"', '"say ""hi"""',
                 'x' * 40, '', 'naïve', '"a\n\nb"', "it's"]
        for delim in (",", ";", "\t", "|"):
            for newline in ("\n", "\r\n"):
                header = delim.join(["id", "name", "note"]) + newline
                rows = []
                for i in range(300):
                    picked = [str(i), rng.choice(cells), rng.choice(cells)]
                    rows.append(delim.join(c if delim not in c or c.startswith('"') else '"' + c + '"'
                                           for c in picked) + newline)
                data = header + "".join(rows)
                name = f"d{ord(delim)}{len(newline)}.csv"
                record, texts, result = self.parts_of(name, data, 1500)
                path = store.raw_path(record["id"])
                with path.open(encoding=splitter.text_encoding(path), newline="") as stream:
                    dialect = splitter._csv_dialect(stream)
                    _, units = splitter._csv_units(stream, dialect)
                    expected = [u.cells for u in units if u.cells]
                got = []
                for text in texts:
                    self.assertLessEqual(len(text), 1500)
                    parsed = list(csv.reader(io.StringIO(text, newline=""), dialect))
                    got += [r for r in parsed[1:] if r]
                self.assertEqual(len(got), len(expected), name)
                self.assertEqual(got, [[c.replace("\r\n", "\n") for c in r] for r in expected], name)
                self.assertEqual(sum(p["records"] for p in result.parts), len(expected), name)

    def test_one_long_line_is_refused_in_bounded_memory(self):
        # No line break at all: readline is bounded by the row limit, so the peak
        # is flat in the file's size (the encoding check reads 1 MiB blocks, a
        # fixed ~2 MB): compare a 4 MB and a 16 MB file.
        import tracemalloc
        peaks = {}
        for size in (4_000_000, 16_000_000):
            record = self.document(f"flat{size}.csv", "a,b\n1," + "y" * size)
            with patch.object(preparation, "PART_LIMIT", 20_000):
                tracemalloc.start()
                try:
                    with self.assertRaisesRegex(ValueError, "CSV row longer than"):
                        self.prepare(record)
                    _, peaks[size] = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
        self.assertLess(peaks[16_000_000], 16_000_000 / 4, peaks)
        self.assertLess(peaks[16_000_000] - peaks[4_000_000], 1_000_000, peaks)

    def test_the_csv_is_closed_on_every_path(self):
        import gc
        import warnings
        cases = [("ok.csv", "a,b\n1,2\n", None),
                 ("headonly.csv", "a,b\n", None),
                 ("blank.csv", "\n\n\n", None),
                 # An unclosed quote that swallows more than a part is refused while
                 # it is read; one that still fits is refused by the part check's
                 # partition_csv, as ingest refuses it.
                 ("bad.csv", 'a,b\n1,"open\n' + "z,z\n" * 400, "CSV row longer than"),
                 ("unclosed.csv", 'a,b\n1,"open\n' + "z,z\n" * 200, "EOF inside string"),
                 ("field.csv", None, "is not a CSV that can be read")]
        from unstructured.partition.csv import CSV_FIELD_LIMIT
        for name, data, error in cases:
            if data is None:
                data = 'a,b\n1,"' + "f" * (CSV_FIELD_LIMIT + 10) + '"\n'
            limit = 4 * CSV_FIELD_LIMIT if name == "field.csv" else 1000
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always", ResourceWarning)
                with patch.object(preparation, "PART_LIMIT", limit):
                    record = self.document(name, data)
                    if error:
                        with self.assertRaisesRegex(ValueError, error):
                            self.prepare(record)
                    else:
                        try:
                            self.prepare(record)
                        except ValueError:
                            pass
                gc.collect()
            leaks = [str(w.message) for w in caught if issubclass(w.category, ResourceWarning)]
            self.assertEqual(leaks, [], name)

    def test_a_refused_csv_fails_its_job_and_keeps_nothing(self):
        with patch.object(preparation, "PART_LIMIT", 1000):
            record = self.document("job.csv", 'id,note\n1,"never closed\n' + "2,two\n" * 400)
            job = asyncio.run(self.run_job([record["id"]]))
        self.assertEqual(job["status"], "failed", job)
        after = store.get(record["id"])
        self.assertEqual(after["status"], "failed")
        self.assertIn("CSV row longer than", after["last_error"])
        self.assertEqual(after["parts"], [])
        folder = Path(settings.preparation_dir) / "docs" / record["id"]
        self.assertFalse((folder / "prepared.new").exists())
        self.assertFalse(store.is_busy(record["id"]))
        # The other documents of the job still run: a good CSV next to a bad one.
        with patch.object(preparation, "PART_LIMIT", 1000):
            good = self.document("good.csv", "x,y\n1,2\n")
            bad = self.document("bad2.csv", "x,y\n1," + "q" * 5000 + "\n")
            job = asyncio.run(self.run_job([good["id"], bad["id"]]))
        self.assertEqual(job["status"], "partial", job)
        self.assertEqual(store.get(good["id"])["status"], "prepared")
        self.assertEqual(store.get(bad["id"])["status"], "failed")


if __name__ == "__main__":
    unittest.main()
