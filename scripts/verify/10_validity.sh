#!/usr/bin/env bash
# Retained-session validity and explicit historical export, without model calls.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
require_stack
section "Retained evaluation validity and historical export"
(cd ../.. && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/session_validity.py)
check "legacy defaults, real stale/orphan markers, export guard and historical file" $?
summary
