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
existing one. While any load runs, a question or export of its collection can
meet it half-built or fail.

It runs only on a query-only instance (RAG_QUERY_ONLY=true), where the API
itself changes no collection; a full instance imports on the Transfer page. One
load runs at a time. Every collection it creates, a rename included, starts
restricted with no grants, so only AI Engineers can use it until one opens it
or grants it on the Collections page; it never copies another collection's
access. Only a replace keeps the collection's access.

An encrypted package is opened by its key file (a key export, the default since
#676) or its passphrase (#27); its age header says which. The key is asked for
without echo when standard input is a terminal (paste the AGE-SECRET-KEY-1...
line), and otherwise read as the whole of standard input, the key file itself
(`docker compose exec -T api python -m loader NAME < NAME.key`). A passphrase is
asked for the same way, or read as the first line of standard input
(`... < passphrase-file`). Neither is ever an argument, printed or recorded.

Each step is one line on standard error, prefixed "loader: ". Exit status: 0
loaded, 1 refused or failed, 2 usage error. Every run past the arguments writes
one audit record (#29), `operator.load`, with actor `operator` and result
`success`, `refused` (before the load), `failed` (the load failed) or
`interrupted` (Ctrl-C): the package, the collection, the conflict mode, whether
it is encrypted, and the chunks loaded or the code. A record that can't be
written is one more line.

Ctrl-C writes the `interrupted` record and then stops the load at once, as a
kill would. What it leaves: a collection it was building, which the next load
removes (it runs the import's clean-up of an interrupted import first); the
load's work folder (import-* in UPLOAD_DIR) and a replace's scratch staging
collection, which stay until the API's next start (docker compose restart api).
A replace builds the package into a recovery collection before it deletes the
old collection, so one stopped after that delete leaves the collection missing
or partial (the recovery collection holds the package's data, not the old
data): load the package again with --on-conflict replace to put it back.
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


# The run's state, for the Ctrl-C handler: the job once the load has started,
# whether a record is being written (the handler then writes none: the audit
# log's locks are held, and that record is the run's own), and whether the
# run's final record has been started (`done`: a later Ctrl-C writes none).
_state: dict = {}


def _audit(package: str, on_conflict: str, encrypted: bool, collection: str | None,
           result: str = "success", code: str | None = None, chunks: int | None = None) -> None:
    from services import audit
    details: dict = {"on_conflict": on_conflict, "encrypted": encrypted}
    if _state.get("encryption"):
        details["encryption"] = _state["encryption"]    # "key" or "passphrase", never the secret (#676)
    if chunks is not None:
        details["chunks"] = chunks
    if code:
        details["code"] = code
    _state["auditing"] = True
    try:
        written = audit.record("operator.load", result=result, actor="operator", actor_kind="operator",
                               target=package, collection=collection, details=details)
    finally:
        _state["auditing"] = False
    if not written:
        _say("the audit record could not be written")


def _interrupted(package: str, on_conflict: str, encrypted: bool):
    """The SIGINT handler: record the load as interrupted, then die of the
    signal as a kill would, so the import's `finally` never retires the marker
    of a half-built collection."""
    def handler(signum, frame):
        # The kill is in `finally`: nothing raised here may reach the import's
        # own clean-up. The line goes straight to fd 2, since print could be
        # interrupted in the middle of the main thread's own write to stderr.
        try:
            signal.signal(signal.SIGINT, signal.SIG_DFL)
            if not (_state.get("auditing") or _state.get("done")):
                os.write(2, b"loader: load interrupted: INTERRUPTED\n")
                job = _state.get("job") or {}
                _audit(package, on_conflict, encrypted, job.get("collection"), result="interrupted",
                       code="INTERRUPTED")
        finally:
            os.kill(os.getpid(), signal.SIGINT)
    return handler


def _passphrase() -> str:
    if sys.stdin.isatty():
        return getpass.getpass("Passphrase: ")
    return sys.stdin.readline().rstrip("\r\n")


def _key(limit: int) -> str:
    """The key file's text (#676): a hidden prompt on a terminal, otherwise the
    whole of standard input, refused past `limit` characters."""
    if sys.stdin.isatty():
        return getpass.getpass("Key (the AGE-SECRET-KEY-1... line): ")
    text = sys.stdin.read(limit + 1)
    if len(text) > limit:
        raise _Refused("KEY_INVALID", f"the key on standard input is longer than {limit} characters; "
                                      "give the package's key file")
    return text


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


def _load(package: str, on_conflict: str, passphrase: str | None, identity=None) -> dict:
    """Run the import code on the package and return its finished job record."""
    from services import importer
    job_id = "load-" + uuid.uuid4().hex[:8]
    # The job record lives in this process only: the import's job store is
    # unbound here (jobstore.py), so nothing is added to the API's job lists.
    importer._jobs[job_id] = {
        "job_id": job_id, "status": "queued", "filename": package, "on_conflict": on_conflict,
        "collection": None, "original_collection": None, "chunks_written": 0, "fidelity": None,
        "renamed": False, "notes": [], "restored_sessions": [], "error": None, "error_code": None,
        "error_detail": None, "encrypted": passphrase is not None or identity is not None,
        "encryption": "key" if identity is not None else "passphrase" if passphrase is not None else "none",
        "owner": None, "operator": True,        # access: restricted, no grants (#670)
    }
    job = importer._jobs[job_id]
    _state["job"] = job
    done = threading.Event()
    ticker = threading.Thread(target=_progress, args=(job, done), daemon=True)
    ticker.start()
    try:
        if identity is not None:
            importer._run(job_id, package, on_conflict, passphrase, identity=identity)
        else:
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

    from config import settings
    from services import importer, packager
    package, on_conflict = args.package, args.on_conflict
    encrypted = age_stream.is_encrypted_name(package)
    # Ctrl-C is recorded, then acts as a kill: Python's KeyboardInterrupt would
    # run the import's clean-up, which retires the marker of a half-built
    # collection that the next load must remove.
    _state.clear()
    signal.signal(signal.SIGINT, _interrupted(package, on_conflict, encrypted))
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
        passphrase = identity = None
        if encrypted and importer.package_kind(package) == "key":
            # A key export (#676): its header says so.
            _state["encryption"] = "key"
            text = _key(age_stream.KEY_FILE_MAX)
            if not text.strip():
                raise _Refused("KEY_REQUIRED", f"'{package}' is encrypted with a key; give its key file "
                                               "on standard input")
            try:
                identity = age_stream.parse_identity(text)
            except age_stream.AgeError:
                raise _Refused("KEY_INVALID", "that isn't the package's key file (one AGE-SECRET-KEY-1... "
                                              "line)") from None
            text = None
        elif encrypted:
            _state["encryption"] = "passphrase"
            passphrase = _passphrase()
            if not passphrase:
                raise _Refused("PASSPHRASE_REQUIRED", f"'{package}' is encrypted; give its passphrase")
        # What an earlier load left when it was killed: the same clean-up the
        # API runs at its start. Under the lock, and nothing else imports here.
        for removed in importer.sweep_interrupted_imports():
            _say(f"removed a collection an interrupted load left half-built: {removed}")
        job = _load(package, on_conflict, passphrase, identity)
    except _Refused as refused:
        _state["done"] = True
        _say(f"load refused: {refused.code}: {refused}")
        _audit(package, on_conflict, encrypted, None, result="refused", code=refused.code)
        return 1
    finally:
        if lock is not None:
            lock.close()

    # The run's own record follows: from here a Ctrl-C writes no other.
    _state["done"] = True
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
    _audit(package, on_conflict, encrypted, job.get("collection"), result="failed", code=code)
    return 1


if __name__ == "__main__":
    sys.exit(main())
