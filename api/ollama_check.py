"""Checks the Ollama this instance uses, from inside the api container (#673).

    docker compose exec -T api python -m ollama_check

scripts/check-external-ollama.sh runs it after its own host-side check. One line
per check on standard output, each starting "ollama-check: ok:" or
"ollama-check: problem:", then exit status 0 when every check passed and 1
otherwise. It only reads (GET /api/version and /api/tags): it never pulls,
loads, unloads or changes a model, so it is safe on an Ollama other apps share.
"""
from __future__ import annotations

import sys

import httpx

from config import settings
from services.ollama_client import required_models, tagged

PREFIX = "ollama-check: "


def run(client: httpx.Client, out=sys.stdout) -> int:
    base = f"http://{settings.ollama_host}:{settings.ollama_port}"
    problems = 0

    def say(kind: str, text: str) -> None:
        nonlocal problems
        problems += kind == "problem"
        print(f"{PREFIX}{kind}: {text}", file=out, flush=True)

    if settings.rag_external_ollama:
        say("ok", "external Ollama mode is on (RAG_EXTERNAL_OLLAMA=true)")
    else:
        say("problem", "external mode is off: start the stack with "
                       "docker-compose.external-ollama.yml (README \"External Ollama (native)\")")
    try:
        resp = client.get(f"{base}/api/version")
        resp.raise_for_status()
        say("ok", f"reached Ollama {resp.json().get('version') or '(version not given)'} at {base}")
    except Exception as exc:                          # noqa: BLE001
        say("problem", f"can't reach Ollama at {base} from the api container: "
                       f"{type(exc).__name__}: {exc}. On Docker Desktop (macOS, Windows), check "
                       "that Ollama is running on the host. On Linux, check that Ollama listens "
                       "on the docker0 address (README \"External Ollama (native)\").")
        return 1
    try:
        resp = client.get(f"{base}/api/tags")
        resp.raise_for_status()
        reported = {tagged(m["name"]) for m in resp.json().get("models", [])}
    except Exception as exc:                          # noqa: BLE001
        say("problem", f"couldn't list Ollama's models: {type(exc).__name__}: {exc}")
        return 1
    for model in required_models():
        if model in reported:
            say("ok", f"model {model} is present")
        else:
            say("problem", f"model {model} is missing. On the Ollama host, run: ollama pull {model}")
    return 1 if problems else 0


def main() -> int:
    with httpx.Client(timeout=10.0) as client:
        return run(client)


if __name__ == "__main__":
    sys.exit(main())
