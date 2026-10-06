#!/usr/bin/env bash
# Independent imported evaluation identities and real rename round trips.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
bindings=$(cd ../.. && docker compose port proxy 80) || exit 2
python3 ./compose_target.py "$API" "$bindings" || exit 2
require_stack
section "Imported evaluation session identities"
python3 ../tests/test_session_identity.py
check "embedded identity sources match" $?
(cd ../.. && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/verify/session_identity_cases.py)
check "owned identity collision, preservation and allocation regressions" $?
(cd ../.. && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/session_identity.py)
check "real export/edit/import-twice lookup and RAGAS roundtrip" $?
summary
