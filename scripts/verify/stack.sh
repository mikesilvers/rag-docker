#!/usr/bin/env bash
#
# Run the verification suite on a disposable compose project, never on the
# live stack (#152).
#
#   bash scripts/verify/stack.sh run [--checkout DIR] [--telemetry [--telemetry-pin FILE] | --lan | --query-only | --external-ollama] [suite ...]
#   bash scripts/verify/stack.sh up [--checkout DIR] [--pull] [--telemetry [--telemetry-pin FILE] | --lan | --query-only | --external-ollama]
#   bash scripts/verify/stack.sh down
#
# `run` brings the verify project up, runs that checkout's all.sh against it
# (RAG_SKIP_SLOW and RAG_ALLOW_RESTART pass through) and always tears it down,
# exiting with all.sh's status. all.sh runs in a process group of its own, and
# before the teardown that whole group is stopped (TERM, then KILL), so no suite
# outlives the run that started it (#184). INT and TERM act at once. A
# stack.sh killed outright (SIGKILL) can't do this; then lock.sh and lib.sh's
# helpers stop the orphaned suite (see lock.sh). `up` leaves the project running and prints the
# environment that points docker compose and the suites at it; `down` removes
# it. --checkout picks the checkout to build and test (default: this one).
#
# The verify project is compose project `rag-verify` on port RAG_VERIFY_PORT
# (default 8081; 8080 is refused), with its own empty volumes, its own image
# tags (rag-verify-api, rag-verify-ui) and its own exports folder. The live
# `rag-docker` stack holds real data, and nothing here builds, starts, stops or
# writes to it. Its only contact with it: the Ollama models are copied from
# rag-docker_ollama_models, mounted read-only in a throwaway container, into
# the volume rag-verify-ollama-models, which is kept between runs and checked
# by sha256 on every `up`.
#
# The overlay docker-compose.verify.yml and this script come from the checkout
# that holds this script, so an evaluation can run a trusted copy against a PR's
# checkout. Before anything is built, the resolved configuration is checked
# against an allow-list of what docker-compose.yml and the overlay need (#154),
# and refused otherwise: any other top-level or service key (volumes_from,
# network_mode, privileged, secrets, ...); a build with options other than a
# context and Dockerfile inside the checkout, or one that would write a tag
# other than rag-verify-<service>; a rag-docker-* image under any registry
# name; a network or volume other than the project's own (and the model copy);
# any published port but the proxy on loopback at the verify port; a mount
# other than a volume or a read-only bind from inside the checkout (never its
# exports folder), apart from the api's own exports folder; the Docker socket.
# It can't see env_file, which compose merges into the environment.
#
# --lan (#26) runs the verify project in LAN mode. It comes up in its default
# configuration first, where the first Engineer (vfy-engineer) is created, as
# the README tells users to do, and then switches to the LAN configuration:
# the checkout's docker-compose.lan.yml plus this checkout's
# docker-compose.lan.verify.yml, which publishes TLS on loopback only, at the
# verify port. The guard checks that configuration too, with rule (c) wanting
# exactly proxy:127.0.0.1:<verify port>->443/tcp. The suites then reach it at
# https://localhost:<port>/api and trust the certificate it generated, copied
# into the private folder. Without suite names, a --lan run runs 18_lan.
#
# --query-only (#670) runs the verify project in its default configuration,
# where 24_query_only builds and exports a collection; that suite then calls
# `stack.sh query-only-mode --package NAME` under the inherited lock, which
# carries the package out of the exports folder, removes the project's
# containers, network and volumes (its images and the model copy stay), puts
# the package back alone in an empty exports folder and starts the project
# again in query-only mode (the checkout's docker-compose.query-only.yml): a
# second instance with its own empty volumes, as on another machine, never two
# at once. Without suite names, a --query-only run runs 24_query_only.
#
# --external-ollama (#673) runs the verify project in external-Ollama mode (the
# checkout's docker-compose.external-ollama.yml): no ollama service, and api and
# weaviate reach an Ollama installed natively on this host as `ollama`, through
# host-gateway. Before anything else it checks that host the way the README's
# guide does: scripts/check-external-ollama.sh --host-only must pass with no
# warning (Ollama listening only on loopback, or on Linux the docker0 address),
# and that Ollama must answer on port 11434. Nothing here installs Ollama or
# pulls a model into it: its models must already be there, or the start fails
# with the `ollama pull` lines. The model copy is not used. The guard then
# allows exactly `ollama:host-gateway` as extra_hosts on api and weaviate, and
# nothing else, and refuses a configuration that still has an ollama service.
# Without suite names, an --external-ollama run runs 26_external_ollama.
#
# --telemetry checks the collector image against this checkout's own
# telemetry/image.json, so a PR that changes the pin is refused. To evaluate
# one, the evaluator checks the PR's pin against the registry, copies its
# telemetry/image.json outside the checkout and passes it with
# --telemetry-pin FILE (#532). The guard then expects that image, which may
# change only the tag and digest of this checkout's pin, never the
# repository. Nothing here contacts a registry, and no file in the checkout
# is read as a pin: the file is checked by device and inode, for telemetry-mode's
# inherited pin too.
#
# What this does not do: the suites, and anything else the checkout runs on the
# host, still have full access to Docker. It keeps verification away from the
# live stack by accident, not from hostile code; that is the security review's.
set -uo pipefail

PROJECT=rag-verify
LIVE_MODELS=rag-docker_ollama_models
MODELS=rag-verify-ollama-models
HARNESS="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"
SCRATCH="${TMPDIR:-/tmp}"
SCRATCH="${SCRATCH%/}"
[ -n "$SCRATCH" ] || SCRATCH=/tmp
# A folder of this user's own, mode 700, holds the exports folder, so another
# local user can't pre-create it or plant a symlink there (#154).
PRIVATE="$SCRATCH/rag-verify-$(id -u)"
EXPORTS="$PRIVATE/exports"
# LAN mode's certificate, copied from the proxy so the suites can verify it (#26).
LAN_CERT="$PRIVATE/lan-cert.pem"
# Which rule (c) the guard applies: default (port 80) or lan (port 443).
GUARD_MODE=default

usage() {
  sed -n '6,8p' "${BASH_SOURCE[0]}" | sed 's/^#  //' >&2
  exit 2
}

fail() {
  printf '\nstack.sh: %s\n\n' "$1" >&2
  exit 2
}

# Prints why the private folder can't be used, or nothing.
private_problem() {
  if [ -L "$PRIVATE" ]; then
    printf '%s is a symlink; refusing to use it.' "$PRIVATE"
  elif [ -e "$PRIVATE" ] && [ ! -d "$PRIVATE" ]; then
    printf '%s exists and is not a folder; refusing to use it.' "$PRIVATE"
  elif [ -d "$PRIVATE" ] && [ ! -O "$PRIVATE" ]; then
    printf '%s belongs to another user; refusing to use it.' "$PRIVATE"
  fi
}

# ── arguments, checked before any Docker command ─────────────────────────────
[ "$#" -ge 1 ] || usage
CMD="$1"; shift
case "$CMD" in up|down|run|telemetry-mode|query-only-mode) ;; *) usage ;; esac
if [ "$CMD" = telemetry-mode ]; then
  [ "${RAG_VERIFY_LOCK_HELD:-}" = 1 ] || fail "telemetry-mode requires an inherited verification lock"
fi
if [ "$CMD" = query-only-mode ]; then
  [ "${RAG_VERIFY_LOCK_HELD:-}" = 1 ] || fail "query-only-mode requires an inherited verification lock"
  [ "${RAG_VERIFY_QUERY_ONLY:-}" = 1 ] || fail "query-only-mode runs only in a stack.sh run --query-only"
fi
CHECKOUT="$HARNESS"
PULL=""
TELEMETRY=0
LAN=0
QUERY_ONLY=0
EXTERNAL=0
QO_PACKAGE=""
OTEL_ENABLED=true
PIN=""
PIN_SET=0
ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --checkout)
      [ "$CMD" != down ] && [ "$#" -ge 2 ] || usage
      CHECKOUT="$2"; shift 2 ;;
    --telemetry)
      [ "$CMD" != down ] || usage
      TELEMETRY=1; shift ;;
    --telemetry-pin)
      { [ "$CMD" = up ] || [ "$CMD" = run ]; } && [ "$#" -ge 2 ] || usage
      PIN="$2"; PIN_SET=1; shift 2 ;;
    --lan)
      { [ "$CMD" = up ] || [ "$CMD" = run ]; } || usage
      LAN=1; shift ;;
    --query-only)
      { [ "$CMD" = up ] || [ "$CMD" = run ]; } || usage
      QUERY_ONLY=1; shift ;;
    --external-ollama)
      { [ "$CMD" = up ] || [ "$CMD" = run ]; } || usage
      EXTERNAL=1; shift ;;
    --package)
      [ "$CMD" = query-only-mode ] && [ "$#" -ge 2 ] || usage
      QO_PACKAGE="$2"; shift 2 ;;
    --disabled)
      [ "$CMD" = telemetry-mode ] || usage
      OTEL_ENABLED=false; shift ;;
    --pull)
      [ "$CMD" = up ] || usage
      PULL=1; shift ;;
    -*) usage ;;
    *)
      [ "$CMD" = run ] || usage
      ARGS+=("$1"); shift ;;
  esac
done

[ "$TELEMETRY" = 0 ] || [ "$LAN" = 0 ] || fail "--lan and --telemetry can't be combined."
[ "$QUERY_ONLY" = 0 ] || { [ "$TELEMETRY" = 0 ] && [ "$LAN" = 0 ]; } \
  || fail "--query-only can't be combined with --lan or --telemetry."
[ "$EXTERNAL" = 0 ] || { [ "$TELEMETRY" = 0 ] && [ "$LAN" = 0 ] && [ "$QUERY_ONLY" = 0 ]; } \
  || fail "--external-ollama can't be combined with --lan, --telemetry or --query-only."
if [ "$CMD" = query-only-mode ]; then
  # A package name as the loader takes it (#670): no path, no other file.
  printf '%s' "$QO_PACKAGE" | grep -Eqx 'ragpkg-[A-Za-z0-9._-]+\.tar\.gz(\.age)?' \
    || fail "query-only-mode needs --package with a package name (ragpkg-*.tar.gz), not '$QO_PACKAGE'."
fi
[ "$PIN_SET" = 0 ] || [ "$TELEMETRY" = 1 ] || fail "--telemetry-pin requires --telemetry."

PORT="${RAG_VERIFY_PORT:-8081}"
case "$PORT" in
  ''|*[!0-9]*) fail "RAG_VERIFY_PORT must be a port number, not '$PORT'." ;;
esac
[ "${#PORT}" -le 5 ] || fail "RAG_VERIFY_PORT must be from 1024 to 65535, not $PORT."
PORT=$((10#$PORT))
{ [ "$PORT" -ge 1024 ] && [ "$PORT" -le 65535 ]; } || fail "RAG_VERIFY_PORT must be from 1024 to 65535, not $PORT."
[ "$PORT" -ne 8080 ] || fail "RAG_VERIFY_PORT can't be 8080: that is the live rag-docker stack's port."

if [ "$CMD" != down ]; then
  { [ -d "$CHECKOUT" ] && [ -f "$CHECKOUT/docker-compose.yml" ]; } \
    || fail "--checkout must be a checkout of this project (a folder with docker-compose.yml): $CHECKOUT"
  CHECKOUT="$(cd "$CHECKOUT" && pwd -P)"
fi

# External-Ollama mode (#673): the host is checked as the README's guide sets
# it up, before any Docker command. The listener check comes from the harness,
# not the checkout under test.
if [ "$EXTERNAL" = 1 ]; then
  [ -f "$CHECKOUT/docker-compose.external-ollama.yml" ] \
    || fail "--external-ollama needs a checkout with docker-compose.external-ollama.yml: $CHECKOUT"
  bash "$HARNESS/scripts/check-external-ollama.sh" --host-only \
    || fail "the host's Ollama isn't set up as README \"External Ollama (native)\" says (above): it must listen on loopback, or on Linux the docker0 address, and nowhere else."
  OLLAMA_ADDR=127.0.0.1
  if [ "$(uname -s)" = Linux ]; then
    OLLAMA_ADDR=$(ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1)
    [ -n "$OLLAMA_ADDR" ] || fail "--external-ollama on Linux needs the docker0 bridge, where Ollama listens."
  fi
  code=$(command curl -s -o /dev/null -m 5 -w '%{http_code}' "http://$OLLAMA_ADDR:11434/api/version" 2>/dev/null)
  [ "$code" = 200 ] \
    || fail "no Ollama answers at http://$OLLAMA_ADDR:11434 (HTTP $code). Install and start it as README \"External Ollama (native)\" says, with its models pulled."
fi

# The reviewed collector pin (#532): the evaluator's file, never the checkout's.
# --telemetry-pin and the inherited pin of telemetry-mode (#764) get one check:
# the file exists, and neither it nor a directory above it is the checkout,
# compared by device and inode so letter case on a case-insensitive volume
# doesn't matter, and it has no second hard link (one could be in the checkout).
check_pin() {
  [ -f "$PIN" ] || fail "$1 must name an existing file: $PIN"
  PIN="$(python3 -c 'import os, sys; print(os.path.realpath(sys.argv[1]))' "$PIN")" \
    || fail "could not resolve the $1 path."
  local rc=0
  python3 - "$PIN" "$CHECKOUT" <<'PINPY' || rc=$?
import os, sys

# Exit 1: the pin is in the checkout. 3: it has a second hard link. 4: an OS error.
pin, checkout = sys.argv[1:3]
try:
    if os.stat(pin).st_nlink > 1:
        sys.exit(3)
    folder = os.path.dirname(pin)
    while True:
        if os.path.samefile(folder, checkout):
            sys.exit(1)
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
except OSError:
    sys.exit(4)
PINPY
  case "$rc" in
    0) ;;
    3) fail "$1 has a second hard link, which could be a file in the checkout under test: $PIN" ;;
    4) fail "$1 could not be checked against the checkout under test: $PIN" ;;
    *) fail "$1 must be outside the checkout under test: $PIN" ;;
  esac
}
if [ "$PIN_SET" = 1 ]; then
  check_pin "--telemetry-pin"
fi
# telemetry-mode, run by a suite under the inherited lock, keeps the run's
# reviewed pin, checked again here; up and run take it only from --telemetry-pin.
if [ "$CMD" = telemetry-mode ] && [ -n "${RAG_VERIFY_TELEMETRY_PIN:-}" ]; then
  PIN="$RAG_VERIFY_TELEMETRY_PIN"
  check_pin "RAG_VERIFY_TELEMETRY_PIN"
fi

# The private folder, checked before any Docker command; up and run create it.
problem=$(private_problem)
[ -z "$problem" ] || fail "$problem"
if [ "$CMD" != down ]; then
  if [ -d "$PRIVATE" ]; then
    chmod 700 "$PRIVATE" || fail "could not set $PRIVATE to mode 700."
  else
    mkdir -m 700 "$PRIVATE" || fail "could not create $PRIVATE."
  fi
fi

# ── one verify run per machine ───────────────────────────────────────────────
# Held for the whole command; the all.sh started below inherits it.
. "$HARNESS/scripts/verify/lock.sh"
TEARDOWN=0
SUITE=""
# Stops all.sh's process group: everything the suites started, including what
# they left running in the background after all.sh ended.
stop_suite() {
  local i
  [ -n "$SUITE" ] || return 0
  kill -TERM -- "-$SUITE" 2>/dev/null
  for i in $(seq 1 50); do
    # Zombies count as gone: only this shell can reap all.sh itself.
    ps -A -o pgid=,stat= 2>/dev/null | awk -v g="$SUITE" '$1 == g && $2 !~ /^Z/ {found=1} END {exit !found}' || break
    sleep 0.1
  done
  kill -KILL -- "-$SUITE" 2>/dev/null
  wait "$SUITE" 2>/dev/null
  SUITE=""
}
on_exit() {
  local rc=$?
  trap - EXIT
  stop_suite
  if [ "$TEARDOWN" = 1 ]; then
    TEARDOWN=0
    do_down || rc=2
  fi
  _rag_lock_release
  exit "$rc"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# ── the verify environment ───────────────────────────────────────────────────
unset COMPOSE_PATH_SEPARATOR
export COMPOSE_PROJECT_NAME="$PROJECT"
export COMPOSE_FILE="$CHECKOUT/docker-compose.yml:$HARNESS/docker-compose.verify.yml"
# Ignore ambient Compose profiles; only this explicit option enables telemetry.
unset COMPOSE_PROFILES
export RAG_VERIFY_TELEMETRY="$TELEMETRY"
export RAG_VERIFY_OTEL_ENABLED="$OTEL_ENABLED"
export RAG_VERIFY_HARNESS="$HARNESS"
export RAG_VERIFY_CHECKOUT="$CHECKOUT"
export RAG_VERIFY_TELEMETRY_PIN="$PIN"
if [ "$TELEMETRY" = 1 ]; then
  export COMPOSE_FILE="$CHECKOUT/docker-compose.yml:$CHECKOUT/docker-compose.telemetry.yml:$HARNESS/docker-compose.verify.yml:$HARNESS/docker-compose.telemetry.verify.yml"
  export COMPOSE_PROFILES=telemetry
fi
# LAN mode's configuration, switched to once the default one is up and set up.
LAN_COMPOSE_FILE="$CHECKOUT/docker-compose.yml:$CHECKOUT/docker-compose.lan.yml:$HARNESS/docker-compose.verify.yml:$HARNESS/docker-compose.lan.verify.yml"
export RAG_VERIFY_LAN="$LAN"
# External-Ollama mode (#673): its configuration from the start.
export RAG_VERIFY_EXTERNAL_OLLAMA="$EXTERNAL"
if [ "$EXTERNAL" = 1 ]; then
  export COMPOSE_FILE="$CHECKOUT/docker-compose.yml:$CHECKOUT/docker-compose.external-ollama.yml:$HARNESS/docker-compose.verify.yml"
  GUARD_MODE=external-ollama
fi
# Query-only mode's configuration (#670), switched to by query-only-mode.
QUERY_ONLY_COMPOSE_FILE="$CHECKOUT/docker-compose.yml:$CHECKOUT/docker-compose.query-only.yml:$HARNESS/docker-compose.verify.yml"
[ "$CMD" != query-only-mode ] || QUERY_ONLY="${RAG_VERIFY_QUERY_ONLY:-0}"
export RAG_VERIFY_QUERY_ONLY="$QUERY_ONLY"
# Where query-only-mode keeps the package while the exports folder is replaced.
QO_CARRY="$PRIVATE/query-only-carry"
export RAG_VERIFY_PORT="$PORT"
export RAG_API="http://localhost:$PORT/api"
export RAG_EXPECTED_PROXY_PORT="$PORT"
export RAG_EXPORTS_DIR="$EXPORTS"

# ── down: remove everything of the verify project, except the model copy ────
# The images the verify project built, by name. Untagged entries are left
# out: they can't be removed by name.
labelled_images() {
  docker image ls --filter "label=com.docker.compose.project=$PROJECT" \
    --format '{{.Repository}}:{{.Tag}}' | grep -v '<none>'
}

do_down() {
  local filter="label=com.docker.compose.project=$PROJECT" ids images problem left
  # Project name only, from a neutral folder: no compose file is needed.
  (cd / && env -u COMPOSE_FILE docker compose -p "$PROJECT" down -v --remove-orphans) >/dev/null 2>&1
  ids=$(docker ps -aq --filter "$filter")
  [ -z "$ids" ] || docker rm -f $ids >/dev/null
  ids=$(docker network ls -q --filter "$filter")
  [ -z "$ids" ] || docker network rm $ids >/dev/null
  ids=$(docker volume ls -q --filter "$filter" | grep -vx "$MODELS")
  [ -z "$ids" ] || docker volume rm $ids >/dev/null
  # Every image built for the project, whatever its service, but only by a
  # rag-verify-* name. A labelled image under any other name is left alone
  # and reported below.
  images=$(labelled_images | grep '^rag-verify-')
  [ -z "$images" ] || docker image rm $images >/dev/null
  problem=$(private_problem)
  if [ -n "$problem" ]; then
    printf 'stack.sh: %s\n' "$problem" >&2
    return 2
  fi
  if [ -L "$EXPORTS" ]; then
    printf 'stack.sh: %s is a symlink; not removing it.\n' "$EXPORTS" >&2
    return 2
  fi
  [ ! -d "$EXPORTS" ] || rm -rf "$EXPORTS"
  rm -f "$LAN_CERT"
  if [ -L "$QO_CARRY" ]; then
    printf 'stack.sh: %s is a symlink; not removing it.\n' "$QO_CARRY" >&2
    return 2
  fi
  [ ! -d "$QO_CARRY" ] || rm -rf "$QO_CARRY"
  left="$(docker ps -aq --filter "$filter")$(docker network ls -q --filter "$filter")$(docker volume ls -q --filter "$filter" | grep -vx "$MODELS")$(labelled_images)"
  if [ -e "$EXPORTS" ] || [ -L "$EXPORTS" ]; then
    left="$left $EXPORTS"
  fi
  if [ -n "$left" ]; then
    printf 'stack.sh: the verify project was not fully removed: %s\n' "$(printf '%s' "$left" | tr '\n' ' ')" >&2
    return 2
  fi
  printf 'verify project %s removed (the model copy %s is kept)\n' "$PROJECT" "$MODELS"
}

# ── the models: a copy of the live store, checked on every up ────────────────
# The image that runs the copy is the ollama image pinned by the harness's own
# docker-compose.yml, already on this machine, not one the checkout picks.
seed_image() {
  python3 - "$HARNESS/docker-compose.yml" <<'IMAGEPY'
import sys
inside = False
for line in open(sys.argv[1]):
    if line.rstrip('\n') == '  ollama:':
        inside = True
    elif inside and line.startswith('  ') and not line.startswith('   ') and line.strip():
        break
    elif inside and line.startswith('    image:'):
        print(line.split(':', 1)[1].strip().strip('"\''))
        sys.exit(0)
sys.exit(1)
IMAGEPY
}

# Runs in the throwaway container: /live is the live store (read-only), /copy
# the verify copy. Every file is compared by sha256 and copied when missing or
# different; files the live store doesn't have are removed.
SYNC='set -euo pipefail
copied=0; repaired=0; removed=0
cd /live
while IFS= read -r -d "" f; do
  f="${f#./}"
  want=$(sha256sum "/live/$f" | cut -d" " -f1)
  case "$f" in
    */blobs/sha256-*) [ "${f##*/sha256-}" = "$want" ] || echo "warning: live blob $f does not match its name" >&2 ;;
  esac
  if [ -f "/copy/$f" ] && [ ! -L "/copy/$f" ]; then
    [ "$(sha256sum "/copy/$f" | cut -d" " -f1)" = "$want" ] && continue
    repaired=$((repaired + 1))
  else
    copied=$((copied + 1))
  fi
  mkdir -p "/copy/$(dirname "$f")"
  rm -rf "/copy/$f"
  cp -p "/live/$f" "/copy/$f"
done < <(find . -type f -print0)
cd /copy
while IFS= read -r -d "" f; do
  f="${f#./}"
  if [ ! -f "/live/$f" ] || [ -L "/live/$f" ]; then rm -f "/copy/$f"; removed=$((removed + 1)); fi
done < <(find . \( -type f -o -type l \) -print0)
find /copy -mindepth 1 -type d -empty -delete
echo "models: copied=$copied repaired=$repaired removed=$removed"'

do_seed() {
  local img
  if ! docker volume inspect "$MODELS" >/dev/null 2>&1; then
    docker volume create --label rag-verify.models=1 "$MODELS" >/dev/null || fail "could not create the volume $MODELS."
  fi
  # External-Ollama mode (#673): the models are the host Ollama's own; nothing
  # in the project uses the copy, so it isn't checked or filled.
  [ "$EXTERNAL" = 0 ] || return 0
  if ! docker volume inspect "$LIVE_MODELS" >/dev/null 2>&1; then
    printf 'note: there is no %s volume to copy the models from, so the verify\n' "$LIVE_MODELS"
    printf 'project'"'"'s ollama will pull them into %s (this needs the internet).\n' "$MODELS"
    return 0
  fi
  img=$(seed_image) || fail "could not find the ollama image in $HARNESS/docker-compose.yml."
  docker image inspect "$img" >/dev/null 2>&1 \
    || fail "the image $img, used to copy the models, isn't on this machine; it isn't pulled for this."
  printf 'checking the model copy %s against %s (read-only)...\n' "$MODELS" "$LIVE_MODELS"
  docker run --rm --network none --entrypoint /bin/bash \
    -v "$LIVE_MODELS:/live:ro" -v "$MODELS:/copy" "$img" -c "$SYNC" \
    || fail "copying the models into $MODELS failed."
}

# ── the guard on the resolved configuration ──────────────────────────────────
do_guard() {
  local config rc
  config=$(mktemp "$SCRATCH/rag-verify-config.XXXXXX") || fail "could not create a scratch file."
  if ! docker compose -p "$PROJECT" config --format json > "$config"; then
    rm -f "$config"
    fail "docker compose config failed for $CHECKOUT."
  fi
  python3 - "$CHECKOUT" "$EXPORTS" "$PORT" "$config" "$HARNESS" "$GUARD_MODE" "$PIN" <<'GUARDPY'
import json, os, sys

checkout, exports, port, path = sys.argv[1:5]
# default, or lan (#26): LAN mode's proxy serves TLS on container port 443.
mode = sys.argv[6] if len(sys.argv) > 6 else 'default'
target = 443 if mode == 'lan' else 80
config = json.load(open(path))
services = config.get('services') or {}
volumes = config.get('volumes') or {}
networks = config.get('networks') or {}
real = os.path.realpath
problems = []

# An allow-list (#154): only the keys and values that docker-compose.yml and
# the overlay resolve to. Anything else could reach the host or other
# containers, so it is refused; a branch that needs more changes this list.
TOP_KEYS = {'name', 'services', 'volumes', 'networks'}
SERVICE_KEYS = {'build', 'command', 'depends_on', 'entrypoint', 'environment',
                'healthcheck', 'image', 'networks', 'ports', 'volumes'}
BUILD_KEYS = {'context', 'dockerfile'}
NETWORK_KEYS = {'name', 'driver', 'ipam'}
VOLUME_KEYS = {'name', 'external'}
MOUNT_KEYS = {'type', 'source', 'target', 'read_only', 'bind', 'volume'}

def inside(child, parent):
    child, parent = real(child), real(parent)
    return child == parent or child.startswith(parent.rstrip('/') + '/')

def normalise(image):
    # The same image under its registry-qualified names.
    for prefix in ('docker.io/', 'index.docker.io/', 'registry-1.docker.io/'):
        if image.startswith(prefix):
            image = image[len(prefix):]
            break
    if image.startswith('library/'):
        image = image[len('library/'):]
    return image

def extra(keys, allowed):
    return ', '.join(sorted(set(keys) - allowed))

# (g) top level: only these keys, and the verify project's name
if extra(config, TOP_KEYS):
    problems.append(f"(g) top-level keys not allowed: {extra(config, TOP_KEYS)}")
if config.get('name') != 'rag-verify':
    problems.append(f"(g) the project name is {config.get('name')!r}, not 'rag-verify'")

# (h) service keys: only the base file's
for svc, service in services.items():
    allowed = SERVICE_KEYS
    if svc in ('otel-collector', 'otel-capture'):
        allowed = allowed | {'profiles', 'mem_limit', 'cpus', 'stop_grace_period', 'read_only', 'logging'}
    if mode == 'external-ollama' and svc in ('api', 'weaviate'):
        allowed = allowed | {'extra_hosts'}
    if extra(service, allowed):
        problems.append(f"(h) service {svc!r} uses keys not allowed: {extra(service, allowed)}")

# (k) external-Ollama mode (#673): api and weaviate may name the host only as
# `ollama`, at host-gateway, and nothing else; the ollama service is left out.
if mode == 'external-ollama':
    for svc in ('api', 'weaviate'):
        hosts = (services.get(svc) or {}).get('extra_hosts')
        if hosts is None:
            continue
        if isinstance(hosts, dict):
            hosts = [f"{name}={value}" for name, value in hosts.items()]
        if not (isinstance(hosts, list) and len(hosts) == 1
                and hosts[0] in ('ollama=host-gateway', 'ollama:host-gateway')):
            problems.append(f"(k) service {svc!r} sets extra_hosts {hosts!r}; only "
                            f"['ollama:host-gateway'] is allowed")
    if 'ollama' in services:
        problems.append("(k) the ollama service must be left out in external-Ollama mode")
if {'otel-collector', 'otel-capture'} & services.keys():
    import runpy
    harness = sys.argv[5] if len(sys.argv) > 5 else checkout
    pin = sys.argv[7] if len(sys.argv) > 7 else ''
    guard = runpy.run_path(os.path.join(harness, 'scripts/verify/telemetry_guard.py'))
    problems.extend(guard['check'](config, harness, pin or None))

# (i) builds: only a context and a Dockerfile, both inside the checkout
for svc, service in services.items():
    if 'build' not in service:
        continue
    build = service['build']
    if not isinstance(build, dict):
        problems.append(f"(i) service {svc!r} has a build of an unexpected form: {build!r}")
        continue
    if extra(build, BUILD_KEYS):
        problems.append(f"(i) service {svc!r} uses build keys not allowed: {extra(build, BUILD_KEYS)}")
    context = build.get('context')
    if not (isinstance(context, str) and os.path.isabs(context) and inside(context, checkout)):
        problems.append(f"(i) service {svc!r} builds from {context!r}, not a folder inside the checkout")
        continue
    dockerfile = os.path.join(context, build.get('dockerfile') or 'Dockerfile')
    if not inside(dockerfile, checkout):
        problems.append(f"(i) service {svc!r} uses the Dockerfile {dockerfile!r}, outside the checkout")

# (a) volumes: the project's own, or the external model copy, nothing else
for key, volume in volumes.items():
    volume = volume or {}
    name = volume.get('name')
    if key == 'ollama_models' or name == 'rag-verify-ollama-models':
        ok = (name == 'rag-verify-ollama-models' and volume.get('external') is True
              and not volume.get('driver_opts'))
    else:
        ok = (name == 'rag-verify_' + key and not volume.get('external')
              and not volume.get('driver_opts'))
    if not ok:
        problems.append(f"(a) volume {key!r} resolves to {name!r}"
                        f"{' (external)' if volume.get('external') else ''}"
                        f"{' (driver_opts)' if volume.get('driver_opts') else ''}; "
                        f"only rag-verify_{key} or the external rag-verify-ollama-models are allowed")
    if extra(volume, VOLUME_KEYS):
        problems.append(f"(a) volume {key!r} uses keys not allowed: {extra(volume, VOLUME_KEYS)}")
for svc, service in services.items():
    for mount in service.get('volumes') or []:
        if mount.get('type') == 'volume' and mount.get('source') and mount['source'] not in volumes:
            problems.append(f"(a) service {svc!r} mounts undeclared volume {mount['source']!r}")

# (b) images: never the live stack's tags; a build writes only its own
# rag-verify-<service> tag, so no tag the live stack runs can be rebuilt
for svc, service in services.items():
    image = normalise(service.get('image') or '')
    if image.startswith('rag-docker-') or image.startswith('rag-docker:'):
        problems.append(f"(b) service {svc!r} uses the live image {service.get('image')!r}")
for svc, want in (('api', 'rag-verify-api:latest'), ('ui', 'rag-verify-ui:latest')):
    if svc in services and services[svc].get('image') != want:
        problems.append(f"(b) service {svc!r} must use {want}, not {services[svc].get('image')!r}")
for svc, service in services.items():
    if 'build' in service and svc not in ('api', 'ui') and 'image' in service \
            and normalise(service.get('image') or '') not in (f'rag-verify-{svc}:latest', f'rag-verify-{svc}'):
        problems.append(f"(b) service {svc!r} builds, so its image must be rag-verify-{svc}:latest "
                        f"or unset, not {service.get('image')!r}")

# (j) networks: the project's own bridge networks, joined with no options
for key, network in networks.items():
    network = network or {}
    if extra(network, NETWORK_KEYS):
        problems.append(f"(j) network {key!r} uses keys not allowed: {extra(network, NETWORK_KEYS)}")
    if network.get('name') != 'rag-verify_' + key:
        problems.append(f"(j) network {key!r} resolves to {network.get('name')!r}; only rag-verify_{key} is allowed")
    if network.get('driver') not in (None, 'bridge'):
        problems.append(f"(j) network {key!r} uses the driver {network.get('driver')!r}; only bridge is allowed")
    if network.get('ipam'):
        problems.append(f"(j) network {key!r} sets ipam options")
for svc, service in services.items():
    joined = service.get('networks') or {}
    if isinstance(joined, list):
        joined = {name: None for name in joined}
    for name, options in joined.items():
        if name not in networks:
            problems.append(f"(j) service {svc!r} joins the undeclared network {name!r}")
        if options:
            problems.append(f"(j) service {svc!r} sets options on network {name!r}")

# (c) exactly one published port: the proxy, on loopback, the verify port,
# container port 80 (443 in LAN mode)
ports = [(svc, p) for svc, service in services.items() for p in service.get('ports') or []]
if not (len(ports) == 1 and ports[0][0] == 'proxy'
        and ports[0][1].get('host_ip') == '127.0.0.1'
        and str(ports[0][1].get('published')) == port
        and ports[0][1].get('target') == target
        and ports[0][1].get('protocol', 'tcp') == 'tcp'):
    shown = ', '.join(f"{svc}:{p.get('host_ip', '')}:{p.get('published')}->{p.get('target')}/{p.get('protocol', 'tcp')}"
                      for svc, p in ports) or 'none'
    problems.append(f"(c) published ports must be exactly proxy:127.0.0.1:{port}->{target}/tcp, not {shown}")

# (d) mounts: volumes and binds only, with no options; binds read-only, from
# inside the checkout but not its exports folder; (e) never the socket
sockets = {'/var/run/docker.sock', '/run/docker.sock'}
checkout_exports = os.path.join(checkout, 'exports')
for svc, service in services.items():
    for mount in service.get('volumes') or []:
        source, target = mount.get('source') or '', mount.get('target') or ''
        if source in sockets or target in sockets or (source and real(source) in {real(s) for s in sockets}):
            problems.append(f"(e) service {svc!r} mounts the Docker socket")
            continue
        if extra(mount, MOUNT_KEYS):
            problems.append(f"(d) service {svc!r} mount {target!r} uses keys not allowed: {extra(mount, MOUNT_KEYS)}")
        if mount.get('type') not in ('volume', 'bind'):
            problems.append(f"(d) service {svc!r} mount {target!r} is of type {mount.get('type')!r}; only volume and bind are allowed")
            continue
        if mount.get('volume'):
            problems.append(f"(d) service {svc!r} mount {target!r} sets volume options")
        if set(mount.get('bind') or {}) - {'create_host_path'}:
            problems.append(f"(d) service {svc!r} mount {target!r} sets bind options")
        if mount.get('type') != 'bind' or (svc == 'api' and target == '/app/exports'):
            continue  # the api's exports: rule (f)
        trusted_asset = (
            svc == 'otel-collector' and source == os.path.join(harness, 'scripts/verify', 'collector.yaml')
            or svc == 'otel-capture' and source == os.path.join(harness, 'scripts/verify'))
        if not (os.path.isabs(source) and (inside(source, checkout) or trusted_asset)):
            problems.append(f"(d) service {svc!r} binds {source!r}, outside the checkout")
        elif inside(source, checkout_exports) or inside(checkout_exports, source):
            problems.append(f"(d) service {svc!r} binds {source!r}, which is or holds the checkout's exports folder")
        elif mount.get('read_only') is not True:
            problems.append(f"(d) service {svc!r} binds {source!r} read-write; binds must be read-only")

# (f) the API's exports are the verify project's own folder
mounts = [m for m in (services.get('api', {}).get('volumes') or []) if m.get('target') == '/app/exports']
if not (len(mounts) == 1 and mounts[0].get('type') == 'bind' and real(mounts[0].get('source') or '/') == real(exports)):
    problems.append(f"(f) the api's /app/exports must be bound from {exports}, not "
                    f"{[m.get('source') for m in mounts] or 'nothing'}")

if problems:
    print('The resolved compose configuration is refused:')
    for problem in problems:
        print('  ' + problem)
    sys.exit(2)
GUARDPY
  rc=$?
  rm -f "$config"
  [ "$rc" -eq 0 ] || fail "the verify project's configuration is refused (see above); nothing was built or started."
}

# ── LAN mode (#26) ───────────────────────────────────────────────────────────
# From the default configuration, up and healthy, to LAN mode, as the README
# tells users: create the first Engineer, then switch. do_up always clears the
# project first, so it is never set up here. The generated password is
# discarded unseen; the suites' account setup resets it like any password it
# doesn't know.
lan_switch() {
  local i code=000 service state health
  printf 'creating the first Engineer, then switching to LAN mode...\n'
  docker compose -p "$PROJECT" exec -T api python -m auth bootstrap vfy-engineer >/dev/null \
    || fail "could not create the first Engineer for LAN mode."
  export COMPOSE_FILE="$LAN_COMPOSE_FILE"
  GUARD_MODE=lan
  do_guard
  if ! docker compose -p "$PROJECT" up -d --no-build --pull never --wait --wait-timeout 300; then
    docker compose -p "$PROJECT" ps -a --format '{{.Service}} {{.State}} {{.Health}}' \
      | while read -r service state health; do
          case "$state/${health:-}" in
            running/healthy|running/) ;;
            *) printf '\n── %s (%s %s) ──\n' "$service" "$state" "${health:-}"
               docker compose -p "$PROJECT" logs --tail 50 "$service" ;;
          esac
        done
    fail "the verify project did not come up in LAN mode."
  fi
  rm -f "$LAN_CERT"
  (umask 077 && docker compose -p "$PROJECT" exec -T proxy cat /etc/nginx/certs/live/cert.pem > "$LAN_CERT") \
    && [ -s "$LAN_CERT" ] || fail "could not copy LAN mode's certificate from the proxy."
  export RAG_API="https://localhost:$PORT/api"
  # curl and Python's ssl trust only this certificate for the run.
  export CURL_CA_BUNDLE="$LAN_CERT" SSL_CERT_FILE="$LAN_CERT" RAG_VERIFY_LAN_CERT="$LAN_CERT"
  for i in $(seq 1 60); do
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' --cacert "$LAN_CERT" "$RAG_API/health" 2>/dev/null)
    [ "$code" = 200 ] && break
    sleep 1
  done
  [ "$code" = 200 ] || fail "$RAG_API/health did not return 200 over TLS within 60 seconds (last: $code)."
}

# ── query-only mode (#670) ───────────────────────────────────────────────────
# Called by 24_query_only under the inherited lock, once it has exported a
# package from the default configuration: the same project comes up again in
# query-only mode with its own empty volumes and only that package in its
# exports folder, as another machine would have it. Images are kept (no build),
# and so is the model copy, an external volume `down -v` doesn't remove.
query_only_switch() {
  local i code=000 service state health
  [ -f "$EXPORTS/$QO_PACKAGE" ] && [ ! -L "$EXPORTS/$QO_PACKAGE" ] \
    || fail "query-only-mode: no package $QO_PACKAGE in $EXPORTS."
  { [ ! -e "$QO_CARRY" ] && [ ! -L "$QO_CARRY" ]; } || rm -rf "$QO_CARRY" \
    || fail "query-only-mode: could not clear $QO_CARRY."
  mkdir -m 700 "$QO_CARRY" && cp -p "$EXPORTS/$QO_PACKAGE" "$QO_CARRY/" \
    || fail "query-only-mode: could not copy the package out of the exports folder."
  printf 'removing the default instance (its containers, network and volumes)...\n'
  docker compose -p "$PROJECT" down -v --remove-orphans >/dev/null 2>&1 \
    || fail "query-only-mode: could not remove the default instance."
  { [ ! -L "$EXPORTS" ] && rm -rf "$EXPORTS" && mkdir -p "$EXPORTS" \
      && cp -p "$QO_CARRY/$QO_PACKAGE" "$EXPORTS/" && rm -rf "$QO_CARRY"; } \
    || fail "query-only-mode: could not put the package back alone in $EXPORTS."
  export COMPOSE_FILE="$QUERY_ONLY_COMPOSE_FILE"
  do_guard
  printf 'starting the query-only instance...\n'
  if ! docker compose -p "$PROJECT" up -d --no-build --pull never --wait --wait-timeout 900; then
    docker compose -p "$PROJECT" ps -a --format '{{.Service}} {{.State}} {{.Health}}' \
      | while read -r service state health; do
          case "$state/${health:-}" in
            running/healthy|running/) ;;
            *) printf '\n── %s (%s %s) ──\n' "$service" "$state" "${health:-}"
               docker compose -p "$PROJECT" logs --tail 50 "$service" ;;
          esac
        done
    fail "the verify project did not come up in query-only mode."
  fi
  for i in $(seq 1 60); do
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$RAG_API/health" 2>/dev/null)
    [ "$code" = 200 ] && break
    sleep 1
  done
  [ "$code" = 200 ] || fail "$RAG_API/health did not return 200 within 60 seconds in query-only mode (last: $code)."
  printf 'the verify project is up in query-only mode with %s\n' "$QO_PACKAGE"
}

# ── up ───────────────────────────────────────────────────────────────────────
do_up() {
  local i code=000 service state health
  printf 'clearing any leftover verify project...\n'
  do_down >/dev/null || fail "could not remove a leftover verify project."
  mkdir -p "$EXPORTS" || fail "could not create $EXPORTS."
  do_seed
  do_guard
  printf 'building the verify project from %s...\n' "$CHECKOUT"
  docker compose -p "$PROJECT" build ${PULL:+--pull} || fail "the build failed."
  if ! docker compose -p "$PROJECT" up -d --wait --wait-timeout 900; then
    # Weaviate can be slow to report healthy (#130): one more try.
    printf 'the first start failed; trying once more...\n'
    if ! docker compose -p "$PROJECT" up -d --wait --wait-timeout 900; then
      docker compose -p "$PROJECT" ps -a --format '{{.Service}} {{.State}} {{.Health}}' \
        | while read -r service state health; do
            case "$state/${health:-}" in
              running/healthy|running/) ;;
              *) printf '\n── %s (%s %s) ──\n' "$service" "$state" "${health:-}"
                 docker compose -p "$PROJECT" logs --tail 50 "$service" ;;
            esac
          done
      if [ "$EXTERNAL" = 1 ]; then
        printf '\n── the api container'"'"'s Ollama check (#673) ──\n'
        docker compose -p "$PROJECT" exec -T api python -m ollama_check </dev/null || true
      fi
      [ "$CMD" != run ] || fail "the verify project did not come up; run removes it now."
      fail "the verify project did not come up; it is left running for inspection (stack.sh down removes it)."
    fi
  fi
  for i in $(seq 1 60); do
    code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$RAG_API/health" 2>/dev/null)
    [ "$code" = 200 ] && break
    sleep 1
  done
  [ "$code" = 200 ] || fail "$RAG_API/health did not return 200 within 60 seconds (last: $code)."
  [ "$LAN" = 0 ] || lan_switch
  printf '\nThe verify project is up at %s. To point commands at it:\n\n' "${RAG_API%/api}"
  for var in COMPOSE_PROJECT_NAME COMPOSE_FILE RAG_API RAG_EXPECTED_PROXY_PORT RAG_EXPORTS_DIR RAG_VERIFY_PORT RAG_VERIFY_TELEMETRY RAG_VERIFY_LAN RAG_VERIFY_QUERY_ONLY RAG_VERIFY_EXTERNAL_OLLAMA; do
    printf 'export %s=%q\n' "$var" "${!var}"
  done
  if [ "$LAN" = 1 ]; then
    for var in CURL_CA_BUNDLE SSL_CERT_FILE RAG_VERIFY_LAN_CERT; do
      printf 'export %s=%q\n' "$var" "${!var}"
    done
  fi
  if [ "$TELEMETRY" = 1 ]; then
    for var in COMPOSE_PROFILES RAG_VERIFY_HARNESS RAG_VERIFY_CHECKOUT RAG_VERIFY_OTEL_ENABLED RAG_VERIFY_TELEMETRY_PIN; do
      printf 'export %s=%q\n' "$var" "${!var}"
    done
  fi
  printf '\n'
}

case "$CMD" in
  telemetry-mode)
    # Called only by a suite under the inherited lock and explicit opt-in.
    [ "$TELEMETRY" = 1 ] || fail "telemetry-mode requires --telemetry"
    do_guard
    docker compose -p "$PROJECT" up -d --no-deps --no-build --pull never --wait --wait-timeout 120 api || fail "API telemetry reconfiguration failed" ;;
  query-only-mode)
    # Called only by 24_query_only under the inherited lock of a --query-only run.
    query_only_switch ;;
  down)
    do_down || exit 2 ;;
  up)
    do_up ;;
  run)
    TEARDOWN=1
    do_up
    rc=0
    # A LAN run checks LAN mode; other suites assume the default configuration.
    [ "$LAN" = 0 ] || [ "${#ARGS[@]}" -gt 0 ] || ARGS=(18_lan)
    # A query-only run checks query-only mode (#670).
    [ "$QUERY_ONLY" = 0 ] || [ "${#ARGS[@]}" -gt 0 ] || ARGS=(24_query_only)
    # An external-Ollama run checks external-Ollama mode (#673).
    [ "$EXTERNAL" = 0 ] || [ "${#ARGS[@]}" -gt 0 ] || ARGS=(26_external_ollama)
    # In the background with job control on, all.sh leads a process group of
    # its own (pgid $!), which stop_suite ends; and `wait`, unlike a foreground
    # command, returns as soon as INT or TERM arrives, so the traps act at once.
    # Its stdin is /dev/null: a background job keeps the terminal otherwise,
    # and run from one, its first read (docker compose exec -T) would be
    # stopped by SIGTTIN and the run would hang.
    # Record signals until the new process group has a registered owner.
    SPAWN_SIGNAL=0
    trap 'SPAWN_SIGNAL=130' INT
    trap 'SPAWN_SIGNAL=143' TERM
    set -m
    (trap '' TTOU; exec bash "$CHECKOUT/scripts/verify/all.sh" ${ARGS[@]+"${ARGS[@]}"}) </dev/null &
    SUITE=$!
    set +m
    trap 'exit 130' INT
    trap 'exit 143' TERM
    [ "$SPAWN_SIGNAL" -eq 0 ] || exit "$SPAWN_SIGNAL"
    wait "$SUITE" || rc=$?
    exit "$rc" ;;
esac
