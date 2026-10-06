# RAG Docker — MCP Server Specification

> **STATUS: PARKED (not wired into the project).**
> The MCP server is fully built and passed all 16 acceptance criteria, but it is
> deliberately not part of the running stack: no compose service, not in the
> offline bundle, nothing depends on it. The source is preserved in `./mcp` and
> these documents remain accurate as of that build.
>
> To resume: restore the compose service recorded in `MCP_IMPLEMENTATION.md` §7,
> add `rag-docker-mcp:latest` to the `IMAGES` array in `package-offline.sh`, and
> `docker compose build mcp`. No code changes are required.

**Status:** specification
**Depends on:** `MCP_ANALYSIS.md` (decisions and measurements)
**Followed by:** `MCP_IMPLEMENTATION.md`

This document is the contract. Where it conflicts with the analysis, this
document wins; where it is silent, the analysis explains the reasoning.

---

## 1. Scope

A single MCP server exposing the RAG platform's HTTP API as 15 MCP tools over
stdio, shipped as a sixth Docker image inside the existing packages.

### Settled decisions

| Decision | Value | Source |
|---|---|---|
| Runtime | Python 3.11 | confirmed |
| Transport | stdio, in a container | confirmed |
| Launch | `docker compose run --rm -T --no-deps mcp` | Analysis 7.3 (measured) |
| API route | compose network, `http://api:8000` | Analysis 7.3 (measured) |
| Tool surface | 15 tools, one server | confirmed |
| File ingest | one dedicated mounted folder | confirmed |
| Ingest blocking budget | 120 s; gold standard returns immediately | confirmed |
| Platform | macOS, Apple Silicon | confirmed |

### Explicitly out of scope

- Authentication, TLS, remote/HTTP transport.
- MCP resources and prompts (tools only).
- Text-content ingestion (`rag_ingest_text`). Analysis 7.1 floated it as a
  convenience; the dedicated-folder decision covers the real cases and keeps the
  surface at 15 tools. Deferred, not rejected.
- `GET /goldstandard/download/{filename}` — deliberately not exposed
  (Analysis 6).
- Linux and Intel macOS.

---

## 2. Deployment

### 2.1 Compose service

A service named `mcp` is added to `docker-compose.yml`:

- **Image:** `rag-docker-mcp:latest` (explicit tag, for the same
  directory-name-independence reason as `api` and `ui`).
- **Build context:** `./mcp`.
- **Profile:** `mcp`. It MUST NOT start on a plain `docker compose up -d`; the
  MCP client owns its lifecycle.
- **No `depends_on`.** Declaring a health dependency would make the client hang
  at launch waiting for the stack. The server starts regardless and reports a
  clear error per call if the API is unreachable (§6.3).
- **Networks:** `rag-internal`, so it resolves `api` by service name.
- **Volume:** `./ingest-inbox:/host:ro` — read-only.
- **`stdin_open: true`, `tty: false`.** stdio transport requires stdin; a TTY
  would corrupt the JSON-RPC framing.

### 2.2 The ingest inbox

- The repository MUST contain `./ingest-inbox/` with a `.gitkeep`, so the mount
  target exists on a fresh clone or extraction.
- It is mounted **read-only**. The server never writes to the host.
- It is the **only** host filesystem the server can see.
- `package.sh` and `package-offline.sh` MUST include the directory but MUST NOT
  include its contents (user documents are not part of the distribution).

### 2.3 Client configuration

```json
{
  "mcpServers": {
    "rag": {
      "command": "docker",
      "args": ["compose", "run", "--rm", "-T", "--no-deps", "mcp"],
      "cwd": "/absolute/path/to/rag-docker"
    }
  }
}
```

`cwd` MUST be the project directory; `docker compose` resolves the project,
network and service names from the compose file found there.

---

## 3. Configuration

All configuration is by environment variable, with defaults that work unmodified
in the standard deployment.

| Variable | Default | Meaning |
|---|---|---|
| `RAG_API_BASE_URL` | `http://api:8000` | Base URL of the RAG API. Note the api service serves routes at the **root** (`/health`), not under `/api` — that prefix belongs to the nginx proxy only |
| `RAG_MCP_INGEST_ROOT` | `/host` | Container path of the mounted inbox |
| `RAG_MCP_INGEST_WAIT_SECONDS` | `120` | Budget for `rag_ingest_files` before returning a handle |
| `RAG_MCP_HTTP_TIMEOUT` | `300` | Per-request HTTP timeout, seconds. Must exceed worst-case LLM generation |
| `RAG_MCP_LOG_LEVEL` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |

---

## 4. Tool catalogue

15 tools. Every tool MUST declare MCP annotations honestly:
`readOnlyHint`, `destructiveHint`, `idempotentHint`.

Annotations are **advisory** — a signal to the client, not enforcement
(Analysis 7.4). The server MUST NOT rely on them for safety.

### 4.1 Query

**`rag_query`** — ask a question against a collection.

| Field | Type | Required | Default |
|---|---|---|---|
| `question` | string | yes | — |
| `collection` | string | yes | — |
| `retrieval_mode` | enum `hnsw`\|`flat`\|`hybrid`\|`semantic` | no | `hnsw` |
| `top_k` | integer 1–50 | no | `5` |
| `alpha` | number 0–1 | no | `0.75` |
| `include_citations` | boolean | no | `true` |
| `response_format` | enum `end_user`\|`engineer` | no | `end_user` |

Numeric bounds above are **tool-level** validation choices, not limits the API
enforces. They exist to turn a nonsensical argument into a clear error instead of
a strange result.

Annotations: `readOnlyHint: true`, `destructiveHint: false`.

Notes:
- The API default for `include_citations` is `false`; this server overrides it to
  `true`, because a model benefits from provenance and the cost is small.
- `alpha` applies only to `hybrid`; the server MUST accept and forward it
  regardless rather than erroring, matching API behaviour.
- **`retrieval_mode` has exactly four canonical values.** `hnsw` and `flat` both
  route to a vector search over an embedding of the reformulated question;
  `hybrid` routes to Weaviate's hybrid search weighted by `alpha`; `semantic`
  routes to Weaviate `near_text`, which embeds the query server-side via
  `text2vec-ollama`. In `rag_pipeline.run_query` the `semantic` path is reached
  through a catch-all `else`, so any unrecognised string silently behaves as
  `semantic`; the enum must still be restricted here so an invalid mode is an
  explicit error rather than a silent change of algorithm.

  (An earlier revision of this document claimed only three modes on the grounds
  that the fallback branch was unnamed. That was wrong: the Retrieval Config page
  has always offered it as "Semantic", and `POST /retrieval/config` accepts it.)
- **`response_format` selects the synthesis prompt**, not just formatting:
  `end_user` asks for a short answer in plain language for a non-technical
  reader; `engineer` asks for technical detail plus an explicit confidence level
  (high/medium/low). The
  default here matches the API's own default rather than overriding it. A caller
  that wants confidence signalling should pass `engineer` — worth considering as
  the default, since the consumer of this tool is a program, but that is a
  behaviour change and is left as an explicit choice.
- Returns `answer`, `citations`, `retrieval_latency_ms`, `llm_latency_ms`,
  `chunks_retrieved`.
- Expected duration ~15 s (Analysis 7.2).

### 4.2 Collections

**`rag_list_collections`** — no input. `readOnlyHint: true`.
Returns name, object_count, index_type, distance_metric, created_at.

**`rag_create_collection`**

| Field | Type | Required | Default |
|---|---|---|---|
| `name` | string | yes | — |
| `index_type` | enum `hnsw`\|`flat` | no | `hnsw` |
| `distance_metric` | enum `cosine`\|`dot`\|`l2-squared` | no | `cosine` |
| `ef_construction` | integer | no | `128` |
| `max_connections` | integer | no | `64` |
| `ef` | integer | no | `64` |

Annotations: `readOnlyHint: false`, `destructiveHint: false`,
`idempotentHint: false`.

**Wire mapping (normative).** The three HNSW fields are flattened for ergonomics
but the API expects them nested under `hnsw_config` with **camelCase** keys:

| Tool field | API field |
|---|---|
| `ef_construction` | `hnsw_config.efConstruction` |
| `max_connections` | `hnsw_config.maxConnections` |
| `ef` | `hnsw_config.ef` |

**Why the enums are closed.** The API silently falls back rather than
rejecting: `DISTANCE_MAP.get(distance_metric, COSINE)` turns an unknown metric
into cosine, and any `index_type` other than `flat` becomes hnsw. A typo would
therefore produce a working collection with the wrong index and no warning.
Constraining both to enums at the tool boundary converts that silent substitution
into an explicit error. Accepted values are exactly `cosine`, `dot`, `l2-squared`
and `hnsw`, `flat`.

**MUST** return the **stored** name, not the submitted one. Weaviate capitalises
the first letter (Analysis 7.5, measured). The tool description MUST state that
only the first letter of a name is normalised and that the remainder is
case-sensitive, so a name differing beyond the first character will 404.
Implementation: after a successful create, read the name back from
`GET /collections` rather than echoing the input.

**`rag_delete_collection`** — input `name` (string, required).
Annotations: `readOnlyHint: false`, **`destructiveHint: true`**,
`idempotentHint: true`.
The description MUST state that this deletes the collection and all its chunks
irreversibly.

### 4.3 Ingestion

**`rag_ingest_files`**

| Field | Type | Required | Default |
|---|---|---|---|
| `collection` | string | yes | — |
| `paths` | array of string, min 1 | yes | — |
| `wait_seconds` | integer 0–600 | no | `RAG_MCP_INGEST_WAIT_SECONDS` |

`paths` are **relative to the inbox root**. Path resolution rules (normative):

1. Reject absolute paths.
2. Resolve against `RAG_MCP_INGEST_ROOT`, then canonicalise (resolving symlinks).
3. Reject any result that does not remain within the root — this covers `..`
   traversal and symlinks pointing outside.
4. Reject anything that is not an existing regular file.

Each rejection MUST name the offending path and state the inbox contract.

Behaviour: upload, then poll `GET /ingest/job/{job_id}` until terminal or the
budget expires.
- Completed within budget → return final job status.
- Budget expires → return `job_id` with `status: "running"` and instruct the
  caller to use `rag_ingest_status`.

Annotations: `readOnlyHint: false`, `destructiveHint: false`,
`idempotentHint: false` (re-ingesting duplicates chunks).

**`rag_ingest_status`** — input `job_id`. `readOnlyHint: true`.

**`rag_get_ingest_config`** — input `collection`. `readOnlyHint: true`.

**`rag_set_ingest_config`**

| Field | Type | Required | Default |
|---|---|---|---|
| `collection` | string | yes | — |
| `chunking_strategy` | string | no | `overlap` |
| `chunk_size` | integer | no | `1000` |
| `chunk_overlap` | integer | no | `200` |
| `similarity_threshold` | number | no | — |
| `min_chunk_size` | integer | no | `100` |

`readOnlyHint: false`, `idempotentHint: true`.

### 4.4 Gold standard

**`rag_generate_goldstandard`**

| Field | Type | Required | Default |
|---|---|---|---|
| `collection` | string | yes | — |
| `sample_size` | integer 1–100 | no | `20` |
| `seed` | integer | no | — |

MUST return the `session_id` **immediately** without polling (confirmed
decision): generation runs one LLM call per pair, so at the default of 20 pairs
it would exceed any sane budget. The description MUST tell the caller to poll
`rag_get_goldstandard`.
`readOnlyHint: false`, `idempotentHint: false`.

**`rag_get_goldstandard`** — input `session_id`. `readOnlyHint: true`.
Returns status, counts, pairs, collection, errors.

**`rag_update_goldstandard_pair`** — inputs `session_id`, `pair_id`, and optional
`status`, `question`, `answer`, `ground_truth`. At least one optional field MUST
be supplied. `readOnlyHint: false`, `idempotentHint: true`.

**`rag_regenerate_goldstandard_pair`** — inputs `session_id`, `pair_id`.
One LLM call; expect it to be slow. `readOnlyHint: false`,
`idempotentHint: false`.

**`rag_save_goldstandard`** — inputs `session_id`, `filename`.
`readOnlyHint: false`, `idempotentHint: true`.

### 4.5 Diagnostics

**`rag_health`** — no input. `readOnlyHint: true`.
Returns overall status plus Weaviate, LLM and embedding status. MUST succeed as a
tool call even when the API reports `degraded` (HTTP 503) — a degraded stack is
information, not a tool failure. Only an unreachable API is a tool error.

**`rag_metrics`** — no input. `readOnlyHint: true`.
Returns p50/p95/p99/mean/count for retrieval, LLM and total latency.

---

## 5. Protocol and process rules

1. **stdout carries JSON-RPC only.** No `print()`, no library writing to stdout.
   All logging goes to **stderr**. This is the single most common failure mode
   for Python MCP servers (Analysis 8, risk 1) and MUST be enforced by
   configuring logging explicitly at startup, not by convention.
2. The server MUST hold no state between invocations. All state lives in the RAG
   stack.
3. The server MUST NOT make outbound network calls other than to
   `RAG_API_BASE_URL`. This preserves air-gapped operation.
4. Tool names MUST match the `rag_*` names in §4 exactly.

---

## 6. Error model

### 6.1 Errors MUST be raised as `ToolError`

Discovered during implementation and verified against the SDK: only a
`ToolError` (`mcp.server.mcpserver.exceptions`) has its message delivered to the
client. Every other exception is re-raised as `UnexpectedToolError` carrying the
bare text `Error executing tool <name>` with no detail — by design, so an
unexpected crash leaks nothing.

Consequently every caller-facing failure in §6.2–§6.4 MUST be a `ToolError`, or
the message is silently discarded and the caller sees only the tool name. This is
not optional polish: without it, the API error code, the inbox path contract and
the connection-failure guidance are all lost, and every failure mode becomes a
dead end.

### 6.2 Passing through API errors

The API returns `{"error": {"code", "message", "detail"}}`. For any 4xx/5xx
carrying that envelope, the tool error message MUST include both `code` and
`message`, because codes such as `COLLECTION_NOT_FOUND` tell a model what to do
next.

### 6.3 Connection failures

The likeliest failure is the stack not running. On connection refused, DNS
failure or timeout, the server MUST return a message that:

- states the RAG API could not be reached, and at which base URL;
- suggests `docker compose ps` and `docker compose up -d`;
- does **not** surface a raw client exception as the entire message.

### 6.4 Classification

| Condition | Tool result |
|---|---|
| API unreachable | error, per §6.3 |
| API 4xx with envelope | error, code + message |
| API 5xx with envelope | error, code + message |
| `GET /health` returns 503 | **success**, returning the API's own body intact — the client must allow that status through rather than converting it to an error, or the per-service detail is lost |
| Path outside inbox | error naming the path and the contract |
| Ingest exceeds budget | **success**, with `job_id` and `status: running` |

---

## 7. Packaging

1. `mcp/Dockerfile` builds from `python:3.11-slim` — already present in both
   packages as the `api` base, so the marginal bundle cost is the MCP layer only.
2. Dependencies follow the existing discipline: `mcp/requirements.in` for direct
   dependencies, `mcp/requirements.txt` as the generated, fully pinned lock, and
   the Dockerfile installs the lock.
3. `mcp/.dockerignore` MUST exist, mirroring `api/.dockerignore`.
4. `package.sh` MUST include `mcp/` and `ingest-inbox/` (directory only).
5. `package-offline.sh` MUST add `rag-docker-mcp:latest` to its saved image list.
6. `install-offline.sh` requires no change — it loads whatever images the bundle
   contains and runs `docker compose up -d --no-build`, which does not start
   profile-gated services.

---

## 8. Acceptance criteria

Each is independently verifiable. The specification is met when all pass.

| # | Criterion | How it is checked |
|---|---|---|
| A1 | `docker compose up -d` starts 5 services, not 6 | `docker compose ps` shows no `mcp` |
| A2 | The server starts and lists exactly 15 tools | MCP `tools/list` over stdio |
| A3 | Tool names match §4 exactly | compare `tools/list` to the spec list |
| A4 | stdout contains only JSON-RPC | run with `RAG_MCP_LOG_LEVEL=DEBUG`; stdout parses as JSON-RPC, logs appear on stderr |
| A5 | `rag_health` succeeds against a healthy stack | call it; assert `status: ok` |
| A6 | `rag_health` succeeds against a **degraded** stack | stop `weaviate`; assert tool succeeds and reports the degradation |
| A7 | Unreachable API yields the §6.3 message | stop the stack; assert wording, not a raw exception |
| A8 | `rag_create_collection` returns the normalised name | create `casetest`; assert the tool returns `Casetest` |
| A9 | Query round-trip | create, ingest a file from the inbox, query, assert a non-empty answer and ≥1 citation |
| A10 | Path traversal is rejected | `../etc/passwd`, an absolute path and a symlink pointing outside all fail with the contract message |
| A11 | Ingest budget honoured | set `wait_seconds: 0`; assert a `job_id` and `status: running` come back |
| A12 | `rag_ingest_status` resumes that job | poll the returned `job_id` to completion |
| A13 | Gold standard returns immediately | call it; assert a `session_id` returns in well under one LLM call |
| A14 | Destructive annotation present | `tools/list` shows `destructiveHint: true` for `rag_delete_collection` only |
| A15 | Offline bundle carries the image | `package-offline.sh` output contains `rag-docker-mcp` |
| A16 | Works from a differently named directory | extract to `ragplatform/`, run the client command, assert `rag_health` succeeds |

---

## 9. Deferred

| Item | Reason |
|---|---|
| `rag_ingest_text` | Covered by the inbox for real cases; keeps the surface at 15 |
| MCP resources / prompts | Tools first; revisit once usage patterns are known |
| HTTP transport | No remote requirement yet |
| Linux support | Project is Apple Silicon throughout |
| Auth | API is anonymous by design |
