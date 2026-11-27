"""The start-up model check (#673): one log line per model Ollama doesn't have.

Runs as a background task from the API's lifespan, so a slow or unreachable
Ollama never delays the start. It only reads `/api/tags`; it never pulls.
"""
from __future__ import annotations

import asyncio
import logging

from config import settings
from services import ollama_client

_log = logging.getLogger(__name__)
_TIMEOUT = 10.0


def missing_line(model: str) -> str:
    """The log line for a missing `name:tag`, with the command for this mode."""
    if settings.rag_external_ollama:
        return f"Ollama model missing: {model}. On the Ollama host, run: ollama pull {model}"
    return f"Ollama model missing: {model}. Run: docker compose exec ollama ollama pull {model}"


def unreachable_line() -> str:
    where = f"http://{settings.ollama_host}:{settings.ollama_port}"
    if settings.rag_external_ollama:
        return (f"Ollama at {where} could not be reached to check the models. "
                "Run scripts/check-external-ollama.sh on the host.")
    return f"Ollama at {where} could not be reached to check the models."


async def log_missing_models() -> None:
    try:
        missing = await asyncio.wait_for(ollama_client.missing_models(), _TIMEOUT)
    except asyncio.CancelledError:
        raise
    except Exception:                                 # noqa: BLE001 (a timeout included)
        missing = None
    try:
        if missing is None:
            _log.warning(unreachable_line())
            return
        for model in missing:
            _log.warning(missing_line(model))
    except Exception:                                 # noqa: BLE001
        _log.exception("The start-up model check failed")
