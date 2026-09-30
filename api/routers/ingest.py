from __future__ import annotations
import asyncio
import json
import re
from pathlib import Path
from fastapi import APIRouter, Form, UploadFile, File
from pydantic import ValidationError

from config import settings
from models.schemas import IngestConfig, IngestConfigResponse, IngestUploadResponse, JobStatusResponse
from services import ingest_config
from services import ingest_pipeline
from services import weaviate_client as wc
from utils import api_error

router = APIRouter(prefix="/ingest")

class SaveIngestConfigBody(IngestConfig):
    collection: str


@router.post("/upload", response_model=IngestUploadResponse, status_code=202)
async def ingest_upload(
    collection: str = Form(...),
    strategy: str = Form("overlap"),
    chunk_size: int = Form(1000),
    chunk_overlap: int = Form(200),
    similarity_threshold: float = Form(0.85),
    min_chunk_size: int = Form(100),
    files: list[UploadFile] = File(...),
):
    try:
        IngestConfig(chunking_strategy=strategy, chunk_size=chunk_size,
                     chunk_overlap=chunk_overlap, similarity_threshold=similarity_threshold,
                     min_chunk_size=min_chunk_size)
    except ValidationError as exc:
        return api_error(422, "INVALID_SETTINGS", "Invalid chunking settings.",
                         detail={"errors": exc.errors(include_context=False, include_input=False)})
    if not await wc.collection_exists(collection):
        return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{collection}' not found.")

    try:
        job_id = await ingest_pipeline.start_ingest_job(
            files=files,
            collection=collection,
            strategy=strategy,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            similarity_threshold=similarity_threshold,
            min_chunk_size=min_chunk_size,
        )
    except ValueError as exc:
        return api_error(400, "NO_SUPPORTED_FILES", str(exc))

    return IngestUploadResponse(
        job_id=job_id,
        status="queued",
        files_queued=len(files),
        collection=collection,
    )


@router.get("/job/{job_id}", response_model=JobStatusResponse)
async def job_status(job_id: str):
    job = ingest_pipeline.get_job(job_id)
    if job is None:
        return api_error(404, "JOB_NOT_FOUND", f"Job '{job_id}' not found.")
    return JobStatusResponse(**job)


@router.get("/config/{collection}", response_model=IngestConfigResponse)
async def get_ingest_config(collection: str):
    cfg, is_default = await asyncio.to_thread(ingest_config.resolve, collection)
    return IngestConfigResponse(is_default=is_default, **cfg)


@router.post("/config", response_model=IngestConfigResponse, status_code=201)
async def save_ingest_config(body: SaveIngestConfigBody):
    cfg = body.model_dump()
    await asyncio.to_thread(ingest_config.save, cfg)
    return IngestConfigResponse(is_default=False, **cfg)
