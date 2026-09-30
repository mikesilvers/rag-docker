from __future__ import annotations
import asyncio
import json
import logging

import httpx
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from config import settings
from models.schemas import SessionResponse
from services import ollama_client as ollama
from services import weaviate_client as wc

GS_SYSTEM = (
    "You are creating evaluation data for a RAG system. Given a text chunk, generate one question "
    "that can be answered from this chunk, the correct answer based only on this chunk, and a ground "
    "truth answer (same as the answer). Return a JSON object with keys: question, answer, ground_truth. "
    "Do not include any text outside the JSON object."
)
GS_RETRY_SUFFIX = (
    " Your previous response was not valid JSON. Return ONLY the JSON object with keys: "
    "question, answer, ground_truth. No markdown, no explanation."
)

log = logging.getLogger(__name__)


class GoldStandardError(Exception):
    """Carries an API error code so the router does not have to guess."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

_sessions: dict[str, dict] = {}
_tasks: set[asyncio.Task] = set()
_SESSION_ID = re.compile(r"gs_[0-9a-f]{8}")


def validate_session_id(session_id: str) -> None:
    """Imported identities use the same grammar as locally generated ones."""
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        raise ValueError("Invalid evaluation session ID.")


def validate_session(session: dict) -> None:
    """Validate without normalising or dropping historical validity metadata."""
    SessionResponse.model_validate(session, strict=True)
    validate_session_id(session["session_id"])


def _session_storage_root() -> Path:
    upload = Path(settings.upload_dir).resolve()
    p = upload / "goldstandard_sessions"
    if p.is_symlink() or (p.exists() and not p.is_dir()):
        raise ValueError("Evaluation session storage is not a regular directory.")
    root = p.resolve()
    if root.parent != upload:
        raise ValueError("Evaluation session storage is outside the upload directory.")
    return root


def _sessions_dir() -> Path:
    p = _session_storage_root()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _session_path(session_id: str) -> Path:
    validate_session_id(session_id)
    # Preflight must be read-only; the writer creates the directory only after
    # every imported session has been checked.
    root = _session_storage_root()
    candidate = root / f"{session_id}.json"
    if candidate.is_symlink() or (candidate.exists() and not candidate.is_file()):
        raise ValueError("Evaluation session destination is not a regular file.")
    target = candidate.resolve()
    # Grammar prevents metadata-derived paths; containment also refuses an
    # existing file symlink which would redirect a valid identity's write.
    if target.parent != root:
        raise ValueError("Evaluation session destination is outside session storage.")
    return target


def _save_session_sync(session: dict) -> None:
    path = _session_path(session["session_id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session, indent=2))


async def _save_session(session: dict) -> None:
    await asyncio.to_thread(_save_session_sync, session)


def _sessions_on_disk() -> list[dict]:
    """Leave invalid legacy files untouched and reported, outside the cache.

    A previously accepted ID must not make post-deletion flagging raise after
    the collection has already gone. Every disk reader uses this same boundary.
    """
    try:
        paths = sorted(_session_storage_root().glob("*.json"))
    except (OSError, ValueError, RuntimeError):
        log.exception("Cannot read evaluation session storage; existing files are unchanged")
        return []
    sessions = []
    for p in paths:
        try:
            if p.is_symlink() or not p.is_file():
                raise ValueError("Evaluation session must be a regular file.")
            data = json.loads(p.read_text())
            validate_session(data)
            if p != _session_path(data["session_id"]):
                raise ValueError("Evaluation session filename does not match its identity.")
        except (OSError, ValueError, RuntimeError) as exc:
            # ValidationError text can include document-derived field values.
            log.warning("Skipping invalid evaluation session %s; file is unchanged (%s)",
                        p.name, type(exc).__name__)
            continue
        sessions.append(data)
    return sessions


def load_sessions_from_disk() -> None:
    for data in _sessions_on_disk():
        _sessions[data["session_id"]] = data


def sessions_for(collection: str) -> list[dict]:
    """Every session generated against a collection, in-memory and on disk."""
    found = {}
    for sid, sess in list(_sessions.items()):
        if sess.get("collection") != collection:
            continue
        try:
            validate_session(sess)
            if sid != sess["session_id"]:
                raise ValueError("Cached evaluation identity does not match its key.")
            _session_path(sid)
        except (OSError, ValueError, RuntimeError) as exc:
            log.warning("Skipping invalid cached evaluation session %s (%s)",
                        sid, type(exc).__name__)
            continue
        found[sid] = sess
    # A session written by an import may not be in memory yet.
    for data in _sessions_on_disk():
        if data.get("collection") == collection and data["session_id"] not in found:
            found[data["session_id"]] = data
            _sessions[data["session_id"]] = data
    return list(found.values())


def store_session(session: dict) -> None:
    """Write a session to disk *and* into the in-memory cache.

    Anything outside this module that writes a session file directly will be
    silently undone: the cache still holds the previous version, and the next
    flagging pass writes that back over the file. Import learned this the hard
    way — a restored session reverted to its pre-import orphaned state.
    """
    validate_session(session)
    _save_session_sync(session)
    _sessions[session["session_id"]] = session


def _flag_sessions(collection: str, flag: str, reason: str) -> int:
    """Mark every session for a collection, on disk and in memory.

    Sessions are never deleted and never remapped. A remap that guesses which
    new chunk replaces an old one corrupts an evaluation baseline silently,
    which is worse than an honest flag the user can act on.
    """
    now = datetime.now(timezone.utc).isoformat()
    marked = 0
    for session in sessions_for(collection):
        session[flag] = True
        session[f"{flag}_reason"] = reason
        session[f"{flag}_at"] = now
        try:
            _save_session_sync(session)
        except (OSError, ValueError, RuntimeError):
            log.exception("Could not flag gold-standard session %s", session["session_id"])
            continue
        marked += 1
    return marked


def mark_stale(collection: str, reason: str) -> int:
    """Chunk identity changed, so the pairs no longer describe what is stored."""
    return _flag_sessions(collection, "stale", reason)


def mark_orphaned(collection: str, reason: str) -> int:
    """The collection is gone. Retained rather than deleted — see spec §8 rule 4."""
    return _flag_sessions(collection, "orphaned", reason)


def get_session(session_id: str) -> dict | None:
    return _sessions.get(session_id)


def _parse_gs_json(text: str) -> dict:
    """Pull the JSON object out of a model reply.

    The model reliably returns a correct object and then keeps talking --
    "Extra data: line 6 column 1" was the single most common generation
    failure, costing pairs on nearly every session. `raw_decode` reads the
    leading value and ignores whatever follows, so trailing commentary is no
    longer fatal. A reply that opens with prose is still handled, by starting
    at the first brace.
    """
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
    text = re.sub(r"\n?```$", "", text)
    text = text.strip()

    decoder = json.JSONDecoder()
    positions = [i for i, ch in enumerate(text) if ch == "{"]
    if not positions:
        raise ValueError("model reply contained no JSON object")
    # Anchoring on the first brace is not enough: a reply that explains itself
    # first ("return an object like { this }") puts a brace before the real
    # payload. Try each candidate and keep the first that decodes.
    last_error: Exception | None = None
    for start in positions:
        try:
            result, _ = decoder.raw_decode(text[start:])
        except ValueError as exc:
            last_error = exc
            continue
        if isinstance(result, dict):
            return result
        last_error = ValueError(f"Expected JSON object, got {type(result).__name__}")
    raise last_error or ValueError("model reply contained no usable JSON object")


async def _chat_once(system: str, user: str) -> str:
    """One chat call, retried once if the transport fails.

    Ollama serialises requests per model, so generating while someone is
    querying can push a call past the client timeout. `httpx.ReadTimeout`
    carries an empty message, which is why these used to be recorded as an
    empty string. A transient timeout should cost a retry, not a pair.
    """
    try:
        return await ollama.chat(system, user)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        log.warning("Ollama call failed (%s); retrying once", type(exc).__name__)
        return await ollama.chat(system, user)


# One initial attempt plus two reprompts. The model's failure mode is malformed
# JSON (a missing comma, an unterminated string), which is independent between
# attempts, so a second reprompt converts most remaining failures into pairs.
# Each attempt costs an LLM call, so the budget is small and fixed.
_GENERATION_ATTEMPTS = 3


async def _generate_pair(chunk: dict) -> dict:
    user_msg = f"Chunk:\n{chunk['content']}"
    data = None
    last_error: Exception | None = None
    for attempt in range(_GENERATION_ATTEMPTS):
        system = GS_SYSTEM if attempt == 0 else GS_SYSTEM + GS_RETRY_SUFFIX
        raw = await _chat_once(system, user_msg)
        try:
            data = _parse_gs_json(raw)
            break
        except Exception as exc:                      # noqa: BLE001
            last_error = exc
            log.info("Pair generation attempt %d/%d did not yield valid JSON: %s",
                     attempt + 1, _GENERATION_ATTEMPTS, exc)
    if data is None:
        raise last_error or ValueError("no usable reply from the model")

    return {
        "pair_id": f"p_{uuid.uuid4().hex[:8]}",
        "question": str(data.get("question", "")),
        "answer": str(data.get("answer", "")),
        "contexts": [chunk["content"]],
        "ground_truth": str(data.get("ground_truth", data.get("answer", ""))),
        "source_file": chunk.get("source_file", ""),
        "chunk_index": int(chunk.get("chunk_index", 0)),
        "status": "pending",
    }


async def _run_generation(session_id: str, chunks: list[dict]) -> None:
    session = _sessions[session_id]
    cancelled = False
    try:
        for chunk in chunks:
            try:
                pair = await _generate_pair(chunk)
                session["pairs"].append(pair)
                session["pairs_completed"] += 1
                await _save_session(session)
            except asyncio.CancelledError:
                cancelled = True
                raise
            except Exception as exc:
                # Some of these carry an empty str(), which produced sessions
                # whose only record of a lost pair was an empty string.
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.warning("Gold-standard pair generation failed: %s", reason, exc_info=True)
                session["pairs_failed"] = session.get("pairs_failed", 0) + 1
                session.setdefault("errors", []).append(reason)
                try:
                    await _save_session(session)
                except Exception:
                    pass
            finally:
                if not cancelled:
                    session["pairs_attempted"] = session.get("pairs_attempted", 0) + 1
    finally:
        if cancelled:
            if session.get("status") == "generating":
                session["status"] = "cancelled"
                _save_session_sync(session)
        else:
            if session.get("status") == "generating":
                if not session["pairs"] and session.get("errors"):
                    session["status"] = "failed"
                else:
                    session["status"] = "completed"
            try:
                await _save_session(session)
            except Exception:
                _save_session_sync(session)


async def start_generation(
    collection: str,
    sample_size: int,
    seed: int | None,
) -> dict:
    from models.schemas import GenerateRequest
    request = GenerateRequest(collection=collection, sample_size=sample_size, seed=seed)
    all_chunks = await wc.sample_chunks(collection, limit=request.sample_size, seed=request.seed)
    actual_size = len(all_chunks)

    session_id = f"gs_{uuid.uuid4().hex[:8]}"
    session = {
        "session_id": session_id,
        "collection": collection,
        "status": "generating",
        "pairs_total": actual_size,
        # `attempted` drives progress and always reaches `total`; `completed`
        # counts pairs that actually exist. Reporting one number for both made
        # a session with a failed pair read "3/3" while holding 2.
        "pairs_attempted": 0,
        "pairs_completed": 0,
        "pairs_failed": 0,
        "pairs": [],
    }
    _sessions[session_id] = session
    await _save_session(session)

    task = asyncio.create_task(_run_generation(session_id, all_chunks))
    _tasks.add(task)

    def _on_task_done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        exc = t.exception() if not t.cancelled() else None
        if exc is not None:
            s = _sessions.get(session_id)
            if s and s.get("status") == "generating":
                s["status"] = "failed"
                s.setdefault("errors", []).append(str(exc))
                _save_session_sync(s)

    task.add_done_callback(_on_task_done)

    return {
        "session_id": session_id,
        "status": "generating",
        "pairs_total": actual_size,
        "pairs_completed": 0,
    }


async def update_pair(session_id: str, pair_id: str, updates: dict) -> dict | None:
    session = _sessions.get(session_id)
    if session is None:
        return None
    for pair in session["pairs"]:
        if pair["pair_id"] == pair_id:
            pair.update({k: v for k, v in updates.items() if v is not None})
            await _save_session(session)
            return pair
    return None


async def regenerate_pair(session_id: str, pair_id: str) -> dict | None:
    session = _sessions.get(session_id)
    if session is None:
        return None
    if session.get("status") == "generating":
        # The generation loop is appending to session["pairs"] and saving it;
        # regenerating underneath that races with it and can lose a pair.
        raise GoldStandardError(
            "GENERATION_IN_PROGRESS",
            f"Session '{session_id}' is still generating. Wait for it to finish "
            "before regenerating a pair.", 409)
    for i, pair in enumerate(session["pairs"]):
        if pair["pair_id"] == pair_id:
            chunk = {
                "content": pair["contexts"][0],
                "source_file": pair["source_file"],
                "chunk_index": pair["chunk_index"],
            }
            try:
                new_pair = await _generate_pair(chunk)
            except Exception as exc:                  # noqa: BLE001
                # The model regularly returns unparseable JSON. Generation
                # records that and moves on; regeneration used to let it escape
                # as a bare 500. Some of these carry an empty str(), so the
                # type name is always included or the message says nothing.
                reason = f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__
                log.warning("Regeneration of pair %s failed: %s", pair_id, reason, exc_info=True)
                raise GoldStandardError(
                    "PAIR_GENERATION_FAILED",
                    f"The model did not return a usable question/answer pair "
                    f"({reason}). The existing pair is unchanged; try again.", 502) from exc
            new_pair["pair_id"] = pair_id
            session["pairs"][i] = new_pair
            await _save_session(session)
            return new_pair
    return None


def _save_export_sync(out_path: Path, ragas: list[dict]) -> None:
    out_path.write_text(json.dumps(ragas, indent=2))


async def save_session(session_id: str, filename: str | None, allow_historical: bool = False) -> dict | None:
    session = _sessions.get(session_id)
    if session is None:
        return None

    from models.schemas import SessionValidity
    if not isinstance(allow_historical, bool):
        raise ValueError("allow_historical must be a boolean")
    validity = SessionValidity.model_validate(session).model_dump()
    historical = validity["stale"] or validity["orphaned"]
    if historical and not allow_historical:
        raise GoldStandardError(
            "HISTORICAL_SESSION",
            "This retained session is stale or orphaned and is not a current "
            "collection baseline. Inspect its validity metadata and explicitly "
            "set allow_historical=true to export historical pairs.", 409)

    approved = [p for p in session["pairs"] if p["status"] in ("approved", "edited")]
    excluded = len(session["pairs"]) - len(approved)

    collection = session.get("collection", "export")
    if not filename:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        safe = re.sub(r"[^a-zA-Z0-9_]", "", collection.replace(" ", "_"))
        filename = f"{safe}_{ts}.json"

    # Sanitize: only the basename; no path traversal
    filename = Path(filename).name
    out_dir = Path(settings.upload_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = (out_dir / filename).resolve()
    if not str(out_path).startswith(str(out_dir) + "/"):
        raise ValueError("Invalid filename.")

    ragas = [
        {
            "question": p["question"],
            "answer": p["answer"],
            "contexts": p["contexts"],
            "ground_truth": p["ground_truth"],
        }
        for p in approved
    ]
    await asyncio.to_thread(_save_export_sync, out_path, ragas)

    return {
        "filename": filename,
        "pairs_saved": len(approved),
        "pairs_excluded": excluded,
        "download_url": f"/api/goldstandard/download/{filename}",
        "historical": historical,
        "session_validity": validity,
    }
