"""Atomic settings publication shared by ingest and retrieval saves."""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import tempfile
import threading

from services.folder_sync import sync_folder as _sync_folder  # shared helper (#488)

log = logging.getLogger(__name__)

# Saves are short filesystem transactions dispatched to API worker threads.
# One lock also covers collection names that map to the same sanitized path.
_write_lock = threading.Lock()

# The temporary names publish makes: "." + the saved file's name (a safe name,
# A-Z, a-z, 0-9 and _, plus ".json") + "." + tempfile's 8 random characters
# (a-z, 0-9 and _) + ".tmp" (#488).
_LEFTOVER = re.compile(r"\.[A-Za-z0-9_]+\.json\.[a-z0-9_]{8}\.tmp")


def publish(path: Path, config: dict) -> None:
    """Publish this request's complete value, preserving the old file on failure.

    Serialize the whole transaction within this API process. Exclusive temporary
    names ensure even independent processes cannot rename each other's payloads.
    A later successful save may supersede this value after atomic replacement.
    The temporary file is fsynced before the replacement (#488), and the folder
    after it (#445); if the folder's fsync fails, the save raises with the new
    value already in place.
    """
    with _write_lock:
        payload = json.dumps(config, indent=2, sort_keys=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=f".{path.name}.", suffix=".tmp", delete=False,
            ) as output:
                temporary = Path(output.name)
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            temporary.replace(path)
            temporary = None
            _sync_folder(path.parent)
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    log.warning("Could not remove settings temporary file %s", temporary)


def remove_interrupted_saves(folder: Path) -> None:
    """Remove the temporary files of saves a crash interrupted in `folder` (#488).

    The saved files are whole: a save replaces one only once its temporary file
    is complete. Called once at startup, before any request, so no save is in
    progress. Only the names publish makes, at the top of the folder, are
    removed; a link is removed without following it, and a leftover that can't
    be removed is logged and kept. A missing folder is not created; an error
    listing the folder is the sweep failing, which the caller logs.
    """
    if not folder.is_dir():
        return
    for leftover in folder.iterdir():
        if _LEFTOVER.fullmatch(leftover.name):
            try:
                leftover.unlink()
            except OSError:
                log.exception("Could not remove interrupted settings write %s", leftover)
