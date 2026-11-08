"""Names of the parts a file over the per-file limits is stored as (#621).

A file whose text or chunks go over the per-file limits is stored as parts named
`<name> (part N)`. The original is retained once, under `<name>`, and every part
maps back to it. A name and its parts are its family: uploading the name again
replaces all of them.
"""
from __future__ import annotations

import re
from typing import Mapping

_PART = re.compile(r" \(part ([1-9][0-9]*)\)\Z")


def part_name(name: str, number: int) -> str:
    """The stored name of part `number` (from 1) of `name`."""
    return f"{name} (part {number})"


def base_of(stored: str) -> str | None:
    """The name a part belongs to, by one ` (part N)` suffix; None for any other name."""
    match = _PART.search(stored)
    return stored[:match.start()] if match else None


def in_family(stored: str, name: str) -> bool:
    """Whether `stored` is `name` itself or one of its parts, exactly."""
    return stored == name or base_of(stored) == name


def resolve(stored: str, listed: Mapping[str, object]) -> str | None:
    """The retained name whose original `stored` came from: `stored` itself when it
    is listed, else the name it is a part of when that is listed, else None."""
    if stored in listed:
        return stored
    base = base_of(stored)
    return base if base is not None and base in listed else None
