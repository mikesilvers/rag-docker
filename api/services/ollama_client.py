from __future__ import annotations
from services import telemetry
import logging
import time

import httpx

from config import settings

_BASE = f"http://{settings.ollama_host}:{settings.ollama_port}"
_log = logging.getLogger(__name__)

# Set once /api/embeddings answers 404: an Ollama that has dropped the older
# endpoint, superseded by /api/embed (#673). Every later embedding goes to
# /api/embed, the endpoint Weaviate's text2vec-ollama calls.
_use_embed = False


@telemetry.traced("ollama.embed")
async def embed(text: str) -> list[float]:
    # keep_alive only: the embedding model keeps its own context (num_ctx), as
    # Weaviate's own embedding calls do (#673).
    global _use_embed
    async with httpx.AsyncClient(timeout=60.0) as client:
        if not _use_embed:
            resp = await client.post(
                f"{_BASE}/api/embeddings",
                json={"model": settings.embed_model, "prompt": text,
                      "keep_alive": settings.ollama_keep_alive},
            )
            if resp.status_code != 404:
                resp.raise_for_status()
                return resp.json()["embedding"]
            _use_embed = True
            _log.warning("Ollama answered 404 for /api/embeddings; using /api/embed from now on")
        resp = await client.post(
            f"{_BASE}/api/embed",
            json={"model": settings.embed_model, "input": text,
                  "keep_alive": settings.ollama_keep_alive},
        )
        resp.raise_for_status()
        return resp.json()["embeddings"][0]


async def _chat(model: str, system: str, user: str) -> str:
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(
            f"{_BASE}/api/chat",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "stream": False,
                # In the request, never on the server, which may be shared (#673).
                "options": {"num_ctx": settings.ollama_num_ctx},
                "keep_alive": settings.ollama_keep_alive,
            },
        )
        resp.raise_for_status()
        return resp.json()["message"]["content"]


@telemetry.traced("ollama.chat")
async def chat(system: str, user: str) -> str:
    """A call to the answer model: query's reformulation and answer (#671)."""
    return await _chat(settings.answer_model, system, user)


@telemetry.traced("ollama.chat")
async def build_chat(system: str, user: str) -> str:
    """A call to the build model: preparation and gold standard generation (#671)."""
    return await _chat(settings.build_model, system, user)


def _tagged(model: str) -> str:
    """`name:tag`, with `:latest` for a name without a tag (after its last `/`)."""
    return model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"


def same_model(a: str, b: str) -> bool:
    """Whether two model names are one model: `x` and `x:latest` are."""
    return _tagged(a) == _tagged(b)


def required_models() -> list[str]:
    """The embedding, answer and build models, `name:tag`, each once, in that order."""
    seen: list[str] = []
    for model in (settings.embed_model, settings.answer_model, settings.build_model):
        if _tagged(model) not in seen:
            seen.append(_tagged(model))
    return seen


@telemetry.traced("ollama.models")
async def list_models() -> set[str]:
    """Full names (`name:tag`) of the models Ollama reports."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_BASE}/api/tags")
        resp.raise_for_status()
        return {m["name"] for m in resp.json().get("models", [])}


async def missing_models() -> list[str] | None:
    """The required models Ollama doesn't list, tag-exact (#673), or None when it
    can't be asked."""
    try:
        reported = {_tagged(name) for name in await list_models()}
    except Exception:                                 # noqa: BLE001
        return None
    return [model for model in required_models() if model not in reported]


@telemetry.traced("ollama.models")
async def version() -> str | None:
    """The running Ollama's version (`GET /api/version`), or None (#673)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{_BASE}/api/version")
            resp.raise_for_status()
            value = resp.json().get("version")
            return value if isinstance(value, str) and value else None
    except Exception:                                 # noqa: BLE001
        return None


@telemetry.traced("ollama.models")
async def loaded_models() -> list[dict]:
    """What Ollama has loaded (`GET /api/ps`), for information only (#673).

    The Ollama may be shared with other apps: nothing here, or anywhere in
    rag-docker, unloads or changes another model."""
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{_BASE}/api/ps")
        resp.raise_for_status()
        return [{key: m.get(key) for key in ("name", "size", "size_vram", "expires_at")}
                for m in resp.json().get("models", []) if isinstance(m, dict)]


@telemetry.traced("ollama.health")
async def check_health(with_version: bool = True) -> dict:
    """`llm` is the answer model, `build_llm` the build model (#671). A model is
    present only when Ollama lists its exact `name:tag` (#673). `with_version`
    adds `version` (`GET /api/version`), for /health/details."""
    answer, build = settings.answer_model, settings.build_model
    same = same_model(answer, build)
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{_BASE}/api/tags")
            resp.raise_for_status()
            latency_ms = int((time.monotonic() - start) * 1000)
            models = {_tagged(m["name"]) for m in resp.json().get("models", [])}

            def entry(model: str) -> dict:
                ok = _tagged(model) in models
                return {"status": "ok" if ok else "error", "latency_ms": latency_ms, "model": model}

            result = {"llm": entry(answer), "build_llm": entry(build),
                      "embed": entry(settings.embed_model), "same_model": same}
            if any(result[k]["status"] != "ok" for k in ("llm", "build_llm", "embed")):
                telemetry.outcome("error")
    except Exception as exc:
        telemetry.outcome("error", exc)
        latency_ms = int((time.monotonic() - start) * 1000)
        return {
            "llm": {"status": "error", "latency_ms": latency_ms, "model": answer},
            "build_llm": {"status": "error", "latency_ms": latency_ms, "model": build},
            "embed": {"status": "error", "latency_ms": latency_ms, "model": settings.embed_model},
            "same_model": same,
            **({"version": None} if with_version else {}),
        }
    # Information only: the version never decides the status (#673).
    if with_version:
        result["version"] = await version()
    return result
