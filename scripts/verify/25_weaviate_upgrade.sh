#!/usr/bin/env bash
# The Weaviate upgrade path (#805): data written by the previous Weaviate opens
# on the checkout's, and a restart after a Raft snapshot works on upgraded data.
#
# Full runs only (maintainer decision 2026-11-26): a run with RAG_SKIP_SLOW=1,
# CI's among them, skips it. The tag it upgrades from is RAG_WEAVIATE_FROM, or
# else the one in origin/main's docker-compose.yml, the last release; the tag it
# upgrades to is the checkout's. When they are the same, the write, restart and
# restart-after-snapshot steps still run on that one version.
#
# Only on the verify project as stack.sh sets it up (the condition of removing
# its volumes, #465): weaviate_upgrade.py creates a throwaway container, volume
# and internal network labelled as part of rag-verify, publishes nothing, and
# removes them at the end; `stack.sh down` removes whatever a killed run leaves.
# It never touches the project's own services or volumes, and removes no image.
cd "$(dirname "$0")" && . ./lib.sh
REPO_ROOT="$(cd ../.. && pwd)"
require_stack
OUT="$(mktemp)"; ERR="$(mktemp)"; CONFIG="$(mktemp)"
trap 'rm -f "$OUT" "$ERR" "$CONFIG"' EXIT

section "Weaviate upgrade path (#805)"

if [ "$SKIP_SLOW" = "1" ]; then
  skip "the Weaviate upgrade path from the release on main to the checkout" \
    "full runs only; set RAG_SKIP_SLOW=0 to include it"
  summary; exit $?
fi

refusal=$(volume_removal_refusal_reason)
if [ -n "$refusal" ]; then
  check "the Weaviate upgrade path runs only on the verify project" 1 "$refusal"
  summary; exit $?
fi

_rag_require_lock_owner
if ! (cd "$REPO_ROOT" && docker compose config --format json) > "$CONFIG" 2> "$ERR"; then
  check "the verify project's configuration resolves" 1 "$(tail -c 400 "$ERR")"
  summary; exit $?
fi
api_image=$(cd "$REPO_ROOT" && docker compose images -q api 2>/dev/null | head -n 1)
if [ -z "$api_image" ]; then
  check "the verify project's api image is there" 1 "docker compose images -q api gave nothing"
  summary; exit $?
fi

python3 ./weaviate_upgrade.py run --checkout "$REPO_ROOT" --api-image "$api_image" \
  --compose-json "$CONFIG" > "$OUT" 2> "$ERR"
status=$?
lines=0
while IFS=$'\t' read -r result name detail; do
  case "$result" in
    PASS) check "$name" 0; lines=$((lines+1)) ;;
    FAIL) check "$name" 1 "$detail"; lines=$((lines+1)) ;;
  esac
done < "$OUT"
if [ "$lines" -eq 0 ] || { [ "$status" -ne 0 ] && [ "$FAIL" -eq 0 ]; }; then
  check "weaviate_upgrade.py ran to the end" 1 "exit $status: $(tail -c 600 "$ERR")"
fi
summary
