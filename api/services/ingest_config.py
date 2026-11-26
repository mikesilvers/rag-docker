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
from models.schemas import IngestConfigResponse, StoredIngestConfig
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
    """Return (config, is_default). Never raises; always usable.

    Saved settings that cannot be served, JSON that is not an object, a field of
    the wrong type, a non-finite similarity_threshold or the response's own
    is_default key (#515), are logged and give the defaults, like an unreadable
    file and as retrieval settings do (#439, #477). Values of the right type,
    including chunk sizes from before the #53 bounds, are served as saved; the
    next valid save replaces the file, and export still refuses it.
    """
    saved = load(collection)
    if saved is None:
        return {"collection": collection, **DEFAULTS}, True
    try:
        merged = {"collection": collection, **DEFAULTS, **saved}   # TypeError: not an object
        merged["collection"] = collection
        if "is_default" in merged:
            # Not a setting; the router would get the keyword twice.
            raise ValueError("saved settings hold is_default")
        IngestConfigResponse.model_validate({**merged, "is_default": False})
    except (TypeError, ValueError):   # pydantic's ValidationError is a ValueError
        log.warning("Invalid ingest config for %r; falling back to defaults", collection)
        return {"collection": collection, **DEFAULTS}, True
    return merged, False


def validate(config: dict, collection: str) -> dict:
    """Check package settings against the save contract, bound to `collection` (#510).

    Missing fields take the defaults and unknown fields are dropped, as on API
    saves. A chunk_size or min_chunk_size from before the #53 bounds is kept as
    it is. Raises ValueError for anything else the API would not save.
    """
    if not isinstance(config, dict):
        raise ValueError("Ingest settings must be an object")
    return {"collection": collection, **StoredIngestConfig.model_validate(config).model_dump()}


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
