"""Splitting a file over the per-file limits into parts (#621).

A file whose text or chunks would go over the chunker's per-file limits (the
10,000,000-character text limit, the 10,000-chunk cap and, for `overlap`, the
window limits) is not refused: it is split into parts that each stay under them,
and every part is chunked on its own, as a file of its own would be. The caller
stores each part as `<name> (part N)`.

Parts are cut from units, in file order: JSON and JSON Lines at record
boundaries (a record is never split), `.txt` and `.md` at paragraphs (blank
lines; `.md` never inside a fenced code block), a CSV by rows with its header
repeated in every part, `.pdf` and `.docx` between elements. A paragraph,
element or CSV row over a limit on its own is cut at line breaks, then at
whitespace, then hard at the limit, as _cap_chunk_length cuts a chunk.

JSON Lines, `.txt`, `.md` and `.csv` are read part by part, so memory is bounded
by one part whatever the file's size. `.json`, `.pdf` and `.docx` are parsed
whole, as before, and split by record or element. A file that fits in one part
is chunked exactly as before and keeps its name.
"""
from __future__ import annotations

import codecs
import csv
import io
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from services import chunker
from services.chunker import ChunkLimitError

_BLOCK = 1024 * 1024
# How much of a file the encoding is detected from when it isn't UTF-8: reading
# the whole file to detect it would undo the per-part memory bound.
_DETECT_BYTES = 1024 * 1024
_SNIFF_CHARACTERS = 64 * 1024


@dataclass
class Unit:
    """What a part is cut from. `kind` is "record" (never cut) or "text";
    `ordinal` names a record in errors; `element` is a parsed element (its class
    is kept when the unit is cut); `cells` are a CSV row's cells."""
    text: str
    kind: str = "text"
    ordinal: str | None = None
    element: Any = None
    cells: list[str] | None = None


class PartError(Exception):
    """Producing part `number` failed; `cause` is why. The caller names the part."""

    def __init__(self, number: int, cause: BaseException) -> None:
        super().__init__(str(cause) or type(cause).__name__)
        self.number = number
        self.cause = cause


@dataclass
class Settings:
    strategy: str
    chunk_size: int
    chunk_overlap: int
    similarity_threshold: float
    min_chunk_size: int


# ── types ────────────────────────────────────────────────────────────────────

def file_type(path: Path) -> str:
    """jsonl, json, txt, md, csv or elements (`.pdf`, `.docx`)."""
    from services import ingest_pipeline as ip
    if ip._is_jsonl(path.name):
        return "jsonl"
    if ip._is_json_gz(path.name):
        return "json"
    ext = path.suffix.lower()
    if ext == ".json":
        return "json"
    if ext in (".txt", ".md", ".csv"):
        return ext[1:]
    return "elements"


_STREAMED_TEXT = ("txt", "md", "csv")


# ── sizing (S4) ──────────────────────────────────────────────────────────────

def _overlap_windows(length: int, size: int, overlap: int) -> int:
    """How many windows chunk_overlap makes of `length` characters."""
    if length <= 0:
        return 0
    if length <= size:
        return 1
    stride = size - overlap
    return 1 + (length - size + stride - 1) // stride


def part_limits(settings: Settings) -> tuple[int, int | None]:
    """The most text one part may hold, and the most units (`context_aware`), so
    that its estimated chunking stays inside the per-file limits."""
    cap = chunker.MAX_CHUNKS_PER_FILE
    text = chunker.MAX_TEXT_CHARACTERS
    size, overlap = settings.chunk_size, settings.chunk_overlap
    strategy = settings.strategy
    if strategy == "fixed":
        return min(text, cap * size), None
    if strategy == "language":
        return min(text, cap * max(size - overlap, 1)), None
    if strategy == "context_aware":
        # Packing never puts less than a whole unit in a chunk, but a unit longer
        # than a chunk can give several.
        return min(text, cap * size), cap
    if strategy == "overlap":
        def fits(length: int) -> bool:
            windows = _overlap_windows(length, size, overlap)
            return (windows <= chunker.MAX_OVERLAP_WINDOWS
                    and length + max(windows - 1, 0) * overlap
                    <= chunker.MAX_OVERLAP_OUTPUT_CHARACTERS)
        low, high = 0, text
        while low < high:
            middle = (low + high + 1) // 2
            if fits(middle):
                low = middle
            else:
                high = middle - 1
        return max(low, 1), None
    return text, None


def _separator(ftype: str, units_have_elements: bool) -> int:
    """The characters joining two units in a part's text."""
    if ftype == "csv":
        return 0
    if ftype == "elements" or units_have_elements:
        return 1
    return 2


# ── encoding of streamed text (S3.6) ─────────────────────────────────────────

def _decodes(path: Path, encoding: str) -> bool:
    decoder = codecs.getincrementaldecoder(encoding)()
    try:
        with path.open("rb") as fh:
            while True:
                block = fh.read(_BLOCK)
                if not block:
                    break
                decoder.decode(block)
        decoder.decode(b"", final=True)
    except (UnicodeDecodeError, UnicodeError):
        return False
    return True


def text_encoding(path: Path) -> str:
    """The encoding a streamed `.txt`, `.md` or `.csv` is read in: UTF-8 when the
    whole file is (a byte-order mark dropped), else what Unstructured detects from
    its first MiB, checked over the whole file before anything is stored."""
    if _decodes(path, "utf-8-sig"):
        return "utf-8-sig"
    try:
        from unstructured.file_utils.encoding import detect_file_encoding
        with path.open("rb") as fh:
            sample = fh.read(_DETECT_BYTES)
        encoding, _ = detect_file_encoding(file=sample)
        codecs.lookup(encoding)
    except Exception:
        encoding = None
    if encoding and _decodes(path, encoding):
        return encoding
    raise ValueError("is not in an encoding that can be read in parts")


# ── unit readers (S3) ────────────────────────────────────────────────────────

def _cut_text(text: str, limit: int) -> list[str]:
    """Pieces of at most `limit` characters: at the last line break that fits,
    else the last whitespace, else hard at the limit, as _cap_chunk_length cuts."""
    limit = max(limit, 1)
    pieces: list[str] = []
    start, end = 0, len(text)
    while end - start > limit:
        stop = start + limit
        cut = text.rfind("\n", start + 1, stop + 1)
        if cut <= start:
            cut = max(text.rfind(ws, start + 1, stop + 1) for ws in (" ", "\t", "\r"))
        if cut <= start:
            cut = stop
        piece = text[start:cut]
        if piece.strip():
            pieces.append(piece)
        start = cut
        if start < end and text[start] in "\n \t\r":
            start += 1
    if text[start:end].strip():
        pieces.append(text[start:end])
    return pieces


def _new_element(element: Any, text: str) -> Any:
    try:
        return type(element)(text=text)
    except Exception:
        from unstructured.documents.elements import Text
        return Text(text=text)


def _csv_row(cells: list[str], dialect: Any) -> str:
    out = io.StringIO()
    csv.writer(out, dialect=dialect, lineterminator="\n").writerow(cells)
    return out.getvalue()


def cut_unit(unit: Unit, limit: int, dialect: Any = None) -> list[Unit]:
    """A text unit over a limit on its own, as pieces of at most `limit`
    characters. A CSV row is cut on its cells' text, each piece one cell."""
    if unit.cells is not None:
        pieces = _cut_text(" ".join(unit.cells), limit)
        return [Unit(_csv_row([p], dialect), cells=[p]) for p in pieces]
    pieces = _cut_text(unit.text, limit)
    if unit.element is not None:
        return [Unit(p, element=_new_element(unit.element, p)) for p in pieces]
    return [Unit(p) for p in pieces]


def _text_units(stream: Iterable[str] | Any, markdown: bool, limit: int) -> Iterator[Unit]:
    """Paragraphs, the text between blank lines; for Markdown, a blank line inside
    a fenced code block doesn't end one. A paragraph is given out in pieces when
    it passes `limit`, cut at a line break, so one is never held whole."""
    lines: list[str] = []
    size = 0
    fence = False
    while True:
        line = stream.readline(limit)
        if not line:
            break
        if markdown and line.lstrip().startswith(("```", "~~~")):
            fence = not fence
        if not line.strip() and not fence:
            if lines:
                yield Unit("".join(lines).rstrip("\n"))
                lines, size = [], 0
            continue
        if lines and size + len(line) > limit:
            yield Unit("".join(lines).rstrip("\n"))
            lines, size = [], 0
        lines.append(line)
        size += len(line)
    if lines:
        yield Unit("".join(lines).rstrip("\n"))


def _csv_dialect(stream: Any) -> Any:
    sample = stream.read(_SNIFF_CHARACTERS)
    stream.seek(0)
    try:
        return csv.Sniffer().sniff(sample)
    except csv.Error:
        return csv.excel


def _csv_units(stream: Any, dialect: Any) -> tuple[str, Iterator[Unit]]:
    """The header row as CSV text, and every later row as a unit."""
    try:
        from unstructured.partition.csv import CSV_FIELD_LIMIT
        csv.field_size_limit(CSV_FIELD_LIMIT)
    except ImportError:
        pass
    rows = csv.reader(stream, dialect)
    header_cells = next(rows, None)
    header = _csv_row(header_cells, dialect) if header_cells is not None else ""

    def units() -> Iterator[Unit]:
        for cells in rows:
            yield Unit(_csv_row(cells, dialect), cells=list(cells))
    return header, units()


class _Source:
    """A file's units, read lazily, and what a part's text is built from. `parse`
    and `chunk` are the caller's _parse_file and chunk."""

    def __init__(self, path: Path, ftype: str, settings: Settings, limit: int,
                 parse: Callable, chunk: Callable) -> None:
        from services import ingest_pipeline as ip
        self.path, self.ftype, self.settings, self.limit = path, ftype, settings, limit
        self.parse, self.chunk = parse, chunk
        self.header = ""
        self.dialect = None
        self._stream = None
        if ftype == "jsonl":
            self.units = (Unit(t, "record", o) for o, t, _ in ip._jsonl_units(path))
        elif ftype == "json":
            raw = ip._json_gz_units(path) if ip._is_json_gz(path.name) else ip._json_units(path)
            self.units = (Unit(t, "record", o, e) for o, t, e in raw)
        elif ftype == "elements":
            _, elements = parse(path)
            self.units = iter([Unit(str(el), element=el) for el in elements])
        else:
            encoding = text_encoding(path)
            self._stream = path.open("r", encoding=encoding, newline="" if ftype == "csv" else None)
            if ftype == "csv":
                self.dialect = _csv_dialect(self._stream)
                self.header, self.units = _csv_units(self._stream, self.dialect)
            else:
                self.units = _text_units(self._stream, ftype == "md", limit)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
        close = getattr(self.units, "close", None)
        if close is not None:
            close()

    def presized(self) -> Iterator[Unit]:
        """The units, a text unit over the part limit already cut into pieces."""
        for unit in self.units:
            if unit.kind == "text" and len(unit.text) > self.limit:
                yield from cut_unit(unit, self.limit, self.dialect)
            else:
                yield unit

    def build(self, units: list[Unit]) -> tuple[str, list[Any]]:
        """A part's text and elements, as a file holding only these units would
        give them (S3's table)."""
        ftype = self.ftype
        if ftype in ("jsonl", "json"):
            from services import ingest_pipeline as ip
            return ip._join_json_units([(u.ordinal, u.text, u.element) for u in units])
        if ftype == "elements":
            elements = [u.element for u in units]
            return "\n".join(str(el) for el in elements), elements
        if ftype == "csv":
            from unstructured.partition.csv import partition_csv
            data = (self.header + "".join(u.text for u in units)).encode("utf-8")
            elements = partition_csv(file=io.BytesIO(data), encoding="utf-8")
        elif ftype == "md":
            from unstructured.partition.md import partition_md
            elements = partition_md(text="\n\n".join(u.text for u in units))
        else:
            from unstructured.partition.text import partition_text
            elements = partition_text(text="\n\n".join(u.text for u in units))
        return "\n".join(str(el) for el in elements), elements


# ── chunking a part (S3.3, S3.4) ─────────────────────────────────────────────

def _chunk(chunk: Callable, text: str, elements: list[Any], settings: Settings) -> list[str]:
    return chunk(
        text=text,
        strategy=settings.strategy,
        chunk_size=settings.chunk_size,
        chunk_overlap_size=settings.chunk_overlap,
        similarity_threshold=settings.similarity_threshold,
        min_chunk_size=settings.min_chunk_size,
        elements=elements if settings.strategy == "context_aware" else None,
    )


def _record_over(unit: Unit, exc: ChunkLimitError) -> ValueError:
    if getattr(exc, "kind", None) == "chunks":
        return ValueError(
            f"{unit.ordinal} gives more than {chunker.MAX_CHUNKS_PER_FILE:,} chunks with these "
            f"chunk settings, the most one part can hold; a record is never split, so use a "
            f"larger chunk size")
    return ValueError(f"{unit.ordinal} is more than one part can hold with these chunk "
                      f"settings ({exc}); a record is never split")


def _chunk_units(source: _Source, units: list[Unit]) -> Iterator[list[str]]:
    """The chunks of one part; a part that still goes over an in-scope limit is
    split in half and each half chunked again, and a single text unit is cut."""
    text, elements = source.build(units)
    try:
        chunks = _chunk(source.chunk, text, elements, source.settings)
    except ChunkLimitError as exc:
        del text, elements
        if len(units) > 1:
            middle = len(units) // 2
            yield from _chunk_units(source, units[:middle])
            yield from _chunk_units(source, units[middle:])
            return
        unit = units[0]
        if unit.kind == "record":
            raise _record_over(unit, exc) from exc
        pieces = cut_unit(unit, (len(unit.text) + 1) // 2, source.dialect)
        if len(pieces) < 2:
            raise
        yield from _chunk_units(source, pieces)
        return
    yield chunks


# ── a file (S2, S3.1) ────────────────────────────────────────────────────────

def _whole(path: Path, settings: Settings, parse: Callable, chunk: Callable) -> list[str]:
    """A streamed text file chunked exactly as before the split existed."""
    text, elements = parse(path)
    return _chunk(chunk, text, elements, settings)


def split_file(path: Path, settings: Settings, parse: Callable | None = None,
               chunk: Callable | None = None) -> Iterator[tuple[str, list[str]]]:
    """Yields ("whole", chunks) once for a file that fits in one part, else
    ("part", chunks) for each of its two or more parts, in order.

    `parse` and `chunk` default to ingest's _parse_file and the chunker's chunk;
    callers pass their own, so whatever they parse and chunk with is used.

    An exception raised while producing part N, once the file is known to be
    split, is raised as PartError(N, exc).
    """
    if parse is None:
        from services import ingest_pipeline as ip
        parse = ip._parse_file
    chunk = chunk or chunker.chunk
    ftype = file_type(path)
    limit, max_units = part_limits(settings)

    if ftype in _STREAMED_TEXT and path.stat().st_size <= limit:
        # Every encoding takes at least a byte a character, so the text fits.
        try:
            chunks = _whole(path, settings, parse, chunk)
        except ChunkLimitError:
            pass
        else:
            yield "whole", chunks
            return
        # Its chunks go over: split from its units (S3.1), at most one part's
        # worth of text, so they are read at once.
        source = _Source(path, ftype, settings, limit, parse, chunk)
        try:
            everything = list(source.presized())
            yield from _parts(source, everything, None, iter(()), halve_first=True)
        finally:
            source.close()
        return

    source = _Source(path, ftype, settings, limit, parse, chunk)
    try:
        units = source.presized()
        first: list[Unit] = []
        chars = 0
        pending = None
        for unit in units:
            added = len(unit.text) + (_separator(ftype, unit.element is not None) if first else 0)
            if first and not _fits(chars + added, len(first) + 1, limit, max_units):
                pending = unit
                break
            first.append(unit)
            chars += added
        else:
            # The reader ended with the file inside one part's limits.
            try:
                if ftype in _STREAMED_TEXT:
                    chunks = _whole(path, settings, parse, chunk)
                else:
                    text, elements = source.build(first)
                    chunks = _chunk(chunk, text, elements, settings)
            except ChunkLimitError:
                pass
            else:
                yield "whole", chunks
                return
            yield from _parts(source, first, None, iter(()), halve_first=True)
            return
        yield from _parts(source, first, pending, units)
    finally:
        source.close()


def _fits(chars: int, count: int, limit: int, max_units: int | None) -> bool:
    return chars <= limit and (max_units is None or count <= max_units)


def _halves(source: _Source, units: list[Unit]) -> Iterator[list[str]]:
    """The chunks of units known to go over as one part: at least two parts."""
    if len(units) == 1:
        unit = units[0]
        if unit.kind == "record":
            # A record is never split; chunking it says why it is too large.
            yield from _chunk_units(source, units)
            return
        units = cut_unit(unit, (len(unit.text) + 1) // 2, source.dialect)
        if len(units) < 2:
            yield from _chunk_units(source, units)
            return
    middle = len(units) // 2
    yield from _chunk_units(source, units[:middle])
    yield from _chunk_units(source, units[middle:])


def _parts(source: _Source, first: list[Unit], pending: Unit | None,
           units: Iterator[Unit], halve_first: bool = False) -> Iterator[tuple[str, list[str]]]:
    """Every part of a split file, from the units already read (`first`, and the
    one that did not fit, `pending`) and the rest of the reader. With
    `halve_first`, `first` is known to go over as one part."""
    limit, max_units = part_limits(source.settings)
    number = 0
    current = first
    # A file that is a single record over a limit is not split at all: its own
    # error is the file's, not a part's.
    alone = halve_first and len(first) == 1 and first[0].kind == "record"
    try:
        while current:
            sep = _separator(source.ftype, current[0].element is not None)
            if pending is None and not halve_first:
                chars = sum(len(u.text) for u in current) + sep * (len(current) - 1)
                for unit in units:
                    added = len(unit.text) + sep
                    if not _fits(chars + added, len(current) + 1, limit, max_units):
                        pending = unit
                        break
                    current.append(unit)
                    chars += added
            produced = _halves(source, current) if halve_first else _chunk_units(source, current)
            halve_first = False
            for chunks in produced:
                number += 1
                yield "part", chunks
            if pending is not None:
                current, pending = [pending], None
            else:
                current = []
                for unit in units:
                    current = [unit]
                    break
    except Exception as exc:
        if alone:
            raise
        raise PartError(number + 1, exc) from exc
