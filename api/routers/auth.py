"""Login, logout, the current user, their own password change, account
administration, and service principals and their API keys, for Engineers (#25).

Which role every other route needs is in auth/policy.py, enforced by
auth/middleware.py before a route runs; the /auth/users and
/auth/service-principals routes need an Engineer's session (an API key can't
use them). The first account is created by the operator command, `python -m
auth`, which also recovers lockouts.

Each of these requests that logs in, changes something or is refused is also an
audit record (#29), written by services/audit.py's middleware with what the
handlers note here (a typed username, a new role, a key's id; never a password
or a key). The WARNING log lines are unchanged.

Two-step verification (#607): for an enrolled user a right password starts a
challenge (the `rag_mfa` cookie) instead of a session, and POST
/auth/login/mfa with a TOTP or recovery code completes the login. The user's
own set-up, recovery codes and turn-off are under /auth/mfa; engineers reset a
user's enrollment and switch "required for everyone". Codes and secrets are in
their one answer only (no-store) and never logged or audited.
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth import passwords, store
from config import settings
from services import audit
from models.schemas import (AdminUserList, AdminUserResult, AuthStatus, AuthUser, KeyIssueRequest, LoginRequest,
                            MfaCodeRequest, MfaDisableRequest, MfaPolicyRequest, PasswordChangeRequest,
                            PrincipalCreateRequest, RoleChangeRequest, ServicePrincipalList, ServicePrincipalResult,
                            UserCreateRequest)
from utils import api_error

router = APIRouter(prefix="/auth")
# The API configures no logging, so only WARNING and above reach the container
# log; login lines must reach it (one per attempt, never a password).
_log = logging.getLogger("auth")

LOGIN_FAILED = "The username or password is incorrect."
MFA_FAILED = "The code is incorrect."
MFA_CHALLENGE_INVALID = "Your sign-in expired. Log in again."


def _address(request: Request) -> str:
    # nginx sets X-Real-IP to the connection's address, replacing anything the
    # client sent; every request reaches the API through it.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def _user_body(username: str, role: str, must_change_password: bool) -> dict:
    # query_only (#670) and lan_mode (#26; the Transfer page hides the
    # unencrypted export, #676) in every user answer, so the UI knows them from
    # the login on.
    return {"username": username, "role": role, "must_change_password": must_change_password,
            "query_only": settings.rag_query_only, "lan_mode": settings.rag_lan_mode}


def _mfa_body(enrolled: bool, required: bool) -> dict:
    """The MFA fields of a user's answers (#607); never a secret or code."""
    return {"mfa_enrolled": enrolled, "mfa_required": required,
            "mfa_enrollment_required": required and not enrolled}


def _cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(store.COOKIE, token, max_age=store.ABSOLUTE_S, path="/", httponly=True,
                        samesite="strict", secure=settings.auth_cookie_secure)


def _challenge_cookie(response: JSONResponse, token: str | None) -> None:
    """Set the login challenge's cookie, or expire it with None."""
    if token is None:
        response.delete_cookie(store.MFA_COOKIE, path="/", httponly=True, samesite="strict",
                               secure=settings.auth_cookie_secure)
    else:
        response.set_cookie(store.MFA_COOKIE, token, max_age=store.MFA_CHALLENGE_S, path="/", httponly=True,
                            samesite="strict", secure=settings.auth_cookie_secure)


def _session_answer(user: store.User, token: str) -> JSONResponse:
    """A login's answer: the user, their MFA state and the session cookie."""
    required = store.mfa_required()
    response = JSONResponse({**_user_body(user.username, user.role, user.must_change_password),
                             **_mfa_body(user.mfa_enrolled, required)})
    _cookie(response, token)
    return response


@router.get("/status", response_model=AuthStatus)
async def auth_status():
    return {"setup_required": not await asyncio.to_thread(store.is_set_up)}


def _login(typed: str, password: str, address: str):
    """('throttled' | 'failed' | 'ok' | 'mfa', user, token). Blocking: scrypt and SQLite.
    'mfa': the password was right for an enrolled user; the token is a
    challenge's, not a session's (#607)."""
    key = typed.lower()
    # The attempt is counted before the password is checked, in the same
    # transaction as the lockout check, so parallel requests can't all slip in.
    if not store.reserve_attempt(key, address):
        return "throttled", None, None
    username = passwords.normalize_username(typed)
    user = store.get_user(username) if username else None
    # Verified against a dummy hash when there is no such user, or the name is a
    # service principal's (it can't log in, Q21), so a failure takes as long
    # either way and the timing doesn't reveal which accounts exist.
    person = user if user and user.kind == "user" else None
    matched = passwords.verify_password(password, person.password_hash if person else passwords.dummy_hash())
    ok = bool(matched and person and person.enabled)
    if ok and user.mfa_enrolled:
        # Not a failure, but the pair's earlier failures stay counted until the
        # second factor passes (#607).
        store.release_attempt(key, address)
        return "mfa", user, store.create_challenge(user.id, key)
    store.finish_attempt(key, address, ok)
    if not ok:
        return "failed", None, None
    return "ok", user, store.create_session(user.id)


@router.post("/login", response_model=AuthUser)
async def login(body: LoginRequest, request: Request):
    if not await asyncio.to_thread(store.is_set_up):
        return api_error(503, "SETUP_REQUIRED",
                         "No account exists yet. An operator must create the first Engineer with "
                         "`docker compose exec api python -m auth bootstrap <username>`.")
    address = _address(request)
    outcome, user, token = await asyncio.to_thread(_login, body.username, body.password, address)
    # The audit record (#29): a success names the account, a failure the
    # username as typed; never the password.
    if outcome in ("ok", "mfa"):
        audit.identify(user.username, user.role)
    else:
        audit.note(target=body.username)
    if outcome == "throttled":
        _log.warning("Login refused for %r from %s: too many failed attempts", body.username, address)
        return api_error(429, "LOGIN_THROTTLED", "Too many failed logins. Wait 15 minutes and try again.")
    if outcome == "failed":
        _log.warning("Login failed for %r from %s", body.username, address)
        return api_error(401, "LOGIN_FAILED", LOGIN_FAILED)
    if outcome == "mfa":
        audit.note(mfa="required")
        _log.warning("Login for %r (%s) from %s: password accepted, code required", user.username, user.role, address)
        response = JSONResponse({"mfa_required": True})
        _challenge_cookie(response, token)
        return response
    _log.warning("Login succeeded for %r (%s) from %s", user.username, user.role, address)
    return await asyncio.to_thread(_session_answer, user, token)


def _second_factor(token: str | None, code: str, address: str):
    """('invalid' | 'throttled' | 'failed' | 'ok', user, method, session token).
    The attempt counts against the login throttle of the challenge's username
    and this address before the code is checked, as a password is."""
    found = store.challenge(token)
    if found is None:
        return "invalid", None, None, None
    username, key = found
    if not store.reserve_attempt(key, address):
        return "throttled", store.get_user(username), store.factor_method(code), None
    outcome, user, method = store.answer_challenge(token, code)
    store.finish_attempt(key, address, outcome == "ok")
    if outcome == "invalid":
        return "invalid", None, None, None
    if outcome == "failed":
        return "failed", user, method, None
    return "ok", user, method, store.create_session(user.id)


@router.post("/login/mfa", response_model=AuthUser, response_model_exclude_none=True)
async def login_mfa(body: MfaCodeRequest, request: Request):
    if not await asyncio.to_thread(store.is_set_up):
        return api_error(503, "SETUP_REQUIRED",
                         "No account exists yet. An operator must create the first Engineer with "
                         "`docker compose exec api python -m auth bootstrap <username>`.")
    address = _address(request)
    token = request.cookies.get(store.MFA_COOKIE)
    outcome, user, method, session = await asyncio.to_thread(_second_factor, token, body.code, address)
    # Every attempt that reaches a live challenge is its own audit record, with
    # its user as the actor (#607); the code never is.
    if user is not None:
        audit.identify(user.username, user.role)
        audit.note(method=method)
    if outcome == "invalid":
        return api_error(401, "MFA_CHALLENGE_INVALID", MFA_CHALLENGE_INVALID)
    if outcome == "throttled":
        _log.warning("Second factor refused for %r from %s: too many failed attempts", user.username if user else None,
                     address)
        return api_error(429, "LOGIN_THROTTLED", "Too many failed attempts. Wait 15 minutes and try again.")
    if outcome == "failed":
        _log.warning("Second factor failed for %r from %s (%s)", user.username, address, method)
        return api_error(401, "MFA_FAILED", MFA_FAILED)
    _log.warning("Login succeeded for %r (%s) from %s with a %s code", user.username, user.role, address, method)
    response = await asyncio.to_thread(_session_answer, user, session)
    _challenge_cookie(response, None)
    return response


@router.post("/logout")
async def logout(request: Request):
    principal = request.state.auth
    await asyncio.to_thread(store.delete_session, principal.token_hash)
    _log.warning("Logout of %r", principal.username)
    response = JSONResponse({"status": "logged_out"})
    response.delete_cookie(store.COOKIE, path="/", httponly=True, samesite="strict",
                           secure=settings.auth_cookie_secure)
    return response


@router.get("/me", response_model=AuthUser)
async def me(request: Request):
    # A session's user (kind "user") or an API key's service principal ("service").
    principal = request.state.auth
    required = principal.kind == "user" and await asyncio.to_thread(store.mfa_required)
    return {**_user_body(principal.username, principal.role, principal.must_change_password),
            "kind": principal.kind, **_mfa_body(principal.mfa_enrolled, required)}


def _change(principal, current: str, new: str, address: str):
    # A wrong current password counts against the same throttle as a failed
    # login, so a session left open can't be used to guess the password.
    if not store.reserve_attempt(principal.username, address):
        return "throttled"
    user = store.get_user(principal.username)
    right = user is not None and passwords.verify_password(current, user.password_hash)
    store.finish_attempt(principal.username, address, right)
    if not right:
        return "incorrect"
    problem = passwords.password_problem(new, principal.username)
    if problem:
        return problem
    # A generated password must really be replaced (N2), and so must any other.
    if new == current:
        return "The new password must differ from the current one."
    store.change_password(principal, new)
    return None


@router.post("/password")
async def change_password(body: PasswordChangeRequest, request: Request):
    principal = request.state.auth
    address = _address(request)
    problem = await asyncio.to_thread(_change, principal, body.current_password, body.new_password, address)
    if problem == "throttled":
        _log.warning("Password change refused for %r from %s: too many failed attempts", principal.username, address)
        return api_error(429, "LOGIN_THROTTLED", "Too many failed attempts. Wait 15 minutes and try again.")
    if problem == "incorrect":
        _log.warning("Password change failed for %r from %s: wrong current password", principal.username, address)
        return api_error(403, "PASSWORD_INCORRECT", "The current password is incorrect.")
    if problem:
        return api_error(422, "INVALID_PASSWORD", problem)
    _log.warning("Password changed by %r; their other sessions ended", principal.username)
    return {"status": "changed", **_user_body(principal.username, principal.role, False)}


# ── account administration (Engineers only, in auth/policy.py) ───────────────
# Each change and each refusal is one WARNING line naming the Engineer, the
# action and the account (Q30); a generated password is in its answer only.

_REFUSED_STATUS = {"USER_NOT_FOUND": 404, "USER_EXISTS": 409, "LAST_ENGINEER": 409, "MFA_ENROLL_FIRST": 409}
_NO_STORE = {"Cache-Control": "no-store"}


def _admin_user(user: store.User) -> dict:
    return {"username": user.username, "role": user.role, "enabled": user.enabled,
            "must_change_password": user.must_change_password,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(user.created_at)),
            "mfa_enrolled": user.mfa_enrolled}


def _target(username: str) -> str | None:
    """The stored form of a username in a path, or None when no account can have it."""
    return passwords.normalize_username(username)


# A refused name is logged and audited cut to this many characters: the
# request models don't cap its length, so the route can answer INVALID_USERNAME (#551).
_SHOWN_MAX = 256


def _shown(name: str) -> str:
    return name[:_SHOWN_MAX]


def _refusal(actor: str, action: str, target: str, code: str, message: str):
    _log.warning("Admin %r: %s of %r refused: %s", actor, action, target, message)
    return api_error(_REFUSED_STATUS.get(code, 409), code, message)


def _not_found(actor: str, action: str, typed: str):
    return _refusal(actor, action, typed, "USER_NOT_FOUND", f"There is no user {typed!r}.")


def _refused(actor: str, action: str, target: str, exc: store.Refused):
    message = str(exc)
    return _refusal(actor, action, target, exc.code, message[:1].upper() + message[1:] + ".")


@router.get("/users", response_model=AdminUserList)
async def list_users():
    users = await asyncio.to_thread(store.list_users)
    return {"users": [_admin_user(user) for user in users]}


def _create(username: str, role: str):
    password = passwords.new_password(username)
    store.create_user(username, role, password)
    return store.get_user(username), password


@router.post("/users", status_code=201, response_model=AdminUserResult)
async def create_user(body: UserCreateRequest, request: Request):
    actor = request.state.auth.username
    username = passwords.normalize_username(body.username)
    audit.note(target=username or _shown(body.username), role=body.role)
    if username is None:
        _log.warning("Admin %r: create of %r refused: invalid username", actor, _shown(body.username))
        return api_error(422, "INVALID_USERNAME", f"The username must be {passwords.USERNAME_RULE}.")
    try:
        user, password = await asyncio.to_thread(_create, username, body.role)
    except store.Refused as exc:
        return _refused(actor, "create", username, exc)
    _log.warning("Admin %r created %s %r; a generated password is due for change", actor, user.role, user.username)
    return JSONResponse({"user": _admin_user(user), "password": password}, status_code=201, headers=_NO_STORE)


def _reset(username: str):
    password = passwords.new_password(username)
    store.reset_password(username, password, "user")
    return store.get_user(username), password


@router.post("/users/{username}/reset-password", response_model=AdminUserResult)
async def reset_user_password(username: str, request: Request):
    actor = request.state.auth.username
    target = _target(username)
    if target is None:
        return _not_found(actor, "reset-password", username)
    if target == actor:
        # R3: an Engineer's own password is changed with the current one (Q19);
        # the operator command can still reset it, for recovery.
        _log.warning("Admin %r: reset-password of their own account refused", actor)
        return api_error(409, "SELF_RESET_REFUSED",
                         "You can't reset your own password here. Use Change password, which needs your "
                         "current password.")
    try:
        user, password = await asyncio.to_thread(_reset, target)
    except store.Refused as exc:
        return _refused(actor, "reset-password", target, exc)
    _log.warning("Admin %r reset the password of %r; their sessions ended", actor, target)
    return JSONResponse({"user": _admin_user(user), "password": password}, headers=_NO_STORE)


def _apply(action, target: str, *args):
    action(target, *args)
    return store.get_user(target)


@router.post("/users/{username}/role", response_model=AdminUserResult, response_model_exclude_none=True)
async def set_user_role(username: str, body: RoleChangeRequest, request: Request):
    actor = request.state.auth.username
    target = _target(username)
    if target is None:
        return _not_found(actor, "set-role", username)
    try:
        audit.note(role=body.role)
        user = await asyncio.to_thread(_apply, store.set_role, target, body.role, "user")
    except store.Refused as exc:
        return _refused(actor, "set-role", target, exc)
    _log.warning("Admin %r set the role of %r to %s", actor, target, body.role)
    return {"user": _admin_user(user)}


async def _set_enabled(username: str, request: Request, enabled: bool):
    actor = request.state.auth.username
    action = "enable" if enabled else "disable"
    target = _target(username)
    if target is None:
        return _not_found(actor, action, username)
    try:
        user = await asyncio.to_thread(_apply, store.set_enabled, target, enabled, "user")
    except store.Refused as exc:
        return _refused(actor, action, target, exc)
    _log.warning("Admin %r %sd %r%s", actor, action, target, "" if enabled else "; their sessions ended")
    return {"user": _admin_user(user)}


@router.post("/users/{username}/disable", response_model=AdminUserResult, response_model_exclude_none=True)
async def disable_user(username: str, request: Request):
    return await _set_enabled(username, request, False)


@router.post("/users/{username}/enable", response_model=AdminUserResult, response_model_exclude_none=True)
async def enable_user(username: str, request: Request):
    return await _set_enabled(username, request, True)


@router.post("/users/{username}/reset-mfa", response_model=AdminUserResult, response_model_exclude_none=True)
async def reset_user_mfa(username: str, request: Request):
    """Reset a user's two-step verification (#607): their secret and recovery
    codes are deleted and every session of theirs ends."""
    actor = request.state.auth.username
    target = _target(username)
    if target is None:
        return _not_found(actor, "reset-mfa", username)
    if target == actor:
        _log.warning("Admin %r: reset-mfa of their own account refused", actor)
        return api_error(409, "SELF_MFA_RESET_REFUSED",
                         "You can't reset your own two-step verification here. Another engineer can, or the "
                         "operator command `python -m auth reset-mfa`.")
    try:
        user = await asyncio.to_thread(_apply, store.reset_mfa, target, "user")
    except store.Refused as exc:
        return _refused(actor, "reset-mfa", target, exc)
    _log.warning("Admin %r reset the two-step verification of %r; their sessions ended", actor, target)
    return {"user": _admin_user(user)}


@router.post("/mfa/policy")
async def set_mfa_policy(body: MfaPolicyRequest, request: Request):
    """Require two-step verification for everyone, or stop requiring it (#607)."""
    principal = request.state.auth
    audit.note(required=body.required)
    try:
        await asyncio.to_thread(store.set_mfa_required, principal.user_id, body.required)
    except store.Refused as exc:
        return _refusal(principal.username, "require-mfa", principal.username, exc.code,
                        "Set up two-step verification for your own account first, on the Account page.")
    _log.warning("Admin %r %s two-step verification for everyone", principal.username,
                 "required" if body.required else "stopped requiring")
    return {"required": body.required}


# ── the user's own two-step verification (#607) ──────────────────────────────

def _iso_or_none(at: float | None) -> str | None:
    return None if at is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def _status_body(user_id: int) -> dict:
    status = store.mfa_status(user_id)
    return {**status, "enrolled_at": _iso_or_none(status["enrolled_at"])}


_MFA_STATUS = {"MFA_ALREADY_ENROLLED": 409, "MFA_ENROLLMENT_NOT_STARTED": 409, "MFA_CODE_INCORRECT": 422,
               "MFA_NOT_ENROLLED": 409, "MFA_REQUIRED": 409}


def _mfa_refused(principal, action: str, exc: store.Refused):
    message = str(exc)
    _log.warning("Two-step verification %s for %r refused: %s", action, principal.username, message)
    return api_error(_MFA_STATUS.get(exc.code, 409), exc.code, message[:1].upper() + message[1:] + ".")


@router.get("/mfa")
async def mfa_status(request: Request):
    return await asyncio.to_thread(_status_body, request.state.auth.user_id)


@router.post("/mfa/enroll")
async def mfa_enroll(request: Request):
    principal = request.state.auth
    try:
        secret, uri = await asyncio.to_thread(store.start_enrollment, principal.user_id, principal.username)
    except store.Refused as exc:
        return _mfa_refused(principal, "set-up", exc)
    return JSONResponse({"secret": secret, "otpauth_uri": uri}, headers=_NO_STORE)


def _confirm(principal, code: str):
    codes = store.confirm_enrollment(principal, code)
    return codes, _status_body(principal.user_id)


@router.post("/mfa/enroll/confirm")
async def mfa_enroll_confirm(body: MfaCodeRequest, request: Request):
    principal = request.state.auth
    try:
        codes, status = await asyncio.to_thread(_confirm, principal, body.code)
    except store.Refused as exc:
        return _mfa_refused(principal, "confirmation", exc)
    _log.warning("Two-step verification set up by %r; their other sessions ended", principal.username)
    return JSONResponse({"recovery_codes": codes, "status": status}, headers=_NO_STORE)


def _regenerate(principal, code: str, address: str):
    # A wrong code counts against the login throttle, like a wrong current
    # password: an open session can't be used to guess codes.
    if not store.reserve_attempt(principal.username, address):
        return "throttled", None
    try:
        codes = store.regenerate_codes(principal, code)
    except store.Refused:
        store.release_attempt(principal.username, address)   # refused by rule, not a guess
        raise
    store.finish_attempt(principal.username, address, codes is not None)
    if codes is None:
        return "incorrect", None
    return "ok", (codes, _status_body(principal.user_id))


@router.post("/mfa/recovery-codes")
async def mfa_recovery_codes(body: MfaCodeRequest, request: Request):
    principal = request.state.auth
    address = _address(request)
    try:
        outcome, result = await asyncio.to_thread(_regenerate, principal, body.code, address)
    except store.Refused as exc:
        return _mfa_refused(principal, "recovery-code regeneration", exc)
    if outcome == "throttled":
        _log.warning("Recovery codes refused for %r from %s: too many failed attempts", principal.username, address)
        return api_error(429, "LOGIN_THROTTLED", "Too many failed attempts. Wait 15 minutes and try again.")
    if outcome == "incorrect":
        _log.warning("Recovery codes refused for %r from %s: wrong code", principal.username, address)
        return api_error(403, "MFA_CODE_INCORRECT", MFA_FAILED)
    codes, status = result
    _log.warning("Recovery codes regenerated by %r", principal.username)
    return JSONResponse({"recovery_codes": codes, "status": status}, headers=_NO_STORE)


def _disable(principal, password: str, code: str, address: str):
    if not store.reserve_attempt(principal.username, address):
        return "throttled"
    try:
        ok = store.disable_mfa(principal, password, code)
    except store.Refused:
        store.release_attempt(principal.username, address)   # refused by rule, not a guess
        raise
    store.finish_attempt(principal.username, address, ok)
    return "ok" if ok else "incorrect"


@router.post("/mfa/disable")
async def mfa_disable(body: MfaDisableRequest, request: Request):
    principal = request.state.auth
    address = _address(request)
    audit.note(method=store.factor_method(body.code))
    try:
        outcome = await asyncio.to_thread(_disable, principal, body.password, body.code, address)
    except store.Refused as exc:
        return _mfa_refused(principal, "turn-off", exc)
    if outcome == "throttled":
        _log.warning("Two-step turn-off refused for %r from %s: too many failed attempts", principal.username, address)
        return api_error(429, "LOGIN_THROTTLED", "Too many failed attempts. Wait 15 minutes and try again.")
    if outcome == "incorrect":
        _log.warning("Two-step turn-off refused for %r from %s: wrong password or code", principal.username, address)
        return api_error(403, "MFA_DISABLE_INCORRECT", "The password or code is incorrect.")
    _log.warning("Two-step verification turned off by %r", principal.username)
    return {"status": await asyncio.to_thread(_status_body, principal.user_id)}


# ── service principals and their API keys (Engineers only; PR 3) ─────────────
# A principal is an account that can't log in and authenticates only with its
# keys (auth/keys.py). Each change and each refusal is one WARNING line naming
# the Engineer, the principal and the key id; a key is in its 201 answer only.

_PRINCIPAL_STATUS = {"PRINCIPAL_NOT_FOUND": 404, "PRINCIPAL_EXISTS": 409, "KEY_NOT_FOUND": 404}


def _iso(at: float | None) -> str | None:
    return None if at is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(at))


def _key_body(key: store.Key, now: float) -> dict:
    return {"id": key.id, "label": key.label, "created_at": _iso(key.created_at), "expires_at": _iso(key.expires_at),
            "revoked_at": _iso(key.revoked_at), "last_used_at": _iso(key.last_used), "status": key.status(now)}


def _principal_body(principal: store.ServicePrincipal) -> dict:
    now = time.time()
    return {"name": principal.name, "role": principal.role, "enabled": principal.enabled,
            "created_at": _iso(principal.created_at), "keys": [_key_body(k, now) for k in principal.keys]}


def _principal_refusal(actor: str, action: str, target: str, code: str, message: str):
    _log.warning("Admin %r: %s of service principal %r refused: %s", actor, action, target, message)
    return api_error(_PRINCIPAL_STATUS.get(code, 409), code, message)


def _principal_refused(actor: str, action: str, target: str, exc: store.Refused):
    message = str(exc)
    return _principal_refusal(actor, action, target, exc.code, message[:1].upper() + message[1:] + ".")


def _principal_not_found(actor: str, action: str, typed: str):
    return _principal_refusal(actor, action, typed, "PRINCIPAL_NOT_FOUND", f"There is no service principal {typed!r}.")


@router.get("/service-principals", response_model=ServicePrincipalList)
async def list_principals():
    principals = await asyncio.to_thread(store.list_principals)
    return {"principals": [_principal_body(p) for p in principals]}


@router.post("/service-principals", status_code=201, response_model=ServicePrincipalResult)
async def create_principal(body: PrincipalCreateRequest, request: Request):
    actor = request.state.auth.username
    name = passwords.normalize_username(body.name)
    audit.note(target=name or _shown(body.name), role=body.role)
    if name is None:
        _log.warning("Admin %r: create of service principal %r refused: invalid name", actor, _shown(body.name))
        return api_error(422, "INVALID_USERNAME", f"The name must be {passwords.USERNAME_RULE}.")
    try:
        principal = await asyncio.to_thread(store.create_principal, name, body.role)
    except store.Refused as exc:
        return _principal_refused(actor, "create", name, exc)
    _log.warning("Admin %r created service principal %r (%s)", actor, name, body.role)
    return JSONResponse({"principal": _principal_body(principal)}, status_code=201)


def _change_principal(action, name: str, *args):
    action(name, *args, "service")
    return store.get_principal(name)


@router.post("/service-principals/{name}/role", response_model=ServicePrincipalResult)
async def set_principal_role(name: str, body: RoleChangeRequest, request: Request):
    actor = request.state.auth.username
    target = _target(name)
    if target is None:
        return _principal_not_found(actor, "set-role", name)
    try:
        audit.note(role=body.role)
        principal = await asyncio.to_thread(_change_principal, store.set_role, target, body.role)
    except store.Refused as exc:
        return _principal_refused(actor, "set-role", target, exc)
    _log.warning("Admin %r set the role of service principal %r to %s", actor, target, body.role)
    return JSONResponse({"principal": _principal_body(principal)})


async def _set_principal_enabled(name: str, request: Request, enabled: bool):
    actor = request.state.auth.username
    action = "enable" if enabled else "disable"
    target = _target(name)
    if target is None:
        return _principal_not_found(actor, action, name)
    try:
        principal = await asyncio.to_thread(_change_principal, store.set_enabled, target, enabled)
    except store.Refused as exc:
        return _principal_refused(actor, action, target, exc)
    _log.warning("Admin %r %sd service principal %r%s", actor, action, target,
                 "" if enabled else "; its keys stop working")
    return JSONResponse({"principal": _principal_body(principal)})


@router.post("/service-principals/{name}/disable", response_model=ServicePrincipalResult)
async def disable_principal(name: str, request: Request):
    return await _set_principal_enabled(name, request, False)


@router.post("/service-principals/{name}/enable", response_model=ServicePrincipalResult)
async def enable_principal(name: str, request: Request):
    return await _set_principal_enabled(name, request, True)


def _issue(name: str, days: int, label: str):
    key, secret = store.issue_key(name, days, label)
    return store.get_principal(name), key, secret


@router.post("/service-principals/{name}/keys", status_code=201, response_model=ServicePrincipalResult)
async def issue_key(name: str, body: KeyIssueRequest, request: Request):
    actor = request.state.auth.username
    target = _target(name)
    if target is None:
        return _principal_not_found(actor, "issue-key", name)
    try:
        principal, key, secret = await asyncio.to_thread(_issue, target, body.days, body.label)
    except store.Refused as exc:
        return _principal_refused(actor, "issue-key", target, exc)
    audit.note(key_id=key.id, expires_at=_iso(key.expires_at))   # the id, never the key (#29)
    _log.warning("Admin %r issued key %s for service principal %r, expiring %s", actor, key.id, target,
                 _iso(key.expires_at))
    return JSONResponse({"principal": _principal_body(principal), "key": _key_body(key, time.time()),
                         "secret": secret}, status_code=201, headers=_NO_STORE)


def _revoke(name: str, key_id: str):
    store.revoke_key(name, key_id)
    return store.get_principal(name)


@router.post("/service-principals/{name}/keys/{key_id}/revoke", response_model=ServicePrincipalResult)
async def revoke_key(name: str, key_id: str, request: Request):
    actor = request.state.auth.username
    target = _target(name)
    if target is None:
        return _principal_not_found(actor, "revoke-key", name)
    try:
        principal = await asyncio.to_thread(_revoke, target, key_id)
    except store.Refused as exc:
        return _principal_refused(actor, "revoke-key", target, exc)
    _log.warning("Admin %r revoked key %s of service principal %r", actor, key_id, target)
    return JSONResponse({"principal": _principal_body(principal)})
