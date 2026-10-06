from __future__ import annotations
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import settings
from models.schemas import (
    CollectionInfo,
    CollectionsResponse,
    CreateCollectionRequest,
)
from services import weaviate_client as wc
from services.collection_writes import canonical
from utils import api_error

router = APIRouter(prefix="/collections")

_REGISTRY_FILE: Path | None = None
_registry_lock = asyncio.Lock()


def _registry_path() -> Path:
    global _REGISTRY_FILE
    if _REGISTRY_FILE is None:
        _REGISTRY_FILE = Path(settings.upload_dir) / "collection_registry.json"
    return _REGISTRY_FILE


def _load_registry() -> dict:
    p = _registry_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {}


def _save_registry(reg: dict) -> None:
    p = _registry_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(reg, indent=2))


@router.get("", response_model=CollectionsResponse)
async def list_collections():
    raw = await wc.get_collections()
    registry = await asyncio.to_thread(_load_registry)
    items = [
        CollectionInfo(
            name=c["name"],
            object_count=c["object_count"],
            index_type=c["index_type"],
            distance_metric=c["distance_metric"],
            created_at=registry.get(c["name"]),
            hnsw_config=c.get("hnsw_config"),
        )
        for c in raw
    ]
    return CollectionsResponse(collections=items)


@router.post("", status_code=201)
async def create_collection(body: CreateCollectionRequest):
    if await wc.collection_exists(body.name):
        return api_error(409, "COLLECTION_EXISTS", f"Collection '{body.name}' already exists.")

    try:
        await wc.create_collection(
            name=body.name,
            index_type=body.index_type,
            distance_metric=body.distance_metric,
            hnsw_config=body.hnsw_config.model_dump(),
        )
    except Exception as exc:
        return api_error(500, "CREATE_FAILED", "Failed to create collection.", str(exc))

    async with _registry_lock:
        registry = await asyncio.to_thread(_load_registry)
        registry[body.name] = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(_save_registry, registry)

    return JSONResponse(status_code=201, content={"name": body.name, "status": "created"})


@router.delete("/{name}", status_code=200)
async def delete_collection(name: str):
    if not await wc.collection_exists(name):
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{name}' not found.")

    try:
        count = await wc.delete_collection(name)
    except Exception as exc:
        return api_error(500, "DELETE_FAILED", "Failed to delete collection.", str(exc))

    async with _registry_lock:
        registry = await asyncio.to_thread(_load_registry)
        canonical_name = canonical(name)
        for spelling in {canonical_name, canonical_name[:1].lower() + canonical_name[1:]}:
            registry.pop(spelling, None)
        await asyncio.to_thread(_save_registry, registry)

    return {"name": name, "objects_deleted": count}
