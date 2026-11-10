from __future__ import annotations
from services import telemetry
import asyncio
import logging
import threading
import time
from datetime import datetime

import weaviate
from weaviate.classes.config import Configure, Property, DataType, VectorDistances
from weaviate.classes.query import MetadataQuery, Filter

from services import collection_writes, collection_recovery
from config import settings
from models.schemas import CreateCollectionRequest, StoredCollectionRequest
from services import ingest_config
from services import retrieval_config
from services import sources
from services import batch_write

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
            _client = telemetry.call("weaviate.connect", weaviate.connect_to_custom,
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
    with telemetry.span("weaviate.health"):
        client = get_client()
        ready = bool(client.is_ready())
        if not ready:
            telemetry.outcome("error")
        return ready


async def check_health() -> bool:
    return await asyncio.to_thread(_check_health_sync)


@collection_writes.serialized("name")
def _create_collection_sync(
    name: str,
    index_type: str,
    distance_metric: str,
    hnsw_config: dict,
    *,
    preserve_hnsw: bool = False,
    description: str | None = None,
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

    telemetry.call("weaviate.create", client.collections.create,
        name=name,
        # Set only by import, to bind its in-progress marker to this instance.
        description=description,
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
    return telemetry.call("weaviate.exists", get_client().collections.exists, name)


async def collection_exists(name: str) -> bool:
    return await asyncio.to_thread(_collection_exists_sync, name)


@collection_writes.serialized("name")
def _delete_collection_sync(name: str) -> int:
    client = get_client()
    coll = client.collections.get(name)
    canonical_name = telemetry.call("weaviate.config", coll.config.get).name
    count = telemetry.call("weaviate.aggregate", coll.aggregate.over_all, total_count=True).total_count
    telemetry.call("weaviate.delete", client.collections.delete, canonical_name)
    collection_recovery.retire_deleted(canonical_name, client)
    # Retained originals must go with the collection. The sources volume is
    # surfaced nowhere in the UI, so a leak here would be invisible.
    # Older writers saved sidecars under the first-character backend alias.
    # The caller can use either spelling, so clean both after backend deletion.
    # Other case changes can name distinct collections and must be preserved.
    spellings = collection_writes.aliases(canonical_name)
    for spelling in spellings:
        sources.delete(spelling)
        retrieval_config.delete(spelling)
        ingest_config.delete(spelling)
    # Gold-standard sessions are kept and flagged, never deleted (spec §8 rule 4):
    # they are evaluation work the user may still want, and the pairs stay
    # readable even with the collection gone. Imported here rather than at module
    # level because goldstandard imports this module.
    from services import goldstandard
    for spelling in spellings:
        goldstandard.mark_orphaned(
            spelling, f"collection '{canonical_name}' was deleted")
    return count or 0


async def delete_collection(name: str) -> int:
    return await asyncio.to_thread(_delete_collection_sync, name)


def _delete_collection_when_idle_sync(name: str) -> int:
    # The user's delete only. Import-replace and tuning delete their own target
    # while their job is registered, through _delete_collection_sync or the
    # client, and must never be refused by their own registration (#40).
    return collection_writes.delete_when_idle(name, _delete_collection_sync)


async def delete_collection_when_idle(name: str) -> int:
    """Delete unless a job is active for the collection.

    Raises collection_writes.CollectionInUseError naming the active jobs.
    """
    return await asyncio.to_thread(_delete_collection_when_idle_sync, name)


def _get_collections_sync() -> list[dict]:
    client = get_client()
    all_cols = telemetry.call("weaviate.list", client.collections.list_all)
    result = []
    for col_name in all_cols:
        coll = client.collections.get(col_name)
        count = telemetry.call("weaviate.aggregate", coll.aggregate.over_all, total_count=True).total_count or 0

        # list_all() returns _CollectionConfigSimple, which does NOT carry
        # vector_index_config (weaviate-client 4.x dropped it from the reduced
        # config). Fetch the full per-collection config for the index details.
        vector_config = telemetry.call("weaviate.config", coll.config.get).vector_index_config
        index_name = type(vector_config).__name__.lower()
        hnsw_fields = tuple(
            getattr(vector_config, field, None)
            for field in ("ef", "ef_construction", "max_connections")
        )
        if "flat" in index_name:
            index_type = "flat"
        elif "dynamic" in index_name:
            index_type = "dynamic"
        elif all(value is not None for value in hnsw_fields):
            index_type = "hnsw"
        else:
            index_type = "unknown"

        distance_attr = getattr(vector_config, "distance_metric", None)
        distance_str = {
            VectorDistances.COSINE: "cosine",
            VectorDistances.DOT: "dot",
            VectorDistances.L2_SQUARED: "l2-squared",
        }.get(distance_attr, "unknown")

        result.append({
            "name": col_name,
            "object_count": count,
            "index_type": index_type,
            "distance_metric": distance_str,
            "hnsw_config": ({"ef": hnsw_fields[0],
                             "efConstruction": hnsw_fields[1],
                             "maxConnections": hnsw_fields[2]}
                            if index_type == "hnsw" else None),
        })
    return result


async def get_collections() -> list[dict]:
    return await asyncio.to_thread(_get_collections_sync)


def _sweep_staging_sync() -> list[str]:
    return collection_recovery.sweep(get_client())


async def sweep_staging() -> list[str]:
    """Remove positively owned scratch; preserve recovery and unowned names."""
    return await asyncio.to_thread(_sweep_staging_sync)


def _meta_sync() -> dict:
    """Server metadata. `version` goes into the export manifest."""
    try:
        return telemetry.call("weaviate.meta", get_client().get_meta) or {}
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
    cfg = telemetry.call("weaviate.config", coll.config.get)
    vi = cfg.vector_index_config
    vectorizer = getattr(cfg, "vectorizer_config", None)
    kind = getattr(vectorizer, "vectorizer", None)
    model_config = getattr(vectorizer, "model", None)
    embedding_model = (model_config.get("model")
                       if getattr(kind, "value", kind) == "text2vec-ollama"
                       and isinstance(model_config, dict)
                       and not getattr(cfg, "vector_config", None) else None)

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
        "embedding_model": embedding_model,
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


def _validate_reindex_vectorizer_sync(name: str) -> None:
    """Fail before staging if recreation would change the stored vector space."""
    cfg = telemetry.call("weaviate.config", get_client().collections.get(name).config.get)
    vectorizer = getattr(cfg, "vectorizer_config", None)
    kind = getattr(vectorizer, "vectorizer", None)
    model = getattr(vectorizer, "model", None)
    expected_model = {"model": settings.embed_model,
                      "apiEndpoint": f"http://{settings.ollama_host}:{settings.ollama_port}"}
    compatible = (getattr(kind, "value", kind) == "text2vec-ollama"
                  and model == expected_model
                  and getattr(vectorizer, "vectorize_collection_name", None) is False
                  and not getattr(cfg, "vector_config", None))
    # Property names/types and skip/name flags also determine provider input.
    # Refuse unknown module options and custom properties instead of copying
    # old vectors into the fixed schema with different future insert rules.
    expected_properties = {p.name: p._to_dict() for p in COLLECTION_PROPERTIES}
    properties = list(getattr(cfg, "properties", None) or [])
    compatible = compatible and len(properties) == len(expected_properties) and {p.name for p in properties} == set(expected_properties)
    for prop in properties:
        expected = expected_properties.get(prop.name)
        rules = getattr(prop, "vectorizer_config", None)
        compatible = compatible and bool(
            expected
            and getattr(prop.data_type, "value", prop.data_type) == expected["dataType"][0]
            and getattr(prop, "vectorizer", None) == "text2vec-ollama"
            and not getattr(prop, "vectorizer_configs", None)
            and rules is not None
            and rules.skip == expected["skip_vectorization"]
            and rules.vectorize_property_name == expected["vectorize_property_name"]
            and not getattr(prop, "nested_properties", None))
    if not compatible:
        raise ValueError("Reindex would change the collection's vectorizer configuration; "
                         "re-embed with the configured model first")



# Just after a collection is created under a name that was dropped moments
# earlier, Weaviate can reject writes until the new index is loaded.
_INDEX_NOT_READY = "could not find index"
_INSERT_ATTEMPTS = 3
_INSERT_RETRY_DELAY = 1.0


@telemetry.traced("rag.store")
@collection_writes.serialized("collection_name")
def _insert_chunks_sync(collection_name: str, chunks: list[dict]) -> None:
    client = get_client()
    coll = client.collections.get(collection_name)
    for attempt in range(1, _INSERT_ATTEMPTS + 1):
        try:
            # The writer verifies persisted records and removes only UUIDs
            # generated by a failed attempt before a retry can begin.
            return batch_write.insert(
                coll, lambda: ({"properties": chunk} for chunk in chunks),
                exact=False, cleanup_owned=True)
        except RuntimeError as exc:
            failed = coll.batch.failed_objects
            if not (type(exc) is RuntimeError
                    and str(exc).startswith("Weaviate rejected ")
                    and attempt < _INSERT_ATTEMPTS and failed
                    and all(_INDEX_NOT_READY in getattr(f, "message", "") for f in failed)):
                raise
            time.sleep(_INSERT_RETRY_DELAY)


# Weaviate returns at most QUERY_MAXIMUM_RESULTS (10,000 by default) objects per
# query and deletes at most that many per delete_many; offset paging stops there
# too, and its cursor can't be combined with a filter (#622).
_QUERY_MAXIMUM_RESULTS = 10_000
_SOURCE_PROPERTIES = ["source_file", "created_at"]


def _exact_source_objects(coll, name: str, accept=None):
    """The objects stored under exactly `name`, read with only `source_file` and
    `created_at` (#622).

    `source_file` is word-tokenized, so equal(name) matches every name containing
    all its words: `a/report.pdf`, `Report.PDF` and `report.pdf (part 1)` match
    `report.pdf`. That superset always holds the exact name, so it is read and
    filtered here by the exact value. Over QUERY_MAXIMUM_RESULTS matches a filtered
    query can't reach them all, so the whole collection is walked instead.

    `accept(value)` decides which values are kept; by default, the name itself. A
    rule may accept only values among equal(name)'s matches, such as the name and
    its `<name> (part N)` family. Objects are yielded lazily, so a caller that needs
    one stops the walk there.
    """
    if accept is None:
        def accept(value):
            return value == name
    words = Filter.by_property("source_file").equal(name)
    total = telemetry.call("weaviate.aggregate", coll.aggregate.over_all,
                           filters=words, total_count=True).total_count or 0
    if not total:
        return
    if total <= _QUERY_MAXIMUM_RESULTS:
        objects = telemetry.call("weaviate.query", coll.query.fetch_objects,
            filters=words, limit=_QUERY_MAXIMUM_RESULTS, include_vector=False,
            return_properties=_SOURCE_PROPERTIES).objects
    else:
        # The client walks 100 objects per request by default; one query already
        # returns QUERY_MAXIMUM_RESULTS of these two-property objects, so each
        # page of the walk takes that many (#628).
        objects = telemetry.iterate(coll.iterator, include_vector=False,
                                    return_properties=_SOURCE_PROPERTIES,
                                    cache_size=_QUERY_MAXIMUM_RESULTS)
    try:
        for obj in objects:
            value = (obj.properties or {}).get("source_file")
            if isinstance(value, str) and accept(value):
                yield obj
    finally:
        # Closed early, a walk ends its span now rather than when collected.
        close = getattr(objects, "close", None)
        if close is not None:
            close()


def _source_has_chunks_sync(collection_name: str, source_file: str, accept=None) -> bool:
    """Whether the collection holds any chunk stored under exactly this name (or,
    with `accept`, under a value it keeps)."""
    coll = get_client().collections.get(collection_name)
    selected = _exact_source_objects(coll, source_file, accept)
    try:
        return next(selected, None) is not None
    finally:
        selected.close()


def _family(name: str):
    """The rule keeping `name` and its `<name> (part N)` parts, exactly (#621).
    Every part's words include the name's, so equal(name) matches them all."""
    from services import parts
    return lambda value: parts.in_family(value, name)


def _family_has_chunks_sync(collection_name: str, name: str) -> bool:
    """Whether `name` or any of its parts has chunks (#621)."""
    return _source_has_chunks_sync(collection_name, name, _family(name))


class ReplaceError(RuntimeError):
    """A name's earlier chunks could not all be removed after its new ones were
    stored. `deleted` is how many were removed before it stopped."""

    def __init__(self, message: str, deleted: int) -> None:
        super().__init__(message)
        self.deleted = deleted


# One replacement deletes at most this many batches of QUERY_MAXIMUM_RESULTS
# earlier chunks: a million per name.
_REPLACE_DELETE_ROUNDS = 100


@collection_writes.serialized("collection_name")
def _delete_earlier_chunks_sync(collection_name: str, source_file: str,
                                created_at: datetime, expected: int, accept=None) -> int:
    """Remove a name's earlier chunks after its `expected` new ones are stored (#22).

    Called only after the insert has been verified, never before: if the insert
    fails, the earlier version stays whole; if this fails, the name has
    duplicates but nothing is lost, and uploading it again repairs it.

    Only chunks stored under exactly this name are counted or deleted (#622): they
    are selected with _exact_source_objects and deleted by UUID. The new chunks all
    carry the same `created_at`, written by this job, and are told apart by
    equality with it, in Python, since a walk of the collection can't filter; the
    value reads back to the microsecond. Equality rather than "older than" keeps
    the selection independent of earlier chunks' timestamps (an imported
    package's can be later than now). Before deleting, exactly `expected` of the
    name's chunks must carry it; afterwards exactly they must remain. Returns how
    many earlier chunks were deleted. With `accept`, the chunks are those of the
    values it keeps, such as a name and its parts (#621).
    """
    coll = get_client().collections.get(collection_name)
    new, earlier = 0, []
    for obj in _exact_source_objects(coll, source_file, accept):
        if obj.properties.get("created_at") == created_at:
            new += 1
        else:
            earlier.append(obj.uuid)
    if new != expected:
        raise ReplaceError(f"found {new} of its {expected} new chunks by their timestamp, "
                           "so none of the earlier ones were removed", 0)
    bound = _REPLACE_DELETE_ROUNDS * _QUERY_MAXIMUM_RESULTS
    if len(earlier) > bound:
        raise ReplaceError(f"{len(earlier)} earlier chunks are more than one replacement "
                           f"removes ({bound}), so none were removed", 0)
    deleted = 0
    for start in range(0, len(earlier), _QUERY_MAXIMUM_RESULTS):
        batch = earlier[start:start + _QUERY_MAXIMUM_RESULTS]
        result = telemetry.call("weaviate.delete", coll.data.delete_many,
                                where=Filter.by_id().contains_any(batch))
        deleted += result.successful or 0
        if result.failed:
            raise ReplaceError(f"Weaviate failed to delete {result.failed} earlier chunk(s)", deleted)
    left = sum(1 for _ in _exact_source_objects(coll, source_file, accept)) - expected
    if left > 0:
        raise ReplaceError(f"{left} earlier chunk(s) remain", deleted)
    if left < 0:
        raise ReplaceError(f"{-left} of its {expected} new chunks are missing afterwards", deleted)
    return deleted


def _delete_earlier_family_sync(collection_name: str, name: str, created_at: datetime,
                                expected: int) -> int:
    """Remove the earlier chunks of `name` and of every `<name> (part N)` once the
    `expected` new ones are stored, so a re-upload leaves exactly the new family
    whether or not either version was split (#621)."""
    return _delete_earlier_chunks_sync(collection_name, name, created_at, expected,
                                       accept=_family(name))


@collection_writes.serialized("collection_name")
def _delete_new_family_sync(collection_name: str, name: str, created_at: datetime) -> int:
    """Remove the chunks this ingest stored for `name` and its parts (they carry
    its `created_at`), so a file whose later part failed leaves its earlier
    version whole (#621). Returns how many were removed; ReplaceError, with that
    count, when Weaviate fails to remove some or some remain."""
    coll = get_client().collections.get(collection_name)
    accept = _family(name)
    new = [obj.uuid for obj in _exact_source_objects(coll, name, accept)
           if obj.properties.get("created_at") == created_at]
    deleted = 0
    for start in range(0, len(new), _QUERY_MAXIMUM_RESULTS):
        batch = new[start:start + _QUERY_MAXIMUM_RESULTS]
        result = telemetry.call("weaviate.delete", coll.data.delete_many,
                                where=Filter.by_id().contains_any(batch))
        deleted += result.successful or 0
        if result.failed:
            raise ReplaceError(f"Weaviate failed to delete {result.failed} new chunk(s)", deleted)
    left = sum(1 for obj in _exact_source_objects(coll, name, accept)
               if obj.properties.get("created_at") == created_at)
    if left:
        raise ReplaceError(f"{left} new chunk(s) remain", deleted)
    return deleted


async def insert_chunks(collection_name: str, chunks: list[dict]) -> None:
    await asyncio.to_thread(_insert_chunks_sync, collection_name, chunks)


# A citation's score is higher-is-better in every retrieval mode (#376), so the
# distance-based modes turn Weaviate's distance into a similarity by the
# collection's distance metric.
_SIMILARITY = {
    VectorDistances.COSINE: lambda d: 1 - d,            # cosine similarity, -1 to 1
    VectorDistances.DOT: lambda d: -d,                  # the dot product itself
    VectorDistances.L2_SQUARED: lambda d: 1 / (1 + d),  # 0 to 1
}


def _distance_to_score(coll):
    """The distance-to-score conversion for this collection's metric.

    The metric is read on every query, not cached, so a collection deleted and
    recreated with another metric is converted by its new one. A missing
    distance, or a config with no metric or a metric with no conversion, gives
    None. A failed config read raises, and the query answers the #373 503.
    """
    vector_config = telemetry.call("weaviate.config", coll.config.get).vector_index_config
    convert = _SIMILARITY.get(getattr(vector_config, "distance_metric", None))

    def to_score(distance: float | None) -> float | None:
        if convert is None or distance is None:
            return None
        return convert(distance)
    return to_score


def _near_vector_query_sync(
    collection_name: str, vector: list[float], top_k: int
) -> list[dict]:
    client = get_client()
    coll = client.collections.get(collection_name)
    to_score = _distance_to_score(coll)
    result = telemetry.call("weaviate.query", coll.query.near_vector,
        near_vector=vector,
        limit=top_k,
        return_metadata=MetadataQuery(distance=True),
        return_properties=["content", "source_file", "chunk_index"],
    )
    rows = []
    for obj in result.objects:
        rows.append({
            "content": obj.properties.get("content", ""),
            "source_file": obj.properties.get("source_file", ""),
            "chunk_index": obj.properties.get("chunk_index", 0),
            "score": to_score(obj.metadata.distance),
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
    to_score = _distance_to_score(coll)
    result = telemetry.call("weaviate.query", coll.query.near_text,
        query=query,
        limit=top_k,
        return_metadata=MetadataQuery(distance=True),
        return_properties=["content", "source_file", "chunk_index"],
    )
    rows = []
    for obj in result.objects:
        rows.append({
            "content": obj.properties.get("content", ""),
            "source_file": obj.properties.get("source_file", ""),
            "chunk_index": obj.properties.get("chunk_index", 0),
            "score": to_score(obj.metadata.distance),
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
    result = telemetry.call("weaviate.query", coll.query.hybrid,
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
            # Weaviate's fusion score, higher is better; missing reads as None (#376).
            "score": obj.metadata.score,
        })
    return rows


async def hybrid_query(
    collection_name: str, query: str, alpha: float, top_k: int
) -> list[dict]:
    return await asyncio.to_thread(_hybrid_query_sync, collection_name, query, alpha, top_k)


class CollectionNotFoundError(LookupError):
    """The collection disappeared before guarded sampling."""


def _sample_chunks_sync(collection_name: str, limit: int, seed: int | None = None) -> list[dict]:
    from models.schemas import GenerateRequest
    from services.chunk_sampling import select_chunk_ids
    request = GenerateRequest(collection=collection_name, sample_size=limit, seed=seed)
    client = get_client()
    if not telemetry.call("weaviate.exists", client.collections.exists, collection_name):
        raise CollectionNotFoundError(collection_name)
    coll = client.collections.get(collection_name)
    objects = telemetry.iterate(coll.iterator, include_vector=False, return_properties=[], cache_size=100)
    identities = select_chunk_ids(objects, request.sample_size, request.seed)
    if not identities:
        return []
    payloads = telemetry.call("weaviate.query", coll.query.fetch_objects,
        filters=Filter.by_id().contains_any(identities), limit=len(identities),
        include_vector=False, return_properties=["content", "source_file", "chunk_index"],
    ).objects
    by_id = {str(obj.uuid): obj.properties for obj in payloads}
    # Concurrent deletion can remove a winner between the UUID and payload passes.
    return [{"object_id": identity, "content": by_id[identity].get("content", ""),
             "source_file": by_id[identity].get("source_file", ""),
             "chunk_index": by_id[identity].get("chunk_index", 0)}
            for identity in identities if identity in by_id]
