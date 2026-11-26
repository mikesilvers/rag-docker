"""How long ingesting a file will take on this machine (#614).

Embedding is the slow step of ingest on a CPU-only host, so a running ingest job
shows each file's chunk count once it is chunked, the embedding rate this
instance has measured, and the time left for the file and the job.

**The rate** comes from real ingests only, never a benchmark or a built-in
default. One measurement is one stored part (a file stored whole is one part):
the characters of the chunk texts handed to the store, and the seconds the
store took, embedding and the insert check included. A failed store is not a
measurement. The instance keeps its last RATE_WINDOW measurements, and the rate
is their characters over their seconds, so a large part weighs more than a
one-chunk file. They are saved in `<UPLOAD_DIR>/embedding-rate.json` (counts and
times only), so a restart keeps the rate; the older `ingest-rate.json` is read
until that file exists (#896). Until the first measurement there is no rate,
and the job status says so ("no estimate yet"). A failed save is logged once.

**The time left** counts only work already chunked and not yet stored. Files
the job hasn't reached, the file it is still parsing (before its first chunks,
`file_chunking`), and a split file's later parts (#621), aren't guessed:
`files_not_chunked` counts the first two, and `seconds_left_is_minimum` says the
job's time left is then a minimum. Inside a long part the time left counts down
by the time the part has been storing (`storing_since`), never below 0. Only a
running job has a time left; a queued, finished or interrupted one shows its
counts alone.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from services import settings_store

_log = logging.getLogger(__name__)

RATE_WINDOW = 20
# A ZIP can hold thousands of files, and the job store writes the whole record
# up to once a second, so the record keeps the newest entries and counts the rest.
FILE_PROGRESS_LIMIT = 100
RATE_FILE = "embedding-rate.json"
# The name before #896. It starts with `ingest-`, one of the startup sweep's
# work-directory prefixes (importer._WORKDIR_PREFIXES), so the rate moved to
# RATE_FILE; this one is read only while RATE_FILE doesn't exist yet.
OLD_RATE_FILE = "ingest-rate.json"

_lock = threading.Lock()
_samples: list[tuple[int, float]] | None = None
_warned = False
_save_warned = False


def _path() -> Path:
    return Path(settings.upload_dir) / RATE_FILE


def _old_path() -> Path:
    return Path(settings.upload_dir) / OLD_RATE_FILE


def _valid(characters, seconds) -> bool:
    return (isinstance(characters, int) and not isinstance(characters, bool) and characters > 0
            and isinstance(seconds, (int, float)) and not isinstance(seconds, bool)
            and seconds > 0)


def _read(path: Path) -> list[tuple[int, float]]:
    """One rate file's valid measurements; raises FileNotFoundError when it is
    missing, and another error when it is unreadable or malformed."""
    data = json.loads(path.read_text(encoding="utf-8"))
    samples = data["samples"]
    if not isinstance(samples, list):
        raise ValueError("samples is not a list")
    kept = []
    for sample in samples:
        if not (isinstance(sample, list) and len(sample) == 2):
            raise ValueError("a sample is not a [characters, seconds] pair")
        if _valid(sample[0], sample[1]):
            kept.append((sample[0], float(sample[1])))
    return kept[-RATE_WINDOW:]


def _load() -> list[tuple[int, float]]:
    """The saved measurements: from RATE_FILE, or from OLD_RATE_FILE while
    RATE_FILE doesn't exist; none when neither exists, or when the one read is
    unreadable or malformed (logged once: the next measurement replaces it)."""
    global _warned
    for path in (_path(), _old_path()):
        try:
            return _read(path)
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError) as exc:
            if not _warned:
                _log.warning("Ignoring the unreadable ingest rate file %s: %s", path, exc)
                _warned = True
            return []
    return []


def _loaded() -> list[tuple[int, float]]:
    global _samples
    if _samples is None:
        _samples = _load()
    return _samples


def record(characters: int, seconds: float) -> None:
    """Add one stored part's measurement and save the window. A failure to save
    never fails the ingest, and the measurement still counts. The first failure
    in the process is logged with its traceback, later ones only at DEBUG: a
    process without a writable UPLOAD_DIR fails every save (#896)."""
    global _save_warned
    if not _valid(characters, seconds):
        return
    with _lock:
        samples = _loaded()
        samples.append((characters, float(seconds)))
        del samples[:-RATE_WINDOW]
        try:
            settings_store.publish(_path(), {"samples": [list(s) for s in samples]})
        except Exception:                               # noqa: BLE001
            if not _save_warned:
                _save_warned = True
                _log.warning("Could not save the ingest rate to %s", _path(), exc_info=True)
            else:
                _log.debug("Could not save the ingest rate to %s", _path())


def rate() -> float | None:
    """The rolling rate in characters per second, or None before any measurement."""
    with _lock:
        samples = _loaded()
        if not samples:
            return None
        return sum(c for c, _ in samples) / sum(s for _, s in samples)


def _reset_for_tests() -> None:
    global _samples, _warned, _save_warned
    with _lock:
        _samples = None
        _warned = False
        _save_warned = False


# ── the job record ───────────────────────────────────────────────────────────
# Each helper changes the record in one update and assigns a new list: the job
# store doesn't see changes made in place to a nested list (jobstore.py).

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _entries(job: dict) -> list[dict]:
    return [dict(entry) for entry in (job.get("file_progress") or [])]


def file_started(job: dict) -> None:
    """The job loop has reached a file, whether or not it makes chunks. Until
    its first part is chunked it has no entry, and counts as not chunked (#896)."""
    job.update({"files_started": (job.get("files_started") or 0) + 1, "file_chunking": True})


def part_chunked(job: dict, label: str, chunks: int, characters: int, first: bool) -> None:
    """A whole file, or one part of a split file, is chunked and about to be stored."""
    entries = _entries(job)
    omitted = job.get("file_progress_omitted") or 0
    if first or not entries:
        entries.append({"file": label, "chunks": chunks, "chunks_stored": 0,
                        "characters": characters, "characters_stored": 0,
                        "chunked": False, "failed": False})
        if len(entries) > FILE_PROGRESS_LIMIT:
            dropped = len(entries) - FILE_PROGRESS_LIMIT
            entries = entries[dropped:]
            omitted += dropped
    else:
        last = entries[-1]
        last["chunks"] = last.get("chunks", 0) + chunks
        last["characters"] = last.get("characters", 0) + characters
    job.update({"file_progress": entries, "file_progress_omitted": omitted,
                "storing_since": _now_iso(), "file_chunking": False})


def part_stored(job: dict, chunks: int, characters: int, seconds: float) -> None:
    """The part chunked last was stored in `seconds`: count it and measure it."""
    entries = _entries(job)
    if entries:
        last = entries[-1]
        last["chunks_stored"] = last.get("chunks_stored", 0) + chunks
        last["characters_stored"] = last.get("characters_stored", 0) + characters
    job.update({"file_progress": entries, "storing_since": None})
    record(characters, seconds)


def file_ended(job: dict, failed: bool, has_entry: bool = True) -> None:
    """The file is finished: all chunked, or failed. With `has_entry` it is the
    file with the last entry; without, it ended before any chunks (#896)."""
    update = {"storing_since": None, "file_chunking": False}
    entries = _entries(job)
    if has_entry and entries:
        last = entries[-1]
        last["failed"] = bool(failed)
        if not failed:
            last["chunked"] = True
        update["file_progress"] = entries
    job.update(update)


# ── the job status ───────────────────────────────────────────────────────────

def _parse(stamp) -> datetime | None:
    if not isinstance(stamp, str):
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


_UNSET = object()


def view(job: dict, now: datetime | None = None, current_rate=_UNSET) -> dict:
    """The estimate fields of a job status, computed when it is read."""
    entries = _entries(job)
    started = job.get("files_started")
    # The files not reached, and the one being parsed before its first chunks.
    not_chunked = (max((job.get("files_total") or 0) - started, 0)
                   + (1 if job.get("file_chunking") else 0)
                   if isinstance(started, int) else None)
    out = {"file_progress": entries,
           "file_progress_omitted": job.get("file_progress_omitted") or 0,
           "files_not_chunked": not_chunked,
           "rate_characters_per_second": None,
           "seconds_left": None,
           "seconds_left_is_minimum": False}
    for entry in entries:
        entry["seconds_left"] = None
    if job.get("status") != "running":
        return out

    speed = rate() if current_rate is _UNSET else current_rate
    if speed is not None and speed > 0:
        now = now or datetime.now(timezone.utc)
        since = _parse(job.get("storing_since"))
        elapsed = max((now - since).total_seconds(), 0.0) if since else 0.0
        total = 0.0
        for index, entry in enumerate(entries):
            if entry.get("failed"):
                left = 0.0
            else:
                left = max(entry.get("characters", 0) - entry.get("characters_stored", 0), 0) / speed
                if index == len(entries) - 1:
                    left = max(left - elapsed, 0.0)
            entry["seconds_left"] = left
            total += left
        out["rate_characters_per_second"] = speed
        out["seconds_left"] = total
    out["seconds_left_is_minimum"] = bool(
        (not_chunked or 0) > 0
        or any(not e.get("chunked") and not e.get("failed") for e in entries))
    return out
