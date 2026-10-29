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
                            PasswordChangeRequest, PrincipalCreateRequest, RoleChangeRequest,
                            ServicePrincipalList, ServicePrincipalResult, UserCreateRequest)
from utils import api_error

router = APIRouter(prefix="/auth")
# The API configures no logging, so only WARNING and above reach the container
# log; login lines must reach it (one per attempt, never a password).
_log = logging.getLogger("auth")

LOGIN_FAILED = "The username or password is incorrect."


def _address(request: Request) -> str:
    # nginx sets X-Real-IP to the connection's address, replacing anything the
    # client sent; every request reaches the API through it.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "unknown")


def _user_body(username: str, role: str, must_change_password: bool) -> dict:
    return {"username": username, "role": role, "must_change_password": must_change_password}


def _cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(store.COOKIE, token, max_age=store.ABSOLUTE_S, path="/", httponly=True,
                        samesite="strict", secure=settings.auth_cookie_secure)


@router.get("/status", response_model=AuthStatus)
async def auth_status():
    return {"setup_required": not await asyncio.to_thread(store.is_set_up)}


def _login(typed: str, password: str, address: str):
    """('throttled' | 'failed' | 'ok', user, token). Blocking: scrypt and SQLite."""
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
    if outcome == "ok":
        audit.identify(user.username, user.role)
    else:
        audit.note(target=body.username)
    if outcome == "throttled":
        _log.warning("Login refused for %r from %s: too many failed attempts", body.username, address)
        return api_error(429, "LOGIN_THROTTLED", "Too many failed logins. Wait 15 minutes and try again.")
    if outcome == "failed":
        _log.warning("Login failed for %r from %s", body.username, address)
        return api_error(401, "LOGIN_FAILED", LOGIN_FAILED)
    _log.warning("Login succeeded for %r (%s) from %s", user.username, user.role, address)
    response = JSONResponse(_user_body(user.username, user.role, user.must_change_password))
    _cookie(response, token)
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
    return {**_user_body(principal.username, principal.role, principal.must_change_password),
            "kind": principal.kind}


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

_REFUSED_STATUS = {"USER_NOT_FOUND": 404, "USER_EXISTS": 409, "LAST_ENGINEER": 409}
_NO_STORE = {"Cache-Control": "no-store"}


def _admin_user(user: store.User) -> dict:
    return {"username": user.username, "role": user.role, "enabled": user.enabled,
            "must_change_password": user.must_change_password,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(user.created_at))}


def _target(username: str) -> str | None:
    """The stored form of a username in a path, or None when no account can have it."""
    return passwords.normalize_username(username)


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
    audit.note(target=username or body.username, role=body.role)
    if username is None:
        _log.warning("Admin %r: create of %r refused: invalid username", actor, body.username)
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
    audit.note(target=name or body.name, role=body.role)
    if name is None:
        _log.warning("Admin %r: create of service principal %r refused: invalid name", actor, body.name)
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
