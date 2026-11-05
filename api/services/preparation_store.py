"""The preparation area (#638): raw documents and their prepared parts, on disk.

One area for the instance, in the rag_preparation volume (`settings.preparation_dir`):

    docs/<id>/record.json        the document's record (SPECIFICATIONS.md §3.1.11)
    docs/<id>/raw/<file name>    the raw bytes, as uploaded
    docs/<id>/prepared/NNN<sfx>  the prepared parts, by index; names are in the record
    docs/<id>/prepared.new/      a preparation run's parts until they replace them
    staging/                     files being copied in, renamed into docs/ when whole

Ids are 12 hex characters, checked before any path is built; a client's name is
never a path, only the last segment of it names the raw file. Links are refused.

One lock guards the records and the busy set. It is held only for record reads
and writes, the busy set and renames, never while content is copied or parsed;
async callers run every function that touches the disk off the event loop.

The size limit is a best-effort cap (maintainer's decision): `usage()` reads the
files at the moment it is called, takes no lock and reserves nothing, so writes
running at once can pass the limit briefly.

Nothing here talks HTTP or to the LLM; errors carry the API's error codes.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from config import settings

_log = logging.getLogger(__name__)

_ID = re.compile(r"[0-9a-f]{12}\Z")
RECORD = "record.json"
PREVIEW_CHARACTERS = 2000

STATUSES = ("raw", "queued", "preparing", "prepared", "failed")

_lock = threading.RLock()
# Document id -> why it is busy (a job id, or "import").
_busy: dict[str, str] = {}


class AreaError(Exception):
    """An error the API answers with `status` and `code`."""
    status = 400
    code = "PREPARE_FAILED"

    def __init__(self, message: str, detail: Any = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail


class DocumentNotFound(AreaError):
    status, code = 404, "DOCUMENT_NOT_FOUND"


class DocumentBusy(AreaError):
    status, code = 409, "DOCUMENT_BUSY"


class DocumentNotPrepared(AreaError):
    status, code = 409, "DOCUMENT_NOT_PREPARED"


class StorageFull(AreaError):
    status, code = 409, "PREPARATION_STORAGE_FULL"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── paths ────────────────────────────────────────────────────────────────────

def _root() -> Path:
    return Path(settings.preparation_dir)


def _docs() -> Path:
    return _root() / "docs"


def _staging() -> Path:
    return _root() / "staging"


def _ensure_dirs() -> None:
    for folder in (_docs(), _staging()):
        if folder.is_symlink():
            raise AreaError("The preparation area holds a link where a folder belongs.")
        folder.mkdir(parents=True, exist_ok=True)


def valid_id(document_id: Any) -> bool:
    return isinstance(document_id, str) and bool(_ID.fullmatch(document_id))


def _doc_dir(document_id: str) -> Path:
    if not valid_id(document_id):
        raise DocumentNotFound(f"Document '{document_id}' not found.")
    folder = _docs() / document_id
    if folder.is_symlink():
        raise DocumentNotFound(f"Document '{document_id}' not found.")
    return folder


def raw_file_name(name: str) -> str:
    """The file name the raw bytes are kept under: the name's last segment, or
    `file` when that segment couldn't be a plain file name."""
    last = name.replace("\\", "/").rsplit("/", 1)[-1]
    if not last or last in (".", "..") or "\x00" in last:
        return "file"
    return last


def _check_plain(path: Path) -> Path:
    if path.is_symlink():
        raise AreaError("The preparation area holds a link where a file belongs.")
    return path


# ── records ──────────────────────────────────────────────────────────────────

def _write_record(folder: Path, record: dict) -> None:
    """Write a record whole: a temporary file, fsync, then rename."""
    fd, tmp = tempfile.mkstemp(prefix=".record.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, folder / RECORD)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_record(folder: Path) -> dict:
    path = _check_plain(folder / RECORD)
    try:
        with path.open(encoding="utf-8") as fh:
            record = json.load(fh)
    except FileNotFoundError:
        raise DocumentNotFound(f"Document '{folder.name}' not found.") from None
    if not isinstance(record, dict) or record.get("id") != folder.name:
        raise AreaError(f"The record of document '{folder.name}' can't be read.")
    return record


def _public(record: dict) -> dict:
    """The record as the API shows it: without internal fields."""
    return {key: value for key, value in record.items() if not key.startswith("_")}


def get(document_id: str) -> dict:
    with _lock:
        return _public(_read_record(_doc_dir(document_id)))


def _get_internal(document_id: str) -> dict:
    return _read_record(_doc_dir(document_id))


def _save(record: dict) -> None:
    _write_record(_doc_dir(record["id"]), record)


def list_documents() -> list[dict]:
    """Every document's record, newest first by `created_at`. A record that
    can't be read is left out and logged, so one bad record never fails the list."""
    records = []
    with _lock:
        if not _docs().is_dir():
            return []
        for entry in _docs().iterdir():
            if not valid_id(entry.name) or entry.is_symlink() or not entry.is_dir():
                continue
            try:
                records.append(_public(_read_record(entry)))
            except (AreaError, OSError, ValueError):
                _log.warning("Left document %r out of the preparation list", entry.name,
                             exc_info=True)
    records.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return records


# ── usage (best-effort) ──────────────────────────────────────────────────────

def _tree_size(folder: Path) -> int:
    total = 0
    try:
        entries = list(os.scandir(folder))
    except (FileNotFoundError, NotADirectoryError):
        return 0
    for entry in entries:
        try:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                total += _tree_size(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                total += entry.stat(follow_symlinks=False).st_size
        except FileNotFoundError:
            # Deleted or renamed while the walk ran: not an error (best-effort).
            continue
    return total


def usage() -> int:
    """Bytes the area's files hold now, docs/ and staging/ together. No lock,
    nothing reserved: a check made with it is best-effort."""
    return _tree_size(_docs()) + _tree_size(_staging())


def limit() -> int:
    return settings.max_preparation_bytes


def _mib(size: int) -> str:
    return f"{size / (1024 * 1024):,.1f} MB"


def check_space(adding: int, what: str) -> None:
    """Refuse with StorageFull when `adding` more bytes would pass the limit."""
    used = usage()
    if used + adding > limit():
        left = max(limit() - used, 0)
        raise StorageFull(
            f"The preparation area can't hold {what} ({_mib(adding)}): it uses "
            f"{_mib(used)} of {_mib(limit())}, {_mib(left)} left. Delete documents "
            f"you no longer need and try again.",
            detail={"used_bytes": used, "limit_bytes": limit(), "left_bytes": left,
                    "needed_bytes": adding})


def check_not_full() -> None:
    """Refuse with StorageFull when the area is already at or over the limit."""
    used = usage()
    if used >= limit():
        raise StorageFull(
            f"The preparation area is full: it uses {_mib(used)} of {_mib(limit())}. "
            f"Delete documents you no longer need and try again.",
            detail={"used_bytes": used, "limit_bytes": limit(), "left_bytes": 0})


# ── documents ────────────────────────────────────────────────────────────────

def new_id() -> str:
    return secrets.token_hex(6)


def raw_type_of(name: str) -> str:
    lower = name.lower()
    for double in (".jsonl.gz", ".json.gz"):
        if lower.endswith(double):
            return double
    return Path(lower).suffix


def create(name: str, source: Path, owner: dict | None) -> dict:
    """A new `raw` document named `name` from the file at `source`, which is
    copied into staging/ outside the lock and renamed into place under it."""
    _ensure_dirs()
    document_id = new_id()
    fd, tmp = tempfile.mkstemp(prefix="upload.", dir=_staging())
    os.close(fd)
    staged = Path(tmp)
    try:
        shutil.copyfile(source, staged)
        size = staged.stat().st_size
        record = {
            "id": document_id,
            "name": name,
            "raw_type": raw_type_of(name),
            "raw_bytes": size,
            "owner": owner,
            "created_at": now_iso(),
            "status": "raw",
            "prepared_at": None,
            "prepared_bytes": 0,
            "parts": [],
            "fallback": False,
            "code_only_by_type": raw_type_of(name) in CODE_ONLY_TYPES,
            "last_error": None,
            "imports": [],
        }
        with _lock:
            folder = _docs() / document_id
            (folder / "raw").mkdir(parents=True)
            os.replace(staged, folder / "raw" / raw_file_name(name))
            _write_record(folder, record)
        return _public(record)
    except BaseException:
        staged.unlink(missing_ok=True)
        shutil.rmtree(_docs() / document_id, ignore_errors=True)
        raise


CODE_ONLY_TYPES = (".csv", ".json", ".json.gz", ".jsonl", ".jsonl.gz")


def raw_path(document_id: str) -> Path:
    with _lock:
        record = _read_record(_doc_dir(document_id))
        path = _check_plain(_doc_dir(document_id) / "raw" / raw_file_name(record["name"]))
        if not path.is_file():
            raise DocumentNotFound(f"Document '{document_id}' has no raw file.")
        return path


def part_paths(document_id: str) -> list[tuple[str, Path]]:
    """(name, path) of each prepared part, in order."""
    with _lock:
        record = _read_record(_doc_dir(document_id))
        return _part_paths(record)


def _part_paths(record: dict) -> list[tuple[str, Path]]:
    folder = _doc_dir(record["id"]) / "prepared"
    out = []
    for index, part in enumerate(record.get("parts") or []):
        out.append((part["name"], _check_plain(folder / part["file"])))
    return out


def open_raw(document_id: str) -> tuple[dict, Any]:
    """The record and an open handle on the raw file, opened under the lock, so a
    delete after this can't cut a download short."""
    with _lock:
        record = _read_record(_doc_dir(document_id))
        path = _check_plain(_doc_dir(document_id) / "raw" / raw_file_name(record["name"]))
        try:
            return _public(record), path.open("rb")
        except FileNotFoundError:
            raise DocumentNotFound(f"Document '{document_id}' has no raw file.") from None


def open_parts(document_id: str) -> tuple[dict, list[tuple[str, Any]]]:
    """The record and open handles on its prepared parts, opened under the lock."""
    with _lock:
        record = _read_record(_doc_dir(document_id))
        if record.get("status") not in ("prepared", "queued", "preparing") or not record.get("parts"):
            raise DocumentNotPrepared(f"Document '{record['name']}' has no prepared output.")
        handles = []
        try:
            for name, path in _part_paths(record):
                handles.append((name, path.open("rb")))
        except BaseException:
            for _, fh in handles:
                fh.close()
            raise
        return _public(record), handles


def preview(document_id: str) -> dict:
    """The record, each part with its first 2,000 characters as `preview`."""
    with _lock:
        record = _read_record(_doc_dir(document_id))
        paths = _part_paths(record) if record.get("parts") else []
    out = _public(record)
    parts = []
    for part, (_, path) in zip(record.get("parts") or [], paths):
        item = dict(part)
        item.pop("file", None)
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                item["preview"] = fh.read(PREVIEW_CHARACTERS)
        except OSError:
            item["preview"] = ""
        parts.append(item)
    out["parts"] = parts
    return out


def list_entry(record: dict) -> dict:
    """A record as the list shows it: parts without their file names."""
    out = dict(record)
    out["parts"] = [{k: v for k, v in part.items() if k != "file"}
                    for part in record.get("parts") or []]
    return out


def is_busy(document_id: str) -> bool:
    with _lock:
        return document_id in _busy


def mark_busy(ids: Iterable[str], reason: str) -> None:
    """Mark documents busy (a job, or an import being admitted). All or none:
    any already busy refuses them all with DocumentBusy."""
    ids = list(ids)
    with _lock:
        taken = [i for i in ids if i in _busy]
        if taken:
            names = [_safe_name(i) for i in taken]
            raise DocumentBusy(
                f"{_list_names(names)} {'is' if len(names) == 1 else 'are'} being prepared "
                f"or imported; wait until that finishes.", detail={"documents": names})
        for i in ids:
            _busy[i] = reason


def release(ids: Iterable[str]) -> None:
    with _lock:
        for i in ids:
            _busy.pop(i, None)


def _safe_name(document_id: str) -> str:
    try:
        return _read_record(_doc_dir(document_id))["name"]
    except Exception:
        return document_id


def _list_names(names: list[str]) -> str:
    shown = ", ".join(f"'{n}'" for n in names[:10])
    if len(names) > 10:
        shown += f" and {len(names) - 10} more"
    return shown


def delete(document_id: str) -> dict:
    """Remove a document, raw and prepared. Refused while it is busy."""
    with _lock:
        folder = _doc_dir(document_id)
        record = _read_record(folder)
        if document_id in _busy or record.get("status") in ("queued", "preparing"):
            raise DocumentBusy(f"'{record['name']}' is being prepared or imported; wait "
                               f"until that finishes.", detail={"documents": [record["name"]]})
        # Renamed aside first, so the document is gone at once, then removed.
        _ensure_dirs()
        trash = Path(tempfile.mkdtemp(prefix="delete.", dir=_staging()))
        os.replace(folder, trash / document_id)
    shutil.rmtree(trash, ignore_errors=True)
    return _public(record)


def delete_prepared(document_id: str) -> dict:
    """Remove a document's prepared output; it is `raw` again, its imports kept."""
    with _lock:
        folder = _doc_dir(document_id)
        record = _read_record(folder)
        if document_id in _busy or record.get("status") in ("queued", "preparing"):
            raise DocumentBusy(f"'{record['name']}' is being prepared or imported; wait "
                               f"until that finishes.", detail={"documents": [record["name"]]})
        if record.get("status") != "prepared" or not record.get("parts"):
            raise DocumentNotPrepared(f"Document '{record['name']}' has no prepared output.")
        _ensure_dirs()
        trash = Path(tempfile.mkdtemp(prefix="delete.", dir=_staging()))
        if (folder / "prepared").exists():
            os.replace(folder / "prepared", trash / "prepared")
        record.update(status="raw", parts=[], prepared_bytes=0, prepared_at=None,
                      fallback=False, last_error=None)
        _write_record(folder, record)
    shutil.rmtree(trash, ignore_errors=True)
    return _public(record)


# ── preparation runs ─────────────────────────────────────────────────────────

def queue_for_job(ids: list[str], job_id: str) -> None:
    """Mark documents busy for a job and `queued`, remembering their status."""
    mark_busy(ids, job_id)
    try:
        with _lock:
            for document_id in ids:
                record = _read_record(_doc_dir(document_id))
                record["_previous_status"] = record.get("status")
                record["status"] = "queued"
                _save(record)
    except BaseException:
        abandon(ids)
        raise


def abandon(ids: Iterable[str]) -> None:
    """Cancel or interrupt: each document goes back to the status it had before
    the job, its unfinished output removed, and is released."""
    ids = list(ids)
    with _lock:
        for document_id in ids:
            try:
                folder = _doc_dir(document_id)
                record = _read_record(folder)
            except AreaError:
                continue
            shutil.rmtree(folder / "prepared.new", ignore_errors=True)
            if record.get("status") in ("queued", "preparing"):
                record["status"] = record.pop("_previous_status", None) or (
                    "prepared" if record.get("parts") else "raw")
                record.pop("_job_id", None)
                _save(record)
        release(ids)


def begin(document_id: str, job_id: str) -> tuple[dict, Path]:
    """Start preparing a document: `preparing`, an empty prepared.new/. Returns
    the record and the folder parts are written to."""
    with _lock:
        folder = _doc_dir(document_id)
        record = _read_record(folder)
        record["status"] = "preparing"
        record["_job_id"] = job_id
        new = folder / "prepared.new"
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir()
        _save(record)
        return _public(record), new


def commit(document_id: str, parts: list[dict], fallback: bool) -> dict:
    """The new output replaces any earlier one: prepared.new/ becomes prepared/."""
    with _lock:
        folder = _doc_dir(document_id)
        record = _read_record(folder)
        new, current = folder / "prepared.new", folder / "prepared"
        _ensure_dirs()
        trash = Path(tempfile.mkdtemp(prefix="replace.", dir=_staging()))
        if current.exists():
            os.replace(current, trash / "prepared")
        os.replace(new, current)
        record.update(status="prepared", parts=parts, fallback=fallback,
                      prepared_bytes=sum(p["bytes"] for p in parts),
                      prepared_at=now_iso(), last_error=None)
        record.pop("_previous_status", None)
        record.pop("_job_id", None)
        _save(record)
    shutil.rmtree(trash, ignore_errors=True)
    release([document_id])
    return _public(record)


def fail(document_id: str, reason: str) -> dict:
    """A failed run: its new output is removed. A document with earlier output
    stays `prepared` with it and records the reason (maintainer's decision);
    one without becomes `failed`."""
    with _lock:
        folder = _doc_dir(document_id)
        record = _read_record(folder)
        shutil.rmtree(folder / "prepared.new", ignore_errors=True)
        has_output = bool(record.get("parts")) and (folder / "prepared").is_dir()
        record["status"] = "prepared" if has_output else "failed"
        record["last_error"] = reason
        record.pop("_previous_status", None)
        record.pop("_job_id", None)
        _save(record)
    release([document_id])
    return _public(record)


def add_import(ids: Iterable[str], collection: str, job_id: str) -> None:
    with _lock:
        for document_id in ids:
            try:
                record = _read_record(_doc_dir(document_id))
            except AreaError:
                continue
            record.setdefault("imports", []).append(
                {"collection": collection, "job_id": job_id, "at": now_iso()})
            _save(record)


def recover() -> list[str]:
    """At startup: documents left `queued` or `preparing` by a killed process go
    back to the status they had, and staging and unfinished output are removed."""
    restored = []
    if not _root().is_dir():
        return restored
    _busy.clear()
    if _docs().is_dir():
        for entry in _docs().iterdir():
            if not valid_id(entry.name) or entry.is_symlink() or not entry.is_dir():
                continue
            try:
                record = _read_record(entry)
            except (AreaError, OSError, ValueError):
                continue
            shutil.rmtree(entry / "prepared.new", ignore_errors=True)
            if record.get("status") in ("queued", "preparing"):
                abandon([entry.name])
                restored.append(record.get("name") or entry.name)
    if _staging().is_dir() and not _staging().is_symlink():
        for entry in _staging().iterdir():
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink(missing_ok=True)
    return restored
