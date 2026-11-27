"""Which model answers each task (#671). Run in the api container by
23_llm_split.sh, with BUILD_LLM_MODEL set for this process only:

    docker compose exec -T -e BUILD_LLM_MODEL=qwen2.5:0.5b \
        -e RAG_LLM_SPLIT_COLLECTION=<collection> api python - < scripts/verify/llm_split.py

It pulls the build model, then runs a preparation of a short text, one gold
standard pair and one query through the services' own code, recording the model
Ollama reports in each /api/chat reply (the calls themselves are unchanged).
Preparation writes its parts to a temporary folder and the pair isn't stored,
so nothing is left behind; the build model is deleted from Ollama at the end.
The pull is tried again when Ollama answers with a 5xx (3 attempts, waiting 5 s
and then 10 s); a pull that still fails, or fails another way, runs no task and
is reported as `setup_error` (#922).
Prints one JSON line: the resolved models (as set, and as `name:tag`) and, per
task, the sorted set of models that answered (Ollama's names, `name:tag`) and,
when the task raised, `<task>_error`; or, when the pull failed, `setup_error`.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path

import httpx

from config import settings
from services import goldstandard, preparation, preparation_jobs, rag_pipeline

COLLECTION = os.environ["RAG_LLM_SPLIT_COLLECTION"]
BASE = f"http://{settings.ollama_host}:{settings.ollama_port}"
TEXT = ("Overtime policy\n\nDepartment managers approve overtime before it is worked. "
        "Requests go to the manager in writing at least one day ahead.\n\n"
        "Page 1 of 1\n\nPayroll pays approved overtime at one and a half times the "
        "hourly rate, in the next pay period.\n")


def tagged(model):
    return model if ":" in model.rsplit("/", 1)[-1] else f"{model}:latest"


out = {"build_model": settings.build_model, "answer_model": settings.answer_model,
       # As Ollama names the model in a reply, so the suite compares like with like.
       "build_tagged": tagged(settings.build_model),
       "answer_tagged": tagged(settings.answer_model)}
build = settings.build_model

# The build model, pulled into the verify project's Ollama (removed below). A
# 5xx, which a busy Ollama gives now and then, is tried again (#922).
PULL_WAITS = (5, 10)                  # seconds before the 2nd and the 3rd attempt
PULL_ATTEMPTS = len(PULL_WAITS) + 1


def pull(name):
    """Pulls name into Ollama: None once it is pulled, else why it wasn't."""
    for attempt in range(1, PULL_ATTEMPTS + 1):
        try:
            response = httpx.post(f"{BASE}/api/pull", json={"name": name, "stream": False},
                                  timeout=1800)
        except httpx.HTTPError as exc:
            return f"{type(exc).__name__}: {exc}"
        if response.is_success:
            return None
        if response.status_code < 500 or attempt == PULL_ATTEMPTS:
            tries = f"{attempt} attempt" + ("s" if attempt > 1 else "")
            return f"HTTP {response.status_code} after {tries}: {response.text[:200]}"
        wait = PULL_WAITS[attempt - 1]
        print(f"pull of {name}: attempt {attempt} of {PULL_ATTEMPTS} answered HTTP "
              f"{response.status_code}; trying again in {wait} s", file=sys.stderr)
        time.sleep(wait)


seen = []
real_post = httpx.AsyncClient.post


async def recording_post(self, url, *args, **kwargs):
    response = await real_post(self, url, *args, **kwargs)
    if str(url).endswith("/api/chat"):
        try:
            seen.append(response.json().get("model"))
        except ValueError:
            seen.append(None)
    return response

httpx.AsyncClient.post = recording_post


def task(name, coroutine):
    seen.clear()
    try:
        asyncio.run(coroutine)
    except Exception as exc:                          # noqa: BLE001
        out[name + "_error"] = f"{type(exc).__name__}: {exc}"
    out[name] = sorted({tagged(m) if m else "none" for m in seen})


async def prepare():
    with tempfile.TemporaryDirectory() as folder:
        raw = Path(folder) / "overtime.txt"
        raw.write_text(TEXT)
        parts = Path(folder) / "parts"
        parts.mkdir()

        async def write_part(index, suffix, data):
            path = parts / f"{index + 1:03d}{suffix}"
            path.write_bytes(data)
            return path

        chat, list_models = preparation_jobs._llm()
        await preparation.prepare_document(raw, raw.name, ".txt", chat, list_models,
                                           lambda: False, write_part)


async def gold_pair():
    await goldstandard._generate_pair({"content": TEXT})


async def query():
    await rag_pipeline.run_query("Who approves overtime?", COLLECTION, "hnsw", 3, 0.5,
                                 False, "end_user")


try:
    failed = pull(build)
    if failed:
        out["setup_error"] = f"could not pull {build} into the verify project's Ollama: {failed}"
    else:
        task("prepare", prepare())
        task("goldstandard", gold_pair())
        task("query", query())
finally:
    httpx.AsyncClient.post = real_post
    try:
        httpx.request("DELETE", f"{BASE}/api/delete", json={"name": build}, timeout=60)
    except httpx.HTTPError as exc:
        print(f"could not delete {build}: {exc}", file=sys.stderr)

print(json.dumps(out))
