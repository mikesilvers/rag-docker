#!/usr/bin/env bash
# Observe physical config and execute top-K on real backend, controlling models.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
proxy_bindings=$(cd ../.. && docker compose port proxy 80) || exit 2
python3 ./compose_target.py "$API" "$proxy_bindings" || exit 2
require_stack
section "Physical index and effective query controls"
(cd ../.. && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/retrieval_controls.py)
check "physical index, legacy vector aliases, executed topK and config lifecycle" $?
summary
