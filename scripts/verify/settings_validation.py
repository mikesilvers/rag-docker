"""Live HTTP settings acceptance on one disposable, uniquely named collection.

RAG_API=http://127.0.0.1:18080/api python3 scripts/verify/settings_validation.py
No query/model jobs are launched. Only this script's new collection is deleted.
"""
import json
import os
import uuid
import urllib.error
import urllib.request

api = os.environ.get('RAG_API', 'http://localhost:8080/api').rstrip('/')
collection = 'VfySettings' + uuid.uuid4().hex[:12]


def request(path, body=None, method=None, raw=None, content_type='application/json'):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(api + path, data=data, method=method,
                                 headers={'Content-Type': content_type})
    try:
        with urllib.request.urlopen(req, timeout=60) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as response:
        body = response.read()
        try:
            return response.code, json.loads(body)
        except ValueError:  # a server error page that isn't JSON
            return response.code, body.decode(errors='replace')[:200]


def expect(path, body, code):
    status, result = request(path, body)
    assert status == code, (path, status, result)
    return result


def names():
    status, result = request('/collections')
    assert status == 200, result
    return {entry['name'] for entry in result['collections']}


before = names()
assert collection not in before
expect('/collections', {'name': collection, 'index_type': 'invalid'}, 422)
assert names() == before
print('PASS invalid collection settings do not create a collection', flush=True)
created = False
try:
    expect('/collections', {'name': collection}, 201)
    created = True
    print('PASS omitted collection settings preserve valid defaults', flush=True)

    ingest = {'collection': collection, 'chunking_strategy': 'fixed', 'chunk_size': 150, 'min_chunk_size': 40}
    retrieval = {'collection': collection, 'retrieval_mode': 'hybrid', 'top_k': 50, 'alpha': 1, 'ef': 512, 'response_format': 'engineer'}
    expect('/ingest/config', ingest, 201)
    expect('/retrieval/config', retrieval, 201)
    _, saved_ingest = request('/ingest/config/' + collection)
    _, saved_retrieval = request('/retrieval/config/' + collection)
    assert saved_ingest['chunk_size'] == 150 and saved_ingest['chunk_overlap'] == 200
    assert saved_retrieval['top_k'] == 50 and saved_retrieval['ef'] == 512
    print('PASS valid saved settings round trip, including unused default overlap', flush=True)

    for route, good, bad in (('/ingest/config', ingest, {'chunking_strategy': 'invalid'}),
                             ('/ingest/config', ingest, {'chunk_size': 0}),
                             ('/retrieval/config', retrieval, {'top_k': 0}),
                             ('/retrieval/config', retrieval, {'alpha': 1.1})):
        expect(route, {**good, **bad}, 422)
    assert request('/ingest/config/' + collection)[1] == saved_ingest
    assert request('/retrieval/config/' + collection)[1] == saved_retrieval
    print('PASS rejected saved settings preserve prior configuration', flush=True)

    expect('/ingest/config', {'collection': collection, 'chunking_strategy': 'fixed',
                              'chunk_size': 60, 'min_chunk_size': 100}, 201)
    status, minimum = request('/ingest/config/' + collection)
    assert status == 200 and minimum['chunk_size'] == 60 and minimum['min_chunk_size'] == 100
    print('PASS fixed minimum above split target saves and round trips', flush=True)

    expect('/ingest/config', {'collection': collection, 'chunking_strategy':'fixed',
                              'chunk_size':50, 'min_chunk_size':0}, 201)
    status, smallest = request('/ingest/config/' + collection)
    assert status == 200 and smallest['chunk_size'] == 50 and smallest['min_chunk_size'] == 0
    for bad in ({'chunk_size':49}, {'chunk_size':6001}, {'min_chunk_size':6001}):
        expect('/ingest/config', {'collection':collection,'chunking_strategy':'fixed','chunk_size':50, **bad},422)
    assert request('/ingest/config/' + collection)[1] == smallest
    expect('/ingest/config', {'collection': collection, 'chunking_strategy':'fixed',
                              'chunk_size':6000, 'min_chunk_size':6000}, 201)
    status, largest = request('/ingest/config/' + collection)
    assert status == 200 and largest['chunk_size'] == 6000 and largest['min_chunk_size'] == 6000
    print('PASS chunk bounds (50-6000, minimum 0-6000) accept both edges, reject their neighbours and preserve settings',flush=True)

    for path, bad in (('/query', {'question': 'inert', 'retrieval_mode': 'invalid'}),
                      ('/query', {'question': 'inert', 'response_format': 'invalid'}),
                      ('/query', {'question': 'inert', 'top_k': True}),
                      ('/tune/rechunk', {'chunk_overlap': 1000}),
                      ('/tune/reembed', {'chunk_size': 0}),
                      ('/tune/reindex', {'distance_metric': 'invalid'})):
        expect(path, {'collection': collection, **bad}, 422)
    print('PASS invalid query and tuning settings return 422', flush=True)

    raw = ('{"collection":"' + collection + '","question":"inert","alpha":NaN}').encode()
    assert request('/query', raw=raw)[0] == 422
    print('PASS non-finite input has a serializable 422 response', flush=True)

    boundary = 'ReviewSettingsBoundary'
    form = (f'--{boundary}\r\nContent-Disposition: form-data; name="collection"\r\n\r\n{collection}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="chunk_size"\r\n\r\n0\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="files"; filename="inert.txt"\r\n'
            f'Content-Type: text/plain\r\n\r\nInert review text.\r\n--{boundary}--\r\n').encode()
    assert request('/ingest/upload', raw=form, content_type='multipart/form-data; boundary=' + boundary)[0] == 422
    status, current = request('/collections')
    assert status == 200
    assert next(c for c in current['collections'] if c['name'] == collection)['object_count'] == 0
    print('PASS invalid multipart settings leave the collection empty', flush=True)

    # #141: concurrent saves through the live routes. Every save must be
    # acknowledged, and the persisted value must be one acknowledged response,
    # complete and unmixed (no request publishes another request's temp file).
    from concurrent.futures import ThreadPoolExecutor
    rounds, width = 15, 12
    for route, make in (('/ingest/config', lambda r, i: {'collection': collection, 'chunking_strategy': 'fixed',
                                                         'chunk_size': 100 + 20 * r + i, 'min_chunk_size': i}),
                        ('/retrieval/config', lambda r, i: {'collection': collection, 'retrieval_mode': 'hybrid',
                                                            'top_k': i + 1, 'alpha': r / 20, 'ef': 16 + 16 * i,
                                                            'response_format': 'engineer'})):
        for r in range(rounds):
            bodies = [make(r, i) for i in range(width)]
            with ThreadPoolExecutor(max_workers=width) as pool:
                results = list(pool.map(lambda body: request(route, body), bodies))
            failed = [(status, result) for status, result in results if status != 201]
            assert not failed, (route, r, failed[:3])
            acknowledged = [result for _, result in results]
            status, persisted = request(route + '/' + collection)
            assert status == 200, persisted
            assert persisted in acknowledged, (route, r, persisted)
    print('PASS concurrent saves are all acknowledged and publish one complete acknowledged value (ingest and retrieval)', flush=True)
finally:
    if created:
        status, result = request('/collections/' + collection + '?confirm=true', method='DELETE')
        assert status == 200, result
assert collection not in names()
print('PASS owned disposable collection removed', flush=True)
