"""The endpoint/role matrix (#25): which role every API route needs.

Every route has exactly one entry; a route with none is refused for everyone,
and test_auth.py fails when a route lacks an entry or an entry names no route.
The client's own idea of its role is never consulted.
"""
from __future__ import annotations

import re

ROLES = ("end_user", "developer", "engineer")   # lowest to highest
PUBLIC = "public"     # no session needed
SESSION = "session"   # any logged-in user, even one who must change their password

END_USER, DEVELOPER, ENGINEER = ROLES

ACCESS: dict[tuple[str, str], str] = {
    # Liveness for the container healthcheck, stack.sh and CI, unchanged (Q8).
    ("GET", "/health"): PUBLIC,
    ("GET", "/auth/status"): PUBLIC,
    ("POST", "/auth/login"): PUBLIC,
    ("GET", "/auth/me"): SESSION,
    ("POST", "/auth/logout"): SESSION,
    ("POST", "/auth/password"): SESSION,
    # What the Q&A page needs (Q4).
    ("GET", "/collections"): END_USER,
    ("POST", "/query"): END_USER,
    ("GET", "/retrieval/config/{collection}"): END_USER,
    # Import, Chunking, Retrieval, Gold Standard and Transfer (Q2, Q4, Q6).
    ("POST", "/collections"): DEVELOPER,
    ("POST", "/ingest/upload"): DEVELOPER,
    ("GET", "/ingest/job/{job_id}"): DEVELOPER,
    ("GET", "/ingest/config/{collection}"): DEVELOPER,
    ("POST", "/ingest/config"): DEVELOPER,
    ("POST", "/retrieval/config"): DEVELOPER,
    ("POST", "/goldstandard/generate"): DEVELOPER,
    ("GET", "/goldstandard/session/{session_id}"): DEVELOPER,
    ("PATCH", "/goldstandard/session/{session_id}/pair/{pair_id}"): DEVELOPER,
    ("POST", "/goldstandard/regenerate"): DEVELOPER,
    ("POST", "/goldstandard/save"): DEVELOPER,
    ("GET", "/goldstandard/download/{filename}"): DEVELOPER,
    ("POST", "/export"): DEVELOPER,
    ("GET", "/export/job/{job_id}"): DEVELOPER,
    ("POST", "/import"): DEVELOPER,
    ("GET", "/import/job/{job_id}"): DEVELOPER,
    ("GET", "/packages"): DEVELOPER,
    ("GET", "/help/transfer"): DEVELOPER,
    ("GET", "/tune/{collection}"): DEVELOPER,          # read by the Transfer page (Q3)
    # Collections management, tuning, the Health page's data (Q3, Q5, Q6).
    ("DELETE", "/collections/{name}"): ENGINEER,
    ("POST", "/tune/rechunk"): ENGINEER,
    ("POST", "/tune/reembed"): ENGINEER,
    ("POST", "/tune/reindex"): ENGINEER,
    ("GET", "/tune/job/{job_id}"): ENGINEER,
    ("GET", "/metrics/latency"): ENGINEER,
    ("GET", "/goldstandard/diagnostics"): ENGINEER,
    # Account administration (Q20, PR 2 of #25). No route deletes an account.
    ("GET", "/auth/users"): ENGINEER,
    ("POST", "/auth/users"): ENGINEER,
    ("POST", "/auth/users/{username}/reset-password"): ENGINEER,
    ("POST", "/auth/users/{username}/role"): ENGINEER,
    ("POST", "/auth/users/{username}/disable"): ENGINEER,
    ("POST", "/auth/users/{username}/enable"): ENGINEER,
    # FastAPI's own API docs (N5).
    ("GET", "/docs"): ENGINEER,
    ("GET", "/docs/oauth2-redirect"): ENGINEER,
    ("GET", "/redoc"): ENGINEER,
    ("GET", "/openapi.json"): ENGINEER,
}


def required(method: str, path: str) -> str | None:
    """The access a route needs, or None when it has no entry (refused)."""
    return ACCESS.get(("GET" if method == "HEAD" else method, path))


def rank(role: str) -> int:
    return ROLES.index(role) if role in ROLES else -1


def _compile(template: str):
    # A {name} parameter matches one path segment, as Starlette's default str
    # convertor does; no route here uses another convertor.
    pattern = re.sub(r"\\\{[a-z_]+\\\}", "[^/]+", re.escape(template))
    return re.compile(pattern + r"\Z")


# Literal templates first, then by fewer parameters, so `/goldstandard/diagnostics`
# wins over a parameter in the same place, as it does in the app.
_TEMPLATES = sorted({path for _, path in ACCESS}, key=lambda t: (t.count("{"), -len(t)))
_COMPILED = [(template, _compile(template)) for template in _TEMPLATES]


def match(method: str, path: str) -> str | None:
    """The template of the policy route this method and path reach, or None."""
    method = "GET" if method == "HEAD" else method
    for template, pattern in _COMPILED:
        if (method, template) in ACCESS and pattern.match(path):
            return template
    return None
