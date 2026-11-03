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
import itertools
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
    """The most text one part may hold, so that its estimated chunking stays
    inside the per-file limits. The second value is kept for callers and is always
    None: `context_aware` parts are sized by replaying its packing (_Packing),
    since a count of units would split files well inside the limits (PR #630)."""
    cap = chunker.MAX_CHUNKS_PER_FILE
    text = chunker.MAX_TEXT_CHARACTERS
    size, overlap = settings.chunk_size, settings.chunk_overlap
    strategy = settings.strategy
    if strategy == "fixed":
        return min(text, cap * size), None
    if strategy == "language":
        return min(text, cap * max(size - overlap, 1)), None
    if strategy == "context_aware":
        return min(text, cap * size), None
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


def _text_measure(unit: Unit) -> int:
    """The raw measure of a unit, deciding whether a .txt, .md or .csv over
    10,000,000 bytes is tried whole: its text, and a CSV row by its cells' text
    (empty cells and separators not counted, since a sparse CSV's bytes can be
    many times its text). It is not the parsed text and can be more (indentation,
    Markdown link targets, NA cells), so such a file may be split although its
    parsed text would fit; measuring raw units keeps memory bounded (maintainer's
    decision, PR #630)."""
    if unit.cells is not None:
        return sum(len(cell.strip()) for cell in unit.cells)
    return len(unit.text)


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
        self.own_size: int | None = None
        self.header_cells: list[str] = []
        if ftype == "jsonl":
            self.units = (Unit(t, "record", o) for o, t, _ in ip._jsonl_units(path))
        elif ftype == "json":
            raw = ip._json_gz_units(path) if ip._is_json_gz(path.name) else ip._json_units(path)
            self.units = (Unit(t, "record", o, e) for o, t, e in raw)
        elif ftype == "elements":
            _, elements = parse(path)
            self.units = iter([Unit(str(el), element=el) for el in elements])
            # A PDF or DOCX is measured by its extracted text, not its (compressed) bytes.
            self.own_size = sum(len(str(el)) for el in elements)
        else:
            encoding = text_encoding(path)
            self._stream = path.open("r", encoding=encoding, newline="" if ftype == "csv" else None)
            if ftype == "csv":
                self.dialect = _csv_dialect(self._stream)
                self.header, self.units = _csv_units(self._stream, self.dialect)
                self.header_cells = next(csv.reader([self.header], self.dialect), [])
            else:
                self.units = _text_units(self._stream, ftype == "md", limit)
        # What a part's units may hold: a CSV's header is repeated at the start
        # of every part, so it takes its share of each (#621, PR #630 review).
        self.room = limit - len(self.header)

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
        close = getattr(self.units, "close", None)
        if close is not None:
            close()

    def presized(self) -> Iterator[Unit]:
        """The units, a text unit over the room in a part already cut into pieces.
        A CSV header over half a part fails the file before it is split
        (check_header), so the cut never goes below half a part."""
        cut_at = max(self.room, self.limit // 2)
        for unit in self.units:
            if unit.kind == "text" and len(unit.text) > cut_at:
                yield from cut_unit(unit, cut_at, self.dialect)
            else:
                yield unit

    def own(self) -> int:
        """The file's own size, which its stored chunk text is measured against:
        its bytes; a gzip file's decompressed bytes; a PDF's or DOCX's text."""
        if self.own_size is None:
            size = self.path.stat().st_size
            if self.path.name.lower().endswith(".gz"):
                import gzip
                try:
                    size = 0
                    with gzip.open(self.path, "rb") as gz:
                        while True:
                            block = gz.read(_BLOCK)
                            if not block:
                                break
                            size += len(block)
                except (OSError, EOFError):
                    size = self.path.stat().st_size
            self.own_size = size
        return self.own_size

    def check_header(self) -> None:
        """Before a CSV is split: its header is repeated in every part, so one over
        half of what a part can hold would multiply the file's stored text. That
        fails the whole file (maintainer's decision on PR #630). A CSV that fits
        in one part keeps any header."""
        if self.ftype == "csv" and 2 * len(self.header) > self.limit:
            raise ValueError(
                f"its header row is {len(self.header):,} characters, more than half of what one "
                f"part can hold with these chunk settings ({self.limit:,} characters); the header "
                f"is repeated in every part, so shorten the header or split the file")

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
    """Yields ("whole", chunks) once for a file stored whole, else ("part",
    chunks) for each of its two or more parts, in order.

    Whether a file is split is never estimated (#621, PR #630): a file whose text
    is within MAX_TEXT_CHARACTERS is chunked whole, and split only when the
    chunker refuses it (the chunk count or the overlap limits). The part-size
    estimates only size the parts of a file that is split.

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
    limit, _ = part_limits(settings)
    text_limit = chunker.MAX_TEXT_CHARACTERS

    if ftype in _STREAMED_TEXT:
        # Parsed whole from the file itself, as before. Every encoding takes at
        # least a byte a character, so text within the limit is certain when the
        # bytes are; otherwise the text is measured in one pass, nothing kept.
        fits = path.stat().st_size <= text_limit
        if not fits:
            probe = _Source(path, ftype, settings, limit, parse, chunk)
            try:
                total = sum(len(cell.strip()) for cell in probe.header_cells)
                fits = True
                for unit in probe.units:
                    total += _text_measure(unit)
                    if total > text_limit:
                        fits = False
                        break
            finally:
                probe.close()
        if fits:
            try:
                chunks = _whole(path, settings, parse, chunk)
            except ChunkLimitError:
                pass
            else:
                yield "whole", chunks
                return
        source = _Source(path, ftype, settings, limit, parse, chunk)
        try:
            source.check_header()
            yield from _parts(source, [], None, source.presized())
        finally:
            source.close()
        return

    # JSON, JSON Lines, PDF and DOCX: the units are the file's own text, so up to
    # the text limit of them is read, then the file is chunked whole.
    source = _Source(path, ftype, settings, limit, parse, chunk)
    # Measured as the chunker measures it: under context_aware it is given the
    # elements and counts their text alone; otherwise it counts the joined text,
    # the separators between units included (PR #630).
    joined = settings.strategy != "context_aware"
    try:
        units = source.presized()
        first: list[Unit] = []
        chars = 0
        pending = None
        for unit in units:
            sep = _separator(ftype, unit.element is not None) if first and joined else 0
            added = len(unit.text) + sep
            if first and chars + added > text_limit:
                pending = unit
                break
            first.append(unit)
            chars += added
        else:
            try:
                text, elements = source.build(first)
                chunks = _chunk(chunk, text, elements, settings)
            except ChunkLimitError:
                pass
            else:
                yield "whole", chunks
                return
        source.check_header()
        yield from _parts(source, first, pending, units)
    finally:
        source.close()


def _fits(chars: int, count: int, limit: int, max_units: int | None) -> bool:
    return chars <= limit and (max_units is None or count <= max_units)


def _pieces(length: int) -> int:
    """How many chunks _cap_chunk_length makes of one chunk of `length` characters:
    it cuts at whitespace no earlier than half the ceiling."""
    ceiling = chunker.MAX_CHUNK_CHARACTERS
    return 1 if length <= ceiling else -(-length // (ceiling // 2))


class _Packing:
    """Replays chunk_context_aware's packing over a part's units, to count its
    chunks before minimum-size merging (which only lowers the count), plus the
    pieces _cap_chunk_length cuts an over-long one into. Used to size
    `context_aware` parts; halving remains the safety net."""

    def __init__(self, size: int) -> None:
        self.size, self.closed, self.buffer = size, 0, 0

    def copy(self) -> "_Packing":
        other = _Packing(self.size)
        other.closed, other.buffer = self.closed, self.buffer
        return other

    def add(self, unit: Unit) -> None:
        length = len(unit.text.strip())
        if not length:
            return
        if getattr(unit.element, "category", None) == "Table":
            if self.buffer:
                self.closed += _pieces(self.buffer)
                self.buffer = 0
            self.closed += _pieces(length)
        elif self.buffer and self.buffer + length + 1 > self.size:
            self.closed += _pieces(self.buffer)
            self.buffer = length
        else:
            self.buffer = self.buffer + 1 + length if self.buffer else length

    def count(self) -> int:
        return self.closed + (_pieces(self.buffer) if self.buffer else 0)


# A file split into parts may store at most this many times its own size in
# chunk text: a CSV header repeated in every part, overlap and JSON key paths all
# multiply it (maintainer's decision on PR #630).
MAX_STORED_RATIO = 10


class StoredTextTooLarge(ValueError):
    """A split file whose chunks would store more than MAX_STORED_RATIO times its
    own size. The whole file fails; it is not a part's error."""

    def __init__(self, stored: int, own: int) -> None:
        super().__init__(
            f"it would store {stored:,} characters of chunks, more than {MAX_STORED_RATIO} times "
            f"its own size ({own:,} characters); use less chunk_overlap, shorten repeated "
            f"headers or keys, or split the file")


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
           units: Iterator[Unit]) -> Iterator[tuple[str, list[str]]]:
    """Every part of a split file, from the units already read (`first`, and the
    one that did not fit, `pending`) and the rest of the reader.

    Every part is filled from one stream of units by the room left beside a CSV
    header and, for `context_aware`, a replay of its packing (PR #630). The file
    is known to need splitting, so if the first part would take every unit, it is
    halved instead: a split file has two or more parts. Before each part is handed
    on to be stored, the chunk text stored so far is checked against ten times
    the file's own size."""
    limit = source.room  # a CSV's header takes its share of every part
    packing = source.settings.strategy == "context_aware"
    cap = chunker.MAX_CHUNKS_PER_FILE
    stored = 0
    number = 0
    # A file that is a single record over a limit is not split at all: its own
    # error is the file's, not a part's.
    alone = False

    def hand_on(produced: Iterable[list[str]]) -> Iterator[tuple[str, list[str]]]:
        nonlocal stored, number
        for chunks in produced:
            # Checked before the part is handed on to be stored (#621, PR #630).
            stored += sum(len(c) for c in chunks)
            if stored > MAX_STORED_RATIO * source.own():
                raise StoredTextTooLarge(stored, source.own())
            number += 1
            yield "part", chunks

    try:
        stream = itertools.chain(first, [pending] if pending is not None else [], units)
        pending = None
        while True:
            if pending is not None:
                current, pending = [pending], None
            else:
                unit = next(stream, None)
                if unit is None:
                    return
                current = [unit]
            sep = _separator(source.ftype, current[0].element is not None)
            chars = len(current[0].text)
            pack = _Packing(source.settings.chunk_size)
            pack.add(current[0])
            for unit in stream:
                added = len(unit.text) + sep
                trial = pack.copy()
                trial.add(unit)
                if (not _fits(chars + added, len(current) + 1, limit, None)
                        or (packing and trial.count() > cap)):
                    pending = unit
                    break
                current.append(unit)
                chars += added
                pack = trial
            if number == 0 and pending is None:
                # The whole file in one part: it went over whole, so it is halved.
                alone = len(current) == 1 and current[0].kind == "record"
                yield from hand_on(_halves(source, current))
            else:
                yield from hand_on(_chunk_units(source, current))
    except Exception as exc:
        if alone or isinstance(exc, StoredTextTooLarge):
            raise
        raise PartError(number + 1, exc) from exc
