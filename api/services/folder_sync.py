"""Folder fsync shared by the API's atomic saves (#488)."""
from __future__ import annotations

import os
from pathlib import Path


def sync_folder(path: Path) -> None:
    """fsync a folder, so a rename into it survives a power loss (#445).

    The descriptor is closed even when the fsync fails; the error propagates.
    """
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
