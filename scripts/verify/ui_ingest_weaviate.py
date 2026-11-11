"""The UI ingest suite's collections as Weaviate holds them (#641).

Run by 22_ui_ingest.sh in the verify project's api container:

    docker compose exec -T api python - < scripts/verify/ui_ingest_weaviate.py

Prints JSON: for every collection whose name starts with VfyUiIngest, its object
count and, per source_file, the content of its chunks in chunk_index order. It
only reads. ui_ingest_check.py checks the documents' names and phrases against it.
"""
import json

from services import weaviate_client as wc

PREFIX = "VfyUiIngest"

try:
    client = wc.get_client()
    out = {}
    for name in sorted(client.collections.list_all(simple=True)):
        if not name.startswith(PREFIX):
            continue
        documents, count = {}, 0
        for obj in client.collections.get(name).iterator(
                return_properties=["source_file", "chunk_index", "content"]):
            count += 1
            p = obj.properties
            documents.setdefault(p["source_file"], []).append((p["chunk_index"], p["content"]))
        out[name] = {"count": count,
                     "documents": {source: [text for _, text in sorted(chunks, key=lambda c: c[0])]
                                   for source, chunks in documents.items()}}
    print(json.dumps({"collections": out}))
finally:
    wc.close_client()
