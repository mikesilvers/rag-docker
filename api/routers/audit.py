"""The audit log's API (#29): AI Engineers, browser sessions only (auth/policy.py
puts both routes in HUMAN_ONLY, so every API key gets 403 KEY_NOT_ALLOWED).

    GET /audit       a page of records, newest first, with a cursor
    GET /audit.csv   every matching record as CSV

Both take the filters from, to (ISO 8601; without an offset, UTC; from
inclusive, to exclusive), actor, action, collection and result (exact matches);
/audit also limit (1-1000, default 100) and before (a record id: the records
older than it). Parameters are read from the query string by name, since
`from` can't be a Python parameter. Each route is itself recorded (audit.view,
audit.download) once it has answered, so a page or a file never holds its own
record. SPECIFICATIONS.md §3.1.9.
"""
from __future__ import annotations

import asyncio
import re
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool

from services import audit
from utils import api_error

router = APIRouter()
_NO_STORE = {"Cache-Control": "no-store"}
FILTERS = ("from", "to", "actor", "action", "collection", "result")


class _Invalid(ValueError):
    pass


def _read(request: Request, names) -> dict:
    """The named parameters as given, and a note of those that were set."""
    given = {name: request.query_params.get(name) for name in names}
    audit.add_details({name: value for name, value in given.items() if value not in (None, "")})
    return given


def _filters(given: dict) -> dict:
    filters = {name: given[name] or None for name in ("actor", "action", "collection", "result")}
    if filters["result"] is not None and filters["result"] not in audit.RESULTS:
        raise _Invalid("result must be one of " + ", ".join(audit.RESULTS) + ".")
    for name in ("from", "to"):
        filters[name] = None
        if given[name]:
            try:
                filters[name] = audit.parse_time(given[name])
            except ValueError:
                raise _Invalid(f"{name} must be an ISO 8601 date and time.") from None
    return filters


@router.get("/audit")
async def audit_page(request: Request):
    given = _read(request, FILTERS + ("limit", "before"))
    try:
        filters = _filters(given)
        limit = 100
        if given["limit"] is not None:
            # ASCII digits only: str.isdigit() also accepts characters such as
            # "²" that int() rejects, which would answer 500.
            if not re.fullmatch(r"[0-9]+", given["limit"]) or not 1 <= int(given["limit"]) <= 1000:
                raise _Invalid("limit must be a whole number from 1 to 1000.")
            limit = int(given["limit"])
        before = given["before"]
        if before is not None and not audit.ID.fullmatch(before):
            raise _Invalid("before must be a record id.")
    except _Invalid as exc:
        return api_error(422, "INVALID_PARAMETER", str(exc))
    records, next_before = await asyncio.to_thread(audit.page, filters, limit, before)
    return JSONResponse({"records": records, "next_before": next_before,
                         "failed_writes": audit.failed_writes(),
                         "question_text_logged": audit.question_text_logged()}, headers=_NO_STORE)


@router.get("/audit.csv")
async def audit_csv(request: Request):
    given = _read(request, FILTERS)
    try:
        filters = _filters(given)
    except _Invalid as exc:
        return api_error(422, "INVALID_PARAMETER", str(exc))
    name = time.strftime("audit-%Y%m%dT%H%M%SZ.csv", time.gmtime())
    return StreamingResponse(iterate_in_threadpool(audit.csv_lines(filters)), media_type="text/csv; charset=utf-8",
                             headers={**_NO_STORE, "Content-Disposition": f'attachment; filename="{name}"'})
