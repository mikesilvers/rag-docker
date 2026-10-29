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
    # 1.27.0 is the minimum supported by weaviate-client 4.23.1 (pinned in
    # api/requirements.txt); 1.25.x fails at connect with WeaviateStartUpError.
    image: semitechnologies/weaviate:1.40.2
    environment:
      QUERY_DEFAULTS_LIMIT: 25
      AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED: 'true'
      PERSISTENCE_DATA_PATH: /var/lib/weaviate
      ENABLE_MODULES: 'text2vec-ollama'
      TEXT2VEC_OLLAMA_APIENDPOINT: http://ollama:11434
      TEXT2VEC_OLLAMA_MODEL: nomic-embed-text
      # Auto-schema (on by default) made an insert into a deleted collection
      # recreate it, with inferred properties, no vectorizer and no app
      # registration (issue #40). Every collection is created explicitly with
      # the fixed properties, so nothing needs it; an insert into a missing
      # collection now fails instead.
      AUTOSCHEMA_ENABLED: 'false'
      # Weaviate 1.25 persists Raft cluster state keyed by node identity. Without
      # a fixed CLUSTER_HOSTNAME it derives that identity from the container, so
      # the IP recorded in weaviate_data no longer matches after `compose down`
      # and startup dies with:
      #   "could not open cloud meta store: bootstrap: context deadline exceeded"
      # Pinning the name keeps the identity stable across container recreation.
      CLUSTER_HOSTNAME: 'node1'
      RAFT_BOOTSTRAP_EXPECT: 1
      # Without a Raft snapshot, every start replays each collection delete in
      # Weaviate's Raft log and waits ~150ms on each one (issue #178: 922
      # deletes, ~145s). A snapshot after 128 entries, checked every 30-60s,
      # keeps the replayed tail short.
      RAFT_SNAPSHOT_THRESHOLD: 128
      RAFT_SNAPSHOT_INTERVAL: 30
    volumes:
      - weaviate_data:/var/lib/weaviate
    ports: []
    networks: [rag-internal]
    healthcheck:
      # The weaviate image has no curl; busybox wget is what it ships.
      # The first start on a volume whose Raft log has no snapshot yet (the
      # first after upgrading to the snapshot settings above) still replays
      # its whole history before /ready answers: up to 156s on a copy of real
      # data, 163.5s live (issue #130). Later starts replay only the tail since
      # the last snapshot. Without start_period, compose marks Weaviate
      # unhealthy after ~100s and fails the api, ui and proxy behind it.
      test: ["CMD", "wget", "-q", "--spider", "http://localhost:8080/v1/.well-known/ready"]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 180s

  ollama:
    image: ollama/ollama:0.3.14
    # Invoked via bash rather than as ["/entrypoint.sh"] so the script does not
    # need its executable bit. That bit is not reliably preserved when the
    # project is distributed as a zip (Finder compress, cloud storage, or a
    # Windows machine in the transfer path can all drop it), which would
    # otherwise fail with "permission denied" on a fresh install.
    entrypoint: ["/bin/bash", "/entrypoint.sh"]
    volumes:
      - ollama_models:/root/.ollama
      - ./ollama/entrypoint.sh:/entrypoint.sh:ro
    networks: [rag-internal]
    healthcheck:
      # The ollama image has no curl/wget; the CLI is the only probe available.
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
    # Without it compose derives "<project>-api" from the folder, and an
    # offline install extracted to a differently named folder would not find
    # the loaded image and would try to rebuild (which needs the internet).
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
      # Shown by /health and compared against the memory Docker actually
      # provides. Raise this if you allocate more to Docker Desktop.
      RECOMMENDED_MEMORY_GB: 12
      # The most one upload's ZIPs may expand to in all, gzipped members'
      # output included (2 GiB); over it the upload is refused (#298).
      MAX_ZIP_EXPANDED_BYTES: 2147483648
      # The most entries one ZIP may hold, and the most a member of 1 MiB or
      # more may expand relative to its compressed size; over either the
      # upload is refused (#28).
      MAX_ZIP_MEMBERS: 5000
      MAX_ZIP_MEMBER_RATIO: 200
      # Marks the session cookie Secure (#25). false for this loopback HTTP
      # profile, where a Secure cookie would never be sent back. LAN mode's
      # docker-compose.lan.yml serves TLS and sets it true (#26). Exactly true
      # or false.
      AUTH_COOKIE_SECURE: "false"
    volumes:
      - ingest_uploads:/app/uploads
      # Accounts, sessions and the login throttle (#25), one SQLite file.
      - auth_data:/app/auth_data
      # Retained source documents; grows with the corpus.
      - rag_sources:/app/sources
      # Bind mount, deliberately: an export is only useful if the user can
      # reach the file from the host without going through Docker.
      - ./exports:/app/exports
      # Ollama's model store, shared read-write so a package can carry models
      # into an air-gapped machine. Installing a model means writing its exact
      # manifest and blobs; there is no registry to pull from offline, and
      # rebuilding a model through the HTTP API would not reproduce its
      # template, params and license faithfully.
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
    image: nginx:1.29-alpine
    ports:
      # The workbench is local to the host by default. LAN mode (#26) is the
      # opt-in docker-compose.lan.yml, which replaces this with TLS on port 443.
      - "127.0.0.1:8080:80"
    volumes:
      - ./proxy/nginx.conf:/etc/nginx/nginx.conf:ro
      # The routes, shared with LAN mode's nginx.lan.conf (#26).
      - ./proxy/locations.conf:/etc/nginx/rag/locations.conf:ro
    networks: [rag-internal]
    depends_on: [ui, api]

# ── MCP server: PARKED ────────────────────────────────────────────────────────
# The MCP server is built and tested but intentionally NOT wired into the stack.
# Its source lives in ./mcp and its design in MCP_ANALYSIS.md,
# MCP_SPECIFICATIONS.md and MCP_IMPLEMENTATION.md. Nothing here depends on it,
# and the offline bundle does not ship its image.
#
# To bring it back: restore the service block recorded in MCP_IMPLEMENTATION.md
# §7, add rag-docker-mcp:latest to the IMAGES array in package-offline.sh, and
# rebuild. No code changes are needed -- it passed all 16 acceptance criteria.

volumes:
  weaviate_data:
  ollama_models:
  ingest_uploads:
  rag_sources:
  auth_data:

networks:
  rag-internal:
    driver: bridge
```

### 2.2 Nginx Routing

**Files:** `proxy/nginx.conf` (the default server, HTTP on container port 80) and `proxy/nginx.lan.conf` (LAN mode's
TLS server on container port 443, §2.5) both include `proxy/locations.conf`, which holds the routes, so the two
servers route identically.

| Path prefix | Routes to | Notes |
|---|---|---|
| `/api/goldstandard/regenerate` (exact) | `http://api:8000/goldstandard/regenerate` | The same headers as `/api/`. Read timeout 1860 s, longer than the API's 1800 s regeneration deadline (§3.1.5), so the API's own answer, its 504 `PAIR_GENERATION_TIMEOUT` included, reaches the client |
| `/api/` | `http://api:8000/` | Strip `/api` prefix. Forwards the browser's `Host` with its port (`$http_host`), which the API compares with `Origin` (§3.1.8), and the client address as `X-Real-IP` |
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

1. Weaviate starts with a 180-second healthcheck start period for cold Raft replay → healthcheck passes (ready endpoint returns 200)
2. Ollama starts → pulls models (phi3.5, nomic-embed-text) → healthcheck passes
3. API starts → verifies both downstream connections → healthcheck passes
4. UI starts (static build, no runtime deps)
5. Nginx proxy starts

### 2.5 LAN Mode (#26)

LAN mode shares the workbench on the local network over TLS. It is opt-in: only
`docker compose -f docker-compose.yml -f docker-compose.lan.yml ...` turns it on, and without the override the stack
is exactly as in §2.1.

**File:** `docker-compose.lan.yml`. Over the base file it:

- replaces the proxy's publication with exactly one, `0.0.0.0:${RAG_LAN_PORT:-443}->443/tcp`: TLS on every host
  interface, nothing on port 80 and no redirect;
- sets `RAG_LAN_MODE: "true"` and `AUTH_COOKIE_SECURE: "true"` on the api;
- mounts `proxy/nginx.lan.conf` over `nginx.conf` and the `lan_certs` volume at `/etc/nginx/certs`, both read-only,
  and starts the proxy only after `tls-init` has completed successfully;
- adds `tls-init`, a one-shot service in the API image (`rag-docker-api:latest`, `python -m tls_cert`), with
  `RAG_LAN_HOSTNAMES` from the shell or `.env`, `proxy/certs` mounted read-only at `/byo` and `lan_certs` at
  `/certs`.

**Startup refusal** (`api/lan_mode.py`, called first by `api/main.py`). With `RAG_LAN_MODE=true` the API checks,
before any other import, startup work or request, that `AUTH_COOKIE_SECURE` is `true` and then that an enabled human
(not service principal) AI Engineer exists. On the first failure it writes one line, starting `LAN mode refused:`, to
standard error and exits 1. The ui waits for a healthy api and the proxy for the ui, so nothing is published. The
check runs once; LAN mode changes nothing else in the API. The operator command is unaffected, and the first
Engineer is created in the default mode before switching.

**TLS server** (`proxy/nginx.lan.conf`): one server, `listen 443 ssl`, `http2 on`, TLS 1.2 and 1.3 only, Mozilla's
intermediate cipher list for TLS 1.2, `ssl_prefer_server_ciphers off`, session tickets off, no
`Strict-Transport-Security` header, the pair at `/etc/nginx/certs/live/{cert,key}.pem`, and the shared routes.

**Certificate step** (`api/tls_cert.py`, run by `tls-init` on every start in LAN mode):

| Case | Result |
|---|---|
| `proxy/certs/cert.pem` and `key.pem` both present | Used as given (a chain allowed, server certificate first). Refused, exit 1, when either doesn't parse or the key doesn't match; an expired certificate is used with a warning |
| Only one of the two present | Refused, exit 1 |
| Neither present | A self-signed certificate for `RAG_LAN_HOSTNAMES` (comma-separated DNS names and IP addresses) plus `localhost`: ECDSA P-256, CN the first name, valid 825 days, `CA:FALSE`, server-authentication EKU. An empty value or an entry that is neither a DNS name nor an IP address is refused, exit 1 |
| A self-signed pair already active | Reused, unless it is unreadable, its key doesn't match, it has expired, its names differ from the ones wanted now, or the active pair came from bring-your-own files that are gone |

The active pair lives in `lan_certs` as `live`, a symlink to a `pair-<random>` folder (`cert.pem`, `key.pem` mode
0600, `source`), switched with one rename, so a failed step never leaves a certificate beside a key that doesn't match
it. The step prints one line with the source, names, expiry and SHA-256 fingerprint, or one line on standard error
when it refuses; then the proxy doesn't start. `proxy/certs/` is tracked through `.gitkeep`; everything else in it is
ignored by git.

**Client address.** nginx passes `$remote_addr` to the API as `X-Real-IP`, which keys the login throttle. Measured
on Docker Desktop for Mac: a request through the host's LAN address arrives from `192.168.65.1` and one through
`localhost` from the Docker network's gateway, not from the client's own address.

---

## 3. API Service Specification

**Runtime:** Python 3.11, FastAPI, Uvicorn  
**Base URL (internal):** `http://api:8000`  
**Base URL (external via proxy):** `http://<host>/api`

### 3.1 Endpoints

**Collection names (#372).** Weaviate stores a collection under its name with the first
character upper-cased and the rest unchanged (`myDocs` is stored as `MyDocs`). Every
endpoint that takes a collection name from the caller, in the body, the path, the query
string or a form field, replaces it with that stored name before using it. So whichever
spelling of the first character a request uses, the registry entry, retained originals,
ingest and retrieval settings, gold-standard sessions, query metrics and job guards are
keyed by the stored name, and responses and messages that name the collection give it.
Nothing else is case-folded: `mydocs` is a different name from `MyDocs`. Import takes its
name from the package, which must already be in this form (`RAG_EXPORT_SPECIFICATIONS.md`).
State written under a lowercase-first spelling before #372 (a registry entry, a `sources/` folder, a
settings file, a session's `collection`, a metric record) is not migrated, and it isn't read under the
stored name either: such a collection lists `created_at` as `null`, exports and tunes without those
originals, settings and sessions, and its old metric records match only that old spelling. Recreating
the state (re-ingesting, saving the settings again) puts it under the stored name.

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
      "hnsw_config": {
        "ef": 64,
        "efConstruction": 128,
        "maxConnections": 64
      },
      "created_at": "2026-09-10T14:23:00Z"
    }
  ]
}
```

`index_type`: `"hnsw"`, `"flat"`, `"dynamic"`, or `"unknown"` when the collection has no recognized vector index configuration (for example, named vectors).  
`distance_metric`: `"cosine"`, `"dot"`, `"l2-squared"`, or `"unknown"` when the distance is unavailable or isn't one of these three.  
`hnsw_config`: the actual HNSW settings `{ef, efConstruction, maxConnections}` when `index_type` is `"hnsw"`; `null` otherwise.

`created_at` is `null` for any collection that exists in Weaviate but has no entry in `collection_registry.json` (e.g. created outside this system).

The registry is written atomically (#388): each create or delete writes the whole registry to a
uniquely named temporary file in the same folder, fsyncs it and replaces
`collection_registry.json` with it, under one lock in the API process, so a crash or a failed
write (for example on a full `ingest_uploads` volume) leaves the old file whole. The folder is
then fsynced, so a power loss right after a save doesn't lose the replacement; if that fsync
fails, the save fails with the new file already in place (#445). At startup, before the first
request, the API removes the temporary files (`.collection_registry.json.*.tmp`) of saves a
crash interrupted; one it can't remove is logged and kept (#445). The file is
owner-only (mode 0600). A registry that exists but can't be read (it can't be opened, isn't
UTF-8 JSON, or isn't a JSON object) is never treated as empty: it is left unchanged and logged.
`GET /collections` then answers with `created_at` `null` for every collection, and
`POST /collections` and `DELETE /collections/{name}` still create or delete the collection and
answer as usual, without recording or removing its entry, until the file is fixed or removed.
A missing file is an empty registry.

**Response 503** `SERVICE_UNAVAILABLE` when Weaviate can't be read to list the collections
(unreachable, timed out or any other error) (#373): "Cannot list the collections: Weaviate
could not be read. Try again when Weaviate is available." `detail` is `null`. The cause is
logged, not returned.

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

`name` is the name as stored (§3.1, #372): a request for `myDocs` answers `MyDocs`, and
`collection_registry.json` records its `created_at` under `MyDocs`.

**Response 409** if collection already exists.

**Response 503** `SERVICE_UNAVAILABLE` when Weaviate can't be read to check whether the
collection exists (#366). No collection is created; `detail` is `{"collection": "<name>"}`.
The cause is logged, not returned.

---

```
DELETE /collections/{name}
```

Deletes a collection and all its objects. Requires `confirm=true` query parameter as a safeguard.
The value is compared as text, case-insensitively: `true`, `TRUE` or `True` confirm; a missing
value or any other value (`false`, `1`, `yes`, empty) answers `400 CONFIRMATION_REQUIRED`,
never `422`. Confirmation is checked first, before the collection is looked up, so a missing
collection without it answers `400`, not `404` (#302). The UI and the MCP tool
`rag_delete_collection` always send it.

**Response 200:**
```json
{ "name": "Documents", "status": "deleted", "objects_removed": 1842 }
```

Before deletion, resolves the backend's canonical collection name. After successful backend deletion, removes retained source documents and ingest/retrieval configurations for both the canonical name and its lowercase-first-character alias, regardless of the spelling used in the request. Only that first-character alias matches; distinct collection names are not case-folded or swept. Matching sessions for both spellings are preserved and durably marked orphaned. Also removes both spellings from `collection_registry.json`. Import-replace reports the number of retained sessions across both spellings. Deleting an original collection preserves distinct retained recovery copies; explicitly deleting a retained recovery collection retires only its matching ownership journal and snapshots.

**Response 404** if the collection does not exist:
```json
{ "error": { "code": "COLLECTION_NOT_FOUND", "message": "Collection 'Documents' does not exist.", "detail": null } }
```

**Response 503** `SERVICE_UNAVAILABLE` when Weaviate can't be read to check that the
collection exists (#366). Nothing is deleted; `detail` is `{"collection": "<name>"}`. The
cause is logged, not returned. Confirmation is still checked first.

**Response 400** if `confirm=true` is not supplied:
```json
{ "error": { "code": "CONFIRMATION_REQUIRED", "message": "Add ?confirm=true to confirm deletion.", "detail": null } }
```

**Response 409** while a job is active for the collection (issue #40): an ingest,
export, import, tune or gold-standard generation job, under either spelling of the
name. Nothing is deleted. The message names the first job's kind and id; `detail`
names it and lists every active job, oldest first. For gold-standard generation the
id is the session id.
```json
{ "error": { "code": "COLLECTION_BUSY",
  "message": "An ingest job is running for this collection ('Documents', job 1a2b3c4d). Delete the collection after the job finishes.",
  "detail": { "collection": "Documents", "job_kind": "ingest", "job_id": "1a2b3c4d",
              "jobs": [ { "job_kind": "ingest", "job_id": "1a2b3c4d" } ] } } }
```
The message starts "An ingest job", "An export job", "An import job", "A tuning job"
or "A gold-standard generation job" "is running for this collection". A delete never
waits behind a job: a job counts as active from its admission (ingest: before its
upload is saved; import: once its target name is known; generation: once its session
is published) until just before its status turns terminal, so a client that has seen
the job end can delete at once. While the delete waits for the collection's writer
guard (held briefly by collection creation, say), it rechecks every 0.25 s and
answers `409` as soon as a job appears; it checks once more when it holds the guard.
Once the delete holds the guard and that last check finds no job, the delete is *in
progress* until it returns, under either spelling (#302). A delete still waiting for the
guard is not, so a job that registers then still refuses the delete as above. While a delete
is in progress, a new job for the collection is refused and not admitted: ingest upload,
export, tune and gold-standard generation answer
```json
{ "error": { "code": "COLLECTION_DELETING",
  "message": "Collection 'Documents' is being deleted. Start the job again once the delete finishes; the collection must be created again first.",
  "detail": { "collection": "Documents" } } }
```
with the collection as the request named it, and leave no job record, registration, upload
or session behind. Generation checks before its one-second guard wait (§3.1.5); a delete that
starts during that wait and outlasts it still gives `409 COLLECTION_BUSY`. An import learns its
target only after opening the package, so an import whose target is being deleted ends
`failed` with `error_code` `COLLECTION_DELETING`, that message and `error_detail`
`{"collection": "<target>"}`. With `on_conflict` `abort` this is checked before the name
collision, so a target the delete has not removed yet ends this way too, not with
`COLLECTION_EXISTS` (#324). With `on_conflict` `rename`, a target the delete has not removed
yet is a name collision as usual: the import goes ahead under its new name and doesn't touch
the collection being deleted (#336). After the delete, admission behaves as before (usually
`404 COLLECTION_NOT_FOUND`). Deletions an import-replace or a tune makes of its own target are
not subject to these checks.

---

#### 3.1.3 Ingest

```
POST /ingest/upload
Content-Type: multipart/form-data
```

Accepts one or more files. For ZIP uploads, extracts and processes all supported files within the archive. For folder-equivalent uploads (multiple files in one request), processes all submitted files.

**Size limit:** one request may be at most **512 MB**, all files together. The proxy enforces this (`client_max_body_size` in `proxy/locations.conf`) and answers an oversize request itself with **HTTP 413**, an nginx HTML page rather than the API's JSON error shape, before the API sees it. The proxy streams accepted uploads to the API without buffering them, and the API writes each file to disk in blocks rather than holding it in memory. The Import page refuses a selection over the limit before sending it.

**Form fields:**

| Field | Type | Required | Description |
|---|---|---|---|
| `files` | file[] | Yes | One or more files (PDF, DOCX, TXT, MD, CSV, JSON) or a single ZIP |
| `collection` | string | Yes | Target Weaviate collection name |
| `strategy` | string | No | Default: `overlap`. One of: `fixed`, `overlap`, `semantic`, `context_aware`, `language` |
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

Invalid multipart settings, including values that aren't numbers, return **422
`INVALID_SETTINGS` before collection lookup, upload staging or job creation**. Invalid JSON settings return 422 `INVALID_PARAMETER` with
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

**Errors (#290).** All use the standard error shape. The whole request is refused:
no job is created, nothing from it is ingested and its temporary files are removed.

Every uploaded ZIP is validated before anything is queued: each member's data is read to
its end, including members that will be skipped. A ZIP that can't be opened, or has any
unreadable member, is refused as a whole; nothing is repaired or recovered.

| Status | `code` | When |
|---|---|---|
| 400 | `NO_SUPPORTED_FILES` | nothing in the upload is a supported type; the message lists what was skipped |
| 400 | `ARCHIVE_UNREADABLE` | the ZIP is damaged: it can't be opened (truncated, e.g. an incomplete download, or a bad central directory), or a member can't be read (wrong offset, bad local header, bad CRC, truncated data) |
| 400 | `ARCHIVE_UNSUPPORTED` | no member is damaged, but one is password-protected or uses a compression method the server can't read (e.g. Deflate64) |
| 400 | `ARCHIVE_TOO_LARGE` | the upload's ZIPs expand to more than `max_zip_expanded_bytes` in all (2 GiB by default; below) |
| 400 | `ARCHIVE_TOO_MANY_MEMBERS` | one ZIP holds more than `max_zip_members` entries (5,000 by default; below, #28) |
| 400 | `ARCHIVE_RATIO_TOO_HIGH` | a ZIP member of 1 MiB or more expands to more than `max_zip_member_ratio` times its compressed size (200 by default; below, #28) |
| 400 | `UPLOAD_WRITE_FAILED` | a filesystem error while saving the upload or extracting a member (disk full, name too long, ...); the message gives the reason |
| 409 | `COLLECTION_DELETING` | a delete of the collection is in progress (§3.1.2; #302); nothing is saved or queued |
| 503 | `SERVICE_UNAVAILABLE` | Weaviate can't be read to check that the collection exists (#366); nothing is saved or queued, `detail` is `{"collection": "<name>"}` and the cause is logged, not returned |
| 500 | `INGEST_FAILED` | anything unexpected; the message says so and the API log holds the traceback, collection and filenames |

The ZIP messages name the upload, say nothing from it was imported, name the unreadable
members with a reason each (up to 10, then "and N more") and suggest re-creating the ZIP.
`detail` is `{"file": ..., "members": [{"member": ..., "reason": ...}, ...], "members_omitted": N}`:
`members` holds at most the first 10 members, the ones the message names, and
`members_omitted` counts the rest (0 when there are none; #28). Every refusal in this table
that carries a `detail` with `file` (the ZIP refusals and `UPLOAD_WRITE_FAILED`) has this shape.

**Members whose path leaves the archive (#28).** A member whose normalised path is absolute or
starts with `..` (for example `../bad.txt` or `a/../../up.txt`) is never extracted. It is still
validated like every other member, so a damaged one refuses the ZIP as `ARCHIVE_UNREADABLE`
(an encrypted or unsupported-method one as `ARCHIVE_UNSUPPORTED`), and its own bytes count toward
the expansion cap and the ratio cap below; it is not gunzipped to count gzip output, since the
job never reads it. An accepted upload lists it in the job's `skipped` as
`<zip filename>:<member path> (its path leaves the archive, so it was not extracted)`.

**Expansion cap (#298).** All the ZIPs of one upload request may expand to at most
`max_zip_expanded_bytes` (env `MAX_ZIP_EXPANDED_BYTES`, default 2,147,483,648 bytes = 2 GiB) in
all; files uploaded directly, a `.jsonl.gz` included, don't count. Before a ZIP's members are
read, the total so far plus the uncompressed sizes the ZIP declares for the members validation
reads is checked. While validation reads each member, the bytes it decompresses are counted, once
per member (extraction reads supported members again but doesn't count them); a `.json.gz` or
`.jsonl.gz` member is also gunzipped, only to count its output, on top of its own bytes. It is
read with the job's own gzip reader (Python's `gzip` module), so the count is what the job would
decompress: the same headers accepted, concatenated streams and zero padding followed. Gzip data
that reader refuses stops that member's count, and the job reports the file as usual. As soon as the total is over the cap the
whole request is refused, whatever else was found: 400 `ARCHIVE_TOO_LARGE`, `'<zip>' expands to
more than 2,048 MB when unpacked, the most one upload's ZIPs can expand to, so nothing from it was
imported. Split it into smaller zips and upload them in separate imports.` (`<zip>` is the ZIP
being checked or read; the limit is in MB when it is a whole number of MiB, otherwise in bytes),
`detail` `{"file": "<zip>", "members": [], "members_omitted": 0}`. A total equal to the cap is accepted. The ZIP work
(opening, the size check, validation, counting and extraction) runs in a worker thread, so the
API keeps answering other requests meanwhile.

**Member-count and ratio caps (#28).** Each ZIP may hold at most `max_zip_members` entries (env
`MAX_ZIP_MEMBERS`, default 5,000), counted per ZIP over every entry in its central directory:
files, folders, unsupported files, macOS metadata and members whose path leaves the archive. The
count is checked as soon as the ZIP is opened, before anything else; a count equal to the cap is
accepted. Over it: 400 `ARCHIVE_TOO_MANY_MEMBERS`, `'<zip>' holds 5,001 entries, more than 5,000,
the most one zip may hold, so nothing from it was imported. Split it into smaller zips and upload
them in separate imports.`, `detail` `{"file": "<zip>", "members": [], "members_omitted": 0}`.

A member of 1 MiB (1,048,576 bytes) or more, uncompressed, may expand to at most
`max_zip_member_ratio` (env `MAX_ZIP_MEMBER_RATIO`, default 200) times its compressed size;
smaller members are exempt, and exactly the ratio is accepted. It is checked on the sizes the
ZIP declares, after the declared total and before any member is read, and again, as a backstop, on
the bytes the ZIP layer decompresses for each member, after every block read. With CPython's
zipfile that second check doesn't trigger: zipfile stops reading a member at its declared
uncompressed size, so a member whose header understates its size fails its CRC check and is
refused as unreadable (`ARCHIVE_UNREADABLE`, `damaged: Bad CRC-32 ...`), not as
`ARCHIVE_RATIO_TOO_HIGH`. Only the ZIP layer counts:
a `.json.gz` or `.jsonl.gz` member's gzip output counts toward the expansion cap, not its ratio.
Over it, whatever else was found: 400 `ARCHIVE_RATIO_TOO_HIGH`, `'<zip>' can't be imported: its
member '<member>' expands to more than 200 times its compressed size, which is refused as a
possible zip bomb, so nothing from it was imported. Remove that file from the zip, or upload it on
its own, and upload again.`, `detail` `{"file": "<zip>", "members": [{"member": "<member>",
"reason": "expands to more than 200 times its compressed size"}], "members_omitted": 0}`.

For one ZIP the checks run in this order, the first refusal ending the request: the member count;
the declared total (`ARCHIVE_TOO_LARGE`); the declared ratio; then, while each member is read,
the total and the member's ratio; then unreadable members. So a ZIP over both the expansion cap
and the ratio by its declared sizes is `ARCHIVE_TOO_LARGE`.

**`.json.gz` members.** A ZIP member ending in `.json.gz` is extracted and queued like a
supported file. The job decompresses it (at most 30,000,000 bytes decompressed, the JSON input limit of §5.1; #300) and parses the result
as JSON; its chunks have `source_type` `json` and `source_file` the member's logical name (below), and
the retained original is the `.json.gz` itself. A `.json.gz` uploaded directly, and other
`.gz` members, are skipped as unsupported types. The JSON is converted to text as described in §5.1.

**JSON Lines (#291).** `.jsonl` and `.jsonl.gz` files (case-insensitive) are queued whether
uploaded directly or as ZIP members. A `.jsonl.gz` is decompressed while the job reads it (at
most 512 MB decompressed, nothing decompressed written to disk). Each line becomes one record
as described in §5.1; chunks have `source_type` `jsonl`, and the retained original is the file
as uploaded.

**macOS metadata in ZIPs (#378).** A ZIP made with the macOS Finder holds, beside each file, an
AppleDouble file with its resource fork and extended attributes, `__MACOSX/<folder>/._<name>`,
which keeps the file's suffix; other macOS tools write `._<name>` beside the file. These are not
documents. A ZIP member whose logical name (below) has `__MACOSX` as its first folder, or whose
file name starts with `._`, is not queued, whatever its suffix, and is listed in the job's
`skipped` as `<zip filename>:<member path> (macOS metadata (AppleDouble), not a document)`, not
left out silently. The match is exact and case-sensitive, so names that only contain these
strings (`_report.txt`, `notes._v2.txt`, `.hidden.txt`, `__MACOSX_notes.txt`,
`docs/__MACOSX/readme.txt`) are queued as usual. Such members are still validated like any
member that will be skipped. A ZIP holding nothing else is refused with `NO_SUPPORTED_FILES`.

**Logical names and re-ingesting (#22).** Every file is stored under a logical name: the
`source_file` of its chunks and the filename in its retained-source index entry.

- A direct upload's logical name is its filename without any path. A ZIP member's is its
  path inside the archive, POSIX-style and normalised: `a/report.pdf` and `b/report.pdf`
  are two documents, `a/../b.txt` is `b.txt` and `./c.txt` is `c.txt`. Members at the
  archive's top level keep their bare name. A member whose path leaves the archive
  (`../x.txt`, an absolute path) is not ingested; it is validated and listed in `skipped`
  (§3.1.3, #28). Error and skip messages still name a
  member as `<zip filename>:<member path as stored in the archive>`.
- Each queued file is staged in a directory of its own, so no input overwrites another's
  bytes.
- **Within one upload the last input with a logical name wins.** That covers a name sent
  twice in one request, a name repeated inside a ZIP, the same path in two ZIPs, and a direct
  upload with the same name as a top-level member. Each earlier input is dropped, not
  counted in `files_total`, and listed in the job's `skipped` as
  `<earlier> (replaced by <later> later in this upload, same name '<logical name>')`.
- **Re-ingesting a name replaces it**, always, even when the content is unchanged: the file
  is parsed and chunked again with the request's settings, the new chunks are stored, and
  only then are the name's earlier chunks deleted, so a failed store leaves the earlier
  version whole. The earlier chunks are those with the same `source_file` and a different
  `created_at` than the new chunks (which all share one); deletion repeats until none match
  (Weaviate deletes at most `QUERY_MAXIMUM_RESULTS` per call), at most 100 times. It
  proceeds only if exactly the new chunks match their own `created_at`, and afterwards
  exactly they must remain. The job's `chunks_replaced` counts the deleted chunks.
- If the earlier chunks can't all be deleted, the new chunks stay, the file counts as
  failed (so the job is `partial`, or `failed` if every file failed) and `errors` gets
  `<file>: its new chunks were stored, but earlier chunks could not all be removed
  (<reason>); upload it again to retry`.
- Before the first file in a job replaces earlier chunks, the collection's gold-standard
  sessions are marked `stale` (§3.1.5), once per job, with the reason `documents were
  re-ingested, replacing their earlier chunks: <names>`, naming that file and the job's
  later files that also have earlier chunks (10 names, then `and N more`). The marking is
  saved before anything is stored; if it can't be, that file fails with `<file>: could not
  mark gold-standard sessions stale before replacing its chunks, so nothing was changed:
  <reason>`, and the next replacing file tries again. If the lookup of which later files
  have earlier chunks fails, nothing is marked and that file fails the same way with
  `<file>: could not check which later files in this upload replace earlier chunks, so
  nothing was changed: <reason>` (#311). Files with new names mark nothing.
- The retained original follows the replacement: the name is removed from any other
  retained digest, and a digest left with no names is deleted with its blob (see
  `RAG_EXPORT_SPECIFICATIONS.md` §2.2).
- Ingest jobs for one collection run one at a time, so for one name the job that runs last
  wins and each job applies whole; the order of concurrent uploads is not promised.
- Chunks already duplicated before this rule (a name ingested twice) stay until the name is
  ingested again.

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
  "chunks_submitted": 142,
  "chunks_accepted": 142,
  "chunks_replaced": 0,
  "errors": [],
  "skipped": []
}
```

`chunks_stored` counts chunks whose insert was checked: read back and matched (§5.4).
`chunks_submitted` counts chunks the job sent to Weaviate, once per file even when the "index
not ready" retry sends them again; `chunks_accepted` counts those Weaviate acknowledged without
a per-chunk error, including ones removed again because the check failed. Both come from a
file's final attempt; a file that fails before its insert (parsing, chunking) adds to none, and
one whose batch could not be sent adds what was handed to the batch to `chunks_submitted` and
nothing to `chunks_accepted`. A clean job has all three equal, while it runs too: a file's
three counts change in one update (#324). Job records written before
these two fields existed read back with both `null`, not `0`, which would be untrue (#302).
`chunks_replaced` is how many earlier chunks of the same names the job deleted (#22); job
records written before it existed read back as `0`. `skipped` lists inputs that were not
ingested, each with its reason: an unsupported type, macOS metadata in a ZIP (#378), or a
later input with the same name.

`status` values: `"queued"`, `"running"`, `"completed"`, `"partial"` (some files failed),
`"failed"`.

**Job records survive an API restart (#23).** Ingest, export, import and tune job records are
kept in `{UPLOAD_DIR}/jobs/{kind}/{job_id}.json` (owner-only, atomic replacement), so
`GET /…/job/{job_id}` keeps answering after a restart instead of `404`. Status changes and the
final record are written at once; progress counters at most once a second per job. At startup,
before the first request, a job still `queued` or `running` is marked interrupted, because its
worker died with the old process. An interrupted ingest becomes `partial` when
`files_completed > 0`, otherwise `failed`, with "Interrupted: the API restarted while this job
was running. Re-upload the files to try again." appended to `errors`; its counters stay as
they were. An unreadable record file is left as it is and logged. The newest 200 records per
kind are kept: at startup by file time, and while the API runs, starting a job past 200 removes
the finished records saved longest ago (a `queued` or `running` one never is). A removed job
answers `404 JOB_NOT_FOUND`, as it would after a restart. A job's first record is written off
the event loop; a start whose request is cancelled during that write leaves no record and
releases its guards (#309). An interrupted ingest's upload staging directory
(`{UPLOAD_DIR}/ingest-*`, the upload and its extracted ZIP members) is removed at the next
startup, before the first request; links are never followed (#377).

On failure, `errors` contains per-file error messages, each `"<file>: <reason>"`, where
`<file>` is the uploaded filename, or `<zip filename>:<member path>` for a ZIP member. A
`.json.gz` member can fail with: not a valid gzip file (truncated or corrupt);
`decompresses to more than 30,000,000 bytes, the most one JSON file can hold; split the file or
convert it to JSON Lines` (decompression stops there; #300); or JSON that can't be parsed after
decompression. A `.json` file over 30,000,000 bytes fails, before it is read, with `is larger than
30,000,000 bytes, the most one JSON file can hold; split the file or convert it to JSON Lines`. A `.json` file
that isn't valid JSON fails with `is not valid JSON: <reason>`, giving the parser's line and column; one whose
converted text would exceed 10,000,000 characters fails with `converts to more than 10,000,000 characters of
text, the per-file limit for every chunking strategy; split the file` (a `.json.gz` member: `was decompressed,
but it converts to more than …`); one with more than 1,000,000 records fails with `has more than 1,000,000
records, the most one file can hold; split the file` (a `.json.gz` member: `was decompressed, but it has more
than …`).

A `.jsonl` or `.jsonl.gz` file fails as a whole, with nothing stored, at its first bad line
(§5.1): `line N is blank` (a blank line followed by another record), `line N is not valid JSON:
<reason> at column C`, `line N is not UTF-8 text (<reason> at byte B)`, `line N is longer than
10,000,000 bytes, the most one line can hold; split the record across lines or files`,
`converts to more than 10,000,000 characters of text by line N, the per-file limit for every
chunking strategy; split the file`, or, past 1,000,000 lines, `has more than 1,000,000 records,
the most one file can hold; split the file` (#314). For a `.jsonl.gz` these read `was
decompressed, but line N …` (and `was decompressed, but it converts to more than …`, `was
decompressed, but it has more than …`); a corrupt or truncated gzip gives the
`.json.gz` message above, and passing the 512 MB limit gives `decompresses to more than 512 MB,
the limit for one compressed file; nothing from it was stored`.

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

Ingest and retrieval settings saves serialize publication across the API process's
worker threads. Each save writes its own exclusively created temporary file in
the destination directory and atomically replaces the saved configuration. A 201
acknowledges that request's complete value was published; a later successful save
may supersede it. Serialization, temporary write/close or replacement failure
does not acknowledge success and leaves the last valid configuration readable.
Failed saves clean up their own temporary file when filesystem permissions
allow. After the replacement the folder is fsynced, so a power loss right after a
save doesn't lose it (#445); a failed folder fsync doesn't acknowledge the save
either, though the new value is then already in place. Package import publishes ingest settings through this same lock and
atomic writer. Published files use mode 0600 (API owner read/write), including
replacements of older files.

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

A citation's `score` is higher-is-better in every retrieval mode (#376). Citations are listed in
retrieval order, best match first, so their scores don't increase down the list.

| Mode | `score` |
|---|---|
| `hnsw`, `flat`, `semantic` | A similarity converted from Weaviate's distance by the collection's distance metric: `cosine`: `1 - distance` (the cosine similarity, -1 to 1); `dot`: `-distance` (the dot product); `l2-squared`: `1 / (1 + distance)` (0 to 1). |
| `hybrid` | Weaviate's fusion score, unchanged. |

The metric is read from Weaviate on every query, so a collection deleted and recreated with
another metric is converted by its new one. `score` is `null` when Weaviate returns no distance
(or no hybrid score), or when the collection's config has no metric or a metric that isn't one
of the three above; it is never replaced by 0.0. A failed read of the metric is a Weaviate
failure: the query answers the 503 below (#373). Non-null scores are rounded to 4 decimals.
Scores rank the chunks of one answer; they aren't comparable across modes or metrics.

**Response 404** if the collection does not exist (uses `COLLECTION_NOT_FOUND` error code).

**Response 503** `SERVICE_UNAVAILABLE` when Weaviate can't be read to check that the
collection exists (#366). No query is run; `detail` is `{"collection": "<name>"}`. The cause
is logged, not returned.

**Response 503** `SERVICE_UNAVAILABLE` when, after that check, Ollama or Weaviate fails during
the pipeline (#373): an Ollama call (the reformulation, the query embedding or the synthesis)
that can't connect, times out or answers an error status, or a Weaviate retrieval call that
raises. The message names the service: "The question was not answered: Ollama could not be
reached, timed out or returned an error. Try again when Ollama is available." (or Weaviate).
In `hybrid` and `semantic` modes Weaviate embeds the question itself through Ollama, so an
Ollama failure there comes back as a Weaviate error; a Weaviate failure in those modes names
both (#411): "The question was not answered: Weaviate could not be reached, timed out or
returned an error, or Ollama could not embed the question for it. Try again when Weaviate and
Ollama are available."
`detail` is `{"collection": "<name>"}`, no metrics record is written, and the cause is logged,
not returned. Any other failure in the pipeline answers `500 INTERNAL_ERROR` (§3.2).

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

A saved file that can't be read, isn't JSON, holds JSON that isn't an object, or has a
field of the wrong type (for example `"top_k": "x"`) is logged as a warning, and the
response is the defaults with `is_default: true`, never an error (#439). Fields of the
right type are returned as saved, missing fields are filled from the defaults, and an
export still refuses such a file (RAG_EXPORT_SPECIFICATIONS.md §5). An `alpha` of
`NaN` or `Infinity`, which JSON can't carry, counts as the wrong type, while a finite
`alpha` outside 0.0-1.0 is returned as saved. A saved `is_default` key, the response's
own field rather than a setting, also gives the defaults; export ignores it, as it
does any unknown key (#477).

Saved `ef` is a legacy inactive field, not a query override. `GET /collections` reports actual HNSW settings as `hnsw_config` (ef, efConstruction, maxConnections), or null for a Flat, Dynamic or unrecognized index configuration. An unavailable distance metric is reported as `unknown`. Both hnsw/flat query-mode names use the existing physical index.

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
atomically with the same unique-temporary-file and serialized-publication contract
as ingest settings above. Deleting a collection deletes its retrieval config.

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

Generation waits at most one second to acquire the collection mutation guard
before sampling and publishing its session. A busy collection returns
`409 COLLECTION_BUSY` without creating a session or model task. If a completed
deletion removed the collection before sampling, it returns
`404 COLLECTION_NOT_FOUND`. Retry a busy request after the writer finishes. A delete in
progress is checked before that wait and answers `409 COLLECTION_DELETING` at once (§3.1.2;
#302).

When Weaviate can't be read to check that the collection exists, generation answers
`503 SERVICE_UNAVAILABLE` without creating a session or model task (#366); `detail` is
`{"collection": "<name>"}`. The cause is logged, not returned.

If the request that started a generation is cancelled while the session is being prepared,
the preparation still finishes; a session it published is then settled as a cancelled
generation is: its registration ends (so `DELETE` no longer answers `409`) and its status
becomes `cancelled` (#302).

Sampling scans all chunk UUIDs using the SDK iterator without vectors or text properties, then fetches text/metadata only for the at-most 100 selected UUIDs. Returned rows retain rank order; a winner deleted between passes is omitted. Each canonical UUID is ranked by SHA-256 of a versioned domain, the seed (or random nonce), and UUID bytes; UUID order breaks hash ties. The best requested candidates are retained in a bounded heap and returned in rank order. A fixed seed and unchanged UUID population produce the same selected UUIDs and order regardless of backend iteration order. Different seeds can select the same subset, especially when all available objects are selected. This contract concerns selection, not deterministic model answers. Concurrent collection mutation is not a snapshot and can change the candidate population.

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

Generation runs asynchronously. Each sampled chunk permits three validation attempts; each attempt retries a transport failure once. Exhausted model failures are reported with nonempty reasons in `errors` and counted in `pairs_failed`. Generation that finishes processing all sampled chunks has `pairs_attempted = pairs_completed + pairs_failed = pairs_total`, with `pairs_completed` equal to the retained pair count. Partial results finish as `completed`; when every pair fails, status is `failed`. These bounded retries do not guarantee a usable pair for every sampled chunk. Poll for progress and results via `GET /goldstandard/session/{session_id}`.

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

**Session persistence:** Gold standard sessions are stored in `{UPLOAD_DIR}/goldstandard_sessions/` as individual JSON files (`{session_id}.json`). Sessions survive API container restarts. The API loads existing session files on startup into an in-memory dict, then settles sessions left `generating` as `failed` before generation workers start. Settlement durably records a plain interruption reason and preserves completed pairs, acknowledged edits, counters, and all stale/orphaned flags, reasons and timestamps. Completed sessions remain unchanged; ordinary archive scans do not settle active work. Retained pairs can be regenerated after settlement. If settlement fails before replacement, the cache reports failure with storage diagnostics while the prior file remains available for retry on the next startup. A directory-sync failure after replacement retains the existing durability-uncertain policy. The local deployment uses one API process. A process-wide reentrant lock serializes each session mutation, snapshot and durable write across generation, review, regeneration, import storage and history markers. Files use unique temporary names, file fsync, atomic replacement and directory fsync. Readers receive independent snapshots; no lock is held across model calls. Concurrent edits to distinct fields retain each acknowledged change; edits to the same field follow the serialized commit order. Review during generation remains supported. Session files are owner-only (mode 0600); only the API process reads them.

Imported sessions keep a free source identity or receive a new local `gs_` identity when the cache or any existing session file occupies it. Import refuses a source identity that is not `gs_[0-9a-f]{8}` (`PACKAGE_CORRUPT`, export check 4a). Concurrent imports and generation starts share the same process lock for identity selection and durable publication. If identity selection at generation start cannot inspect session storage, or runs out of attempts, generation returns 503 `SESSION_WRITE_FAILED` without writing, and the diagnostic names the candidate session file being checked (or the last one tried). The original session and its newer human edits remain intact. Imported sessions retain `imported_from` (`session_id`, `collection`, `imported_at` UTC); GET session exposes that provenance. Import job `restored_sessions` maps source IDs to local lookup IDs and collections, and the UI notes display those IDs. RAGAS rows keep their four fields.

Regeneration remains synchronous. Model work has an 1800-second total deadline (three validation attempts, each permitting one transport retry with a nominal 300-second call timeout). The exact proxy route allows 1860 seconds; clients waiting through the full budget should allow at least 1920 seconds. Deadline expiry returns 504 `PAIR_GENERATION_TIMEOUT` and leaves the pair untouched.

Regeneration compares the target pair after the model call. If the target changed, it returns 409 `PAIR_CHANGED_DURING_REGENERATION` and preserves the acknowledged edit; updates to other pairs and validity flags are retained. A write failure before replacement returns 503 `SESSION_WRITE_FAILED`, leaving the prior cache/disk snapshot intact. A directory fsync failure after replacement returns 503 `SESSION_DURABILITY_UNCERTAIN`: cache reflects the replacement, but the caller must refresh and inspect storage before retrying. These errors are not acknowledged edits.

`GET /goldstandard/diagnostics` returns `{"issues": [{"filename": "...", "code": "...", "message": "..."}]}`. Unreadable/invalid session files are preserved and reported as `SESSION_READ_FAILED`, rather than silently omitted. An unavailable or unreadable storage directory is reported as `SESSION_STORAGE_UNAVAILABLE` without aborting startup or erasing the cached last valid state. Recovery scans do not create a missing directory. Owned unpublished temporary snapshots left by interruption are preserved and reported as `SESSION_INTERRUPTED_WRITE`; the final JSON remains authoritative. A valid restored file needs API restart to reload its cache. Health displays diagnostic filenames, codes and recovery messages, plus a warning if diagnostics cannot refresh. Diagnostic issues do not change dependency health. Pending refreshes label retained results as previous; failed refreshes clear them, and responses from older refresh requests cannot overwrite newer results. Removed unreadable files or inspected temporary snapshots clear their file-read diagnostics on the next successful scan. Write failures keep their separate successful-write recovery policy; failed generation persists its original code/reason and keeps its warning through status updates and restart. When recording a generation failure itself has uncertain durability, `persistence_error` describes that latest publication failure while `errors` preserves both failure reasons. A generation whose final write keeps failing ends `failed` in the process cache with the write error, ahead of disk, until a later successful write of that session saves it. File inspection occurs outside the writer lock; a scan that races a durable commit is discarded. Session marker write failures are individually reported without aborting the already completed primary delete/import/tuning operation; marker counts include only durably acknowledged updates. A stale/orphaned marker that cannot be written to its session file is saved as a pending marker in `goldstandard_sessions/pending_markers/{session_id}.json`; it is applied when the session loads at startup and to the copies in collection export packages, keeps the session's `SESSION_WRITE_FAILED` diagnostic with marker wording, and is removed by the next successful write of that session. An unreadable pending marker is preserved and reported. If the pending marker cannot be written either, the historical guard lasts only until restart. This is a single-process session-store contract, not cross-process locking, historical-export policy (#47), or a collection-wide export snapshot.

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

`status` must be one of `"approved"`, `"edited"`, `"rejected"`, `"pending"`. Providing `question`, `answer`, or `ground_truth` fields without setting `status: "edited"` is a 422 error. A provided `question`, `answer` or `ground_truth` must contain text: an empty or whitespace-only value is refused with 422 `INVALID_PARAMETER`, since `save` would export it as an empty RAGAS field. Other values are stored as sent. All text fields are optional — only provided fields are updated. The body is validated before the session is looked up.

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

**Response 404** if the session or pair ID does not exist (same error format as PATCH above): `SESSION_NOT_FOUND` for an unknown session, `PAIR_NOT_FOUND` for an unknown pair in an existing session. Neither calls the model.

**Response 409** (`SESSION_BUSY`) if called while the session's `status` is still `"generating"`.

**Response 504** (`PAIR_GENERATION_TIMEOUT`) if model work passes the 1800-second regeneration deadline. The existing pair is unchanged.

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

`filename`: if null or empty, defaults to `{collection}_{YYYYMMDD_HHMMSS}.json` where `collection` is taken from the session's own collection name (spaces become `_`, other characters outside `A-Z`, `a-z`, `0-9` and `_` are dropped). If that name would be longer than 128 characters, the collection part is shortened so the name is exactly 128 characters.

Exports are written to `{UPLOAD_DIR}/goldstandard_exports/{filename}`, a folder of their own, and nowhere else (#370). A supplied `filename` must be 1-128 characters, each an ASCII letter (`A-Z`, `a-z`), a digit, `_`, `-`, `.` or a space, and must end in `.json` (any letter case). It is checked as given: a path is refused, not stripped, and a missing `.json` is refused, not appended. Saving under the name of an existing export overwrites that export. The export is written to a new temporary file in the exports folder and then renamed onto its name, so a reader never sees a partly written export, and a symbolic link that appears at the name is replaced, not followed (#402).

**Response 400** (`INVALID_PATH`) if `filename` breaks that rule (for example `.`, `..`, `metrics.jsonl`, `report.txt`, `a/b.json`), or if something other than a regular file (a directory or a symbolic link) already has that name in the exports folder. Nothing is written. The session check (404) and the historical check (409) come first.

**Response 409** (`EXPORTS_FOLDER_INVALID`) if `goldstandard_exports` in `UPLOAD_DIR` is not a directory: a file saved under that name before #370, or a symbolic link (#402). Nothing is written; move or rename that entry, then retry. The name check (400) comes first.

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

Returns the saved file from `{UPLOAD_DIR}/goldstandard_exports/` as a JSON download (`Content-Disposition: attachment`). Only that folder is served: no other file in `UPLOAD_DIR` is. Exports saved at the top level of `UPLOAD_DIR` before #370 stay on disk but are not served.

**Response 400** (`INVALID_PATH`) if `filename` breaks the save name rule, or if the name in the exports folder is not a regular file (a directory or a symbolic link).

**Response 409** (`EXPORTS_FOLDER_INVALID`) if `goldstandard_exports` in `UPLOAD_DIR` is not a directory (a file or a symbolic link has its name), as on save (#402). The name check (400) comes first.

**Response 404** if `filename` is valid and no export has that name:
```json
{ "error": { "code": "FILE_NOT_FOUND", "message": "File 'Documents_20260910_143022.json' not found.", "detail": null } }
```

---

#### 3.1.6 Metrics

```
GET /metrics/latency
```

Returns rolling aggregate statistics AND a time-series history for charting. The API service maintains an in-memory ring buffer of the last 500 query records, persisted to `{UPLOAD_DIR}/metrics.jsonl` for survival across restarts.

The file is bounded (#374). Each query appends one line; when that brings the file to 1,000
lines, it is compacted to its last 500 records by writing them to a temporary file in the same
folder, fsyncing it and replacing `metrics.jsonl` atomically, so a crash leaves either the old
file or the compacted one, never a mix. At startup the API reads at most the last 1 MiB of the
file, loads its last 500 records into the ring, and compacts the file to them at once if it
held anything more (an older unbounded file, or lines that aren't records); it also removes
temporary files left by a compaction a crash interrupted. A record whose write fails (for
example on a full `ingest_uploads` volume), a failed compaction, or a metrics file that can't
be read at startup is logged and changes nothing else: the query still answers 200 with its
answer, the record still counts in the ring, and the API still starts. A line left unfinished
by a failed write doesn't cost the next record, which starts a line of its own.

**Query parameters:** `collection`, `retrieval_mode` (both optional filters), and
`history_limit` (integer, 0-500, default 100). Queries are recorded, and `collection` is
matched, under the stored collection name (§3.1, #372), so `myDocs` and `MyDocs` select the
same records.

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

#### 3.1.8 Authentication and the endpoint/role matrix (#25)

Every route needs an account, except `GET /health` (unchanged, for container healthchecks), `GET /auth/status`
and `POST /auth/login`. Roles are ordered `end_user` < `developer` < `engineer`; a role may do everything a lower
one may. The role of a request is the one stored for its user at that moment, never anything the client sends.

| Needs | Routes |
|---|---|
| nothing | `GET /health`, `GET /auth/status`, `POST /auth/login` |
| any session (also one with a password change due) | `GET /auth/me`, `POST /auth/logout`, `POST /auth/password` |
| `end_user` | `GET /collections`, `POST /query`, `GET /retrieval/config/{collection}` |
| `developer` | `POST /collections`; `POST /ingest/upload`, `GET /ingest/job/{job_id}`, `GET /ingest/config/{collection}`, `POST /ingest/config`; `POST /retrieval/config`; `POST /goldstandard/generate`, `GET /goldstandard/session/{session_id}`, `PATCH /goldstandard/session/{session_id}/pair/{pair_id}`, `POST /goldstandard/regenerate`, `POST /goldstandard/save`, `GET /goldstandard/download/{filename}`; `POST /export`, `GET /export/job/{job_id}`, `POST /import`, `GET /import/job/{job_id}`, `GET /packages`, `GET /help/transfer`; `GET /tune/{collection}` |
| `engineer` | `DELETE /collections/{name}`; `POST /tune/rechunk`, `POST /tune/reembed`, `POST /tune/reindex`, `GET /tune/job/{job_id}`; `GET /metrics/latency`; `GET /goldstandard/diagnostics`; FastAPI's `GET /docs`, `GET /docs/oauth2-redirect`, `GET /redoc`, `GET /openapi.json`; account administration: `GET /auth/users`, `POST /auth/users`, `POST /auth/users/{username}/reset-password`, `POST /auth/users/{username}/role`, `POST /auth/users/{username}/disable`, `POST /auth/users/{username}/enable`; service principals and keys: `GET /auth/service-principals`, `POST /auth/service-principals`, `POST /auth/service-principals/{name}/role`, `POST /auth/service-principals/{name}/disable`, `POST /auth/service-principals/{name}/enable`, `POST /auth/service-principals/{name}/keys`, `POST /auth/service-principals/{name}/keys/{key_id}/revoke`; the audit log (§3.1.9): `GET /audit`, `GET /audit.csv` |

The matrix is one table in `api/auth/policy.py`. A route without an entry is refused for everyone (`403
FORBIDDEN`), and a registered check fails when a route lacks an entry or an entry names no route. Jobs and
gold-standard sessions have no owner: any user with the route's role may poll or open them. A path that matches
no route keeps `404 NOT_FOUND`, and a wrong method `405 METHOD_NOT_ALLOWED`. A path holding a control character
(for example a trailing newline, `%0A`, which Starlette's `$`-anchored routes would still match) answers
`404 NOT_FOUND` before anything else. The check runs before the route reads the request body, so a refused upload
is never received.

**Refusals**, the first that applies answering: `503 SETUP_REQUIRED` (no account exists yet); `401
AUTH_REQUIRED` (no valid session); `403 CROSS_ORIGIN_REFUSED` (a cookie-authenticated `POST`, `PUT`, `PATCH` or
`DELETE` whose `Origin` names another host or port than its `Host`, or that has no `Origin` and no `Sec-Fetch-Site:
same-origin`); `403 PASSWORD_CHANGE_REQUIRED` (a generated password is due for change; only the session routes
answer); `403 FORBIDDEN` (the role is too low; `detail: {"required_role": ...}`). The API has no CORS middleware
and sends no `Access-Control-*` headers. A request with an `Authorization` header is a service key request and
has its own refusals (**Service keys**, below).

**Accounts.** Usernames are 3-32 characters of lowercase letters, digits, `.`, `_` and `-`; input is lowercased,
so they are case-insensitive. Passwords are 12-256 characters and not the username (ignoring case); no
composition rules. They are stored only as `scrypt$<log2 n>$<r>$<p>$<salt>$<hash>` with n = 2^15, r = 8, p = 1
and a random 16-byte salt. The operator command (below) creates the first engineer and recovers lockouts; after
that engineers manage accounts through the account administration routes (below) and the Users page (§7.12).
No route deletes an account: accounts are disabled, so a username is never reused.

**Sessions.** Server-side, in the SQLite file `auth.db` in `AUTH_DIR` (the `auth_data` volume), with accounts
and the login throttle, so all survive an API restart. The cookie `rag_session` holds a random 256-bit token; the
store keeps its SHA-256. Cookie attributes: `HttpOnly`, `SameSite=Strict`, `Path=/`, `Max-Age=604800` (it
survives closing the browser), and `Secure` when `AUTH_COOKIE_SECURE=true`. A session ends after 12 hours without
a request or 7 days after login, whichever is first; an ended session is deleted. Every request re-reads the
user: a role change applies to the next request; disabling an account, resetting its password and logging out
delete its sessions.

**Login throttling.** Failed logins are counted per (username as typed, lowercased; client address, `X-Real-IP`
from nginx). Five within 15 minutes throttle the pair for 15 minutes: its logins answer `429 LOGIN_THROTTLED`
without checking the password. The same applies to unknown usernames. An attempt is counted before its password
is checked, in the same transaction as the throttle check, so parallel attempts can't exceed five. A successful
login clears the pair's count. A wrong current password on `POST /auth/password` counts against the same pair, and
a throttled pair's password change answers `429 LOGIN_THROTTLED`. Behind Docker's port publishing every client on
the host may share one address, so in practice the throttle is per username.

| Route | Request | Success |
|---|---|---|
| `GET /auth/status` | | `200 {"setup_required": bool}` |
| `POST /auth/login` | `{"username", "password"}` | `200 {"username", "role", "must_change_password"}` and the cookie. Every failure (unknown user, wrong password, disabled account, malformed username) is the same `401 LOGIN_FAILED` "The username or password is incorrect.", checked against a hash either way so the time doesn't differ |
| `GET /auth/me` | | `200 {"username", "role", "must_change_password", "kind"}`; `kind` is `user` for a session and `service` for an API key (whose `must_change_password` is `false`) |
| `POST /auth/logout` | | `200 {"status": "logged_out"}`; the session is deleted and the cookie expired |
| `POST /auth/password` | `{"current_password", "new_password"}` | `200 {"status": "changed", "username", "role", "must_change_password": false}`; the user's other sessions end, this one stays. A wrong current password is `403 PASSWORD_INCORRECT`; a new one breaking the rules, or equal to the current one (so a generated password can't be kept), is `422 INVALID_PASSWORD` naming the rule |

**Account administration** (engineers only). `User` is `{"username", "role", "enabled", "must_change_password",
"created_at"}` (`created_at` in ISO 8601 UTC); it never holds a password, hash or session. A `{username}` in a path
is lowercased; one that doesn't exist, or breaks the username rule, is `404 USER_NOT_FOUND`. A role other than
`end_user`, `developer` or `engineer`, or a missing field, is `422 INVALID_PARAMETER`.

| Route | Request | Success |
|---|---|---|
| `GET /auth/users` | | `200 {"users": [User, ...]}`, by username |
| `POST /auth/users` | `{"username", "role"}` | `201 {"user": User, "password"}`: an enabled account with a generated password, to be changed at first login. A username breaking the rule is `422 INVALID_USERNAME`; one that exists (ignoring case) is `409 USER_EXISTS` |
| `POST /auth/users/{username}/reset-password` | | `200 {"user": User, "password"}`: a new generated password, to be changed at next login; the user's sessions end. The engineer's own account is `409 SELF_RESET_REFUSED` (use `POST /auth/password`, which needs the current password) |
| `POST /auth/users/{username}/role` | `{"role"}` | `200 {"user": User}`; existing sessions take the role on their next request |
| `POST /auth/users/{username}/disable` | | `200 {"user": User}`; the user's sessions end and their logins fail with `LOGIN_FAILED` |
| `POST /auth/users/{username}/enable` | | `200 {"user": User}`; the user can log in again with their current password |

A generated password (as the operator command's) is in its `201` or `200` answer only, sent with
`Cache-Control: no-store`, and nowhere else. Demoting or disabling the last enabled engineer is `409
LAST_ENGINEER`, for the engineer's own account too; the check and the change are one transaction. Otherwise an
engineer may demote or disable themselves (disabling ends their own session). Setting the current role, or
disabling a disabled account, succeeds and changes nothing.

**Service keys** (PR 3 of #25). A *service principal* is an account with `kind = 'service'` in the same `users`
table (an existing store gains the `kind` column on first open, every existing row `user`; schema version 2). It
has a name under the username rule, sharing one name pool with people, and one role; it has no usable password
and no session, so a login with its name is the generic `401 LOGIN_FAILED` (checked against the dummy hash). It is
disabled, never deleted. Keys are issued only to principals. The last-enabled-engineer rule counts people only.

A key is `rag_` + a 12-character id (lowercase letters and digits) + `_` + 43 characters of
`secrets.token_urlsafe(32)` (256 bits), 60 characters. The `api_keys` table keeps the id, the principal, the
key's SHA-256, a label (0-64 characters), created, expires, revoked and last used (written at most once a minute);
never the key. A key expires a whole number of days after issuance, 1-365, default 90, and can't be extended;
revoking is immediate; revoked and expired keys stay listed and never work again. There is no rotate operation:
issuing another key rotates, and the old key works until it is revoked or expires. A principal may hold any
number of keys.

A request with an `Authorization` header is authenticated by it alone; a session cookie on it is ignored, with no
fallback. The header must be exactly one `Bearer <key>` (scheme case-insensitive) with a key of that form; a key in
a query parameter, a cookie or another header is no credential (`401 AUTH_REQUIRED`). A valid key (known, hash
matching, not revoked, not expired, its principal enabled) runs as its principal with the principal's role at that
moment, so a role change applies at once, disabling stops all the principal's keys and enabling restores the
unrevoked, unexpired ones. Refusals, first that applies: `503 SETUP_REQUIRED`; `401 KEY_INVALID` "The API key is
invalid, expired or revoked." for every reason; `403 KEY_NOT_ALLOWED` on every `/auth/users...` and
`/auth/service-principals...` route, `POST /auth/logout`, `POST /auth/password`, `GET /audit` and `GET
/audit.csv`, whatever the role; `403
FORBIDDEN` when the role is too low. A key request has no origin check and no password-change check. Public
routes ignore the header. Failed key requests are neither logged one by one nor throttled.

**Service principal and key administration** (engineers' sessions only). `Principal` is `{"name", "role",
"enabled", "created_at", "keys": [Key, ...]}`, keys newest first; `Key` is `{"id", "label", "created_at",
"expires_at", "revoked_at", "last_used_at", "status"}`, times ISO 8601 UTC or `null`, `status` `revoked`, else
`expired`, else `active`. Neither holds a key, hash or password.

| Route | Request | Success |
|---|---|---|
| `GET /auth/service-principals` | | `200 {"principals": [Principal, ...]}`, by name |
| `POST /auth/service-principals` | `{"name", "role"}` | `201 {"principal"}`. A name breaking the rule is `422 INVALID_USERNAME`; one a person or principal holds is `409 PRINCIPAL_EXISTS` |
| `POST /auth/service-principals/{name}/role` | `{"role"}` | `200 {"principal"}`; its keys take the role on their next request |
| `POST /auth/service-principals/{name}/disable` | | `200 {"principal"}`; its keys fail until it is enabled |
| `POST /auth/service-principals/{name}/enable` | | `200 {"principal"}` |
| `POST /auth/service-principals/{name}/keys` | `{"label"?, "days"?}` | `201 {"principal", "key", "secret"}`, with `Cache-Control: no-store`; `secret` is the key, in this answer only |
| `POST /auth/service-principals/{name}/keys/{key_id}/revoke` | | `200 {"principal"}`; revoking again keeps the first revoked time |

A `{name}` that isn't a principal (unknown, a person's account, or breaking the rule) is `404 PRINCIPAL_NOT_FOUND`;
a `{key_id}` not of that principal is `404 KEY_NOT_FOUND`; a bad role, `days` outside 1-365 or not a whole number,
a label over 64 characters, or a missing field is `422 INVALID_PARAMETER`. Setting the current role, or disabling a
disabled principal, changes nothing. A key may be issued to a disabled principal; it works once the principal is
enabled. The account routes stay for people: `GET /auth/users` lists no principal, a principal's name in a
`/auth/users/{username}/...` path is `404 USER_NOT_FOUND`, and `POST /auth/users` with a principal's name is
`409 USER_EXISTS`.

**Operator command**, inside the api container: `docker compose exec api python -m auth <subcommand>`.

| Subcommand | Effect |
|---|---|
| `bootstrap USERNAME` | Only when no account exists: creates USERNAME as an engineer |
| `create-user USERNAME ROLE` | Only after bootstrap; refused if the name exists |
| `reset-password USERNAME` | New generated password; the user's sessions end |
| `set-role USERNAME ROLE` | Applies to existing sessions on their next request |
| `disable USERNAME` / `enable USERNAME` | Disabling ends the user's sessions |
| `list` | Username, role, enabled, password change due (`service account` for a service principal), created; never a password, hash or key |

`bootstrap`, `create-user` and `reset-password` print a generated password (24 characters, 144 random bits)
alone on standard output and nowhere else, and mark it to be changed at first login. Each change writes one line
to standard error; exit status 0 done, 1 refused, 2 usage error. The last enabled engineer can't be disabled or
demoted; an account a user is locked out of (lost password, disabled) is recovered with `create-user`, `enable` or
`reset-password`, which don't lift a login throttle (it ends by itself after 15 minutes). No account or password comes
from the environment, and none exists by default. The command issues no keys; `set-role`, `disable` and `enable`
work on service principals too (disabling one stops its keys), `reset-password` refuses one (exit 1), and
`create-user` is refused for a principal's name.

**Upgrading.** An existing install answers `503 SETUP_REQUIRED` everywhere but `/health` until `bootstrap`. The
proxy must be recreated (`docker compose up -d --force-recreate proxy`, or the whole stack): `proxy/nginx.conf`
is a single-file bind mount, and a proxy still running the old copy sends `Host` without the port, so every
cookie-authenticated change fails the origin check with `403 CROSS_ORIGIN_REFUSED`.

**Logging.** One `WARNING` line per login attempt (success, failure, throttled) on logger `auth`, with the
username and client address; never a password, token or cookie. One `WARNING` line per account administration
change (create, reset, role, disable, enable) naming the engineer, the change and the account, and one per refusal
(`INVALID_USERNAME`, `USER_EXISTS`, `USER_NOT_FOUND`, `LAST_ENGINEER`, `SELF_RESET_REFUSED`) saying why; never a
password. One `WARNING` line per service principal or key change (create, role, disable, enable, key issued, key
revoked) naming the engineer, the principal and the key id (and the role or expiry), and one per refusal
(`INVALID_USERNAME`, `PRINCIPAL_EXISTS`, `PRINCIPAL_NOT_FOUND`, `KEY_NOT_FOUND`); never a key or its hash.
Successful key requests aren't logged one by one (`last_used_at` records them). The telemetry route vocabulary gains the eighteen `/auth` routes as labels.
These lines are unchanged by the audit log (§3.1.9), which records the same events durably.

#### 3.1.9 Audit log (#29)

An append-only record of who did what, to which data, with what result, for AI Engineers to review and export.
It is separate from OpenTelemetry, which is operational and never durable audit storage.

**Storage.** JSON Lines in `/app/uploads/audit/` on the `ingest_uploads` volume (`AUDIT_DIR`): `audit.jsonl`, and
after rotation `audit.jsonl.1` (newest) to `audit.jsonl.<AUDIT_LOG_KEEP>` (oldest). Folder mode 0700, files 0600.
After an append leaves `audit.jsonl` at `AUDIT_LOG_MAX_BYTES` or more (default 10485760, at least 4096) the files
shift by one and the one beyond `AUDIT_LOG_KEEP` (default 10, 1-1000) is deleted. No time-based retention; no
route edits or deletes a record. Records survive restarts and `docker compose down` without `-v`. Backup (#38) isn't
built yet; it is to include this folder.

**Record.** One JSON object per line with exactly these keys, `null` where a value doesn't apply:

| Key | Value |
|---|---|
| `id` | Write time in nanoseconds (19 digits), `-`, 6 hex characters. Assigned under the log's lock, so line order is id order |
| `time` | UTC, ISO 8601 with milliseconds and `Z` |
| `actor` | Username, service principal name, `operator`, `system`, or `null` (nobody identified) |
| `actor_kind` | `user`, `service`, `operator`, `system` or `anonymous` |
| `role` | The actor's role at that request, from the server-side principal |
| `key_id` | The public id of the API key used |
| `client_ip` | `X-Real-IP` (the address nginx saw), else the connection's address. On Docker Desktop all clients share one address (README "Sharing with your office") |
| `action` | See below |
| `collection` | The collection acted on |
| `target` | The account or principal acted on, or a failed login's typed username |
| `result` | `success` (2xx), `refused` (4xx), `error` (5xx); for jobs `accepted`, then `succeeded`, `failed` or `interrupted` |
| `status_code` | The HTTP status; `null` for job ends, operator and system records |
| `job_id` | The job's id |
| `details` | The action's other parameters (below); `code`: the error envelope's code on a non-2xx answer |

**Actions.** `auth.login` (success, failure, throttled), `auth.logout`, `auth.password`; `query`;
`collection.create`, `collection.delete`; `ingest`, `ingest.config`; `retrieval.config`; `export`, `import`;
`tune.rechunk`, `tune.reembed`, `tune.reindex`; `goldstandard.save`, `goldstandard.download`; `admin.user.create`,
`admin.user.reset-password`, `admin.user.set-role`, `admin.user.disable`, `admin.user.enable`;
`admin.principal.create`, `admin.principal.set-role`, `admin.principal.disable`, `admin.principal.enable`,
`admin.key.issue`, `admin.key.revoke`; `audit.view`, `audit.download`; `operator.bootstrap`, `operator.create-user`,
`operator.reset-password`, `operator.set-role`, `operator.disable`, `operator.enable` (`python -m auth`; not `list`,
not usage errors); `system.start` (each API start, after the job records are loaded, with `question_text_logged`,
`max_bytes` and `keep`). Nothing else: no other GET, no job polling, no gold-standard generate, edit or regenerate.

**When.** A request to an audited route writes one record once its answer has been sent, so a page or CSV never
holds its own `audit.view` or `audit.download`. A refusal by the authentication middleware is recorded only when it
is a 403 for an identified principal (`FORBIDDEN`, `KEY_NOT_ALLOWED`, `CROSS_ORIGIN_REFUSED`,
`PASSWORD_CHANGE_REQUIRED`); `401 AUTH_REQUIRED`, `401 KEY_INVALID` and `503 SETUP_REQUIRED` write nothing. A
failed or throttled login has actor `null`, kind `anonymous` and the typed username as `target`. A job (ingest,
export, import, tune) gets an `accepted` record (status 202) when it is stored, before it can end, and one end
record with the same actor and job id: `succeeded` for `completed`, `failed` for `failed`, `partial` or
`cancelled` (`details.job_status` says which), `interrupted` when the API's start marks it so. Jobs started before
this feature get no end record.

**Details.** `query`: `retrieval_mode`, `top_k`, `alpha`, `response_format`, `include_citations`, and `question`
only when `AUDIT_LOG_QUESTIONS=true` (exactly `true` or `false`, default `false`; anything else stops the API);
never the answer. `ingest`: `files` (names), `file_count`, `total_bytes`. `export`: `include_models`, `encrypted`,
and at the end `package` and `fidelity`. `import`: `package`, `on_conflict`, `encrypted`; the end record's
`collection` is the one imported into. `tune.*`: `operation`. `goldstandard.save` and `goldstandard.download`:
`file` (and `session_id` for save). Admin actions: `role` for creates and role changes, `key_id` (and `expires_at`
on issue) for keys. Audit reads: the filters given. Operator actions: `role` where given, `code` when refused.
Never recorded: a password (typed, current, new or generated), session cookie or token, API key, passphrase, answer
or document text.

**Write failures** don't change the action's answer: one `ERROR` line on logger `audit` naming the action, and the
API counts it (`failed_writes`). The operator command prints one more `auth:` line instead. The API and the
operator command append under a thread lock and an `flock` on `.lock`, so records are whole lines.

**`GET /audit`** (engineer sessions; every API key `403 KEY_NOT_ALLOWED`). Query parameters, all optional: `from`
(inclusive) and `to` (exclusive), ISO 8601, UTC when no offset is given; `actor`, `action`, `collection`, `result`
(exact matches; `result` one of the values above); `limit` 1-1000 in ASCII digits, default 100; `before`, a record
`id`. Any invalid value: `422 INVALID_PARAMETER`. Answer (`Cache-Control: no-store`):

```json
{"records": [{"id": "...", "time": "...", "...": "..."}], "next_before": "<id or null>",
 "failed_writes": 0, "question_text_logged": false}
```

Records are newest first across every file; `next_before` is the last record's id when older matches exist.
`before` returns the records whose id sorts below it, so pages don't shift as records arrive and a cursor whose
record was rotated away still works. A line that isn't a JSON object (a torn last line) is skipped; the next append
starts a new line. A read opens every file under the writers' lock and then reads the open files, so a rotation
during it can't make it repeat or miss a record that existed when it started.

**`GET /audit.csv`** (same access and filters, no `limit` or `before`): `200`, `text/csv; charset=utf-8`,
`Content-Disposition: attachment; filename="audit-<yyyymmddThhmmssZ>.csv"`, `Cache-Control: no-store`, streamed.
Header `time,actor,actor_kind,role,key_id,client_ip,action,collection,target,result,status_code,job_id,details`,
then every matching record newest first, `details` as compact JSON, empty cells for `null`; a cell starting with
`=`, `+`, `-`, `@`, a tab or a carriage return gets a leading `'`.

### 3.2 Error Response Format

All errors use a consistent envelope, including the ones no route raises itself (#373): a
path that matches no API route answers `404 NOT_FOUND`, a method the route doesn't accept
answers `405 METHOD_NOT_ALLOWED` (with the `Allow` header), and an error no route handles
answers `500 INTERNAL_ERROR`, its cause logged, not returned. `detail` is `null` for all three.

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
| `COLLECTION_BUSY` | 409 | Delete refused while an ingest, export, import, tune or gold-standard generation job is active for the collection; generation could not get the collection's writer guard within one second |
| `COLLECTION_DELETING` | 409 | A new ingest, export, tune or generation job refused while a delete of its collection is in progress (an import job fails with it) |
| `EXPORT_STARTING` | 409 | An export of the collection is still storing its first record after 30 s; no `job_id`; retry shortly |
| `INVALID_FILE_TYPE` | 422 | Uploaded file type not supported |
| `JOB_NOT_FOUND` | 404 | Ingest job ID not found |
| `SERVICE_UNAVAILABLE` | 503 | Weaviate or Ollama unreachable |
| `NOT_FOUND` | 404 | No API route matches the path |
| `METHOD_NOT_ALLOWED` | 405 | The route doesn't accept the request's method |
| `INTERNAL_ERROR` | 500 | An unexpected error no route handles; the cause is logged, not returned |
| `INVALID_PARAMETER` | 422 | Request parameter out of range or invalid |
| `INVALID_SETTINGS` | 422 | Multipart chunking settings invalid before ingest work |
| `SESSION_NOT_FOUND` | 404 | Gold standard session ID not found |
| `PAIR_CHANGED_DURING_REGENERATION` | 409 | Target changed during model generation; acknowledged edit retained |
| `SESSION_WRITE_FAILED` | 503 | Failure before replacement; prior session snapshot unchanged |
| `PAIR_GENERATION_TIMEOUT` | 504 | Regeneration model deadline exceeded; existing pair unchanged |
| `SESSION_DURABILITY_UNCERTAIN` | 503 | Replacement published but directory durability uncertain; refresh before retry |
| `CONFIRMATION_REQUIRED` | 400 | Destructive operation called without `?confirm=true` |
| `INVALID_PATH` | 400 | Gold-standard export name breaks the name rule (1-128 ASCII letters, digits, `_`, `-`, `.` or spaces, ending in `.json`), or the name in the exports folder is not a regular file |
| `FILE_NOT_FOUND` | 404 | Requested download file does not exist |
| `EXPORTS_FOLDER_INVALID` | 409 | Gold-standard save or download refused: `goldstandard_exports` in `UPLOAD_DIR` is a file or a symbolic link, not a directory; move or rename it |
| `PAIR_NOT_FOUND` | 404 | pair_id not found within the given session |
| `SESSION_BUSY` | 409 | Operation not allowed while session is still generating |
| `HISTORICAL_SESSION` | 409 | Explicit historical export choice required for stale/orphaned session |
| `SETUP_REQUIRED` | 503 | No account exists yet; run `python -m auth bootstrap` (§3.1.8) |
| `AUTH_REQUIRED` | 401 | No valid session: none, unknown, ended or of a disabled user |
| `FORBIDDEN` | 403 | The user's role is too low (`detail.required_role`), or the route has no policy entry |
| `CROSS_ORIGIN_REFUSED` | 403 | A cookie-authenticated change from another origin |
| `PASSWORD_CHANGE_REQUIRED` | 403 | A generated password must be changed first; only `/auth/me`, `/auth/logout` and `/auth/password` answer |
| `LOGIN_FAILED` | 401 | Login refused; the same answer for every cause |
| `LOGIN_THROTTLED` | 429 | Five failed logins for this username and address within 15 minutes; wait 15 minutes |
| `PASSWORD_INCORRECT` | 403 | The current password given to `POST /auth/password` is wrong |
| `INVALID_PASSWORD` | 422 | The new password breaks a rule; the message names it |
| `INVALID_USERNAME` | 422 | `POST /auth/users`: the username breaks the rule; the message states it |
| `USER_EXISTS` | 409 | `POST /auth/users`: an account with this username exists |
| `USER_NOT_FOUND` | 404 | No account has the username in the path |
| `LAST_ENGINEER` | 409 | The change would demote or disable the last enabled engineer |
| `SELF_RESET_REFUSED` | 409 | An engineer's reset of their own password; they use `POST /auth/password` |
| `KEY_INVALID` | 401 | An `Authorization` header that isn't one valid `Bearer` key: unknown, malformed, expired, revoked or of a disabled principal |
| `KEY_NOT_ALLOWED` | 403 | An API key on account, principal or key administration, logout, password change or the audit log |
| `PRINCIPAL_EXISTS` | 409 | `POST /auth/service-principals`: a person or principal already has the name |
| `PRINCIPAL_NOT_FOUND` | 404 | No service principal has the name in the path |
| `KEY_NOT_FOUND` | 404 | The principal has no key with the id in the path |

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
opentelemetry-sdk==1.45.1
opentelemetry-exporter-otlp-proto-http==1.45.1
```

**Dependency pinning (normative).** The list above is *intent*, held in
`api/requirements.in`. It is **not** what the build installs. `api/requirements.txt`
is a generated lock containing every package pinned to an exact version, and the
Dockerfile installs the lock. Without this, an unpinned rebuild resolves whatever
is current: that is exactly how `weaviate-client` drifted to a release requiring a
newer Weaviate server than the one pinned here, breaking every collection call
while the stack still reported healthy.

The SDK and OTLP HTTP exporter entries in `api/requirements.in` are required
for enabled telemetry startup; lock regeneration must retain both at 1.45.1
and resolve their transitive dependencies.

Regenerate the lock from the repo root using the procedure at the top of
`api/requirements.in`: resolve the edited inputs against the current API image
with `pip install --dry-run --report`, incorporate the resolved pins into the
sorted lock, rebuild, and compare the resulting image's `pip freeze` (excluding
torch/torchvision) to the lock. Editing the input and rebuilding alone does not
resolve new dependencies, because the Dockerfile installs only the lock.

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

An optional `passphrase` (12 to 1024 characters, any characters) writes the
package encrypted, as `<stem>.tar.gz.age`: an age v1 file with one scrypt
(passphrase) recipient, work factor 18, streamed in 64 KiB chunks so no
unencrypted archive is written and the package is never held in memory
(`RAG_EXPORT_SPECIFICATIONS.md` §4.6; #27). Without it the export is unencrypted,
as before. The passphrase is never stored in the job record, logged, attached to
telemetry or returned; the job reports `encrypted`.

- **422 `INVALID_PARAMETER`** for a `passphrase` shorter than 12 or longer than
  1024 characters, or one with no UTF-8 form (a lone surrogate). No job is created, and the response does not contain the
  passphrase.

**Response 202:**
```json
{ "job_id": "0c5fd445", "status": "queued", "collection": "Policies" }
```

- **404 `COLLECTION_NOT_FOUND`** if the collection does not exist. Checked
  before a job is created, so a typo does not leave a failed job behind.
- **503 `SERVICE_UNAVAILABLE`** when Weaviate can't be read for that check
  (#366). No job is created; `detail` is `{"collection": "<name>"}`. The cause
  is logged, not returned.
- **409 `EXPORT_IN_PROGRESS`** if an export of the same collection is already
  running; `detail.job_id` names it. Two concurrent exports of one collection
  would race on the staging directory and the temporary archive. Exports of
  *different* collections run concurrently. A request that arrives while that
  export is still storing its first record waits for it, at most 30 s, so the
  named `job_id` can always be read; if that start is cancelled instead, or its
  record write fails, the request starts its own export (#326, #347).
- **409 `EXPORT_STARTING`** if that export is still storing its first record
  after the 30 s wait. `detail` is null: no `job_id`, since that job can't be
  read yet. Retry shortly (#338).
- **409 `COLLECTION_DELETING`** while a delete of the collection is in progress
  (§3.1.2; #302). No job is created.

The build runs off the event loop, so an export never blocks queries or the API.
It holds the collection's writer guard for its whole run (#509), so its chunks and
retained sources come from one point in time: while it runs, guarded writes to that
collection wait (ingest, re-ingest and tuning jobs stay `queued`; a replace-import
waits before its conflict check) or,
for gold-standard generation, are refused `409 COLLECTION_BUSY` after one second.
Other collections are not affected. Settings saves and gold-standard session edits
don't take the guard, so a package's config and session files are each coherent but
may be newer than its chunks (`RAG_EXPORT_SPECIFICATIONS.md` §4.2). Chunks are
streamed one at a time and never accumulated: a 100k-chunk corpus is roughly
880 MB of JSON.

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
  "encrypted": false,
  "warnings": [],
  "error": null
}
```

`status` is `queued`, `running`, `completed` or `failed`; an export waiting for its
collection's writer guard reads `queued` (#509). On failure `error`
carries the reason and `filename` stays null. An export whose build is cancelled
reads `failed`, with an `error` saying it was cancelled before it finished and to
start it again (#521). Export, import and tune job records persist
across an API restart like ingest's (§3.1.3); a job the restart interrupted reads `failed`
with `error_code` `INTERRUPTED` and an `error` saying the API restarted while it was running. A failed build leaves nothing
behind: the staging directory and the partial archive are both removed, and the
collection's export slot is released. A build interrupted by a restart leaves them in
`EXPORTS_DIR` (`.build-*`, and `.<stem>.tar.gz.partial` or, encrypted, `.<stem>.tar.gz.age.partial`) until the next startup removes them,
before the first request; completed packages are never touched (#377).

`warnings` is non-fatal. It records, for example, chunks whose `source_sha256`
could not be resolved to exactly one retained document that the package carries;
their `source_sha256` is `null` (#509).

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

`passphrase` is required for an encrypted package, one whose name ends in
`.tar.gz.age`, and refused for any other (#27). It is not checked for length
here; one with no UTF-8 form (a lone surrogate) is refused `422
INVALID_PARAMETER`, as on export. The job decrypts the package as a stream while unpacking it, and every
check from the manifest on reads the decrypted files; it is never stored in the
job record, logged, attached to telemetry or returned. The job reports
`encrypted`.

**Response 202:** `{ "job_id": "...", "status": "queued", "filename": "..." }`

- **400 `PASSPHRASE_REQUIRED`** for a `.tar.gz.age` package sent without a
  passphrase, and **400 `PASSPHRASE_NOT_EXPECTED`** for a passphrase sent with any
  other package. Both are decided by the filename, before any job exists, and
  change nothing; `detail` is `{"filename": "<as sent>"}`.

**409 `IMPORT_IN_PROGRESS`** if the same package file is already being imported:
the file's name in `./exports` is compared, so `x.tar.gz`, `./x.tar.gz` and
`a/../x.tar.gz` are one file (#391).

Everything else — unreadable archive, a wrong passphrase or a damaged encrypted
package (`DECRYPT_FAILED`, with `error_detail.reason` `passphrase`, `header`,
`payload` or `work_factor`, the last for a scrypt work factor above 20 or not written as one or two decimal digits, refused before any key derivation; a file cut inside the payload nonce is `payload`; nothing is touched), bad digest, embedding mismatch, name
collision, a target being deleted (`COLLECTION_DELETING`, §3.1.2), another import
into the same target collection (`IMPORT_IN_PROGRESS`) — is reported through the
job rather than this response, because none of it is knowable without reading the
package. One import runs per target collection, whatever its `on_conflict`: a second
import into a collection another import is building fails with `error_code`
`IMPORT_IN_PROGRESS` and `error_detail` `{"collection": "<target>"}`, and changes
nothing. A `rename` that moves to a new name competes for that name, not the
original (#391).

A package's ingest settings (`ingest_config.json`, optional) are checked before the
import changes anything, like its retrieval settings (#510). They must be a JSON
object that `POST /ingest/config` would accept, except that a `chunk_size` or
`min_chunk_size` from before the chunk bounds (§8) is kept as it is: `chunk_size`
only has to be an integer of at least 1 and `min_chunk_size` one of at least 0.
Missing fields take the defaults, numeric strings are converted and unknown fields
are dropped. Anything else (invalid JSON, a non-object, an unknown strategy, a wrong
type or an out-of-range value) fails the job in every conflict mode with
`error_code` `PACKAGE_CORRUPT`, `error` `Invalid ingest settings.` and
`error_detail` `{"file": "ingest_config.json"}`, before models are installed or any
collection, source or setting is touched. A completed import publishes the checked
settings for the collection it imported, which `GET /ingest/config/{collection}`
returns with `is_default: false`. Rules and order: `RAG_EXPORT_SPECIFICATIONS.md`
§6.2, check 4c.

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
  "error_detail": null,
  "encrypted": false
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
- **409 `COLLECTION_DELETING`** while a delete of the collection is in progress
  (§3.1.2; #302). No job is created.
- **409 `SOURCES_REQUIRED`** from `/tune/rechunk`, and from `/tune/reembed` with
  chunking fields, when the collection retains originals but none of the names
  they list has stored chunks (every file produced no chunks at ingest), so there
  is nothing to rebuild (#328). No job is created. The other `SOURCES_REQUIRED`
  refusals below still come through the job.
- **409 `SOURCE_INDEX_INVALID`** from `/tune/rechunk`, and from `/tune/reembed`
  with chunking fields, when the collection's retained source index is invalid
  (#389). No job is created; repair the index before re-chunking. Re-embedding
  without chunking fields and re-indexing don't read the retained sources, so
  they still start.
- **503 `SERVICE_UNAVAILABLE`** from those same requests when Weaviate can't be
  read for that check (#340). No job is created; try again when Weaviate is
  available. The cause is logged, not returned.
- **503 `SERVICE_UNAVAILABLE`** from any of the three when Weaviate can't be read
  to check that the collection exists (#349). No job is created. The cause is
  logged, not returned.
- **`SOURCES_REQUIRED`** (through the job) when the operation needs originals the
  collection does not have. Re-embedding a `chunks-only` collection *with*
  chunking fields is refused rather than half-honoured. Re-chunking is also
  refused when a stored source file has no single retained original (a name
  uploaded more than once, or a second name for the same content) or a
  retained original is missing from disk.
- Re-chunking rebuilds only the names that have stored chunks (#311). A name the
  retained index lists without stored chunks (for example a file that produced no
  chunks at ingest) is skipped, not brought back; the index keeps it.

Each rebuild is staged into a temporary collection and verified before the
original is deleted, so a failure before replacement leaves the original
untouched. Weaviate has no atomic swap: once replacement starts, a failure can
leave the original name missing or partial. A verified recovery copy and its
sidecars are then kept across restarts and named in the job error
(`RAG_EXPORT_SPECIFICATIONS.md` §7.4). Vectors are copied out of
the staging collection rather than regenerated, so the corpus is embedded once.

Any operation that changes chunk identity marks the collection's gold-standard
sessions `stale`. `/tune/reindex` does not, because chunk identity is unchanged.

---

```
GET /tune/{collection}
```

What this collection can be tuned with, given its fidelity. `can_rechunk` is
`false` when re-chunking would be refused: the collection is `chunks-only`, no
retained original has stored chunks (#328), a stored source file has no single
retained original, or a retained original is missing from disk. `note` says
which; when no retained original has stored chunks it says so, whatever else
holds.

- **503 `SERVICE_UNAVAILABLE`** when Weaviate can't be read for the options
  (#349). The cause is logged, not returned.

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
      "readable": true,
      "encrypted": false
    }
  ]
}
```

Encrypted packages (`ragpkg-*.tar.gz.age`, #27) are listed too, with
`encrypted: true` and `collection`, `chunk_count`, `fidelity` and `created_at`
null: their manifest can't be read without the passphrase, and no plaintext
metadata is kept beside them. Their `readable` says whether the file starts with a
valid age passphrase header; the listing never needs the passphrase and never runs
scrypt. A file that can't be read or parsed is listed `readable: false` and never
fails the listing of the others.

A file that cannot be read as a package is still listed, with `readable: false`
and null metadata, so it does not silently disappear from the UI. Import reports
precisely why it cannot be used.

The listing reads each archive only up to its top-level `manifest.json`
(`<directory>/manifest.json`) and nothing after it, so a large package is not
decompressed to list it (#383). New packages put the manifest first
(`RAG_EXPORT_SPECIFICATIONS.md` §4.2); in older ones the files before it are read
past, which is slower but works. The manifest read is bounded at 16 MiB: a larger
manifest is not read and its package is listed `readable: false`, as is one whose
manifest is missing, damaged, not UTF-8, not valid JSON or not a JSON object.

The listed values are typed as in the manifest (`RAG_EXPORT_SPECIFICATIONS.md`
§4.4): `collection` an object, its `name` a string and its `chunk_count` an
integer, `fidelity` and `created_at` strings. A value that is absent or `null`
(`collection` included) is listed as `null`. A value of any other JSON type is not
coerced (`"12"`, `12.0` and `true` are not integers): the package is listed
`readable: false` with null metadata, and the listing still answers 200 with the
other packages (#451).

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
| `source_type` | `text` | filterable only (BM25 disabled) | `pdf`, `docx`, `txt`, `md`, `csv`, `json`, `jsonl` |
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
| JSON | `.json` | Own converter; Unstructured (`partition_json`) for element files | Converted to readable `key: value` lines, keys in the file's order, non-ASCII text as is, strings unquoted, numbers as written, `true`/`false`/`null` as is. Nested keys are dotted paths (`meta.author: A`); an array of scalars is one line (`tags: a, b, c`); other arrays use indexed paths (`items[0].name: x`). A top-level array holding any object or array gives one element per item (scalar items included), separated by a blank line, so `language` and `context_aware` chunking split between records. Empty objects and arrays write nothing: `{}`, `[]` or an empty file completes with 0 chunks. Invalid JSON (including `NaN`/`Infinity` and non-UTF-8 text; a UTF-8 byte-order mark is accepted) fails that file with "is not valid JSON: <reason>". Because every line repeats its key path, the converted text is bounded: a file whose records, with the blank lines between them, would hold more than 10,000,000 characters (`MAX_TEXT_CHARACTERS`, the chunker's per-file limit for every strategy) fails with "converts to more than 10,000,000 characters of text …" before that text is built. A file keeps at most 1,000,000 records (records with no text don't count); more fails with "has more than 1,000,000 records …", before any element is built (#300). An element file's items count the same way, every item included, and are counted before `partition_json` builds them (#321). A file is parsed whole, so a `.json` file over 30,000,000 bytes (`MAX_JSON_INPUT_BYTES`, an element file included) fails before it is read with "is larger than 30,000,000 bytes, the most one JSON file can hold; split the file or convert it to JSON Lines", and a `.json.gz` member stops decompressing there ("decompresses to more than 30,000,000 bytes …"); the rest of the job carries on (#300). A non-empty array of serialized Unstructured elements is rehydrated by `partition_json` (#295) |
| JSON Lines | `.jsonl`, `.jsonl.gz` | Own reader and the JSON converter above (#291) | One file is one document. Read one line at a time (lines end at `\n` or `\r\n`, so CRLF and LF files read the same; the last line needs no newline); a `.jsonl.gz` is decompressed as it is read, at most 512 MB. Each non-blank line is one record: its JSON value (any value, not only an object) converted by the JSON rules above from the root, so an array line is one record with indexed paths (`[0].a: 1`). Records are separated by a blank line and each is one element, so `language` and `context_aware` chunking split between lines; lines are never treated as Unstructured element files. A line with no text (`{}`, `[]`, `""`) adds nothing; an empty or blank-only file completes with 0 chunks. A UTF-8 byte-order mark on line 1 is accepted. The file fails as a whole, with nothing stored, at its first bad line: a blank line followed by another record (`line N is blank`; blank lines after the last record are accepted), invalid JSON (`line N is not valid JSON: <reason> at column C`), non-UTF-8 text, or a line over 10,000,000 bytes, not counting its line ending or line 1's byte-order mark (`line N is longer than 10,000,000 bytes …`; at most that much of one line is read, so one record's parse stays bounded; #314). A file holds at most 1,000,000 lines, the JSON record cap: every line counts, blank lines and lines with no text included, and reading stops at line 1,000,001, which fails the file (`has more than 1,000,000 records, the most one file can hold; split the file`; #314). The whole file's text, blank lines between records included, is bounded by the same 10,000,000 characters as JSON: reading stops at the line that passes it (`converts to more than 10,000,000 characters of text by line N …`). Larger corpora are #312. `source_type` is `jsonl` |

For ZIP uploads: extract to temp directory, process all files with supported extensions, skip unsupported files (logged as warnings) and macOS metadata (`__MACOSX/` members and `._*` AppleDouble files, listed in `skipped`, #378). `.json.gz` members are also extracted; each is decompressed (at most 30,000,000 bytes, #300) and parsed as JSON, with per-file errors for a corrupt gzip, an over-limit size or unparseable JSON. `.jsonl` and `.jsonl.gz` members are extracted and read as JSON Lines (#291), like direct uploads. Every member is validated first; a damaged or unreadable archive refuses the whole upload with a 400 (§3.1.3). An upload's ZIPs may expand to at most `max_zip_expanded_bytes` (2 GiB) in all, gzipped members' output included; over it, the upload is refused with `ARCHIVE_TOO_LARGE` (§3.1.3, #298). One ZIP may hold at most `max_zip_members` (5,000) entries, and a member of 1 MiB or more may expand to at most `max_zip_member_ratio` (200) times its compressed size, or the upload is refused with `ARCHIVE_TOO_MANY_MEMBERS` or `ARCHIVE_RATIO_TOO_HIGH`. Members whose path leaves the archive are validated and counted but never extracted, and are listed in `skipped` (§3.1.3, #28). This work runs in a worker thread, off the event loop.

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

Inserts never create a collection. Weaviate's auto-schema is off
(`AUTOSCHEMA_ENABLED: 'false'`, §2.1): with it on, an insert sent after its
collection was deleted recreated the collection with inferred properties, no
vectorizer and no registry entry (issue #40). Every collection is created explicitly
with the §4.1 properties, so an insert into a missing collection, or one carrying a
property the collection does not declare, fails instead. When storing a file's chunks
fails and the job's collection no longer exists, the ingest job stops: it ends
`failed` and adds the error `collection '<name>' was deleted while this job was
running`; that file and every later one count as failed, files already stored stay
counted, and no later file is parsed or retained. The underlying batch error goes to
the API log. Only a failed store checks for the collection; other failures are
reported per file as before. The check also runs when the job's first backend call for a
file, asking whether its name already has chunks (#22, §3.1.3), fails.

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
You are a helpful assistant. Answer the user's question using only the provided context. If the context does not contain enough information to answer the question, say so clearly. Do not use any knowledge outside the provided context. Write in plain, clear language for a non-technical reader. Keep the answer short: a few sentences, without technical detail.
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

Each chunk gets at most three attempts. A reply is usable when it holds a JSON object whose `question` and `answer` are nonempty strings and whose `ground_truth` is a nonempty string or left out; a left-out `ground_truth` defaults to the validated `answer`. A missing `question` or `answer`, or a null, list, object, number, boolean or blank value in any of the three, makes the reply unusable, and such a value is never stored as its Python representation. After an unusable reply the API asks again with the same user message, up to two more times, adding one of these texts to the end of the system prompt. Each starts with a space.

After a reply with no JSON object:

```
 Your previous response was not valid JSON. Return ONLY the JSON object with keys: question, answer, ground_truth. No markdown, no explanation.
```

After a JSON object with an unusable field:

```
 Your previous response did not give question, answer and ground_truth as nonempty strings. Return ONLY the JSON object with keys: question, answer, ground_truth, each a nonempty string. No markdown, no explanation.
```

After a third unusable reply the pair fails; §3.1.5 describes how failures are reported. A chunk whose source filename is null gives a pair with an empty `source_file`.

---

## 7. Web UI Specification

**Framework:** React 18 + Vite 5  
**Component library:** shadcn/ui + Tailwind CSS  
**State management:** React Context + useReducer (no external state library)  
**Routing:** React Router v6

### 7.1 Login (#25)

The role picker is gone. The UI takes the user and role only from `GET /api/auth/me` and keeps nothing about
them in browser storage; the session is the HttpOnly cookie. On load it asks `GET /api/auth/status`:

| State | Shown |
|---|---|
| Setup required | "Not set up yet" with the command `docker compose exec api python -m auth bootstrap <username>`, and nothing else |
| No valid session | The login page at `/login` (any other path redirects there): username, password, "Log in". A failure shows the API's message (the generic `LOGIN_FAILED`, or "wait" when throttled) and clears the password. Success opens `/qa` |
| Password change due | Only the password page at `/account/password`: current password, new password, the new one again, "Change password" and "Log out". A mismatch is caught before sending; the API's rule message is shown; success opens `/qa` |
| Logged in | The pages of the user's role (§7.2) and `/account/password`, where a change says the other sessions were logged out |

Any API answer `401 AUTH_REQUIRED` returns the UI to the login page, and `403 PASSWORD_CHANGE_REQUIRED` to the
password page.

| Role | Label |
|---|---|
| `engineer` | AI Engineer |
| `developer` | Developer |
| `end_user` | End User |

### 7.2 Navigation Bar

Visible on all pages after login. Contents vary by role; hiding a page is a convenience, and the API refuses
what the role can't do (§3.1.8):

| Nav Item | Engineer | Developer | End User |
|---|---|---|---|
| Q&A | ✓ | ✓ | ✓ |
| Import Documents | ✓ | ✓ | — |
| Chunking Config | ✓ | ✓ | — |
| Retrieval Config | ✓ | ✓ | — |
| Gold Standard | ✓ | ✓ | — |
| Transfer | ✓ | ✓ | — |
| Collections | ✓ | — | — |
| Health | ✓ | — | — |
| Users | ✓ | — | — |
| Service keys | ✓ | — | — |
| Audit | ✓ | — | — |
| Username and role badge | ✓ | ✓ | ✓ |
| Change password (button) | ✓ | ✓ | ✓ |
| Log out (button) | ✓ | ✓ | ✓ |

### 7.3 Q&A Page

**Route:** `/qa`  
**Roles:** All

**Elements:**

- Collection selector dropdown (populated from `GET /collections`). Defaults to first available collection. If the collection list can't be loaded, the page says so as an alert, "Could not load the collections:" followed by the reason, instead of showing an empty selector (#379).
- Question text input (multi-line, submit via "Ask" button or Ctrl+Enter; plain Enter inserts a newline).
- Citations toggle (checkbox, off by default, labeled "Show source citations").
- Answer display area (markdown-rendered).
- Citation list (shown below answer when toggle is on): source file, chunk index, relevance score (higher is better, 3 decimals; `n/a` when the API returns `null`, #376), excerpt.
- Latency display (small, below answer): `Retrieved in Xms · Generated in Xms`.
- Retrieval mode selector — visible to Engineer and Developer roles only. Hidden for End User. Read-only: it displays the mode saved for the selected collection, which is changed on the Retrieval Config page. The settings are fetched from `GET /retrieval/config/{collection}` whenever the selected collection changes; if nothing is saved for that collection the API returns the defaults (`hnsw`, `top_k: 5`).

The collection selection is shared with the Retrieval Config page, because retrieval settings are stored per collection and must follow the selection.

Loads and saves belong to the collection selection generation that started
them. Changing the selection (including leaving and returning to the same
collection) invalidates pending responses for the prior selection. A stale
save may finish persisting its original collection, but must not change the
active settings, loading/error state, or invalidate the new collection's load.
Within the current generation, the latest initiated save may publish. If it
fails, the newest acknowledged success may publish, even if its response arrives
after that failure. Each acknowledged save publishes at most once, so a later
older completion cannot erase new edits by republishing the same result. Published
saves supersede pending older loads. Superseded responses show neither Saved! nor
an error, and an earlier notice timer cannot clear a newer Saved! notice.
After the selected collection's load resolves, the next Q&A request uses its
settings, regardless of when a prior collection's save completes.

Each question's answer belongs to the query that produced it (#511). Ctrl+Enter
is ignored while a question is being answered, like the disabled Ask button.
Asking a question or changing the collection starts a new query generation, and
only the current generation's query may show its answer, citations, latency or
error, or end the "Thinking…" state. Changing the collection (including leaving
and returning to the same collection) clears the answer, citations, latency and
error on screen, ends the wait so the new collection can be asked at once, and
drops the response of any query still in flight; that query is not cancelled.
The question and the citations toggle are kept.

**Response format** sent to API:
- End User role → `response_format: "end_user"`
- Engineer and Developer → `response_format: "engineer"`

### 7.4 Import Documents Page

**Route:** `/import`  
**Roles:** Engineer, Developer

**Elements:**

- Drag-and-drop file upload zone. Accepts: `.pdf`, `.docx`, `.txt`, `.md`, `.csv`, `.json`, `.zip`.
- File list showing queued files with remove buttons.
- Collection selector (populated from `GET /collections`). Includes a "Create new collection" option that opens a modal with a name field and index type selector (defaults: HNSW, cosine). On confirm, calls `POST /collections` before submitting ingest. A refused create keeps the modal open and shows the API's message inside it, as an alert; the message clears on Cancel, when the modal is opened again, or when a create succeeds (#379). If the collection list can't be loaded, the page says so as an alert, "Could not load the collections:" followed by the reason, instead of showing an empty selector (#379). A create that succeeds closes the modal even if the list reload after it fails; that failure is shown as a failed list. After a create, the selector selects the new collection under the name the API answered with, which is the name the list shows (`myDocs` selects `MyDocs`, #372).
- Chunking strategy selector (dropdown). On selection, displays the plain-language explanation for the chosen strategy.
- Strategy parameters form (shown below strategy selector):
  - Engineer and Developer both see: chunk size slider, overlap slider (hidden for `semantic` and `context_aware`), minimum chunk size.
  - Engineer additionally sees: similarity threshold slider (visible only when `semantic` is selected).
- "Start Ingest" button → calls `POST /ingest/upload`, displays job ID. The line "Job ID: <job_id>" appears as soon as the upload is accepted and stays while the job runs and after it ends, until a later upload is accepted. Until the first successful poll, a panel in the progress panel's place shows the status the upload was accepted with (`queued`) and "Waiting for the first progress update". With files chosen and no collection selected (none exist, or the list failed to load), the button sends nothing and the page says "Choose a collection to import into first. Use + New to create one." (#394).
- Progress panel: polls `GET /ingest/job/{job_id}` every 3 seconds. Shows files completed/total, chunks stored, errors, "Submitted N · accepted N · stored N" when `chunks_submitted` and `chunks_accepted` are numbers and the three counts are not all equal (#302), "Replaced N chunks from a previous version" when `chunks_replaced` is above 0, and the skipped list under "N files skipped:" (each entry gives its reason).

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
- "Save as Default" button → calls `POST /ingest/config` with the collection in the request body (upsert — creates on first save, overwrites on subsequent saves). Does not re-process existing documents. Shows a confirmation notice after successful save. Loads and save results belong to their selection generation, including A→B→A. Changing collection clears old settings and notices; save stays unavailable until the selected configuration loads successfully, and duplicate saves are prevented.

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
| Vector — existing index | Finds similar chunks using the collection's existing physical index. Selecting this query method does not switch HNSW/Flat. |
| Hybrid | Combines keyword search with meaning-based search. Best when your questions include specific terms, names, or codes. Adjust the slider to balance between the two modes. |
| Semantic | Pure meaning-based search. Best for conceptual questions where the exact words are less important than the idea. |

- Top-K slider (1–50, default 5).
- Hybrid alpha slider (visible only when Hybrid selected, range 0.0–1.0, default 0.75, labeled "Keyword ← Balance → Meaning").
- Read-only physical index panel shows observed index type, distance metric and actual HNSW ef/efConstruction/maxConnections. A refresh button reads current backend settings; details are labeled as last refreshed. Missing details and refresh failures are explicit. No build/query-ef sliders appear.
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
`ef` is always sent as `null` from this form; a prior saved override is shown as inactive and cleared on save. `alpha` is always
sent and is only applied by the server for `"hybrid"`. `response_format` records
the active role at the time of saving (End User → `end_user`, Engineer and
Developer → `engineer`) so an exported retrieval script reproduces the same
answer style.

`efConstruction` and `maxConnections` are physical build settings; `ef` belongs to the physical HNSW index as well. The pinned query API does not accept a per-request ef override. Query settings do not alter the physical index. The legacy API modes `hnsw` and `flat` both use near_vector against the existing index; the UI displays a single Vector method and normalizes a saved flat alias when saving. Exact search depends on the observed physical Flat index, not a query-mode name.

### 7.7 Gold Standard Page

A retained session can be loaded/refreshed by its session ID, including when its collection no longer exists. Its collection and ID remain visible. Stale/orphaned sessions show reasons and recorded timestamps above review/export; missing legacy metadata has clear defaults. Loading or receiving new validity metadata resets the historical-export checkbox. A new lookup clears the previous session and export controls before fetching, including when the lookup fails. Export shows a disabled progress state while its request is pending, and duplicate clicks cannot start another request.

**Route:** `/goldstandard`  
**Roles:** Engineer, Developer

**Phase 1 — Generate:**
- Collection selector. If the collection list can't be loaded, the page says so as an alert, "Could not load the collections:" followed by the reason, instead of showing an empty selector (#379).
- Sample size input (1–100, default 20). While the value is empty, not a whole number, below 1 or above 100, "Generate Pairs" is disabled and the page says "Sample size must be a whole number from 1 to 100."; no request is sent (#393).
- "Generate Pairs" button → calls `POST /goldstandard/generate`, receives `session_id`. Shows progress bar during generation.
- "Generate Pairs" is disabled while its start request is in flight, while a generation runs and between the start being accepted and the first poll answer (so a second click cannot start another session or skip the confirmation below); a retained session that is still `generating` disables it too (#380).
- Generation progress: polls `GET /goldstandard/session/{session_id}` every 2 seconds. Shows `pairs_completed / pairs_total`. Pairs appear in the review list as they complete (partial display during generation).
- If a poll fails, polling stops and the page says so as an alert: "Could not read the generation's progress: " followed by the reason, then "Polling stopped; use Load / refresh session to check again." The progress is no longer shown and Generate is enabled again. "Load / refresh session" clears the message and polls again while the session is `generating` (#380).
- When the session is no longer `generating`, the page shows its outcome under the controls (#380), for a session generated here and for a retained session loaded by ID. `completed`: "Generation completed: C of T pairs generated." and, when `pairs_failed` is above 0, "F pairs failed and are missing from the review list." (a warning). `completed` with `pairs_total` 0: "No pairs were generated: the collection has no chunks." `failed`: "Generation failed." and "F of T pairs failed." when `pairs_failed` is above 0 (an error). `cancelled`: "Generation was cancelled." Any other status: "Generation ended with status S." Under the headline the `errors` reasons are listed once each, with "(xN)" when a reason repeats, at most five, then "and K more reasons". Warnings and errors are alerts.
- If a session already exists in the UI (from a previous generation), clicking "Generate Pairs" displays a confirmation dialog: "This will start a new session and replace the current one. Any unsaved pairs will be lost. Continue?" Previous sessions remain on the server (accessible via direct session ID) but are no longer referenced by the UI after confirmation.

**Phase 2 — Review:**
- List of generated pairs, one expandable row per pair (#393). The collapsed row shows the question, the source file, the status and the actions below.
- Expanding a row shows the answer, the ground truth and a preview of the first context chunk: its first 300 characters, followed by `…`.
- Actions per row:
  - Approve (green check) → calls `PATCH /goldstandard/session/{session_id}/pair/{pair_id}` with `{ "status": "approved" }`
  - Edit inline (pencil) → opens edit form with three editable fields: question, answer, ground_truth; on save calls `PATCH` with `{ "status": "edited", "question": "...", "answer": "...", "ground_truth": "..." }` (only changed fields included; with no change, only the status). If the `PATCH` is refused (for example a cleared field, `422 INVALID_PARAMETER`), the form stays open with the typed values and shows the API's message inside it as an alert; no page-level error is set (#380).
  - Reject (red X) → calls `PATCH` with `{ "status": "rejected" }`
  - Regenerate (refresh icon) → calls `POST /goldstandard/regenerate` with `session_id` and `pair_id`, replaces row with returned pair. While that request runs (up to 1800 s) the pair's button is disabled with the title "Regenerating..." and the other pairs' buttons stay enabled (#380).
- Summary bar: `X approved · Y edited · Z rejected · W pending`

**Phase 3 — Export:**
- "Export Approved" button (disabled until at least 1 approved or edited pair exists).
- Historical sessions use "Export Historical Approved" and additionally require an explicit checkbox acknowledging that the file omits validity warnings and is not a current baseline. The backend independently enforces the choice.
- Filename field: pre-filled with the API's default name, `{collection}_{YYYYMMDD_HHMMSS}.json` (UTC, with the collection part made safe and shortened as the `POST /goldstandard/save` default does), when a generation starts and when a retained session is loaded; editable (#393).
- Calls `POST /goldstandard/save` → on success, opens `GET /goldstandard/download/{filename}` in a new tab, with the returned filename percent-encoded as one path segment, to download the resulting JSON file (#393). Browsers allow that tab only shortly after the click: with Chromium's popup blocker on, it opens when the save answers within about 5 seconds and is blocked after that.
- Export summary shown: `17 pairs exported, 3 excluded (rejected/pending)`.

### 7.8 Collections Page

**Route:** `/collections`  
**Roles:** Engineer only

- Table of collections: name, object count, index type, distance metric, created date. If the list can't be loaded, the page says so as an alert, "Could not load the collections:" followed by the reason, and keeps any rows already shown; the next successful load (after a create or delete) clears it (#379).
- "New Collection" button → modal with fields: name, index type, distance metric, HNSW params.
- A refused create (for example `409 COLLECTION_EXISTS`, a 422 or a 503) keeps the modal open and shows the API's message inside it, as an alert. The message clears on Cancel, when the modal is opened again, or when a create succeeds (#379).
- Delete button per row → confirmation modal with collection name typed to confirm. Calls `DELETE /collections/{name}?confirm=true`.
- A refused delete keeps the modal open and shows the API's message inside it, as an alert: for `409 COLLECTION_BUSY`, for example, "An ingest job is running for this collection (…)". The collection stays listed; the message clears on Cancel, when the modal is opened again, or when a delete succeeds.

### 7.9 Health Dashboard Page

**Route:** `/health`  
**Roles:** Engineer only

- Service status cards: Weaviate, Ollama LLM, Ollama Embed. Each shows status (green/red), latency (ms). A service is red when its `status` isn't `ok`. `GET /health` answers 503 with the same per-service body when a dependency is down; the page reads that body, so the failing services show red (#375).
- When health can't be read (no answer from the API, a proxy error page, any other error without a health body, or a 200 without a health body (#415)), the page shows "Service health could not be read:" with the reason, as an alert, and no service cards, so an earlier green result never stays up. The next successful load clears it.
- Latency trend charts (from the `history` field of `GET /metrics/latency`, bounded by `history_limit`, default 100): line chart of `total_ms` over time alongside `retrieval_ms` and `llm_ms`. Summary stat row shows P50/P95/P99 for each component from the `retrieval_latency`, `llm_latency` and `total_latency` objects.
- Health and latency metrics load separately: a degraded or failed health call doesn't stop the charts from loading. A failed metrics call keeps the charts last shown.
- A slow response from an older refresh never replaces a newer one's: a health result (cards or message) is shown only if no later refresh's health result is already shown, and the same for latency metrics (#415). A failed metrics call changes nothing, so a late metrics result from an older refresh is still shown when no later one has been.
- Auto-refreshes every 30 seconds.

---

### 7.10 Transfer Page

**Route:** `/transfer`  
**Roles:** AI Engineer, Developer

**Export elements:**

- Collection selector (from `GET /collections`).
- **Fidelity shown before the export runs**, from `GET /tune/{collection}`:
  `with-sources` with the document count, or `chunks-only` with what that costs.
  The choice is informed rather than discovered afterwards in the manifest. The
  line is never hidden: it reads "Fidelity: reading…" while the lookup runs, and
  "Fidelity: unknown — it could not be read:" with the reason when the lookup
  fails (409 `SOURCE_INDEX_INVALID`, 503, 404 or a proxy error). It always belongs
  to the selected collection; a late answer for another one is ignored.
- "Include the models" checkbox, labelled with the ~2.3 GB cost and when it is
  needed.
- "Encrypt with a passphrase" checkbox (#27). Checked, it shows a passphrase field
  and a confirmation field; Export stays disabled, with a hint naming the unmet
  rule, until the passphrase has 12 to 1024 characters and both fields match. The
  passphrase is sent only when the box is checked, is kept only in the page's
  state, and both fields are cleared once the export has started.
- Progress while the job runs, then the filename, size, fidelity, whether it is
  encrypted, whether models were bundled and whether `retrieve.py` was generated. Warnings from the job are
  shown in full. A failed export shows its `error_code` (for example
  `EMBEDDING_MISMATCH`) before the error, as the import half does.

**Import elements:**

- Package selector listing `GET /packages`, each entry showing the collection,
  chunk count, fidelity and size, or "encrypted" and the size for an encrypted
  package. Unreadable files are listed too, marked as such, so nothing silently
  disappears.
- A passphrase field when the selected package is encrypted (#27); Import stays
  disabled until it is filled. The passphrase is sent only for an encrypted
  package and the field is cleared once the import has started. A failed `GET /packages` shows "Could not
  list the packages in ./exports:" with the reason, instead of the empty-folder
  hint; the last list loaded stays, and the next good load clears the message.
- `on_conflict` radio group — `abort`, `rename`, `replace` — each with its
  consequence spelled out. There is no default in the API, and the page makes the
  difference explicit because the wrong choice can delete a collection.
- On completion: the name it was imported under, whether it was renamed, and
  every note the job returned (models installed or skipped, sessions orphaned,
  sessions restored).

Both halves poll their job endpoint every 2s and stop polling when the job
finishes or the page unmounts; a poll in flight at unmount, or a start answered
after it, schedules nothing. A failed poll is shown ("Could not check the
export's progress, retrying:" with the reason) and retried at the same interval,
with the button still busy; the next good poll clears the message. After 30
failed polls in a row (about a minute, enough for an API restart) the half stops
watching the job, says so with the last error and that the job may still be
running, and enables its button again, so the page never stays stuck until a
reload (#382).

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

### 7.12 Users Page (#25)

**Route:** `/users`  
**Roles:** Engineer only (others are sent to `/qa`)

- A table of every account from `GET /auth/users`: username (the engineer's own marked "(you)"), a role selector,
  Enabled or Disabled, the password state ("Change due" or "Set"), and the creation date. If the list can't be
  loaded the page says "Could not load the users:" with the reason, as an alert.
- A form with username, role (End User by default) and "Create user". The username rule is shown under it.
- Each row: changing the role selector sends the role change; "Disable" or "Enable"; and "Reset password", except
  on the engineer's own row, which says to use Change password instead.
- After a create or a reset, a panel shows the username and the generated password, says it is shown only once and
  must be changed at first login, and has "Done", which removes it. The password is kept only in the page's memory.
- A refused change shows the API's message as an alert and reloads the list. After a change to the engineer's own
  account the UI re-reads `GET /auth/me`: a demoted engineer leaves the page, a disabled one is logged out.

### 7.13 Service Keys Page (#25)

**Route:** `/service-keys`  
**Roles:** Engineer only (others are sent to `/qa`)

- A short explanation: a principal is an account for a program that can't log in; its keys are sent as
  `Authorization: Bearer <key>` and can do what its role allows, except manage accounts or keys; rotating is
  issuing a new key, switching the service over, then revoking the old one.
- A form with name, role (End User by default) and "Create principal". The name rule is shown under it.
- Each principal from `GET /auth/service-principals` in its own section: its name, a role selector that sends the
  role change, Enabled or Disabled, and "Disable" or "Enable"; a form to issue a key (label, optional; lifetime in
  days, 90 by default, 1-365; "Issue key"); and a table of its keys: id, label, created, expires, last used
  ("never" when empty), status (Active, Expired, Revoked), and "Revoke" on active keys only.
- After issuance a panel shows the principal's name and the key, says it is shown only once and is sent as
  `Authorization: Bearer <key>`, and has "Done", which removes it. The key is kept only in the page's memory.
- A refused change shows the API's message as an alert and reloads the list; a list that can't be loaded says
  "Could not load the service principals:" with the reason.

### 7.14 Audit Page (#29)

**Route:** `/audit`  
**Roles:** Engineer only (others are sent to `/qa`)

- A short explanation of what is recorded and that passwords, keys, passphrases and answers never are, and whether
  question text is being recorded (`question_text_logged`). A warning when `failed_writes` is above zero.
- Filters: From and To (date and time, UTC, sent with `Z`), Actor, Action (Any or one of §3.1.9's actions),
  Collection, Result (Any or one of the results); "Apply".
- A table of `GET /audit`'s records, 100 a page, newest first: time (UTC), actor (with the key id for a service
  key, or the kind when it isn't a user), role, address, action, collection, target, result with status code, job,
  details (JSON). "Older" loads the page `next_before` names; "Newest" returns to the first. While a page loads the
  page says "Loading…" and "Older" is disabled, so a double click asks for the older page once.
- "Download CSV" links to `/api/audit.csv` with the applied filters.
- An API error shows its message.

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
| `ef` | 64 | 16 | 512 | Physical HNSW setting; saved query override inactive |
| `efConstruction` | 128 | 64 | 512 | HNSW build param |
| `maxConnections` | 64 | 16 | 128 | HNSW build param |
| Gold standard `sample_size` | 20 | 1 | 100 | |
| `max_zip_expanded_bytes` | 2147483648 | — | — | API setting (env `MAX_ZIP_EXPANDED_BYTES`), not a request parameter: the most one upload's ZIPs may expand to in all, gzipped members' output included (§3.1.3, #298) |
| `max_zip_members` | 5000 | — | — | API setting (env `MAX_ZIP_MEMBERS`), not a request parameter: the most entries one ZIP may hold, folders included (§3.1.3, #28) |
| `max_zip_member_ratio` | 200 | — | — | API setting (env `MAX_ZIP_MEMBER_RATIO`), not a request parameter: the most a ZIP member of 1 MiB or more may expand relative to its compressed size, declared and as read (§3.1.3, #28) |

Optional telemetry (#282) is configured by API container environment settings, not request parameters. They are read only when `RAG_OTEL_ENABLED` is `true`; an invalid value fails API startup with an error naming the field only. See "Optional telemetry foundation (#282)" below and README "Optional backend telemetry".

| Setting | Default | Accepted values |
|---|---|---|
| `RAG_OTEL_ENABLED` | `false` | Exact `true` or `false` |
| `RAG_OTEL_ENDPOINT` | none (required when enabled) | HTTP(S) origin, e.g. `http://collector:4318`; no userinfo, path, query or fragment |
| `RAG_OTEL_PROTOCOL` | `http/protobuf` | `http/protobuf` only |
| `RAG_OTEL_HEADERS_FILE` | none | Mounted JSON regular file with `Authorization` and/or `X-Api-Key`; at most 8 KiB, 2 KiB per value; requires an HTTPS endpoint |
| `RAG_OTEL_CA_FILE` | none (bundled roots) | Mounted PEM CA bundle (a regular file); at most 1 MiB; must load as a CA bundle; replaces the bundled roots; requires an HTTPS endpoint (#356) |
| `RAG_OTEL_SERVICE_NAME` | `rag-api` | 1–64 ASCII letters, digits, `.`, `_` or `-`, starting with a letter or digit |
| `RAG_OTEL_SERVICE_VERSION` | `1.1.0` | As `RAG_OTEL_SERVICE_NAME` |
| `RAG_OTEL_ENVIRONMENT` | `development` | As `RAG_OTEL_SERVICE_NAME` |
| `RAG_OTEL_TRACES`, `RAG_OTEL_LOGS`, `RAG_OTEL_METRICS` | `true` | Exact `true` or `false` |
| `RAG_OTEL_SAMPLE_RATIO` | `1.0` | Finite 0–1 |
| `RAG_OTEL_QUEUE_SIZE` | `256` | Integer 1–4096 |
| `RAG_OTEL_BATCH_SIZE` | `64` | Integer 1–512, no larger than `RAG_OTEL_QUEUE_SIZE` |
| `RAG_OTEL_TIMEOUT_MS` | `1000` | Integer 100–10000 |
| `RAG_OTEL_INTERVAL_MS` | `5000` | Integer 1000–60000 |
| `RAG_OTEL_SHUTDOWN_MS` | `3000` | Integer 100–30000 |

Authentication (#25) has two API container settings:

| Setting | Default | Accepted values |
|---|---|---|
| `AUTH_COOKIE_SECURE` | `false` | Exactly `true` or `false`; anything else fails API startup. `true` marks the session cookie `Secure`, for a stack served over TLS (LAN mode sets it, #26); leave `false` on the loopback HTTP profile |
| `AUTH_DIR` | `/app/auth_data` | The folder of `auth.db`, the `auth_data` volume |

LAN mode (#26, §2.5) adds one API container setting and two read by `docker-compose.lan.yml` from the shell or
`.env`:

| Setting | Default | Accepted values |
|---|---|---|
| `RAG_LAN_MODE` | `false` | Exactly `true` or `false`; anything else fails API startup. `true` only through `docker-compose.lan.yml`; then the API refuses to start unless `AUTH_COOKIE_SECURE` is `true` and an enabled human AI Engineer exists |
| `RAG_LAN_HOSTNAMES` | empty | Comma-separated DNS names and IP addresses for the self-signed certificate; required unless `proxy/certs` holds `cert.pem` and `key.pem` |
| `RAG_LAN_PORT` | `443` | The host port of LAN mode's TLS publication, on every interface |

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
├── package.sh                  # Source-only zip (~1.3 MB; target needs internet)
├── package-offline.sh          # Self-contained bundle (~4.6 GB; no network needed)
├── install-offline.sh          # Installs from that bundle on an air-gapped Mac
├── docker-compose.lan.yml      # LAN mode, opt-in (#26)
├── proxy/
│   ├── nginx.conf              # Default server: HTTP, container port 80
│   ├── nginx.lan.conf          # LAN mode: TLS, container port 443
│   ├── locations.conf          # The routes, included by both
│   └── certs/                  # Bring-your-own LAN certificate (ignored by git but .gitkeep)
├── ollama/
│   └── entrypoint.sh            # Starts server, pulls models, waits
├── api/
│   ├── Dockerfile
│   ├── .dockerignore
│   ├── requirements.in         # Direct dependencies — edit this one
│   ├── requirements.txt        # Generated lock (pinned); installed by the build
│   ├── main.py                  # FastAPI app entry point
│   ├── utils.py                 # Shared helpers (api_error response factory)
│   ├── auth/                    # Accounts, sessions, service keys, the role matrix (#25)
│   │   ├── __main__.py          # The operator command, python -m auth
│   │   ├── passwords.py         # Username/password rules, scrypt hashing
│   │   ├── keys.py              # Service API key format (rag_<id>_<secret>)
│   │   ├── store.py             # SQLite accounts, principals, keys, sessions, login throttle
│   │   ├── policy.py            # The endpoint/role matrix
│   │   ├── middleware.py        # Enforces it before a route runs
│   │   └── testing.py           # Real sessions for in-process tests
│   ├── routers/
│   │   ├── auth.py              # /auth/status, login, logout, me, password, users, service principals
│   │   ├── audit.py             # /audit and /audit.csv, for Engineers (#29)
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
│   │   ├── settings_store.py    # Shared serialized settings publication
│   │   ├── packager.py          # Package format: naming, digests, manifest, reader
│   │   ├── age_stream.py        # Streaming age passphrase encryption of packages (#27)
│   │   ├── exporter.py          # Export job lifecycle and the per-collection guard
│   │   ├── jobstore.py          # Job records that survive an API restart
│   │   ├── audit.py             # The audit log: records, rotation, reading, the request middleware (#29)
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
│   ├── lan_mode.py              # LAN mode's startup refusal (#26)
│   ├── tls_cert.py              # LAN mode's certificate step, `python -m tls_cert` (#26)
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
│   │   │   ├── AuthContext.tsx      # The user and role, from GET /api/auth/me
│   │   │   └── QueryConfigContext.tsx
│   │   ├── pages/
│   │   │   ├── LoginPage.tsx
│   │   │   ├── SetupRequiredPage.tsx
│   │   │   ├── ChangePasswordPage.tsx
│   │   │   ├── QAPage.tsx
│   │   │   ├── ImportPage.tsx
│   │   │   ├── ChunkingPage.tsx
│   │   │   ├── RetrievalPage.tsx
│   │   │   ├── GoldStandardPage.tsx
│   │   │   ├── TransferPage.tsx
│   │   │   ├── HelpTransferPage.tsx
│   │   │   ├── CollectionsPage.tsx
│   │   │   ├── HealthPage.tsx
│   │   │   ├── UsersPage.tsx          # Engineers manage accounts (#25)
│   │   │   ├── ServiceKeysPage.tsx    # Engineers manage service principals and keys (#25)
│   │   │   └── AuditPage.tsx          # Engineers read the audit log (#29)
│   │   ├── components/
│   │   │   ├── NavBar.tsx
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

- [x] A collection created, ingested into, configured, queried, exported, tuned or used for gold-standard generation under a lowercase-first spelling keeps all its state under the stored name: the 201 answers `MyDocs` for `myDocs`, the list shows its `created_at`, its originals are retained under the stored name so export is with-sources and re-chunk finds them, one settings file serves both spellings, and the metrics filter and the one-export guard treat both spellings as one collection (#372).
      *`test_collection_spelling.py` (run by `02_ingest.sh`) drives each route through the app with a lowercase-first name and checks the name every worker, file and record receives; it fails on the code before #372. `05_transfer.sh`, on the verify stack, creates and ingests under a lowercase-first name, reads the tuning options and exports under the stored name (with-sources), re-chunks under the lowercase name and, with LLM work, checks a query's metrics under both spellings; on the code before #372 the 201, `created_at`, upload answer, tuning-options, with-sources export and metrics checks fail. `ui_criteria.js` checks the Import page selects the created collection as stored.*
- [x] Deleting a collection by either its canonical name or first-character alias removes both spellings' retained sources, configurations and registry entries, durably orphans both spellings' sessions, and preserves case-distinct neighbours and retained recovery copies. Import-replace notes count sessions under both spellings.
      *`test_collection_writes.py` covers both deletion spellings and a case-distinct neighbour; `reindex.py` runs both deletion paths through the HTTP handler against owned backend and sidecar fixtures, with `reindex_verifier_cases.py` checking registration. The live backend rejects case-only neighbouring collections; Linux-volume checks still verify case-distinct sidecar/session preservation, while controlled tests cover both backend collections.*
- [x] Invalid ingest/saved settings are rejected before staging, jobs or configuration writes; valid defaults and fixed size/minimum preferences are retained.
      *`test_settings_validation.py` checks mocked work boundaries and persistence; `07_settings.sh` runs real HTTP rejection, unchanged-config and valid round-trip checks on an owned collection. Full affected ingest verification passes 18 checks.*
- [x] Concurrent ingest/retrieval settings saves publish their own complete values; failed publication preserves the previous valid configuration (#141).
      *`test_settings_persistence.py` controls worker contention and first-save directory creation with events, observes each publication and unique temporary path, and injects serialization, creation, partial-write, close and replacement failures for both services. Suite 07 registers these controlled tests (including failed import publication), host-side `ImplementationTests`, and 15 rounds of 12 concurrent live saves per ingest/retrieval route; each round checks that the persisted config is one complete acknowledged response. Full-stack verification is recorded separately.*
- [x] An import whose `ingest_config.json` is invalid JSON, not an object, or has a wrong type or invalid value fails `PACKAGE_CORRUPT` naming the file, in every conflict mode, before any live change; the existing collection's ingest settings are unchanged. After an import with valid ingest settings, `GET /ingest/config/{collection}` returns them for the imported collection, a `chunk_size`/`min_chunk_size` from before the bounds unchanged (#510).
      *`test_retrieval_import.py` (`IngestImportTests`, run by `05_transfer.sh`) imports digest-valid packages with each refusal in abort, rename and replace mode with every live operation mocked and asserted unused, and reads valid imports back through the GET route. The refusal and numeric-string cases fail on the code before #510.*
- [x] `chunk_size` is bounded to 50–6000 and `min_chunk_size` to 0–6000 wherever chunk settings are saved or used; a saved configuration from before the bounds is still returned and exported unchanged, and must be within them to be saved again or used for tuning (#53).
      *`test_settings_validation.py` saves and reads back both edges, rejects 49, 6001 and a minimum of 6001 without changing the saved configuration, and checks a saved 16000/8000 configuration is returned and exported unclamped but refused by save, rechunk and reembed. `07_settings.sh` checks both edges and their neighbours against the live stack.*

- [x] Single file upload (all six types) completes without error and stores chunks in Weaviate.
      *One file of each type. `.md` failed — `unstructured[pdf,docx,csv]` omitted
      the `md` extra, so Markdown ingestion had never worked. **Fixed**: added the
      extra and `Markdown==3.10.3` to the lock.*
- [x] An ordinary JSON file (flat or nested object, or an array of records) is stored as readable `key: value` text in the file's key order, with non-ASCII text kept; a file of serialized Unstructured elements is still rehydrated (#295).
      *Issue #295 reported a schema error, which came from Unstructured 0.17.2; at
      the resolved 0.27.5 `partition_json` accepted any JSON but stored it
      pretty-printed with sorted keys and escaped non-ASCII text (`"Zo\u00eb"`),
      unlike §5.1. **Fixed**: the API converts ordinary JSON itself (§5.1).
      `test_json_ingest.py` (36 cases in the API image) covers each conversion rule,
      element-schema rehydration, a byte-order mark, empty files and invalid-JSON errors
      for `.json` and `.json.gz`. The PR #299 review found that repeated key paths let a
      512 KB file (a 256 KB key over 65,536 values) expand to about 17 GB of text: the
      converted text is now bounded by the chunker's 10,000,000-character per-file limit,
      which every strategy already enforced, so nothing that could be stored is refused.
      That 524,295-byte file now fails in 0.2 s with a 21.7 MB traced peak, and a file
      converting to 9,999,999 characters still converts and chunks; `02_ingest.sh` ingests a nested `plain.json` through the proxy and
      reads back `meta.author: Zoë Müller` with no JSON syntax. Full verification runs
      2026-10-17: all seven suites passed (9m31s; after the review fixes, 9m36s).
      The PR #299 review's follow-ups (#300): the blank lines between records now count
      toward that limit, a file keeps at most 1,000,000 records, and JSON input is limited to
      30,000,000 bytes before it is parsed (a `.json.gz` member stops decompressing there), each
      failing that file alone with its own message. `test_json_ingest.py` covers each limit at
      and one past its edge, a file of 1,000,001 tiny records failing fast without building
      elements, and the traced peaks of deep keys, an over-long scalar array and an
      element-schema file's parse; `02_ingest.sh` sends one file over each limit beside
      `plain.json` and checks the exact errors.
      The PR #320 review's follow-up (#321): an element file's items count toward the
      1,000,000-record cap, checked before `partition_json` runs; `test_json_ingest.py` covers
      it at and one past the edge, for a `.json.gz` member and in a job that stores nothing
      from the file.*
- [x] A JSON Lines file (`.jsonl` or `.jsonl.gz`, uploaded directly or in a ZIP) is stored one `key: value` record per line with `source_type` `jsonl`; a blank, malformed, non-UTF-8 or over-long line fails the whole file naming that line, and the file's text is bounded by the per-file limit while it is read (#291).
      *`test_jsonl_ingest.py` (39 cases in the API image) covers the conversion of each kind
      of line, CRLF, a byte-order mark, trailing blank lines, every line error and its
      ordering, the 10,000,000-byte line limit at and one past the edge, the text limit at and
      one past the edge, reading that stops at the line passing the limit, gzip errors and
      prefixes, `source_type`, a job storing nothing for a bad file, and rechunk of a retained
      `.jsonl.gz`. `test_ingest_errors.py` checks which inputs are queued. `02_ingest.sh`
      ingests a direct `.jsonl` and `.jsonl.gz` and a `.jsonl.gz` ZIP member through the proxy,
      reads the stored records back, and checks that a malformed line fails the file and
      stores nothing; `06_ui.sh` checks the Import page's file picker and hint.
      The PR #313 review's follow-ups (#314): a file holds at most 1,000,000 lines, every
      line counting, so lines with no text no longer run on to the 512 MB gzip bound; the
      10,000,000-byte line limit no longer counts the line ending or line 1's byte-order mark,
      so a CRLF file reads like an LF one. `test_jsonl_ingest.py` (56 cases) adds the cap at
      and one past its edge, blank and empty lines counting, reading stopping at the line past
      the cap, the line limit at and one past the edge for `\n` and `\r\n` with and without a
      byte-order mark, the `.jsonl.gz` line-limit prefix, and job-level outcomes (empty files
      completing with 0 chunks under every strategy, the 10,000-chunk limit and the cap failing
      the file and storing nothing, `context_aware` one chunk per record); `02_ingest.sh`
      checks that a gzipped file of 1,000,001 `{}` lines fails with the exact message and
      stores nothing.*
- [x] ZIP batch upload extracts and processes all supported files; unsupported files are skipped with a warning.
      *A ZIP of 8 files yielded 6 processed, 2 skipped — but silently, with
      nothing naming what was dropped. **Fixed**: the job now carries a `skipped`
      list naming each file and its unsupported extension, shown by the UI.*
- [x] A damaged or unreadable ZIP refuses the whole upload with a readable 400 naming its unreadable members (`ARCHIVE_UNREADABLE`, or `ARCHIVE_UNSUPPORTED` when none is damaged), a filesystem error is `UPLOAD_WRITE_FAILED` and anything unexpected a JSON 500 `INGEST_FAILED`; `.json.gz` members ingest as JSON. An upload's ZIPs expand to at most `max_zip_expanded_bytes` (2 GiB) in all, gzip output included, or the upload is refused with `ARCHIVE_TOO_LARGE`, and the ZIP work runs off the event loop (#290, #298).
      *#290 replaced a bare HTTP 500 for a truncated ZIP. `test_ingest_errors.py` (in the API
      image) covers a truncated archive, wrong member offsets, bad CRCs (also in a member that
      would be skipped), unsupported compression, an encrypted member, damaged and unsupported
      members together (`ARCHIVE_UNREADABLE` with the generic remedy), extraction and upload-save
      filesystem errors, the JSON 500, `.json.gz` parsing and its errors, and rechunk of a
      retained `.json.gz`. The #297 review found the ZIP work ran on the event loop with no total
      cap, so a small highly compressible ZIP could stall every request: `test_zip_expansion.py`
      runs with a 1 MiB cap set through `MAX_ZIP_EXPANDED_BYTES` and covers declared sizes over
      the cap (refused before any member is read), a total at the cap accepted and one byte
      over refused, the total shared by two ZIPs, counting when declared sizes understate,
      `.json.gz` and `.jsonl.gz` output, concatenated streams with zero padding, a corrupt and a
      truncated gzip, a direct `.jsonl.gz` not counted, the cap passed after an unreadable
      member, no member read and gzip counting stopped within one block once the cap is passed,
      the 2 GiB default and the ZIP work running in a worker thread while the loop serves other
      work. The PR #318 security review found that counting with zlib's gzip mode let a member
      whose header zlib refuses (FHCRC with a bogus CRC, a reserved flag bit) count nothing
      although the job decompresses it all: members are now counted with the job's own gzip
      reader, and a test checks the count equals what that reader produces for plain,
      concatenated, padded, truncated, trailing-garbage, non-gzip and both header cases.
      Since #319 the count reads the member through a buffer: tests check that zero padding and a
      long gzip file name are read in blocks, that bytes read ahead past a stream's end count
      once and that the cap still stops a buffered read, and that ZIP damage found during the
      gzip count is refused as `ARCHIVE_UNREADABLE`. `02_ingest.sh` runs both in the verify
      project's API container, checks that the API takes the 2 GiB cap from
      `docker-compose.yml`'s `MAX_ZIP_EXPANDED_BYTES`, then
      through the proxy refuses a truncated ZIP, ingests a ZIP of `.json`, `.json.gz` and
      `.jsonl.gz` members, and refuses with the exact message a ZIP declaring 2,049 MiB and one
      whose gzip member expands to 2,112 MiB, while job polls are still answered;
      `06_ui.sh` checks the Import page shows the API's message and a failing status poll,
      which a new ingest and the next successful poll clear.*
- [x] One ZIP holds at most `max_zip_members` (5,000) entries, or the upload is refused with `ARCHIVE_TOO_MANY_MEMBERS`; a ZIP member of 1 MiB or more expands to at most `max_zip_member_ratio` (200) times its compressed size, declared and as read, or the upload is refused with `ARCHIVE_RATIO_TOO_HIGH`; members whose path leaves the archive are validated and counted but never extracted, and are listed in `skipped`; a refusal's `detail.members` holds at most 10 members, with `members_omitted` counting the rest (#28).
      *On develop a ZIP of 20,000 entries and one holding 50 MiB of zeros (about 1,000:1) were
      accepted, a corrupt `../bad.txt` beside a valid file was dropped unvalidated and unlisted,
      and 12 damaged members gave 12 `detail.members`. `test_zip_limits.py` (in the API image,
      with a member cap of 20 set through `MAX_ZIP_MEMBERS`) covers the settings and defaults,
      20 entries accepted and 21 refused before any member is read with the exact message and
      `detail`, folders, unsupported files, macOS metadata and escaping members all counted, the
      cap per ZIP, the count winning over damage, 5,001 entries refused at the default; the ratio
      rule (under 1 MiB exempt, exactly the ratio accepted), a 1 MiB member of zeros refused
      before anything is read, 1 MiB - 1 accepted, a stored member and a `.json.gz` member's gzip
      output not counted toward the ratio, a member refused while read within a block of passing
      the ratio when the declared check is bypassed, the expansion cap winning over the ratio and
      the ratio over an earlier unreadable member; a corrupt escaping member refusing the ZIP,
      escaping members listed in `skipped` and not staged, their bytes (not their gzip output)
      counted and their ratio checked; and `detail.members` bounded to the ten the message
      names. 19 of its 28 tests fail against develop's `ingest_pipeline.py`. Fixtures that used a
      highly compressible Deflate member only to reach another limit now store it uncompressed
      (`json_over_size_member.zip`, two `test_zip_expansion.py` cases, the gzip buffer tests).
      `02_ingest.sh` runs the test in the verify project's API container, checks that the API
      takes 5,000 and 200 from `docker-compose.yml`, and through the proxy refuses
      `zip_many_members.zip` (5,001 entries) with `ARCHIVE_TOO_MANY_MEMBERS` and
      `zip_high_ratio.zip` (2 MiB of zeros in 2 KB) with `ARCHIVE_RATIO_TOO_HIGH`, each with the
      exact message and `detail`.*
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
      *19 controlled runtime groups plus one five-source documentation group pass. Suite08, called by suite02, passes eight real parser/window/text-storage checks with vectorization disabled. Focused production ingest plus the nested check passes 19 checks in 1m19s. Optional production-model checks are separate; two prior attempts failed embedding timeouts covered by PR61.*

- [x] Deleting a collection while an ingest, export, import, tune or gold-standard generation job is active for it (either spelling) answers `409 COLLECTION_BUSY` naming the job's kind and id, deletes nothing and does not wait for the job; once the job's status is terminal the delete succeeds (#40).
      *Issue #40 reported a `DELETE` during an ingest returning `200`, the job failing
      with batch errors, and the collection coming back with 608 objects and no
      `created_at`. On `develop` at 3a8d462 the writer guard (since the issue was written)
      made the `DELETE` wait for the whole job instead (223 s for a 1,373-chunk upload)
      and then delete what it stored, and a `DELETE` that ran before a queued job's
      worker still let the job recreate the collection. **Fixed**: one registry of active
      jobs per canonical collection name, checked before and while the delete waits for
      the writer guard and again once it holds it. `test_delete_busy.py` (18 cases in the
      API image) drives each kind's real start path, checks its registration ends before
      its status turns terminal, and arranges the request orderings with events: a job
      registered before the delete, while the delete waits for the guard (refused within a
      second while the guard is still held), and once the delete holds it.
      `02_ingest.sh` sends a `DELETE` while a 180-chunk upload is still embedding (`409`,
      objects in place), and again after the job completes (`200`, still gone 10 s later).
      Full verification run 2026-10-17: all seven suites passed (10m57s).*
- [x] `DELETE /collections/{name}` without `?confirm=true` (case-insensitive) answers `400 CONFIRMATION_REQUIRED` before the collection is looked up and deletes nothing; the UI, the MCP tool and the verify scripts send it (#302).
      *`test_delete_busy.py` sends no value, `false`, `1`, `yes`, an empty value and `truee` (400, no lookup, nothing deleted), a missing collection without it (400, not 404) and `TRUE`/`True` (200). `02_ingest.sh` repeats the 400 cases and a `confirm=TRUE` delete on the live verify stack, and `test_mcp_delete.py` checks the MCP tool sends `confirm=true` with `name` still its only input.*
- [x] While a delete of a collection is in progress, a new ingest, export, tune or generation job for it answers `409 COLLECTION_DELETING` and leaves nothing behind, and an import of it ends `failed` with that code; a delete still waiting for the writer guard keeps #40's ordering (#302).
      *`test_delete_busy.py` holds a real `DELETE` inside its backend delete and drives each route and the import worker against it, under both spellings, and checks generation answers without its one-second wait. `ui_criteria.js` checks the Import page shows the message for an upload refused this way. An `abort` import whose target is still present also ends `COLLECTION_DELETING` (#324).*
- [x] When Weaviate can't be read to check whether the collection exists, `POST /query`, `POST /goldstandard/generate`, `POST /collections`, `DELETE /collections/{name}`, `POST /ingest/upload` and `POST /export` (#366) and the tune starts `POST /tune/rechunk`, `/tune/reembed` and `/tune/reindex` (#349) answer HTTP 503 `SERVICE_UNAVAILABLE` as a JSON error, `detail` `{"collection": "<name>"}`, with the cause logged and not returned and nothing run, created, saved or queued. `GET /tune/{collection}` answers the same 503 when its existence check or its re-chunk coverage check can't read Weaviate (#349). `POST /tune/rechunk` and `POST /tune/reembed` with chunking fields answer it too when their re-chunk coverage check can't read Weaviate (#340), with no job and the guards released.
      *`test_reingest.py` (run by `02_ingest.sh`) calls each handler with Weaviate failing, and sends each request through the app over HTTP, checking the JSON 503 reaches the client (#369), the tune starts' re-chunk check included (#409).*
- [x] A gold-standard start cancelled while its session is being prepared ends its registration and leaves the session `cancelled`, so the collection can be deleted (#302).
      *`test_delete_busy.py` cancels a start while sampling is held, then checks the registration, the session's status, a `200` delete, and that a settle that fails is reported as a failed generation. A cancelled start whose preparation fails settles nothing (#324).*
- [x] Ingest jobs report `chunks_submitted` and `chunks_accepted` beside `chunks_stored`, counted once per file from its final attempt; older records read them back as `null`; the Import page shows the three only when they differ (#302).
      *`test_write_counts.py` covers the writer's counts for a clean insert, rejected objects, a failed check, a send failure and a cleanup failure, a retried file, job totals over stored, rejected, unparsed and deleted-collection files, and an older record through the route. `02_ingest.sh` checks a clean live job's three counts agree; `ui_criteria.js` checks the panel line for differing, equal and missing counts. `test_write_counts.py` also checks a clean job's counts agree after each file's insert, and `test_mcp_delete.py` that `rag_ingest_status` passes the counts through, `null` included (#324).*
- [x] An insert never creates a collection: an insert into a deleted collection fails and the collection stays absent (#40).
      *Weaviate's auto-schema recreated the class with inferred properties, no
      vectorizer and no vector index, which the app listed with `index_type: unknown`
      and no `created_at`. **Fixed**: `AUTOSCHEMA_ENABLED: 'false'` (§2.1); every path
      that makes a collection already created it explicitly. `02_ingest.sh` creates,
      deletes and then inserts into a collection in the API container, and checks the
      insert fails and nothing is recreated. The full run is the check that import,
      tuning rebuilds and export do not rely on auto-schema.*
- [x] An ingest job whose collection is deleted before or while it stores fails with "collection '<name>' was deleted while this job was running", stops processing files and retains no originals for them (#40).
      *The job used to report "Stored vector is missing or invalid" (auto-schema on) or
      a cleanup error saying accepted chunks may remain (auto-schema off), neither
      naming the deletion. `test_delete_busy.py` covers a job admitted after a completed
      delete, a collection deleted between two files, a store failure with the collection
      present and a failed existence check (both reported as before), and a parse failure,
      which does not consult the collection's existence.*
- [x] Ingest job status survives an API restart (#23): a job that completed before the restart
      reads back unchanged, and a job interrupted after storing a file answers `200` with
      `status: partial`, the interruption message in `errors` and `files_completed` as it was.
      *Every job used to answer `404 JOB_NOT_FOUND` after a restart. `02_ingest.sh`, with
      `RAG_ALLOW_RESTART=1`, ingests a file, then uploads it with a slow text file, restarts the
      API with `docker compose restart --timeout 0 api` once the first file is stored, and
      checks both jobs.*

- [x] Re-ingesting a file under the same logical name replaces its chunks and its retained original, even with unchanged content; the job reports `chunks_replaced`; the earlier version survives a failed store; gold-standard sessions are marked stale before a replacement and never for new names (#22).
      *Re-ingesting used to add a second chunk set beside the first, so answers could cite
      a superseded version, and the old digest kept claiming the name. `test_reingest.py`
      (API image, backends mocked) covers no replacement for new names, one marking per
      job naming the replaced files before any insert, insert before delete, unchanged
      content, a marking failure (file failed, nothing changed, next file retries), a
      failed insert (earlier version kept), a removal failure (`partial`, new chunks kept),
      a deleted collection at the lookup (#40), the delete rounds, precondition and final
      count, and retention releasing the name even when the blob write fails.
      `02_ingest.sh` ingests `policies.txt` twice (one chunk set, `chunks_replaced` equal to
      the first count), then a changed version (only its text remains, one digest names it,
      the first blob is gone). With a real gold-standard session planted, it checks that a
      new name leaves the session current and a replacing re-ingest marks it stale, naming
      the file. `browser/ui_criteria.js` (suite 06) checks the Import page's "Replaced N
      chunks from a previous version" line, hidden at 0, and the plain "N files skipped:"
      heading. `test_reingest.py` also checks that a failed lookup of the later files is
      reported as the lookup, not as the marking (#311).*
- [x] ZIP members are named by their normalised path inside the archive, so same-named members in different folders stay separate documents; within one upload the last input with a logical name wins and each earlier one is listed in `skipped` with its reason (#22).
      *`a/report.pdf` and `b/report.pdf` both became `report.pdf`, and a name sent twice in
      one request was overwritten on disk but queued twice. `ZipFile.extract` also wrote
      `a/../b.txt` to `a/b.txt` while `b.txt` was queued. `test_reingest.py` covers a name
      twice in one body, repeated ZIP entries, the same path in two ZIPs, a direct upload
      against a top-level member, folders, `a/../b.txt` and `./c.txt`, paths leaving the
      archive and the provenance of labels. `02_ingest.sh` ingests a ZIP with
      `a/report.txt` and `b/report.txt` and two parts with one filename through the proxy.*
- [x] A ZIP's macOS metadata (`__MACOSX/` members and `._*` AppleDouble files) is not ingested and is listed in `skipped` with its reason; names that only look like it are ingested (#378).
      *A Finder ZIP's `__MACOSX/._report.pdf` kept `.pdf` and was queued as a document of
      its own. `test_reingest.py` covers a Finder-style ZIP (only the file queued, the
      AppleDouble member in `skipped`), a `._*` file beside its file, other `__MACOSX/`
      members whatever their suffix, look-alike names that are still queued, and a ZIP of
      only metadata refused with `NO_SUPPORTED_FILES`. It also checks that these members
      are still validated, a damaged one refusing the ZIP and their bytes counting toward
      the expansion cap, that `./__MACOSX/._r.txt`, `x/../__MACOSX/notes.txt` and
      `d/./._e.txt` are matched by their normalised names and listed under the archive's
      spelling, and that a direct upload named `._notes.txt` is still ingested (#455).
      `02_ingest.sh` ingests a Finder-style
      ZIP through the proxy: the job completes with no errors, stores only the file's
      chunks and lists the AppleDouble member in `skipped`.*
- [x] Two ingest jobs that replace one logical name in one collection run one at a time: afterwards exactly one job's version is stored, whole (its full chunk set, its `chunks_replaced` equal to the other job's `chunks_stored`, one retained digest naming the name, the winner's); which job runs last is not promised (#22).
      *`02_ingest.sh` sends two uploads of one name, with different content, at once
      through the proxy and checks that both jobs complete, exactly one version's text is
      stored, its chunk count is the winner's `chunks_stored`, the winner's
      `chunks_replaced` is the other job's `chunks_stored`, and one digest, the winner's,
      names it.*

### 10.2 Query

- [x] Invalid query enums, bounds and non-finite values return a serializable 422 before retrieval or model work.
      *Controlled tests assert no backend/model calls; `07_settings.sh` exercises real HTTP errors. The full valid-query suite passes nine checks.*

- [x] A question against an ingested collection returns a non-empty answer.
- [x] All four retrieval modes return results without error.
      *hnsw, flat, hybrid and semantic each returned chunks and an answer.*
- [x] `include_citations: true` returns citation objects with source_file and score.
      *Each citation carries source_file, chunk_index, score and excerpt.*
- [x] A citation's score is higher-is-better in every mode, and `null` when missing (#376).
      *In each of the four modes the scores are numbers, best first. On a real Weaviate, cosine,
      dot and l2-squared collections give the documented similarities of known vectors, and a
      collection recreated with another metric is converted by the new one. Controlled tests
      cover a missing distance or hybrid score (`null`), an unknown metric, and a failed metric
      read answering 503; the Q&A page shows `n/a` for a `null` score.*
- [x] `response_format: "end_user"` produces shorter, plainer answers than `"engineer"` for the same question.
      *Four paired trials: end_user shorter in 4/4, mean 652 vs 907 chars. Brevity
      was originally not instructed, so the margin was only a tendency, and
      `03_query.sh`'s 3-trial mean flipped in several PR evaluations (#109). The
      end_user prompt now asks for a short answer without technical detail, so
      "shorter" is instructed rather than incidental.*
- [x] Latency fields (`retrieval_latency_ms`, `llm_latency_ms`) are present and non-zero in all responses.
- [x] When, after the existence check, an Ollama call in the query pipeline can't connect, times out or answers an error status, or a Weaviate retrieval call fails, `POST /query` answers HTTP 503 `SERVICE_UNAVAILABLE` as a JSON error naming the service, `detail` `{"collection": "<name>"}`, with the cause logged and not returned and no metrics record written (#373). In `hybrid` and `semantic` modes, where Weaviate embeds the question through Ollama, a Weaviate failure names both Weaviate and Ollama (#411).
      *`test_error_envelope.py` (run by `01_infrastructure.sh`) sends each case through the app over HTTP, for each Ollama call (the synthesis in both response formats) and each retrieval mode's Weaviate call; it fails on the code before #373, and its hybrid and semantic wording on the code before #411.*
- [x] When the query's metrics record can't be written to `metrics.jsonl`, `POST /query` still answers 200 with its answer and the failure is logged. The file is bounded: an append that brings it to 1,000 lines, counted from the records loaded at startup, compacts it atomically to its last 500 records. A failed compaction leaves it longer until a later append's retry succeeds, and after a failed startup load it can pass 1,000 lines before its first compaction. The last 500 records reload into the ring after a restart, which reads at most the file's last 1 MiB (#374).
      *`scripts/tests/test_metrics.py` (run by `03_query.sh` in the built API image with `--network none`) sends the failed writes through the app over HTTP and checks `GET /metrics/latency` still counts the record; it checks the bound, compaction failures and concurrent records, and the startup load of a whole file, a large legacy file, a tail without a line break and an unreadable file; it fails on the code before #374. With `RAG_ALLOW_RESTART=1`, `03_query.sh` restarts the API after its queries and checks `GET /metrics/latency` reports the same records.*

- [x] Retrieval controls distinguish query method from the existing physical index and report actual backend HNSW settings; inactive ef/build sliders are absent.
      *Six controlled runtime groups plus one twelve-source documentation group pass. Registered browser criteria cover Top-K 1/50 save payloads, initial/refresh failures and late initial responses. Suite 11 (called by 03/all.sh) passes seven real backend configuration/vector-query checks, controlling only model responses. Actual browser shows 72/160/32 backend settings, labels saved ef 96 inactive, clears it on save without physical changes, normalizes the flat alias, and labels Q&A Vector; zero console errors and owned fixtures removed. Full suite is recorded separately.*

### 10.3 Gold Standard

- [x] A generation failure cached ahead of disk is saved by the next successful write; a removed session is never recreated by the cache fallback.
  *Evidence: session_persistence_cases.py cached_generation_failure_is_saved_by_next_successful_write and cache_fallback_does_not_recreate_a_removed_session.*

- [x] Pending stale/orphan markers survive restart and are resolved by the next successful session write. Session and marker files use mode 0600; imported sessions clear persistence_error.
  *Evidence: session_persistence_cases.py covers owner-only files, marker replay, symlink refusal and durability wording; session-import cases check cleared local persistence errors.*

- [x] Retained stale/orphaned session warnings reach the live API and browser, with reasons/timestamps and legacy defaults. Historical export requires explicit choice, keeps RAGAS compatibility and preserves the original session.
      *Six controlled service/runtime groups plus one twelve-source documentation group cover validity/export and rebuild failure boundaries. Registered real backend/in-process HTTP checks cover actual stale/orphan markers, strict choices, missing sessions, empty history, compatible exports and a failed destructive cutover; browser fixtures cover empty history, failed lookup and duplicate export requests. An actual browser against the built UI and isolated real API shows legacy defaults, warning reasons/timestamps, reset consent after actual deletion, empty historical warnings and explicit four-field RAGAS download. Suite 10 is called by 05/all.sh; full suite is recorded separately.*

- [x] Generate accounts for every sampled chunk as a completed pair or a reported failure, retaining useful partial results. Collection size caps `pairs_total`; all-failed generation is `failed`.
      *Evidence: `04_goldstandard.sh` checks terminal accounting, at least one usable pair and explicit failure reasons. `session_persistence_cases.py::test_generation_accounts_for_partial_and_total_model_failure` exercises durable partial and total model failures.*
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
      *Evidence: `test_session_import.py` (#103, #273): null, list, object, numeric, boolean, blank and left-out model fields fail the pair within the three-attempt budget rather than being stored; a left-out `ground_truth` defaults to the answer; valid strings are stored unchanged.*
- [x] Regenerate call replaces exactly one pair without affecting others; returns 409 if session is still generating, and 504 `PAIR_GENERATION_TIMEOUT`, leaving the pair unchanged, when model work passes the 1800-second deadline.
      *Evidence: `session_persistence_cases.py` `test_regeneration_deadline_returns_typed_error_and_preserves_pair` (504, pair and session file unchanged); suite 04 checks that the proxy's regenerate read timeout outlasts the API's deadline and that the deadline is over 300 s (#280).*
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
- [x] PATCH and regenerate answer 404 `SESSION_NOT_FOUND` for an unknown session and 404 `PAIR_NOT_FOUND` for an unknown pair in an existing session. PATCH refuses an empty or whitespace-only `question`, `answer` or `ground_truth` with 422 `INVALID_PARAMETER` and leaves the pair unchanged.
      *Evidence: `04_goldstandard.sh` runs `scripts/tests/test_goldstandard_review.py` (all four 404 cases, nothing written and no model call; the empty-field refusal for each field, before the session lookup; accepted-edit controls) and checks the unknown-session codes through the proxy, and in the LLM section the unknown-pair code and an empty question on a generated pair.*
      *Originally an unknown session answered `PAIR_NOT_FOUND` on both endpoints, and an empty edit was stored and exported as an empty RAGAS field (#381).*
- [x] Sessions survive API container restart (data loaded from `{UPLOAD_DIR}/goldstandard_sessions/`). Startup settles interrupted generations as failed, preserving completed pairs, counters and validity metadata; ordinary scans leave active generation alone.
      *Evidence: `session_persistence_cases.py` covers fresh-process settlement, mixed completed/interrupted sessions with edits and stale/orphan metadata, worker refusal without cache/file mutation, plain reason persisted without a false storage error, successful regeneration and on-disk retry after write failure. `04_goldstandard.sh` includes a guarded disposable API hard-stop and regeneration check when a retained pair is available.*
- [x] Export, edit original, import with rename twice: all three sessions remain independently usable for lookup/RAGAS export, and imported identities/provenance survive restart.
      *Registered `13_identity.sh` exercises controlled collision, concurrent creation, guarded reads, and actual package/HTTP/backend/fresh-process acceptance. The exact current-head counts and full-suite result are recorded in PR #69.*
- [x] Generation start returns 503 `SESSION_WRITE_FAILED`, writes nothing and records a diagnostic for the candidate session file when identity selection cannot inspect session storage (including a redirected storage directory) or runs out of attempts. Import refuses a source session ID that is not `gs_[0-9a-f]{8}` with `PACKAGE_CORRUPT` naming the sidecar, before restoring anything.
      *Registered `13_identity.sh` runs 21 owned cases in `session_identity_cases.py`. Generation start: `test_reviewer_generation_identity_inspection_failure_is_session_write_failed_503`, `test_generation_allocation_failure_records_diagnostic_for_candidate` (inspection failure and exhaustion), `test_reviewer_generation_redirected_storage_root_is_session_write_failed_503` and `test_reviewer_generate_route_returns_503_session_write_failed_envelope`. Source IDs: `test_noncanonical_source_identity_is_refused_before_restoration` and `test_reviewer_preflight_refuses_near_canonical_and_unbounded_source_ids` (near misses, a 10,003-character ID, an empty string and an integer are refused; `gs_0123abcd` is accepted). The live check in `session_identity.py` imports a digest-valid package with a noncanonical source ID and expects `PACKAGE_CORRUPT` naming the sidecar, nothing restored and the original unchanged. The redirected-storage and route cases fail on the code before #148.*
- [x] Generation guard contention is bounded to one second and returns `409 COLLECTION_BUSY` without sampling or publication; a completed deletion returns `404 COLLECTION_NOT_FOUND`.
      *Suite 04 sampling tests cover a timed-out alias writer, waiter cleanup, a real guarded delete, and a writer released before the deadline.*
- [x] Concurrent edits survive generation, independent-field updates and restart; write faults are not acknowledged, and changed regeneration targets are rejected. Initial chunk sampling and durable session publication share the collection mutation guard so replacement/deletion cannot miss the new snapshot; subsequent model work runs outside that guard.
      *Suite 04 calls `09_sampling.sh`, which runs `test_chunk_sampling.py`: controlled writer contention proves sampling/publication ordering; a paused generation proves both name spellings are unlocked; sampling and publication failures release the guard and start no task.*
      *Registered `12_persistence.sh` runs owned concurrency, interruption and failure-path cases plus real backend/HTTP/fresh-process checks. Exact counts and initial full-suite failures are retained in the PR; this is single-process acceptance.*
- [x] Recovery diagnostics preserve unreadable bytes, clear removed-file warnings, retain generation failure codes, and do not hold the writer lock while reading the archive.
      *Registered cases pause an archive read while an edit commits, inject failed pair writes and later status updates, and remove inspected fixtures. Health browser fixtures exercise pending, failed and out-of-order refreshes.*
- [x] Secondary session marker write failures do not misreport a completed collection deletion/import/rebuild or prevent later session markers.
      *Registered live HTTP deletion checks physical absence and registry removal under an owned marker failure; isolated import/tuning paths and two-session marker cases verify bounded continuation.*
- [x] An interrupted import never deletes a collection it didn't create. Each import writes a version-4 marker with a 32-hex instance token and creates its target carrying that token in abort, rename and replace modes; staging and existing collections carry none. At startup the collection is deleted only when it carries the marker's token and its records don't match the expected snapshot. With no token or another import's token it is kept and the marker and snapshot are retired; with an unreadable identity, a missing or invalid version-4 token, or a version-3 marker still building, the collection and marker are both kept (#126).
      *Registered by `05_transfer.sh`, which runs `scripts/tests/test_batch_recovery.py` in the API container: `test_every_import_mode_binds_its_target_to_the_marker_instance`, `test_import_creates_its_target_with_the_marker_identity`, `test_interrupted_import_compares_records_not_only_count`, `test_new_collection_under_unresolved_marker_name_is_kept`, `test_other_import_token_keeps_collection_and_retires_marker`, `test_unreadable_collection_identity_preserves_collection_and_marker`, `test_v4_marker_with_invalid_instance_never_deletes` and `test_marker_without_instance_identity_never_deletes`.*
- [x] At startup, an import expectation snapshot (`<32 hex>.sqlite3`) that no marker names is removed. Snapshots named by any parseable marker, even an invalid one, other file names and symlinks are kept, and an unreadable marker stops the sweep (#126).
      *`test_batch_recovery.py`: `test_orphaned_expectation_snapshots_are_swept_at_startup`, `test_unreadable_marker_blocks_the_orphan_snapshot_sweep` and `test_orphan_sweep_keeps_snapshots_named_by_invalid_markers_and_symlinks`.*
- [x] When a tuning or import failure retains recovery data, the job's `sidecar_snapshots` is relative to `UPLOAD_DIR` (`collection_operations/<operation>`) wherever the recovery root resolves; the absolute path is written only to the server log (#126).
      *`test_batch_recovery.py`: `test_job_errors_name_sidecar_snapshots_relative_to_upload_dir`, `test_absolute_sidecar_path_goes_to_the_server_log_only` (tuning and both import failure branches) and `test_sidecar_reference_does_not_depend_on_where_the_root_resolves`; `test_collection_writes.py`'s `test_replace_failure_retains_positive_recovery_and_startup_preserves_it`, run by `14_reindex.sh`.*
- [x] When gold-standard sessions can't be marked stale before a tuning replacement, the job fails `TUNE_FAILED` saying the original collection is unchanged; the original keeps its records, the retained copy is discarded and the error carries no `recovered_as` or `sidecar_snapshots`. A failure to discard the copy is logged and finished at the next startup (#126).
      *`test_batch_recovery.py`: `test_stale_marking_failure_before_cutover_discards_copy_and_keeps_original` and `test_stale_marking_discard_failure_is_logged_and_resumed_at_startup`.*
- [x] Export includes only approved/edited pairs; excluded count matches rejected + pending.
      *2 approved + 1 edited saved; 1 rejected + 1 pending excluded.*
- [x] Exported file is valid JSON and each pair matches the RAGAS schema.
      *Exactly `question`, `answer`, `contexts`, `ground_truth`; contexts a
      non-empty list of strings.*
- [x] Default filename follows `{collection}_{YYYYMMDD_HHMMSS}.json` pattern.
- [x] Download endpoint returns 404 for a non-existent filename.
      *Traversal attempts (`..%2F`, absolute paths, sub-directories) are rejected
      at the routing layer before reaching the handler.*
- [x] Save writes, and download serves, only files in `{UPLOAD_DIR}/goldstandard_exports/`
      (#370). Save as `collection_registry.json` writes an export and leaves the registry
      unchanged; `.`, `..`, `metrics.jsonl`, a directory name, a wrong extension, a path or a
      129-character name gets 400 `INVALID_PATH` and touches no file; a directory or link
      in the exports folder gets 400 on save and download; a top-level file is not served
      (404); the default name, valid custom names and the UI's default form still save and
      download; saving twice under one name overwrites.
      *`test_goldstandard_exports.py`, run by `10_validity.sh`, which also checks
      download refusals through the proxy; `04_goldstandard.sh` checks the save
      refusals and that `GET /collections` still answers after a save as
      `collection_registry.json`.*
- [x] A file or a symbolic link named `goldstandard_exports` in `UPLOAD_DIR` gets 409
      `EXPORTS_FOLDER_INVALID` on save and download, not a 500, and nothing changes; a
      bad name is still 400 first. Save writes a temporary file and renames it onto the
      name: a link that appears at the name after the check is replaced and its target
      left unchanged, and a failed rename keeps the previous export and leaves no
      temporary file (#402).
      *`test_goldstandard_exports.py`, run by `10_validity.sh`.*

**Also fixed here:** `pairs_completed` was incremented in a `finally`, so it
counted *attempts*. A session that lost a pair reported "3/3 complete" while
holding 2. There are now three counters: `pairs_attempted` (drives the UI
progress bar, always reaches the total), `pairs_completed` (pairs that exist) and
`pairs_failed`. Failures whose exception carried an empty string were recorded as
`''`; the type name is now always included.

- [x] Seeded selection reaches the complete UUID population, preserves rank order across backend iteration orders, and retrieves payloads only for selected IDs.
      *19 controlled runtime groups plus one nine-source documentation group cover geometry-independent selection, API validation, missing winners and parked MCP function limits. Suite 09, called by 04, exercises 160 owned synthetic SDK objects across pages; supplied vectors avoid model calls. Its registered live HTTP cases reject invalid generation settings before a missing collection can be queried. This is selection evidence, not deterministic LLM output.*

### 10.4 Web UI

- [x] Chunking loads and save results stay with their selection generation, including A→B→A; save is unavailable until the selected config loads. Stale responses preserve current edits and errors, collection changes clear the saved notice, and failed collection lists or mismatched configurations are reported.
  *Evidence: `scripts/verify/browser/chunking_config.js` deferred load/save, stale failure, edited draft, collection-list failure, mismatched configuration and saved-notice cases in suite 06.*

- [x] The Q&A page shows an answer only for the latest query on the selected collection: a response that arrives after the collection changed (including A→B→A) or after a newer query is dropped, success or failure; changing the collection clears the answer, citations, latency and error on screen and ends the wait; Ctrl+Enter sends nothing while a question is being answered (#511).
      *Evidence: `scripts/verify/browser/qa_page.js` (suite 06, through `ui_criteria.js`) answers
      every API call by request interception and holds each query's answer until the check
      releases it: an answer and a failure for A released after the switch to B, an answer
      released after A→B→A, two queries in flight released latest first, a second Ctrl+Enter
      while loading, an answer and an error on screen when the collection changes, and Ask
      used on B while A is still in flight. All eight checks fail on the code before #511.*
- [x] Logging in through the login page opens `/qa` as the account's role; the session is an HttpOnly, `SameSite=Strict` cookie that page scripts can't read, nothing about the user is kept in browser storage, the session persists across navigation and reloads, and Log out ends it at the API and returns to the login page (#25).
      *Evidence: `scripts/verify/browser/ui_criteria.js` (suite 06) logs in with the harness's engineer through the form against the real API, and checks the cookie, storage, navigation and logout.*
- [x] Login page, setup-required page, forced and voluntary password change, the API's messages for a failed or throttled login, and the return to the login or password page on `401 AUTH_REQUIRED` or `403 PASSWORD_CHANGE_REQUIRED` (#25).
      *Evidence: `scripts/verify/browser/ui_controls.js` (suite 06), with every API call answered by request interception.*
- [x] End User role shows only the Q&A page in navigation.
      *Nav shows only Q&A, and `/collections` redirects to `/qa`; the API refuses those pages' requests too (§10.6).*
- [x] Citations toggle correctly hides/shows citation panel.
      *Evidence: `scripts/verify/browser/ui_controls.js` (suite 06) answers `POST /query` by request
      interception and checks that the box starts unchecked and the query sends
      `include_citations` false with no Sources panel, even when the reply carries citations; that
      checked, it sends true and the panel lists file, chunk, score (`n/a` for null) and excerpt; and
      that unchecking and checking it again hides and shows the panel of the answer on screen
      without a new query.*
- [x] Chunking strategy selection updates the explanation panel without page reload.
      *All five strategies render distinct text; zero navigations.*
- [x] Health dashboard shows per-service latency; auto-refreshes every 30 seconds.
      *Three services — Weaviate, LLM and Embed, each with its model name and
      latency. Polling measured at 30s intervals.*
- [x] Health dashboard shows a degraded 503 body with the failing services red, loads the latency metrics while health is degraded or can't be read, and shows a message, with no stale cards, when health can't be read.
      *Evidence: `scripts/verify/browser/ui_criteria.js` (suite 06) stubs `/api/health` with a
      degraded 503, a proxy 502, a refused connection and a 503 without a health body; its checks
      fail on the code before #375. `scripts/tests/test_health.py` (suite 01) checks the 503 body
      for a missing model and an unreachable Ollama or Weaviate.
      `scripts/verify/browser/health_page.js` (suite 06, #415) holds each stubbed answer and
      checks that a failed metrics call keeps the charts, that a 200 without a health body (JSON
      of another shape, empty or not JSON) and a 503 error envelope show a message, not cards,
      and that a late response from an older refresh never replaces a newer health result or
      newer charts; its JSON-200, empty-200 and three late-response checks fail on the code before
      #415. `test_health.py` also checks Weaviate and Ollama down together, and that memory below
      the recommendation or unreadable leaves `/health` at 200 `ok`.*
- [x] The Transfer page keeps polling through failed polls with the button busy, stops after 30 failures in a row with the button enabled again, sends no poll after it unmounts, labels a fidelity it cannot read instead of hiding it, shows an export's `error_code`, and shows a failed package list (#382).
      *Evidence: `scripts/verify/browser/transfer_page.js` (suite 06, through `ui_criteria.js`)
      stubs every API call in the browser: proxy 502s while polling either half, a poll in
      flight and an export start answered after the page is left, 409, 503 and 404 from
      `/tune`, a late fidelity answer for another collection, an `EMBEDDING_MISMATCH` export
      and a 500 from `/packages`. 13 of its 14 checks fail on the code before #382.*
- [x] Delete collection requires typed confirmation before calling API.
      *Confirm button starts disabled and stays disabled for a wrong name; no
      DELETE is sent until the collection name is typed exactly.*
- [x] A refused delete is shown in the delete dialog: the dialog stays open with the API's message as an alert, the collection stays listed, and Cancel clears the message (#40).
      *Before, the error went to the page-level line behind the dialog's overlay, so a
      refused delete looked like nothing happened. `ui_criteria.js` answers the `DELETE`
      with a synthetic `409 COLLECTION_BUSY` by request interception, so no collection is
      touched, and checks one `DELETE` was sent, the alert inside the dialog, the row and
      the cleared message after Cancel.*
- [x] A refused collection create is shown inside the New Collection modal on the Collections page and in the Import page's "+ New" modal: the modal stays open with the API's message as an alert, and Cancel or a successful create clears it (#379).
      *Before, the error went to the page-level line behind the modal's overlay. `ui_criteria.js`
      answers the `POST /collections` with a synthetic `409 COLLECTION_EXISTS`, then a `201`, by
      request interception, so no collection is created, and checks one `POST`, the alert inside
      the open modal, and no alert after Cancel or a success. On the Import page, a create whose
      list reload fails closes the modal and shows a failed list. The checks fail on the code
      before #379.*
- [x] A failed collection list is shown as an alert, "Could not load the collections:" with the reason, on the Collections, Import, Q&A and Gold Standard pages (#379).
      *Before, the error was swallowed, so an outage looked like "no collections". `ui_criteria.js`
      answers `GET /collections` with a synthetic `503` on each page, and checks the Collections
      page clears the message on its next successful load. The checks fail on the code before
      #379.*
- [x] The Gold Standard page pre-fills the export filename as `{collection}_{YYYYMMDD_HHMMSS}.json`, made safe and at most 128 characters, after a generation and after a lookup; opens the download with the filename encoded; refuses a sample size outside 1-100 before calling the API; and shows each pair as an expandable row with a 300-character context preview (#393).
      *Evidence: `scripts/verify/browser/goldstandard_page.js` (suite 06) answers every Gold Standard
      API call by request interception. Its filename, encoded-URL and sample-size checks fail on the
      code before #393 (`..json`, an empty field after a lookup, a raw `#` in the URL, and a generate
      request sent for 0, 101, 2.5 and an empty field). It also clicks Export in Chromium with the
      popup blocker on: a save answered in 1 s opens the download tab, and a control answered once
      `navigator.userActivation.isActive` is false opens none. It also asserts the expanded row's
      answer and ground truth, the `export` and no-safe-character filename fallbacks and that a
      sample size of 1 enables Generate (#469).*
- [x] The Gold Standard page shows how a generation ended (final status, failed pairs and the `errors` reasons, and the empty collection), stops polling with a message when a poll fails, keeps Generate disabled while a start or generation runs and Regenerate disabled per pair while it runs, and keeps the edit form open with the typed values when the `PATCH` is refused, sending only the changed fields (#380).
      *Evidence: `scripts/verify/browser/goldstandard_page.js` (suite 06) answers every Gold Standard
      API call by request interception: a completed, partially failed, failed, cancelled and empty
      session; a poll answered 503 and 404; a second Generate click in the window before the first
      poll; repeated Regenerate clicks; and an edit refused with 422, then retried. 30 of its checks
      fail on the code before #380 (no outcome shown, polling forever, a second session started,
      a second regenerate request, the modal closed with all three fields sent).*
- [x] The Import page states the 512 MB upload limit and refuses a larger selection without sending it.
      *A 513 MB sparse file: the page names the limit and no `POST /ingest/upload`
      is made. A 413 or other proxy error page is shown as a readable message
      instead of a JSON parse error (issue #21).*
      *Evidence for the proxy pages: `ui_controls.js` (suite 06) answers the Import upload with
      nginx's HTML 413, 502, 504 and 500 pages, a non-JSON 200 and a JSON error, and checks the
      message shown for each (the 512 MB limit text for a 413, "HTTP 500" for a plain 500, and
      never a JSON syntax error).*

- [x] Retrieval save confirmations and errors stay with their selection generation; the newest acknowledged success survives a newer failure and publishes at most once.
  *Evidence: `browser/query_config.js` stale success/failure, both acknowledged-success response orders, superseded notices/errors, independent notice timers and three-save edited-draft cases.*

### 10.5 Infrastructure

- [x] A restart restores a Raft snapshot and its later operations, preserving objects and deleted collections; the check reports elapsed restart seconds.
  *Evidence: suite 01 creates 140 schema changes, waits for a snapshot, writes a tail, restarts, and checks the restored snapshot index and data.*

- [x] Job records persist under `{UPLOAD_DIR}/jobs/{kind}/`: status changes and terminal records are written at once and atomically, progress at most once a second but never left unwritten, a failed write never fails the job, startup marks jobs left `queued` or `running` as interrupted, unreadable records are kept and logged, and the newest 200 per kind are kept, at startup and while the API runs, never removing an unfinished job (#23, #309). The first record is written off the event loop, and a start cancelled during it leaves no record or guard (#309).
  *Evidence: `test_jobstore.py` (35 cases in the API image, run by `02_ingest.sh`), including a real lifespan run over a stored running job, the four job starts and their cancellation, retention ordered by real writes, an abandoned first write that fails, a second export start waiting for the first one's record (#326), and that wait's error paths and its bound with `EXPORT_STARTING` (#338); with `RAG_ALLOW_RESTART=1`, live restarts of ingest (`partial` and `failed`), export and import jobs.*

- [x] At startup, before the first request, what a killed job left behind is removed (#377): an interrupted ingest's upload staging (`{UPLOAD_DIR}/ingest-*`), and an interrupted export's `.build-*` directory and `.<stem>.tar.gz.partial` archive (`.<stem>.tar.gz.age.partial` for an encrypted export, #27) in `EXPORTS_DIR`. Only those names at the top of each folder, and only a real directory or regular file as the name says, are removed: completed packages, look-alike names and anything in a sub-folder are kept, and a symlink is never followed. What is removed is logged by name and size; a removal that fails is logged and not reported as removed. An export sweep that fails is logged and startup carries on, with the ingest staging still swept (#423).
  *Evidence: `test_startup_sweep.py` (13 cases in the API image, run by `02_ingest.sh`), including a real lifespan run that removes both kinds and logs them, and one whose export sweep raises: the error is logged, the API still starts and the ingest staging is removed (#423). With `RAG_ALLOW_RESTART=1`, `02_ingest.sh` checks that its two killed ingests' `ingest-*` staging existed before the restart and is gone after it (#423), and `05_transfer.sh` that the killed export's build directory and temporary archive are gone after its restart, with the completed package kept.*


- [x] Weaviate cold startup has a 180-second healthcheck start period; restart limits reject invalid or overlong values before arithmetic.
  *Evidence: compose start_period and `RestartTimingTests` verify capped waiting, elapsed time from the supplied start, and positive six-digit limit validation.*

- [x] `docker compose up` brings all five services healthy within 5 minutes on a first start from empty data volumes, with the models already in the model volume.
      *With `RAG_ALLOW_RESTART=1`, `16_api_paths.sh` (run by `01_infrastructure.sh`)
      removes the verify project's volumes but its model copy (`down -v`) and times
      `up -d --wait`. The model pull isn't part of the automated check (#465). A
      first run with the pull, measured by hand: model volume deleted and re-pulled
      from scratch, **233s (3.9 min)**. Both models returned with their original IDs.*
- [x] `docker compose up` brings all five services healthy within 120 seconds on subsequent runs (models cached in volume, no re-download).
      *22s.*
- [x] Weaviate data persists across `docker compose down && docker compose up`.
      *10 collections with identical object counts.*
- [x] `collection_registry.json` and `ingest_configs/` persist across restarts; `GET /collections` reflects correct `created_at` after restart.
      *`created_at`, index type and distance metric all preserved.*
- [x] `collection_registry.json` is replaced atomically through an fsynced temporary file, so a failed write leaves the old file whole; a registry that can't be read is kept unchanged and logged, never overwritten, while creates, deletes and the list still answer (`created_at` `null`) (#388).
      *Evidence: `scripts/tests/test_collection_registry.py` in the built API image with `--network none` (suite 01); its save and unreadable-registry checks fail on the code before #388.*
- [x] The collection registry and settings saves fsync their folder after the replacement, and a failed folder fsync fails the save; API startup removes the `.collection_registry.json.*.tmp` files of interrupted registry saves, keeping the registry and every other file (#445).
      *Evidence: `scripts/tests/test_collection_registry.py` (suite 01) and `scripts/tests/test_settings_persistence.py` (suite 07); the folder-fsync checks and the startup check (a leftover still there after the lifespan) fail on the code before #445.*
- [x] `GET /packages` reads each package only up to its top-level manifest, with the manifest read bounded at 16 MiB; new packages put `manifest.json` first in the archive; a package with an oversized, missing or unreadable manifest is listed `readable: false` while the listing still answers 200 (#383).
      *Evidence: `scripts/tests/test_package_listing.py` in the built API image with `--network none` (suite 05): a package cut off after its manifest still lists, listing reads under 1 MiB of a package carrying 8 MiB after its manifest, an oversized manifest is refused unread, a manifest of exactly 16 MiB lists while one byte more is refused, and seven kinds of unreadable file are each listed `readable: false`; its cut-off, bytes-read, oversized-manifest, JSON-array and archive-order checks fail on the code before #383. `validate_package.py` checks a real export puts the manifest first, and suite 05 checks that `GET /packages` lists that export as readable with its collection, chunk count, fidelity and creation time.*
- [x] `GET /packages` lists a package whose manifest has a wrongly typed listed value (`collection` not an object, `name`, `fidelity` or `created_at` not a string, `chunk_count` not an integer) as `readable: false` with null metadata, never coercing it, and still answers 200; a `null` or absent value is listed as `null` (#451).
      *Evidence: `scripts/tests/test_package_listing.py` in the built API image with `--network none` (suite 05): eight wrongly typed manifests are each listed `readable: false` beside a readable package, and `collection: null` and absent values list as `null`; both checks fail on the code before #451 (the listing answered 500).*
- [x] Export with a `passphrase` writes `<stem>.tar.gz.age`, an age v1 scrypt (passphrase) file at work factor 18 that the same passphrase decrypts to the package an unencrypted export would write, with no unencrypted archive written and the package streamed in 64 KiB chunks; the README and `retrieve.py` name the `.age` file; a passphrase outside 12 to 1024 characters is refused 422 without being echoed; export and import jobs report `encrypted` and never hold the passphrase; `GET /packages` lists an encrypted package with `encrypted: true` and null metadata, without scrypt (#27).
      *Evidence: `scripts/tests/test_export_encryption.py` in the built API image with `--network none` (suite 05): the age module passes every applicable C2SP CCTV age vector (vendored in `scripts/tests/fixtures/age-testkit`) and an independent decryption written from the age specification, and runs an encrypted export and two imports through the router with tracing on to check no span or telemetry log record holds the passphrase; suite 05 exports the transfer collection encrypted, checks the file header and work factor, validates the decrypted package with `validate_package.py` and checks the listing entry; `browser/transfer_page.js` (suite 06) checks the passphrase rules, what the page sends and that it clears the fields.*
- [x] Import of a `.tar.gz.age` package decrypts it as a stream while unpacking it, checking each member before extraction and authenticating the file to its end; a wrong passphrase, a damaged or truncated file or a scrypt work factor above 20 (refused before any key derivation) fails the job `DECRYPT_FAILED` (with `error_detail.reason`) and a file without a valid age passphrase header `PACKAGE_UNREADABLE`, before any collection, marker, setting or session is touched and with the package file unchanged; an encrypted package without a passphrase is refused 400 `PASSPHRASE_REQUIRED` and a passphrase for any other package 400 `PASSPHRASE_NOT_EXPECTED`, before any job (#27).
      *Evidence: `scripts/tests/test_export_encryption.py` (suite 05) through `packager.open_package`, `importer._run` and the API; suite 05 imports the encrypted export with a wrong passphrase against the existing collection (`replace`) and checks the collections, their counts, the package file and the import work directories are unchanged, checks both 400 refusals, then imports it with the right passphrase (`rename`) with every chunk.*
- [x] `GET /ingest/config` returns `is_default: true` for a collection with no saved config; `is_default: false` after saving one.
- [x] `GET /retrieval/config/{collection}` answers 200 with the defaults and `is_default: true`, and logs a warning naming the collection, when the saved file can't be read, isn't JSON, holds JSON that isn't an object, has a field the response can't carry (a wrong type, a fractional or nested `top_k`, a `NaN` or infinite `alpha`) or holds the response-only key `is_default`; missing fields are filled from the defaults, and coercible and out-of-range values are still served as saved (#439, #477).
      *Evidence: `RetrievalConfigFileTests` in `scripts/tests/test_api_paths.py`, in the built API image with `--network none` (suite 01, through `16_api_paths.sh`), sends each saved file through the app; its non-object, wrong-type and fractional or nested checks fail on the code before #439, and its non-finite `alpha` and `is_default` checks fail on the code before #477.*
- [x] All inter-service traffic stays on the internal Docker network; only the proxy's container port 80 is published on host loopback.
      *Exactly one host binding: `127.0.0.1:8080->80/tcp` on the proxy. api and
      weaviate publish nothing; ollama and ui expose container ports only.*
- [x] Docker Engine is 28.0.0 or newer; older engines are outside the supported localhost-isolation profile. The infrastructure suite checks the daemon version.
- [x] Resolved Compose configuration and live Docker bindings contain exactly one published TCP port, on the proxy at host address `127.0.0.1`, targeting container port 80. A missing or all-interface host address fails verification. A different free host port preserves loopback and matches `RAG_EXPECTED_PROXY_PORT` (default `8080`); verified at `18080` on a disposable deployment.
- [x] LAN mode is opt-in (#26, §2.5): only `docker-compose.lan.yml` turns it on; with it the proxy publishes exactly one port, `0.0.0.0:443->443/tcp` (`RAG_LAN_PORT` changes the host port), nothing on port 80; the api runs with `RAG_LAN_MODE=true` and `AUTH_COOKIE_SECURE=true`; `tls-init` runs in the API image and the proxy waits for it to complete successfully; nginx's LAN configuration and the certificate volume are mounted read-only. Without the override the default publication and routes are unchanged.
  *Evidence: `scripts/verify/lan_config.py` (suite 18, every run) resolves the real override with `docker compose config`, starting nothing, at the default port and at `RAG_LAN_PORT=8443`; suite 01's binding checks still pass on the base file.*
- [x] With `RAG_LAN_MODE=true` the API exits 1 before any other import, startup work or request, with exactly one `LAN mode refused:` line on standard error and no traceback, when `AUTH_COOKIE_SECURE` isn't `true` (checked first) or no enabled human AI Engineer exists (an End User, a Developer, a disabled Engineer or an engineer-role service principal doesn't count); `RAG_LAN_MODE` accepts exactly `true` or `false` (#26).
  *Evidence: `scripts/tests/test_lan_mode.py` in the built API image with `--network none` (suite 18), including `import main` in a subprocess for each refusal and an AST check that `lan_mode.enforce()` is `main.py`'s first statement; in a `stack.sh run --lan`, one-off api containers of the LAN configuration refuse with `AUTH_COOKIE_SECURE=false` and with an empty account store while the running project stays healthy.*
- [x] LAN mode's nginx serves one TLS server on container port 443 with HTTP/2, TLS 1.2 and 1.3 only, Mozilla's intermediate ciphers, no session tickets and no HSTS header, routing exactly as the default server through the shared `locations.conf` (#26).
  *Evidence: `test_lan_mode.py` checks both configurations; in a `stack.sh run --lan`, suite 18 checks that TLS 1.2 and 1.3 connect, TLS 1.1 is refused and ALPN picks `h2`, that the only live binding is `127.0.0.1:<verify port>->443/tcp` and plain HTTP to it gets no API answer, and that a cookie-authenticated change over HTTPS from `https://localhost:<port>` is accepted and from another origin refused `403 CROSS_ORIGIN_REFUSED`.*
- [x] The certificate step uses `proxy/certs/cert.pem` and `key.pem` when both are present, refuses one alone, an unparsable file or a non-matching key, and warns on an expired certificate; otherwise it generates a self-signed certificate for `RAG_LAN_HOSTNAMES` plus `localhost` (refusing an empty value or an invalid entry), valid 825 days, reused until it is unreadable, mismatched, expired, names other hosts or replaces removed bring-your-own files; the pair is switched atomically with the key at mode 0600 (#26).
  *Evidence: `test_lan_mode.py` (suite 18) covers each case and a failed write; in a `stack.sh run --lan`, suite 18 verifies `/api/health` over HTTPS against the generated certificate with the name `localhost` and checks its names and 825-day validity.*
- [x] In LAN mode a login over HTTPS sets the session cookie `Secure`, `HttpOnly` and `SameSite=Strict` (#26, #25 Q12).
  *Evidence: `lan_stack.py cookie` in a `stack.sh run --lan` (suite 18).*
- [x] The verify project runs LAN mode only on loopback: `stack.sh run --lan` comes up in the default configuration, creates the first Engineer, then switches to `docker-compose.lan.yml` plus the harness's `docker-compose.lan.verify.yml` (TLS on `127.0.0.1:<verify port>`), whose guard rule (c) wants exactly `proxy:127.0.0.1:<port>->443/tcp` (#26).
  *Evidence: `scripts/tests/test_verify_stack.py` (the LAN guard rule and its refused variants, the `--lan` argument refusals and the Docker calls of `up --lan` in order); the `stack.sh run --lan` on the #26 pull request.*
- [x] Optional OpenTelemetry foundation (#282, #356): disabled by default with no providers, exporters or file reads; an invalid enabled configuration (§8), including a `RAG_OTEL_HEADERS_FILE` or `RAG_OTEL_CA_FILE` that is not a regular file, fails startup with field-only errors, its bounds accepted at their edges and refused beyond them; private providers leave the global OTel providers and root logging unchanged; only schema-approved fields reach OTLP; bounded queues, one-attempt export and bounded lifecycle waits keep a collector that answers with an error, refuses the connection or hangs from holding application work; an optional `RAG_OTEL_CA_FILE` is validated at startup and is the only trust store for HTTPS export when set.
  *Evidence: `scripts/tests/test_telemetry.py` in the built API image with `--network none` (suite 01) against loopback OTLP receivers, including an HTTPS receiver signed by a test-generated private CA; `scripts/tests/test_telemetry_implementation.py` keeps the embedded copies in step.*
- [x] Every API error answers in the JSON envelope (#373): an unknown route `404 NOT_FOUND`, a wrong method `405 METHOD_NOT_ALLOWED` with its `Allow` header, and an unhandled error `500 INTERNAL_ERROR`. `GET /collections` answers 503 `SERVICE_UNAVAILABLE` when Weaviate can't be read, a missing or unreadable help template answers 500 `HELP_RENDER_FAILED`, and `CREATE_FAILED` and `DELETE_FAILED` send a fixed message with `detail` `null`. In each case the cause is logged, not returned. A help template that fails to render (`RuntimeError`) answers 500 `HELP_RENDER_FAILED` with its own message, and other HTTP errors (FastAPI's 400 for a body it can't parse) keep FastAPI's own `{"detail": ...}` answer (#411).
  *Evidence: `scripts/tests/test_error_envelope.py` in the built API image with `--network none` (suite 01) sends each request through the app with Weaviate and Ollama mocked; it fails on the code before #373. Suite 01 also checks through the proxy on the running stack that an unknown route answers 404 `NOT_FOUND` and a wrong method `METHOD_NOT_ALLOWED` (#411).*
- [x] `package.sh` and `package-offline.sh` archives hold exactly the files git tracks in the current commit (`HEAD`), with no untracked or ignored file and no uncommitted change, and both scripts refuse to run unless their directory is the top level of a git checkout (§11.2, #384).
  *Evidence: `scripts/tests/test_package_archives.py`, run on the host by suite 01 as the check `Distribution archives hold only the tracked files of HEAD (#384)`, runs both scripts in a scratch git repository with `docker` stubbed: untracked and ignored files and an uncommitted edit stay out of the zip and the offline bundle, executable bits are kept, both scripts refuse in an extracted copy and in a subdirectory of a checkout before calling docker, and a checkout reached through a symlink is not refused. The scratch repository and both scripts read no system or global git config, checked with a global config whose failing hook and `export-ignore` attributes would otherwise break or change the result (#447).*
- [x] Every pull request to `develop` or `main` runs the verification suite in CI (#24): `.github/workflows/verify.yml` on `ubuntu-latest`, with `RAG_SKIP_SLOW=1`, on the disposable verify project (`stack.sh up`, `all.sh`, `stack.sh down`, never `RAG_VERIFY_LIVE`). It always uploads the compose logs as an artifact and caches the Ollama models under a key built from the model names `ollama/entrypoint.sh` pulls. It runs with `contents: read` only and no secrets, and pins every action, all GitHub's own, to a full commit SHA. The README shows its status badge.
  *Evidence: `scripts/tests/test_ci_workflow.py`, run on the host by suite 01 as the check `the CI workflow runs the verify suite on every pull request (#24)`, checks the trigger, permissions, pins, step order, the always-run logs, upload and teardown, the cache key and save conditions, the badge and the embedded copies; it fails on the code before #24. The workflow's own run on the #24 pull request is its first execution.*

### 10.6 Authentication (#25)

- [x] Every route but `GET /health`, `GET /auth/status` and `POST /auth/login` answers `401 AUTH_REQUIRED` without a session, downloads and job polling included, and each role gets `403 FORBIDDEN` exactly on the routes above it in the §3.1.8 matrix, whatever the UI shows.
  *Evidence: `scripts/tests/test_auth_enforcement.py` in the API image (suite 17), which fails on the code before #25; `auth_stack.py matrix` for no session and each harness role on the running stack (suite 17), against the matrix written out in the check, not read from the API; `test_auth.py` checks that every app route has exactly one policy entry and every entry a route, and that a route without one is refused.*
- [x] Before bootstrap every protected route and the login answer `503 SETUP_REQUIRED`; `bootstrap` works once and is then refused; `create-user` is refused before it; generated passwords are printed alone on standard output and must be changed at first login, when only the session routes answer; the last enabled engineer can't be disabled or demoted; usage errors exit 2.
  *Evidence: `test_auth.py` (store and `python -m auth` in a subprocess); suite 17 on the stack: `bootstrap` refused, forced change on created accounts.*
- [x] Login, `GET /auth/me` and logout; the cookie's attributes; a session survives an API restart (with `RAG_ALLOW_RESTART=1`) and ends after 12 hours idle or 7 days; a password reset or disable ends the user's sessions at once, and a role change applies to an existing session's next request; the own password change needs the current password, follows the rules, and ends the user's other sessions.
  *Evidence: `test_auth.py` (expiry with a patched clock); suite 17 on the stack, with throwaway `vfy-t-*` accounts and a session aged in the store.*
- [x] A cookie-authenticated change from another origin, or with neither `Origin` nor `Sec-Fetch-Site: same-origin`, is refused `403 CROSS_ORIGIN_REFUSED`; the API sends no `Access-Control-*` headers; every login failure is the same `401 LOGIN_FAILED`; five failures for a username and address within 15 minutes throttle the pair for 15 minutes, for unknown usernames too, attempts are counted before they are checked, a wrong current password on `POST /auth/password` counts too, and the throttle survives a restart; a path with a control character (`%0A`) is refused `404` before its body is read; a password change to the current password is refused; the API log has one line per login and no password or session token.
  *Evidence: `test_auth.py`; suite 17 on the stack; suite 01's CORS check.*
- [x] The verify harness gets its test credentials without committing, printing or passing them on a command line or in the environment: one account per role, generated passwords replaced at first login, kept in a 0600 file in a 0700 folder of the user's own outside the checkout, with the operator command run only on the verify project.
  *Evidence: `scripts/tests/test_verify_auth.py` on the host (suite 17); `auth_stack.py no-secrets-logged`.*
- [x] Only engineers reach the account administration routes. Creating an account checks the username rule and
  uniqueness and gives a generated password, shown once (`Cache-Control: no-store`) and to be changed at first
  login; a reset does the same, ends the user's sessions, and is refused for the engineer's own account; a role
  change applies to an existing session's next request; disabling ends the user's sessions and refuses their login,
  and enabling restores it; the last enabled engineer can't be demoted or disabled, also by themselves; no route
  deletes an account; each change and refusal is one API log line with no password.
  *Evidence: `test_auth.py` `AdminRouteTests` and the policy checks (suite 17); `auth_stack.py matrix` with the six
  routes for every role and `auth_stack.py admin-accounts` on the running stack, with a throwaway `vfy-t-*`
  account and the API log searched for the generated passwords (suite 17). These checks fail on the code before
  PR 2 of #25, which has no such routes.*
- [x] The Users page: an engineer-only nav link and route; the account list; create and reset show the generated
  password once until "Done"; no reset on the own row; role change, disable and enable; a refusal's message.
  *Evidence: `scripts/verify/browser/ui_controls.js` (suite 06), with every API call answered by request
  interception; `ui_criteria.js` checks the real engineer's nav includes Users.*
- [x] Service keys: only engineers' sessions reach the principal and key routes; a key is issued only to a service
  principal, shown once (`Cache-Control: no-store`) and stored only as its SHA-256; a principal can't log in; a key
  works only as `Authorization: Bearer`, with its principal's current role and no `Origin`, and never on account,
  principal or key administration, logout or password change; a header decides even with a cookie; rotation keeps
  the old key until it is revoked or expires; revoked, expired, unknown and malformed keys and keys of a disabled
  principal get `401 KEY_INVALID`; a key in a URL is no credential; expiry is 1-365 days, 90 by default; principals
  share the name pool and don't count for the last-engineer rule; the operator command lists principals, refuses
  their password reset and issues no key; each change and refusal is one API log line, and no key reaches the log.
  *Evidence: `test_auth.py` `KeyFormatTests`, `KeyStoreTests`, `KeyMiddlewareTests`, `PrincipalRouteTests`, the
  `CommandTests` principal case and the policy checks (suite 17); `test_auth_enforcement.py` with the seven routes;
  `auth_stack.py matrix` with the seven routes for every role, `auth_stack.py key-matrix` with a key of each role,
  and `auth_stack.py service-keys` on the running stack with throwaway `vfy-s-*` principals and the API log
  searched for the keys (suite 17). These checks fail on the code before PR 3 of #25, which has no keys.*
- [x] The Service keys page: an engineer-only nav link and route; the principals and their keys; create, role
  change, disable and enable; issuance shows the key once until "Done"; revoke only on active keys; a refusal's
  message.
  *Evidence: `scripts/verify/browser/ui_controls.js` (suite 06), with every API call answered by request
  interception; `ui_criteria.js` checks the real engineer's nav includes Service keys.*

### 10.7 Audit log (#29)

- [x] Only engineers' sessions read the audit log: End Users and Developers get `403 FORBIDDEN` on `GET /audit` and
  `GET /audit.csv`, every API key `403 KEY_NOT_ALLOWED`.
  *Evidence: `test_audit.py` `PolicyTests`, `ApiTests`, `MiddlewareTests` (suite 19); `audit_stack.py access` and
  the two routes in `auth_stack.py matrix` and `key-matrix` (suites 17 and 19).*
- [x] Each audited action writes one record with the server-side actor, role, key id and address, the result and
  status, and its details: logins (success, failure as `anonymous` with the typed username, throttled), logout,
  password changes, queries (no question text unless `AUDIT_LOG_QUESTIONS=true`), collection create and delete,
  configuration saves, ingest, export, import and tune jobs (`accepted`, then one end record with the same job id,
  `interrupted` after a restart), gold-standard save and download, every account, principal and key administration
  action and refusal, operator-command actions, audit reads, and each API start. An identified principal's 403 is
  recorded; 401s and 503s aren't; other GETs aren't.
  *Evidence: `test_audit.py` `MiddlewareTests`, `JobTests`, `CommandTests` (suite 19); `audit_stack.py
  start-record`, `sessions`, `query`, `jobs`, `refused` and `admin` on the running stack (suite 19).*
- [x] No password, session token, API key or passphrase reaches the audit files.
  *Evidence: `test_audit.py`; `audit_stack.py no-secrets`, `jobs` and `admin`, which search the files in the API
  container (suite 19).*
- [x] Records are append-only JSON Lines with exactly the §3.1.9 keys; ids follow line order under concurrent
  writers (API threads and the operator command); rotation keeps `AUDIT_LOG_KEEP` files; a torn last line is
  skipped; the three settings are validated at start; a failed write is logged and counted and never changes the
  answer.
  *Evidence: `test_audit.py` `RecordTests`, `SettingsTests`, `ReviewerEdgeTests` (suite 19); `audit_stack.py
  concurrent`: parallel failed logins with the operator command appending, each recorded once (suite 19).*
- [x] `GET /audit` filters, pages newest first with a stable `before` cursor and refuses invalid parameters with
  `422` (a non-ASCII digit in `limit` included); `GET /audit.csv` has the §3.1.9 header, every matching row and
  escaped formula cells (tab and carriage return included); a read that spans a rotation neither repeats nor misses
  a record.
  *Evidence: `test_audit.py` `ApiTests` (suite 19); `audit_stack.py filters` (suite 19).*
- [x] Records survive an API restart, which writes a new `system.start` (with `RAG_ALLOW_RESTART=1`).
  *Evidence: `audit_stack.py restart-prepare` and `restart-verify` (suite 19).*
- [x] The Audit page: an engineer-only nav link (last) and route; the records and paging; filters applied and
  carried into the CSV link; the question-text note and the failed-writes warning; a double click on Older while a
  page loads asks for it once.
  *Evidence: `scripts/verify/browser/ui_controls.js` (suite 06), with every API call answered by request
  interception; `ui_criteria.js` checks the real engineer's nav includes Audit.*

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
| Archive | ~1.3 MB zip | ~4.6 GB tar |
| Contains | source only | source + all 5 images + model weights |
| Target needs internet | **yes** (~8–10 GB downloaded) | **no** |
| Docker disk on target | 20 GB | ~12 GB (no build cache needed) |

Both archives MUST hold exactly the files git tracks in the current commit
(`HEAD`), built with `git archive`, so nothing untracked or ignored goes in:
not `.DS_Store`, `node_modules/`, `__pycache__/`, `dist/`, `.env*`, `*.tar`,
`offline/`, previous archives, `.claude/` or `dev-docs/`, nor the contents of
`ingest-inbox/` and `exports/` (their tracked `.gitkeep` keeps the directories).
Uncommitted changes are not included. Both scripts MUST refuse to run unless
their directory is the top level of a git checkout (#384). Neither carries the
Weaviate index or uploaded files; those are created empty on first start.

### 11.3 Offline bundle contents

`package-offline.sh` produces `rag-docker-offline.tar` containing:

```
rag-docker/
├── <the files git tracks in HEAD, as package.sh>
├── install-offline.sh
└── offline/
    ├── images.tar.gz            # docker save of all 5 images (~2.5 GB)
    └── ollama_models.tar.gz     # phi3.5 + nomic-embed-text (~2.2 GB)
```

The image list MUST match the images `docker compose config` resolves. It is
`rag-docker-api:latest`, `rag-docker-ui:latest`,
`semitechnologies/weaviate:1.40.2`, `ollama/ollama:0.3.14`, `nginx:1.27-alpine`.
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

- [x] Reindex with embeddings unavailable preserves exact UUIDs, properties and vectors while changing the physical index; retained evaluation validity is unchanged on success.
      *Registered `14_reindex.sh`: 24 runtime cases, fourteen writer/import/recovery cases, four async lifecycle/parent-cleanup cases, two polling cases and 44 real backend/ASGI/restart checks pass on Python3.11 (4 shell groups, 0 failures). The actual API upload remains queued through cutover; its later supplied-vector write and original exact records both survive. A refused vectorizer mismatch leaves records/config/session bytes intact, and a forced final-create failure preserves recovery through an independent API lifespan.*

- [x] A tuning or ingest replacement makes each pre-replacement stale marker durable, in the session file or a pending marker. If both writes fail for a session, the replacement aborts before the target is touched, and the markers that call already wrote, under both first-character spellings, are rolled back: each session's `stale`, `stale_reason` and `stale_at` and its pending marker return to their earlier state. A marker that can't be rolled back stays, so its session stays historical; this is logged and reported as a `SESSION_WRITE_FAILED` diagnostic. Stale marking after cutover stays best-effort: if both writes fail there, the session is historical only until restart (#93, #275).
      *Registered by `10_validity.sh`, which runs `scripts/tests/test_session_validity.py` in the API container: `test_cutover_requires_a_durable_session_or_pending_marker`, `test_unpersistable_marker_aborts_real_rebuild_before_live_delete`, `test_pending_marker_alone_lets_real_rebuild_replace_and_survives_restart`, `test_post_cutover_marking_stays_best_effort`, `test_aborted_marking_rolls_back_written_sessions_across_spellings`, `test_aborted_marking_restores_pending_markers` and `test_unrollbackable_marker_stays_and_is_reported`.*

Overlap retains internal whitespace-only windows to preserve exact character coverage and overlap; wholly blank input yields no chunks. Hard character boundaries and fixed per-file limits remain the existing contract, rather than changing retrieval behavior in this follow-up.

- [x] Interrupted tuning final writes carry an operation instance token. Startup checks only the matching target against its retained recovery records, marks mismatches or missing targets stale, and preserves both collections and snapshots for inspection. A different target instance, unreadable backend, or missing recovery never authorizes deletion. Completed matching targets are left untouched.
      *Controlled restart cases in `test_batch_recovery.py`; startup deliberately does not delete targets using mutable recovery data.*

- [x] After an interrupted tuning cutover is checked, startup durably records the outcome (`complete`, `stale`, or `other-instance`) and clears `cutover_pending`, preserving the recovery collection and journal. Later startups do not compare or re-flag that operation. An undecidable check or failed outcome write remains pending for retry.

## Optional telemetry foundation (#282)

The backend owns one optional private OpenTelemetry runtime initialized before
startup clients and always closed on failed startup or shutdown. It is disabled
by default with no exporter construction or telemetry egress. Enabled invalid
configuration fails with field-only errors. Enabled RAG telemetry rejects a real
process `OTEL_SDK_DISABLED` value whose stripped, lowercase value is `true`,
before secret access or exporter construction, because all three pinned SDK
providers otherwise silently disable signals. Explicit configuration mappings
cannot mask this conflict; disabled RAG telemetry still returns a no-op. The
runtime must not mutate the process environment or global providers.
Configuration is listed in §8 and, with the finite safe
schema, specified in README.md, “Optional backend telemetry”. An optional
`RAG_OTEL_CA_FILE` (#356) names a mounted CA bundle for an HTTPS collector behind
a private CA: it is validated once at startup, replaces the bundled roots for
export, and is re-read by requests on new connections. No global providers,
automatic request instrumentation, application log bridge or collector deployment
are included in this foundation. Raw SDK providers remain internal; supported
instrumentation uses the runtime tracer, safe logger and meter, with lifecycle
through force_flush/shutdown. Python private-state access is outside this API
contract, not prevented by a security sandbox.

Only explicit service resource metadata, fixed instrumentation scope, finite
operation/outcome/error values and trace identifiers may leave through OTLP.
Prompts, answers, request/response bodies, credentials, filenames, paths,
document text and free-form errors/events are excluded. Metric dimensions are
restricted to the finite operational schema in #284. The
export boundary rebuilds protobuf records; trace/log queue inputs are sanitized
as well, including runtime-owned resource/scope metadata and log limits; queued
logs retain neither exception objects nor caller context references. Processor
sanitization must not modify caller-owned records. The runtime logger copies
supplied plain records and wrapper/inner records before SDK processing so both
normalization and exception expansion preserve caller-owned inputs; keyword
emission remains supported. All three forms snapshot finite schema-approved
attribute strings before SDK delegation, excluding forbidden mutable values;
concurrent caller mutation during snapshot construction is unsupported. All
non-mapping attributes normalize to empty. Each emission form is wrapped with
runtime-owned limits before SDK processing, independent of ambient log/global
attribute count and length limits.
The private meter provider explicitly disables exemplar sampling so
attributes removed by metric views cannot remain in exemplar reservoirs; ambient
exemplar filter settings cannot override this policy. Existing application logging
remains unchanged. Bounded SDK queues,
one-attempt HTTP export and bounded lifecycle caller waits keep collector outages
independent of application work; OS DNS cancellation is not guaranteed.

Infrastructure verification runs real SDK traces/logs/metrics through a synthetic
loopback OTLP receiver with sentinel content and checks disabled behavior,
configuration validation, resources, sampling/signals, overload, export failure,
flush/shutdown, and embedded implementation consistency. It requires no collector
or external provider. Full acceptance runs only in the disposable verify project.


## Request and background tracing (#283)

Optional manual instrumentation uses the private application runtime. Request
spans cover ASGI execution including streaming and cancellation, with only
registered method-and-route names and finite response status classes. Incoming
version-00 traceparent supplies at most one sanitized link to a locally sampled
root; no remote baggage, trace state, authentication or sampling authority is
accepted. Invalid headers are ignored without altering the response.

Query stages, ingest stages, export/import, tuning, gold-standard generation and
regeneration, and actual application-owned Ollama/Weaviate calls produce finite
spans. Iterator consumption and batch finalization are included; backend-internal
model execution is not inferred. Worker admission captures runtime and local
parent context before raw executor/task/thread scheduling. A job is a child of
the initiating request even when it executes after the response; concurrent
jobs and reused threads cannot inherit one another's spans. Generation failure
reporters retain originating lineage. No content or job/session IDs are exported.

Safe outcome/error categories include handled failures and partial work, with no
exception text or events. Preserve task ownership, guards, callbacks, cancellation,
timeouts, status transitions and persistence semantics. A cancelled thread waiter
cannot imply that its running work ended. Existing shutdown does not drain jobs;
unfinished work or forced process termination does not guarantee final export.

Synthetic offline verification exercises real API/service launchers and actual
thread/task transitions with fake dependencies: parentage after 202, concurrent
isolation, cancellation/timeout, dependency errors/timing, batch/iterator calls,
header trust, disabled/failing telemetry, and sentinel-free serialized OTLP.
Infrastructure suite 01 registers these checks; full acceptance remains on the
disposable verify project. Collector setup remains a separate story.


## Operational metrics and correlated logs (#284)

The private runtime records API completion/duration/active work, dependency
calls/duration/errors and job completion/duration/active work, using the exact
names, units, finite dimensions and histogram buckets in README.md. No automatic
instrumentation duplicates these manual counts. Nested stages do not count as
additional jobs or requests. Metrics operate independently of trace recording,
sampling and log signal toggles. Only executed work contributes; cancelled
waiters cannot finish still-running workers. Active UpDownCounters settle on
scope exit and represent current process concurrency, not persisted history.
Loading durable job records, including marking interrupted jobs after restart,
does not create execution counts, durations or active work. Logical-name replacement
backend calls use the existing finite dependency labels; source names remain excluded.

Finite dimensions are validated before aggregation, restricted per instrument
by SDK views, and checked again during protobuf rebuilding. Valid multiple
series survive export even with tiny trace/log batches. Malformed, duplicate or
excess metric data rejects its record before admission. Other valid records
remain eligible. Compatible same-name records merge distinct approved point
identities; repeated identities retain the first snapshot without summation.
Incompatible kinds, temporality or monotonicity reject the later record. Valid
disjoint series are never truncated. Histogram statistics must be finite when
present and preserve their optional presence, with ordered boundaries and
consistent bucket totals.
No job IDs, trace exemplars, collection names, user IDs, paths, content or raw
exception values may become metric dimensions. Token usage is not fabricated.

Structured operation completion logs have fixed bodies and finite attributes,
current trace/span context when available, and a newly generated opaque job
execution token for job/dependency correlation. No durable or supplied job ID
is exported. Context survives admitted worker handoffs without leaking across
concurrent jobs or into caller code across iterator yields. Iterators capture
runtime, parent span and job token at construction, rebind them around lazy
factory/iter/pulls and completion, and emit nothing if never consumed. Sanitization occurs
before queueing and at the wire boundary. Existing Python logs are not bridged.
The log and metric schemas do not change application errors, durable statuses,
cancellation behavior or existing latency responses. Partial status normalizes
to metric error while retaining partial in logs and durable storage.

Suite 01 registers deterministic real-SDK tests for exact success/error/cancelled
counts, durations and settled active work, actual job producer outcomes,
concurrent correlation isolation, cardinality under changing IDs, multiple wire
series, disabled/sampled-out tracing, telemetry faults and receiver errors.
Synthetic dependencies require no hosted service; deployment acceptance remains
#285. Abrupt process death or surviving workers after shutdown may lose final
records; operational telemetry is not durable audit storage (#29).

### Operational instrumentation failure contract

SafeMeter is restricted to the registered counter, histogram and up/down
factories, names and dimensions. Unknown names or wrong factory/name pairs
return inert instruments; gauge/observable factories and arbitrary custom
metrics are unsupported. This is not a general SDK Meter compatibility API.

An active-instrument SDK factory/update failure suppresses the entire affected
instrument for that runtime lifetime, including further updates and later wire
exports. Both pre-mutation and mutate-then-raise failures are ambiguous: never
retry or fabricate a reset. Independent instruments and completion logs remain
available. Suppression loses active-instrument availability until restart; it
does not reconstruct concurrency or retract prior backend history or exports
already in flight. The two-instrument suppression registry is finite and local.
Local validation rejection returns false without changing instrument health.

## Optional Collector and end-to-end telemetry (#285)

The explicit `docker-compose.telemetry.yml` overlay plus `telemetry` profile adds
a digest-pinned local Collector and passes supported settings into the API.
Base startup remains five services with telemetry disabled. Collector ingestion
has no host-published ports and no application health dependency. Resource limits,
finite queues/retries, local summary-only diagnostics and optional standard OTLP
export with runtime mounted secrets are documented in `telemetry/README.md`.

Optional offline packaging includes only the selected platform image. Before
loading, validate its config identity against the tracked pin allow-list and
layer hashes against that config; never trust archive tags or image overrides.
Missing/mismatched optional images fail without pulling. Default offline behavior
remains unchanged.

The registered disposable telemetry suite distinguishes default-mode checks from
explicit `--telemetry` acceptance. Enabled acceptance must decode received
protobuf after the actual Collector: a real synthetic query and concurrent jobs
have connected request/operation/worker/dependency spans and matching completion
logs, finite safe metrics and no planted credential/content sentinels. Capture
overflow is failed acceptance. Disabled mode emits zero application exports;
Collector refusal and a slow destination preserve usable query/job work with
bounded configuration and reported latency/resource observations. Restart and
shutdown evidence does not imply job draining, durable queues or cross-restart
trace continuity. Full default and enabled runs and their actual limitations are
required; a resolved Compose file alone is not runtime acceptance.

Collector verification binds exact harness-owned assets across alternate checkouts
and pins verification protocol/version/export budgets. Disabled-mode observation
requires termination of previous producer, Collector queues and capture handlers.
Malformed decoded evidence is incomplete acceptance; malformed archive metadata
and incomplete TAR framing are rejected before offline reconstruction.
