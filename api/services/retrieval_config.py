"""Per-collection retrieval settings.

Retrieval mode, top_k, alpha and ef previously existed only in the browser's
sessionStorage, so a user's tuning died with the tab and there was nothing on the
server to export. They are persisted here, beside ingest_configs, so a collection
can record how it is meant to be queried.

Kept in a service rather than in the router because the exporter needs
programmatic access: an export package ships a retrieval script carrying these
parameters.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from config import settings
from models.schemas import SaveRetrievalConfigBody

log = logging.getLogger(__name__)

RETRIEVAL_MODES = ("hnsw", "flat", "hybrid", "semantic")
RESPONSE_FORMATS = ("end_user", "engineer")

DEFAULTS = {
    "retrieval_mode": "hnsw",
    "top_k": 5,
    "alpha": 0.75,
    "ef": None,
    "response_format": "end_user",
}

_DIR: Path | None = None


def _dir() -> Path:
    global _DIR
    if _DIR is None:
        _DIR = Path(settings.upload_dir) / "retrieval_configs"
        _DIR.mkdir(parents=True, exist_ok=True)
    return _DIR


def _safe_name(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", name)


def _path(collection: str) -> Path:
    return _dir() / f"{_safe_name(collection)}.json"


def load(collection: str) -> dict | None:
    """Saved config, or None when the collection has never been tuned."""
    p = _path(collection)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        log.warning("Unreadable retrieval config for %r; falling back to defaults", collection)
        return None


def resolve(collection: str) -> tuple[dict, bool]:
    """Return (config, is_default). Never raises; always usable."""
    saved = load(collection)
    if saved is None:
        return {"collection": collection, **DEFAULTS}, True
    # Fill in any key added since the file was written, so an older config does
    # not lose a field that callers now expect.
    merged = {"collection": collection, **DEFAULTS, **saved}
    merged["collection"] = collection
    return merged, False


def validate(config: dict, collection: str) -> dict:
    """Apply the API save contract, binding settings to the actual collection.

    Older packages may omit defaulted fields or carry an obsolete collection
    name. Extra fields are ignored just as they are for API saves.
    """
    if not isinstance(config, dict):
        raise ValueError("Retrieval settings must be an object")
    return SaveRetrievalConfigBody.model_validate(
        {**config, "collection": collection}).model_dump()


def save(config: dict) -> dict:
    collection = config["collection"]
    p = _path(collection)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(config, indent=2, sort_keys=True))
    tmp.replace(p)
    return config


def delete(collection: str) -> None:
    """Remove a collection's retrieval config; called when it is deleted."""
    _path(collection).unlink(missing_ok=True)
