"""Health and metrics tools."""
from __future__ import annotations

from mcp.types import ToolAnnotations

import ragclient
from mcpapp import server

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


@server.tool(
    name="rag_health",
    description=(
        "Check whether the RAG stack is working: its overall status, 'ok' or "
        "'degraded' (Weaviate, the LLMs and the embedding model together). "
        "Use this first when anything else fails. Which service is down, the "
        "model names and Docker's memory are at the API's /health/details, "
        "for a Developer or AI Engineer (a login or a service key)."
    ),
    annotations=READ_ONLY,
)
async def rag_health() -> dict:
    # A degraded stack is information, not a tool failure. The API answers 503
    # {"status": "degraded"}, so that status is allowed through and returned
    # intact rather than flattened into an error string. Only an unreachable API
    # is a tool error, which ragclient raises. GET /health needs no login and
    # carries only the status (#750); the details need a key, which the MCP
    # server doesn't have yet (#39).
    return await ragclient.request("GET", "/health", allow_status=(503,))


@server.tool(
    name="rag_metrics",
    description=(
        "Latency statistics (p50/p95/p99/mean) for retrieval, LLM generation "
        "and total query time. Populated by queries already run."
    ),
    annotations=READ_ONLY,
)
async def rag_metrics() -> dict:
    return await ragclient.get("/metrics/latency")
