#!/usr/bin/env bash
# Durable session edits, generation interleaving and visible recovery diagnostics.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
bindings=$(cd ../.. && docker compose port proxy 80) || exit 2
python3 ./compose_target.py "$API" "$bindings" || exit 2
require_stack
section "Durable evaluation session updates"
(cd ../.. && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/verify/session_persistence_cases.py)
check "owned persistence failure, concurrency and interruption regressions" $?
(cd ../.. && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/session_persistence.py)
check "concurrent HTTP edits, fresh-process reload, write failures and diagnostics" $?
summary
