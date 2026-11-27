"""The API's query vectors are the ones Weaviate makes, on a real Ollama and Weaviate (#936).

Inside the disposable API container (`03_query.sh`):

    docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/query_embedding.py

Weaviate's text2vec-ollama embeds documents (and `semantic`/`hybrid` questions)
through Ollama's `/api/embed`, which returns unit-length vectors; the API embeds
`hnsw`/`flat` questions itself. This first reports, for one text, the norms of
Ollama's `/api/embeddings` and `/api/embed` vectors and their cosine (step 1 of
#936, on the verify project's Ollama), and fails only if `/api/embed`'s vector
isn't unit length. Then it creates an owned `dot` collection (flat index) and an
owned `l2-squared` collection (hnsw index), each with its own three fixtures,
embedded by Weaviate, and checks that:

- the vector the API makes for a fixture's text matches the one Weaviate stored
  for a probe object holding only that text (deleted before the questions);
- a known question returns its own fixture first in the `hnsw` and `flat` modes;
- each `hnsw`/`flat` citation's score equals the `semantic` mode's for the same
  chunk (a query vector of another length would change `dot` and `l2-squared`
  scores).

The question's reformulation and the answer are controlled (no answer-model
work); the embedding model is real. Its collections are deleted at the end,
pass or fail.
"""
import asyncio
import math
import os
import sys
import tempfile
import uuid
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

from config import settings
from main import app
from auth import testing as auth_testing  # a real session in a temporary store (#25)
from services import ollama_client
from services import weaviate_client as wc

prefix = os.environ.get("RAG_TEST_PREFIX", "Vfy") + "QEmbed" + uuid.uuid4().hex[:8]

# metric -> (index type, fixtures, a known question about the second fixture).
# Lowercase and under 200 characters, so a citation's excerpt is the whole text.
CASES = {
    "dot": ("flat", [
        "the cafeteria opens at seven in the morning and serves breakfast until ten.",
        "employees must submit travel expense reports within thirty days of returning.",
        "the parking garage is closed on weekends for cleaning and repairs.",
    ], "how long do i have to file a travel expense report?"),
    "l2-squared": ("hnsw", [
        "the library lends laptops to students for up to two weeks at a time.",
        "fire drills happen on the first monday of every month at noon.",
        "the gym pool is heated to twenty-eight degrees all year round.",
    ], "when are the monthly fire drills held?"),
}

client = TestClient(app, headers=auth_testing.headers())
created = []
failures = []


def norm(vector):
    return math.sqrt(sum(v * v for v in vector))


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / (norm(a) * norm(b))


def report(ok, message):
    print(("PASS " if ok else "FAIL ") + message, flush=True)
    if not ok:
        failures.append(message)


def step_one():
    """Both endpoints' vectors for one text on the running Ollama (informational,
    except that /api/embed must be unit length)."""
    text = "who approves overtime?"
    with httpx.Client(timeout=120.0) as http:
        embed = http.post(f"{ollama_client._BASE}/api/embed",
                          json={"model": settings.embed_model, "input": text})
        embed.raise_for_status()
        unit = embed.json()["embeddings"][0]
        try:
            legacy = http.post(f"{ollama_client._BASE}/api/embeddings",
                               json={"model": settings.embed_model, "prompt": text})
            legacy.raise_for_status()
            raw = legacy.json()["embedding"]
            print(f"INFO {settings.embed_model}: /api/embeddings norm {norm(raw):.4f}, "
                  f"/api/embed norm {norm(unit):.4f}, cosine {cosine(raw, unit):.6f}", flush=True)
        except Exception as error:  # noqa: BLE001
            print(f"INFO /api/embeddings didn't answer: {error!r}; /api/embed norm {norm(unit):.4f}",
                  flush=True)
    report(abs(norm(unit) - 1) <= 1e-3, f"/api/embed returns a unit-length vector (norm {norm(unit):.6f})")


def create(name, metric, index_type, fixtures):
    response = client.post("/collections", json={"name": name, "index_type": index_type,
                                                  "distance_metric": metric})
    assert response.status_code == 201, response.text
    created.append(name)
    coll = wc.get_client().collections.get(name)
    for i, text in enumerate(fixtures):
        # Weaviate embeds it (/api/embed); a citation needs source_file and chunk_index.
        coll.data.insert(properties={"content": text, "source_file": "query-embedding.txt",
                                     "chunk_index": i})
    return coll


def stored_vector_matches(coll, metric, text):
    """Weaviate embeds all of an object's text properties joined by spaces, so the
    probe has only `content`; it is deleted before any question is asked."""
    probe = coll.data.insert(properties={"content": text})
    try:
        vector = coll.query.fetch_object_by_id(probe, include_vector=True).vector
        stored = vector.get("default") if isinstance(vector, dict) else vector
    finally:
        coll.data.delete_by_id(probe)
    mine = asyncio.run(ollama_client.embed(text))
    ok = (stored is not None and len(stored) == len(mine)
          and cosine(stored, mine) >= 0.9999 and abs(norm(stored) - norm(mine)) <= 1e-3)
    detail = ("no stored vector" if stored is None else
              f"cosine {cosine(stored, mine):.6f}, norms {norm(stored):.4f} and {norm(mine):.4f}")
    report(ok, f"{metric}: the API's vector for a fixture is the one Weaviate stored ({detail})")


def ask(name, question, mode):
    with patch("services.rag_pipeline.ollama.chat", new=AsyncMock(return_value=question)):
        response = client.post("/query", json={"collection": name, "question": question,
                                               "retrieval_mode": mode, "top_k": 3,
                                               "include_citations": True})
    if response.status_code != 200:
        return None, f"{response.status_code} {response.text[:300]}"
    return response.json()["citations"], None


def check_queries(name, metric, fixtures, question):
    semantic, error = ask(name, question, "semantic")
    if error:
        report(False, f"{metric}, semantic mode: {error}")
        return
    expected = {c["excerpt"]: c["score"] for c in semantic}
    for mode in ("hnsw", "flat"):
        citations, error = ask(name, question, mode)
        label = f"{metric} collection, {mode} mode"
        if error:
            report(False, f"{label}: {error}")
            continue
        first = citations[0]["excerpt"] if citations else None
        report(first == fixtures[1], f"{label}: its own fixture comes first ({first!r})")
        got = {c["excerpt"]: c["score"] for c in citations}
        ok = (set(got) == set(expected)
              and all(isinstance(got[k], (int, float)) and isinstance(expected[k], (int, float))
                      and abs(got[k] - expected[k]) <= 1e-3 for k in got))
        report(ok, f"{label}: scores as the semantic mode's ({sorted(got.values(), reverse=True)} "
                   f"vs {sorted(expected.values(), reverse=True)})")


with tempfile.TemporaryDirectory(prefix="query-embedding-") as directory, \
        patch.object(settings, "upload_dir", directory):
    try:
        step_one()
        for metric, (index_type, fixtures, question) in CASES.items():
            name = prefix + ("Dot" if metric == "dot" else "L2")
            coll = create(name, metric, index_type, fixtures)
            stored_vector_matches(coll, metric, fixtures[0])
            check_queries(name, metric, fixtures, question)
    except Exception as error:  # noqa: BLE001
        failures.append(f"unexpected error: {error!r}")
    finally:
        for name in created:
            try:
                if wc._collection_exists_sync(name):
                    response = client.delete("/collections/" + name + "?confirm=true")
                    if response.status_code != 200:
                        failures.append(f"cleanup {name}: {response.status_code} {response.text}")
            except Exception as error:  # noqa: BLE001
                failures.append(f"cleanup {name}: {error!r}")
        wc.close_client()

if failures:
    print("FAIL " + "; ".join(failures), flush=True)
    sys.exit(1)
print("PASS hnsw and flat query vectors are Weaviate's: own fixtures first, scores as semantic's", flush=True)
