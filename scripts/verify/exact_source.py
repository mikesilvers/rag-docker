"""Replacing a name selects exactly that name in real Weaviate (#622).

Runs in the verify project's API container, against its Weaviate:

    docker compose exec -T api python - <prefix> page|walk|bulk < scripts/verify/exact_source.py

`source_file` is word-tokenized, so Weaviate's equal("report.pdf") also matches
`a/report.pdf`, `Report.PDF` and `report.pdf (part 1)`. The scratch collection
uses the real chunk properties without a vectorizer, so no embedding is needed:
- page: a few names share report.pdf's words, under 10,000 matches, so the
  matches are read in one query and filtered exactly;
- walk: over 10,000 objects share them, so the whole collection is walked;
- bulk: the name itself has over 10,000 earlier chunks, so they are deleted by
  UUID in more than one batch.
The collection is removed afterwards. Exits 0 only if every expectation holds.
"""
import sys
import uuid
import warnings
from datetime import datetime, timedelta, timezone

from weaviate.classes.config import Configure
from weaviate.classes.data import DataObject
from weaviate.classes.query import Filter

from services import weaviate_client as wc

warnings.simplefilter("ignore", DeprecationWarning)

prefix, mode = sys.argv[1], sys.argv[2]
NAME = f"{prefix}ExactSource"
EARLIER = datetime(2026, 1, 1, tzinfo=timezone.utc)
problems = []


def expect(ok, what):
    if not ok:
        problems.append(what)


def add(coll, name, when, n):
    """Store n objects under name, created_at as ingest writes it; their UUIDs."""
    ids = []
    for start in range(0, n, 1000):
        objects = [DataObject(properties={"source_file": name, "content": f"{name} {i}",
                                          "chunk_index": i, "created_at": when.isoformat()},
                              uuid=uuid.uuid4())
                   for i in range(start, min(n, start + 1000))]
        result = coll.data.insert_many(objects)
        if result.errors:
            raise RuntimeError(f"insert failed: {list(result.errors.values())[:3]}")
        ids += [str(o.uuid) for o in objects]
    return set(ids)


def by_name(coll):
    names = {}
    for obj in coll.iterator(return_properties=["source_file"]):
        names.setdefault(obj.properties["source_file"], set()).add(str(obj.uuid))
    return names


client = wc.get_client()
try:
    if client.collections.exists(NAME):
        client.collections.delete(NAME)
    coll = client.collections.create(NAME, vectorizer_config=Configure.Vectorizer.none(),
                                     properties=wc.COLLECTION_PROPERTIES)
    created = datetime.now(timezone.utc)
    if mode == "page":
        others = {name: add(coll, name, EARLIER, n) for name, n in (
            ("a/report.pdf", 3), ("Report.PDF", 3), ("notes/report.pdf", 2), ("report.pdf (part 1)", 2))}
        expect(not wc._source_has_chunks_sync(NAME, "report.pdf"),
               "report.pdf has chunks before it is stored")
        add(coll, "report.pdf", EARLIER - timedelta(days=1), 4)
        new = add(coll, "report.pdf", created, 3)
        expect(wc._source_has_chunks_sync(NAME, "report.pdf"), "report.pdf has no chunks once stored")
        deleted = wc._delete_earlier_chunks_sync(NAME, "report.pdf", created, 3)
        expect(deleted == 4, f"deleted {deleted} earlier chunks, not 4")
    elif mode == "walk":
        others = {"a/report.pdf": add(coll, "a/report.pdf", EARLIER, 10_005)}
        add(coll, "report.pdf", EARLIER - timedelta(days=1), 2)
        new = add(coll, "report.pdf", created, 2)
        words = coll.aggregate.over_all(filters=Filter.by_property("source_file").equal("report.pdf"),
                                        total_count=True).total_count
        expect(words > 10_000, f"only {words} word matches; the walk isn't exercised")
        expect(wc._source_has_chunks_sync(NAME, "report.pdf"), "report.pdf has no chunks")
        expect(not wc._source_has_chunks_sync(NAME, "Report.PDF"), "Report.PDF has chunks, unstored")
        deleted = wc._delete_earlier_chunks_sync(NAME, "report.pdf", created, 2)
        expect(deleted == 2, f"deleted {deleted} earlier chunks, not 2")
    elif mode == "bulk":
        # More earlier chunks of the exact name than one delete removes: the
        # by-UUID deletes run in two batches, 10,000 and 3 (#622).
        others = {name: add(coll, name, EARLIER, n) for name, n in (
            ("a/report.pdf", 3), ("Report.PDF", 2))}
        add(coll, "report.pdf", EARLIER - timedelta(days=1), 10_003)
        new = add(coll, "report.pdf", created, 2)
        deleted = wc._delete_earlier_chunks_sync(NAME, "report.pdf", created, 2)
        expect(deleted == 10_003, f"deleted {deleted} earlier chunks, not 10003")
    else:
        raise SystemExit(f"unknown mode {mode!r}")
    after = by_name(coll)
    expect(after.get("report.pdf") == new, "report.pdf doesn't hold exactly its new chunks")
    for name, ids in others.items():
        kept = len(after.get(name, set()) & ids)
        expect(after.get(name) == ids, f"{name} kept {kept} of its {len(ids)} chunks")
except Exception as exc:  # noqa: BLE001
    problems.append(f"{type(exc).__name__}: {exc}")
finally:
    try:
        if client.collections.exists(NAME):
            client.collections.delete(NAME)
    finally:
        wc.close_client()

print("; ".join(problems) if problems else f"{mode}: ok")
sys.exit(1 if problems else 0)
