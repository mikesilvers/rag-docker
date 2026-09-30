"""Order-independent selection of stable chunk UUIDs."""
import hashlib
import heapq
import secrets
from uuid import UUID

MAX_SAMPLE_SIZE = 100


def select_chunk_ids(objects, limit: int, seed: int | None = None) -> list[str]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_SAMPLE_SIZE:
        raise ValueError("sample_size must be an integer between 1 and 100")
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int)):
        raise ValueError("seed must be an integer or null")
    domain = b"rag-evaluation-sample-v1\0"
    prefix = (domain + b"seed\0" + str(seed).encode("ascii") + b"\0" if seed is not None
              else domain + b"nonce\0" + secrets.token_bytes(32) + b"\0")
    base = hashlib.sha256(prefix)
    heap = []
    selected = set()
    for obj in objects:
        identity = UUID(str(obj.uuid))
        if identity in selected:
            continue
        digest = base.copy()
        digest.update(identity.bytes)
        priority = int.from_bytes(digest.digest(), "big")
        # Negated scores make the heap root the worst retained candidate.
        key = (-priority, -identity.int)
        if len(heap) == limit and key <= heap[0][:2]:
            continue
        entry = (*key, identity)
        if len(heap) == limit:
            removed = heapq.heapreplace(heap, entry)
            selected.remove(removed[2])
        else:
            heapq.heappush(heap, entry)
        selected.add(identity)
    # The UUID tie-breaker also fixes output order if priorities collide.
    return [str(entry[2]) for entry in sorted(heap, key=lambda entry: (-entry[0], -entry[1]))]
