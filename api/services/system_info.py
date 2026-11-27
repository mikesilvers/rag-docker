"""Host/VM resource reporting for the health endpoint.

The numbers here describe the Docker VM the containers run inside, not the
macOS host. On Docker Desktop, /proc/meminfo inside a container reports the
VM's total memory, which is exactly the figure a user needs when deciding
whether Ollama has room for the LLM.
"""
from __future__ import annotations

# The guest always sees slightly less than the amount configured in Docker
# Desktop, because the VM reserves some for itself (8192 MiB configured was
# measured as 7.75 GiB visible, ~3% overhead). Comparing the visible figure
# directly against the recommendation would therefore report "below" even when
# the user has allocated exactly the recommended amount, so allow a margin.
_VM_OVERHEAD_TOLERANCE = 0.95


def _read_mem_total_gb() -> float | None:
    """Total memory of the Docker VM, in GiB, or None if unreadable."""
    try:
        with open("/proc/meminfo", "r") as fh:
            for line in fh:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) / (1024 * 1024)
    except (OSError, ValueError, IndexError):
        return None
    return None


def memory_info(recommended_gb: float) -> dict:
    """Report memory allocated to Docker against the recommended minimum.

    Never raises: a health endpoint must not fail because a resource probe did.
    """
    allocated = _read_mem_total_gb()

    if allocated is None:
        return {
            "status": "unknown",
            "allocated_gb": None,
            "recommended_minimum_gb": round(recommended_gb, 1),
            "note": "Could not read /proc/meminfo to determine allocated memory.",
        }

    meets = allocated >= recommended_gb * _VM_OVERHEAD_TOLERANCE
    info = {
        "status": "ok" if meets else "below_recommended",
        "allocated_gb": round(allocated, 2),
        "recommended_minimum_gb": round(recommended_gb, 1),
    }
    if not meets:
        info["note"] = (
            f"Docker is allocated {allocated:.2f} GB; {recommended_gb:.1f} GB is "
            "recommended. The LLM needs roughly 6 GB resident, so below this it is "
            "repeatedly evicted and reloaded, and queries time out. Raise it in "
            "Docker Desktop -> Settings -> Resources -> Memory."
        )
    return info


def external_memory_info() -> dict:
    """The memory report with an external Ollama (#673): the LLM isn't in Docker,
    so there is no recommendation to compare Docker's memory with."""
    return {
        "status": "external",
        "note": ("The LLM runs in the external Ollama, outside Docker, so Docker's "
                 "memory isn't compared with RECOMMENDED_MEMORY_GB."),
    }
