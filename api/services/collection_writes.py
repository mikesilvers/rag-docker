"""Serialize collection mutations within one API process.

The registry counts both owners and waiting writers. A guard spans synchronous
worker work; callers must enter it in their executor, never across an async wait.
"""
from contextlib import contextmanager
from functools import wraps
from inspect import signature
import threading

_registry_lock = threading.Lock()
_registry = {}


def canonical(collection):
    """The same first-character alias normalization used by the backend SDK."""
    return collection[:1].upper() + collection[1:]


@contextmanager
def guard(collection):
    collection = canonical(collection)
    with _registry_lock:
        entry = _registry.setdefault(collection, [threading.RLock(), 0])
        entry[1] += 1
    try:
        with entry[0]:
            yield
    finally:
        with _registry_lock:
            entry[1] -= 1
            if not entry[1]:
                del _registry[collection]


def serialized(argument):
    def decorate(function):
        parameters = signature(function)
        @wraps(function)
        def run(*args, **kwargs):
            collection = parameters.bind(*args, **kwargs).arguments[argument]
            with guard(collection):
                return function(*args, **kwargs)
        return run
    return decorate
