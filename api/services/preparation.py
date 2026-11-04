"""Preparing one raw document (#638): code does the rule work, the LLM cleans.

SPECIFICATIONS.md §3.1.11. In order, for one document:

1. **Extract** the text with ingest's own readers, so a file ingest would refuse
   fails here with ingest's reason, and **normalise** it (UTF-8, LF, NFC, no
   control characters; JSON and JSON Lines keep their records unchanged).
2. **Split** it into parts of at most PART_LIMIT characters as ingest will read
   them, at ingest's boundaries: paragraphs, PDF/DOCX elements, CSV rows with
   the header repeated, JSON Lines records (a record is never cut).
3. **Clean** each PDF, DOCX, TXT or MD part with the LLM: it is shown windows of
   numbered lines and answers which lines are headers, footers, page numbers or
   boilerplate. Code removes them; the LLM never rewrites text. A window whose
   answer fails the fidelity check keeps its code-only text, and the fallback
   is recorded with its reason.
4. Give every part a **title and summary** (in the text for the four text
   types, in the record only for CSV and JSON Lines).
5. **Check** each written part as ingest reads it, against the rules that don't
   depend on chunk settings.

The LLM and the part writer are passed in, so tests use a stub and the job
decides where parts go and whether there is room (a best-effort cap).
"""
from __future__ import annotations

import asyncio
import codecs
import gzip
import json
import logging
import math
import os
import re
import tempfile
import unicodedata
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator

import httpx

from config import settings
from services import ingest_pipeline as ip
from services import splitter
from services.chunker import MAX_CHUNKS_PER_FILE, MAX_TEXT_CHARACTERS

_log = logging.getLogger(__name__)

# The corpus rules' targets for the default `overlap` 1,000/200 (#615): at most
# 7,000,000 characters and 8,000 chunks, which is about 6,400,000 characters.
PART_LIMIT = 6_400_000
# Room kept in a PDF/DOCX/TXT/MD part for the title and summary header.
HEADER_ROOM = 2_000
WINDOW_CHARACTERS = 3_000
MAX_REMOVED_LINE = 200
MAX_REMOVED_SHARE = 0.20
MAX_WINDOWS = 1_000
TITLE_INPUT = 6_000
MAX_TITLE = 150
MAX_SUMMARY_WORDS = 80
CALL_SECONDS = 120.0
FAILED_CALLS_IN_A_ROW = 3

CLEANING_TYPES = (".pdf", ".docx", ".txt", ".md")

CLEAN_SYSTEM = (
    "You clean text extracted from a document. You are given numbered lines. Find the lines "
    "that are page headers, page footers, page numbers, or repeated boilerplate (such as "
    "copyright notices, navigation text or \"continued on next page\"), and nothing else. "
    "Never choose a line that holds the document's own content. Return only a JSON object: "
    "{\"remove\": [line numbers]}. Return {\"remove\": []} when there are none."
)
CLEAN_RETRY = (
    " Your previous response was not a JSON object with a \"remove\" list of line numbers "
    "from the lines given. Return ONLY that JSON object."
)
TITLE_SYSTEM = (
    "You write a title and a short summary for a part of a document. Use only the text "
    "given. The title is at most 15 words. The summary is at most 60 words. Return only a "
    "JSON object: {\"title\": \"...\", \"summary\": \"...\"}."
)
TITLE_RETRY = (
    " Your previous response was not a JSON object with a nonempty \"title\" of one line and "
    "a nonempty \"summary\" of at most 60 words. Return ONLY that JSON object."
)


class Cancelled(Exception):
    """The job was cancelled; the document goes back to its earlier state."""


class StorageRefused(ValueError):
    """The part writer refused a part: the area would pass its limit."""


# ── normalisation ────────────────────────────────────────────────────────────

_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def normalise_text(text: str) -> str:
    """UTF-8 text without a byte-order mark, LF line endings, NFC, and no NUL or
    other C0 control character except tab and newline, nor DEL."""
    if text.startswith("﻿"):
        text = text[1:]
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    return _CONTROL.sub("", text)


# ── output kinds and names ───────────────────────────────────────────────────

def output_kind(raw_type: str) -> str:
    """`text` (.md output), `csv` or `jsonl`."""
    if raw_type in CLEANING_TYPES:
        return "text"
    if raw_type == ".csv":
        return "csv"
    return "jsonl"


SUFFIX = {"text": ".md", "csv": ".csv", "jsonl": ".jsonl"}


def stem_of(name: str) -> str:
    lower = name.lower()
    for double in (".jsonl.gz", ".json.gz"):
        if lower.endswith(double):
            return name[:-len(double)]
    suffix = Path(name).suffix
    return name[:-len(suffix)] if suffix else name


def part_names(name: str, kind: str, count: int) -> list[str]:
    """The names parts are imported under: the raw name with the output suffix,
    folders kept; with several parts, `<stem>-part-001<suffix>` and on."""
    stem, suffix = stem_of(name), SUFFIX[kind]
    if count == 1:
        return [stem + suffix]
    return [f"{stem}-part-{i:03d}{suffix}" for i in range(1, count + 1)]


# ── extraction and parts ─────────────────────────────────────────────────────

@dataclass
class Part:
    """A part's code-only content. `body` is what the file holds (before any
    cleaning and header); `measure` is its text as ingest reads it, for the
    title and summary of CSV and JSON Lines; `records` counts CSV rows or JSON
    Lines records."""
    body: str
    measure: str = ""
    records: int | None = None
    header: str = ""          # a CSV part's header row


def _joined_parts(units: Iterator[str], limit: int) -> Iterator[str]:
    """Units joined by a blank line into parts of at most `limit` characters; a
    unit over the limit is cut at line breaks, then whitespace, then hard."""
    current: list[str] = []
    size = 0
    for unit in units:
        if not unit.strip():
            continue
        pieces = [unit] if len(unit) <= limit else splitter._cut_text(unit, limit)
        for piece in pieces:
            added = len(piece) + (2 if current else 0)
            if current and size + added > limit:
                yield "\n\n".join(current)
                current, size = [], 0
                added = len(piece)
            current.append(piece)
            size += added
    if current:
        yield "\n\n".join(current)


def _text_units(path: Path, markdown: bool) -> Iterator[str]:
    encoding = splitter.text_encoding(path)
    with path.open(encoding=encoding, newline="") as stream:
        for unit in splitter._text_units(stream, markdown, PART_LIMIT):
            yield normalise_text(unit.text)


def _element_units(path: Path) -> Iterator[str]:
    _text, elements = ip._parse_file(path)
    for element in elements:
        text = normalise_text(str(element))
        if text.strip():
            yield text


def _csv_rows(path: Path) -> tuple[str, Iterator[str]]:
    """The header row and the other rows, each exactly as written (normalised),
    ending in a line break. A row ends at a line break outside the dialect's
    quote character, so a quoted field may hold line breaks."""
    encoding = splitter.text_encoding(path)
    with path.open(encoding=encoding, newline="") as probe:
        dialect = splitter._csv_dialect(probe)
    quote = getattr(dialect, "quotechar", '"') or '"'

    def rows() -> Iterator[str]:
        with path.open(encoding=encoding, newline="") as stream:
            pending: list[str] = []
            open_quote = False
            for line in stream:
                pending.append(line)
                if line.count(quote) % 2:
                    open_quote = not open_quote
                if not open_quote:
                    row = normalise_text("".join(pending))
                    pending = []
                    if not row.endswith("\n"):
                        row += "\n"
                    if row.strip():
                        yield row
            if pending:
                row = normalise_text("".join(pending))
                if not row.endswith("\n"):
                    row += "\n"
                if row.strip():
                    yield row

    iterator = rows()
    header = next(iterator, "")
    return header, iterator


def _csv_parts(path: Path) -> Iterator[Part]:
    header, rows = _csv_rows(path)
    if not header:
        return
    if 2 * len(header) > PART_LIMIT:
        raise ValueError(
            f"its header row is {len(header):,} characters, more than half of what one part "
            f"can hold ({PART_LIMIT:,} characters); the header is repeated in every part, so "
            f"shorten the header or split the file")
    room = PART_LIMIT - len(header)
    current: list[str] = []
    size = 0
    for row in rows:
        if current and size + len(row) > room:
            body = header + "".join(current)
            yield Part(body=body, measure=body, records=len(current), header=header)
            current, size = [], 0
        current.append(row)
        size += len(row)
    body = header + "".join(current)
    yield Part(body=body, measure=body, records=len(current), header=header)


def _compact_json(value: Any) -> str:
    """JSON with no spaces, keys in order, non-ASCII as is, numbers as written."""
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(str(k), ensure_ascii=False) + ":" + _compact_json(v)
                              for k, v in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_compact_json(v) for v in value) + "]"
    if isinstance(value, ip._JsonNumber):
        return value.text
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)


def _json_records(path: Path) -> Iterator[tuple[str, str]]:
    """A .json file's (or decompressed .json.gz member's) records as (line, text):
    each record written as compact JSON, and its text as ingest converts it."""
    if path.stat().st_size > ip.MAX_JSON_INPUT_BYTES:
        raise ValueError(ip._json_input_too_large("is larger than"))
    try:
        raw, value = ip._read_json(path)
    except ip._JsonError as exc:
        raise ValueError(str(exc)) from exc
    if not raw:
        return
    raw = None
    groups = ip._json_groups(value)
    if len(groups) > ip.MAX_JSON_RECORDS:
        raise ValueError(ip._too_many_records())
    for position, group in enumerate(groups, 1):
        lines: list[str] = []
        try:
            ip._json_lines(group, None, lines, ip._JsonBudget())
        except ip._JsonTooLarge as exc:
            raise ValueError(ip._record_too_large(f"record {position}")) from exc
        except RecursionError as exc:
            raise ValueError("is not valid JSON: it is nested too deeply to read") from exc
        yield _compact_json(group), "\n".join(lines)


def _json_gz_records(path: Path) -> Iterator[tuple[str, str]]:
    fd, name = tempfile.mkstemp(dir=settings.upload_dir, prefix="prepare-", suffix=".json")
    os.close(fd)
    plain = Path(name)
    try:
        ip._gunzip_bounded(path, plain)
        try:
            yield from _json_records(plain)
        except ValueError as exc:
            raise ValueError(f"was decompressed, but {exc}") from exc
    finally:
        plain.unlink(missing_ok=True)


def _jsonl_records(path: Path) -> Iterator[tuple[str, str]]:
    """A JSON Lines file's records as (line as written, text), blank lines left out."""
    try:
        if ip._is_jsonl_gz(path.name):
            with gzip.open(path, "rb") as gz:
                for _n, raw, text in ip._iter_jsonl_lines(ip._BoundedLines(gz)):
                    yield raw.decode("utf-8"), text
        else:
            with path.open("rb") as fh:
                for _n, raw, text in ip._iter_jsonl_lines(fh):
                    yield raw.decode("utf-8"), text
    except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
        raise ValueError(
            f"is not a valid gzip file; it may be truncated or corrupt ({exc})") from exc
    except ip._JsonlError as exc:
        raise ValueError(ip._jsonl_reason(path, exc)) from exc


def _record_parts(records: Iterator[tuple[str, str]]) -> Iterator[Part]:
    """JSON Lines parts: records in order, each part's text as ingest reads it
    (records with text joined by a blank line) at most PART_LIMIT characters and
    at most MAX_JSON_RECORDS records. A record over the limit is a part of its own."""
    lines: list[str] = []
    texts: list[str] = []
    size = 0
    for line, text in records:
        added = (len(text) + (2 if texts else 0)) if text.strip() else 0
        if lines and (size + added > PART_LIMIT or len(lines) >= ip.MAX_JSON_RECORDS):
            yield Part(body="\n".join(lines) + "\n", measure="\n\n".join(texts),
                       records=len(lines))
            lines, texts, size = [], [], 0
            added = len(text) if text.strip() else 0
        lines.append(line)
        if text.strip():
            texts.append(text)
        size += added
    if lines:
        yield Part(body="\n".join(lines) + "\n", measure="\n\n".join(texts),
                   records=len(lines))


def iter_parts(path: Path, raw_type: str) -> Iterator[Part]:
    """The document's parts, code-only, in order. Raises ValueError with ingest's
    reason for a file ingest would refuse."""
    if raw_type in (".txt", ".md"):
        for body in _joined_parts(_text_units(path, raw_type == ".md"), PART_LIMIT - HEADER_ROOM):
            yield Part(body=body)
    elif raw_type in (".pdf", ".docx"):
        for body in _joined_parts(_element_units(path), PART_LIMIT - HEADER_ROOM):
            yield Part(body=body)
    elif raw_type == ".csv":
        yield from _csv_parts(path)
    elif raw_type == ".json":
        yield from _record_parts(_json_records(path))
    elif raw_type == ".json.gz":
        yield from _record_parts(_json_gz_records(path))
    elif raw_type in (".jsonl", ".jsonl.gz"):
        yield from _record_parts(_jsonl_records(path))
    else:
        raise ValueError(ip._skip_reason(path.name))


# ── windows ──────────────────────────────────────────────────────────────────

@dataclass
class Window:
    lines: list[str]
    send: bool = True

    @property
    def characters(self) -> int:
        return sum(len(line) + 1 for line in self.lines)


def windows(text: str) -> list[Window]:
    """Whole lines, in order, as many as fit in WINDOW_CHARACTERS; a longer line is
    a window of its own and isn't sent (no line that long can be removed)."""
    out: list[Window] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        if len(line) + 1 > WINDOW_CHARACTERS:
            if current:
                out.append(Window(current))
                current, size = [], 0
            out.append(Window([line], send=False))
            continue
        if current and size + len(line) + 1 > WINDOW_CHARACTERS:
            out.append(Window(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        out.append(Window(current))
    return out


def _lines(text: str) -> Iterator[str]:
    """The lines of `text`, as text.split("\n") gives them, one at a time."""
    start = 0
    while True:
        end = text.find("\n", start)
        if end < 0:
            yield text[start:]
            return
        yield text[start:end]
        start = end + 1


def count_windows(text: str) -> int:
    """len(windows(text)), without building the windows."""
    count = 0
    open_window = False
    size = 0
    for line in _lines(text):
        if len(line) + 1 > WINDOW_CHARACTERS:
            count += 1 + (1 if open_window else 0)
            open_window, size = False, 0
            continue
        if open_window and size + len(line) + 1 > WINDOW_CHARACTERS:
            count += 1
            size = 0
        open_window = True
        size += len(line) + 1
    return count + (1 if open_window else 0)


# ── the LLM ──────────────────────────────────────────────────────────────────

Chat = Callable[[str, str], Awaitable[str]]


class LLM:
    """One document's LLM calls: 120 s each, a failed or timed-out call sent once
    more, and after three failed calls in a row, no more calls (`down`)."""

    def __init__(self, chat: Chat, should_stop: Callable[[], bool],
                 seconds: float = CALL_SECONDS) -> None:
        self.chat = chat
        self.should_stop = should_stop
        self.seconds = seconds
        self.failed_in_a_row = 0
        self.down = False

    async def call(self, system: str, user: str) -> tuple[str, str]:
        """("ok", reply), or ("llm-timeout" | "llm-error" | "llm-unavailable", "")."""
        kinds = []
        for _ in range(2):
            if self.down:
                return "llm-unavailable", ""
            if self.should_stop():
                raise Cancelled()
            try:
                reply = await asyncio.wait_for(self.chat(system, user), self.seconds)
            except (asyncio.TimeoutError, httpx.TimeoutException):
                kinds.append("timeout")
            except asyncio.CancelledError:
                raise
            except Exception as exc:                        # noqa: BLE001
                _log.info("LLM call failed: %s", type(exc).__name__)
                kinds.append("error")
            else:
                self.failed_in_a_row = 0
                return "ok", reply if isinstance(reply, str) else ""
            self.failed_in_a_row += 1
            if self.failed_in_a_row >= FAILED_CALLS_IN_A_ROW:
                self.down = True
        return ("llm-timeout" if all(k == "timeout" for k in kinds) else "llm-error"), ""


def _first_object(reply: str) -> dict | None:
    """The first JSON object in a reply (code fences and text around it allowed)."""
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", reply):
        try:
            value, _end = decoder.raw_decode(reply[match.start():])
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


def _removals(reply: str, count: int) -> list[int] | None:
    """The line numbers to remove, or None when the answer is unusable."""
    obj = _first_object(reply)
    if obj is None or not isinstance(obj.get("remove"), list):
        return None
    numbers = obj["remove"]
    if not all(isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= count
               for n in numbers):
        return None
    return sorted(set(numbers))


@dataclass
class Cleaned:
    lines: list[str]
    reason: str | None = None   # a fallback's reason; None when cleaned


def check_removal(window: Window, numbers: list[int]) -> str | None:
    """The fidelity check: the reason the removal is refused, or None."""
    removed = [window.lines[n - 1] for n in numbers]
    if any(len(line) > MAX_REMOVED_LINE for line in removed):
        return "removed-long-line"
    if sum(len(line) + 1 for line in removed) > MAX_REMOVED_SHARE * window.characters:
        return "removed-too-much"
    return None


async def clean_window(window: Window, llm: LLM) -> Cleaned:
    if not window.send:
        return Cleaned(window.lines)
    user = "Lines:\n" + "\n".join(f"{i}: {line}" for i, line in enumerate(window.lines, 1))
    system = CLEAN_SYSTEM
    for attempt in range(2):
        status, reply = await llm.call(system, user)
        if status != "ok":
            return Cleaned(window.lines, status)
        numbers = _removals(reply, len(window.lines))
        if numbers is not None:
            reason = check_removal(window, numbers)
            if reason:
                return Cleaned(window.lines, reason)
            drop = set(numbers)
            return Cleaned([line for i, line in enumerate(window.lines, 1) if i not in drop])
        system = CLEAN_SYSTEM + CLEAN_RETRY
    return Cleaned(window.lines, "unusable-answer")


def _usable_title(obj: dict | None) -> tuple[str, str] | None:
    if obj is None:
        return None
    title, summary = obj.get("title"), obj.get("summary")
    if not isinstance(title, str) or not isinstance(summary, str):
        return None
    title = normalise_text(title).strip()
    summary = " ".join(normalise_text(summary).split())
    if not title or "\n" in title or len(title) > MAX_TITLE:
        return None
    if not summary or len(summary.split()) > MAX_SUMMARY_WORDS:
        return None
    if len(header_text(title, summary)) > HEADER_ROOM:
        return None
    return title, summary


def header_text(title: str, summary: str) -> str:
    return f"# {title}\n\nSummary (generated): {summary}\n\n"


async def title_summary(text: str, llm: LLM) -> tuple[str | None, str | None, str | None]:
    """(title, summary, None), or (None, None, reason)."""
    user = "Text:\n" + text[:TITLE_INPUT]
    system = TITLE_SYSTEM
    for attempt in range(2):
        status, reply = await llm.call(system, user)
        if status != "ok":
            return None, None, status
        usable = _usable_title(_first_object(reply))
        if usable:
            return usable[0], usable[1], None
        system = TITLE_SYSTEM + TITLE_RETRY
    return None, None, "unusable-answer"


async def model_available(list_models: Callable[[], Awaitable[set[str]]]) -> bool:
    try:
        models = await asyncio.wait_for(list_models(), 15)
    except Exception:                                       # noqa: BLE001
        return False
    wanted = settings.llm_model
    return wanted in models or f"{wanted}:latest" in models or (
        ":" not in wanted and any(m.split(":")[0] == wanted for m in models))


# ── checks on a written part ─────────────────────────────────────────────────

def overlap_estimate(characters: int) -> int:
    """Chunks the default `overlap` 1,000/200 gives at most, as the rules count them."""
    if characters <= 0:
        return 0
    return 1 + math.ceil(max(characters - 1000, 0) / 800)


def check_part(path: Path, kind: str) -> tuple[int, int | None]:
    """Read a written part as ingest reads it; refuse it when it breaks a rule
    that doesn't depend on chunk settings. Returns (characters, records)."""
    records = None
    if kind == "jsonl":
        texts = []
        records = 0
        with path.open("rb") as fh:
            try:
                for _n, _raw, text in ip._iter_jsonl_lines(fh):
                    records += 1
                    if text.strip():
                        texts.append(text)
            except ip._JsonlError as exc:
                raise ValueError(f"a prepared part fails a JSON Lines rule: {exc.detail}") from exc
        characters = sum(len(t) for t in texts) + 2 * max(len(texts) - 1, 0)
    else:
        text, _elements = ip._parse_file(path)
        characters = len(text)
    if characters > MAX_TEXT_CHARACTERS:
        raise ValueError(f"a prepared part has {characters:,} characters of text, more than "
                         f"{MAX_TEXT_CHARACTERS:,}, the most one file can hold")
    if records is not None and records > ip.MAX_JSON_RECORDS:
        raise ValueError(ip._too_many_records())
    chunks = overlap_estimate(characters)
    if chunks > MAX_CHUNKS_PER_FILE:
        raise ValueError(f"a prepared part would give about {chunks:,} chunks with the default "
                         f"settings, more than {MAX_CHUNKS_PER_FILE:,}; a record that large "
                         f"can't be imported")
    return characters, records


# ── one document ─────────────────────────────────────────────────────────────

WritePart = Callable[[int, str, bytes], Awaitable[Path]]
Progress = Callable[[int, int], None]


@dataclass
class Result:
    parts: list[dict] = field(default_factory=list)
    fallback: bool = False


async def prepare_document(raw: Path, name: str, raw_type: str, chat: Chat,
                           list_models: Callable[[], Awaitable[set[str]]],
                           should_stop: Callable[[], bool], write_part: WritePart,
                           progress: Progress = lambda done, total: None) -> Result:
    """Prepare one document. Raises ValueError (its failure reason), Cancelled,
    or StorageRefused from write_part."""
    kind = output_kind(raw_type)
    llm = LLM(chat, should_stop)
    if not await model_available(list_models):
        llm.down = True
    if should_stop():
        raise Cancelled()

    if kind == "text":
        return await _prepare_text(raw, raw_type, name, llm, should_stop, write_part, progress)
    return await _prepare_records(raw, raw_type, name, kind, llm, should_stop, write_part)


def _no_text(name: str) -> ValueError:
    return ValueError("has no text to prepare")


def _count_text_windows(parts: Iterator[Part], should_stop) -> tuple[int, int]:
    """(parts, windows) of a document, one part held at a time."""
    count = total = 0
    for part in parts:
        if should_stop():
            raise Cancelled()
        count += 1
        total += count_windows(part.body)
    return count, total


async def _prepare_text(raw: Path, raw_type: str, name: str, llm: LLM, should_stop,
                        write_part, progress) -> Result:
    """PDF, DOCX, TXT and MD. A .txt or .md is read part by part twice, as ingest
    reads it (#621), so memory is bounded by one part whatever the file's size:
    once to count its windows against MAX_WINDOWS, then to clean and write each
    part, building only that part's windows. A PDF or DOCX is parsed whole, as
    ingest parses it, and its parts kept for both passes. Parts are written under
    their index and named once the count is known."""
    if raw_type in (".pdf", ".docx"):
        held = await asyncio.to_thread(lambda: list(iter_parts(raw, raw_type)))

        def source() -> Iterator[Part]:
            return iter(held)
    else:
        def source() -> Iterator[Part]:
            return iter_parts(raw, raw_type)
    count, total = await asyncio.to_thread(_count_text_windows, source(), should_stop)
    if not count:
        raise _no_text(name)
    too_large = total > MAX_WINDOWS
    done = 0
    progress(done, total)
    result = Result()
    iterator = source()
    index = 0
    while True:
        part = await asyncio.to_thread(next, iterator, None)
        if part is None:
            break
        fallbacks: list[dict] = []
        kept: list[str] = []
        if too_large:
            fallbacks.append({"window": None, "reason": "too-large-for-llm"})
            kept = [part.body]
            done += count_windows(part.body)
            progress(done, total)
        else:
            unavailable_noted = False
            for number, window in enumerate(windows(part.body), 1):
                if should_stop():
                    raise Cancelled()
                if llm.down:
                    if not unavailable_noted and window.send:
                        fallbacks.append({"window": None, "reason": "llm-unavailable"})
                        unavailable_noted = True
                    kept.extend(window.lines)
                else:
                    cleaned = await clean_window(window, llm)
                    if cleaned.reason == "llm-unavailable":
                        if not unavailable_noted:
                            fallbacks.append({"window": None, "reason": "llm-unavailable"})
                            unavailable_noted = True
                    elif cleaned.reason:
                        fallbacks.append({"window": number, "reason": cleaned.reason})
                    kept.extend(cleaned.lines)
                done += 1
                progress(done, total)
        body = "\n".join(kept).strip("\n")
        del kept, part
        title, summary, error = await title_summary(body, llm)
        text = (header_text(title, summary) if title else "") + body + "\n"
        del body
        entry = await _write(index, "", text, "text", write_part)
        del text
        entry.update(title=title, summary=summary, summary_error=error,
                     fallback=bool(fallbacks), fallbacks=fallbacks)
        result.parts.append(entry)
        result.fallback = result.fallback or bool(fallbacks)
        index += 1
    if index != count:
        raise ValueError("changed while it was being prepared; prepare it again")
    for entry, part_name in zip(result.parts, part_names(name, "text", count)):
        entry["name"] = part_name
    return result


async def _write(index: int, part_name: str, text: str, kind: str, write_part) -> dict:
    data = text.encode("utf-8")
    path = await write_part(index, SUFFIX[kind], data)
    characters, records = await asyncio.to_thread(check_part, path, kind)
    entry = {"name": part_name, "file": path.name, "bytes": len(data),
             "characters": characters}
    if records is not None:
        entry["records"] = records
    return entry


async def _prepare_records(raw: Path, raw_type: str, name: str, kind: str, llm: LLM,
                           should_stop, write_part) -> Result:
    """CSV, JSON and JSON Lines: code-only, a part at a time. Their names depend on
    how many parts there are, so parts are written under their index and named
    when the count is known."""
    iterator = iter_parts(raw, raw_type)
    result = Result()
    entries: list[dict] = []
    index = 0
    while True:
        part = await asyncio.to_thread(next, iterator, None)
        if part is None:
            break
        if should_stop():
            raise Cancelled()
        title, summary, error = await title_summary(part.measure or part.body, llm)
        entry = await _write(index, "", part.body, kind, write_part)
        if kind == "csv":
            entry["records"] = part.records
        entry.update(title=title, summary=summary, summary_error=error,
                     fallback=False, fallbacks=[])
        entries.append(entry)
        index += 1
    if not entries:
        raise _no_text(name)
    for entry, part_name in zip(entries, part_names(name, kind, len(entries))):
        entry["name"] = part_name
    result.parts = entries
    return result
