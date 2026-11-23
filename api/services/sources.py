"""Retention of original uploaded documents.

Ingest previously parsed uploads and deleted them, so only chunked text survived.
Export, re-chunking and re-embedding all need the originals, so accepted files
are copied here instead.

Files are content-addressed: the SHA-256 of the raw bytes is the storage key, so
re-ingesting the same document stores one copy and records the extra logical
name. Storage is proportional to the corpus rather than to upload count.

A logical name belongs to one digest at a time (#22): ingest stores with
replace=True, so a name ingested with new content leaves the digest it had, and a
digest left with no names is removed with its blob.

The index is only ever replaced whole (#52): a uniquely named temporary file in
the collection's folder, fsynced, then an atomic replace, so a failed or
interrupted save leaves the previous index. One collection's updates (read,
update, write, orphan deletion) are serialized within the API process, so
concurrent stores can't lose each other's entries. An index that can't be read
or parsed is refused like an invalid one, never treated as empty: an update
would otherwise replace every earlier entry with a partial index.
"""
from __future__ import annotations
from services import telemetry

import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path

from config import settings

log = logging.getLogger(__name__)

INDEX_NAME = "index.json"
INDEX_VERSION = 1
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def validate_index(index: dict) -> dict:
    """Refuse source identities that could address anything but a stored blob."""
    if not isinstance(index, dict) or not isinstance(index.get("documents"), dict):
        raise ValueError("Invalid retained source index")
    for digest, entry in index["documents"].items():
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError("Invalid retained source digest")
        if not isinstance(entry, dict):
            raise ValueError("Invalid retained source entry")
        names = entry.get("filenames")
        if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
            raise ValueError("Invalid retained source filenames")
    return index


def blob_path(collection: str, digest: str) -> Path:
    """Return a retained blob path only when it cannot escape its collection."""
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("Invalid retained source digest")
    directory = collection_dir(collection)
    if directory.is_symlink():
        raise ValueError("Retained source directory is a link")
    blob = directory / digest
    if blob.is_symlink():
        raise ValueError("Retained source blob is a link")
    return blob


def _root() -> Path:
    return Path(settings.sources_dir)


def collection_dir(collection: str) -> Path:
    return _root() / collection


def _index_path(collection: str) -> Path:
    return collection_dir(collection) / INDEX_NAME


def load_index(collection: str) -> dict:
    p = _index_path(collection)
    if p.is_symlink():
        raise ValueError("Retained source index is a link")
    if not p.exists():
        return {"version": INDEX_VERSION, "documents": {}}
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        # Never treated as empty (#52): the next store would replace every
        # earlier entry with an index holding only its own. Refused like an
        # invalid index, so the callers report SOURCE_INDEX_INVALID or fail.
        log.warning("Unreadable source index for %r; leaving it unchanged", collection)
        raise ValueError("Unreadable retained source index") from exc
    if not isinstance(data, dict):
        raise ValueError("Invalid retained source index")
    data.setdefault("version", INDEX_VERSION)
    data.setdefault("documents", {})
    return validate_index(data)


# One lock per collection folder serializes store's read/update/write cycle
# within this process (#52), like collection_writes; the API is one process.
_locks_guard = threading.Lock()
_locks: dict[str, threading.Lock] = {}


def _collection_lock(collection: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(collection_dir(collection)), threading.Lock())


def _temporary(path: Path) -> Path:
    """A new name beside path, so concurrent writers never share one (#52)."""
    return path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")


# The names _temporary makes. Only a crash or kill between creating one and
# replacing its target leaves one behind (#882).
_LEFTOVER = re.compile(r".+\.[0-9a-f]{16}\.tmp\Z")


def _publish(path: Path, payload: bytes) -> None:
    """Replace path whole: a uniquely named temporary file in the same folder,
    fsynced, then os.replace. A failure before the replace removes the
    temporary file and leaves path as it was (#52)."""
    tmp = _temporary(path)
    try:
        # "x" creates it exclusively, with the default permissions an index
        # always had.
        with open(tmp, "xb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _save_index(collection: str, index: dict) -> None:
    p = _index_path(collection)
    p.parent.mkdir(parents=True, exist_ok=True)
    _publish(p, json.dumps(index, indent=2, sort_keys=True).encode())


def restore_package(package: Path, collection: str) -> None:
    """Copy only preflighted indexed originals, including into recovery copies.

    Import preflight has checked the private extraction's index and blob bytes.
    Unindexed files must never become retained originals under a future digest.
    """
    src = package / "sources"
    if not (src / INDEX_NAME).is_file():
        return
    index = validate_index(json.loads((src / INDEX_NAME).read_text()))
    dest = collection_dir(collection)
    dest.mkdir(parents=True, exist_ok=True)
    for digest in index["documents"]:
        shutil.copyfile(src / digest, dest / digest)
    # The package's exact bytes, published whole like every index save (#52).
    _publish(dest / INDEX_NAME, (src / INDEX_NAME).read_bytes())


def _drop_blobs(collection: str, digests: list[str]) -> None:
    """Delete blobs the index no longer lists. Called after the index is saved,
    so it never names a missing blob; a blob left behind is only unused space."""
    for digest in digests:
        try:
            blob_path(collection, digest).unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            log.error("Could not delete unreferenced source %s of %r: %s",
                      digest[:12], collection, exc)


def _release(index: dict, filename: str, keep: str | None = None) -> tuple[bool, list[str]]:
    """Remove filename from every digest but keep, and drop each digest left
    with no names from the index. Returns whether the index changed and the
    dropped digests, whose blobs are deleted only after the index is saved."""
    changed = False
    orphans: list[str] = []
    for other, entry in list(index["documents"].items()):
        if other != keep and filename in entry["filenames"]:
            changed = True
            entry["filenames"] = [n for n in entry["filenames"] if n != filename]
            if not entry["filenames"]:
                del index["documents"][other]
                orphans.append(other)
    return changed, orphans


def _save_release(collection: str, index: dict, filename: str) -> None:
    """Save an index that releases filename. When even that fails, the name
    still points at its earlier version, which a re-chunk would rebuild (#880):
    best effort, so that is logged loudly and the error raised."""
    try:
        _save_index(collection, index)
    except OSError as exc:
        log.error("Could not release %r from its earlier version in %r; a re-chunk could "
                  "rebuild that earlier version: %s", filename, collection, exc)
        raise


def release(collection: str, filename: str) -> None:
    """Release filename from every digest (#880): ingest calls this when the
    re-uploaded file can't be read for retention, so the name never keeps
    claiming the version whose chunks it replaced.

    Holds the collection's lock (#52). An unreadable or invalid index raises
    ValueError, and nothing is written then or when no digest lists the name.
    """
    with _collection_lock(collection):
        index = load_index(collection)
        changed, orphans = _release(index, filename)
        if changed:
            _save_release(collection, index, filename)
            _drop_blobs(collection, orphans)


@telemetry.traced("rag.retain")
def store(collection: str, filename: str, data: bytes, media_type: str | None = None,
          *, replace: bool = False) -> str:
    """Retain one accepted upload. Returns its sha256.

    With replace=True (ingest, #22) the filename is first released from every
    other digest, and digests left with no names are dropped. That release is
    saved before the new blob is written (#880), so a failed blob write or a
    failed final save still leaves the name released: it never keeps claiming
    the version it replaced, which a rechunk would otherwise rebuild. When the
    release itself can't be saved, that is logged and the error raised.

    The whole cycle holds the collection's lock (#52), and the index is loaded
    before anything is written: an unreadable or invalid index raises
    ValueError and leaves the folder exactly as it was.
    """
    digest = hashlib.sha256(data).hexdigest()
    with _collection_lock(collection):
        target = collection_dir(collection) / digest
        target.parent.mkdir(parents=True, exist_ok=True)

        index = load_index(collection)
        if replace:
            changed, orphans = _release(index, filename, keep=digest)
            if changed:
                _save_release(collection, index, filename)
                _drop_blobs(collection, orphans)

        if not target.exists():
            # Published like an index (#882): fsynced before the index that
            # names it, so a power loss can't leave it empty or truncated.
            _publish(target, data)

        now = datetime.now(timezone.utc).isoformat()
        entry = index["documents"].get(digest)
        if entry is None:
            index["documents"][digest] = {
                "filenames": [filename],
                "size": len(data),
                "media_type": media_type,
                "first_seen": now,
                "last_seen": now,
            }
        else:
            if filename not in entry["filenames"]:
                entry["filenames"].append(filename)
            entry["last_seen"] = now
        _save_index(collection, index)
    return digest


def digests_for_filename(collection: str, filename: str) -> list[str]:
    """All digests ever stored under this logical filename.

    Returns more than one when the same name was ingested with different
    content. Callers must decide what that means rather than assume one.
    """
    index = load_index(collection)
    return [d for d, e in index["documents"].items() if filename in e.get("filenames", [])]


def has_sources(collection: str) -> bool:
    """True when the collection has retained originals (export fidelity)."""
    return bool(load_index(collection)["documents"])


def stats(collection: str) -> dict:
    docs = load_index(collection)["documents"]
    return {
        "document_count": len(docs),
        "total_bytes": sum(e.get("size", 0) for e in docs.values()),
    }


def delete(collection: str) -> None:
    """Remove every retained source for a collection.

    Called when the collection is deleted. Without this the volume leaks
    silently: it is surfaced nowhere in the UI.
    """
    directory = collection_dir(collection)
    shutil.rmtree(directory, ignore_errors=True)
    # Its lock goes too (#882), so the map doesn't keep one per collection
    # ever stored into; a later store into the same name makes a new one.
    with _locks_guard:
        _locks.pop(str(directory), None)


def remove_interrupted_saves() -> None:
    """Remove the temporary files of saves a crash or kill interrupted (#882).

    Every index and blob is written whole through a uniquely named temporary
    file, so a leftover is never used, only disk space. Called once at startup,
    before any request, so no save is in progress. Only names _temporary makes
    are removed, in each collection folder that isn't a link; a leftover that
    can't be removed is logged and kept. An error listing the folders is the
    whole sweep failing, which the caller logs.
    """
    root = _root()
    if not root.is_dir():
        return
    for folder in root.iterdir():
        if folder.is_symlink() or not folder.is_dir():
            continue
        for leftover in folder.iterdir():
            if _LEFTOVER.fullmatch(leftover.name):
                try:
                    leftover.unlink()
                except OSError:
                    log.exception("Could not remove interrupted source write %s", leftover)
