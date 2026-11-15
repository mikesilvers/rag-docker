#!/usr/bin/env bash
# The models the ollama service pulls (#671), on the host: the names
# ollama/models.sh resolves from the LLM settings `docker compose config` gives
# the ollama service, one per line. package-offline.sh and the CI model cache
# read them here, so they read what the entrypoint pulls.
#
#   bash ollama/compose-models.sh
#
# Run from the folder docker compose reads its files from. Needs docker compose
# and python3. Exits non-zero, with a message, when it can't give the names.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

config="$(docker compose config --format json)" \
  || { echo "compose-models.sh: docker compose config failed." >&2; exit 1; }

# LLM_MODEL, BUILD_LLM_MODEL, ANSWER_LLM_MODEL, one per line, each after "v:" so
# an empty one still has its line.
settings="$(printf '%s' "$config" | python3 -c '
import json, re, sys
try:
    services = json.load(sys.stdin)["services"]
    env = services["ollama"].get("environment") or {}
except (ValueError, KeyError, TypeError, AttributeError):
    sys.exit("compose-models.sh: the compose config has no ollama service.")
for key in ("LLM_MODEL", "BUILD_LLM_MODEL", "ANSWER_LLM_MODEL"):
    value = env.get(key) or ""
    if not re.fullmatch(r"[A-Za-z0-9._:/-]*", value):
        sys.exit("compose-models.sh: %s is not a model name: %r" % (key, value))
    print("v:" + value)
')" || exit 1

{ read -r llm; read -r build; read -r answer; } <<< "$settings"
llm="${llm#v:}"; build="${build#v:}"; answer="${answer#v:}"
names="$(env -i PATH="$PATH" LLM_MODEL="$llm" BUILD_LLM_MODEL="$build" ANSWER_LLM_MODEL="$answer" \
  bash "$here/models.sh" list)" \
  || { echo "compose-models.sh: ollama/models.sh failed." >&2; exit 1; }
[ -n "$names" ] || { echo "compose-models.sh: no model names." >&2; exit 1; }
printf '%s\n' "$names"
