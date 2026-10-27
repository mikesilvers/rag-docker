"""Login, logout, the current user and their own password change (#25).

Which role every other route needs is in auth/policy.py, enforced by
auth/middleware.py before a route runs. Accounts are created and recovered by
the operator command, `python -m auth`.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from auth import passwords, store
from config import settings
from models.schemas import AuthStatus, AuthUser, LoginRequest, PasswordChangeRequest
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
    if store.locked(key, address):
        return "throttled", None, None
    username = passwords.normalize_username(typed)
    user = store.get_user(username) if username else None
    # Verified against a dummy hash when there is no such user, so a failure
    # takes as long either way and the timing doesn't reveal which accounts exist.
    matched = passwords.verify_password(password, user.password_hash if user else passwords.dummy_hash())
    if not (matched and user and user.enabled):
        store.record_failure(key, address)
        return "failed", None, None
    store.clear_failures(key, address)
    return "ok", user, store.create_session(user.id)


@router.post("/login", response_model=AuthUser)
async def login(body: LoginRequest, request: Request):
    if not await asyncio.to_thread(store.is_set_up):
        return api_error(503, "SETUP_REQUIRED",
                         "No account exists yet. An operator must create the first Engineer with "
                         "`docker compose exec api python -m auth bootstrap <username>`.")
    address = _address(request)
    outcome, user, token = await asyncio.to_thread(_login, body.username, body.password, address)
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
    principal = request.state.auth
    return _user_body(principal.username, principal.role, principal.must_change_password)


def _change(principal, current: str, new: str):
    user = store.get_user(principal.username)
    if user is None or not passwords.verify_password(current, user.password_hash):
        return "incorrect"
    problem = passwords.password_problem(new, principal.username)
    if problem:
        return problem
    store.change_password(principal, new)
    return None


@router.post("/password")
async def change_password(body: PasswordChangeRequest, request: Request):
    principal = request.state.auth
    problem = await asyncio.to_thread(_change, principal, body.current_password, body.new_password)
    if problem == "incorrect":
        return api_error(403, "PASSWORD_INCORRECT", "The current password is incorrect.")
    if problem:
        return api_error(422, "INVALID_PASSWORD", problem)
    _log.warning("Password changed by %r; their other sessions ended", principal.username)
    return {"status": "changed", **_user_body(principal.username, principal.role, False)}
