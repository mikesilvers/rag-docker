"""The memory recommendation suite 01 raises past the allocation (#536).

Suite 01 checks that /health's memory report flips to below_recommended, with a
note, once the recommendation exceeds what Docker is allocated. It raises the
recommendation in-process, and the value must come from the allocation /health
reports: a fixed 13 GB stayed 'ok' on any host giving Docker 12.35 GB or more.
Standard library only.

Run: python3 scripts/verify/memory_override.py <saved /health JSON>
Prints the override in whole GB, or nothing (exit 1) without a usable allocation.
"""
import json
import math
import sys

# api/services/system_info.py reports 'ok' while allocated >= recommended * 0.95.
TOLERANCE = 0.95
# /health rounds allocated_gb to 2 decimals, so the real figure can be this much higher.
ROUNDING = 0.005


def override_gb(allocated_gb):
    """The smallest whole GB the allocation stays below after the tolerance, plus one."""
    if isinstance(allocated_gb, bool) or not isinstance(allocated_gb, (int, float)):
        return None
    if not math.isfinite(allocated_gb) or allocated_gb <= 0:
        return None
    return math.floor((allocated_gb + ROUNDING) / TOLERANCE) + 1


def main(argv):
    try:
        with open(argv[1]) as fh:
            allocated = json.load(fh)['resources']['memory']['allocated_gb']
    except (IndexError, OSError, ValueError, KeyError, TypeError) as exc:
        print('memory_override: no allocation in the /health report: {!r}'.format(exc), file=sys.stderr)
        return 1
    override = override_gb(allocated)
    if override is None:
        print('memory_override: unusable allocated_gb {!r}'.format(allocated), file=sys.stderr)
        return 1
    print(override)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
