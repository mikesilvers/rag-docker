"""The real LAN override, resolved without starting it (#26). Host-side, standard library, Python 3.9+.

    python3 lan_config.py CHECKOUT

Resolves CHECKOUT's docker-compose.yml plus docker-compose.lan.yml with
`docker compose config` (which creates no container, volume or network), under
a project name that is neither rag-docker nor rag-verify, with no .env file and
none of the caller's COMPOSE_* or RAG_LAN_* variables, and checks what LAN mode
publishes and sets: the proxy alone on every interface at port 443 (8443 with
RAG_LAN_PORT=8443), the API in LAN mode with a Secure cookie, the certificate
step in the API image before the proxy, and the read-only mounts. The verify
project never runs this configuration as is: there, docker-compose.lan.verify.yml
keeps the publication on loopback. Prints each problem; exits 0 or 1.
"""
import json
import os
import subprocess
import sys

PROJECT = "rag-lan-config"


def resolve(checkout, port=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("COMPOSE_", "RAG_LAN_"))}
    if port is not None:
        env["RAG_LAN_PORT"] = port
    result = subprocess.run(
        ["docker", "compose", "--env-file", os.devnull, "-p", PROJECT,
         "-f", os.path.join(checkout, "docker-compose.yml"), "-f", os.path.join(checkout, "docker-compose.lan.yml"),
         "config", "--format", "json"],
        cwd=checkout, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        universal_newlines=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError("docker compose config failed: " + result.stderr.strip())
    return json.loads(result.stdout)


def mount(service, target):
    found = [m for m in service.get("volumes") or [] if m.get("target") == target]
    return found[0] if len(found) == 1 else None


def problems(config, checkout, port):
    found = []
    services = config.get("services") or {}
    ports = [(name, p) for name, service in services.items() for p in service.get("ports") or []]
    shown = ", ".join("{}:{}:{}->{}/{}".format(n, p.get("host_ip", ""), p.get("published"), p.get("target"),
                                              p.get("protocol", "tcp")) for n, p in ports) or "none"
    if not (len(ports) == 1 and ports[0][0] == "proxy" and ports[0][1].get("host_ip") == "0.0.0.0"
            and str(ports[0][1].get("published")) == port and ports[0][1].get("target") == 443
            and ports[0][1].get("protocol", "tcp") == "tcp"):
        found.append("publications are {}, not exactly proxy:0.0.0.0:{}->443/tcp".format(shown, port))
    environment = (services.get("api") or {}).get("environment") or {}
    for key in ("RAG_LAN_MODE", "AUTH_COOKIE_SECURE"):
        if environment.get(key) != "true":
            found.append("the api's {} is {!r}, not 'true'".format(key, environment.get(key)))
    init = services.get("tls-init") or {}
    if init.get("image") != "rag-docker-api:latest":
        found.append("tls-init uses {!r}, not the API image rag-docker-api:latest".format(init.get("image")))
    if init.get("command") != ["python", "-m", "tls_cert"]:
        found.append("tls-init runs {!r}, not python -m tls_cert".format(init.get("command")))
    if init.get("ports"):
        found.append("tls-init publishes a port")
    proxy = services.get("proxy") or {}
    wait = (proxy.get("depends_on") or {}).get("tls-init") or {}
    if wait.get("condition") != "service_completed_successfully":
        found.append("the proxy doesn't wait for tls-init to complete successfully")
    expected = [
        ("proxy", "/etc/nginx/nginx.conf", "bind", os.path.join(checkout, "proxy", "nginx.lan.conf"), True),
        ("proxy", "/etc/nginx/rag/locations.conf", "bind", os.path.join(checkout, "proxy", "locations.conf"), True),
        ("proxy", "/etc/nginx/certs", "volume", "lan_certs", True),
        ("tls-init", "/byo", "bind", os.path.join(checkout, "proxy", "certs"), True),
        ("tls-init", "/certs", "volume", "lan_certs", False),
    ]
    for name, target, kind, source, read_only in expected:
        found_mount = mount(services.get(name) or {}, target)
        ok = (found_mount is not None and found_mount.get("type") == kind
              and (os.path.realpath(found_mount.get("source") or "") == os.path.realpath(source)
                   if kind == "bind" else found_mount.get("source") == source)
              and bool(found_mount.get("read_only")) == read_only)
        if not ok:
            found.append("{} doesn't mount {} {} at {}{}".format(
                name, kind, source, target, " read-only" if read_only else ""))
    if "lan_certs" not in (config.get("volumes") or {}):
        found.append("no lan_certs volume")
    return found


def main(argv):
    if len(argv) != 1:
        print(__doc__.split("\n\n")[1], file=sys.stderr)
        return 2
    checkout = os.path.realpath(argv[0])
    failed = False
    for port in (None, "8443"):
        try:
            config = resolve(checkout, port)
        except (RuntimeError, ValueError, OSError, subprocess.SubprocessError) as error:
            print("lan_config: {}".format(error), file=sys.stderr)
            return 1
        for problem in problems(config, checkout, port or "443"):
            failed = True
            print("lan_config{}: {}".format(" (RAG_LAN_PORT=8443)" if port else "", problem), file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
