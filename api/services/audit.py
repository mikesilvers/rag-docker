"""The audit log (#29): who did what, to which data, with what result.

Append-only JSON Lines in `settings.audit_dir` (`/app/uploads/audit/` on the
ingest_uploads volume): `audit.jsonl`, rotated by size to `audit.jsonl.1`
(newest) ... `audit.jsonl.<keep>` (oldest), the oldest beyond that deleted.
Nothing edits or deletes a record otherwise. SPECIFICATIONS.md §3.1.9.

**What writes a record.** `AuditMiddleware` records each request to a route in
ACTIONS once its answer has been sent: the actor from the server-side
principal (never the client), the address nginx saw (X-Real-IP), the result
from the status and the error envelope's code. Handlers add what only they
know with `note()` (a collection named in a body, file names, a typed
username). A refusal by the auth middleware is recorded only when it is a 403
for an identified principal; 401s and 503s write nothing. Jobs get one record
when they are stored (`job_started`, called by the job store) and one when they
end (`job_ended`). The operator command and the API's start write their own.

**What never does.** No password, session token, API key, passphrase, answer
or document text reaches a record: handlers note named parameters only, and a
query's question text only when AUDIT_LOG_QUESTIONS is true.

**Writing never blocks an action.** A record that can't be written is one
ERROR line on the `audit` logger and one more in `failed_writes()`; the action
answers as it would have. The API (threads) and the operator command (another
process in the same container) append under a thread lock and an flock on
`.lock`, so each record is one whole line and rotation never races an append.
`id` and `time` are assigned under that lock, so the files' line order is the
order of ids, which is what makes `before` a stable cursor.
"""
from __future__ import annotations

import asyncio
import contextvars
import csv
import fcntl
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from config import settings

_log = logging.getLogger("audit")

FILE = "audit.jsonl"
# Every request route that is audited, by policy template, and its action name.
ACTIONS: dict[tuple[str, str], str] = {
    ("POST", "/auth/login"): "auth.login",
    ("POST", "/auth/logout"): "auth.logout",
    ("POST", "/auth/password"): "auth.password",
    ("POST", "/query"): "query",
    ("POST", "/collections"): "collection.create",
    ("DELETE", "/collections/{name}"): "collection.delete",
    ("POST", "/ingest/upload"): "ingest",
    ("POST", "/ingest/config"): "ingest.config",
    ("POST", "/retrieval/config"): "retrieval.config",
    ("POST", "/export"): "export",
    ("POST", "/import"): "import",
    ("POST", "/tune/rechunk"): "tune.rechunk",
    ("POST", "/tune/reembed"): "tune.reembed",
    ("POST", "/tune/reindex"): "tune.reindex",
    ("POST", "/goldstandard/save"): "goldstandard.save",
    ("GET", "/goldstandard/download/{filename}"): "goldstandard.download",
    ("POST", "/auth/users"): "admin.user.create",
    ("POST", "/auth/users/{username}/reset-password"): "admin.user.reset-password",
    ("POST", "/auth/users/{username}/role"): "admin.user.set-role",
    ("POST", "/auth/users/{username}/disable"): "admin.user.disable",
    ("POST", "/auth/users/{username}/enable"): "admin.user.enable",
    ("POST", "/auth/service-principals"): "admin.principal.create",
    ("POST", "/auth/service-principals/{name}/role"): "admin.principal.set-role",
    ("POST", "/auth/service-principals/{name}/disable"): "admin.principal.disable",
    ("POST", "/auth/service-principals/{name}/enable"): "admin.principal.enable",
    ("POST", "/auth/service-principals/{name}/keys"): "admin.key.issue",
    ("POST", "/auth/service-principals/{name}/keys/{key_id}/revoke"): "admin.key.revoke",
    ("GET", "/audit"): "audit.view",
    ("GET", "/audit.csv"): "audit.download",
}
OPERATOR_ACTIONS = ("bootstrap", "create-user", "reset-password", "set-role", "disable", "enable")
# Every action name, for the API's filter and the UI's list.
ALL_ACTIONS = tuple(sorted(set(ACTIONS.values()) | {"operator." + a for a in OPERATOR_ACTIONS}
                          | {"system.start"}))
RESULTS = ("success", "refused", "error", "accepted", "succeeded", "failed", "interrupted")
# A job's final status and the result its end record carries.
JOB_RESULT = {"completed": "succeeded", "failed": "failed", "partial": "failed", "cancelled": "failed"}
KEYS = ("id", "time", "actor", "actor_kind", "role", "key_id", "client_ip", "action", "collection", "target",
        "result", "status_code", "job_id", "details")
CSV_COLUMNS = KEYS[1:]
ID = re.compile(r"\d{19}-[0-9a-f]{6}")
# Enough of an error answer to read its envelope's code.
_BODY_LIMIT = 64 * 1024

_current: contextvars.ContextVar[dict | None] = contextvars.ContextVar("audit_request", default=None)
_lock = threading.Lock()
_last_ns = 0
_failed = 0


# ── handlers' notes ──────────────────────────────────────────────────────────

def note(collection: str | None = None, target: str | None = None, **details) -> None:
    """Add what only the handler knows to the current request's record. Does
    nothing outside an audited request. Never pass a secret."""
    ctx = _current.get()
    if ctx is None:
        return
    if collection is not None:
        ctx["collection"] = collection
    if target is not None:
        ctx["target"] = target
    ctx["details"].update(details)


def add_details(values: dict) -> None:
    """Add `values` to the current record's details only (their names may be
    ones note() would put in a column, such as an audit filter's collection)."""
    ctx = _current.get()
    if ctx is not None:
        ctx["details"].update(values)


def identify(username: str, role: str) -> None:
    """The actor of a public route that identifies one (a successful login)."""
    ctx = _current.get()
    if ctx is not None:
        ctx.update(actor=username, actor_kind="user", role=role)


# ── writing ──────────────────────────────────────────────────────────────────

def failed_writes() -> int:
    """Records this process couldn't write since it started."""
    return _failed


def question_text_logged() -> bool:
    return settings.audit_log_questions


def _next_id() -> tuple[str, str]:
    """(id, time). Called under the lock, so ids follow the files' line order;
    bumped past the last one so a coarse clock can't repeat or reverse."""
    global _last_ns
    now = max(time.time_ns(), _last_ns + 1)
    _last_ns = now
    stamp = datetime.fromtimestamp(now / 1e9, timezone.utc)
    return f"{now:019d}-{secrets.token_hex(3)}", stamp.strftime("%Y-%m-%dT%H:%M:%S.") + f"{stamp.microsecond // 1000:03d}Z"


def _rotate(folder: Path) -> None:
    keep = settings.audit_log_keep
    (folder / f"{FILE}.{keep}").unlink(missing_ok=True)
    for n in range(keep - 1, 0, -1):
        older = folder / f"{FILE}.{n}"
        if older.exists():
            os.replace(older, folder / f"{FILE}.{n + 1}")
    os.replace(folder / FILE, folder / f"{FILE}.1")


def _append(fields: dict) -> None:
    folder = Path(settings.audit_dir)
    with _lock:
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_fd = os.open(folder / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            record_id, stamp = _next_id()
            record = {key: None for key in KEYS}
            record.update(fields, id=record_id, time=stamp)
            line = (json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode()
            fd = os.open(folder / FILE, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                end = os.fstat(fd).st_size
                if end and os.pread(fd, 1, end - 1) != b"\n":
                    line = b"\n" + line              # after a torn last line, start a new one
                os.write(fd, line)
                size = os.fstat(fd).st_size
            finally:
                os.close(fd)
            if size >= settings.audit_log_max_bytes:
                _rotate(folder)
        finally:
            os.close(lock_fd)                        # releases the flock too


def record(action: str, *, result: str, actor: str | None = None, actor_kind: str = "anonymous",
           role: str | None = None, key_id: str | None = None, client_ip: str | None = None,
           collection: str | None = None, target: str | None = None, status_code: int | None = None,
           job_id: str | None = None, details: dict | None = None) -> bool:
    """Append one record. Never raises: a failure is logged and counted, and
    the caller's action goes on as if the record had been written."""
    global _failed
    try:
        _append({"action": action, "result": result, "actor": actor, "actor_kind": actor_kind, "role": role,
                 "key_id": key_id, "client_ip": client_ip, "collection": collection, "target": target,
                 "status_code": status_code, "job_id": job_id, "details": dict(details or {})})
        return True
    except Exception as exc:                          # noqa: BLE001 - logged and counted, never raised
        with _lock:
            _failed += 1
        _log.error("Could not write the audit record of %s: %s", action, type(exc).__name__)
        return False


def _identify(ctx: dict) -> bool:
    """Take the actor from the request's principal: the one the auth middleware
    let through, or the one it refused with a 403. True when there is one."""
    state = ctx["scope"].get("state") or {}
    principal = state.get("auth") or state.get("audit_refused")
    if principal is None:
        return False
    ctx.update(actor=principal.username, role=principal.role, key_id=principal.key_id,
               actor_kind="service" if principal.kind == "service" else "user")
    return True


def _actor(ctx: dict) -> dict:
    return {k: ctx.get(k) for k in ("actor", "actor_kind", "role", "key_id", "client_ip")}


def _result(status: int) -> str:
    return "success" if status < 400 else "refused" if status < 500 else "error"


# ── jobs ─────────────────────────────────────────────────────────────────────

def job_started(job_id: str) -> dict | None:
    """Called by the job store when a new job is stored. Inside an audited
    request, writes the `accepted` record, before the job can end, and returns
    what its end record needs; otherwise None."""
    ctx = _current.get()
    if ctx is None or not _identify(ctx):
        return None
    ctx["job_recorded"] = True
    snapshot = {**_actor(ctx), "action": ctx["action"], "collection": ctx.get("collection"),
                "target": ctx.get("target"), "details": dict(ctx["details"])}
    record(snapshot["action"], result="accepted", status_code=202, job_id=job_id,
           **{k: v for k, v in snapshot.items() if k != "action"})
    return snapshot


def job_ended(job: dict, interrupted: bool) -> None:
    """The end record of a job whose start was recorded (job["_audit"])."""
    snapshot = job.get("_audit") or {}
    status = job.get("status")
    details = {**snapshot.get("details", {}), "job_status": status}
    collection = snapshot.get("collection")
    if snapshot.get("action") == "export":
        details.update(package=job.get("filename"), fidelity=job.get("fidelity"))
    elif snapshot.get("action") == "import":
        collection = job.get("collection") or collection
    record(snapshot.get("action", "unknown"), result="interrupted" if interrupted else JOB_RESULT.get(status, "failed"),
           job_id=job.get("job_id") or getattr(job, "_id", None), collection=collection, target=snapshot.get("target"),
           details=details, **{k: snapshot.get(k) for k in ("actor", "actor_kind", "role", "key_id", "client_ip")})


# ── reading ──────────────────────────────────────────────────────────────────

def _files() -> list[Path]:
    """The log's files, newest first."""
    folder = Path(settings.audit_dir)
    names = [folder / FILE] + [folder / f"{FILE}.{n}" for n in range(1, settings.audit_log_keep + 1)]
    return [p for p in names if p.is_file()]


def _open_all() -> list:
    """Every file of the log, opened for reading, newest first: one consistent
    set. Opened under the writers' locks, so no rotation runs meanwhile; read
    after they are released, which is safe because a rename doesn't change
    what an open file holds. Without this a read that spans a rotation would
    read one file twice and miss another."""
    folder = Path(settings.audit_dir)
    if not folder.is_dir():
        return []
    handles = []
    with _lock:
        lock_fd = os.open(folder / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
            try:
                for path in _files():
                    handles.append(open(path, "rb"))
            except BaseException:
                for handle in handles:
                    handle.close()
                raise
        finally:
            os.close(lock_fd)                        # releases the flock too
    return handles


def newest_first():
    """Every record, newest first, from the files as they were when the read
    started (_open_all). A line that isn't a JSON object (a torn last line
    after a crash) is skipped."""
    handles = _open_all()
    try:
        for handle in handles:
            data = handle.read()
            handle.close()
            for raw in reversed(data.split(b"\n")):
                if not raw.strip():
                    continue
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    yield item
    finally:
        for handle in handles:
            handle.close()


def parse_time(value: str) -> datetime:
    """ISO 8601; without an offset it is UTC. ValueError when it isn't one."""
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00").replace("z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _record_time(item: dict) -> datetime | None:
    try:
        return parse_time(item["time"])
    except (KeyError, TypeError, ValueError):
        return None


def matching(filters: dict, before: str | None = None):
    """Records matching `filters` (from, to, actor, action, collection, result),
    newest first, with ids below `before` if given."""
    start, end = filters.get("from"), filters.get("to")
    exact = {k: filters[k] for k in ("actor", "action", "collection", "result") if filters.get(k) is not None}
    for item in newest_first():
        if before is not None and item["id"] >= before:
            continue
        at = _record_time(item)
        if start is not None and (at is None or at < start):
            if at is not None:
                return                                # older from here on
            continue
        if end is not None and (at is None or at >= end):
            continue
        if all(item.get(k) == v for k, v in exact.items()):
            yield item


def page(filters: dict, limit: int, before: str | None) -> tuple[list[dict], str | None]:
    """Up to `limit` records and the cursor of the next page (None at the end)."""
    found = []
    for item in matching(filters, before):
        found.append({key: item.get(key) for key in KEYS})
        if len(found) > limit:
            break
    more = len(found) > limit
    found = found[:limit]
    return found, (found[-1]["id"] if more and found else None)


def _cell(value) -> str:
    if value is None:
        return ""
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":")) if isinstance(value, dict) else str(value)
    # A spreadsheet runs a cell that starts with one of these as a formula.
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def csv_lines(filters: dict):
    """The CSV: the header, then every matching record, newest first."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)

    def line(row) -> str:
        buffer.seek(0)
        buffer.truncate()
        writer.writerow(row)
        return buffer.getvalue()

    yield line(CSV_COLUMNS)
    for item in matching(filters):
        yield line([_cell(item.get(key)) for key in CSV_COLUMNS])


# ── the request middleware ───────────────────────────────────────────────────

def _compile(template: str):
    pattern = re.sub(r"\\\{([a-z_]+)\\\}", r"(?P<\1>[^/]+)", re.escape(template))
    return re.compile(pattern + r"\Z")


_PATTERNS = {key: _compile(key[1]) for key in ACTIONS}


def _from_path(method: str, template: str, path: str, ctx: dict) -> None:
    found = _PATTERNS[(method, template)].match(path)
    params = found.groupdict() if found else {}
    if template.startswith("/collections/") and "name" in params:
        ctx["collection"] = params["name"]
    elif template.startswith("/auth/"):
        if "username" in params or "name" in params:
            ctx["target"] = params.get("username") or params.get("name")
        if "key_id" in params:
            ctx["details"]["key_id"] = params["key_id"]
    if "filename" in params:
        ctx["details"]["file"] = params["filename"]


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


def _error_code(body: bytes) -> str | None:
    try:
        code = json.loads(body)["error"]["code"]
    except (ValueError, KeyError, TypeError):
        return None
    return code if isinstance(code, str) else None


class AuditMiddleware:
    """Records each audited request once its answer is sent. Runs outside the
    auth middleware, so it sees that middleware's refusals too."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        from auth import policy
        method = "GET" if scope["method"] == "HEAD" else scope["method"]
        template = policy.match(scope["method"], scope["path"])
        action = ACTIONS.get((method, template)) if template else None
        if action is None:
            return await self.app(scope, receive, send)
        client = scope.get("client")
        ctx = {"scope": scope, "action": action, "actor": None, "actor_kind": "anonymous", "role": None, "key_id": None,
               "client_ip": _header(scope, b"x-real-ip") or (client[0] if client else None),
               "collection": None, "target": None, "details": {}, "job_recorded": False}
        _from_path(method, template, scope["path"], ctx)
        seen = {"status": None, "body": b""}

        async def watched(message):
            if message["type"] == "http.response.start":
                seen["status"] = message["status"]
            elif (message["type"] == "http.response.body" and (seen["status"] or 0) >= 400
                  and len(seen["body"]) < _BODY_LIMIT):
                seen["body"] += message.get("body", b"")[:_BODY_LIMIT - len(seen["body"])]
            await send(message)

        token = _current.set(ctx)
        try:
            await self.app(scope, receive, watched)
        except BaseException:
            if seen["status"] is None:
                seen["status"] = 500
            raise
        finally:
            _current.reset(token)
            try:
                await self._write(scope, ctx, seen)
            except Exception:                         # noqa: BLE001 - never breaks the answer
                _log.exception("Could not build the audit record of %s", action)

    @staticmethod
    async def _write(scope, ctx, seen):
        if not _identify(ctx):
            # Nobody identified: a 401 or 503 from the auth middleware writes
            # nothing (D4). Only a login, which is public, is recorded without
            # a principal: a success names its account, a failure the username
            # as typed.
            if ctx["action"] != "auth.login" or (ctx["actor"] is None and ctx["target"] is None):
                return
        status = seen["status"] or 500
        if ctx["job_recorded"] and status == 202:
            return                                    # job_started wrote the accepted record
        details = dict(ctx["details"])
        if status >= 400:
            code = _error_code(seen["body"])
            if code:
                details["code"] = code
        await asyncio.to_thread(record, ctx["action"], result=_result(status), status_code=status,
                                collection=ctx["collection"], target=ctx["target"], details=details,
                                **_actor(ctx))
