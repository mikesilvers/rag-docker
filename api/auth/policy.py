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
# Any logged-in user's own two-step verification (#607): after a due password
# change, and still open while a required set-up is due (the way out of it).
MFA = "mfa"

END_USER, DEVELOPER, ENGINEER = ROLES

ACCESS: dict[tuple[str, str], str] = {
    # Liveness for the container healthcheck, stack.sh and CI (Q8): only
    # {"status"} since #750; the full report is GET /health/details.
    ("GET", "/health"): PUBLIC,
    ("GET", "/auth/status"): PUBLIC,
    ("POST", "/auth/login"): PUBLIC,
    ("GET", "/auth/me"): SESSION,
    ("POST", "/auth/logout"): SESSION,
    ("POST", "/auth/password"): SESSION,
    # Two-step verification (#607): the code after the password, and the
    # user's own set-up, recovery codes and turn-off.
    ("POST", "/auth/login/mfa"): PUBLIC,
    ("GET", "/auth/mfa"): MFA,
    ("POST", "/auth/mfa/enroll"): MFA,
    ("POST", "/auth/mfa/enroll/confirm"): MFA,
    ("POST", "/auth/mfa/recovery-codes"): MFA,
    ("POST", "/auth/mfa/disable"): MFA,
    # What the Q&A page needs (Q4).
    ("GET", "/collections"): END_USER,
    ("POST", "/query"): END_USER,
    ("GET", "/retrieval/config/{collection}"): END_USER,
    # Import, Chunking, Retrieval, Gold Standard and Transfer (Q2, Q4, Q6).
    ("POST", "/collections"): DEVELOPER,
    ("POST", "/ingest/upload"): DEVELOPER,
    ("GET", "/ingest/job/{job_id}"): DEVELOPER,
    ("GET", "/ingest/jobs"): DEVELOPER,                 # the caller's jobs only (#617)
    ("GET", "/ingest/config/{collection}"): DEVELOPER,
    ("POST", "/ingest/config"): DEVELOPER,
    ("POST", "/retrieval/config"): DEVELOPER,
    ("POST", "/goldstandard/generate"): DEVELOPER,
    ("GET", "/goldstandard/session/{session_id}"): DEVELOPER,
    ("GET", "/goldstandard/sessions"): DEVELOPER,       # the caller's generations only (#617)
    ("PATCH", "/goldstandard/session/{session_id}/pair/{pair_id}"): DEVELOPER,
    ("POST", "/goldstandard/regenerate"): DEVELOPER,
    ("POST", "/goldstandard/save"): DEVELOPER,
    ("GET", "/goldstandard/download/{filename}"): DEVELOPER,
    ("POST", "/export"): DEVELOPER,
    ("GET", "/export/job/{job_id}"): DEVELOPER,
    ("GET", "/export/jobs"): DEVELOPER,                 # the caller's jobs only (#617)
    ("POST", "/import"): DEVELOPER,
    ("GET", "/import/job/{job_id}"): DEVELOPER,
    ("GET", "/import/jobs"): DEVELOPER,                 # the caller's jobs only (#617)
    ("GET", "/packages"): DEVELOPER,
    # Downloading a package; a full download of one exported here deletes it (#677).
    ("GET", "/packages/{filename}"): DEVELOPER,
    ("GET", "/help/transfer"): DEVELOPER,
    ("GET", "/tune/{collection}"): DEVELOPER,          # read by the Transfer page (Q3)
    # The full health report (#750): services, models and memory, keys too.
    ("GET", "/health/details"): DEVELOPER,
    # Document preparation (#638): the ingest permissions (C3).
    ("POST", "/prepare/upload"): DEVELOPER,
    ("GET", "/prepare/documents"): DEVELOPER,
    ("GET", "/prepare/documents/{document_id}"): DEVELOPER,
    ("GET", "/prepare/documents/{document_id}/raw"): DEVELOPER,
    ("GET", "/prepare/documents/{document_id}/prepared"): DEVELOPER,
    ("DELETE", "/prepare/documents/{document_id}"): DEVELOPER,
    ("DELETE", "/prepare/documents/{document_id}/prepared"): DEVELOPER,
    ("POST", "/prepare/jobs"): DEVELOPER,
    ("GET", "/prepare/jobs"): DEVELOPER,
    ("GET", "/prepare/job/{job_id}"): DEVELOPER,
    ("POST", "/prepare/job/{job_id}/cancel"): DEVELOPER,
    ("POST", "/prepare/import"): DEVELOPER,
    # Collections management, tuning, the Health page's data (Q3, Q5, Q6).
    ("DELETE", "/collections/{name}"): ENGINEER,
    # Who may use a collection (#606): Engineers, in a browser session only.
    ("GET", "/collections/{name}/access"): ENGINEER,
    ("PUT", "/collections/{name}/access"): ENGINEER,
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
    ("POST", "/auth/users/{username}/reset-mfa"): ENGINEER,
    ("POST", "/auth/mfa/policy"): ENGINEER,               # require two-step verification for everyone
    # Service principals and their keys (PR 3 of #25). Nothing deletes a principal.
    ("GET", "/auth/service-principals"): ENGINEER,
    ("POST", "/auth/service-principals"): ENGINEER,
    ("POST", "/auth/service-principals/{name}/role"): ENGINEER,
    ("POST", "/auth/service-principals/{name}/disable"): ENGINEER,
    ("POST", "/auth/service-principals/{name}/enable"): ENGINEER,
    ("POST", "/auth/service-principals/{name}/keys"): ENGINEER,
    ("POST", "/auth/service-principals/{name}/keys/{key_id}/revoke"): ENGINEER,
    # Groups for collection access (#784): Engineers, in a browser session only.
    # Nothing under /auth uses DELETE, so a group is deleted with a POST.
    ("GET", "/auth/groups"): ENGINEER,
    ("POST", "/auth/groups"): ENGINEER,
    ("POST", "/auth/groups/{name}/rename"): ENGINEER,
    ("PUT", "/auth/groups/{name}/members"): ENGINEER,
    ("POST", "/auth/groups/{name}/delete"): ENGINEER,
    # The audit log (#29): Engineers, in a browser session only (HUMAN_ONLY).
    ("GET", "/audit"): ENGINEER,
    ("GET", "/audit.csv"): ENGINEER,
    # FastAPI's own API docs (N5).
    ("GET", "/docs"): ENGINEER,
    ("GET", "/docs/oauth2-redirect"): ENGINEER,
    ("GET", "/redoc"): ENGINEER,
    ("GET", "/openapi.json"): ENGINEER,
}


# Routes an API key can't use, whatever its principal's role (S5 of PR 3):
# account, principal and key administration, so a leaked key can't mint further
# keys or accounts that outlive its revocation, and the session's own routes.
# GET /auth/me answers for a key. The audit log too (#29): a leaked key can't
# read who did what, or question text when that is recorded. And two-step
# verification (#607): service keys are unchanged and have no second factor.
# And per-collection access (#606), and groups (#784), so a leaked key can't
# open a collection to another principal.
HUMAN_ONLY = frozenset(path for _, path in ACCESS
                       if path.startswith(("/auth/users", "/auth/service-principals", "/auth/mfa",
                                           "/auth/groups"))) | {
    "/auth/logout", "/auth/password", "/audit", "/audit.csv", "/collections/{name}/access"}


# GET routes that change state (#794): a full download of a package exported
# here deletes it. A cookie-authenticated GET of one sent from another origin
# (`Sec-Fetch-Site` present and neither `same-origin` nor `none`) is refused
# like a cross-origin change (middleware.py); HEAD changes nothing.
STATE_CHANGING_GET = frozenset({"/packages/{filename}"})


# Query-only mode (#670, RAG_QUERY_ONLY): the routes that create, change or
# delete a collection or its data. With the mode on, the auth middleware
# refuses each of them with 403 QUERY_ONLY, after every other check.
QUERY_ONLY_REFUSED = frozenset({
    ("POST", "/collections"),
    ("DELETE", "/collections/{name}"),
    ("POST", "/ingest/upload"),
    ("POST", "/ingest/config"),
    ("POST", "/retrieval/config"),
    ("POST", "/goldstandard/generate"),
    ("POST", "/goldstandard/regenerate"),
    ("PATCH", "/goldstandard/session/{session_id}/pair/{pair_id}"),
    ("POST", "/goldstandard/save"),
    ("POST", "/import"),          # packages are loaded on the host (python -m loader)
    ("POST", "/prepare/upload"),
    ("DELETE", "/prepare/documents/{document_id}"),
    ("DELETE", "/prepare/documents/{document_id}/prepared"),
    ("POST", "/prepare/jobs"),
    ("POST", "/prepare/job/{job_id}/cancel"),
    ("POST", "/prepare/import"),
    ("POST", "/tune/rechunk"),
    ("POST", "/tune/reembed"),
    ("POST", "/tune/reindex"),
})
# Every other route that changes state keeps working in query-only mode: the
# /auth routes, a query, export and the package download (#677), and collection
# access (#606), since the query-only machine has its own accounts. A new
# data-changing route lands here until it is classified; test_query_only.py
# pins this set, so it fails until someone decides.
QUERY_ONLY_KEPT = frozenset(key for key in ACCESS
                            if (key[0] != "GET" or key[1] in STATE_CHANGING_GET)
                            and key not in QUERY_ONLY_REFUSED)


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
