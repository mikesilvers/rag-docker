#!/usr/bin/env bash
#
# Build a fully self-contained OFFLINE bundle of this project.
#
#   bash package-offline.sh [output.tar]
#
# Unlike package.sh (source only, ~150 KB, needs the internet on first run),
# this bundle contains everything required to run with NO network access:
#
#   * the source tree
#   * all five Docker images, pre-built (docker save)
#   * the Ollama model weights (phi3.5 + nomic-embed-text)
#
# Measured at 4.6 GB (2.5 GB images + 2.2 GB model weights) with both models
# present. Build it on a machine where the stack already works,
# then move the file to the target Mac and run install-offline.sh there.

set -euo pipefail
cd "$(dirname "$0")"

TELEMETRY=0
if [ "${1:-}" = --telemetry ]; then TELEMETRY=1; shift; fi
[ "$#" -le 1 ] || { echo "Usage: bash package-offline.sh [--telemetry] [output.tar]" >&2; exit 2; }
OUT="${1:-rag-docker-offline.tar}"
OUT="$(cd "$(dirname "$OUT")" && pwd)/$(basename "$OUT")"   # absolute

# The MCP server is parked (see docker-compose.yml). Its image is deliberately
# NOT bundled: nothing in the stack uses it, and requiring it here would make
# packaging fail on a machine that has never built it. Re-add
# rag-docker-mcp:latest when the MCP server is brought back.
IMAGES=(
  rag-docker-api:latest
  rag-docker-ui:latest
  semitechnologies/weaviate:1.39.6
  ollama/ollama:0.3.14
  nginx:1.29-alpine
)

echo "==> Checking prerequisites"
for img in "${IMAGES[@]}"; do
  docker image inspect "$img" >/dev/null 2>&1 \
    || { echo "ERROR: image '$img' not found locally. Run 'docker compose build' and 'docker compose up -d' first." >&2; exit 1; }
done

# Resolve the compose project so we read the right volume.
PROJECT="$(docker compose config --format json | tr -d ' \n' | sed -n 's/^{"name":"\([^"]*\)".*/\1/p')"
[ -n "$PROJECT" ] || PROJECT="$(basename "$PWD" | tr '[:upper:]' '[:lower:]')"
MODELVOL="${PROJECT}_ollama_models"
docker volume inspect "$MODELVOL" >/dev/null 2>&1 \
  || { echo "ERROR: volume '$MODELVOL' not found. Start the stack once so the models download." >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/rag-docker/offline"

echo "==> Copying source"
tar cf - \
  --exclude='./.DS_Store' --exclude='*/.DS_Store' \
  --exclude='*/node_modules/*' --exclude='*/__pycache__/*' \
  --exclude='*.pyc' --exclude='*/dist/*' \
  --exclude='*/.venv/*' --exclude='*/venv/*' \
  --exclude='./.env' --exclude='*/.env' --exclude='*/.env.*' \
  --exclude='./.git/*' --exclude='*.tar' --exclude='*.zip' \
  --exclude='./offline/*' --exclude='./dev-docs/*' --exclude='./telemetry/secrets/*' \
  --exclude='./ingest-inbox/*' \
  --exclude='./exports/*' \
  . | ( cd "$STAGE/rag-docker" && tar xf - )

# Both zip and tar drop a directory once all its contents are excluded, so the
# mount target is recreated explicitly. Without it Docker creates ./ingest-inbox
# as root on the target machine and the user cannot drop files into it.
mkdir -p "$STAGE/rag-docker/ingest-inbox"
touch "$STAGE/rag-docker/ingest-inbox/.gitkeep"
mkdir -p "$STAGE/rag-docker/exports"
touch "$STAGE/rag-docker/exports/.gitkeep"

echo "==> Saving images (this is the slow part)"
docker save "${IMAGES[@]}" | gzip > "$STAGE/rag-docker/offline/images.tar.gz"

if [ "$TELEMETRY" = 1 ]; then
  echo "==> Saving optional pinned collector (Python 3.11+ required)"
  python3 scripts/collector_offline.py package "$STAGE/rag-docker/offline"
fi

echo "==> Exporting model weights from volume '$MODELVOL'"
# Uses the ollama image itself as the tar helper: it ships /usr/bin/tar and is
# already part of the bundle, so no extra image is needed here or on the target.
docker run --rm --entrypoint sh \
  -v "$MODELVOL":/models:ro \
  -v "$STAGE/rag-docker/offline":/out \
  ollama/ollama:0.3.14 \
  -c 'tar czf /out/ollama_models.tar.gz -C /models .'

echo "==> Assembling bundle"
rm -f "$OUT"
tar cf "$OUT" -C "$STAGE" rag-docker

echo
echo "Created $OUT ($(du -h "$OUT" | cut -f1))"
echo "  images:       $(du -h "$STAGE/rag-docker/offline/images.tar.gz" | cut -f1)"
echo "  model weights:$(du -h "$STAGE/rag-docker/offline/ollama_models.tar.gz" | cut -f1)"
echo
echo "On the target Mac (no internet required):"
echo "  tar xf $(basename "$OUT") && cd rag-docker"
if [ "$TELEMETRY" = 1 ]; then
  echo "  bash install-offline.sh --telemetry"
else
  echo "  bash install-offline.sh"
fi
