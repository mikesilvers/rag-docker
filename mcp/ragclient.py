"""Thin async HTTP client for the RAG API, with error translation.

All business logic stays in the api service. This module's only job is to make
failures legible to a model: an API error code the caller can act on, or a plain
statement that the stack is not running.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx
from mcp.server.mcpserver.exceptions import ToolError

import config

log = logging.getLogger(__name__)


class RagApiError(ToolError):
    """A failure the caller should see verbatim.

    Subclasses ToolError deliberately: the SDK surfaces a ToolError's message to
    the client and masks everything else as "Error executing tool <name>" with no
    detail. An API error code or a "stack is not running" hint is exactly what the
    caller needs, so these must not be masked.
    """


def _unreachable(exc: Exception) -> RagApiError:
    return RagApiError(
        f"Could not reach the RAG API at {config.RAG_API_BASE_URL}. "
        "The stack is probably not running. Check it with 'docker compose ps' "
        "and start it with 'docker compose up -d' from the project directory. "
        f"({type(exc).__name__})"
    )


def _format_error(resp: httpx.Response) -> RagApiError:
    """Translate an error response, preserving the API's own error code."""
    try:
        body = resp.json()
    except ValueError:
        return RagApiError(f"HTTP {resp.status_code} from {resp.request.url}: {resp.text[:300]}")

    # The project envelope: {"error": {"code", "message", "detail"}}
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        code = err.get("code", f"HTTP_{resp.status_code}")
        msg = err.get("message", "")
        detail = err.get("detail")
        text = f"{code}: {msg}" if msg else code
        return RagApiError(f"{text} (detail: {detail})" if detail else text)

    # FastAPI validation errors use {"detail": [...]} instead of the envelope,
    # so reaching for body["error"] here would raise KeyError and hide the cause.
    if isinstance(body, dict) and "detail" in body:
        return RagApiError(f"HTTP {resp.status_code}: {body['detail']}")

    return RagApiError(f"HTTP {resp.status_code}: {str(body)[:300]}")


async def request(
    method: str,
    path: str,
    allow_status: tuple[int, ...] = (),
    **kwargs: Any,
) -> Any:
    """Call the RAG API.

    allow_status lists status codes to return normally instead of raising. It
    exists for /health, which answers 503 {"status": "degraded"} when a
    dependency is down: that is information for the caller, not an error.
    """
    url = f"{config.RAG_API_BASE_URL}{path}"
    log.debug("%s %s", method, url)
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as client:
            resp = await client.request(method, url, **kwargs)
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        raise _unreachable(exc) from exc
    except httpx.ReadTimeout as exc:
        raise RagApiError(
            f"The RAG API did not respond within {config.HTTP_TIMEOUT:.0f}s. "
            "LLM generation is CPU-bound and slow when Docker is short on memory; "
            "check 'resources.memory' at the API's /health/details (a Developer "
            "or AI Engineer login)."
        ) from exc
    except httpx.HTTPError as exc:
        raise _unreachable(exc) from exc

    if resp.status_code >= 400 and resp.status_code not in allow_status:
        raise _format_error(resp)

    if not resp.content:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


async def get(path: str, **kw: Any) -> Any:
    return await request("GET", path, **kw)


async def post(path: str, **kw: Any) -> Any:
    return await request("POST", path, **kw)


async def patch(path: str, **kw: Any) -> Any:
    return await request("PATCH", path, **kw)


async def delete(path: str, **kw: Any) -> Any:
    return await request("DELETE", path, **kw)
