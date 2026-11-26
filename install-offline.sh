#!/usr/bin/env bash
#
# Install this project on a machine with NO internet access.
#
#   bash install-offline.sh [--telemetry] [--external-ollama]
#
# Run it from inside the extracted bundle directory. It loads the pre-built
# Docker images, restores the Ollama model weights into the project's volume,
# and starts the stack without building or pulling anything.
#
# --external-ollama (#673) installs for an Ollama on this host instead of the
# stack's own (README "External Ollama (native)"): the model weights aren't
# restored (rag-docker never writes to an Ollama it doesn't run), the stack
# starts with docker-compose.external-ollama.yml, and the `ollama pull` lines
# for the models are printed, to run on the Ollama host.
#
# Requires only Docker Desktop (running) on the target Mac.

set -euo pipefail
cd "$(dirname "$0")"

[ -f offline/images.tar.gz ] || { echo "ERROR: offline/images.tar.gz missing. This is the source-only package, not the offline bundle (see package-offline.sh)." >&2; exit 1; }

TELEMETRY=0
EXTERNAL=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --telemetry) TELEMETRY=1; shift ;;
    --external-ollama) EXTERNAL=1; shift ;;
    *) echo "Usage: bash install-offline.sh [--telemetry] [--external-ollama]" >&2; exit 2 ;;
  esac
done
[ "$EXTERNAL" = 1 ] || [ -f offline/ollama_models.tar.gz ] \
  || { echo "ERROR: offline/ollama_models.tar.gz missing." >&2; exit 1; }
if [ "$TELEMETRY" = 1 ]; then
  # Validate bytes against tracked platform identities before docker load sees them.
  COLLECTOR_STAGE="$(mktemp -d)"
  trap 'rm -rf "$COLLECTOR_STAGE"' EXIT
  COLLECTOR_ID="$(python3 scripts/collector_offline.py install offline --output "$COLLECTOR_STAGE/collector.tar")"
  printf 'services:\n  otel-collector:\n    image: %s\n' "$COLLECTOR_ID" > "$COLLECTOR_STAGE/offline.yml"
  export COMPOSE_FILE="$PWD/docker-compose.yml:$PWD/docker-compose.telemetry.yml:$COLLECTOR_STAGE/offline.yml"
  export COMPOSE_PROFILES=telemetry
fi
if [ "$EXTERNAL" = 1 ]; then
  # Last, after telemetry's files when both are given.
  export COMPOSE_FILE="${COMPOSE_FILE:-$PWD/docker-compose.yml}:$PWD/docker-compose.external-ollama.yml"
fi

docker info >/dev/null 2>&1 || { echo "ERROR: Docker is not running. Start Docker Desktop and retry." >&2; exit 1; }

echo "==> Loading Docker images (no network used)"
docker load -i offline/images.tar.gz

if [ "$EXTERNAL" = 1 ]; then
  echo "==> External Ollama: the model weights are not restored (#673)"
else
echo "==> Restoring Ollama model weights"
# `compose run` resolves the project's ollama_models volume for us, so this does
# not depend on the directory name. The ollama image ships tar and was just
# loaded above, so no extra image needs pulling.
RESTORE_PULL=""
[ "$TELEMETRY" = 0 ] || RESTORE_PULL="--pull=never"
docker compose run --rm --no-deps ${RESTORE_PULL:+$RESTORE_PULL} --entrypoint sh \
  -v "$PWD/offline:/backup:ro" \
  ollama -c 'tar xzf /backup/ollama_models.tar.gz -C /root/.ollama && ls /root/.ollama'
fi

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
  # Five services, six with telemetry; one fewer with an external Ollama.
  [ "$running" = "$((5 + TELEMETRY - EXTERNAL))" ] && break
  sleep 5
done
docker compose ps

echo
echo "If the requested services are up, open http://localhost:8080"
if [ "$EXTERNAL" = 1 ]; then
  echo "On the Ollama host, pull the models rag-docker uses (it never pulls them itself):"
  # Read from the base file alone: the overlay leaves the ollama service out,
  # and its settings are where the model names are.
  for model in $(COMPOSE_FILE="$PWD/docker-compose.yml" COMPOSE_PROFILES= bash ollama/compose-models.sh); do
    echo "  ollama pull $model"
  done
  echo "Then check the setup:"
  echo "  bash scripts/check-external-ollama.sh"
  exit 0
fi
echo "Verify the models were restored (no download should occur):"
echo "  docker compose exec ollama ollama list"
