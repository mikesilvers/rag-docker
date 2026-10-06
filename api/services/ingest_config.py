"""Per-collection chunking settings.

Extracted from the ingest router because three places needed the same on-disk
convention: the router that serves it, the exporter that packages it, and
collection deletion that must remove it. The third was missing, so a new
collection silently inherited the chunking settings of a deleted one with the
same name -- and each copy of the path logic was a chance for them to drift.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from config import settings
from services import settings_store

log = logging.getLogger(__name__)

DEFAULTS = {
    "chunking_strategy": "overlap",
    "chunk_size": 1000,
    "chunk_overlap": 200,
    "similarity_threshold": None,
    "min_chunk_size": 100,
}

_DIR: Path | None = None


def _dir() -> Path:
    global _DIR
    if _DIR is None:
        directory = Path(settings.upload_dir) / "ingest_configs"
        directory.mkdir(parents=True, exist_ok=True)
        _DIR = directory
    return _DIR


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _path(collection: str) -> Path:
    return _dir() / f"{_safe_name(collection)}.json"


def load(collection: str) -> dict | None:
    """Saved config, or None when the collection has never been configured."""
    p = _path(collection)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        log.warning("Unreadable ingest config for %r; falling back to defaults", collection)
        return None


def resolve(collection: str) -> tuple[dict, bool]:
    """Return (config, is_default). Never raises; always usable."""
    saved = load(collection)
    if saved is None:
        return {"collection": collection, **DEFAULTS}, True
    merged = {"collection": collection, **DEFAULTS, **saved}
    merged["collection"] = collection
    return merged, False


def save(config: dict) -> dict:
    collection = config["collection"]
    p = _path(collection)
    settings_store.publish(p, config)
    return config


def delete(collection: str) -> None:
    """Remove a collection's chunking settings.

    Called when the collection is deleted (spec §8 rule 1). Without this a
    recreated collection of the same name picks up settings the user never
    chose for it.
    """
    _path(collection).unlink(missing_ok=True)
