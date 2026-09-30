from __future__ import annotations
import asyncio
import logging
import threading
import time

import weaviate
from weaviate.classes.config import Configure, Property, DataType, VectorDistances
from weaviate.classes.query import MetadataQuery, Filter

from config import settings
from models.schemas import CreateCollectionRequest, StoredCollectionRequest
from services import ingest_config
from services import retrieval_config
from services import sources

log = logging.getLogger(__name__)

_client: weaviate.WeaviateClient | None = None
_client_lock = threading.Lock()

DISTANCE_MAP = {
    "cosine": VectorDistances.COSINE,
    "dot": VectorDistances.DOT,
    "l2-squared": VectorDistances.L2_SQUARED,
}

COLLECTION_PROPERTIES = [
    Property(name="content", data_type=DataType.TEXT, index_searchable=True, index_filterable=True),
    Property(name="source_file", data_type=DataType.TEXT, index_searchable=False, index_filterable=True),
    Property(name="source_type", data_type=DataType.TEXT, index_searchable=False, index_filterable=True),
    Property(name="chunk_index", data_type=DataType.INT, index_filterable=True),
    Property(name="chunk_strategy", data_type=DataType.TEXT, index_searchable=False, index_filterable=True),
    Property(name="chunk_size", data_type=DataType.INT, index_filterable=True),
    Property(name="chunk_overlap", data_type=DataType.INT, index_filterable=True),
    Property(name="created_at", data_type=DataType.DATE, index_filterable=True),
]


def get_client() -> weaviate.WeaviateClient:
    global _client
    with _client_lock:
        if _client is None or not _client.is_connected():
            if _client is not None:
                try:
                    _client.close()
                except Exception:
                    pass
            _client = weaviate.connect_to_custom(
                http_host=settings.weaviate_host,
                http_port=settings.weaviate_port,
                http_secure=False,
                grpc_host=settings.weaviate_host,
                grpc_port=50051,
                grpc_secure=False,
            )
    return _client


def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def _check_health_sync() -> bool:
    """Liveness check that exercises the real client, not just HTTP readiness.

    get_client() performs the actual connect handshake, which is where a
    client/server version mismatch surfaces as WeaviateStartUpError. A plain
    GET of /v1/.well-known/ready does NOT catch that case: the server happily
    answers "ready" while every client call fails, so health reports green
    during a total outage of Weaviate functionality.
    """
    client = get_client()
    return bool(client.is_ready())


async def check_health() -> bool:
    return await asyncio.to_thread(_check_health_sync)


def _create_collection_sync(
    name: str,
    index_type: str,
    distance_metric: str,
    hnsw_config: dict,
    *,
    preserve_hnsw: bool = False,
) -> None:
    schema = StoredCollectionRequest if preserve_hnsw else CreateCollectionRequest
    validated = schema(name=name, index_type=index_type,
                                        distance_metric=distance_metric, hnsw_config=hnsw_config)
    hnsw_config = validated.hnsw_config.model_dump()
    client = get_client()
    dist = DISTANCE_MAP[validated.distance_metric]

    if index_type == "flat":
        vector_index = Configure.VectorIndex.flat(distance_metric=dist)
    else:
        vector_index = Configure.VectorIndex.hnsw(
            distance_metric=dist,
            ef_construction=hnsw_config.get("efConstruction", 128),
            max_connections=hnsw_config.get("maxConnections", 64),
            ef=hnsw_config.get("ef", 64),
        )

    vectorizer = Configure.Vectorizer.text2vec_ollama(
        api_endpoint=f"http://{settings.ollama_host}:{settings.ollama_port}",
        model=settings.embed_model,
        vectorize_collection_name=False,
    )

    client.collections.create(
        name=name,
        vectorizer_config=vectorizer,
        vector_index_config=vector_index,
        properties=COLLECTION_PROPERTIES,
    )


async def create_collection(
    name: str,
    index_type: str = "hnsw",
    distance_metric: str = "cosine",
    hnsw_config: dict | None = None,
) -> None:
    await asyncio.to_thread(
        _create_collection_sync, name, index_type, distance_metric, hnsw_config or {}
    )


def _collection_exists_sync(name: str) -> bool:
    return get_client().collections.exists(name)


async def collection_exists(name: str) -> bool:
    return await asyncio.to_thread(_collection_exists_sync, name)


def _delete_collection_sync(name: str) -> int:
    client = get_client()
    coll = client.collections.get(name)
    count = coll.aggregate.over_all(total_count=True).total_count
    client.collections.delete(name)
    # Retained originals must go with the collection. The sources volume is
    # surfaced nowhere in the UI, so a leak here would be invisible.
    sources.delete(name)
    retrieval_config.delete(name)
    ingest_config.delete(name)
    # Gold-standard sessions are kept and flagged, never deleted (spec §8 rule 4):
    # they are evaluation work the user may still want, and the pairs stay
    # readable even with the collection gone. Imported here rather than at module
    # level because goldstandard imports this module.
    from services import goldstandard
    goldstandard.mark_orphaned(
        name, f"collection '{name}' was deleted")
    return count or 0


async def delete_collection(name: str) -> int:
    return await asyncio.to_thread(_delete_collection_sync, name)


def _get_collections_sync() -> list[dict]:
    client = get_client()
    all_cols = client.collections.list_all()
    result = []
    for col_name in all_cols:
        coll = client.collections.get(col_name)
        count = coll.aggregate.over_all(total_count=True).total_count or 0

        # list_all() returns _CollectionConfigSimple, which does NOT carry
        # vector_index_config (weaviate-client 4.x dropped it from the reduced
        # config). Fetch the full per-collection config for the index details.
        vector_config = coll.config.get().vector_index_config
        index_type = "flat" if "flat" in type(vector_config).__name__.lower() else "hnsw"

        distance_attr = getattr(vector_config, "distance_metric", VectorDistances.COSINE)
        distance_str = {
            VectorDistances.COSINE: "cosine",
            VectorDistances.DOT: "dot",
            VectorDistances.L2_SQUARED: "l2-squared",
        }.get(distance_attr, "cosine")

        result.append({
            "name": col_name,
            "object_count": count,
            "index_type": index_type,
            "distance_metric": distance_str,
        })
    return result


async def get_collections() -> list[dict]:
    return await asyncio.to_thread(_get_collections_sync)


# Collections created mid-operation and normally removed by the operation's own
# cleanup. A hard kill (SIGKILL, OOM, `docker compose kill`) skips that cleanup,
# so they are swept at startup instead — nothing can legitimately be using one
# before the application has begun serving.
STAGING_MARKERS = ("__importing_", "__tuning_")


def _sweep_staging_sync() -> list[str]:
    client = get_client()
    removed = []
    for name in list(client.collections.list_all()):
        if any(marker in name for marker in STAGING_MARKERS):
            try:
                client.collections.delete(name)
            except Exception:                         # noqa: BLE001
                log.exception("Could not remove abandoned staging collection %r", name)
                continue
            removed.append(name)
    return removed


async def sweep_staging() -> list[str]:
    """Remove staging collections abandoned by a previous process."""
    return await asyncio.to_thread(_sweep_staging_sync)


def _meta_sync() -> dict:
    """Server metadata. `version` goes into the export manifest."""
    try:
        return get_client().get_meta() or {}
    except Exception:
        return {}


async def get_meta() -> dict:
    return await asyncio.to_thread(_meta_sync)


def _collection_config_sync(name: str) -> dict:
    """The collection's schema and index settings, as plain JSON.

    Shaped to match the body `POST /collections` accepts, so an import can
    recreate the collection by feeding this straight back in.
    """
    coll = get_client().collections.get(name)
    cfg = coll.config.get()
    vi = cfg.vector_index_config

    index_type = "flat" if "flat" in type(vi).__name__.lower() else "hnsw"
    distance = {
        VectorDistances.COSINE: "cosine",
        VectorDistances.DOT: "dot",
        VectorDistances.L2_SQUARED: "l2-squared",
    }.get(getattr(vi, "distance_metric", VectorDistances.COSINE), "cosine")

    # Absent on a flat index; the defaults mirror _create_collection_sync so a
    # flat collection imported as hnsw would still be built sanely.
    hnsw = {
        "efConstruction": getattr(vi, "ef_construction", 128),
        "maxConnections": getattr(vi, "max_connections", 64),
        "ef": getattr(vi, "ef", 64),
    }

    return {
        "name": name,
        "index_type": index_type,
        "distance_metric": distance,
        "hnsw_config": hnsw,
        "properties": [
            {"name": p.name, "data_type": getattr(p.data_type, "value", str(p.data_type))}
            for p in (cfg.properties or [])
        ],
        # Recorded for information. Import always rebuilds the collection with
        # this instance's vectorizer, because the vectors come from the package.
        "vectorizer": str(getattr(cfg, "vectorizer", "") or ""),
    }


async def get_collection_config(name: str) -> dict:
    return await asyncio.to_thread(_collection_config_sync, name)


# Just after a collection is created under a name that was dropped moments
# earlier, Weaviate can reject writes with "could not find index for class ...
# It might have been deleted in the meantime" until the new index is loaded.
# The verify suite hit this when it recreated a collection between checks (#95).
_INDEX_NOT_READY = "could not find index"
_INSERT_ATTEMPTS = 3
_INSERT_RETRY_DELAY = 1.0


def _insert_chunks_sync(collection_name: str, chunks: list[dict]) -> None:
    client = get_client()
    coll = client.collections.get(collection_name)
    pending = list(chunks)
    for attempt in range(1, _INSERT_ATTEMPTS + 1):
        with coll.batch.dynamic() as batch:
            for chunk in pending:
                batch.add_object(properties=chunk)
        # Read failures only after the batch has flushed on exit. Checking
        # inside the block missed them, so a job could report 'completed'
        # with nothing stored.
        failed = coll.batch.failed_objects
        if not failed:
            return
        if attempt < _INSERT_ATTEMPTS and all(_INDEX_NOT_READY in f.message for f in failed):
            pending = [f.object_.properties for f in failed]
            time.sleep(_INSERT_RETRY_DELAY)
            continue
        raise RuntimeError(
            f"{len(failed)} batch error(s) inserting into '{collection_name}': {failed[0].message}")


async def insert_chunks(collection_name: str, chunks: list[dict]) -> None:
    await asyncio.to_thread(_insert_chunks_sync, collection_name, chunks)


def _near_vector_query_sync(
    collection_name: str, vector: list[float], top_k: int
) -> list[dict]:
    client = get_client()
    coll = client.collections.get(collection_name)
    result = coll.query.near_vector(
        near_vector=vector,
        limit=top_k,
        return_metadata=MetadataQuery(distance=True),
        return_properties=["content", "source_file", "chunk_index"],
    )
    rows = []
    for obj in result.objects:
        dist = obj.metadata.distance or 0.0
        rows.append({
            "content": obj.properties.get("content", ""),
            "source_file": obj.properties.get("source_file", ""),
            "chunk_index": obj.properties.get("chunk_index", 0),
            "score": dist,
        })
    return rows


async def near_vector_query(
    collection_name: str, vector: list[float], top_k: int
) -> list[dict]:
    return await asyncio.to_thread(_near_vector_query_sync, collection_name, vector, top_k)


def _near_text_query_sync(
    collection_name: str, query: str, top_k: int
) -> list[dict]:
    client = get_client()
    coll = client.collections.get(collection_name)
    result = coll.query.near_text(
        query=query,
        limit=top_k,
        return_metadata=MetadataQuery(distance=True),
        return_properties=["content", "source_file", "chunk_index"],
    )
    rows = []
    for obj in result.objects:
        dist = obj.metadata.distance or 0.0
        rows.append({
            "content": obj.properties.get("content", ""),
            "source_file": obj.properties.get("source_file", ""),
            "chunk_index": obj.properties.get("chunk_index", 0),
            "score": dist,
        })
    return rows


async def near_text_query(
    collection_name: str, query: str, top_k: int
) -> list[dict]:
    return await asyncio.to_thread(_near_text_query_sync, collection_name, query, top_k)


def _hybrid_query_sync(
    collection_name: str, query: str, alpha: float, top_k: int
) -> list[dict]:
    client = get_client()
    coll = client.collections.get(collection_name)
    result = coll.query.hybrid(
        query=query,
        alpha=alpha,
        limit=top_k,
        return_metadata=MetadataQuery(score=True),
        return_properties=["content", "source_file", "chunk_index"],
    )
    rows = []
    for obj in result.objects:
        rows.append({
            "content": obj.properties.get("content", ""),
            "source_file": obj.properties.get("source_file", ""),
            "chunk_index": obj.properties.get("chunk_index", 0),
            "score": obj.metadata.score or 0.0,
        })
    return rows


async def hybrid_query(
    collection_name: str, query: str, alpha: float, top_k: int
) -> list[dict]:
    return await asyncio.to_thread(_hybrid_query_sync, collection_name, query, alpha, top_k)


def _sample_chunks_sync(collection_name: str, limit: int, seed: int | None = None) -> list[dict]:
    from models.schemas import GenerateRequest
    from services.chunk_sampling import select_chunk_ids
    request = GenerateRequest(collection=collection_name, sample_size=limit, seed=seed)
    client = get_client()
    coll = client.collections.get(collection_name)
    objects = coll.iterator(include_vector=False, return_properties=[], cache_size=100)
    identities = select_chunk_ids(objects, request.sample_size, request.seed)
    if not identities:
        return []
    payloads = coll.query.fetch_objects(
        filters=Filter.by_id().contains_any(identities), limit=len(identities),
        include_vector=False, return_properties=["content", "source_file", "chunk_index"],
    ).objects
    by_id = {str(obj.uuid): obj.properties for obj in payloads}
    # Concurrent deletion can remove a winner between the UUID and payload passes.
    return [{"object_id": identity, "content": by_id[identity].get("content", ""),
             "source_file": by_id[identity].get("source_file", ""),
             "chunk_index": by_id[identity].get("chunk_index", 0)}
            for identity in identities if identity in by_id]


async def sample_chunks(collection_name: str, limit: int, seed: int | None = None) -> list[dict]:
    return await asyncio.to_thread(_sample_chunks_sync, collection_name, limit, seed)
