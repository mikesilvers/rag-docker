#!/usr/bin/env bash
# UUID selection through the real SDK; owned fixtures and supplied vectors.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
require_stack
section "Evaluation sampling across iterator pages"
(cd ../.. && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/chunk_sampling.py)
check "seeded selection, iterator paging, bounds and owned-fixture cleanup" $?
section "Generation request validation before backend/model work"
for body in \
  '{"collection":"MissingSamplingFixture","sample_size":0}' \
  '{"collection":"MissingSamplingFixture","sample_size":101}' \
  '{"collection":"MissingSamplingFixture","sample_size":true}' \
  '{"collection":"MissingSamplingFixture","sample_size":1.5}' \
  '{"collection":"MissingSamplingFixture","seed":false}' \
  '{"collection":"MissingSamplingFixture","seed":1.5}' \
  '{"collection":"MissingSamplingFixture","seed":NaN}' \
  '{"collection":"MissingSamplingFixture","sample_size":Infinity}'
do
  code=$(api_post_code /goldstandard/generate "$body")
  check_eq "invalid sampling request rejected with422: $body" "$code" "422"
done
summary
