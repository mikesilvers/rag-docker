"""The operator command for accounts (#25).

    docker compose exec api python -m auth bootstrap USERNAME
    docker compose exec api python -m auth create-user USERNAME ROLE
    docker compose exec api python -m auth reset-password USERNAME
    docker compose exec api python -m auth set-role USERNAME ROLE
    docker compose exec api python -m auth disable USERNAME
    docker compose exec api python -m auth enable USERNAME
    docker compose exec api python -m auth reset-mfa USERNAME
    docker compose exec api python -m auth list

ROLE is end_user, developer or engineer. bootstrap, create-user and
reset-password print a generated password, alone, on standard output; it is
shown nowhere else, and the user must change it at their first login. Each
action writes one line to standard error. Exit status: 0 done, 1 refused,
2 usage error.

The last enabled engineer can't be disabled or demoted. An account a user is
locked out of (lost password, disabled) is recovered with create-user, enable
or reset-password; these don't lift a login throttle, which ends by itself
after 15 minutes.

reset-mfa (#607) is the last-resort recovery of two-step verification: it
deletes the user's secret and recovery codes and ends their sessions, so their
next login asks for no code (and, while MFA is required for everyone, for a new
set-up). It works for an engineer's own account too, which the app refuses.

Service principals (API keys, PR 3) are created and given keys by Engineers in
the app; this command issues no key. `list` shows principals too, marked
"service account"; set-role, disable and enable work on them (disabling one
stops its keys, an escape hatch for a leaked key when no Engineer can log in);
reset-password refuses one, since it has no password.

Every action but `list` that reaches the store, done or refused, is also an
audit record (#29) with actor `operator`; the generated password never is. A
record that can't be written is one more line on standard error.
"""
from __future__ import annotations

import argparse
import sys
import time

from . import passwords, store
from .policy import ROLES


def _audit(command: str, username: str, role: str | None, code: str | None = None) -> None:
    from services import audit
    details = {"role": role} if role and command in ("bootstrap", "create-user", "set-role") else {}
    if code:
        details["code"] = code
    if not audit.record("operator." + command, result="refused" if code else "success", actor="operator",
                        actor_kind="operator", target=username, details=details):
        _say(f"{command}: the audit record could not be written")


def _say(line: str) -> None:
    print("auth: " + line, file=sys.stderr, flush=True)


def _username(parser, value: str) -> str:
    name = passwords.normalize_username(value)
    if name is None:
        parser.error(f"invalid username {value!r}: {passwords.USERNAME_RULE}")
    return name


def _role(parser, value: str) -> str:
    if value not in ROLES:
        parser.error(f"invalid role {value!r}: use one of {', '.join(ROLES)}")
    return value


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m auth", description="Manage RAG platform accounts.")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    p = commands.add_parser("bootstrap", help="create the first engineer (only when no account exists)")
    p.add_argument("username")
    p = commands.add_parser("create-user", help="create an account with a generated password")
    p.add_argument("username")
    p.add_argument("role")
    p = commands.add_parser("reset-password", help="set a generated password and end the user's sessions")
    p.add_argument("username")
    p = commands.add_parser("set-role", help="change a user's role")
    p.add_argument("username")
    p.add_argument("role")
    p = commands.add_parser("disable", help="disable an account (a user's sessions end, a service account's keys stop working)")
    p.add_argument("username")
    p = commands.add_parser("enable", help="enable a disabled account")
    p.add_argument("username")
    p = commands.add_parser("reset-mfa", help="remove a user's two-step verification and end their sessions")
    p.add_argument("username")
    commands.add_parser("list", help="list the accounts")
    args = parser.parse_args(argv)

    username = _username(parser, args.username) if hasattr(args, "username") else None
    role = _role(parser, args.role) if hasattr(args, "role") else None
    try:
        if args.command in ("bootstrap", "create-user"):
            password = passwords.new_password(username)
            role = "engineer" if args.command == "bootstrap" else role
            store.create_user(username, role, password, bootstrap=args.command == "bootstrap")
            print(password, flush=True)
            _say(f"{args.command}: created {role} {username!r}; the password above must be changed at first login")
        elif args.command == "reset-password":
            password = passwords.new_password(username)
            store.reset_password(username, password)
            print(password, flush=True)
            _say(f"reset-password: new password for {username!r}, to be changed at next login; "
                 "their sessions ended")
        elif args.command == "set-role":
            store.set_role(username, role)
            _say(f"set-role: {username!r} is now {role}")
        elif args.command == "reset-mfa":
            store.reset_mfa(username)
            _say(f"reset-mfa: two-step verification of {username!r} removed; their sessions ended")
        elif args.command in ("disable", "enable"):
            store.set_enabled(username, args.command == "enable")
            ended = ""
            if args.command == "disable":
                # A service principal has no sessions: disabling it stops its keys (#553).
                service = store.get_user(username).kind == "service"
                ended = "; its keys stop working" if service else "; their sessions ended"
            _say(f"{args.command}: {username!r} {args.command}d{ended}")
        else:
            for user in store.list_users(kind=None):
                created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(user.created_at))
                password = ("service account" if user.kind == "service"
                            else "password change due" if user.must_change_password else "-")
                print(f"{user.username}\t{user.role}\t{'enabled' if user.enabled else 'disabled'}\t"
                      f"{password}\t{created}")
    except store.Refused as exc:
        _say(f"{args.command} refused: {exc}")
        if args.command != "list":
            _audit(args.command, username, role, exc.code)
        return 1
    if args.command != "list":
        _audit(args.command, username, role)
    return 0


if __name__ == "__main__":
    sys.exit(main())
