#!/usr/bin/env bash
#
# Install this project on a machine with NO internet access.
#
#   bash install-offline.sh
#
# Run it from inside the extracted bundle directory. It loads the pre-built
# Docker images, restores the Ollama model weights into the project's volume,
# and starts the stack without building or pulling anything.
#
# Requires only Docker Desktop (running) on the target Mac.

set -euo pipefail
cd "$(dirname "$0")"

[ -f offline/images.tar.gz ] || { echo "ERROR: offline/images.tar.gz missing. This is the source-only package, not the offline bundle (see package-offline.sh)." >&2; exit 1; }
[ -f offline/ollama_models.tar.gz ] || { echo "ERROR: offline/ollama_models.tar.gz missing." >&2; exit 1; }

TELEMETRY=0
if [ "${1:-}" = --telemetry ]; then TELEMETRY=1; shift; fi
[ "$#" = 0 ] || { echo "Usage: bash install-offline.sh [--telemetry]" >&2; exit 2; }
if [ "$TELEMETRY" = 1 ]; then
  # Validate bytes against tracked platform identities before docker load sees them.
  COLLECTOR_STAGE="$(mktemp -d)"
  trap 'rm -rf "$COLLECTOR_STAGE"' EXIT
  COLLECTOR_ID="$(python3 scripts/collector_offline.py prepare offline --output "$COLLECTOR_STAGE/collector.tar")"
  docker load -i "$COLLECTOR_STAGE/collector.tar"
  [ "$(docker image inspect "$COLLECTOR_ID" --format '{{.Id}}')" = "$COLLECTOR_ID" ]
  printf 'services:\n  otel-collector:\n    image: %s\n' "$COLLECTOR_ID" > "$COLLECTOR_STAGE/offline.yml"
  export COMPOSE_FILE="$PWD/docker-compose.yml:$PWD/docker-compose.telemetry.yml:$COLLECTOR_STAGE/offline.yml"
  export COMPOSE_PROFILES=telemetry
fi

docker info >/dev/null 2>&1 || { echo "ERROR: Docker is not running. Start Docker Desktop and retry." >&2; exit 1; }

echo "==> Loading Docker images (no network used)"
docker load -i offline/images.tar.gz

echo "==> Restoring Ollama model weights"
# `compose run` resolves the project's ollama_models volume for us, so this does
# not depend on the directory name. The ollama image ships tar and was just
# loaded above, so no extra image needs pulling.
RESTORE_PULL=""
[ "$TELEMETRY" = 0 ] || RESTORE_PULL="--pull=never"
docker compose run --rm --no-deps ${RESTORE_PULL:+$RESTORE_PULL} --entrypoint sh \
  -v "$PWD/offline:/backup:ro" \
  ollama -c 'tar xzf /backup/ollama_models.tar.gz -C /root/.ollama && ls /root/.ollama'

echo "==> Starting the stack (--no-build: nothing is compiled or pulled)"
if [ "$TELEMETRY" = 1 ]; then
  docker compose up -d --no-build --pull never
else
  docker compose up -d --no-build
fi

echo
echo "Waiting for services to report healthy..."
for i in $(seq 1 60); do
  running=$(docker compose ps --services --filter status=running | wc -l | tr -d ' ')
  [ "$running" = "$((5 + TELEMETRY))" ] && break
  sleep 5
done
docker compose ps

echo
echo "If the requested services are up, open http://localhost:8080"
echo "Verify the models were restored (no download should occur):"
echo "  docker compose exec ollama ollama list"
