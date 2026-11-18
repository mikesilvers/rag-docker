"""The operator command that loads an export package into a query-only instance (#670).

    docker compose exec api python -m loader PACKAGE [--on-conflict abort|rename|replace]

PACKAGE is the name of a package file in ./exports (the api container's
/app/exports): ragpkg-*.tar.gz, or ragpkg-*.tar.gz.age when it is encrypted.
Copy the package there first (scp, a shared folder, a USB drive). The load runs
the import code of POST /import (services/importer.py) in this process, so
every check of an import applies: the archive, its digests, its sources, and
the embedding model and dimensions (#390), which must be this instance's. A
package that carries models installs them, and the collection's retrieval and
ingest settings and gold standard sessions come with it.

--on-conflict says what to do when the collection already exists: abort (the
default) refuses, rename loads it under a new name, replace loads it over the
existing one (a query or export of that collection can fail while that runs).

It runs only on a query-only instance (RAG_QUERY_ONLY=true), where the API
itself changes no collection; a full instance imports on the Transfer page. One
load runs at a time. A collection it creates starts restricted with no grants,
so only AI Engineers can use it until one opens it or grants it on the
Collections page; a replace keeps the collection's access.

The passphrase of an encrypted package is asked for without echo when standard
input is a terminal, and otherwise read as the first line of standard input
(`docker compose exec -T api python -m loader NAME < file`). It is never an
argument, never printed and never recorded.

Each step is one line on standard error, prefixed "loader: ". Exit status: 0
loaded, 1 refused or failed, 2 usage error. Every run past the arguments writes
one audit record (#29), `operator.load`, with actor `operator`: the package, the
collection, the conflict mode, whether it is encrypted, and the chunks loaded or
the code of the refusal. A record that can't be written is one more line.

Ctrl-C stops a load at once, as a kill would. The half-built collection it may
leave is removed by the next load, or by the API's next start.
"""
from __future__ import annotations

import argparse
import fcntl
import getpass
import os
import signal
import stat
import sys
import threading
import uuid
from pathlib import Path

PROGRESS_SECONDS = 5


class _Refused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _say(line: str) -> None:
    print("loader: " + line, file=sys.stderr, flush=True)


def _audit(package: str, on_conflict: str, encrypted: bool, collection: str | None,
           code: str | None = None, chunks: int | None = None) -> None:
    from services import audit
    details: dict = {"on_conflict": on_conflict, "encrypted": encrypted}
    if chunks is not None:
        details["chunks"] = chunks
    if code:
        details["code"] = code
    if not audit.record("operator.load", result="refused" if code else "success", actor="operator",
                        actor_kind="operator", target=package, collection=collection, details=details):
        _say("the audit record could not be written")


def _passphrase() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Passphrase: ")
    return sys.stdin.readline().rstrip("\r\n")


def _lock(upload_dir: str):
    """The one-load-at-a-time lock, held until the returned file is closed."""
    Path(upload_dir).mkdir(parents=True, exist_ok=True)
    handle = open(os.path.join(upload_dir, "loader.lock"), "a")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise _Refused("LOAD_IN_PROGRESS", "another load is running on this instance; "
                                           "try again when it has finished") from None
    return handle


def _progress(job: dict, done: threading.Event) -> None:
    shown = 0
    while not done.wait(PROGRESS_SECONDS):
        count = job.get("chunks_written") or 0
        if count != shown:
            shown = count
            _say(f"chunks written: {count}")


def _load(package: str, on_conflict: str, passphrase: str | None) -> dict:
    """Run the import code on the package and return its finished job record."""
    from services import importer
    job_id = "load-" + uuid.uuid4().hex[:8]
    # The job record lives in this process only: the import's job store is
    # unbound here (jobstore.py), so nothing is added to the API's job lists.
    importer._jobs[job_id] = {
        "job_id": job_id, "status": "queued", "filename": package, "on_conflict": on_conflict,
        "collection": None, "original_collection": None, "chunks_written": 0, "fidelity": None,
        "renamed": False, "notes": [], "restored_sessions": [], "error": None, "error_code": None,
        "error_detail": None, "encrypted": passphrase is not None,
        "owner": None, "operator": True,        # access: restricted, no grants (#670)
    }
    job = importer._jobs[job_id]
    done = threading.Event()
    ticker = threading.Thread(target=_progress, args=(job, done), daemon=True)
    ticker.start()
    try:
        importer._run(job_id, package, on_conflict, passphrase)
    finally:
        done.set()
    return job


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m loader",
        description="Load an export package from ./exports into this query-only instance.")
    parser.add_argument("package", help="the package's file name in ./exports")
    parser.add_argument("--on-conflict", choices=("abort", "rename", "replace"), default="abort",
                        help="when the collection already exists (default: abort)")
    args = parser.parse_args(argv)

    from services import age_stream, package_retention
    if "/" in args.package or not package_retention.valid_name(args.package):
        parser.error(f"not a package name: {args.package!r} (ragpkg-*.tar.gz or ragpkg-*.tar.gz.age, "
                     "a file in ./exports)")

    # Ctrl-C acts as a kill: the import's clean-up would otherwise retire the
    # marker of a half-built collection that the next load must remove.
    signal.signal(signal.SIGINT, signal.SIG_DFL)

    from config import settings
    from services import importer, packager
    package, on_conflict = args.package, args.on_conflict
    encrypted = age_stream.is_encrypted_name(package)
    _say(f"loading {package} (on conflict: {on_conflict})")
    lock = None
    try:
        if not settings.rag_query_only:
            raise _Refused("NOT_QUERY_ONLY", "this instance isn't query-only (RAG_QUERY_ONLY is false); "
                                             "import the package on the Transfer page instead")
        lock = _lock(settings.upload_dir)
        path = packager.exports_dir() / package
        try:
            regular = stat.S_ISREG(os.lstat(path).st_mode)
        except OSError:
            regular = False
        if not regular:
            raise _Refused("PACKAGE_NOT_FOUND", f"no package named '{package}' in ./exports; copy it there first")
        passphrase = None
        if encrypted:
            passphrase = _passphrase()
            if not passphrase:
                raise _Refused("PASSPHRASE_REQUIRED", f"'{package}' is encrypted; give its passphrase")
        # What an earlier load left when it was killed: the same clean-up the
        # API runs at its start. Under the lock, and nothing else imports here.
        for removed in importer.sweep_interrupted_imports():
            _say(f"removed a collection an interrupted load left half-built: {removed}")
        job = _load(package, on_conflict, passphrase)
    except _Refused as refused:
        _say(f"load refused: {refused.code}: {refused}")
        _audit(package, on_conflict, encrypted, None, code=refused.code)
        return 1
    finally:
        if lock is not None:
            lock.close()

    if job.get("status") == "completed":
        renamed = f" (renamed from {job['original_collection']})" if job.get("renamed") else ""
        _say(f"loaded into collection {job['collection']}{renamed}: {job['chunks_written']} chunks, "
             f"{job.get('fidelity')}")
        for note in job.get("notes") or []:
            _say("note: " + str(note))
        _audit(package, on_conflict, encrypted, job["collection"], chunks=job["chunks_written"])
        return 0
    code = job.get("error_code") or "IMPORT_FAILED"
    _say(f"load failed: {code}: {job.get('error')}")
    _audit(package, on_conflict, encrypted, job.get("collection"), code=code)
    return 1


if __name__ == "__main__":
    sys.exit(main())
