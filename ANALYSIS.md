# RAG Docker — Project Analysis

**Version:** 1.2  
**Date:** 2026-09-10  
**Status:** Final

---

## 1. Executive Summary

This project delivers a self-contained, Dockerized Retrieval-Augmented Generation (RAG) platform. It combines a vector database (Weaviate), a local small language model (Phi-3.5 Mini via Ollama), and a multi-role web interface into a single deployable unit. The system is designed for internal trusted networks, targets thousands of documents initially, and is architected from the outset for future extraction and deployment to other server environments.

---

## 2. Goals and Non-Goals

### Goals

- Ingest, chunk, embed, and store documents from multiple source types (PDF, DOCX, TXT, Markdown, CSV, JSON).
- Provide configurable chunking strategies with user-friendly explanations.
- Provide configurable retrieval strategies (ANN, KNN, HNSW, Hybrid) with user-friendly explanations.
- Serve RAG queries through a local LLM (Phi-3.5 Mini) that reformulates prompts, synthesizes retrieved context, and formats responses.
- Offer a role-based web interface for three audiences: AI Engineer, Developer, and End User.
- Provide a gold standard creation tool (AI-assisted) that outputs RAGAS-compatible evaluation data.
- Run entirely on a trusted internal network with no external dependencies.
- Be portable: the Docker Compose stack can be extracted and deployed to other Linux servers in the future.

### Non-Goals (Current Phase)

- Multi-user authentication or access control.
- Multi-node Weaviate clustering (deferred to future phase).
- Internet access or external API calls from within the container stack.
- Support for document types beyond the initial six (PDF, DOCX, TXT, MD, CSV, JSON).
- Real-time streaming of LLM responses (may be added as enhancement).
- GPU requirement — the system must run on CPU, with GPU as an optional accelerator.

---

## 3. Architecture Overview

The system is a Docker Compose stack of five services, communicating over an internal Docker network. No service is exposed to external networks except through a single reverse-proxy port.

```
┌─────────────────────────────────────────────────────────────┐
│                    Docker Compose Stack                     │
│                                                             │
│  ┌─────────────┐          ┌─────────────┐                  │
│  │  Weaviate   │          │   Ollama    │                  │
│  │ (vector DB) │          │ (Phi-3.5 +  │                  │
│  │  Port 8080  │          │  nomic-emb) │                  │
│  └──────▲──────┘          │  Port 11434 │                  │
│         │                 └──────▲──────┘                  │
│         │                        │                         │
│         └──────────┬─────────────┘                         │
│                    │                                        │
│            ┌───────┴───────┐                               │
│            │  Ingest/API   │                               │
│            │   (FastAPI)   │                               │
│            │   Port 8000   │                               │
│            └───────▲───────┘                               │
│                    │                                        │
│            ┌───────┴───────┐                               │
│            │   Web UI      │                               │
│            │   (React)     │                               │
│            │   Port 3000   │                               │
│            └───────────────┘                               │
└─────────────────────────────────────────────────────────────┘
                        │
              Nginx Reverse Proxy
                 Port 80 (host)
```

### Services

| Service | Technology | Purpose |
|---|---|---|
| **weaviate** | Weaviate OSS (latest stable) | Vector store, ANN/HNSW/hybrid search, schema management |
| **ollama** | Ollama | Hosts Phi-3.5 Mini (LLM) and nomic-embed-text (embeddings) |
| **api** | Python / FastAPI | Ingest pipeline, RAG query orchestration, gold standard generation |
| **ui** | React (Vite) | Role-based web interface |
| **proxy** | Nginx | Single entry point, routes /api → api service, / → ui |

---

## 4. Component Deep-Dive

### 4.1 Vector Store — Weaviate

Weaviate is chosen because:
- Native support for vector + scalar (hybrid) search in a single query.
- Clean Python client (weaviate-client v4).
- HNSW index is the default and is configurable at collection-creation time.
- Schema supports multiple named collections, enabling future multi-domain expansion.
- Data is persisted to a Docker volume for durability.
- Extraction path: the volume can be tarred and restored on any Weaviate instance.

**Retrieval modes supported:**

| Mode | Weaviate Mechanism | Notes |
|---|---|---|
| ANN (approximate) | `near_vector` with HNSW | Default, fastest |
| KNN (exact) | `near_vector` with flat index | Accurate, slower; useful for small corpora |
| HNSW tuning | `ef`, `efConstruction`, `maxConnections` | Exposed in Engineer role UI |
| Hybrid | `hybrid` query (BM25 + vector) | Best recall for keyword-heavy domains |
| Semantic only | `near_text` via `text2vec-ollama` module | Pure meaning-based retrieval; requires Weaviate text2vec-ollama module enabled |

### 4.2 Local LLM — Phi-3.5 Mini via Ollama

**Model:** `phi3.5` (3.8B parameters, Q4_K_M quantization)  
**Served by:** Ollama (REST API on port 11434)  
**Embedding model:** `nomic-embed-text` (via same Ollama instance)

**Why Phi-3.5 Mini:**
- Instruction-tuned specifically for reasoning over supplied text — ideal for RAG synthesis.
- 128K token context window accommodates large retrieved chunks.
- 3.8B at Q4 quantization runs in ~3GB RAM — acceptable on CPU-only hosts.
- Runs entirely offline; model weights are pulled once on first startup and cached in the `ollama_models` Docker volume — subsequent starts are instant.

**Why nomic-embed-text:**
- 768-dimension embeddings with 8192-token context (handles long document chunks).
- Served through the same Ollama instance — no separate embedding service needed.
- Consistent infrastructure: one model server, two model endpoints.

**LLM Roles in the Pipeline:**
1. **Query reformulation:** Rewrite user question for better vector retrieval.
2. **Context synthesis:** Combine retrieved chunks into a coherent answer.
3. **Response formatting:** Format answer per role (verbose for engineers, plain language for end users).
4. **Gold standard generation:** Given a document chunk, generate candidate Q&A pairs for human review.

### 4.3 Ingest & API Service — FastAPI

The API service handles all data movement and serves the web UI's backend. It uses:

- **Unstructured** — universal document parser for all six input types. Handles OCR in PDFs, table extraction from DOCX, etc.
- **LangChain** — text splitter library for chunking strategies (wraps around Unstructured output).
- **weaviate-client v4** — Python client for all Weaviate operations.
- **httpx** — async HTTP client for Ollama API calls.

**Key API surface:**

| Endpoint Group | Purpose |
|---|---|
| `POST /ingest/upload` | Accept document upload, run parse → chunk → embed → store pipeline |
| `GET/POST /ingest/config` | Get or set chunking strategy and parameters |
| `POST /query` | Execute RAG query: reformulate → retrieve → synthesize |
| `GET /collections` | List Weaviate collections |
| `POST /goldstandard/generate` | Generate AI-assisted Q&A pairs from a collection |
| `POST /goldstandard/save` | Save reviewed gold standard to RAGAS format |
| `GET /health` | Health check for all downstream services |

### 4.4 Web UI — React (Role-Based)

The UI is a single React application with role selection on the landing page. The selected role is stored in browser session storage. Navigation and available features change based on role.

**Roles:**

| Role | Access |
|---|---|
| **AI Engineer** | All features: chunking config, retrieval config, HNSW parameters, gold standard, Q&A, collection management, health dashboard |
| **Developer** | Ingest, chunking config (simplified), retrieval config (simplified), Q&A, gold standard |
| **End User** | Q&A interface only |

**Key UI pages:**

| Page | Roles | Description |
|---|---|---|
| Landing / Role Select | All | Role picker, persisted to session |
| Q&A Interface | All | Ask questions, see answers with source citations |
| Document Import | Engineer, Developer | Upload files, set ingest pipeline config |
| Chunking Config | Engineer, Developer | Select strategy, set parameters, read explanation |
| Retrieval Config | Engineer, Developer | Select retrieval mode, set parameters, read explanation |
| Gold Standard | Engineer, Developer | AI-generates Q&A pairs; human reviews, edits, approves; export to RAGAS |
| Collection Manager | Engineer | Create/delete Weaviate collections, view stats |
| Health Dashboard | Engineer | Service status, model availability, index stats |

---

## 5. Chunking Strategies

Each strategy is available in the UI with a plain-language explanation visible to Engineer and Developer roles. End Users see only the Q&A interface and are not exposed to chunking configuration.

| Strategy | Implementation | Plain-language Explanation |
|---|---|---|
| **Fixed Size** | `CharacterTextSplitter` (LangChain) | Splits document into equal-sized pieces by character count. Simple and predictable, but may cut sentences mid-thought. |
| **Fixed Size with Overlap** | `CharacterTextSplitter` with `chunk_overlap` | Same as fixed size, but each piece shares some text with the next. Helps preserve context across boundaries. |
| **Semantic** | Sentence embeddings + cosine similarity threshold | Groups sentences that are about the same topic together. Slower but produces more meaningful chunks. |
| **Context-Aware** | Unstructured element types (titles, tables, list items) | Uses the document's own structure (headings, paragraphs, tables) to define chunk boundaries. Best for structured documents like reports. |
| **Language-Based** | `RecursiveCharacterTextSplitter` with language separators | Splits on natural language boundaries (sentences, paragraphs). Balanced option for narrative text. |

**Tunable parameters per strategy:**
- Chunk size (tokens or characters)
- Overlap size
- Similarity threshold (semantic only)
- Minimum chunk size (filter out fragments)

---

## 6. Retrieval Strategies

Each strategy is available in the UI with a plain-language explanation.

| Strategy | Plain-language Explanation |
|---|---|
| **HNSW — Approximate (default)** | Fast approximate nearest-neighbor search using a navigable graph index. The standard choice for most use cases — high speed, high accuracy. This is the recommended default. |
| **Flat — Exact (KNN)** | Scans every stored chunk for the mathematically closest match. Perfectly accurate but slower as the collection grows. Best for small collections or when precision is critical. |
| **Hybrid (BM25 + Vector)** | Combines keyword matching with meaning-based search. Best when queries contain specific terms, product names, or codes that pure meaning-search might miss. |
| **Semantic (near_text)** | Pure meaning-based search. Best for conceptual questions where the exact words matter less than the idea. Requires the text2vec-ollama Weaviate module. |

Note: ANN (Approximate Nearest Neighbor) is the general class of algorithm; HNSW is Weaviate's ANN implementation. In the UI, users select the index type and retrieval mode — not the underlying algorithm class.

**Tunable parameters:**
- `top_k` — number of chunks to retrieve
- `alpha` — hybrid search balance between vector and BM25 (0.0 = pure keyword, 1.0 = pure vector)
- `ef` / `efConstruction` / `maxConnections` — HNSW index parameters (Engineer role only)
- Distance metric — cosine (default), dot product, L2 euclidean

---

## 7. Gold Standard Tool

The gold standard tool is AI-assisted and outputs RAGAS-compatible evaluation sets.

**Workflow:**
1. User selects a collection and sample size.
2. API samples chunks from Weaviate.
3. For each chunk, Phi-3.5 Mini generates: one question, one reference answer, and the source context.
4. UI presents each generated pair for human review: accept, edit, or reject.
5. Approved pairs are exported as a JSON file in RAGAS format.

**RAGAS output format:**
```json
{
  "question": "...",
  "answer": "...",
  "contexts": ["..."],
  "ground_truth": "..."
}
```

**Future use:** The exported file feeds directly into the RAGAS evaluation framework to measure faithfulness, answer relevancy, context precision, and context recall against the live RAG system.

---

## 8. Data Model

### Weaviate Collection Schema

Each ingested document set is stored in a named Weaviate collection. Default collection name: `Documents`.

**Properties:**

| Property | Type | Description |
|---|---|---|
| `content` | text | The chunk text |
| `source_file` | text | Original filename |
| `source_type` | text | PDF, DOCX, TXT, MD, CSV, JSON |
| `chunk_index` | int | Position of chunk within source document |
| `chunk_strategy` | text | Strategy used at ingest time |
| `chunk_size` | int | Configured chunk size |
| `chunk_overlap` | int | Configured overlap size (0 if not applicable) |
| `created_at` | date | Ingest timestamp |

**Vector:** 768-dimension float array from nomic-embed-text.  
**Index:** HNSW (default), configurable to flat (KNN) at collection creation.

---

## 9. Deployment Model

### Current Phase — Fully Containerized

Single `docker-compose.yml` with named volumes for persistence:

| Volume | Contents |
|---|---|
| `weaviate_data` | Weaviate vector store data |
| `ollama_models` | Downloaded model weights (Phi-3.5 Mini, nomic-embed-text) |
| `ingest_uploads` | Uploaded documents pending or completed ingest |

**Startup sequence:**
1. Weaviate starts, waits for ready.
2. Ollama starts, pulls models if not present in volume, waits for ready.
3. API service starts, verifies Weaviate and Ollama connectivity.
4. UI service starts.
5. Nginx proxy starts.

### Future Phase — Extraction

The architecture is designed to minimize extraction friction:
- Weaviate data is in a single volume — portable via tar/restore.
- Ollama models are in a single volume — portable or re-pulled.
- The API service is a standard FastAPI app — runs outside Docker with `uvicorn`.
- The UI is a static build — serves from any web server.
- Compose file can be adapted for remote hosts by changing volume mounts.

---

## 10. Technology Stack Summary

| Layer | Technology | Version Target |
|---|---|---|
| Container runtime | Docker + Docker Compose | Docker 24+, Compose v2 |
| Base OS | Linux (Debian Slim in containers) | Debian 12 |
| Vector store | Weaviate OSS | 1.25+ |
| LLM runtime | Ollama | Latest stable |
| LLM model | Phi-3.5 Mini (Q4_K_M) | microsoft/phi3.5 via Ollama |
| Embedding model | nomic-embed-text | v1.5 via Ollama |
| API framework | FastAPI + Uvicorn | Python 3.11 |
| Document parsing | Unstructured | 0.14+ |
| Text splitting | LangChain text splitters | 0.3+ |
| Vector client | weaviate-client | v4 |
| Web UI | React + Vite | React 18, Vite 8 |
| UI component library | shadcn/ui + Tailwind CSS | Latest |
| Reverse proxy | Nginx | Alpine |

---

## 11. Constraints and Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| Phi-3.5 Mini too slow on CPU for real-time Q&A | Medium | Q4 quantization keeps model in 3GB RAM; response latency acceptable for single-user use |
| Unstructured OCR accuracy on poor-quality PDFs | Medium | Expose confidence scores in UI; allow re-ingest with different settings |
| Weaviate schema changes requiring re-ingestion | Low | Version collection schema; document migration path |
| nomic-embed-text context overflow on large chunks | Low | Chunk size defaults capped at 512 tokens; user warned if chunk_size > 2048 |
| Docker volume data loss | Low | Document backup procedure; future: Weaviate backup API |

---

## 12. Resolved Questions

| # | Question | Decision | Rationale |
|---|---|---|---|
| 1 | Source citations in Q&A | Optional toggle (off by default) | Keeps End User view clean; Engineers/Developers can enable for debugging |
| 2 | Document upload scope | Single file AND batch (folder/zip) | Both modes required in v1 |
| 3 | Gold standard regeneration | Individual pair regeneration | More efficient — preserves already-approved pairs, saves LLM compute, faster iteration |
| 4 | Health Dashboard metrics | Full latency metrics (not just up/down) | Allows users to measure performance impact of configuration changes |
| 5 | RAGAS export filename | Default: `{collection}_{timestamp}.json`; optional user-defined override | Predictable default with traceability; user override for pipeline integration |

---

*End of Analysis — Version 1.2*
