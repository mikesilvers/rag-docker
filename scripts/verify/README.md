# Verification suite

Integration tests that run the acceptance criteria in `SPECIFICATIONS.md` §10
and `RAG_EXPORT_SPECIFICATIONS.md` §13 against a live stack.

```bash
docker compose up -d          # they need the stack running

bash scripts/verify/all.sh                    # everything, ~20 min
RAG_SKIP_SLOW=1 bash scripts/verify/all.sh    # skip LLM work, ~3 min
bash scripts/verify/all.sh 02 04              # only the named suites
```

Exits non-zero if any check fails.

## Focused import validation regressions

Run `python3 scripts/tests/test_session_implementation.py` from the repository root to check that the embedded session/import/package service examples retain the current validated implementation.

`05_transfer.sh` registers both controlled regressions and the source-contract check, so `all.sh` runs them. Its E23 live checks use digest-valid synthetic packages to verify malformed metadata is refused before replacement and valid metadata is restored on rename.

`scripts/tests/test_session_import.py` exercises the real package reader and
evaluation persistence with disposable fixtures. Model and database mutation
seams are mocked; this complements the live transfer suite and does not prove
Weaviate/Ollama acceptance. Run it using the API image's pinned dependencies:

```bash
docker compose run --rm --no-deps \
  -v "$PWD/scripts/tests:/tests:ro" -e RAG_TEST_API_DIR=/app \
  api python /tests/test_session_import.py
```

Alternatively, with `uv` on the host:

```bash
uv run --no-project --python 3.11 \
  --with pydantic-settings==2.15.0 --with pydantic==2.13.5 \
  --with httpx==0.28.1 --with weaviate-client==4.23.1 \
  python scripts/tests/test_session_import.py
```

## Why integration tests

Every defect this project has actually produced was invisible to a unit test of
the same code:

- `.md` files could never be ingested — the `markdown` package was missing from
  the image, so `unstructured.partition.md` failed at import time. The function
  was correct.
- `PATCH /goldstandard/.../pair/...` returned 200 and silently rewrote approved
  content. The handler did exactly what it was written to do.
- Explicit vectors survive Weaviate's vectorizer — the entire import design
  rests on that, and only the database can confirm it.
- A recreated collection inherited a deleted one's chunking settings, because
  deletion removed two of the three things it should have.

These tests talk to the running API, the real Weaviate and the real model, and
drive the UI in a real browser.

## Layout

| File | Covers |
|---|---|
| `all.sh` | entry point; runs the suites and aggregates |
| `lib.sh` | shared helpers: checks, job polling, cleanup |
| `07_settings.sh` | registered live settings validation suite; invokes the standalone helper |
| `settings_validation.py` | standalone: `RAG_API=http://localhost:8080/api python3 scripts/verify/settings_validation.py`; invalid settings, valid defaults and saved round trips on a unique disposable collection; no LLM work |
| `lock.sh` | one verify run at a time: a second `all.sh` or suite exits 3 while another is running, because runs share collection names, scratch files and fixtures. Tested by `scripts/tests/test_verify_lock.sh` |
| `fixtures.py` | the test corpus — six file types plus edge cases, stdlib only |
| `01_infrastructure.sh` | §10.5 — ports, health, config lifecycle, startup sweeps |
| `02_ingest.sh` | §10.1 — six types, ZIP, five strategies, merge rule, partial failure |
| `03_query.sh` | §10.2 — four retrieval modes, citations, latencies, answer style |
| `08_overlap.sh` | called by suite02 (and thus all.sh); real parser/ingest/Weaviate text-storage check on an owned fixture with vectorization disabled; optional `RAG_OVERLAP_REAL_EMBEDDING=1` model acceptance |
| `overlap_chunks.py` | helper for suite08; asserts nonempty text/windows, exact coverage/overlap, tail bounds and pre-storage output limits |
| `04_goldstandard.sh` | §10.3 — generation, the 409 and 422 guards, export schema |
| `05_transfer.sh` | export/import/tuning — E5–E20, E23, E26 and E27; live metadata and model checks, controlled regressions and source drift |
| `../tests/test_session_import.py` | controlled import/persistence/generation regressions, registered by transfer |
| `../tests/test_session_implementation.py` | exact embedded source checks, registered by transfer |
| `06_ui.sh` + `browser/` | §10.4 — roles, gating, explainer, delete guard, help page |
| `model_integrity.py` | bundled-model byte checks with the pulled embedding model, using a temporary package/store |
| `validate_package.py` | one export package against `RAG_EXPORT_SPECIFICATIONS.md` §4 |
| `09_sampling.sh` | called by suite04 (and thus all.sh), including slow-skip runs; owned synthetic UUID selection without model calls |
| `chunk_sampling.py` | standalone inside disposable API: `python - < scripts/verify/chunk_sampling.py`; real SDK seeded selection on owned synthetic UUIDs with supplied vectors, no model calls |

`10_validity.sh` is called by suite05 (and thus all.sh). It runs
`session_validity.py` inside the disposable API, using an owned real collection,
synthetic retained pairs and in-process HTTP without startup sweeps or model calls.
It checks warning metadata, actual deletion marking and explicit historical export.

## Environment

| Variable | Default | Effect |
|---|---|---|
| `RAG_EXPECTED_PROXY_PORT` | `8080` | expected resolved/live proxy host port; use `18080` with a deliberate loopback test override |
| `RAG_API` | `http://localhost:8080/api` | where the API is |
| `RAG_SKIP_SLOW` | `0` | `1` skips everything that needs an LLM call |
| `RAG_ALLOW_RESTART` | `0` | `1` allows suites to restart the stack (persistence checks) |
| `RAG_GS_SAMPLE` | `3` | gold-standard pairs to generate |
| `RAG_FORMAT_TRIALS` | `3` | paired trials for the answer-length comparison |
| `RAG_NETWORK` | detected | compose network for the browser container |

## Writing a check

`check <name> <exit-status> [detail]` — pass `$?` straight in:

```bash
[ "$status" = completed ] && [ "$chunks" -gt 0 ]
check "the job stored chunks" $? "status=$status chunks=$chunks"
```

Two rules the hard way:

- **Never pipe an API response through `echo`.** Shells interpret backslash
  escapes, and an LLM-generated answer containing `\n` becomes invalid JSON.
  Pipe `curl` straight into `python3`, or capture to a file.
- **Make a failing assertion fail loudly.** A check that compares two empty
  strings passes and proves nothing. Several early versions of these tests
  passed vacuously — asserting on a selector that matched nothing, or comparing
  a count to itself.

## Bundled-model integrity

On a disposable stack with its embedding model already pulled, run:

```bash
docker compose exec -T api python - < scripts/verify/model_integrity.py
```

This verifies the real model's referenced bytes, copies them into a temporary
package/store, checks valid install and unchanged reuse, and refuses ordinary
mismatched bytes without changing the published manifest or healthy blobs.
The actual shared model store is only read; temporary content is removed. It
needs disk space for two copies of the embedding model and performs several
streamed hash passes. It does not invoke a model parser or claim trusted model
provenance. Controlled regression tests additionally cover digest grammar,
containment, interrupted publication and concurrent blob publication:

```bash
python -m unittest discover -s scripts/tests -p 'test_model_bundle.py'
```

## Cleaning up

Suites create collections prefixed `Vfy` (`RAG_TEST_PREFIX`) and remove them at
the end. If a run is interrupted:

```bash
curl -s localhost:8080/api/collections | python3 -c \
  "import json,sys;[print(c['name']) for c in json.load(sys.stdin)['collections']]" \
  | grep '^Vfy' | xargs -I{} curl -s -X DELETE "localhost:8080/api/collections/{}?confirm=true"
```

The infrastructure suite requires Docker Engine 28.0.0+ and checks both resolved Compose and live Docker bindings for a single loopback proxy publication. When deploying an alternate host port for testing, set `RAG_EXPECTED_PROXY_PORT` to that port as well as `RAG_API`. Its inspection files are kept in a private temporary directory removed on exit. The local profile assumes standard bridge/NAT routing.

Run `python3 scripts/tests/test_loopback_verification.py` from the repository root for the verifier's engine-version and adverse binding regressions. These exercise the actual assertion blocks and resolved default Compose; the live infrastructure suite still requires a running stack.
