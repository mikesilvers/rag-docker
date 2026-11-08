"""LAN mode's startup refusal (#26).

docker-compose.lan.yml publishes the proxy on every host interface, over TLS,
and sets RAG_LAN_MODE=true and AUTH_COOKIE_SECURE=true. Before the API serves
anything in that mode it checks that

  (b) AUTH_COOKIE_SECURE is true, so the session cookie never travels over
      plain HTTP, and
  (a) an enabled human Engineer exists, so the system is set up and someone
      can administer it from the LAN,

in that order, and otherwise exits with one line on standard error. The API
then never becomes healthy, so the ui and the proxy, which wait for it, don't
start either and nothing is published: LAN mode fails closed.

main.py calls enforce() before anything else, so uvicorn's import of the app
stops here. Nothing else in the API depends on LAN mode.
"""
from __future__ import annotations

import sys

from config import settings

PREFIX = "LAN mode refused: "


def refusal() -> str | None:
    """The line explaining why LAN mode can't start, or None."""
    if not settings.rag_lan_mode:
        return None
    if not settings.auth_cookie_secure:
        return (PREFIX + "AUTH_COOKIE_SECURE must be true when RAG_LAN_MODE is true, so the session cookie is "
                "sent only over TLS. Start LAN mode with docker-compose.lan.yml, which sets both.")
    from auth import store
    if not store.has_enabled_engineer():
        return (PREFIX + "there is no enabled AI Engineer account. Create the first one in the default "
                "(loopback) mode, then switch: docker compose up -d && docker compose exec api python -m auth "
                "bootstrap <username> (README, \"Sharing with your office\").")
    return None


def enforce() -> None:
    """Exit with the refusal line if LAN mode can't start; otherwise do nothing."""
    reason = refusal()
    if reason:
        # One line, and SystemExit, which Python doesn't print as a traceback.
        print(reason, file=sys.stderr, flush=True)
        raise SystemExit(1)
