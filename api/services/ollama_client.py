from __future__ import annotations
from services import telemetry
import time

import httpx

from config import settings

_BASE = f"http://{settings.ollama_host}:{settings.ollama_port}"


@telemetry.traced("ollama.embed")
async def embed(text: str) -> list[float]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{_BASE}/api/embeddings",
            json={"model": settings.embed_model, "prompt": text},
        )
        resp.raise_for_status()
        return resp.json()["embedding"]


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


@telemetry.traced("ollama.models")
async def list_models() -> set[str]:
    """Full names (`name:tag`) of the models Ollama reports."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(f"{_BASE}/api/tags")
        resp.raise_for_status()
        return {m["name"] for m in resp.json().get("models", [])}


@telemetry.traced("ollama.health")
async def check_health() -> dict:
    """`llm` is the answer model, `build_llm` the build model (#671)."""
    answer, build = settings.answer_model, settings.build_model
    same = same_model(answer, build)
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{_BASE}/api/tags")
            resp.raise_for_status()
            latency_ms = int((time.monotonic() - start) * 1000)
            models = {m["name"].split(":")[0] for m in resp.json().get("models", [])}

            def entry(model: str) -> dict:
                ok = model.split(":")[0] in models
                return {"status": "ok" if ok else "error", "latency_ms": latency_ms, "model": model}

            result = {"llm": entry(answer), "build_llm": entry(build),
                      "embed": entry(settings.embed_model), "same_model": same}
            if any(result[k]["status"] != "ok" for k in ("llm", "build_llm", "embed")):
                telemetry.outcome("error")
            return result
    except Exception as exc:
        telemetry.outcome("error", exc)
        latency_ms = int((time.monotonic() - start) * 1000)
        return {
            "llm": {"status": "error", "latency_ms": latency_ms, "model": answer},
            "build_llm": {"status": "error", "latency_ms": latency_ms, "model": build},
            "embed": {"status": "error", "latency_ms": latency_ms, "model": settings.embed_model},
            "same_model": same,
        }
