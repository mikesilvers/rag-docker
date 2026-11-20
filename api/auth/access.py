"""Per-collection access (#606): who may use which collection.

A collection is open, or restricted to the accounts granted it (people or
service principals; a key acts as its principal), directly or through a group
granted it (#784: store.has_access and store.restricted_collections answer for
both). Engineers reach every collection. The state is in the store (store.py, schema 4); this module holds
the rule and the check the routes call.

A refused caller gets the answer the route gives a collection, session, job or
file that doesn't exist, so a restricted collection's name isn't confirmed. The
refusal is audited (services/audit.py, `access_refused`).

A staging or recovery collection of a rebuild or import
(`<target>__tuning_<token>`, `<target>__importing_<token>`) holds its target's
data, so it is given its target's access when it is made
(collection_recovery.begin) and keeps that state under its own name. A kept
copy can itself be rebuilt, so a copy's target may be a copy too: its `chain`
is the copy, its target, that one's target and so on, down to the original
collection. A new copy takes the narrowest state across its target's chain
(#792). While any collection in the chain has a state too, a caller other than
an Engineer may use the copy only when they may use every one (`scope`, #788,
#792): restricting the original later reaches its existing copies and their
copies, including ones kept from before #606 with no state of their own. A
deleted collection's state is dropped with it, so it then narrows nothing; a
re-created one's new state narrows its copies again. A package of a copy that
no longer exists follows the nearest collection in its chain that exists here,
or the original (`package_source`; services/importer.py, routers/transfer.py).
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


def chain(name: str) -> tuple[str, ...]:
    """Collection `name` (canonical), then each collection it was copied from:
    its target while it is a staging or recovery copy's name, that one's
    target, and so on, ending at the original (#792). An ordinary name is its
    own chain. Each step is strictly shorter, so the walk ends."""
    names = [canonical(name)]
    while (target := target_of(names[-1])) != names[-1]:
        names.append(target)
    return tuple(names)


def package_source(name: str, exists) -> str:
    """Whose access a package of collection `name` follows (#606): the name
    itself, unless it is a staging or recovery copy's that no longer exists here
    (`exists(name)` is False); then the nearest collection in its `chain` that
    exists here, or the original when none does (#792), so a package of a
    discarded copy isn't open to everyone. `exists` is asked in chain order and
    an ordinary name is never asked about."""
    names = chain(name)
    for i, n in enumerate(names):
        if i == len(names) - 1 or exists(n):
            return n
    return names[-1]


def scope(name: str) -> tuple[str, ...]:
    """The names whose state decides who may use collection `name`: its whole
    `chain`, the name itself and, for a staging or recovery copy, every
    collection it was made from (#788, #792)."""
    return chain(name)


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
