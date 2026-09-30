# RAG Docker — Specifications

**Version:** 1.4  
**Date:** 2026-09-10  
**Status:** Final — Pending User Approval  
**Depends on:** ANALYSIS.md v1.2

---

## 1. Document Scope

This document translates the decisions in ANALYSIS.md into precise, implementable specifications. Each section defines exact behaviors, data contracts, configuration schemas, and acceptance criteria. The implementation document will reference these specifications directly.

---

## 2. Docker Compose Stack

### 2.1 Service Definitions

**File:** `docker-compose.yml`

```yaml
services:
  weaviate:
    image: semitechnologies/weaviate:1.39.4
    environment:
      QUERY_DEFAULTS_LIMIT: 25
      AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED: 'true'
      PERSISTENCE_DATA_PATH: /var/lib/weaviate
      ENABLE_MODULES: 'text2vec-ollama'
      TEXT2VEC_OLLAMA_APIENDPOINT: http://ollama:11434
      TEXT2VEC_OLLAMA_MODEL: nomic-embed-text
      # Raft state is keyed by node identity; without a fixed hostname the
      # recorded identity stops matching after `compose down` and startup dies
      # with "could not open cloud meta store: bootstrap: context deadline
      # exceeded".
      CLUSTER_HOSTNAME: 'node1'
      RAFT_BOOTSTRAP_EXPECT: 1
    volumes:
      - weaviate_data:/var/lib/weaviate
    ports: []
    networks: [rag-internal]
    healthcheck:
      # The weaviate image ships busybox wget, NOT curl.
      test: ["CMD", "wget", "-q", "--spider", "http://localhost:8080/v1/.well-known/ready"]
      interval: 10s
      timeout: 5s
      retries: 10

  ollama:
    image: ollama/ollama:0.3.14
    # Run via bash so the script does not need its executable bit, which is
    # not reliably preserved when the project is distributed as a zip.
    entrypoint: ["/bin/bash", "/entrypoint.sh"]
    volumes:
      - ollama_models:/root/.ollama
      - ./ollama/entrypoint.sh:/entrypoint.sh:ro
    networks: [rag-internal]
    healthcheck:
      # The ollama image ships ONLY the ollama binary -- no curl, wget or nc --
      # so the CLI is the only usable probe.
      test: ["CMD-SHELL", "ollama list | grep -q phi3.5 && ollama list | grep -q nomic-embed-text"]
      interval: 20s
      timeout: 15s
      retries: 20
      start_period: 120s
    # Optional GPU support (uncomment on GPU host):
    # deploy:
    #   resources:
    #     reservations:
    #       devices:
    #         - capabilities: [gpu]

  api:
    build: ./api
    # Explicit tag so the image name does not depend on the directory name.
    # Without it compose derives "<project>-api" from the folder, and an offline
    # install extracted to a differently named folder would not find the loaded
    # image and would try to rebuild (which needs the internet).
    image: rag-docker-api:latest
    environment:
      WEAVIATE_HOST: weaviate
      WEAVIATE_PORT: 8080
      OLLAMA_HOST: ollama
      OLLAMA_PORT: 11434
      LLM_MODEL: phi3.5
      EMBED_MODEL: nomic-embed-text
      UPLOAD_DIR: /app/uploads
      SOURCES_DIR: /app/sources
      EXPORTS_DIR: /app/exports
      OLLAMA_MODELS_DIR: /ollama
      # Reported by /health and compared against the memory Docker actually
      # provides. Raise it if you allocate more to Docker Desktop; no rebuild.
      RECOMMENDED_MEMORY_GB: 12
    volumes:
      - ingest_uploads:/app/uploads
      # Retained source documents; grows with the corpus.
      - rag_sources:/app/sources
      # Bind mount, deliberately: an export is only useful if the user can
      # reach the file from the host without going through Docker.
      - ./exports:/app/exports
      # Ollama's model store, shared read-write so a package can carry models
      # into an air-gapped machine.
      - ollama_models:/ollama
    networks: [rag-internal]
    depends_on:
      weaviate: { condition: service_healthy }
      ollama: { condition: service_healthy }
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:8000/health"]
      interval: 10s
      timeout: 5s
      retries: 5

  ui:
    build: ./ui
    image: rag-docker-ui:latest
    networks: [rag-internal]
    depends_on:
      api: { condition: service_healthy }

  proxy:
    image: nginx:1.27-alpine
    ports:
      # Local unauthenticated workbench: host loopback only.
      - "127.0.0.1:8080:80"
    volumes:
      - ./proxy/nginx.conf:/etc/nginx/nginx.conf:ro
    networks: [rag-internal]
    depends_on: [ui, api]

# The MCP server is PARKED: its service block has been removed from
# docker-compose.yml and its image is not bundled. Source is preserved in ./mcp
# and the block to restore is in MCP_IMPLEMENTATION.md §7.

volumes:
  weaviate_data:
  ollama_models:
  ingest_uploads:
  rag_sources:

networks:
  rag-internal:
    driver: bridge
```

### 2.2 Nginx Routing

**File:** `proxy/nginx.conf`

| Path prefix | Routes to | Notes |
|---|---|---|
| `/api/` | `http://api:8000/` | Strip `/api` prefix |
| `/` | `http://ui:3000/` | SPA fallback to `index.html` |

### 2.3 Model Initialization

The `ollama` service uses a custom entrypoint script (`ollama/entrypoint.sh`) that starts the Ollama server and then pulls required models. The script must be made executable on the host before first run: `chmod +x ollama/entrypoint.sh`.

```bash
#!/bin/bash
ollama serve &
OLLAMA_PID=$!
# Wait for server to be ready
until ollama list > /dev/null 2>&1; do sleep 2; done   # no curl in this image
# Pull models if not already cached in volume
ollama pull phi3.5
ollama pull nomic-embed-text
wait $OLLAMA_PID
```

The Ollama healthcheck is extended to verify both models are present, not merely that the server is running:

```yaml
healthcheck:
  test: ["CMD-SHELL", "ollama list | grep -q phi3.5 && ollama list | grep -q nomic-embed-text"]
  interval: 20s
  timeout: 15s
  retries: 20
  start_period: 120s
```

`start_period: 120s` gives the model pull time to complete on first run before health retries begin counting as failures. The API service's `depends_on: { ollama: service_healthy }` ensures no API traffic flows until both models are confirmed present.

### 2.4 Startup Sequence

1. Weaviate starts → healthcheck passes (ready endpoint returns 200)
2. Ollama starts → pulls models (phi3.5, nomic-embed-text) → healthcheck passes
3. API starts → verifies both downstream connections → healthcheck passes
4. UI starts (static build, no runtime deps)
5. Nginx proxy starts

---

## 3. API Service Specification

**Runtime:** Python 3.11, FastAPI, Uvicorn  
**Base URL (internal):** `http://api:8000`  
**Base URL (external via proxy):** `http://<host>/api`

### 3.1 Endpoints

#### 3.1.1 Health

```
GET /health
```

**Response 200:**
```json
{
  "status": "ok",
  "services": {
    "weaviate": { "status": "ok", "latency_ms": 1 },
    "ollama": {
      "llm": { "status": "ok", "latency_ms": 5, "model": "phi3.5" },
      "embed": { "status": "ok", "latency_ms": 5, "model": "nomic-embed-text" }
    }
  },
  "resources": {
    "memory": {
      "status": "ok",
      "allocated_gb": 11.67,
      "recommended_minimum_gb": 12.0
    }
  }
}
```

**Response 503** if any downstream service is unreachable. Individual service `status` is `"ok"` or `"error"`.

**Weaviate is probed through the client, not over plain HTTP.** A bare `GET
/v1/.well-known/ready` cannot detect a client/server version mismatch: the server
answers "ready" while every client call fails, so health reports green during a
total outage of Weaviate functionality. The check calls `get_client()` — the real
connect handshake, where such a mismatch raises — bounded by a 4 s timeout so a
hung connect cannot stall past the container healthcheck's own 5 s limit.

**`resources.memory` is advisory and never fails the endpoint.**

| Field | Meaning |
|---|---|
| `allocated_gb` | `MemTotal` from `/proc/meminfo`, which on Docker Desktop is the VM's total memory |
| `recommended_minimum_gb` | From `RECOMMENDED_MEMORY_GB` (default 12) |
| `status` | `ok`, `below_recommended`, or `unknown` if `/proc/meminfo` is unreadable |
| `note` | Present only when below recommended; states what to change |

The guest always sees slightly less than the figure configured in Docker Desktop
— 12288 MiB configured reads as 11.7 GB, roughly 5% lost to VM overhead — so the
comparison allows a 5% margin. Without it a correctly sized allocation would
report itself as too small.

Low memory must **not** set `status: degraded` or return 503: it slows the stack
but does not break it, and failing the endpoint would mark the api container
unhealthy and take the whole stack down over a tuning warning.

---

#### 3.1.2 Collections

```
GET /collections
```

Returns all Weaviate collections with stats. `created_at` is tracked by the API service (written to `{UPLOAD_DIR}/collection_registry.json` on each `POST /collections` call) since Weaviate does not expose a native collection creation timestamp.

**Response 200:**
```json
{
  "collections": [
    {
      "name": "Documents",
      "object_count": 1842,
      "index_type": "hnsw",
      "distance_metric": "cosine",
      "created_at": "2026-09-10T14:23:00Z"
    }
  ]
}
```

`created_at` is `null` for any collection that exists in Weaviate but has no entry in `collection_registry.json` (e.g. created outside this system).

---

```
POST /collections
```

Creates a new Weaviate collection.

**Request body:**
```json
{
  "name": "Documents",
  "index_type": "hnsw",
  "distance_metric": "cosine",
  "hnsw_config": {
    "efConstruction": 128,
    "maxConnections": 64,
    "ef": 64
  }
}
```

`index_type`: `"hnsw"` (default) or `"flat"` (exact KNN).  
`distance_metric`: `"cosine"` (default), `"dot"`, `"l2-squared"`. This is a **collection-level** setting — it cannot be changed after creation.  
`hnsw_config` is ignored when `index_type` is `"flat"`. All HNSW field names use camelCase to match the Weaviate v4 client API.

Invalid index/distance values or HNSW settings receive **422 before backend
lookup or creation**. HNSW numeric bounds are §4.3: `efConstruction` 64–512,
`maxConnections` 16–128 and `ef` 16–512. Defaults remain 128/64/64; flat indexes
ignore valid HNSW settings.

**Response 201:**
```json
{ "name": "Documents", "status": "created" }
```

**Response 409** if collection already exists.

---

```
DELETE /collections/{name}
```

Deletes a collection and all its objects. Requires `confirm=true` query parameter as a safeguard.

**Response 200:**
```json
{ "name": "Documents", "status": "deleted", "objects_removed": 1842 }
```

Also removes the entry from `{UPLOAD_DIR}/collection_registry.json` and deletes `{UPLOAD_DIR}/ingest_configs/{collection}.json` if it exists.

**Response 404** if the collection does not exist:
```json
{ "error": { "code": "COLLECTION_NOT_FOUND", "message": "Collection 'Documents' does not exist.", "detail": null } }
```

**Response 400** if `confirm=true` is not supplied:
```json
{ "error": { "code": "CONFIRMATION_REQUIRED", "message": "Add ?confirm=true to confirm deletion.", "detail": null } }
```

---

#### 3.1.3 Ingest

```
POST /ingest/upload
Content-Type: multipart/form-data
```

Accepts one or more files. For ZIP uploads, extracts and processes all supported files within the archive. For folder-equivalent uploads (multiple files in one request), processes all submitted files.

**Size limit:** one request may be at most **512 MB**, all files together. The proxy enforces this (`client_max_body_size` in `proxy/nginx.conf`) and answers an oversize request itself with **HTTP 413**, an nginx HTML page rather than the API's JSON error shape, before the API sees it. The proxy streams accepted uploads to the API without buffering them, and the API writes each file to disk in blocks rather than holding it in memory. The Import page refuses a selection over the limit before sending it.

**Form fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `files` | file[] | Yes | One or more files (PDF, DOCX, TXT, MD, CSV, JSON) or a single ZIP |
| `collection` | string | Yes | Target Weaviate collection name |
| `chunking_strategy` | string | Yes | One of: `fixed`, `overlap`, `semantic`, `context_aware`, `language` |
| `chunk_size` | int | No | Default: 1000 (characters). All size parameters are in characters, not tokens. |
| `chunk_overlap` | int | No | Default: 200 (characters). Ignored by `semantic` and `context_aware`. |
| `similarity_threshold` | float | No | Default: 0.85. Used by `semantic` only. Range: 0.0–1.0. |
| `min_chunk_size` | int | No | Default: 100 (characters). Chunks smaller than this are merged with adjacent chunk. |

Direct upload, saved ingest configuration and optional tuning chunking settings
share validation. `chunk_size` must be an integer from 50 to 6000, and
`min_chunk_size` an integer from 0 to 6000 (§8); overlap must be a nonnegative
integer. For `overlap`/`language`, overlap must be
smaller than chunk size. Minimum size is a merge preference and may exceed the
split target; for example, fixed size 60/minimum 100 preserves the existing
acceptance case by merging small chunks.
`similarity_threshold` is finite and in 0.0–1.0, or `null` where saved/optional
configuration permits it (the effective semantic default is 0.85). Boolean
values are not numeric settings. Strategies must be one of the documented five.
Ignored overlap settings remain ignored for fixed/context-aware/semantic; the
context-aware fallback uses language splitting with zero overlap.

Invalid multipart settings return **422 `INVALID_SETTINGS` before collection
lookup, upload staging or job creation**. Invalid JSON settings return 422 `INVALID_PARAMETER` with
sanitized field errors in `error.detail`. Raw input/error-context values are omitted from validation replies
so non-finite input also produces a serializable 422. Defaults remain unchanged.

**Response 202 (accepted, async):**
```json
{
  "job_id": "a1b2c3d4",
  "status": "queued",
  "files_queued": 3,
  "collection": "Documents"
}
```

Ingest runs asynchronously. Progress is polled via `/ingest/job/{job_id}`.

---

```
GET /ingest/job/{job_id}
```

**Response 200:**
```json
{
  "job_id": "a1b2c3d4",
  "status": "running",
  "files_total": 3,
  "files_completed": 1,
  "files_failed": 0,
  "chunks_stored": 142,
  "errors": []
}
```

`status` values: `"queued"`, `"running"`, `"completed"`, `"failed"`.

On failure, `errors` contains per-file error messages.

**Response 404** if the job ID does not exist:
```json
{ "error": { "code": "JOB_NOT_FOUND", "message": "Job 'a1b2c3d4' not found.", "detail": null } }
```

---

```
GET /ingest/config/{collection}
```

Returns the saved default chunking configuration for a collection. If no configuration has been saved yet, returns the system defaults.

**Response 200:**
```json
{
  "collection": "Documents",
  "chunking_strategy": "overlap",
  "chunk_size": 1000,
  "chunk_overlap": 200,
  "similarity_threshold": null,
  "min_chunk_size": 100,
  "is_default": false
}
```

`is_default: true` means no configuration has been explicitly saved — these are system defaults. `is_default: false` means the user has saved a configuration for this collection. The UI uses this flag to show a "using defaults" notice when appropriate.

---

```
POST /ingest/config
```

Saves (upserts) a default chunking configuration for a collection. Creates the config entry on first call; overwrites on subsequent calls. Used by the Chunking Config page. Does not re-process existing documents.

**Request body:** same schema as the GET response above, including `collection` (which selects the target) but excluding `is_default` (server-computed).

**Response 201:** the saved configuration, in the same shape as the GET response, with `is_default: false`.
```json
{
  "collection": "Documents",
  "chunking_strategy": "overlap",
  "chunk_size": 1000,
  "chunk_overlap": 200,
  "similarity_threshold": null,
  "min_chunk_size": 100,
  "is_default": false
}
```

Config is persisted to `{UPLOAD_DIR}/ingest_configs/{collection}.json` so it survives restarts. The `{collection}` segment in all file paths is the collection name with spaces replaced by underscores and non-alphanumeric characters removed, to ensure safe filenames.

---

#### 3.1.4 Query (RAG)

```
POST /query
```

**Request body:**
```json
{
  "question": "What is the policy for overtime pay?",
  "collection": "Documents",
  "retrieval_mode": "hybrid",
  "top_k": 5,
  "alpha": 0.75,
  "include_citations": false,
  "response_format": "end_user"
}
```

| Field | Type | Default | Description |
|---|---|---|---|
| `question` | string | required | User's question |
| `collection` | string | required | Weaviate collection to query |
| `retrieval_mode` | string | `"hnsw"` | One of: `"hnsw"`, `"flat"`, `"hybrid"`, `"semantic"` |
| `top_k` | int | 5 | Number of chunks to retrieve |

| `alpha` | float | 0.75 | Hybrid mode only: 0.0 = pure BM25, 1.0 = pure vector |
| `include_citations` | bool | false | Whether to return source document citations |
| `response_format` | string | `"end_user"` | `"end_user"` (plain language) or `"engineer"` (verbose, with chunk details) |

Direct query and saved retrieval settings share enums and bounds: retrieval mode
is `hnsw`, `flat`, `hybrid` or `semantic`; response format is `end_user` or
`engineer`; `top_k` is an integer 1–50; `alpha` is finite and in 0.0–1.0.
Invalid settings receive 422 before collection lookup, retrieval or LLM work.
Internal collection creation, ingestion, chunking and query entry points also
validate their supported settings before starting backend/model/staging work;
unknown values do not silently select a default implementation.

Note: distance metric is a collection-level property set at creation, not a per-query parameter.

**Response 200:**
```json
{
  "answer": "Overtime pay is calculated at 1.5x the base hourly rate for all hours over 40 per week.",
  "citations": null,
  "retrieval_latency_ms": 18,
  "llm_latency_ms": 2340,
  "chunks_retrieved": 5
}
```

When `include_citations` is `true`:
```json
{
  "answer": "Overtime is compensated at 1.5x the base rate.",
  "citations": [
    {
      "source_file": "HR-Policy-2026.pdf",
      "chunk_index": 14,
      "score": 0.91,
      "excerpt": "...overtime shall be compensated..."
    }
  ],
  "retrieval_latency_ms": 326,
  "llm_latency_ms": 9424,
  "chunks_retrieved": 3
}
```

**Response 404** if the collection does not exist (uses `COLLECTION_NOT_FOUND` error code).

**RAG pipeline (internal steps):**
1. Reformat user question for retrieval using Phi-3.5 Mini (Section 6.1 prompt).
2. Embed reformulated question — **only for `hnsw` and `flat` modes**: call Ollama `/api/embeddings` directly; for `hybrid` and `semantic` modes, Weaviate handles query embedding internally via `text2vec-ollama`.
3. Retrieve top_k chunks from Weaviate using the specified retrieval mode and its parameters.
4. Pass original question + retrieved chunks to Phi-3.5 Mini for synthesis (Section 6.2 or 6.3 prompt).
5. Format response according to `response_format`.

---

```
GET /retrieval/config/{collection}
```

Returns the saved retrieval configuration for a collection. If nothing has been
saved, returns the system defaults rather than a 404, so a caller never has to
special-case a new collection.

**Response 200:**
```json
{
  "collection": "Documents",
  "retrieval_mode": "hybrid",
  "top_k": 12,
  "alpha": 0.4,
  "ef": 96,
  "response_format": "engineer",
  "is_default": false
}
```

`is_default: true` means no configuration has been explicitly saved and these are
system defaults (`hnsw`, `top_k: 5`, `alpha: 0.75`, `ef: null`,
`response_format: "end_user"`).

---

```
POST /retrieval/config
```

Saves (upserts) the retrieval configuration for a collection. Used by the
Retrieval Config page, and read by exported retrieval scripts so an export
carries the settings it was tuned with.

**Request body:** the GET response shape, including `collection`, excluding
`is_default`.

**Validation (422 on failure):**

| Field | Constraint |
|---|---|
| `retrieval_mode` | one of `hnsw`, `flat`, `hybrid`, `semantic` |
| `top_k` | integer, 1–50 |
| `alpha` | float, 0.0–1.0 |
| `response_format` | one of `end_user`, `engineer` |
| `ef` | integer 16–512 or `null`; see §4.3 |

**Response 201:** the saved configuration, with `is_default: false`.

Config is persisted to `{UPLOAD_DIR}/retrieval_configs/{collection}.json`, written
atomically (temp file then replace) so an interrupted write cannot leave a
half-written config. Deleting a collection deletes its retrieval config.

---

#### 3.1.5 Gold Standard

```
POST /goldstandard/generate
```

Samples chunks from a collection and generates Q&A pairs.

**Request body:**
```json
{
  "collection": "Documents",
  "sample_size": 20,
  "seed": null
}
```

`sample_size`: integer from 1 through 100 (default 20). `seed`: optional integer; null selects with a fresh random nonce. Booleans, non-integral and non-finite values are rejected with 422 before collection lookup, session persistence or generation. Existing integral numeric coercion is retained.

Sampling scans all chunk UUIDs using the SDK iterator without vectors or text properties, then fetches text/metadata only for the at-most100 selected UUIDs. Returned rows retain rank order; a winner deleted between passes is omitted. Each canonical UUID is ranked by SHA-256 of a versioned domain, the seed (or random nonce), and UUID bytes; UUID order breaks hash ties. The best requested candidates are retained in a bounded heap and returned in rank order. A fixed seed and unchanged UUID population produce the same selected UUIDs and order regardless of backend iteration order. Different seeds can select the same subset, especially when all available objects are selected. This contract concerns selection, not deterministic model answers. Concurrent collection mutation is not a snapshot and can change the candidate population.

The iterator caches 100 objects and selection retains at most `sample_size` candidate payloads; the complete corpus is scanned once. Full scans can take longer than fetching an initial prefix. Payload size is inherited from stored chunks; this is a candidate-count bound, not a byte-size limit.

**Response 202 (accepted, async):**
```json
{
  "session_id": "gs_abc123",
  "status": "generating",
  "pairs_total": 20,
  "pairs_completed": 0
}
```

Generation runs asynchronously (one LLM call per chunk). Poll for progress and results via `GET /goldstandard/session/{session_id}`.

If `sample_size` exceeds the number of objects in the collection, all available chunks are used. `pairs_total` in the response reflects the actual number of pairs being generated, which may be less than the requested `sample_size`.

---

```
GET /goldstandard/session/{session_id}
```

**Response 200:**
```json
{
  "session_id": "gs_abc123",
  "status": "generating",
  "pairs_total": 20,
  "pairs_completed": 7,
  "pairs": [
    {
      "pair_id": "p_001",
      "question": "What is the minimum notice period for schedule changes?",
      "answer": "The minimum notice period is 72 hours.",
      "contexts": ["...chunk text..."],
      "ground_truth": "The minimum notice period is 72 hours.",
      "source_file": "Schedule-Policy.pdf",
      "chunk_index": 7,
      "status": "pending"
    }
  ]
}
```

Every session response also carries `collection`, `stale`, `orphaned`, and each flag's nullable `_reason` and `_at` metadata. Flags default to false and metadata to null for legacy sessions. Stale means the retained pairs no longer describe current chunks; orphaned means the collection is gone. These warnings do not delete or remap historical pairs.

`status` (session): `"generating"`, `"completed"`, `"failed"`.  
`status` (pair): `"pending"`, `"approved"`, `"edited"`, `"rejected"`.

The `pairs` array contains only pairs whose generation has completed so far. During generation, pairs are added to the array as each LLM call finishes. `pairs_completed` equals the length of the `pairs` array at any point.

**Response 404** if the session ID does not exist:
```json
{ "error": { "code": "SESSION_NOT_FOUND", "message": "Session 'gs_abc123' not found.", "detail": null } }
```

**Session persistence:** Gold standard sessions are stored in `{UPLOAD_DIR}/goldstandard_sessions/` as individual JSON files (`{session_id}.json`). Sessions survive API container restarts. The API loads existing session files on startup into an in-memory dict.

---

```
PATCH /goldstandard/session/{session_id}/pair/{pair_id}
```

Updates the status or edited content of one pair. Called by the UI when the user approves, rejects, or edits a pair inline.

**Request body:**
```json
{
  "status": "edited",
  "question": "Updated question text (optional)",
  "answer": "Updated answer text (optional)",
  "ground_truth": "Updated ground truth text (optional)"
}
```

`status` must be one of `"approved"`, `"edited"`, `"rejected"`, `"pending"`. Providing `question`, `answer`, or `ground_truth` fields without setting `status: "edited"` is a 422 error. All text fields are optional — only provided fields are updated.

**Response 200:** Returns the updated pair object.

**Response 404** if the session ID does not exist:
```json
{ "error": { "code": "SESSION_NOT_FOUND", "message": "Session 'gs_abc123' not found.", "detail": null } }
```

**Response 404** if the pair ID does not exist within the session:
```json
{ "error": { "code": "PAIR_NOT_FOUND", "message": "Pair 'p_001' not found in session 'gs_abc123'.", "detail": null } }
```

---

```
POST /goldstandard/regenerate
```

Regenerates one specific pair. Preserves all other pairs in the session.

**Request body:**
```json
{
  "session_id": "gs_abc123",
  "pair_id": "p_001"
}
```

**Response 200:** Returns the single regenerated pair object (same schema as above). The regenerated pair's `status` is reset to `"pending"`.

**Response 404** if the session or pair ID does not exist (same error format as PATCH above).

**Response 409** (`SESSION_BUSY`) if called while the session's `status` is still `"generating"`.

---

```
POST /goldstandard/save
```

Saves reviewed pairs as a RAGAS-compatible JSON file. Only `approved` and `edited` pairs are included. `rejected` and `pending` pairs are excluded.

Stale/orphaned sessions return **409 `HISTORICAL_SESSION`** before writing unless the caller explicitly supplies boolean `allow_historical: true` (default false; numeric/string substitutes are rejected). This permits historical inspection/export, not use as a current collection baseline. `historical` and `session_validity` in the response report the captured validity decision. The exported RAGAS array retains its four standard fields and does not embed validity warnings; consumers must retain the session metadata separately. This request-time check does not add persistence locking or a transactional snapshot; those are separate work.


**Request body:**
```json
{
  "session_id": "gs_abc123",
  "filename": null,
  "allow_historical": false
}
```

`filename`: if null, defaults to `{collection}_{YYYYMMDD_HHMMSS}.json` where `collection` is taken from the session's own collection name.

**Response 404** if the session does not exist (uses `SESSION_NOT_FOUND` error code).

**Response 200:**
```json
{
  "filename": "Documents_20260910_143022.json",
  "pairs_saved": 17,
  "pairs_excluded": 3,
  "download_url": "/api/goldstandard/download/Documents_20260910_143022.json",
  "historical": false,
  "session_validity": {
    "stale": false, "stale_reason": null, "stale_at": null,
    "orphaned": false, "orphaned_reason": null, "orphaned_at": null
  }
}
```

**RAGAS output format (per pair):**
```json
{
  "question": "What is the minimum notice period for schedule changes?",
  "answer": "The minimum notice period is 72 hours.",
  "contexts": ["...chunk text used to generate the answer..."],
  "ground_truth": "The minimum notice period is 72 hours."
}
```

- `answer`: the LLM-generated answer (may have been edited by the reviewer)
- `ground_truth`: the human-verified correct answer; initially identical to `answer` from generation, but the reviewer may have edited it separately via the PATCH endpoint before export
- `contexts`: the source chunk(s) passed to the LLM during generation — used by RAGAS to evaluate context precision and recall

The file is a JSON array of these objects.

---

```
GET /goldstandard/download/{filename}
```

Returns the saved file as a JSON download (`Content-Disposition: attachment`).

**Response 404** if the file does not exist:
```json
{ "error": { "code": "FILE_NOT_FOUND", "message": "File 'Documents_20260910_143022.json' not found.", "detail": null } }
```

---

#### 3.1.6 Metrics

```
GET /metrics/latency
```

Returns rolling aggregate statistics AND a time-series history for charting. The API service maintains an in-memory ring buffer of the last 500 query records, persisted to `{UPLOAD_DIR}/metrics.jsonl` for survival across restarts.

**Query parameters:** `collection`, `retrieval_mode` (both optional filters), and
`history_limit` (integer, 0-500, default 100).

**Response 200:**
```json
{
  "total_records": 14,
  "retrieval_latency": { "p50": 476.0, "p95": 2244.8, "p99": 2498.6, "mean": 753.6, "count": 14 },
  "llm_latency": { "p50": 11932.0, "p95": 274280.0, "p99": 288536.8, "mean": 50379.4, "count": 14 },
  "total_latency": { "p50": 12334.0, "p95": 276524.8, "p99": 291035.4, "mean": 51133.0, "count": 14 },
  "history": [
    {
      "timestamp": "2026-09-13T20:43:49.756840+00:00",
      "collection": "Mcpcheck",
      "retrieval_mode": "hnsw",
      "retrieval_ms": 385,
      "llm_ms": 9365,
      "total_ms": 9750
    }
  ]
}
```

`total_records` is the number of records in the ring buffer after filtering (max
500), not the total number of queries ever made. Each `*_latency` object carries
`p50`, `p95`, `p99`, `mean` and `count`.

`history` holds the most recent `history_limit` records in **ascending timestamp
order**, so a chart reads left to right. It is bounded deliberately: the buffer
holds up to 500 entries and the Health Dashboard polls every 30 seconds, so an
unbounded array would grow the payload with no benefit. `history_limit=0` returns
an empty array; a value above 500 is rejected with `422`.

The Health Dashboard renders `history` as trend lines and the three `*_latency`
objects as the aggregate stat row.

---

### 3.2 Error Response Format

All errors use a consistent envelope:

```json
{
  "error": {
    "code": "COLLECTION_NOT_FOUND",
    "message": "Collection 'Documents' does not exist.",
    "detail": null
  }
}
```

Standard error codes:

| Code | HTTP Status | Meaning |
|---|---|---|
| `COLLECTION_NOT_FOUND` | 404 | Named collection does not exist |
| `COLLECTION_EXISTS` | 409 | Collection already exists |
| `INVALID_FILE_TYPE` | 422 | Uploaded file type not supported |
| `JOB_NOT_FOUND` | 404 | Ingest job ID not found |
| `SERVICE_UNAVAILABLE` | 503 | Weaviate or Ollama unreachable |
| `INVALID_PARAMETER` | 422 | Request parameter out of range or invalid |
| `INVALID_SETTINGS` | 422 | Multipart chunking settings invalid before ingest work |
| `SESSION_NOT_FOUND` | 404 | Gold standard session ID not found |
| `CONFIRMATION_REQUIRED` | 400 | Destructive operation called without `?confirm=true` |
| `FILE_NOT_FOUND` | 404 | Requested download file does not exist |
| `PAIR_NOT_FOUND` | 404 | pair_id not found within the given session |
| `SESSION_BUSY` | 409 | Operation not allowed while session is still generating |
| `HISTORICAL_SESSION` | 409 | Explicit historical export choice required for stale/orphaned session |

---

### 3.3 Python Package Dependencies

```
fastapi>=0.111
uvicorn[standard]>=0.30
weaviate-client>=4.6
httpx>=0.27
unstructured[pdf,docx,csv]>=0.14
langchain-text-splitters>=0.3
sentence-transformers>=3.0
python-multipart>=0.0.9
aiofiles>=23.0
```

**Dependency pinning (normative).** The list above is *intent*, held in
`api/requirements.in`. It is **not** what the build installs. `api/requirements.txt`
is a generated lock containing every package pinned to an exact version, and the
Dockerfile installs the lock. Without this, an unpinned rebuild resolves whatever
is current: that is exactly how `weaviate-client` drifted to a release requiring a
newer Weaviate server than the one pinned here, breaking every collection call
while the stack still reported healthy.

Regenerate the lock from the repo root after editing `requirements.in`:

```bash
docker compose build api
docker run --rm rag-docker-api:latest pip freeze \
  | grep -viE '^(torch|torchvision)==' | LC_ALL=C sort > /tmp/pins.txt
awk '/^[a-zA-Z0-9]/{exit} {print}' api/requirements.txt > /tmp/header.txt
cat /tmp/header.txt /tmp/pins.txt > api/requirements.txt
docker compose build api          # confirm the lock installs cleanly
```

`LC_ALL=C` keeps ordering stable so a re-lock produces a clean diff.

**torch is installed separately and deliberately.** `api/Dockerfile` installs
`torch` and `torchvision` from the PyTorch **CPU** index
(`--index-url https://download.pytorch.org/whl/cpu`, pinned to `torch==2.14.0`
and `torchvision==0.29.0`) *before* resolving `requirements.txt`. The default PyPI wheel for linux/aarch64 declares the full
NVIDIA CUDA dependency set — about 3.3 GB of `nvidia-*` packages plus 800 MB of
Triton — none of which can execute without an NVIDIA GPU. Installing the CPU
build first satisfies the requirement so the lock install leaves it alone, taking
the image from 7.6 GB to 2.8 GB. The two pins are therefore **excluded from the
lock**: their `+cpu` local versions are not published on PyPI, so listing them
would break the build.

**Build contexts must carry a `.dockerignore`.** Both `api/` and `ui/` have one.
For `ui/` this is load-bearing rather than tidiness: the Dockerfile runs
`COPY package*.json ./` → `npm ci` → `COPY . .`, so without the exclusion a host
`node_modules/` would overwrite the container's freshly installed tree with
packages built for the host platform.

**Note on semantic chunking:** `sentence-transformers` is used locally within the API service to compute sentence-level similarity for the `semantic` chunking strategy. It does not require a separate service and uses `all-MiniLM-L6-v2`. The model weights (~90MB) must be downloaded at Docker **build** time (not runtime) to preserve the self-contained constraint. The `api/Dockerfile` includes a `RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"` step to pre-cache the model in the image.

---

#### 3.1.7 Export

```
POST /export
```

Starts an export of one collection into a portable package under `./exports`.
Long-running, so it follows the same job-and-poll shape as `/ingest/upload`.

**Request body:**
```json
{ "collection": "Policies", "include_models": false }
```

`include_models: true` bundles the embedding model and the LLM into the package
(`RAG_EXPORT_SPECIFICATIONS.md` §6.3), taking it from roughly 80 KB to ~2.3 GB.
The job reports `models_bundled` — what the package actually carries, which is
`false` with an explanation in `warnings` if the request could not be honoured.

**Response 202:**
```json
{ "job_id": "0c5fd445", "status": "queued", "collection": "Policies" }
```

- **404 `COLLECTION_NOT_FOUND`** if the collection does not exist. Checked
  before a job is created, so a typo does not leave a failed job behind.
- **409 `EXPORT_IN_PROGRESS`** if an export of the same collection is already
  running; `detail.job_id` names it. Two concurrent exports of one collection
  would race on the staging directory and the temporary archive. Exports of
  *different* collections run concurrently.

The build runs off the event loop, so an export does not block ingest or query.
Chunks are streamed one at a time and never accumulated: a 100k-chunk corpus is
roughly 880 MB of JSON.

---

```
GET /export/job/{job_id}
```

**Response 200:**
```json
{
  "job_id": "0c5fd445",
  "status": "completed",
  "collection": "Policies",
  "chunks_written": 1842,
  "filename": "ragpkg-policies-20260918T153127Z-c5c39e72.tar.gz",
  "size_bytes": 22273,
  "source_document_count": 37,
  "fidelity": "with-sources",
  "models_bundled": false,
  "retrieve_script": true,
  "warnings": [],
  "error": null
}
```

`status` is `queued`, `running`, `completed` or `failed`. On failure `error`
carries the reason and `filename` stays null. A failed build leaves nothing
behind: the staging directory and the partial archive are both removed, and the
collection's export slot is released.

`warnings` is non-fatal. It records, for example, chunks whose `source_sha256`
could not be resolved to exactly one retained document.

**404 `JOB_NOT_FOUND`** for an unknown id.

---

```
POST /import
```

Imports a package from `./exports` into this instance. Long-running, so it
follows the job-and-poll pattern.

**Request body:**
```json
{ "filename": "ragpkg-policies-20260918T153127Z-c5c39e72.tar.gz", "on_conflict": "abort" }
```

`on_conflict` is **required and has no default** — silently choosing a policy
could destroy a collection the caller did not mean to touch. It is one of
`abort`, `rename` or `replace` (spec §6.4).

**Response 202:** `{ "job_id": "...", "status": "queued", "filename": "..." }`

**409 `IMPORT_IN_PROGRESS`** if the same file is already being imported.

Everything else — unreadable archive, bad digest, embedding mismatch, name
collision — is reported through the job rather than this response, because none
of it is knowable without reading the package.

---

```
GET /import/job/{job_id}
```

**Response 200:**
```json
{
  "job_id": "75f3a192",
  "status": "completed",
  "filename": "ragpkg-policies-20260918T153127Z-c5c39e72.tar.gz",
  "on_conflict": "rename",
  "collection": "Policies_imported_b602671d",
  "original_collection": "Policies",
  "chunks_written": 20,
  "fidelity": "with-sources",
  "renamed": true,
  "notes": ["3 gold-standard session(s) restored"],
  "error": null,
  "error_code": null,
  "error_detail": null
}
```

`collection` is the name actually imported under and differs from
`original_collection` when `rename` resolved a collision. On failure,
`error_code` is one of the codes in `RAG_EXPORT_SPECIFICATIONS.md` §11 and
`error_detail` names the offending file, model or collection.

Validation order, the conflict policies and the atomicity guarantees are
specified in `RAG_EXPORT_SPECIFICATIONS.md` §6.

---

```
POST /tune/rechunk
POST /tune/reembed
POST /tune/reindex
```

Rebuild a collection in place (`RAG_EXPORT_SPECIFICATIONS.md` §7). All three are
long-running and return `202` + `job_id`.

| Endpoint | Does | Requires |
|---|---|---|
| `/tune/rechunk` | re-splits the retained originals and rebuilds | `with-sources` |
| `/tune/reembed` | regenerates vectors; with chunking fields it re-chunks too | chunking fields need `with-sources` |
| `/tune/reindex` | changes `index_type` or `distance_metric`, reusing the existing vectors | nothing |

Chunking fields (`chunking_strategy`, `chunk_size`, `chunk_overlap`,
`similarity_threshold`, `min_chunk_size`) are optional; an omitted field takes the
ingest default.

- **404 `COLLECTION_NOT_FOUND`** for an unknown collection.
- **409 `TUNE_IN_PROGRESS`** if the collection is already being tuned.
- **`SOURCES_REQUIRED`** (through the job) when the operation needs originals the
  collection does not have. Re-embedding a `chunks-only` collection *with*
  chunking fields is refused rather than half-honoured.

Each rebuild is staged into a temporary collection and swapped in only once it
succeeds, so a failure leaves the original untouched. Vectors are copied out of
the staging collection rather than regenerated, so the corpus is embedded once.

Any operation that changes chunk identity marks the collection's gold-standard
sessions `stale`. `/tune/reindex` does not, because chunk identity is unchanged.

---

```
GET /tune/{collection}
```

What this collection can be tuned with, given its fidelity.

**Response 200:**
```json
{
  "collection": "Policies",
  "fidelity": "with-sources",
  "source_document_count": 37,
  "can_rechunk": true,
  "can_reembed": true,
  "can_reindex": true,
  "note": "Every tuning operation is available."
}
```

---

```
GET /tune/job/{job_id}
```

Reports `status`, `chunks_total`, `chunks_written`, `notes` (including how many
gold-standard sessions were marked stale) and, on failure, `error_code`.

---

```
GET /help/transfer
```

Returns the export/import help page as markdown.

**Response 200:** `{ "topic": "transfer", "markdown": "# Moving a RAG between machines\n…" }`

Rendered server-side from `api/templates/help_transfer.md.tmpl` and the shared
partials in `api/templates/partials/`, which are the same source the `README.md`
inside every package is built from (`RAG_EXPORT_SPECIFICATIONS.md` §10). The UI
renders the markdown rather than holding its own copy, so the page cannot drift
from what ships inside a package. The embedding dimensions quoted in the page
come from a real embedding call, not a constant.

---

```
GET /packages
```

Lists the packages sitting in `./exports` — both those exported here and those
dropped in to be imported.

**Response 200:**
```json
{
  "packages": [
    {
      "filename": "ragpkg-policies-20260918T153127Z-c5c39e72.tar.gz",
      "size_bytes": 22273,
      "collection": "Policies",
      "chunk_count": 1842,
      "fidelity": "with-sources",
      "created_at": "2026-09-18T15:31:27Z",
      "readable": true
    }
  ]
}
```

A file that cannot be read as a package is still listed, with `readable: false`
and null metadata, so it does not silently disappear from the UI. Import reports
precisely why it cannot be used.

The package format itself — filename convention, layout, manifest and
`chunks.jsonl` — is specified in `RAG_EXPORT_SPECIFICATIONS.md` §4.

---

## 4. Weaviate Schema Specification

### 4.1 Collection Properties

Every collection created through this system uses this fixed property schema:

| Property | Weaviate Type | Indexing | Description |
|---|---|---|---|
| `content` | `text` | BM25 + vector | Chunk text content |
| `source_file` | `text` | filterable only (BM25 disabled) | Original filename |
| `source_type` | `text` | filterable only (BM25 disabled) | `pdf`, `docx`, `txt`, `md`, `csv`, `json` |
| `chunk_index` | `int` | filterable | Zero-based position of chunk within source document |
| `chunk_strategy` | `text` | filterable only (BM25 disabled) | `fixed`, `overlap`, `semantic`, `context_aware`, `language` |
| `chunk_size` | `int` | filterable | Configured chunk size at ingest time |
| `chunk_overlap` | `int` | filterable | Configured overlap at ingest time (0 if not applicable) |
| `created_at` | `date` | filterable | ISO 8601 UTC timestamp |

BM25 indexing is disabled on all metadata properties (`source_file`, `source_type`, `chunk_strategy`) to prevent them from polluting hybrid search keyword results. Only `content` participates in BM25 and vector search.

### 4.2 Vector Configuration

- **Vectorizer module:** `text2vec-ollama`
- **Model:** `nomic-embed-text`
- **Dimensions:** 768
- **Distance metric:** configurable at collection creation (default: cosine)

### 4.3 Index Configuration Defaults

| Parameter | Default | Range | Description |
|---|---|---|---|
| `efConstruction` | 128 | 64–512 | Build-time accuracy/speed tradeoff |
| `maxConnections` | 64 | 16–128 | Connections per node in graph |
| `ef` | 64 | 16–512 | Query-time accuracy/speed tradeoff |

Flat (KNN) index has no tunable parameters.

---

## 5. Ingest Pipeline Specification

### 5.1 Supported File Types and Parsers

| Type | Extension | Parser | Notes |
|---|---|---|---|
| PDF | `.pdf` | Unstructured (`partition_pdf`) | Extracts text and tables; OCR applied automatically for scanned PDFs |
| Word | `.docx` | Unstructured (`partition_docx`) | Preserves heading hierarchy for context-aware chunking |
| Plain text | `.txt` | Unstructured (`partition_text`) | UTF-8 encoding assumed |
| Markdown | `.md` | Unstructured (`partition_md`) | Heading structure preserved |
| CSV | `.csv` | Unstructured (`partition_csv`) | Each row treated as a text chunk candidate |
| JSON | `.json` | Unstructured (`partition_json`) | Flat key-value pairs converted to text; nested objects flattened |

For ZIP uploads: extract to temp directory, process all files with supported extensions, skip unsupported files (logged as warnings).

### 5.2 Chunking Strategy Specifications

#### Fixed Size (`fixed`)
- Splitter: `CharacterTextSplitter`
- Parameters: `chunk_size`, `min_chunk_size`
- Behavior: splits on character count with no intentional overlap. Sentence boundaries not respected.

#### Fixed Size with Overlap (`overlap`)
- Splitter: explicit character windows, independent of paragraph/word separators
- Parameters: `chunk_size`, `chunk_overlap`, `min_chunk_size`
- Per-file pre-merge output limits: at most 10,000 windows and 10,000,000 total window characters, including repeated overlap. Count and total payload are computed before allocating windows. Excess fails that file with a clear ingest-job error; other files can continue. These are candidate/payload character bounds, not parser or encoded-byte limits. The optional final-tail merge does not relax the pre-allocation limits.
- Behavior: consecutive windows share exactly `chunk_overlap` characters. Nonblank
  text is covered in order, including internal and boundary whitespace; blank-only
  input yields no chunks. Python character counts, rather than encoded byte counts,
  determine window sizes. Long tokens and single-newline parser text remain bounded.
- Window size must be positive, `0 <= chunk_overlap < chunk_size`, and
  `min_chunk_size >= 0`; impossible size/overlap values are rejected.
- The final undersized window is merged by appending only its new suffix, removing
  duplicated overlap without adding a separator. All other windows are at most
  `chunk_size`; the final output is at most
  `chunk_size + max(0, min(chunk_size, min_chunk_size - 1) - chunk_overlap)`
  characters. With defaults
  (size 1000, overlap 200, minimum 100), every chunk is at most 1000 characters. A whole
  document shorter than the minimum remains one short chunk; no text is fabricated.
  Minimum size applies to the final-window merge preference. A minimum above the
  target does not combine every full window; the final output is still bounded by
  at most two windows minus their overlap.

#### Language-Based (`language`)
- Splitter: `RecursiveCharacterTextSplitter`
- Parameters: `chunk_size`, `chunk_overlap`, `min_chunk_size`
- Separators (in priority order): `\n\n`, `\n`, `. `, ` `, `""` (empty string — split on any character as last resort)
- Behavior: attempts to split at natural language boundaries before falling back to character splitting.

#### Context-Aware (`context_aware`)
- Splitter: Unstructured element boundaries
- Parameters: `chunk_size`, `min_chunk_size`
- Behavior: uses document element types (Title, NarrativeText, Table, ListItem) as natural chunk boundaries. Adjacent elements of the same type are merged until `chunk_size` is exceeded. Tables are kept intact as single chunks regardless of size.

#### Semantic (`semantic`)
- Splitter: sentence-level cosine similarity with `all-MiniLM-L6-v2`
- Parameters: `similarity_threshold`, `min_chunk_size`
- Behavior: sentences with cosine similarity above `similarity_threshold` are merged into the same chunk. A new chunk begins when similarity drops below the threshold. This is the slowest strategy.

### 5.3 Minimum Chunk Enforcement

Overlap uses the final-window policy and size bound in §5.2. Other strategies
use a shared merge pass: a chunk below `min_chunk_size` is appended to its
predecessor with a space when a predecessor exists. A short initial chunk can
remain short. That shared pass can extend a preceding chunk beyond `chunk_size`;
context-aware tables and semantic chunks also have no hard size cap. Those
algorithms are outside the overlap size guarantee. All size parameters throughout
the pipeline are in characters.

### 5.4 Embedding and Storage

The `text2vec-ollama` Weaviate module handles embedding automatically when objects are inserted — Weaviate calls Ollama internally for each object's `content` field. The API service does **not** call Ollama's embedding endpoint directly for ingest.

For each chunk, the API service creates a Weaviate object with all properties populated. Embedding is generated by Weaviate automatically on insert via the configured `text2vec-ollama` module.

Inserts are batched: up to 50 objects per batch using the weaviate-client v4 context-manager pattern (`with client.batch.dynamic() as batch: batch.add_object(...)`).

**Query-time embedding paths:**

| Retrieval mode | Weaviate query type | Who embeds the query |
|---|---|---|
| `hnsw` | `near_vector` | API calls Ollama `/api/embeddings` directly |
| `flat` | `near_vector` | API calls Ollama `/api/embeddings` directly |
| `hybrid` | `hybrid` (BM25 + vector) | Weaviate embeds the vector component via `text2vec-ollama` |
| `semantic` | `near_text` | Weaviate embeds via `text2vec-ollama` |

---

## 6. LLM Prompt Specifications

All prompts use Ollama's `/api/chat` endpoint with `model: phi3.5`.

### 6.1 Query Reformulation Prompt

**System:**
```
You are a search query optimizer. Given a user's question, rewrite it as a concise search query that maximizes retrieval of relevant text chunks from a vector database. Return only the rewritten query with no explanation.
```

**User:**
```
Original question: {user_question}
```

### 6.2 RAG Synthesis Prompt — End User Format

**System:**
```
You are a helpful assistant. Answer the user's question using only the provided context. If the context does not contain enough information to answer the question, say so clearly. Do not use any knowledge outside the provided context. Write in plain, clear language for a non-technical reader.
```

**User:**
```
Context:
[1] {chunk_1_text}

[2] {chunk_2_text}

...

Question: {original_question}
```

### 6.3 RAG Synthesis Prompt — Engineer Format

**System:**
```
You are a precise technical assistant. Answer the user's question using only the provided context. Include relevant technical details. Indicate the confidence level of your answer (high/medium/low) based on how directly the context addresses the question. If the context is insufficient, state this explicitly.
```

**User:**
```
Context:
[1] (source: {source_file_1}, chunk {chunk_index_1}) {chunk_1_text}

[2] (source: {source_file_2}, chunk {chunk_index_2}) {chunk_2_text}

...

Question: {original_question}
```

Each retrieved chunk is numbered sequentially. The engineer format includes source metadata per chunk to support confidence reasoning. Both formats pass the original (unrewritten) question to the synthesis step.

### 6.4 Gold Standard Generation Prompt

**System:**
```
You are creating evaluation data for a RAG system. Given a text chunk, generate one question that can be answered from this chunk, the correct answer based only on this chunk, and a ground truth answer (same as the answer). Return a JSON object with keys: question, answer, ground_truth. Do not include any text outside the JSON object.
```

**User:**
```
Chunk:
{chunk_text}
```

**Expected output (parsed as JSON):**
```json
{ "question": "...", "answer": "...", "ground_truth": "..." }
```

If the model output is not valid JSON, the API retries once with an explicit reminder to return only JSON.

---

## 7. Web UI Specification

**Framework:** React 18 + Vite 5  
**Component library:** shadcn/ui + Tailwind CSS  
**State management:** React Context + useReducer (no external state library)  
**Routing:** React Router v6

### 7.1 Role Selection

**Route:** `/`

On first load (or when no role in session storage), the landing page displays three role cards:

| Role | Label | Description shown to user |
|---|---|---|
| `engineer` | AI Engineer | Full access: ingest, chunking, retrieval, gold standard, collection management, health |
| `developer` | Developer | Ingest, simplified configuration, Q&A, gold standard |
| `end_user` | End User | Q&A only |

Selecting a role stores `{ role: "<selected_role>" }` (e.g. `{ role: "engineer" }`, `{ role: "developer" }`, or `{ role: "end_user" }`) in `sessionStorage` under key `rag_role` and navigates to `/qa`.

A "Switch Role" button is always visible in the navigation bar, which returns to this page.

### 7.2 Navigation Bar

Visible on all pages post-role-selection. Contents vary by role:

| Nav Item | Engineer | Developer | End User |
|---|---|---|---|
| Q&A | ✓ | ✓ | ✓ |
| Import Documents | ✓ | ✓ | — |
| Chunking Config | ✓ | ✓ | — |
| Retrieval Config | ✓ | ✓ | — |
| Gold Standard | ✓ | ✓ | — |
| Collections | ✓ | — | — |
| Health | ✓ | — | — |
| Switch Role | ✓ | ✓ | ✓ |

### 7.3 Q&A Page

**Route:** `/qa`  
**Roles:** All

**Elements:**

- Collection selector dropdown (populated from `GET /collections`). Defaults to first available collection.
- Question text input (multi-line, submit via "Ask" button or Ctrl+Enter; plain Enter inserts a newline).
- Citations toggle (checkbox, off by default, labeled "Show source citations").
- Answer display area (markdown-rendered).
- Citation list (shown below answer when toggle is on): source file, chunk index, relevance score, excerpt.
- Latency display (small, below answer): `Retrieved in Xms · Generated in Xms`.
- Retrieval mode selector — visible to Engineer and Developer roles only. Hidden for End User. Read-only: it displays the mode saved for the selected collection, which is changed on the Retrieval Config page. The settings are fetched from `GET /retrieval/config/{collection}` whenever the selected collection changes; if nothing is saved for that collection the API returns the defaults (`hnsw`, `top_k: 5`).

The collection selection is shared with the Retrieval Config page, because retrieval settings are stored per collection and must follow the selection.

**Response format** sent to API:
- End User role → `response_format: "end_user"`
- Engineer and Developer → `response_format: "engineer"`

### 7.4 Import Documents Page

**Route:** `/import`  
**Roles:** Engineer, Developer

**Elements:**

- Drag-and-drop file upload zone. Accepts: `.pdf`, `.docx`, `.txt`, `.md`, `.csv`, `.json`, `.zip`.
- File list showing queued files with remove buttons.
- Collection selector (populated from `GET /collections`). Includes a "Create new collection" option that opens a modal with a name field and index type selector (defaults: HNSW, cosine). On confirm, calls `POST /collections` before submitting ingest.
- Chunking strategy selector (dropdown). On selection, displays the plain-language explanation for the chosen strategy.
- Strategy parameters form (shown below strategy selector):
  - Engineer and Developer both see: chunk size slider, overlap slider (hidden for `semantic` and `context_aware`), minimum chunk size.
  - Engineer additionally sees: similarity threshold slider (visible only when `semantic` is selected).
- "Start Ingest" button → calls `POST /ingest/upload`, displays job ID.
- Progress panel: polls `GET /ingest/job/{job_id}` every 3 seconds. Shows files completed/total, chunks stored, errors.

**Chunking strategy explanations** (shown in an info box when a strategy is selected):

| Strategy | Explanation text |
|---|---|
| Fixed Size | Splits your document into equal-sized pieces by character count. Simple and fast, but may cut sentences in the middle. Best for structured data like CSV or JSON. |
| Fixed Size with Overlap | Like Fixed Size, but each piece shares some text with the next one. This helps the system find answers that fall near a boundary. A good general-purpose choice. |
| Language-Based | Splits at natural sentence and paragraph breaks before falling back to character count. Keeps sentences intact. Recommended for most narrative documents. |
| Context-Aware | Uses the document's own structure — headings, paragraphs, tables — to define boundaries. Best for structured reports, policies, or manuals with clear section headings. |
| Semantic | Groups sentences that are about the same topic together, regardless of their position. Produces the most meaningful chunks but is the slowest option. Best for long, dense documents. |

### 7.5 Chunking Config Page

**Route:** `/chunking`  
**Roles:** Engineer, Developer

**Elements:**

- Collection selector (populated from `GET /collections`).
- Current configuration display (from `GET /ingest/config/{collection}` for the selected collection). Shows a "using defaults" notice when `is_default: true`.
- Editable parameter form: chunking strategy selector, chunk size, overlap, similarity threshold, min chunk size. Controls follow the same visibility rules as the Import page (overlap hidden for `semantic`/`context_aware`; similarity threshold visible only for `semantic`).
- "Save as Default" button → calls `POST /ingest/config` with the collection in the request body (upsert — creates on first save, overwrites on subsequent saves). Does not re-process existing documents. Shows a confirmation notice after successful save.

### 7.6 Retrieval Config Page

**Route:** `/retrieval`  
**Roles:** Engineer, Developer

Retrieval configuration is stored **per collection**, on the server, via
`GET`/`POST /retrieval/config` (Section 3.1.4). It therefore survives a restart,
is shared by every browser, and travels with a RAG export. It is not held in
`sessionStorage`.

**Elements (selection):**

- Collection selector dropdown (populated from `GET /collections`), shared with
  the Q&A page. Changing it loads that collection's saved settings into the form.
- A status line below the selector reading either "No settings saved for this
  collection yet — showing defaults." or "Showing the settings saved for this
  collection.", driven by the `is_default` flag.
- If the settings cannot be loaded, the form falls back to the defaults and shows
  a non-blocking warning; Q&A stays usable.

**Elements:**

- Retrieval mode selector with explanation panel:

| Mode | Explanation text |
|---|---|
| HNSW — Approximate (default) | The fastest option. Uses a smart graph to find the closest matches quickly. May very rarely miss the single best result, but works well for almost all use cases. |
| Flat — Exact | Checks every stored chunk to find the mathematically perfect match. More accurate but slower as your collection grows. Best for collections under 10,000 chunks. |
| Hybrid | Combines keyword search with meaning-based search. Best when your questions include specific terms, names, or codes. Adjust the slider to balance between the two modes. |
| Semantic | Pure meaning-based search. Best for conceptual questions where the exact words are less important than the idea. |

- Top-K slider (1–20, default 5).
- Hybrid alpha slider (visible only when Hybrid selected, range 0.0–1.0, default 0.75, labeled "Keyword ← Balance → Meaning").
- HNSW advanced parameters accordion (Engineer role only, collapsed by default):
  - `ef` slider (16–512)
  - `efConstruction` slider (64–512)
  - `maxConnections` slider (16–128)
  - Each parameter has an explanation tooltip.
- "Save for this collection" button — `POST /retrieval/config` with the full
  configuration. Disabled while no collection is selected or while settings are
  loading. On success the button shows a transient "Saved!" confirmation.
```json
{
  "collection": "Documents",
  "retrieval_mode": "hybrid",
  "top_k": 5,
  "alpha": 0.75,
  "ef": null,
  "response_format": "engineer"
}
```
`ef` is sent as `null` unless `retrieval_mode` is `"hnsw"`. `alpha` is always
sent and is only applied by the server for `"hybrid"`. `response_format` records
the active role at the time of saving (End User → `end_user`, Engineer and
Developer → `engineer`) so an exported retrieval script reproduces the same
answer style.

`efConstruction` and `maxConnections` are collection build-time properties set
when the collection is created; the sliders shown here are informational and are
not part of the saved retrieval configuration.

### 7.7 Gold Standard Page

A retained session can be loaded/refreshed by its session ID, including when its collection no longer exists. Its collection and ID remain visible. Stale/orphaned sessions show reasons and recorded timestamps above review/export; missing legacy metadata has clear defaults. Loading or receiving new validity metadata resets the historical-export checkbox. A new lookup clears the previous session and export controls before fetching, including when the lookup fails. Export shows a disabled progress state while its request is pending, and duplicate clicks cannot start another request.

**Route:** `/goldstandard`  
**Roles:** Engineer, Developer

**Phase 1 — Generate:**
- Collection selector.
- Sample size input (1–100, default 20).
- "Generate Pairs" button → calls `POST /goldstandard/generate`, receives `session_id`. Shows progress bar during generation.
- Generation progress: polls `GET /goldstandard/session/{session_id}` every 2 seconds. Shows `pairs_completed / pairs_total`. Pairs appear in the review table as they complete (partial display during generation).
- If a session already exists in the UI (from a previous generation), clicking "Generate Pairs" displays a confirmation dialog: "This will start a new session and replace the current one. Any unsaved pairs will be lost. Continue?" Previous sessions remain on the server (accessible via direct session ID) but are no longer referenced by the UI after confirmation.

**Phase 2 — Review:**
- Table of generated pairs. Columns: Question, Answer, Source File, Status, Actions.
- Each row expandable to show full context chunk.
- Actions per row:
  - Approve (green check) → calls `PATCH /goldstandard/session/{session_id}/pair/{pair_id}` with `{ "status": "approved" }`
  - Edit inline (pencil) → opens edit form with three editable fields: question, answer, ground_truth; on save calls `PATCH` with `{ "status": "edited", "question": "...", "answer": "...", "ground_truth": "..." }` (only changed fields included)
  - Reject (red X) → calls `PATCH` with `{ "status": "rejected" }`
  - Regenerate (refresh icon) → calls `POST /goldstandard/regenerate` with `session_id` and `pair_id`, replaces row with returned pair
- Summary bar: `X approved · Y edited · Z rejected · W pending`

**Phase 3 — Export:**
- "Export Approved" button (disabled until at least 1 approved or edited pair exists).
- Historical sessions use "Export Historical Approved" and additionally require an explicit checkbox acknowledging that the file omits validity warnings and is not a current baseline. The backend independently enforces the choice.
- Filename field: pre-filled with `{collection}_{timestamp}`, editable.
- Calls `POST /goldstandard/save` → on success, triggers download of the resulting JSON file.
- Export summary shown: `17 pairs exported, 3 excluded (rejected/pending)`.

### 7.8 Collections Page

**Route:** `/collections`  
**Roles:** Engineer only

- Table of collections: name, object count, index type, distance metric, created date.
- "New Collection" button → modal with fields: name, index type, distance metric, HNSW params.
- Delete button per row → confirmation modal with collection name typed to confirm. Calls `DELETE /collections/{name}?confirm=true`.

### 7.9 Health Dashboard Page

**Route:** `/health`  
**Roles:** Engineer only

- Service status cards: Weaviate, Ollama LLM, Ollama Embed. Each shows status (green/red), latency (ms).
- Latency trend charts (from the `history` field of `GET /metrics/latency`, bounded by `history_limit`, default 100): line chart of `total_ms` over time alongside `retrieval_ms` and `llm_ms`. Summary stat row shows P50/P95/P99 for each component from the `retrieval_latency`, `llm_latency` and `total_latency` objects.
- Auto-refreshes every 30 seconds.

---

### 7.10 Transfer Page

**Route:** `/transfer`  
**Roles:** AI Engineer, Developer

**Export elements:**

- Collection selector (from `GET /collections`).
- **Fidelity shown before the export runs**, from `GET /tune/{collection}`:
  `with-sources` with the document count, or `chunks-only` with what that costs.
  The choice is informed rather than discovered afterwards in the manifest.
- "Include the models" checkbox, labelled with the ~2.3 GB cost and when it is
  needed.
- Progress while the job runs, then the filename, size, fidelity, whether models
  were bundled and whether `retrieve.py` was generated. Warnings from the job are
  shown in full.

**Import elements:**

- Package selector listing `GET /packages`, each entry showing the collection,
  chunk count, fidelity and size. Unreadable files are listed too, marked as
  such, so nothing silently disappears.
- `on_conflict` radio group — `abort`, `rename`, `replace` — each with its
  consequence spelled out. There is no default in the API, and the page makes the
  difference explicit because the wrong choice can delete a collection.
- On completion: the name it was imported under, whether it was renamed, and
  every note the job returned (models installed or skipped, sessions orphaned,
  sessions restored).

Both halves poll their job endpoint every 2s and stop polling when the job
finishes or the page unmounts.

### 7.11 Transfer Help Page

**Route:** `/help/transfer`  
**Roles:** AI Engineer, Developer

Fetches `GET /help/transfer` and renders the returned markdown. The page holds no
copy of the content: it is generated from the same templates as the `README.md`
inside every package (`RAG_EXPORT_SPECIFICATIONS.md` §10), so the two cannot
drift. Markdown tables require `remark-gfm`, and readable headings require the
`@tailwindcss/typography` plugin — without it the `prose` classes compile to
nothing and Tailwind's preflight has already stripped heading styling, so the
page renders as one undifferentiated block.

---

## 8. Configuration Defaults and Constraints

The table gives API request bounds; narrower UI sliders are presentation choices. Internal import/rebuild preserves positive stored HNSW construction/connections settings and stored `ef=-1` (dynamic) or positive values beyond new-request limits. Index/distance enums and numeric type validation still apply; no clamping or migration is performed.

The `chunk_size` and `min_chunk_size` bounds apply whenever chunk settings are saved (`POST /ingest/config`) or used (`POST /ingest/upload`, `POST /tune/rechunk`, `POST /tune/reembed`). A saved or imported configuration from before the bounds is still returned by `GET /ingest/config/{collection}` and exported as it is, never clamped; saving it again, or using its values, requires them to be within the bounds.

| Parameter | Default | Min | Max | Notes |
|---|---|---|---|---|
| `chunk_size` | 1000 | 50 | 6000 | Characters. The minimum stops a flood of tiny chunks; the maximum keeps a chunk within what the embedding model reads (about 1,500 tokens), so nothing is silently truncated |
| `chunk_overlap` | 200 | 0 | Strategy-dependent | Repeated characters between adjacent chunks; must be less than `chunk_size` for overlap/language, ignored by other strategies |
| `similarity_threshold` | 0.85 | 0.0 | 1.0 | Semantic chunking only |
| `min_chunk_size` | 100 | 0 | 6000 | Soft merge preference in characters; may exceed the split target |
| `top_k` | 5 | 1 | 50 | API bounds |
| `alpha` | 0.75 | 0.0 | 1.0 | Hybrid mode only |
| `ef` | 64 | 16 | 512 | HNSW query param |
| `efConstruction` | 128 | 64 | 512 | HNSW build param |
| `maxConnections` | 64 | 16 | 128 | HNSW build param |
| Gold standard `sample_size` | 20 | 1 | 100 | |

---

## 9. File and Directory Structure

```
rag-docker/
├── docker-compose.yml
├── exports/                    # Bind mount for export packages; contents never packaged
│   └── .gitkeep
├── ingest-inbox/               # Read-only mount for the MCP server's file ingest
│   └── .gitkeep
├── mcp/                        # MCP server — PARKED, not wired in (see MCP_*.md)
│   ├── Dockerfile
│   ├── .dockerignore
│   ├── requirements.in
│   ├── requirements.txt        # Generated lock
│   ├── server.py               # Entrypoint: logging first, then SDK, then tools
│   ├── mcpapp.py               # Shared MCPServer instance
│   ├── logging_setup.py        # stderr-only logging
│   ├── config.py
│   ├── ragclient.py            # HTTP client + error translation
│   ├── paths.py                # Inbox path validation
│   └── tools/                  # 15 tools across 5 modules
├── package.sh                  # Source-only zip (~150 KB; target needs internet)
├── package-offline.sh          # Self-contained bundle (~4.6 GB; no network needed)
├── install-offline.sh          # Installs from that bundle on an air-gapped Mac
├── proxy/
│   └── nginx.conf
├── ollama/
│   └── entrypoint.sh            # Starts server, pulls models, waits
├── api/
│   ├── Dockerfile
│   ├── .dockerignore
│   ├── requirements.in         # Direct dependencies — edit this one
│   ├── requirements.txt        # Generated lock (pinned); installed by the build
│   ├── main.py                  # FastAPI app entry point
│   ├── utils.py                 # Shared helpers (api_error response factory)
│   ├── routers/
│   │   ├── health.py
│   │   ├── collections.py
│   │   ├── ingest.py
│   │   ├── query.py
│   │   ├── retrieval_config.py  # Per-collection retrieval settings endpoints
│   │   ├── transfer.py          # /export, /import, /packages
│   │   ├── tuning.py            # /tune/rechunk, /reembed, /reindex
│   │   ├── help.py              # /help/transfer, rendered from the templates
│   │   ├── goldstandard.py
│   │   └── metrics.py           # Latency ring buffer + JSONL persistence
│   ├── services/
│   │   ├── weaviate_client.py   # Weaviate connection, schema, and query operations
│   │   ├── ollama_client.py     # Ollama LLM and embed calls
│   │   ├── ingest_pipeline.py   # Parse → chunk → embed → store
│   │   ├── chunker.py           # All five chunking strategies
│   │   ├── rag_pipeline.py      # Reformulate → retrieve → synthesize
│   │   ├── sources.py           # Retained original documents (content-addressed)
│   │   ├── retrieval_config.py  # Per-collection retrieval settings (atomic JSON)
│   │   ├── packager.py          # Package format: naming, digests, manifest, reader
│   │   ├── exporter.py          # Export job lifecycle and the per-collection guard
│   │   ├── importer.py          # Import job: validation order, conflicts, atomicity
│   │   ├── model_bundle.py      # Ollama manifest + blob copy, both directions
│   │   ├── tuning.py            # Re-chunk / re-embed / re-index, staged rebuilds
│   │   ├── system_info.py       # VM memory reporting for /health
│   │   └── goldstandard.py      # Generation, session management
│   ├── models/
│   │   └── schemas.py           # Pydantic request/response models
│   ├── templates/               # Rendered into packages and the help page
│   │   ├── package_readme.md.tmpl
│   │   ├── help_transfer.md.tmpl
│   │   ├── retrieve.py.tmpl
│   │   └── partials/            # Shared by the package README and help page
│   │       ├── contents.md
│   │       ├── embedding_rule.md
│   │       ├── encryption.md
│   │       ├── fidelity_table.md
│   │       ├── naming.md
│   │       └── retrieve_usage.md
│   └── config.py                # Environment variable config
├── ui/
│   ├── Dockerfile
│   ├── .dockerignore
│   ├── package.json
│   ├── package-lock.json       # Required — `npm ci` fails without it
│   ├── vite.config.ts
│   ├── src/
│   │   ├── main.tsx
│   │   ├── App.tsx
│   │   ├── router.tsx
│   │   ├── context/
│   │   │   ├── RoleContext.tsx
│   │   │   └── QueryConfigContext.tsx
│   │   ├── pages/
│   │   │   ├── LandingPage.tsx
│   │   │   ├── QAPage.tsx
│   │   │   ├── ImportPage.tsx
│   │   │   ├── ChunkingPage.tsx
│   │   │   ├── RetrievalPage.tsx
│   │   │   ├── GoldStandardPage.tsx
│   │   │   ├── TransferPage.tsx
│   │   │   ├── HelpTransferPage.tsx
│   │   │   ├── CollectionsPage.tsx
│   │   │   └── HealthPage.tsx
│   │   ├── components/
│   │   │   ├── NavBar.tsx
│   │   │   ├── RoleGate.tsx
│   │   │   ├── CitationsPanel.tsx
│   │   │   ├── ProgressPanel.tsx
│   │   │   ├── StrategyExplainer.tsx
│   │   │   └── LatencyCharts.tsx
│   │   └── api/
│   │       └── client.ts        # Typed fetch wrappers for all API endpoints
├── ANALYSIS.md
├── SPECIFICATIONS.md
├── IMPLEMENTATION.md
├── RAG_EXPORT_ANALYSIS.md       # Export / import: background
├── RAG_EXPORT_SPECIFICATIONS.md # Export / import: package format and rules
├── RAG_EXPORT_IMPLEMENTATION.md # Export / import: phased build plan
└── MCP_*.md                     # MCP server — PARKED
```

---

## 10. Acceptance Criteria

**All 32 verified 2026-09-21** against the running stack, and now automated in
`scripts/verify/` so they can be re-run on demand. Eight defects were found and
fixed; each is noted against the criterion that exposed it.

The eighth was found by the test suite itself, on its second run: deleting a
collection removed its sources and retrieval config but **not** its ingest
config, so a collection recreated under the same name silently inherited
chunking settings the user never chose (Section 8 rule 1 of
`RAG_EXPORT_SPECIFICATIONS.md` requires both configs to go). The path convention
had been written out three separate times — in the ingest router, in the
exporter, and needed a fourth time here — which is how the omission survived. It
now lives once, in `api/services/ingest_config.py`.

### 10.1 Ingest

- [x] Invalid ingest/saved settings are rejected before staging, jobs or configuration writes; valid defaults and fixed size/minimum preferences are retained.
      *`test_settings_validation.py` checks mocked work boundaries and persistence; `07_settings.sh` runs real HTTP rejection, unchanged-config and valid round-trip checks on an owned collection. Full affected ingest verification passes 18 checks.*
- [x] `chunk_size` is bounded to 50–6000 and `min_chunk_size` to 0–6000 wherever chunk settings are saved or used; a saved configuration from before the bounds is still returned and exported unchanged, and must be within them to be saved again or used for tuning (#53).
      *`test_settings_validation.py` saves and reads back both edges, rejects 49, 6001 and a minimum of 6001 without changing the saved configuration, and checks a saved 16000/8000 configuration is returned and exported unclamped but refused by save, rechunk and reembed. `07_settings.sh` checks both edges and their neighbours against the live stack.*

- [x] Single file upload (all six types) completes without error and stores chunks in Weaviate.
      *One file of each type. `.md` failed — `unstructured[pdf,docx,csv]` omitted
      the `md` extra, so Markdown ingestion had never worked. **Fixed**: added the
      extra and `Markdown==3.10.3` to the lock.*
- [x] ZIP batch upload extracts and processes all supported files; unsupported files are skipped with a warning.
      *A ZIP of 8 files yielded 6 processed, 2 skipped — but silently, with
      nothing naming what was dropped. **Fixed**: the job now carries a `skipped`
      list naming each file and its unsupported extension, shown by the UI.*
- [x] Each chunking strategy produces non-empty chunks for a test PDF.
      *fixed 3, overlap 1, language 4, context_aware 4, semantic 8 chunks.*
- [x] Chunks below `min_chunk_size` are merged and not stored as independent objects.
      *Two short paragraphs with `min_chunk_size=100` produced one 169-char chunk.*
- [x] Ingest job status correctly transitions: `queued → running → completed`.
      *`queued` is invisible when the pool is free. Proved by submitting 22
      concurrent jobs against the 16-worker executor: exactly 6 sat in `queued`.*
- [x] On parser failure for one file, other files in the batch continue processing.
      *A corrupt PDF with two good files gave `partial`, 2 completed, 1 failed,
      and the parser error recorded against the offending filename.*
- [x] An upload over 1 MB is accepted through the proxy and ingests; one over 512 MB is refused with 413.
      *nginx's default 1 MB body limit rejected every real-world PDF with a 413
      before the API saw it, and the few-KB fixtures could not catch it (issue
      #21). **Fixed**: `client_max_body_size 512m` with request buffering off,
      uploads written to disk in blocks, and a 3 MB `large.pdf` fixture, which
      is accepted and completes. A 2.7 MB random-text upload that got 413 before
      the fix returned 202 and stored 3,048 chunks before the run was stopped;
      ingest embeds about one chunk per second, so that file takes over an hour.
      A 513 MB sparse file gets 413.*

- [x] Overlap windows recover all nonblank parsed text with exact repeated overlap and the documented tail bound; output exceeding per-file budgets fails before storage.
      *19 controlled runtime groups plus one five-source documentation group pass. Suite08, called by suite02, passes eight real parser/window/text-storage checks with vectorization disabled. Focused production ingest plus the nested check passes19 checks in1m19s. Optional production-model checks are separate; two prior attempts failed embedding timeouts covered by PR61.*

### 10.2 Query

- [x] Invalid query enums, bounds and non-finite values return a serializable 422 before retrieval or model work.
      *Controlled tests assert no backend/model calls; `07_settings.sh` exercises real HTTP errors. The full valid-query suite passes nine checks.*

- [x] A question against an ingested collection returns a non-empty answer.
- [x] All four retrieval modes return results without error.
      *hnsw, flat, hybrid and semantic each returned chunks and an answer.*
- [x] `include_citations: true` returns citation objects with source_file and score.
      *Each citation carries source_file, chunk_index, score and excerpt.*
- [x] `response_format: "end_user"` produces shorter, plainer answers than `"engineer"` for the same question.
      *Four paired trials: end_user shorter in 4/4, mean 652 vs 907 chars. Note
      that "plain language" is instructed but brevity is not — it follows from
      engineer being told to add technical detail and a confidence level, so the
      margin is a tendency rather than a guarantee.*
- [x] Latency fields (`retrieval_latency_ms`, `llm_latency_ms`) are present and non-zero in all responses.

### 10.3 Gold Standard

- [x] Retained stale/orphaned session warnings reach the live API and browser, with reasons/timestamps and legacy defaults. Historical export requires explicit choice, keeps RAGAS compatibility and preserves the original session.
      *Six controlled service/runtime groups plus one twelve-source documentation group cover validity/export and rebuild failure boundaries. Registered real backend/in-process HTTP checks cover actual stale/orphan markers, strict choices, missing sessions, empty history, compatible exports and a failed destructive cutover; browser fixtures cover empty history, failed lookup and duplicate export requests. An actual browser against the built UI and isolated real API shows legacy defaults, warning reasons/timestamps, reset consent after actual deletion, empty historical warnings and explicit four-field RAGAS download. Suite10 is called by05/all.sh; full suite is recorded separately.*

- [x] Generate call returns `sample_size` pairs (or fewer if collection has fewer chunks).
      *Originally failed: sessions routinely lost pairs because the model returns
      a valid JSON object followed by prose, and `json.loads` rejected the whole
      reply ("Extra data: line 6 column 1"). **Fixed**: `_parse_gs_json` now uses
      `raw_decode` from the first brace, tolerating leading and trailing text.
      Reliability went from losing pairs on most sessions to **20/20 across five
      sessions**.*
      *Two further failure modes surfaced during review. A pair could be lost to
      `httpx.ReadTimeout` when Ollama queued requests (it serialises per model,
      so generating while someone queries can push a call past the client
      timeout) — that exception carries an empty message, which is why such
      failures were recorded as `''`. A transport failure now costs one retry
      rather than a pair. The model also occasionally emits genuinely malformed
      JSON that no amount of leading/trailing tolerance can rescue, so the budget
      is one attempt plus two reprompts; the failures are independent between
      attempts.*
- [x] Each pair has non-empty question, answer, ground_truth, and contexts fields.
- [x] Regenerate call replaces exactly one pair without affecting others; returns 409 if session is still generating.
      *Both halves originally failed. Regenerating mid-generation returned **500**,
      not 409 — there was no guard at all, and an unparseable model reply escaped
      as an unhandled exception. **Fixed**: 409 `GENERATION_IN_PROGRESS` while
      generating (the old behaviour raced with the generation loop, which appends
      to the same list), and a parse failure now returns 502
      `PAIR_GENERATION_FAILED` naming the cause and leaving the pair untouched.*
- [x] PATCH call correctly updates pair status; providing question/answer/ground_truth without `status: "edited"` returns 422.
      *Originally returned **200** and silently rewrote the content while the
      status still read "pending" — an export could ship text nobody approved
      under a status saying otherwise. **Fixed**: a model validator requires
      `status="edited"` whenever content fields are present.*
- [x] Sessions survive API container restart (data loaded from `{UPLOAD_DIR}/goldstandard_sessions/`).
- [x] Export includes only approved/edited pairs; excluded count matches rejected + pending.
      *2 approved + 1 edited saved; 1 rejected + 1 pending excluded.*
- [x] Exported file is valid JSON and each pair matches the RAGAS schema.
      *Exactly `question`, `answer`, `contexts`, `ground_truth`; contexts a
      non-empty list of strings.*
- [x] Default filename follows `{collection}_{YYYYMMDD_HHMMSS}.json` pattern.
- [x] Download endpoint returns 404 for a non-existent filename.
      *Traversal attempts (`..%2F`, absolute paths, sub-directories) are rejected
      at the routing layer before reaching the handler.*

**Also fixed here:** `pairs_completed` was incremented in a `finally`, so it
counted *attempts*. A session that lost a pair reported "3/3 complete" while
holding 2. There are now three counters: `pairs_attempted` (drives the UI
progress bar, always reaches the total), `pairs_completed` (pairs that exist) and
`pairs_failed`. Failures whose exception carried an empty string were recorded as
`''`; the type name is now always included.

- [x] Seeded selection reaches the complete UUID population, preserves rank order across backend iteration orders, and retrieves payloads only for selected IDs.
      *19 controlled runtime groups plus one nine-source documentation group cover geometry-independent selection, API validation, missing winners and parked MCP function limits. Suite09, called by04, exercises160 owned synthetic SDK objects across pages; supplied vectors avoid model calls. Its registered live HTTP cases reject invalid generation settings before a missing collection can be queried. This is selection evidence, not deterministic LLM output.*

### 10.4 Web UI

- [x] Role selection persists across page navigation within same browser session.
- [x] End User role shows only the Q&A page in navigation.
      *Nav shows only Q&A, and `/collections` redirects to `/qa`.*
- [x] Citations toggle correctly hides/shows citation panel.
- [x] Chunking strategy selection updates the explanation panel without page reload.
      *All five strategies render distinct text; zero navigations.*
- [x] Health dashboard shows per-service latency; auto-refreshes every 30 seconds.
      *Three services — Weaviate, LLM and Embed, each with its model name and
      latency. Polling measured at 30s intervals.*
- [x] Delete collection requires typed confirmation before calling API.
      *Confirm button starts disabled and stays disabled for a wrong name; no
      DELETE is sent until the collection name is typed exactly.*
- [x] The Import page states the 512 MB upload limit and refuses a larger selection without sending it.
      *A 513 MB sparse file: the page names the limit and no `POST /ingest/upload`
      is made. A 413 or other proxy error page is shown as a readable message
      instead of a JSON parse error (issue #21).*

### 10.5 Infrastructure

- [x] `docker compose up` brings all five services healthy within 5 minutes on first run (including model pull).
      *Model volume deleted and re-pulled from scratch: **233s (3.9 min)**. Both
      models returned with their original IDs.*
- [x] `docker compose up` brings all five services healthy within 120 seconds on subsequent runs (models cached in volume, no re-download).
      *22s.*
- [x] Weaviate data persists across `docker compose down && docker compose up`.
      *10 collections with identical object counts.*
- [x] `collection_registry.json` and `ingest_configs/` persist across restarts; `GET /collections` reflects correct `created_at` after restart.
      *`created_at`, index type and distance metric all preserved.*
- [x] `GET /ingest/config` returns `is_default: true` for a collection with no saved config; `is_default: false` after saving one.
- [x] All inter-service traffic stays on the internal Docker network; only the proxy's container port 80 is published on host loopback.
      *Exactly one host binding: `127.0.0.1:8080->80/tcp` on the proxy. api and
      weaviate publish nothing; ollama and ui expose container ports only.*
- [x] Docker Engine is 28.0.0 or newer; older engines are outside the supported localhost-isolation profile. The infrastructure suite checks the daemon version.
- [x] Resolved Compose configuration and live Docker bindings contain exactly one published TCP port, on the proxy at host address `127.0.0.1`, targeting container port 80. A missing or all-interface host address fails verification. A different free host port preserves loopback and matches `RAG_EXPECTED_PROXY_PORT` (default `8080`); verified at `18080` on a disposable deployment.

---

*End of Specifications — Version 1.4*

---

## 11. Host Requirements and Distribution

### 11.1 Host requirements

| Requirement | Value | Why |
|---|---|---|
| Hardware | Apple Silicon (arm64) | All images resolve arm64 natively; torch is installed from the CPU index, which publishes linux/aarch64 wheels |
| Docker Desktop | installed and running, Engine 28.0.0+ | The only host dependency. No Python, Node or compiler is required |
| **Docker memory** | **12 GB, plus 2 GB swap** | phi3.5 is ~6 GB resident. At 10 GB, full verification runs still hit Ollama timeouts under memory pressure; below that the model is evicted and reloaded between calls and generation times out with `httpx.ReadTimeout`. Swap absorbs short spikes. Leave macOS at least 4 GB: on a 16 GB Mac, 12 GB is the practical ceiling |
| Docker disk | 20 GB minimum, 32 GB recommended | ~6.5 GB images + ~2.5 GB model weights + build cache |
| Free host port | 8080 | `proxy` publishes `127.0.0.1:8080:80` |

Memory and swap are set in **Docker Desktop → Settings → Resources**, not in
`docker-compose.yml`; a compose `mem_limit` caps a container and cannot raise the
VM ceiling. `/health` reports the allocated figure against
`RECOMMENDED_MEMORY_GB` (§3.1.1) so a misconfigured host is visible rather than
presenting as mysterious timeouts.

If Docker's daemon fails to return after changing this setting, quit Docker
fully and confirm no `com.docker.backend` process survives before relaunching: a
stale backend leaves the VM unreachable and the daemon never comes up.

### 11.2 Two distribution paths

| | `package.sh` | `package-offline.sh` |
|---|---|---|
| Archive | ~150 KB zip | ~4.6 GB tar |
| Contains | source only | source + all 5 images + model weights |
| Target needs internet | **yes** (~8–10 GB downloaded) | **no** |
| Docker disk on target | 20 GB | ~12 GB (no build cache needed) |

Both archives MUST exclude `.DS_Store`, `node_modules/`, `__pycache__/`,
`dist/`, `.env*` and previous archives. Neither carries the Weaviate index or
uploaded files; those are created empty on first start.

### 11.3 Offline bundle contents

`package-offline.sh` produces `rag-docker-offline.tar` containing:

```
rag-docker/
├── <the full source tree>
├── install-offline.sh
└── offline/
    ├── images.tar.gz            # docker save of all 5 images (~2.5 GB)
    └── ollama_models.tar.gz     # phi3.5 + nomic-embed-text (~2.2 GB)
```

The image list MUST match the images `docker compose config` resolves. It is
`rag-docker-api:latest`, `rag-docker-ui:latest`,
`semitechnologies/weaviate:1.39.4`, `ollama/ollama:0.3.14`, `nginx:1.27-alpine`.
A stale entry here silently ships a superseded server.

### 11.4 Offline install procedure

```bash
tar xf rag-docker-offline.tar
cd rag-docker
bash install-offline.sh
```

`install-offline.sh` MUST:

1. Refuse to run if `offline/images.tar.gz` or `offline/ollama_models.tar.gz` is
   absent, naming which, so a source-only package fails clearly.
2. `docker load -i offline/images.tar.gz`.
3. Restore the weights via `docker compose run`, which resolves the project's
   `ollama_models` volume without needing the project name, and uses the ollama
   image just loaded — so the restore depends on no image the bundle does not
   carry.
4. `docker compose up -d --no-build` — nothing is pulled or compiled.

### 11.5 Why the offline path reproduces exactly

| Property | Mechanism |
|---|---|
| Same dependency versions | `ui/package-lock.json` and the pinned `api/requirements.txt` |
| Same images, not rebuilt | `docker save` / `docker load` |
| Directory name irrelevant | `api` and `ui` carry explicit `image:` tags, so compose does not derive names from the folder |
| No executable-bit dependency | `ollama` runs `["/bin/bash", "/entrypoint.sh"]`; a zip transfer can drop the bit |
| Survives restarts | `CLUSTER_HOSTNAME` pins Weaviate's Raft identity, which otherwise breaks on the second `up` |
| No model re-download | Weights restored into the volume; the entrypoint skips a pull when `ollama list` already shows the model |

### 11.6 Recreation checklist

A rebuild from these documents is correct when:

- `docker compose up -d` starts five services and all three healthchecked ones report healthy.
- `curl http://localhost:8080/api/health` returns 200 with `resources.memory.status: ok`.
- `GET /api/collections` returns 200 (not 500 — that indicates a client/server version mismatch).
- A create → ingest → query round-trip returns a non-empty answer with at least one citation.
- `docker compose down && docker compose up -d` succeeds twice in a row.
- The offline bundle installs on a machine with no network and reaches the same state.
