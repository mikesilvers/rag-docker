"""Real parser/ingest/Weaviate text-storage acceptance on one owned collection.

Run inside a disposable API: python - < scripts/verify/overlap_chunks.py
Default fixture disables vectorization to isolate text/window storage from
model availability. RAG_OVERLAP_REAL_EMBEDDING=1 optionally uses production
Ollama vectorization; do not treat the default as model acceptance.
Only this script's unique collection and temporary source/config paths are used.
"""
import os
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch
from weaviate.classes.config import Configure, VectorDistances
from config import settings
from services import chunker, ingest_pipeline, weaviate_client as wc

collection = os.environ.get('RAG_TEST_PREFIX', 'Vfy') + 'Overlap' + uuid.uuid4().hex[:12]
assert not wc._collection_exists_sync(collection)
created = False
try:
    real_embedding = os.environ.get('RAG_OVERLAP_REAL_EMBEDDING') == '1'
    if real_embedding:
        wc._create_collection_sync(collection, 'hnsw', 'cosine', {})
    else:
        wc.get_client().collections.create(name=collection,
            vectorizer_config=Configure.Vectorizer.none(),
            vector_index_config=Configure.VectorIndex.hnsw(distance_metric=VectorDistances.COSINE),
            properties=wc.COLLECTION_PROPERTIES)
    created = True
    print('PASS owned overlap collection created; mode=' + ('real embedding' if real_embedding else 'text storage without vectorization'),flush=True)
    with tempfile.TemporaryDirectory(prefix='overlap-live-') as directory:
        root = Path(directory)
        cases = [('long_token', 'x' * 10000, 200, 100),
                 ('single_newlines', '\n'.join('Inert source line ' + str(i) for i in range(600)), 200, 100),
                 ('paragraphs', '\n\n'.join('Inert paragraph ' + str(i) + ' abc' * 80 for i in range(20)), 200, 100),
                 ('short_tail', 'z' * 1020, 10, 100)]
        with patch.object(settings, 'sources_dir', str(root/'retained')):
            for label, text, overlap, minimum in cases:
                stage = root/label; stage.mkdir()
                source = stage/(label + '.txt'); source.write_text(text)
                parsed, _ = ingest_pipeline._parse_file(source)
                assert parsed.strip(), f'{label}: parser returned no nonblank text'
                expected = chunker.chunk_overlap(parsed,1000,overlap,minimum)
                job_id = 'overlap-' + uuid.uuid4().hex[:8]
                job = {'status':'queued', 'files_total':1, 'files_completed':0, 'files_failed':0,
                       'chunks_stored':0, 'errors':[]}
                ingest_pipeline._jobs[job_id] = job
                try:
                    ingest_pipeline._process_job_sync(job_id,[source],stage,collection,'overlap',1000,overlap,0.85,minimum)
                    assert job['status'] == 'completed' and job['files_failed'] == 0, job
                    objects = wc.get_client().collections.get(collection).iterator()
                    saved = sorted((o.properties for o in objects if o.properties['source_file'] == source.name),
                                   key=lambda p:p['chunk_index'])
                    chunks = [p['content'] for p in saved]
                    assert chunks and expected, f'{label}: no windows were stored'
                    assert chunks == expected and job['chunks_stored'] == len(chunks), (label,job)
                    restored = chunks[0] + ''.join(c[overlap:] for c in chunks[1:]) if chunks else ''
                    assert restored == parsed, label
                    assert all(len(c) <= 1000 for c in chunks[:-1])
                    assert all(len(c) <= 1000 + max(0,min(1000,minimum-1)-overlap) for c in chunks)
                    assert all(a[-overlap:] == b[:overlap] for a,b in zip(chunks,chunks[1:]))
                    print(f'PASS {label}: real parsed text stored as {len(chunks)} bounded windows with exact coverage/overlap', flush=True)
                finally:
                    ingest_pipeline._jobs.pop(job_id,None)
        # #621: a file whose overlap output would pass the per-file limits is
        # stored as parts, each inside them, with every character kept. 1000/900
        # stores just under ten times the text, inside the cap (PR #630).
        stage = root / 'budget'
        stage.mkdir()
        source = stage / 'over-budget.txt'
        text = '\n\n'.join(['x' * 998] * 1100)
        source.write_text(text)
        job_id='overlap-budget-'+uuid.uuid4().hex[:8]
        job={'status':'queued','files_total':1,'files_completed':0,'files_failed':0,'chunks_stored':0,'errors':[],'split':[]}
        ingest_pipeline._jobs[job_id]=job
        try:
            with patch.object(settings, 'sources_dir', str(root/'retained')):
                ingest_pipeline._process_job_sync(job_id,[source],stage,collection,'overlap',1000,900,0.85,100)
            assert job['status']=='completed' and job['files_completed']==1,job
            stored=[o.properties for o in wc.get_client().collections.get(collection).iterator()
                    if o.properties['source_file'].startswith(source.name)]
            names=sorted({p['source_file'] for p in stored})
            assert names==[f'{source.name} (part 1)',f'{source.name} (part 2)'],names
            assert job['chunks_stored']==len(stored),job
            assert job['split']==[f"{source.name}: split into 2 parts, stored as '{source.name} (part 1)' "
                                  f"to '{source.name} (part 2)'"],job
            # Each part restores exactly: its paragraphs, as the parser joins them
            # (one line break between), every character and separator in place,
            # and the parts together hold every paragraph once.
            paragraphs=0
            for name in names:
                windows=[p['content'] for p in sorted((p for p in stored if p['source_file']==name),
                                                      key=lambda p:p['chunk_index'])]
                assert len(windows)<=chunker.MAX_OVERLAP_WINDOWS,(name,len(windows))
                assert sum(map(len,windows))<=chunker.MAX_OVERLAP_OUTPUT_CHARACTERS,name
                restored=windows[0]+''.join(w[900:] for w in windows[1:])
                count=restored.count('\n')+1
                assert restored=='\n'.join(['x'*998]*count),(name,len(restored))
                paragraphs+=count
            assert paragraphs==1100,paragraphs
            print('PASS excessive overlap output is stored as parts inside the per-file limits',flush=True)
        finally:
            ingest_pipeline._jobs.pop(job_id, None)
        # PR #630: a split file whose chunks would store more than ten times its
        # size fails whole: 1000/999 stores about 1,000 times, and nothing stays.
        stage = root / 'capped'
        stage.mkdir()
        source = stage / 'capped.txt'
        source.write_text('x' * 20000)
        job_id='overlap-capped-'+uuid.uuid4().hex[:8]
        job={'status':'queued','files_total':1,'files_completed':0,'files_failed':0,'chunks_stored':0,'errors':[],'split':[]}
        ingest_pipeline._jobs[job_id]=job
        try:
            with patch.object(settings, 'sources_dir', str(root/'retained')):
                ingest_pipeline._process_job_sync(job_id,[source],stage,collection,'overlap',1000,999,0.85,100)
            assert job['status']=='failed' and job['chunks_stored']==0,job
            assert len(job['errors'])==1 and job['errors'][0].startswith('capped.txt: it would store ') \
                and 'more than 10 times its own size (20,000 characters)' in job['errors'][0],job
            assert not any(o.properties['source_file'].startswith('capped.txt')
                           for o in wc.get_client().collections.get(collection).iterator())
            print('PASS a split file storing over ten times its size fails whole, nothing stored',flush=True)
        finally:
            ingest_pipeline._jobs.pop(job_id, None)
    print('PASS owned temporary source paths removed', flush=True)
finally:
    if created:
        wc._delete_collection_sync(collection)
    wc.close_client()
assert not wc._collection_exists_sync(collection)
wc.close_client()
print('PASS owned overlap collection and its configuration removed', flush=True)
