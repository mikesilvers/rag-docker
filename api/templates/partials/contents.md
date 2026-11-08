```
manifest.json           what this package is; authoritative
collection.json         schema, stored embedding_model, index type, distance metric, HNSW parameters
chunks.jsonl            one JSON object per chunk, with its vector
ingest_config.json      chunking settings, if the collection had any saved
retrieval_config.json   the retrieval settings the collection was tuned with
README.md               generated from the manifest
retrieve.py             a standalone query script, when retrieval settings exist
goldstandard/           evaluation sessions for this collection
sources/                the original documents, at `with-sources` fidelity only
models/                 Ollama model files, when exported with include_models
```

Every file listed in the manifest's `files` map carries a SHA-256 digest, and
import verifies all of them before touching the database. `manifest.json`,
`README.md` and `retrieve.py` are not in that map: the first cannot contain its
own digest, and the other two are generated from it afterwards.
