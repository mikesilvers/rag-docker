# RAG Docker — MCP Server Implementation Plan

> **STATUS: PARKED (not wired into the project).**
> The MCP server is fully built and passed all 16 acceptance criteria, but it is
> deliberately not part of the running stack: no compose service, not in the
> offline bundle, nothing depends on it. The source is preserved in `./mcp` and
> these documents remain accurate as of that build.
>
> To resume: restore the compose service recorded in `MCP_IMPLEMENTATION.md` §7,
> add `rag-docker-mcp:latest` to the `IMAGES` array in `package-offline.sh`, and
> `docker compose build mcp`. No code changes are required.

**Status:** plan
**Implements:** `MCP_SPECIFICATIONS.md`
**Background:** `MCP_ANALYSIS.md`

---

## 1. Verified SDK facts

These were confirmed by installing the SDK and introspecting it, not from
memory. They matter because the obvious guesses are wrong.

| Fact | Value | Why it matters |
|---|---|---|
| Package | `mcp` on PyPI, latest **2.2.0**, requires Python ≥3.10 | Python 3.11 qualifies |
| Server class | `from mcp.server.mcpserver import MCPServer` | **`FastMCP` does not exist in 2.x** — it was renamed. Code written from v1 memory fails to import |
| Tool registration | `@server.tool(name=..., description=..., annotations=...)` | Decorator returns the function unchanged |
| Transport | `server.run(transport="stdio")` (the default) | Also `run_stdio_async()` for an existing event loop |
| Annotations type | `from mcp.types import ToolAnnotations` | Fields are **snake_case**: `read_only_hint`, `destructive_hint`, `idempotent_hint`, `open_world_hint` — the spec states the wire names (`readOnlyHint`), the SDK takes the Python names |
| HTTP client | `httpx` **0.28.1** | Already the pinned version in `api/requirements.txt`; reuse it for consistency |
| **Error surfacing** | `from mcp.server.mcpserver.exceptions import ToolError` | **Only a `ToolError`'s message reaches the client.** Anything else is re-raised as `UnexpectedToolError` with the bare text `Error executing tool <name>` and no detail. Raising `ValueError` or a custom exception silently discards the message |

If a future `mcp` release changes these, the pin in `mcp/requirements.txt` keeps
the build reproducible until someone deliberately bumps it.

---

## 2. Sequencing

Six phases, each independently verifiable. Do not start a phase before the
previous one passes its check.

| Phase | Deliverable | Verified by |
|---|---|---|
| 1 | Scaffold, dependencies, image builds | image builds; `python -c "import mcp"` |
| 2 | Config, API client, path validation | unit checks; A7, A10 |
| 3 | The 15 tools | A2, A3, A5, A8, A9, A11–A14 |
| 4 | Compose integration | A1, A4, A16 |
| 5 | Packaging | A15 |
| 6 | Full acceptance run | A1–A16 |

---

## 3. File manifest

```
mcp/
├── Dockerfile
├── .dockerignore
├── requirements.in          # direct deps (edit this)
├── requirements.txt         # generated lock (do not hand-edit)
├── server.py                # entrypoint: logging, imports, run()
├── mcpapp.py                # the shared MCPServer instance
├── logging_setup.py         # stderr-only logging; imported before the SDK
├── config.py                # env-var settings
├── ragclient.py             # httpx wrapper + error translation
├── paths.py                 # inbox path validation
└── tools/
    ├── __init__.py
    ├── query.py
    ├── collections.py
    ├── ingest.py
    ├── goldstandard.py
    └── diagnostics.py
ingest-inbox/
└── .gitkeep
```

**Naming trap.** The build context is `./mcp`, and the Dockerfile copies its
*contents* into `/app`. That is deliberate: if a directory named `mcp/` ended up
on `sys.path` inside the image, it would shadow the installed `mcp` package and
every SDK import would fail with a confusing error. Never add an `mcp/__init__.py`
at the image root, and never `COPY` the folder itself into `/app/mcp`.

---

## 4. Phase 1 — Scaffold

### 4.1 `mcp/requirements.in`

```
mcp>=2.2,<3
httpx>=0.28,<0.29
```

Bounded upper ranges, because the SDK's 1.x→2.x rename shows majors are
breaking here.

### 4.2 `mcp/requirements.txt`

Generated, never hand-edited, following the existing discipline:

```bash
docker compose build mcp
docker run --rm rag-docker-mcp:latest pip freeze \
  | LC_ALL=C sort > /tmp/pins.txt
awk '/^[a-zA-Z0-9]/{exit} {print}' mcp/requirements.txt > /tmp/header.txt
cat /tmp/header.txt /tmp/pins.txt > mcp/requirements.txt
```

`LC_ALL=C` keeps ordering stable so a re-lock produces a clean diff.

### 4.3 `mcp/Dockerfile`

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# requirements.txt is a generated lock; edit requirements.in instead.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# stdio transport: the client speaks JSON-RPC over stdin/stdout.
# Unbuffered so responses are not held in a pipe buffer.
ENV PYTHONUNBUFFERED=1
CMD ["python", "server.py"]
```

No `curl`, no build tools — nothing here needs them, and a smaller image keeps
the offline bundle lean.

### 4.4 `mcp/.dockerignore`

Mirror `api/.dockerignore`: `__pycache__/`, `*.py[cod]`, `.venv/`, `.DS_Store`,
`.env`, `Dockerfile`, `.dockerignore`.

**Phase 1 check:** `docker compose build mcp` succeeds and
`docker run --rm --entrypoint python rag-docker-mcp:latest -c "from mcp.server.mcpserver import MCPServer; print('ok')"`
prints `ok`.

---

## 5. Phase 2 — Config, client, paths

### 5.1 `config.py`

```python
import os

RAG_API_BASE_URL = os.getenv("RAG_API_BASE_URL", "http://api:8000").rstrip("/")
INGEST_ROOT = os.getenv("RAG_MCP_INGEST_ROOT", "/host")
INGEST_WAIT_SECONDS = int(os.getenv("RAG_MCP_INGEST_WAIT_SECONDS", "120"))
HTTP_TIMEOUT = float(os.getenv("RAG_MCP_HTTP_TIMEOUT", "300"))
LOG_LEVEL = os.getenv("RAG_MCP_LOG_LEVEL", "INFO").upper()
```

The api service serves routes at the **root** (`/health`), not under `/api`.
That prefix belongs to the nginx proxy, which this server bypasses entirely.

### 5.2 `logging_setup.py` — the highest-risk detail

Spec §5, rule 1 requires stdout to carry JSON-RPC only. Configure this **before**
anything else runs, and pin the stream explicitly rather than trusting defaults:

```python
import logging, sys

def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stderr)   # never sys.stdout
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
```

Review rule: no bare `print()` anywhere under `mcp/`. A single stray print
corrupts the protocol stream and presents as an unexplained client hang.

### 5.3 `ragclient.py`

One `httpx.AsyncClient`, one error-translation path (spec §6):

```python
class RagApiError(Exception):
    """Carries an API error code and message through to the tool result."""

async def request(method: str, path: str, **kw):
    url = f"{config.RAG_API_BASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=config.HTTP_TIMEOUT) as c:
            r = await c.request(method, url, **kw)
    except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout) as e:
        raise RagApiError(
            f"Could not reach the RAG API at {config.RAG_API_BASE_URL}. "
            "Is the stack running? Check with 'docker compose ps' and start it "
            "with 'docker compose up -d'."
        ) from e
    if r.status_code >= 400:
        raise RagApiError(_format_api_error(r))   # code + message, per §6.2
    return r.json()
```

`_format_api_error` reads `{"error": {"code", "message"}}` when present and
falls back to the status line when it is not — some FastAPI validation errors
use `{"detail": [...]}` instead of the project envelope, and that shape must not
produce a `KeyError`.

### 5.4 `paths.py` — inbox validation

Implements spec §4.3 exactly. The order matters: canonicalise **before**
comparing, or a symlink escapes the check.

```python
def resolve_inbox_path(rel: str) -> Path:
    if os.path.isabs(rel):
        raise ValueError(f"'{rel}' is an absolute path. Give a path relative to the ingest inbox.")
    root = Path(config.INGEST_ROOT).resolve()
    candidate = (root / rel).resolve()          # resolves symlinks and '..'
    if root not in candidate.parents and candidate != root:
        raise ValueError(f"'{rel}' resolves outside the ingest inbox. Place the file in ./ingest-inbox/ first.")
    if not candidate.is_file():
        raise ValueError(f"'{rel}' was not found in the ingest inbox, or is not a regular file.")
    return candidate
```

**Phase 2 check:** covers A7 and A10 — traversal, absolute path and
outside-pointing symlink all rejected with the contract message.

---

## 6. Phase 3 — The 15 tools

### 6.1 Registration pattern

```python
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer(name="rag", version="1.2.0")

@server.tool(
    name="rag_query",
    description=(
        "Ask a question against a collection using retrieval-augmented "
        "generation. Only the first letter of a collection name is normalised; "
        "the rest is case-sensitive. Typically takes ~15 seconds."
    ),
    annotations=ToolAnnotations(read_only_hint=True, destructive_hint=False),
)
async def rag_query(question: str, collection: str,
                    retrieval_mode: str = "hnsw", top_k: int = 5,
                    alpha: float = 0.75, include_citations: bool = True,
                    response_format: str = "end_user") -> dict:
    ...
```

Input schemas come from the type hints; validate enums and numeric bounds
explicitly and raise `ValueError` with a message naming the allowed values.

### 6.2 `server.py` and `mcpapp.py` — the entrypoint

**Correction made during the build.** The sketch below originally created the
`MCPServer` inside `server.py` and had the tool modules import it back from
there — a circular import that works only by accident of import ordering. The
instance therefore lives in its own module, `mcpapp.py`, which the tool modules
and `server.py` both import. It is named `mcpapp` rather than `mcp` so it cannot
shadow the installed SDK package.

Responsibilities, in this order. The order is not cosmetic: logging must be
configured before any import that might log, or the first log line lands on
stdout and corrupts the stream.

```python
import config
from logging_setup import configure_logging

configure_logging(config.LOG_LEVEL)          # 1. FIRST, before anything logs

from mcp.server.mcpserver import MCPServer    # 2. then the SDK
server = MCPServer(name="rag", version="1.2.0",
                   instructions="Tools for a local retrieval-augmented "
                                "generation stack: query, manage collections, "
                                "ingest documents and build evaluation sets.")

import tools.diagnostics, tools.collections   # 3. registration by import
import tools.ingest, tools.query, tools.goldstandard

if __name__ == "__main__":
    server.run(transport="stdio")             # 4. blocks on stdio
```

The tool modules register themselves against the shared `server` object on
import, so adding a module to that import list is the only wiring step. Keep
`configure_logging` in its own module so importing it cannot pull in the SDK.

`instructions` is worth writing carefully — it is the server-level description an
MCP client shows when deciding whether these tools are relevant.

### 6.3 Ordering

Build in dependency order so each step is testable against a live stack:

1. `diagnostics.py` — `rag_health`, `rag_metrics`. Smallest, and proves
   connectivity end to end (A5, A6).
2. `collections.py` — list, create, delete. `create` **must read the name back**
   from `GET /collections` rather than echoing the input (spec §4.2, A8).
3. `ingest.py` — `rag_ingest_files`, `rag_ingest_status`, and the two config
   tools. Contains the bounded-polling loop.
4. `query.py` — `rag_query`. Needs a populated collection, so it comes after
   ingest (A9).
5. `goldstandard.py` — the five gold-standard tools.

### 6.4 Error surfacing (normative)

Every anticipated, caller-facing failure MUST be raised as a `ToolError`. The SDK
treats anything else as a crash and replaces the message with
`Error executing tool <name>`, so the API error code, the inbox path contract and
the "stack is not running" hint would all be discarded.

This is enforced by making the two failure types subclass it, rather than by
remembering to catch at each call site:

```python
from mcp.server.mcpserver.exceptions import ToolError

class RagApiError(ToolError): ...      # ragclient.py
class InboxPathError(ToolError): ...   # paths.py
```

Argument validation raised inside a tool body must also use `ToolError`, not
`ValueError`.

`rag_health` is the one endpoint that must not raise on a 4xx/5xx: the API
answers `503` with a full body naming the failing component. `ragclient.request`
takes an `allow_status` tuple so that status returns normally and the structured
body reaches the caller intact.

### 6.5 Bounded polling (spec §4.3)

```python
deadline = time.monotonic() + wait_seconds
while time.monotonic() < deadline:
    status = await ragclient.request("GET", f"/ingest/job/{job_id}")
    if status["status"] in ("completed", "failed"):
        return status
    await asyncio.sleep(2)
return {**status, "note": "Still running. Poll rag_ingest_status with this job_id."}
```

`wait_seconds: 0` must return the handle immediately without a first sleep —
that is exactly what A11 checks.

`rag_generate_goldstandard` does **not** poll at all; it returns the
`session_id` from the initial response (A13).

### 6.6 Annotation mapping

The spec states wire names; the SDK takes Python names:

| Spec | SDK |
|---|---|
| `readOnlyHint` | `read_only_hint` |
| `destructiveHint` | `destructive_hint` |
| `idempotentHint` | `idempotent_hint` |

Only `rag_delete_collection` sets `destructive_hint=True` (A14).

---

## 7. Phase 4 — Compose integration

Add to `docker-compose.yml`:

```yaml
  mcp:
    build: ./mcp
    image: rag-docker-mcp:latest
    profiles: ["mcp"]          # never starts on a plain `docker compose up -d`
    networks: [rag-internal]
    volumes:
      - ./ingest-inbox:/host:ro
    environment:
      RAG_API_BASE_URL: http://api:8000
      RAG_MCP_INGEST_ROOT: /host
      RAG_MCP_INGEST_WAIT_SECONDS: 120
      RAG_MCP_HTTP_TIMEOUT: 300
      RAG_MCP_LOG_LEVEL: INFO
    stdin_open: true
    tty: false
```

No `depends_on` — spec §2.1 explains why (it would hang the MCP client at
launch). `tty: false` is required: a TTY corrupts JSON-RPC framing.

Create `ingest-inbox/.gitkeep` so the mount target exists on a fresh extraction.

**Phase 4 check:** `docker compose up -d` starts **five** services (A1), and the
client command works from a differently named directory (A16).

---

## 8. Phase 5 — Packaging

| File | Change |
|---|---|
| `package.sh` | No change needed — it archives the whole tree. Confirm `mcp/` and `ingest-inbox/` are present in the output and that inbox *contents* are excluded |
| `package-offline.sh` | Add `rag-docker-mcp:latest` to the `IMAGES` array, and recreate `ingest-inbox/` in the staging directory — excluding the inbox contents also drops the directory, and without it Docker creates the bind-mount source as root on the target |
| `package.sh` | Exclude `ingest-inbox/*` but re-add `ingest-inbox/.gitkeep` after the main zip, for the same reason |
| `install-offline.sh` | No change — `docker load` takes whatever the bundle holds, and `up -d --no-build` does not start profile-gated services |
| `README.md` | New section: client configuration, the inbox contract, the 15 tools |

**Phase 5 check:** A15 — the bundle's image list contains `rag-docker-mcp`.

---

## 9. Phase 6 — Acceptance

Each criterion maps to a concrete action. A1–A16 are defined in spec §8.

| # | How to run it |
|---|---|
| A1 | `docker compose up -d && docker compose ps` → 5 services, no `mcp` |
| A2/A3 | `tools/list` over stdio → exactly the 15 names in spec §4 |
| A4 | Launch with `RAG_MCP_LOG_LEVEL=DEBUG`; assert stdout parses as JSON-RPC and log lines appear on stderr |
| A5 | `rag_health` → `status: ok` |
| A6 | `docker compose stop weaviate`; `rag_health` **succeeds** reporting degraded; restart |
| A7 | `docker compose down`; any tool → the §6.3 message, not a raw exception |
| A8 | `rag_create_collection("casetest")` → returns `Casetest` |
| A9 | Place a file in `ingest-inbox/`, ingest, query → non-empty answer, ≥1 citation |
| A10 | `../etc/passwd`, `/etc/passwd`, and a symlink to `/etc` → all rejected |
| A11 | `rag_ingest_files(wait_seconds=0)` → `job_id` + `status: running` |
| A12 | `rag_ingest_status(job_id)` → reaches `completed` |
| A13 | `rag_generate_goldstandard` returns a `session_id` in ≪ one LLM call |
| A14 | `tools/list` → `destructive_hint` true for `rag_delete_collection` only |
| A15 | `bash package-offline.sh` → bundle image list contains `rag-docker-mcp` |
| A16 | Extract to `ragplatform/`, run the client command there, `rag_health` succeeds |

A6, A7 and A10 are the ones most likely to be skipped and most likely to matter:
they are the failure paths a user actually hits.

---

## 10. Risks during implementation

| Risk | Symptom | Response |
|---|---|---|
| A stray `print()` | Client hangs with no error | Grep for `print(` under `mcp/` before every commit; A4 catches it |
| SDK API drift | `ImportError` on `MCPServer` | Pinned lock; bump deliberately, re-run A2 |
| `mcp/` shadowing the SDK package | `ImportError` inside the image | Never create `/app/mcp/`; §3 explains |
| Symlink escape from the inbox | Reads outside the mount | Canonicalise before comparing; A10 |
| Tool timeout on `rag_query` | Client reports failure on a working query | `RAG_MCP_HTTP_TIMEOUT` default 300 s, well above the ~15 s observed |
| Bundle growth | Offline archive larger | Shares the `python:3.11-slim` base with `api`; measure before and after |

---

## 11. Definition of done

- All 16 acceptance criteria pass.
- `mcp/requirements.txt` is a generated lock and reproduces byte-identically from
  the documented command.
- `docker compose up -d` still starts exactly five services.
- The offline bundle installs and `rag_health` succeeds from a differently named
  directory with no network access.
- `README.md` documents client configuration and the inbox contract.
