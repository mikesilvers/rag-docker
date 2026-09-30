from __future__ import annotations
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from utils import api_error
from fastapi.middleware.cors import CORSMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    import logging
    from services import goldstandard, metrics
    from services import weaviate_client as wc
    goldstandard.load_sessions_from_disk()
    metrics.load_from_disk()
    # An import or tune killed part-way cannot run its own cleanup, so its
    # staging collection would survive forever. Nothing can be using one before
    # the app starts serving, so clearing them here is safe.
    log = logging.getLogger(__name__)
    try:
        abandoned = await wc.sweep_staging()
        if abandoned:
            log.warning("Removed %d staging collection(s) left by a previous run: %s. "
                        "Re-import the package to try again; it is still in ./exports.",
                        len(abandoned), ", ".join(abandoned))
    except Exception:                                 # noqa: BLE001
        log.exception("Startup sweep of staging collections failed")
    try:
        import asyncio
        from services import importer
        partial = await asyncio.to_thread(importer.sweep_interrupted_imports)
        if partial:
            log.warning("Removed %d collection(s) left half-built by an interrupted "
                        "import: %s. Re-import the package to try again.",
                        len(partial), ", ".join(partial))
        stale_dirs = await asyncio.to_thread(importer.sweep_stale_workdirs)
        if stale_dirs:
            log.warning("Removed %d abandoned extraction directory(ies): %s",
                        len(stale_dirs), ", ".join(stale_dirs))
    except Exception:                                 # noqa: BLE001
        log.exception("Startup sweep of interrupted imports failed")
    yield
    from services import weaviate_client as wc
    wc.close_client()


app = FastAPI(title="RAG API", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def request_validation_error(request, exc):
    # Invalid settings can include non-finite JSON numbers. Echoing their raw
    # values in a JSONResponse would raise a serialization error instead of 422.
    errors = [{key: value for key, value in error.items() if key not in ("input", "ctx")}
              for error in exc.errors()]
    return api_error(422, "INVALID_PARAMETER", "Request parameters are invalid.", detail=errors)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

from routers import help as help_router, health, collections, ingest, query, goldstandard, metrics, retrieval_config, transfer, tuning

app.include_router(health.router)
app.include_router(help_router.router)
app.include_router(collections.router)
app.include_router(ingest.router)
app.include_router(query.router)
app.include_router(goldstandard.router)
app.include_router(metrics.router)
app.include_router(retrieval_config.router)
app.include_router(transfer.router)
app.include_router(tuning.router)
