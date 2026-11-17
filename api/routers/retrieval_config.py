"""Per-collection retrieval settings.

Mirrors the ingest-config endpoints, including the asymmetry: GET takes the
collection in the path, POST takes it in the body.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from models.schemas import CollectionName, RetrievalConfigResponse, SaveRetrievalConfigBody
from services import audit, retrieval_config
from utils import usable_collection

router = APIRouter(prefix="/retrieval")


@router.get("/config/{collection}", response_model=RetrievalConfigResponse)
async def get_retrieval_config(collection: CollectionName, request: Request = None):
    # A missing collection, or one the caller may not use, is a 404 (#606).
    refusal = await usable_collection(request, collection, "No settings were read")
    if refusal is not None:
        return refusal
    cfg, is_default = await asyncio.to_thread(retrieval_config.resolve, collection)
    return RetrievalConfigResponse(is_default=is_default, **cfg)


@router.post("/config", response_model=RetrievalConfigResponse, status_code=201)
async def save_retrieval_config(body: SaveRetrievalConfigBody, request: Request = None):
    audit.note(collection=body.collection)        # the audit record's collection (#29)
    refusal = await usable_collection(request, body.collection, "No settings were saved")
    if refusal is not None:
        return refusal
    cfg = body.model_dump()
    await asyncio.to_thread(retrieval_config.save, cfg)
    return RetrievalConfigResponse(is_default=False, **cfg)
