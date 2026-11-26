#!/usr/bin/env bash
#
# Check an external Ollama for rag-docker (#673): README "External Ollama (native)".
#
#   bash scripts/check-external-ollama.sh              # on the host, from anywhere
#   bash scripts/check-external-ollama.sh --host-only  # only the listener check
#
# 1. On this host (macOS or Linux): what listens on TCP port 11434. Ollama has
#    no authentication, so it should listen only where Docker reaches it:
#    loopback (127.0.0.1, ::1) with Docker Desktop, and on Linux the docker0
#    bridge address too. Any other address, or a wildcard (0.0.0.0, ::, *), is
#    a warning: other machines on your network may reach it.
# 2. From inside the api container: `python -m ollama_check`, which reaches
#    Ollama as the stack does and checks its version and the models. It uses
#    COMPOSE_FILE when set, else docker-compose.yml with
#    docker-compose.external-ollama.yml.
#
# Exit status: 0 every check passed; 3 passed, with warnings; 1 a problem;
# 2 a usage error. It only reads: it never pulls, changes or unloads a model,
# and never changes Ollama's settings. Windows: see the README's PowerShell
# lines (not yet verified).
set -uo pipefail

usage() {
  sed -n '5,6p' "${BASH_SOURCE[0]}" | sed 's/^#  //' >&2
  exit 2
}

HOST_ONLY=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --host-only) HOST_ONLY=1; shift ;;
    -h|--help) usage ;;
    *) usage ;;
  esac
done

PORT=11434
WARNINGS=0
PROBLEMS=0
say() { printf 'check-external-ollama: %s: %s\n' "$1" "$2"; }
warn() { WARNINGS=$((WARNINGS + 1)); say warning "$1"; }
problem() { PROBLEMS=$((PROBLEMS + 1)); say problem "$1"; }

OS="$(uname -s)"

# The docker0 bridge address on Linux (where Ollama listens for the containers),
# or nothing.
docker0_address() {
  [ "$OS" = Linux ] || return 0
  command -v ip >/dev/null 2>&1 || return 0
  ip -4 -o addr show docker0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1
}

# Each address something listens on at $PORT, one per line, brackets removed.
listeners() {
  case "$OS" in
    Darwin)
      lsof -nP -iTCP:"$PORT" -sTCP:LISTEN 2>/dev/null \
        | awk -v p=":$PORT" 'NR > 1 { for (i = 1; i <= NF; i++) if (index($i, p) && substr($i, length($i) - length(p) + 1) == p) print $i }'
      ;;
    *)
      ss -ltnH "sport = :$PORT" 2>/dev/null | awk '{print $4}'
      ;;
  esac | sed -e "s/:$PORT\$//" -e 's/^\[//' -e 's/\]$//' -e 's/%.*$//' | sort -u
}

host_check() {
  local tool addresses address docker0 allowed
  case "$OS" in
    Darwin) tool=lsof ;;
    Linux) tool=ss ;;
    *) warn "the listener check runs on macOS and Linux only; on Windows use the README's PowerShell lines."
       return 0 ;;
  esac
  if ! command -v "$tool" >/dev/null 2>&1; then
    warn "couldn't check what listens on port $PORT: '$tool' isn't installed."
    return 0
  fi
  docker0="$(docker0_address)"
  allowed="127.0.0.1 or ::1"
  [ -z "$docker0" ] || allowed="$docker0 (docker0), 127.0.0.1 or ::1"
  addresses="$(listeners)"
  if [ -z "$addresses" ]; then
    problem "nothing listens on port $PORT on this host: start Ollama (README \"External Ollama (native)\")."
    return 0
  fi
  while IFS= read -r address; do
    case "$address" in
      127.0.0.1|::1)
        say ok "Ollama listens on $address:$PORT (loopback)" ;;
      0.0.0.0|::|'*')
        warn "Ollama listens on every address ($address:$PORT), so other machines on your network may reach it. Ollama has no authentication; bind it to $allowed (README \"External Ollama (native)\")." ;;
      *)
        if [ -n "$docker0" ] && [ "$address" = "$docker0" ]; then
          say ok "Ollama listens on $address:$PORT (docker0)"
        else
          warn "Ollama listens on $address:$PORT, which other machines on your network may reach. Ollama has no authentication; bind it to $allowed (README \"External Ollama (native)\")."
        fi ;;
    esac
  done <<< "$addresses"
}

host_check

if [ "$HOST_ONLY" = 0 ]; then
  cd "$(dirname "${BASH_SOURCE[0]}")/.." || { problem "couldn't open the repository folder."; exit 1; }
  export COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.yml:docker-compose.external-ollama.yml}"
  if ! docker compose exec -T api python -m ollama_check </dev/null; then
    problem "the api container's check failed (above), or the api container isn't running."
  fi
fi

if [ "$PROBLEMS" -gt 0 ]; then
  exit 1
elif [ "$WARNINGS" -gt 0 ]; then
  exit 3
fi
exit 0
