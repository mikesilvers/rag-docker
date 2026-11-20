"""The real query-only overlay, resolved without starting it (#670). Host-side, standard library, Python 3.9+.

    python3 query_only_config.py CHECKOUT

Resolves CHECKOUT's docker-compose.yml alone and with docker-compose.query-only.yml
through `docker compose config` (which creates no container, volume or network),
under a project name that is neither rag-docker nor rag-verify, with no .env file
and none of the caller's COMPOSE_* or RAG_* variables, and checks that the overlay
changes exactly one thing: the API's RAG_QUERY_ONLY, from "false" to "true". No
other service, setting, port, volume or model changes. Prints each problem;
exits 0 or 1.
"""
import json
import os
import subprocess
import sys

PROJECT = "rag-query-only-config"


def resolve(checkout, overlay):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "RAG_"))}
    files = ["-f", os.path.join(checkout, "docker-compose.yml")]
    if overlay:
        files += ["-f", os.path.join(checkout, "docker-compose.query-only.yml")]
    result = subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, "-p", PROJECT, *files, "config", "--format", "json"],
        cwd=checkout, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError("docker compose config failed: " + result.stderr.strip())
    return json.loads(result.stdout)


def problems(base, query_only):
    found = []
    base_env = ((base.get("services") or {}).get("api") or {}).get("environment") or {}
    qo_env = ((query_only.get("services") or {}).get("api") or {}).get("environment") or {}
    if base_env.get("RAG_QUERY_ONLY") != "false":
        found.append("the base file's api must set RAG_QUERY_ONLY to \"false\", not {!r}".format(
            base_env.get("RAG_QUERY_ONLY")))
    if qo_env.get("RAG_QUERY_ONLY") != "true":
        found.append("with the overlay the api's RAG_QUERY_ONLY must be \"true\", not {!r}".format(
            qo_env.get("RAG_QUERY_ONLY")))
    # Everything else is the same: put the one setting back and compare.
    changed = json.loads(json.dumps(query_only))
    api_env = ((changed.get("services") or {}).get("api") or {}).get("environment")
    if isinstance(api_env, dict) and "RAG_QUERY_ONLY" in base_env:
        api_env["RAG_QUERY_ONLY"] = base_env["RAG_QUERY_ONLY"]
    if changed != base:
        keys = sorted(set(base.get("services") or {}) | set(changed.get("services") or {}))
        differ = [k for k in keys if (base.get("services") or {}).get(k) != (changed.get("services") or {}).get(k)]
        found.append("the overlay changes more than the api's RAG_QUERY_ONLY (services differing: {})".format(
            ", ".join(differ) or "none; a top-level key differs"))
    return found


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    checkout = os.path.realpath(argv[1])
    try:
        found = problems(resolve(checkout, False), resolve(checkout, True))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print("    " + str(exc), file=sys.stderr)
        return 1
    for problem in found:
        print("    " + problem, file=sys.stderr)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
