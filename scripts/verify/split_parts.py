"""Files over the per-file limits stored as parts in the real Weaviate (#621).

Run inside the verify project's API container:
    docker compose exec -T api python - "$PREFIX" < scripts/verify/split_parts.py

Real parser, real ingest worker, real Weaviate and its exact-name selection
(#622), on a scratch collection without a vectorizer: embedding 13,000 chunks
through Ollama takes the verify project about an hour, and the vectors don't
bear on how a file is split or replaced. The collection and the temporary
source and retention paths are removed afterwards.
"""
import json
import sys
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

from weaviate.classes.config import Configure, VectorDistances

from config import settings
from services import chunker, ingest_pipeline, weaviate_client as wc

prefix = sys.argv[1] if len(sys.argv) > 1 else "Vfy"
collection = prefix + "Split" + uuid.uuid4().hex[:12]
assert not wc._collection_exists_sync(collection)
created = False
failures = []


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  — {detail}"), flush=True)
    if not ok:
        failures.append(name)


def stored():
    """Each stored name's chunks, in chunk order."""
    names = {}
    for o in wc.get_client().collections.get(collection).iterator():
        names.setdefault(o.properties["source_file"], []).append(o.properties)
    return {n: [p["content"] for p in sorted(ps, key=lambda p: p["chunk_index"])]
            for n, ps in names.items()}


def parts_of(state, name):
    found = sorted((n for n in state if n.startswith(name + " (part ")),
                   key=lambda n: int(n.rsplit(" ", 1)[1][:-1]))
    return found, found == [f"{name} (part {i})" for i in range(1, len(found) + 1)]


def ingest(root, files, strategy, size):
    """Stage the files under their logical names and run the ingest worker."""
    stage = Path(tempfile.mkdtemp(dir=root))
    paths, names = [], {}
    for name, data in files:
        path = stage / str(len(paths)) / Path(name).name
        path.parent.mkdir()
        path.write_text(data)
        paths.append(path)
        names[path] = name
    job_id = "split-" + uuid.uuid4().hex[:8]
    job = {"job_id": job_id, "status": "queued", "files_total": len(paths), "files_completed": 0,
           "files_failed": 0, "chunks_stored": 0, "chunks_submitted": 0, "chunks_accepted": 0,
           "chunks_replaced": 0, "errors": [], "skipped": [], "split": []}
    ingest_pipeline._jobs[job_id] = job
    try:
        ingest_pipeline._process_job_sync(job_id, paths, stage, collection, strategy, size, 0,
                                          0.85, 0, None, names)
        return dict(job)
    finally:
        ingest_pipeline._jobs.pop(job_id, None)


try:
    wc.get_client().collections.create(
        name=collection, vectorizer_config=Configure.Vectorizer.none(),
        vector_index_config=Configure.VectorIndex.hnsw(distance_metric=VectorDistances.COSINE),
        properties=wc.COLLECTION_PROPERTIES)
    created = True
    with tempfile.TemporaryDirectory(prefix="split-live-") as directory, \
            patch.object(settings, "sources_dir", str(Path(directory) / "retained")):
        root = Path(directory)
        lines = [json.dumps({"t": f"{i:07d} " + "x" * 589}) + "\n" for i in range(13_000)]
        records = "".join(lines)
        paragraph = "word " * 199 + "end."
        long_text = "\n\n".join([paragraph] * 10_500)
        assert len(long_text) > chunker.MAX_TEXT_CHARACTERS

        # The reviewer's case: whole-record packing gives 13,000 chunks.
        job = ingest(root, [("big.jsonl", records)], "context_aware", 1000)
        state = stored()
        jparts, numbered = parts_of(state, "big.jsonl")
        check("13,000 JSON Lines records of 600 characters (context_aware) complete",
              job["status"] == "completed" and not job["errors"], job)
        check("they are stored as big.jsonl (part 1) to (part N), N >= 2, every record once",
              numbered and len(jparts) >= 2 and "big.jsonl" not in state
              and sorted(c[3:10] for n in jparts for c in state[n]) == [f"{i:07d}" for i in range(13_000)],
              {n: len(c) for n, c in state.items()})
        check("each part holds at most 10,000 chunks", all(len(state[n]) <= chunker.MAX_CHUNKS_PER_FILE
                                                           for n in jparts))
        check("the job lists it in split", job["split"] == [
            f"big.jsonl: split into {len(jparts)} parts, stored as 'big.jsonl (part 1)' to "
            f"'big.jsonl (part {len(jparts)})'"], job["split"])

        # A file over 10,000,000 characters, then the same name again within the limits.
        job = ingest(root, [("notes/long.txt", long_text)], "fixed", 6000)
        state = stored()
        tparts, numbered = parts_of(state, "notes/long.txt")
        check("a 10,519,499-character .txt completes as parts",
              job["status"] == "completed" and numbered and len(tparts) >= 2
              and "notes/long.txt" not in state, (job, sorted(state)))
        check("its parts keep every paragraph",
              "".join("".join(state[n]) for n in tparts).count("end.") == 10_500)
        before = {n: state[n] for n in jparts}
        earlier = sum(len(state[n]) for n in tparts)
        job = ingest(root, [("notes/long.txt", "\n\n".join([paragraph] * 3))], "fixed", 6000)
        state = stored()
        check("re-uploading notes/long.txt within the limits replaces every part",
              job["status"] == "completed" and job["split"] == []
              and job["chunks_replaced"] == earlier, job)
        left, _ = parts_of(state, "notes/long.txt")
        check("no part of notes/long.txt is left, and only the new version is stored",
              not left and len(state.get("notes/long.txt", [])) == job["chunks_stored"], sorted(state))
        check("big.jsonl's parts are untouched", all(state.get(n) == before[n] for n in jparts))

        # A bad line after part 1: nothing new is stored and the earlier version stays.
        # 17,000 records are over the text limit, so parts come before the bad line.
        more = lines + [json.dumps({"t": f"{i:07d} " + "x" * 589}) + "\n" for i in range(13_000, 17_000)]
        job = ingest(root, [("big.jsonl", "".join(more) + "{bad\n")], "context_aware", 1000)
        state = stored()
        check("a bad line after part 1 fails the file and stores nothing new",
              job["status"] == "failed" and len(job["errors"]) == 1
              and job["errors"][0].startswith("big.jsonl: part 2: line 17001 ")
              and job["errors"][0].endswith("; nothing from it was stored"), job)
        check("the earlier big.jsonl parts are still whole", all(state.get(n) == before[n] for n in jparts))
finally:
    if created:
        wc._delete_collection_sync(collection)
    wc.close_client()
assert not wc._collection_exists_sync(collection)
wc.close_client()
print("PASS scratch collection removed", flush=True)
sys.exit(1 if failures else 0)
