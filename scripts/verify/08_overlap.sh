#!/usr/bin/env bash
# Real parser/window/ingest/storage acceptance on an owned text-only fixture.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
require_stack
section "Bounded overlap text storage"
(cd ../.. && docker compose exec -T -e RAG_OVERLAP_REAL_EMBEDDING="${RAG_OVERLAP_REAL_EMBEDDING:-0}" api python - < scripts/verify/overlap_chunks.py)
check "overlap coverage, bounds, budget rejection and owned-fixture cleanup" $?
summary
