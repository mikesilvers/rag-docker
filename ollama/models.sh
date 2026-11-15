#!/bin/bash
# The Ollama models this stack needs (#671), from the three LLM settings
# docker-compose.yml gives the ollama service (its environment):
#
#   the answer model  ANSWER_LLM_MODEL, else LLM_MODEL, else llama3.1:8b
#   the build model   BUILD_LLM_MODEL,  else LLM_MODEL, else llama3.1:8b
#   the embedding model, nomic-embed-text
#
# An empty setting counts as not set. The API resolves its two models the same
# way (api/config.py). This is the one place the ollama side resolves them: the
# entrypoint pulls these names, the healthcheck checks them, and
# package-offline.sh and the CI model cache read them through
# ollama/compose-models.sh.
#
#   bash models.sh [list]   print the names, one per line: the answer model, the
#                           build model when it is a different model, the
#                           embedding model
#   bash models.sh present  exit 0 only when `ollama list` has every name
#
# Sourced (by entrypoint.sh), it only defines the functions. Written for bash
# 3.2 too, which the host tests run it with.

_llm="${LLM_MODEL:-llama3.1:8b}"
ANSWER_MODEL="${ANSWER_LLM_MODEL:-$_llm}"
BUILD_MODEL="${BUILD_LLM_MODEL:-$_llm}"
EMBED_MODEL_NAME=nomic-embed-text

# `name:tag`, with `:latest` for a name without a tag (after its last `/`), so
# `x` and `x:latest` are one model, as the API compares them.
tagged() {
    case "${1##*/}" in
        *:*) printf '%s\n' "$1" ;;
        *) printf '%s:latest\n' "$1" ;;
    esac
}

models() {
    printf '%s\n' "${ANSWER_MODEL}"
    if [ "$(tagged "${BUILD_MODEL}")" != "$(tagged "${ANSWER_MODEL}")" ]; then
        printf '%s\n' "${BUILD_MODEL}"
    fi
    printf '%s\n' "${EMBED_MODEL_NAME}"
}

# Is the model downloaded? `ollama list` prints NAME as "<name>:<tag>", with
# ":latest" added to a name pulled without a tag; its columns are separated by
# tabs or spaces, so `read` splits them. The NAME must match exactly: another
# variant of a tagged name (llama3.1:8b-instruct-q8_0 for llama3.1:8b) is a
# different model and doesn't count (#647).
model_present() {
    local name rest
    while read -r name rest; do
        if [ "${name}" = "$1" ] || [ "${name}" = "$1:latest" ]; then
            return 0
        fi
    done < <(ollama list)
    return 1
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    case "${1:-list}" in
        list) models ;;
        present)
            for model in $(models); do
                model_present "${model}" || exit 1
            done ;;
        *) echo "usage: bash models.sh [list|present]" >&2; exit 2 ;;
    esac
fi
