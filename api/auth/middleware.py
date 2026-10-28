"""Enforces the endpoint/role matrix on every API request (#25).

A pure ASGI middleware rather than a FastAPI dependency: FastAPI reads and
parses a request's body before it runs dependencies, so an unauthenticated
512 MB upload would be received in full before being refused. This answers
before the route runs, and never reads the body of a request it refuses.

The refusals, in the order they apply:
  503 SETUP_REQUIRED          no account exists yet (python -m auth bootstrap)
  401 AUTH_REQUIRED           no valid session cookie
  403 CROSS_ORIGIN_REFUSED    a cookie-authenticated POST/PUT/PATCH/DELETE from another origin
  403 PASSWORD_CHANGE_REQUIRED the user must change a generated password first
  403 FORBIDDEN               the user's role is too low

A request with an `Authorization` header is a service key request (PR 3): the
header alone decides and any cookie is ignored, with no fallback to it.
  503 SETUP_REQUIRED          as above
  401 KEY_INVALID             not one `Bearer <key>` header with a valid key (one answer for every reason)
  403 KEY_NOT_ALLOWED         a route keys can't use (policy.HUMAN_ONLY)
  403 FORBIDDEN               the principal's role is too low
It has no origin check (it carries no cookie, Q26) and no password change.

A route with no policy entry is never matched here, so it falls through to the
app; test_auth.py fails when any route lacks an entry.
"""
from __future__ import annotations

import asyncio
from http.cookies import CookieError, SimpleCookie
from types import SimpleNamespace
from urllib.parse import urlsplit

from starlette.requests import Request

from utils import api_error

from . import keys, policy, store

UNSAFE = frozenset(("POST", "PUT", "PATCH", "DELETE"))
# Starlette's route patterns end in `$`, which also matches before a trailing
# newline, so it routes `/ingest/upload\n` (sent as %0A) to the upload handler
# while a policy template wouldn't match it. No route has a control character
# in its path, so such a path is refused outright, before anything else.
_CONTROL = frozenset(chr(c) for c in range(32)) | {"\x7f"}
_DEFAULT_PORTS = {"http": 80, "https": 443}


def _header(scope, name: bytes) -> str | None:
    for key, value in scope.get("headers") or ():
        if key == name:
            return value.decode("latin-1")
    return None


def _headers(scope, name: bytes) -> list[str]:
    return [value.decode("latin-1") for key, value in scope.get("headers") or () if key == name]


def session_token(scope) -> str | None:
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    cookies = SimpleCookie()
    try:
        cookies.load(raw)
    except CookieError:
        return None
    morsel = cookies.get(store.COOKIE)
    return morsel.value if morsel else None


def same_origin(scope) -> bool:
    """Origin names the Host the request was sent to; or, with no Origin, the
    browser says the request is same-origin. Anything else is cross-origin.

    The proxy forwards the browser's Host with its port ($http_host), so
    localhost:8080 is compared with localhost:8080, not with localhost.
    """
    origin = _header(scope, b"origin")
    if origin is None:
        return _header(scope, b"sec-fetch-site") == "same-origin"
    host = _header(scope, b"host")
    if not host or origin == "null":
        return False
    try:
        sent = urlsplit(origin)
        if sent.scheme not in _DEFAULT_PORTS or not sent.hostname:
            return False
        target = urlsplit("//" + host)
        default = _DEFAULT_PORTS[sent.scheme]
        return (sent.hostname == (target.hostname or "")
                and (sent.port or default) == (target.port or default))
    except ValueError:
        return False


class AuthMiddleware:
    """Matches the request against the policy's own route templates, so it
    depends on no router internals; test_auth.py checks that the templates and
    the app's routes are the same set and resolve to each other."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if any(ch in _CONTROL for ch in scope["path"]):
            # Answered here, so the body of such a request is never read.
            return await api_error(404, "NOT_FOUND", "No API route matches this path.")(scope, receive, send)
        template = policy.match(scope["method"], scope["path"])
        if template is None:
            # No route for this method and path: the app's own 404 or 405, which
            # reveal nothing protected.
            return await self.app(scope, receive, send)
        access = policy.required(scope["method"], template)
        if access == policy.PUBLIC:
            return await self.app(scope, receive, send)
        refusal, principal = await self._check(scope, access, template)
        if refusal is not None:
            # Request tracing labels a request by the route it reached (its
            # `path`); give a refused request the same label the router would.
            scope["route"] = SimpleNamespace(path=template)
            return await refusal(scope, receive, send)
        scope.setdefault("state", {})["auth"] = principal
        return await self.app(scope, receive, send)

    async def _check(self, scope, access, template):
        if not await asyncio.to_thread(store.is_set_up):
            return api_error(503, "SETUP_REQUIRED",
                             "No account exists yet. An operator must create the first Engineer with "
                             "`docker compose exec api python -m auth bootstrap <username>`."), None
        authorization = _headers(scope, b"authorization")
        if authorization:
            return await self._check_key(access, template, authorization)
        principal = await asyncio.to_thread(store.authenticate, session_token(scope))
        if principal is None:
            return api_error(401, "AUTH_REQUIRED", "Log in to use this."), None
        if scope["method"] in UNSAFE and not same_origin(scope):
            return api_error(403, "CROSS_ORIGIN_REFUSED",
                             "This request came from another site and was refused."), None
        if principal.must_change_password and access != policy.SESSION:
            return api_error(403, "PASSWORD_CHANGE_REQUIRED",
                             "Change your password before doing anything else."), None
        if access != policy.SESSION and policy.rank(principal.role) < policy.rank(access):
            return api_error(403, "FORBIDDEN", "Your role can't do this.",
                             detail={"required_role": access}), None
        return None, principal

    async def _check_key(self, access, template, authorization):
        key = keys.bearer(authorization[0]) if len(authorization) == 1 else None
        principal = await asyncio.to_thread(store.authenticate_key, key) if key else None
        if principal is None:
            return api_error(401, "KEY_INVALID", "The API key is invalid, expired or revoked."), None
        if template in policy.HUMAN_ONLY:
            return api_error(403, "KEY_NOT_ALLOWED", "API keys can't be used for this."), None
        if access != policy.SESSION and policy.rank(principal.role) < policy.rank(access):
            return api_error(403, "FORBIDDEN", "Your role can't do this.",
                             detail={"required_role": access}), None
        return None, principal


class Unlisted(Exception):
    """A route reached with no policy entry, or with no session the policy needs."""


def require_listed(request: Request) -> None:
    """A global FastAPI dependency: the second, fail-closed half of the policy.

    The middleware answers for every route in the policy. A route added without
    an entry would reach its handler unchecked; this refuses it instead (403 for
    everyone), and also refuses a protected route the middleware let through
    without a session.
    """
    route = request.scope.get("route")
    access = policy.required(request.method, getattr(route, "path", ""))
    if access is None:
        raise Unlisted()
    if access != policy.PUBLIC and request.scope.get("state", {}).get("auth") is None:
        raise Unlisted()
