from __future__ import annotations
from fastapi.responses import JSONResponse


def api_error(status_code: int, code: str, message: str, detail=None) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message, "detail": detail}},
    )


def collection_deleting_error(name: str) -> JSONResponse:
    """A new job refused because a delete of its collection is in progress (#302)."""
    from services.collection_writes import deleting_message
    return api_error(409, "COLLECTION_DELETING", deleting_message(name),
                     detail={"collection": name})


def collection_check_unavailable(name: str, outcome: str) -> JSONResponse:
    """A request refused because Weaviate couldn't say whether its collection exists (#366)."""
    return api_error(503, "SERVICE_UNAVAILABLE",
                     f"Cannot check whether '{name}' exists: Weaviate could not be read. "
                     f"{outcome}; try again when Weaviate is available.",
                     detail={"collection": name})


def collection_not_found(name: str) -> JSONResponse:
    """A collection that doesn't exist, or one the caller may not use (#606)."""
    return api_error(404, "COLLECTION_NOT_FOUND", f"Collection '{name}' not found.")


async def usable_collection(request, name: str, outcome: str) -> JSONResponse | None:
    """For the settings routes (#606): None when the caller may use the
    collection and it exists; otherwise the answer to send, the same 404 for a
    refused collection as for a missing one, or the 503 when Weaviate can't say."""
    import logging
    from auth import access
    from services import weaviate_client as wc
    if not await access.check(request, name):
        return collection_not_found(name)
    try:
        exists = await wc.collection_exists(name)
    except Exception:                                 # noqa: BLE001
        # Weaviate couldn't be asked; the cause is logged, not sent (#366).
        logging.getLogger(__name__).exception("Could not check whether %r exists for its settings", name)
        return collection_check_unavailable(name, outcome)
    return None if exists else collection_not_found(name)
