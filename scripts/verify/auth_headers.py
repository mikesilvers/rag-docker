"""The suite's session for the host-side Python helpers that call the API (#25).

lib.sh logs the test engineer in and exports RAG_AUTH_JAR (a curl cookie jar)
and RAG_AUTH_ORIGIN. headers() returns the Cookie and Origin a same-origin
browser would send. Standard library, Python 3.9+.
"""
import os

COOKIE = "rag_session"


def session_token(jar_path):
    """The session cookie's value in a curl cookie jar, or None."""
    try:
        with open(jar_path) as jar:
            lines = jar.read().splitlines()
    except OSError:
        return None
    for line in lines:
        # curl marks HttpOnly cookies with this prefix on an otherwise
        # ordinary line; other lines starting with # are comments.
        if line.startswith("#HttpOnly_"):
            line = line[len("#HttpOnly_"):]
        elif line.startswith("#") or not line.strip():
            continue
        fields = line.split("\t")
        if len(fields) == 7 and fields[5] == COOKIE:
            return fields[6]
    return None


def headers():
    """{'Cookie', 'Origin'} for the suite's session; empty outside a suite."""
    jar, origin = os.environ.get("RAG_AUTH_JAR"), os.environ.get("RAG_AUTH_ORIGIN")
    token = session_token(jar) if jar else None
    found = {}
    if token:
        found["Cookie"] = "{}={}".format(COOKIE, token)
    if origin:
        found["Origin"] = origin
    return found
