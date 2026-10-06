#!/usr/bin/env bash
#
# Run the verification suite on a disposable compose project, never on the
# live stack (#152).
#
#   bash scripts/verify/stack.sh run [--checkout DIR] [suite ...]
#   bash scripts/verify/stack.sh up [--checkout DIR] [--pull]
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
case "$CMD" in up|down|run) ;; *) usage ;; esac
CHECKOUT="$HARNESS"
PULL=""
ARGS=()
while [ "$#" -gt 0 ]; do
  case "$1" in
    --checkout)
      [ "$CMD" != down ] && [ "$#" -ge 2 ] || usage
      CHECKOUT="$2"; shift 2 ;;
    --pull)
      [ "$CMD" = up ] || usage
      PULL=1; shift ;;
    -*) usage ;;
    *)
      [ "$CMD" = run ] || usage
      ARGS+=("$1"); shift ;;
  esac
done

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
  python3 - "$CHECKOUT" "$EXPORTS" "$PORT" "$config" <<'GUARDPY'
import json, os, sys

checkout, exports, port, path = sys.argv[1:5]
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
    if extra(service, SERVICE_KEYS):
        problems.append(f"(h) service {svc!r} uses keys not allowed: {extra(service, SERVICE_KEYS)}")

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

# (c) exactly one published port: the proxy, on loopback, the verify port
ports = [(svc, p) for svc, service in services.items() for p in service.get('ports') or []]
if not (len(ports) == 1 and ports[0][0] == 'proxy'
        and ports[0][1].get('host_ip') == '127.0.0.1'
        and str(ports[0][1].get('published')) == port
        and ports[0][1].get('target') == 80
        and ports[0][1].get('protocol', 'tcp') == 'tcp'):
    shown = ', '.join(f"{svc}:{p.get('host_ip', '')}:{p.get('published')}->{p.get('target')}/{p.get('protocol', 'tcp')}"
                      for svc, p in ports) or 'none'
    problems.append(f"(c) published ports must be exactly proxy:127.0.0.1:{port}->80/tcp, not {shown}")

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
        if not (os.path.isabs(source) and inside(source, checkout)):
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
  printf '\nThe verify project is up at http://localhost:%s. To point commands at it:\n\n' "$PORT"
  for var in COMPOSE_PROJECT_NAME COMPOSE_FILE RAG_API RAG_EXPECTED_PROXY_PORT RAG_EXPORTS_DIR RAG_VERIFY_PORT; do
    printf 'export %s=%q\n' "$var" "${!var}"
  done
  printf '\n'
}

case "$CMD" in
  down)
    do_down || exit 2 ;;
  up)
    do_up ;;
  run)
    TEARDOWN=1
    do_up
    rc=0
    # In the background with job control on, all.sh leads a process group of
    # its own (pgid $!), which stop_suite ends; and `wait`, unlike a foreground
    # command, returns as soon as INT or TERM arrives, so the traps act at once.
    # Its stdin is /dev/null: a background job keeps the terminal otherwise,
    # and run from one, its first read (docker compose exec -T) would be
    # stopped by SIGTTIN and the run would hang.
    set -m
    bash "$CHECKOUT/scripts/verify/all.sh" ${ARGS[@]+"${ARGS[@]}"} </dev/null &
    SUITE=$!
    set +m
    wait "$SUITE" || rc=$?
    exit "$rc" ;;
esac
