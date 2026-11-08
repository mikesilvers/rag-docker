#!/usr/bin/env bash
# Exact-record reindex and unavailable-embedding acceptance.
set -uo pipefail
cd "$(dirname "$0")" && . ./lib.sh
bindings=$(cd ../.. && docker compose port proxy 80) || exit 2
python3 ./compose_target.py "$API" "$bindings" || exit 2
require_stack
section "Exact-record reindex"
python3 ../../scripts/tests/test_reindex_preservation.py
check "reindex embedded sources match runtime" $?
(cd ../.. && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/tests/test_collection_writes.py)
check "collection writer barrier regressions" $?
# Transport only these three controlled sources into a temporary API directory.
# The tests exercise the actual helper and parent cleanup function with mocks.
(cd ../.. && python3 -c 'import sys,tarfile; t=tarfile.open(fileobj=sys.stdout.buffer,mode="w|"); [t.add("scripts/verify/"+name,arcname=name) for name in ("reindex.py","lib.sh","reindex_verifier_cases.py")]; t.close()' | docker compose exec -T api python -c 'import os,sys,tarfile,tempfile,subprocess; task=tempfile.TemporaryDirectory(prefix="owned-reindex-tests-"); tarfile.open(fileobj=sys.stdin.buffer,mode="r|*").extractall(task.name,filter="data"); result=subprocess.run([sys.executable,task.name+"/reindex_verifier_cases.py"],env={**os.environ,"RAG_TEST_API_DIR":"/app","RAG_REINDEX_VERIFIER_SOURCE":task.name+"/reindex.py","RAG_VERIFIER_LIB":task.name+"/lib.sh"}); task.cleanup(); sys.exit(result.returncode)')
check "verifier async ownership and parent cleanup regressions" $?
(cd ../.. && docker compose exec -T -e RAG_TEST_API_DIR=/app api python - < scripts/verify/reindex_cases.py)
check "owned reindex preservation and failure regressions" $?
(cd ../.. && docker compose exec -T -e RAG_TEST_PREFIX="$PREFIX" api python - < scripts/verify/reindex.py)
check "real reindex with embedding endpoint unavailable" $?
summary
