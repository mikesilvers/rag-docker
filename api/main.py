from __future__ import annotations
# LAN mode (#26) refuses to start without a Secure cookie and an enabled
# Engineer. First, so uvicorn's import of this module stops before any other
# import, any startup work and any request.
import lan_mode
lan_mode.enforce()

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from utils import api_error


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    from services.telemetry import bootstrap
    runtime = bootstrap()
    app.state.telemetry = runtime
    flusher = None
    try:
        import logging
        from services import goldstandard, metrics
        from services import weaviate_client as wc
        goldstandard.load_sessions_from_disk()
        goldstandard.reconcile_interrupted_generations()
        metrics.load_from_disk()
        # Sweep only durably owned scratch. Verified recovery collections and
        # unowned marker-like names must survive startup.
        log = logging.getLogger(__name__)
        try:
            from routers import collections as collection_routes
            await asyncio.to_thread(collection_routes.remove_interrupted_saves)
        except Exception:                                 # noqa: BLE001
            log.exception("Startup sweep of interrupted collection registry writes failed")
        try:
            abandoned = await wc.sweep_staging()
            if abandoned:
                log.warning("Removed %d staging collection(s) left by a previous run: %s. "
                            "Re-import the package to try again; it is still in ./exports.",
                            len(abandoned), ", ".join(abandoned))
        except Exception:                                 # noqa: BLE001
            log.exception("Startup sweep of staging collections failed")
        try:
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
        try:
            from services import packager
            leftovers = await asyncio.to_thread(packager.sweep_build_leftovers)
            if leftovers:
                log.warning("Removed %d leftover(s) of an interrupted export from the "
                            "exports folder: %s", len(leftovers), ", ".join(leftovers))
        except Exception:                                 # noqa: BLE001
            log.exception("Startup sweep of interrupted exports failed")
        # Job records survive a restart (#23). After the sweeps, so a job interrupted
        # mid-import is reported failed and its half-built collection already swept.
        # No worker from the previous process is alive, so any job still queued or
        # running is marked interrupted before the first request is served.
        from services import exporter, importer, ingest_pipeline, tuning
        interrupted = []
        for kind, module in (("ingest", ingest_pipeline), ("export", exporter),
                             ("import", importer), ("tune", tuning)):
            try:
                marked = await asyncio.to_thread(module._jobs.load)
                interrupted += [f"{kind} {job_id}" for job_id in marked]
            except Exception:                             # noqa: BLE001
                log.exception("Could not load the %s job records", kind)
        if interrupted:
            log.warning("Marked %d job(s) interrupted by the restart: %s. Their status now "
                        "says so; start them again.", len(interrupted), ", ".join(interrupted))
        # One audit record per start, saying whether question text is recorded,
        # so a reader can tell why some query records carry it (#29).
        from config import settings
        from services import audit
        await asyncio.to_thread(audit.record, "system.start", result="success", actor="system",
                                actor_kind="system",
                                details={"question_text_logged": settings.audit_log_questions,
                                         "max_bytes": settings.audit_log_max_bytes,
                                         "keep": settings.audit_log_keep})
        # Writes the count records of repeated failed logins as their windows
        # close; the open ones are written at shutdown, below (#577).
        flusher = asyncio.create_task(audit.login_flusher())
        yield
    finally:
        try:
            if flusher is not None:
                flusher.cancel()
                from services import audit
                await asyncio.to_thread(audit.flush_logins)
            from services import weaviate_client as wc
            wc.close_client()
        finally:
            await asyncio.to_thread(runtime.shutdown)


from fastapi import Depends
from auth.middleware import AuthMiddleware, Unlisted, require_listed

# require_listed refuses any route that has no entry in auth/policy.py, so a new
# route is closed until it is given one (#25).
app = FastAPI(title="RAG API", lifespan=lifespan, dependencies=[Depends(require_listed)])


@app.exception_handler(Unlisted)
async def unlisted_route(request, exc):
    return api_error(403, "FORBIDDEN", "This route is not open to any role.")


@app.exception_handler(RequestValidationError)
async def request_validation_error(request, exc):
    # Invalid settings can include non-finite JSON numbers. Echoing their raw
    # values in a JSONResponse would raise a serialization error instead of 422.
    errors = [{key: value for key, value in error.items() if key not in ("input", "ctx")}
              for error in exc.errors()]
    return api_error(422, "INVALID_PARAMETER", "Request parameters are invalid.", detail=errors)

from services.telemetry import RequestTracing

_log = logging.getLogger(__name__)

# Routing errors answer in the envelope like every other error (#373). Other
# statuses (FastAPI's 400 for an unparsable body) keep FastAPI's own answer.
_ROUTING_ERRORS = {
    404: ("NOT_FOUND", "No API route matches this path."),
    405: ("METHOD_NOT_ALLOWED", "This API route does not accept this method."),
}


@app.exception_handler(StarletteHTTPException)
async def routing_error(request, exc):
    if exc.status_code not in _ROUTING_ERRORS:
        return await http_exception_handler(request, exc)
    code, message = _ROUTING_ERRORS[exc.status_code]
    response = api_error(exc.status_code, code, message)
    response.headers.update(exc.headers or {})  # a 405 keeps its Allow header
    return response


@app.exception_handler(Exception)
async def unhandled_error(request, exc):
    # The cause is logged, not sent (#373). Starlette re-raises the exception
    # after this response is sent, so the server logs its traceback as well.
    _log.error("Unhandled error on %s %s: %s: %s", request.method, request.url.path,
               type(exc).__name__, exc)
    return api_error(500, "INTERNAL_ERROR",
                     "The API hit an unexpected error. The cause is in the API log.")

# Every route needs the role auth/policy.py gives it (#25). Added first, so it
# runs inside request tracing and a refused request is still traced. No CORS
# middleware: the UI is served from the same origin through nginx, so no other
# origin needs to read the API's answers.
app.add_middleware(AuthMiddleware)
# The audit log (#29): outside the auth middleware, so it sees that middleware's
# 403 refusals as well as the routes' own answers.
from services.audit import AuditMiddleware
app.add_middleware(AuditMiddleware)
app.add_middleware(RequestTracing)

from routers import audit as audit_router
from routers import auth as auth_router, help as help_router, health, collections, ingest, query, goldstandard, metrics, retrieval_config, transfer, tuning

app.include_router(health.router)
app.include_router(auth_router.router)
app.include_router(help_router.router)
app.include_router(collections.router)
app.include_router(ingest.router)
app.include_router(query.router)
app.include_router(goldstandard.router)
app.include_router(metrics.router)
app.include_router(retrieval_config.router)
app.include_router(transfer.router)
app.include_router(tuning.router)
app.include_router(audit_router.router)
