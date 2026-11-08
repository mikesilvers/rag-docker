#!/usr/bin/env bash
# SPECIFICATIONS.md §10.4 — Web UI, driven by a real headless browser.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
require_stack

IMAGE="rag-verify-browser:latest"
# The browser runs in the proxy container's network namespace and opens
# http://localhost, so its requests carry a Host the proxy answers for (#664):
# as http://proxy on the compose network they would get 421.
PROXY=$( (cd "$REPO_ROOT" && docker compose ps -q proxy) )
if [ -z "$PROXY" ]; then
  printf '    could not find the proxy container\n'
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
docker run --rm -i --network "container:$PROXY" \
  -e RAG_UI_BASE="${RAG_UI_BASE:-http://localhost}" \
  -e RAG_SKIP_SLOW="$SKIP_SLOW" -e RAG_AUTH_STDIN=1 \
  -v "$PWD/browser":/w:ro -w /w "$IMAGE" node ui_criteria.js < "$RAG_AUTH_DIR/credentials"
rc=$?

drop_collection "$C"
cleanup_prefixed
exit $rc
