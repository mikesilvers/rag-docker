from __future__ import annotations
import asyncio
import time

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import settings
from services import ollama_client as ollama
from services import system_info
from services import weaviate_client as wc

router = APIRouter()

# Two routes, one check (#750). GET /health needs no login: the compose
# healthcheck, stack.sh, CI and the verify wait loops read its status code, so it
# answers only {"status"}, and on a LAN nobody without an account learns the
# models, timings or the host's memory. GET /health/details gives Developers and
# AI Engineers the full report, with the same 200/503 rule.


async def _report(details: bool = True) -> tuple[bool, dict]:
    """Whether every dependency is ok, and the full report.

    `details` False is the public route's: it leaves out the reads that only
    inform (the Ollama version and, with an external Ollama, what it has
    loaded), so the container healthcheck waits on nothing it doesn't use (#673)."""
    results = {}
    overall_ok = True

    # Check through the Weaviate client itself, not a bare HTTP readiness probe.
    # The probe cannot detect a client/server version mismatch -- the server
    # answers "ready" while every client call fails. Bounded by wait_for so a
    # hung connect cannot stall past the container healthcheck's 5s timeout.
    t0 = time.monotonic()
    try:
        ok = await asyncio.wait_for(wc.check_health(), timeout=4.0)
    except Exception:
        ok = False
    results["weaviate"] = {
        "status": "ok" if ok else "error",
        "latency_ms": int((time.monotonic() - t0) * 1000),
    }
    if not ok:
        overall_ok = False

    ollama_result = await ollama.check_health(with_version=details)
    results["ollama"] = ollama_result
    # `same_model` is a flag, not a service: it never decides the status (#671).
    if any(v["status"] != "ok" for v in ollama_result.values() if isinstance(v, dict)):
        overall_ok = False

    # Resource reporting is advisory and deliberately does NOT affect
    # overall_ok. Low memory degrades performance but the stack still serves
    # requests; failing the endpoint would mark the api container unhealthy and
    # take the whole stack down over a tuning warning.
    resources = {"memory": system_info.memory_info(settings.recommended_memory_gb)}
    if settings.rag_external_ollama and details:
        # The LLM runs in the external Ollama, outside Docker, so Docker's memory
        # isn't compared with the recommendation; what that Ollama has loaded,
        # other apps' models included, is reported and never acted on (#673).
        resources = {"memory": system_info.external_memory_info()}
        try:
            resources["ollama_loaded"] = await ollama.loaded_models()
        except Exception:                             # noqa: BLE001
            resources["ollama_loaded"] = None
            resources["ollama_loaded_note"] = (
                "Couldn't read what the external Ollama has loaded (/api/ps).")

    return overall_ok, {
        "status": "ok" if overall_ok else "degraded",
        "services": results,
        "resources": resources,
    }


@router.get("/health")
async def health_check():
    overall_ok, _ = await _report(details=False)
    return JSONResponse(status_code=200 if overall_ok else 503,
                        content={"status": "ok" if overall_ok else "degraded"})


@router.get("/health/details")
async def health_details():
    overall_ok, report = await _report()
    return JSONResponse(status_code=200 if overall_ok else 503, content=report)
