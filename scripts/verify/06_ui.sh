#!/usr/bin/env bash
# SPECIFICATIONS.md §10.4 — Web UI, driven by a real headless browser.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
require_stack

IMAGE="rag-verify-browser:latest"
NETWORK="${RAG_NETWORK:-}"
if [ -z "$NETWORK" ]; then
  NETWORK=$( (cd "$REPO_ROOT" && docker compose ps --format '{{.Name}}' | head -1) )
  NETWORK=$(docker inspect "$NETWORK" --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}}{{end}}' 2>/dev/null)
fi
if [ -z "$NETWORK" ]; then
  printf '    could not determine the compose network; set RAG_NETWORK\n'
  exit 2
fi

# Build once; it is cached thereafter.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  printf '    building the browser image (first run only)...\n'
  docker build -q -t "$IMAGE" ./browser >/dev/null || { printf '    image build failed\n'; exit 2; }
fi

# A collection must exist for the delete-confirmation check to have a target.
C="${PREFIX}Ui"
drop_collection "$C"; make_collection "$C"

# The test accounts' credentials go in on standard input (#25), never on a
# command line or in the environment; browser/lib.js reads them once.
_rag_require_lock_owner
docker run --rm -i --network "$NETWORK" \
  -e RAG_UI_BASE="${RAG_UI_BASE:-http://proxy}" \
  -e RAG_SKIP_SLOW="$SKIP_SLOW" -e RAG_AUTH_STDIN=1 \
  -v "$PWD/browser":/w:ro -w /w "$IMAGE" node ui_criteria.js < "$RAG_AUTH_DIR/credentials"
rc=$?

drop_collection "$C"
cleanup_prefixed
exit $rc
