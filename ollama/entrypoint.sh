#!/bin/bash
set -e

ollama serve &
OLLAMA_PID=$!

# The ollama/ollama image ships only the `ollama` binary -- no curl, wget, nc or
# python3 -- so the CLI is the only available readiness probe. `ollama list`
# exits non-zero until the server is accepting requests.
until ollama list > /dev/null 2>&1; do
  sleep 2
done

# The model names and model_present (#671): the answer and build LLMs, resolved
# from the settings compose passes, and the embedding model.
. "$(dirname "$0")/models.sh"

# Pull a model with retry. Ollama resumes partial downloads so retries are cheap.
# timeout 7200 (2 hours) kills a truly hung connection while allowing slow downloads.
pull_with_retry() {
    local model="$1"

    # Skip if already downloaded.
    if model_present "${model}"; then
        echo "ollama: ${model} already present, skipping pull."
        return 0
    fi

    local attempt=1
    local max_attempts=5
    until timeout 7200 ollama pull "${model}"; do
        if [ "${attempt}" -ge "${max_attempts}" ]; then
            echo "ERROR: failed to pull ${model} after ${max_attempts} attempts." >&2
            exit 1
        fi
        echo "ollama: pull of ${model} failed or timed out (attempt ${attempt}/${max_attempts}), retrying in 15s..."
        attempt=$((attempt + 1))
        sleep 15
    done
    echo "ollama: ${model} ready."
}

# Each distinct model, the answer model first (names hold no spaces).
for model in $(models); do
  pull_with_retry "${model}"
done

wait $OLLAMA_PID
