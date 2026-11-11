"""The running and recent jobs a caller may list (#617).

`GET /ingest/jobs`, `/export/jobs`, `/import/jobs` and `/goldstandard/sessions`
let a page find its jobs again after the user left it. What they list:

- **Whose.** An Engineer sees every job, including those recorded before
  #617 with no owner ("owner unknown"). Anyone else sees only jobs whose
  `owner` names them: the same name and kind, so a user's own jobs, or an API
  key's principal's. The lookups by ID are not scoped; only these lists are.
- **Which.** Every running job, and the finished ones whose finish time is no
  more than 24 hours before the request, a finish time later than the request
  (after the clock steps back) included; at most the newest 20 of those per
  list and caller.
- **Finish time.** A record's `finished_at`; for a job record from before #617,
  the time its record was last saved. A finished session without
  `finished_at` is not listed.
- **Order.** Newest first by `created_at`, else by finish time; neither, last.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from pydantic import ValidationError

from services import jobstore

_log = logging.getLogger(__name__)

WINDOW_S = 24 * 3600
FINISHED_CAP = 20


def job_owner(request) -> dict | None:
    """Who is making this request, as a job records it: name and kind. None
    for no request (a handler called directly, as some tests do)."""
    principal = getattr(getattr(request, "state", None), "auth", None)
    if principal is None:
        return None
    return {"name": principal.username,
            "kind": "service" if principal.kind == "service" else "user"}


def visible(owner, principal) -> bool:
    if principal.role == "engineer":
        return True
    if not isinstance(owner, dict):
        return False
    kind = "service" if principal.kind == "service" else "user"
    return owner.get("name") == principal.username and owner.get("kind") == kind


def _parse(value) -> float | None:
    """Epoch seconds for an ISO 8601 timestamp, or None."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat()


def select(items, principal, now: float) -> list[dict]:
    """The records to list, in order, each with `finished_at` as listed.

    `items` are `(record, running, finish)`, where `finish` is the finish time
    in epoch seconds of a finished record, or None when it has none.
    """
    running, finished = [], []
    for record, is_running, finish in items:
        if not visible(record.get("owner"), principal):
            continue
        if is_running:
            running.append((record, None))
        # No upper bound: a finish time ahead of the clock (a clock stepped
        # back) still lists a just-finished job (#629).
        elif finish is not None and now - WINDOW_S <= finish:
            finished.append((record, finish))
    finished.sort(key=lambda pair: pair[1], reverse=True)
    kept = running + finished[:FINISHED_CAP]

    def order(pair):
        record, finish = pair
        started = _parse(record.get("created_at"))
        moment = started if started is not None else finish
        return (moment is not None, moment or 0.0)

    kept.sort(key=order, reverse=True)
    out = []
    for record, finish in kept:
        entry = dict(record)
        entry["finished_at"] = _iso(finish) if finish is not None else None
        out.append(entry)
    return out


def job_items(rows) -> list[tuple[dict, bool, float | None]]:
    """`JobStore.snapshot()` rows as `select` items."""
    items = []
    for _, record, saved in rows:
        is_running = record.get("status") not in jobstore.TERMINAL
        finish = None if is_running else _parse(record.get("finished_at"))
        if not is_running and finish is None:
            finish = saved                      # a record from before #617
        items.append((record, is_running, finish))
    return items


def session_items(sessions) -> list[tuple[dict, bool, float | None]]:
    """Generated sessions as `select` items."""
    items = []
    for session in sessions:
        is_running = session.get("status") == "generating"
        finish = None if is_running else _parse(session.get("finished_at"))
        items.append((session, is_running, finish))
    return items


def entries(model, items, principal, id_key: str = "job_id") -> list:
    """The listed records as `model` entries. A record the model can't
    represent is left out and logged, so one bad record never fails the list."""
    out = []
    for record in select(items, principal, time.time()):
        try:
            out.append(model.model_validate(record))
        except ValidationError as exc:
            _log.warning("Left %r out of the job list: %d invalid field(s)",
                         record.get(id_key), exc.error_count())
    return out
