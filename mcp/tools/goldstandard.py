"""Gold-standard evaluation set tools."""
from __future__ import annotations

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

import ragclient
from mcpapp import server

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True)


@server.tool(
    name="rag_generate_goldstandard",
    description=(
        "Start generating a gold-standard question/answer set from a "
        "collection, for evaluating retrieval quality. Returns a session_id "
        "immediately; generation runs one LLM call per pair and takes minutes. "
        "Poll rag_get_goldstandard with the session_id to see progress."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False),
)
async def rag_generate_goldstandard(
    collection: str,
    sample_size: int = 20,
    seed: int | None = None,
) -> dict:
    if isinstance(sample_size, bool) or not isinstance(sample_size, int) or not 1 <= sample_size <= 100:
        raise ToolError("sample_size must be an integer between 1 and 100.")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ToolError("seed must be an integer or null.")
    body: dict = {"collection": collection, "sample_size": sample_size}
    if seed is not None:
        body["seed"] = seed
    # Deliberately does not poll: at the default of 20 pairs this is one LLM
    # call per pair, which would exceed any sane tool budget.
    return await ragclient.post("/goldstandard/generate", json=body)


@server.tool(
    name="rag_get_goldstandard",
    description=(
        "Read a gold-standard session: status, progress counts and the "
        "generated question/answer pairs."
    ),
    annotations=READ_ONLY,
)
async def rag_get_goldstandard(session_id: str) -> dict:
    return await ragclient.get(f"/goldstandard/session/{session_id}")


@server.tool(
    name="rag_update_goldstandard_pair",
    description=(
        "Edit or approve one question/answer pair in a gold-standard session. "
        "Supply at least one of status, question, answer or ground_truth."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
)
async def rag_update_goldstandard_pair(
    session_id: str,
    pair_id: str,
    status: str | None = None,
    question: str | None = None,
    answer: str | None = None,
    ground_truth: str | None = None,
) -> dict:
    body = {
        k: v
        for k, v in (
            ("status", status),
            ("question", question),
            ("answer", answer),
            ("ground_truth", ground_truth),
        )
        if v is not None
    }
    if not body:
        raise ToolError(
            "Supply at least one of status, question, answer or ground_truth to change."
        )
    return await ragclient.patch(
        f"/goldstandard/session/{session_id}/pair/{pair_id}", json=body
    )


@server.tool(
    name="rag_regenerate_goldstandard_pair",
    description=(
        "Regenerate a single question/answer pair. One LLM call, so expect it "
        "to take several seconds."
    ),
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False),
)
async def rag_regenerate_goldstandard_pair(session_id: str, pair_id: str) -> dict:
    return await ragclient.post(
        "/goldstandard/regenerate", json={"session_id": session_id, "pair_id": pair_id}
    )


@server.tool(
    name="rag_save_goldstandard",
    description="Save a gold-standard session to a named file on the server.",
    annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True),
)
async def rag_save_goldstandard(session_id: str, filename: str) -> dict:
    return await ragclient.post(
        "/goldstandard/save", json={"session_id": session_id, "filename": filename}
    )
