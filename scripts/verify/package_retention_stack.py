"""Fixtures for 05_transfer.sh's download and expiry checks (#677).

Runs inside the verify project's API container, on its exports folder and
upload volume:

    docker compose exec -T api python - <command> [args] < scripts/verify/package_retention_stack.py

Commands:
    junk <name> <MiB> <hours-ago|none>  write a file of random bytes in the exports
                                        folder, recorded as exported that many hours
                                        ago, or not recorded at all (a package copied in)
    sweep                               run one expiry sweep; print the deleted names
    exists <name>                       exit 0 when the file is there, 1 when not
    remove <name>                       delete the file and its record entry

Only names starting `ragpkg-vfy677-` are accepted, so nothing else in the
exports folder can be touched.
"""
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, "/app")

from config import settings                          # noqa: E402
from services import package_retention               # noqa: E402

STEM = "ragpkg-vfy677-"


def name_of(value):
    if not value.startswith(STEM) or not package_retention.valid_name(value):
        sys.exit(f"refused: {value!r} is not a {STEM}* package name")
    return value


def main(argv):
    command, args = argv[0], argv[1:]
    exports = Path(settings.exports_dir)
    if command == "junk":
        name, mib, ago = name_of(args[0]), int(args[1]), args[2]
        with open(exports / name, "wb") as fh:
            for _ in range(mib):
                fh.write(os.urandom(1024 * 1024))
        if ago != "none":
            package_retention.record_export(name, datetime.now(timezone.utc) - timedelta(hours=float(ago)))
        print(name)
    elif command == "sweep":
        print("\n".join(package_retention.sweep()))
    elif command == "exists":
        return 0 if (exports / name_of(args[0])).exists() else 1
    elif command == "remove":
        name = name_of(args[0])
        (exports / name).unlink(missing_ok=True)
        with package_retention._lock:
            package_retention._drop(name)
    else:
        sys.exit(f"unknown command {command!r}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
