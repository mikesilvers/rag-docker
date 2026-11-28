# Shared by scripts/backup.sh and scripts/restore.sh (#848). Sourced, never run.
#
# Every container these scripts start runs the age image (scripts/age) with no
# network and no log driver: Docker's default log driver would keep a copy of
# what a container writes to stdout, which is a new key or an encrypted stream.
# Written for macOS's /bin/bash 3.2 as well as a Linux bash.

# Every named volume of docker-compose.yml and docker-compose.lan.yml except
# ollama_models, which holds only downloadable models.
RB_VOLUMES="weaviate_data ingest_uploads rag_sources rag_preparation auth_data lan_certs"
RB_NAME=$(basename "$0")
# The checkout, found from this file's own path, so another script can source
# it (package-offline.sh does, for rb_image).
RB_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)
RB_YES=no
RB_PREFIX=

rb_die() {
  echo "$RB_NAME: $*" >&2
  exit 1
}

rb_need_docker() {
  command -v docker >/dev/null 2>&1 || rb_die "docker is not on PATH"
}

# The SHA-256 of stdin, in hex: sha256sum where there is one (Linux), else
# shasum (macOS).
rb_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum | awk '{print $1}'
  elif command -v shasum >/dev/null 2>&1; then
    shasum -a 256 | awk '{print $1}'
  else
    rb_die "neither sha256sum nor shasum is on PATH"
  fi
}

# Prints the text the age image's tag hashes (#856, #862): the parser
# directives of scripts/age/Dockerfile (the `# key=value` lines at its top,
# up to the first line that isn't one) and its other lines that are neither
# blank nor a comment, in file order; then, for every other entry of
# scripts/age that isn't a folder, in byte order of its path there, the line
# `file <path>` and its content's SHA-256, or `link <path>` and its target.
rb_age_text() {
  local dir="$RB_ROOT/scripts/age" rel target
  awk 'BEGIN { d = 1 }
       d && /^[[:space:]]*#[[:space:]]*[A-Za-z][A-Za-z0-9_-]*[[:space:]]*=/ { print; next }
       { d = 0 }
       /^[[:space:]]*#/ || /^[[:space:]]*$/ { next }
       { print }' "$dir/Dockerfile" || return 1
  # The listing runs in a subshell with pipefail, so a failed find, sed or sort
  # fails the function (#937) without setting the option in the caller. The
  # loop runs in a subshell of its own (the last part of a pipeline): `exit`
  # ends it.
  (
    set -o pipefail
    (cd "$dir" && find . ! -type d ! -path ./Dockerfile) | sed 's|^\./||' | LC_ALL=C sort |
      while IFS= read -r rel; do
        if [ -L "$dir/$rel" ]; then
          target=$(readlink "$dir/$rel") || exit 1
          printf 'link %s\n%s\n' "$rel" "$target"
        else
          printf 'file %s\n' "$rel"
          rb_sha256 < "$dir/$rel" || exit 1
        fi
      done
  )
}

# Sets RB_IMAGE, building the image if this machine doesn't have it. The tag
# holds the age package version and the first 12 hex digits of the SHA-256 of
# rb_age_text's text, so a change of any instruction (the two pins included),
# of a parser directive, or of any other file in scripts/age builds a new
# image instead of reusing the old one; an edit of a comment doesn't.
rb_image() {
  local version pinned text hash
  version=$(sed -n 's/^RUN apk add --no-cache age=\([0-9A-Za-z._-]*\)$/\1/p' "$RB_ROOT/scripts/age/Dockerfile")
  [ -n "$version" ] || rb_die "scripts/age/Dockerfile names no age version"
  pinned=$(sed -n 's/^FROM [^@ ]*@sha256:\([0-9a-f]\{12\}\)[0-9a-f]\{52\}$/\1/p' "$RB_ROOT/scripts/age/Dockerfile")
  [ -n "$pinned" ] || rb_die "scripts/age/Dockerfile names no alpine digest"
  text=$(rb_age_text) || exit 1
  hash=$(printf '%s\n' "$text" | rb_sha256) || exit 1
  RB_IMAGE="rag-docker-age:$version-${hash:0:12}"
  if ! docker image inspect "$RB_IMAGE" >/dev/null 2>&1; then
    echo "Building the age image $RB_IMAGE from scripts/age (once; this needs the network)..."
    docker build -q -t "$RB_IMAGE" "$RB_ROOT/scripts/age" >/dev/null ||
      rb_die "could not build the age image $RB_IMAGE; nothing was changed"
  fi
}

rb_run() {
  docker run --rm --network none --log-driver none "$@"
}

# Prints what age or Docker said ($1, captured stderr) to stderr, indented. A
# line holding a secret key is never printed (age doesn't print one; this is a
# second guard).
rb_show_error() {
  printf '%s\n' "$1" | grep -v -e 'AGE-SECRET-KEY-' -e '^[[:space:]]*$' | sed 's/^/  /' >&2
  return 0
}

# Sets RB_PREFIX: --prefix, else COMPOSE_PROJECT_NAME, else the checkout
# folder's name as compose derives a project name from it.
rb_prefix() {
  if [ -n "$1" ]; then
    RB_PREFIX=$1
  elif [ -n "${COMPOSE_PROJECT_NAME:-}" ]; then
    RB_PREFIX=$COMPOSE_PROJECT_NAME
  else
    RB_PREFIX=$(basename "$RB_ROOT" | tr 'A-Z' 'a-z' | LC_ALL=C tr -cd 'a-z0-9_-')
  fi
  printf '%s\n' "$RB_PREFIX" | grep -Eq '^[a-z0-9][a-z0-9_-]*$' ||
    rb_die "'$RB_PREFIX' is not a compose project name (lower-case letters, digits, '_' and '-'); pass --prefix <name>"
}

# The projects that have at least one of these volumes.
rb_candidates() {
  local v
  for v in $RB_VOLUMES; do
    docker volume ls --filter "label=com.docker.compose.volume=$v" --format '{{.Label "com.docker.compose.project"}}'
  done | grep -v '^$' | sort -u | tr '\n' ' '
}

rb_exists() {
  docker volume inspect "${RB_PREFIX}_$1" >/dev/null 2>&1
}

rb_count() {
  rb_run -v "${RB_PREFIX}_$1:/data:ro" "$RB_IMAGE" sh -c 'find /data -mindepth 1 ! -path "/data/.rag-docker-restore.*" | wc -l' | tr -d ' '
}

rb_volume_list_line() {
  if [ "$2" = 0 ]; then
    echo "  ${RB_PREFIX}_$1  (empty)"
  else
    echo "  ${RB_PREFIX}_$1  $2 files and folders"
  fi
}

rb_confirm() {
  local answer
  [ "$RB_YES" = yes ] && return 0
  [ -t 0 ] || rb_die "no terminal to confirm on: run it in a terminal, or pass --prefix <name> --yes. Nothing was changed."
  printf '%s [y/N] ' "$1"
  answer=
  read -r answer || true
  case "$answer" in
    y|yes) ;;
    *) echo "Stopped: nothing was changed."; exit 1 ;;
  esac
}

# Stops the project's running containers, whichever compose files started them.
# Never removes a container or a volume.
rb_stop() {
  local ids count
  ids=$(docker ps -q --filter "label=com.docker.compose.project=$RB_PREFIX")
  if [ -z "$ids" ]; then
    echo "No container of project '$RB_PREFIX' is running."
    return 0
  fi
  count=$(printf '%s\n' "$ids" | wc -l | tr -d ' ')
  echo "Stopping the $count running container(s) of project '$RB_PREFIX'..."
  # shellcheck disable=SC2086
  docker stop $ids >/dev/null || rb_die "could not stop the containers of project '$RB_PREFIX'"
}

rb_start_hint() {
  echo "The stack is stopped. To start it again, in $RB_ROOT:"
  echo "  docker compose up -d"
  echo "In LAN mode:"
  echo "  docker compose -f docker-compose.yml -f docker-compose.lan.yml up -d"
}

# The absolute physical path of $1, without creating anything.
rb_abspath() {
  local path=$1 rest=
  case "$path" in /*) ;; *) path="$PWD/$path" ;; esac
  while [ ! -d "$path" ]; do
    rest="/$(basename "$path")$rest"
    path=$(dirname "$path")
  done
  # A '.' or '..' in the part that doesn't exist yet can't be resolved safely.
  case "$rest/" in */./*|*/../*) return 1 ;; esac
  path=$(cd "$path" && pwd -P) || return 1
  [ "$path" = / ] && path=
  printf '%s\n' "$path$rest"
}
