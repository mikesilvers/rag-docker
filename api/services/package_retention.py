"""Export packages don't stay on the server (#677).

A finished export package in `./exports` holds a whole collection, so every copy
left there is data at rest. Two things remove a package this instance exported:

- **A full download** through `GET /packages/{filename}` (routers/transfer.py)
  deletes it once its last byte has been handed to the connection. An
  interrupted download keeps it, so it can be retried.
- **Expiry.** A package nobody downloads is deleted `EXPORT_RETENTION_HOURS`
  (1 to 720, default 24) after its export finished. The sweep runs at start-up
  and then every 15 minutes (`sweeper`, started by main.py).

**Which packages.** Only packages this instance exported: when an export
finishes, its filename and finish time are recorded in
`<UPLOAD_DIR>/exported_packages.json`, never in `./exports`. A package copied
into `./exports` to be imported, or one that was there before this record
existed, is never recorded, so neither a download nor the sweep deletes it.
A package copied straight from the host folder is still deleted after the expiry.

**Busy.** While an import of a package runs, or a download of it is in progress,
a download is refused (409 PACKAGE_BUSY) and the sweep skips it until its next
run. Claiming a download and the sweep's check-and-delete take the same lock.

**One way to delete.** `delete` removes the package, its companion files (none
yet; #676's key file will be one) and its record entry, and writes one
`package.delete` audit record. `may_sweep` alone decides whether the sweep may
delete a package, so a later "keep" flag changes only that function.

**The record.** Replaced whole (a temporary file renamed into place) under a
lock. One that can't be read is logged and taken as empty: nothing recorded is
then deleted until a new record is written.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from config import settings

_log = logging.getLogger(__name__)

RECORD_NAME = "exported_packages.json"
SWEEP_INTERVAL_S = 15 * 60
# The names a download serves: what exports write (packager.package_stem). The
# `ragpkg-` prefix rules out a leading `.` and the class rules out `/`.
NAME = re.compile(r"ragpkg-[A-Za-z0-9._-]+\.tar\.gz(?:\.age)?")
_TIME = "%Y-%m-%dT%H:%M:%SZ"

_lock = threading.RLock()
_downloading: set[str] = set()


def valid_name(name) -> bool:
    return isinstance(name, str) and NAME.fullmatch(name) is not None


def _exports() -> Path:
    return Path(settings.exports_dir)


def _path() -> Path:
    return Path(settings.upload_dir) / RECORD_NAME


# ── the record ───────────────────────────────────────────────────────────────

def _read() -> dict[str, str]:
    """Recorded filename -> finish time. Missing is empty; unreadable is logged
    and taken as empty."""
    path = _path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        _log.error("Could not read %s; no exported package is expired or deleted after a "
                   "download until it is written again", path)
        return {}
    packages = data.get("packages") if isinstance(data, dict) else None
    if not isinstance(packages, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in packages.items()):
        _log.error("%s is not a record of exported packages; it is taken as empty", path)
        return {}
    return dict(packages)


def _write(entries: dict[str, str]) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix="." + RECORD_NAME + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"packages": entries}, fh, indent=1, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record_export(filename: str, finished: datetime) -> None:
    """Record a package this instance exported. Raises when it can't be written."""
    with _lock:
        entries = _read()
        entries[filename] = finished.astimezone(timezone.utc).strftime(_TIME)
        _write(entries)


def _drop(filename: str) -> None:
    entries = _read()
    if entries.pop(filename, None) is not None:
        _write(entries)


def _expiry(finished_at: str) -> datetime | None:
    try:
        finished = datetime.strptime(finished_at, _TIME).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return finished + timedelta(hours=settings.export_retention_hours)


def _format(when: datetime | None) -> str | None:
    return when.strftime(_TIME) if when is not None else None


def expires_at(filename: str) -> str | None:
    """When a recorded package expires (UTC), or None for one not recorded."""
    finished = _read().get(filename)
    return _format(_expiry(finished)) if finished is not None else None


def expiries() -> dict[str, str | None]:
    """Every recorded package's `expires_at`, from one read (the listing)."""
    return {name: _format(_expiry(finished)) for name, finished in _read().items()}


def recorded(filename: str) -> bool:
    return filename in _read()


def companions(filename: str) -> list[str]:
    """Files deleted with a package. None yet: #676's key file will be one."""
    return []


# ── busy, and the download slot ──────────────────────────────────────────────

def busy(filename: str) -> bool:
    """A download of it is in progress, or an import of it runs."""
    from services import importer                     # importer imports nothing from here
    with _lock:
        return filename in _downloading or importer.package_busy(filename)


def claim(filename: str) -> bool:
    """Take the download slot; False when the package is busy."""
    with _lock:
        if busy(filename):
            return False
        _downloading.add(filename)
        return True


def release(filename: str) -> None:
    with _lock:
        _downloading.discard(filename)


def may_sweep(filename: str, finished_at: str, now: datetime) -> bool:
    """The one place that decides the sweep may delete a package: it has
    expired and isn't busy (#677). A "keep" flag would be checked here."""
    expiry = _expiry(finished_at)
    return expiry is not None and expiry <= now and not busy(filename)


# ── deleting ─────────────────────────────────────────────────────────────────

def _remove_regular(path: Path) -> bool | None:
    """Unlink a regular file, never following a link. True when removed, None
    when it is missing, False when something else has the name (kept)."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return False
    path.unlink()
    return True


def delete(filename: str, reason: str, scope=None) -> bool:
    """Delete a recorded package, its companions and its entry, and write its
    `package.delete` record. `scope` is the downloading request's (reason
    `downloaded`); without one the actor is the system (reason `expired`).
    True when the package was deleted."""
    from services import audit
    root = _exports()
    with _lock:
        try:
            removed = _remove_regular(root / filename)
        except OSError:
            _log.exception("Could not delete package %s (%s); it is kept and tried again", filename, reason)
            return False
        if removed is None:
            _drop(filename)
            return False
        if removed is False:
            _log.warning("%s in the exports folder is not a regular file; it is left as it is "
                         "and no longer expired", filename)
            _drop(filename)
            return False
        for name in companions(filename):
            try:
                if _remove_regular(root / name) is False:
                    _log.warning("Companion %s of %s is not a regular file; left as it is", name, filename)
            except OSError:
                _log.exception("Could not delete %s, a companion of %s", name, filename)
        _drop(filename)
    audit.package_deleted(filename, reason, scope)
    return True


def after_download(filename: str, scope) -> bool:
    """After a complete download: delete the package when this instance
    exported it and no import of it has started meanwhile."""
    from services import importer
    with _lock:
        if not recorded(filename) or importer.package_busy(filename):
            return False
        return delete(filename, "downloaded", scope)


def sweep(now: datetime | None = None) -> list[str]:
    """Delete the recorded packages that have expired. Busy ones wait for the
    next run; an entry whose file is gone is dropped. Returns the deleted names."""
    now = now or datetime.now(timezone.utc)
    deleted = []
    with _lock:
        for filename, finished_at in sorted(_read().items()):
            expiry = _expiry(finished_at)
            if expiry is None or expiry > now:
                continue
            if not os.path.lexists(_exports() / filename):
                _drop(filename)
                continue
            if not may_sweep(filename, finished_at, now):
                continue
            if delete(filename, "expired"):
                deleted.append(filename)
    return deleted


async def sweeper() -> None:
    """Sweep at once (the start-up run), then every SWEEP_INTERVAL_S."""
    while True:
        try:
            deleted = await asyncio.to_thread(sweep)
            if deleted:
                _log.warning("Deleted %d expired export package(s) nobody downloaded within "
                             "EXPORT_RETENTION_HOURS (%d h): %s", len(deleted),
                             settings.export_retention_hours, ", ".join(deleted))
        except Exception:                             # noqa: BLE001 - the next run tries again
            _log.exception("The sweep of expired export packages failed")
        await asyncio.sleep(SWEEP_INTERVAL_S)
