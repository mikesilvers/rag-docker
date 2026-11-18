"""Per-collection access (#606): who may use which collection.

A collection is open, or restricted to the accounts granted it (people or
service principals; a key acts as its principal). Engineers reach every
collection. The state is in the store (store.py, schema 4); this module holds
the rule and the check the routes call.

A refused caller gets the answer the route gives a collection, session, job or
file that doesn't exist, so a restricted collection's name isn't confirmed. The
refusal is audited (services/audit.py, `access_refused`).

A staging or recovery collection of a rebuild or import
(`<target>__tuning_<token>`, `<target>__importing_<token>`) holds its target's
data, so it is given its target's access when it is made
(collection_recovery.begin) and keeps that state under its own name. While its
target has a state too, a caller other than an Engineer may use the copy only
when they may use both (`scope`, #788): restricting the target later reaches
its existing copies, including ones kept from before #606 with no state of
their own. A deleted target's state is dropped with it, so the copy is then
governed by its own state alone; a re-created target's new state narrows it
again. `target_of` also maps a package of a copy that no longer exists to its
target (services/importer.py, routers/transfer.py).
"""
from __future__ import annotations

import asyncio
import re

from . import store

_STAGING = re.compile(r"(.+)__(?:tuning|importing)_[0-9a-f]{32}")


def canonical(name: str) -> str:
    return name[:1].upper() + name[1:]


def target_of(name: str) -> str:
    """The target of a staging or recovery copy's name; any other name, canonical."""
    name = canonical(name)
    found = _STAGING.fullmatch(name)
    return found.group(1) if found else name


def package_source(name: str, exists) -> str:
    """Whose access a package of collection `name` follows (#606): the name
    itself, unless it is a staging or recovery copy's that no longer exists here
    (`exists(name)` is False), whose target's it is then, so a package of a
    discarded copy isn't open to everyone."""
    name = canonical(name)
    target = target_of(name)
    if target != name and not exists(name):
        return target
    return name


def scope(name: str) -> tuple[str, ...]:
    """The names whose state decides who may use collection `name`: the name
    itself, and for a staging or recovery copy also its target (#788)."""
    name = canonical(name)
    target = target_of(name)
    return (name,) if target == name else (name, target)


def allowed(principal, name: str) -> bool:
    """True when the principal may use the collection (every name in `scope`).
    No principal means a handler called directly (tests), which is not
    checked, as job owners aren't."""
    if principal is None:
        return True
    return all(store.has_access(principal.user_id, principal.role, n) for n in scope(name))


def principal_of(request):
    return getattr(getattr(request, "state", None), "auth", None) if request is not None else None


async def check(request, name: str, code: str = "COLLECTION_NOT_FOUND") -> bool:
    """Whether the request's caller may use the collection. A refusal is
    audited here, with `code`, the not-found code the caller then answers with."""
    if await asyncio.to_thread(allowed, principal_of(request), name):
        return True
    from services import audit
    await asyncio.to_thread(audit.access_refused, request, canonical(name), code)
    return False


def visible_filter(principal):
    """A predicate for listings: True for a collection name the principal may
    see. One read of the store for the whole listing."""
    if principal is None or principal.role == "engineer":
        return lambda name: True
    restricted = store.restricted_collections()
    me = principal.user_id

    def visible(name: str) -> bool:
        for n in scope(name):
            grants = restricted.get(n)
            if grants is not None and me not in grants:
                return False
        return True
    return visible
