"""Real SDK sampling checks; owned synthetic objects with supplied vectors.

Run inside a disposable API: python - < scripts/verify/chunk_sampling.py
Does not call embedding or language models. UUID selection, not answer output,
is the reproducibility contract. Deletes only its unique collection/config.
"""
import uuid
import os
from services import weaviate_client as wc
from services.chunk_sampling import select_chunk_ids

collection=os.environ.get('RAG_TEST_PREFIX','Vfy')+'Sampling'+uuid.uuid4().hex[:12]
creation_attempted=False
try:
    assert not wc._collection_exists_sync(collection)
    creation_attempted=True
    wc._create_collection_sync(collection,'hnsw','cosine',{})
    coll=wc.get_client().collections.get(collection)
    for i in range(1,161):
        coll.data.insert(uuid=uuid.UUID(int=i),properties={
            'content':f'Inert sampling fixture {i}', 'source_file':'sampling.txt',
            'chunk_index':i},vector=[0.1]*768)
    assert coll.aggregate.over_all(total_count=True).total_count==160
    print('PASS created 160 owned synthetic objects with supplied vectors',flush=True)
    first=wc._sample_chunks_sync(collection,5,7)
    assert len(first)==5 and len({r['object_id'] for r in first})==5
    assert any(uuid.UUID(r['object_id']).int>100 for r in first)
    print('PASS seeded sample reaches beyond the first 100-object iterator page',flush=True)
    assert wc._sample_chunks_sync(collection,5,7)==first
    print('PASS repeated seed preserves UUIDs and ordered payloads',flush=True)
    snapshot=list(coll.iterator(include_vector=False,return_properties=[],cache_size=100))
    assert len(snapshot)==160 and all(not obj.properties for obj in snapshot)
    assert all(row['source_file']=='sampling.txt' and row['content']==f"Inert sampling fixture {uuid.UUID(row['object_id']).int}" for row in first)
    assert select_chunk_ids(reversed(snapshot),5,7)==[row['object_id'] for row in first]
    print('PASS UUID-only SDK scan and reversed order preserve selected payloads',flush=True)
    maximum=wc._sample_chunks_sync(collection,100,7)
    assert len(maximum)==100 and len({r['object_id'] for r in maximum})==100
    print('PASS maximum request is bounded across multiple iterator pages',flush=True)
    for i in range(61,161): coll.data.delete_by_id(uuid.UUID(int=i))
    rows=wc._sample_chunks_sync(collection,100,7)
    assert len(rows)==60 and {r['object_id'] for r in rows}=={str(uuid.UUID(int=i)) for i in range(1,61)}
    print('PASS oversize request returns all available unique objects',flush=True)
    unseeded=wc._sample_chunks_sync(collection,5,None)
    assert len(unseeded)==5 and all(1<=uuid.UUID(r['object_id']).int<=60 for r in unseeded)
    print('PASS null seed produces a bounded valid sample',flush=True)
finally:
    if creation_attempted and wc._collection_exists_sync(collection): wc._delete_collection_sync(collection)
    wc.close_client()
assert not wc._collection_exists_sync(collection)
wc.close_client()
print('PASS owned collection and configuration removed',flush=True)
