"""Destructive fault acceptance for a disposable stack, using only Vfy names.

Run prepare in the API container, restart the API, then run check and cleanup.
No production fault switches are added to the server.
"""
import argparse
import asyncio
import hashlib
import json
import re
import sys
import tarfile
import uuid
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, '/app')
from config import settings
from services import batch_write, collection_recovery as recovery, goldstandard, importer, ollama_client, packager, sources, tuning, weaviate_client as wc


def require(condition, message):
    if not condition:
        raise AssertionError(message)
    print('PASS ' + message)


def write_import_package(package, name, rows, dimensions):
    """Write the replace-import fixture into an existing directory; return its manifest."""
    (package / 'chunks.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
    (package / 'collection.json').write_text('{}')
    source_dir = package / 'sources'
    source_dir.mkdir()
    # Retained sources as an export ships them: digest-named blobs and their index.
    blob = b'synthetic imported source'
    digest = hashlib.sha256(blob).hexdigest()
    (source_dir / digest).write_bytes(blob)
    seen = '2026-09-27T00:00:00Z'
    (source_dir / sources.INDEX_NAME).write_text(json.dumps({'version': sources.INDEX_VERSION, 'documents': {
        digest: {'filenames': ['synthetic.txt'], 'size': len(blob), 'media_type': 'text/plain',
                 'first_seen': seen, 'last_seen': seen}}}, indent=2, sort_keys=True))
    manifest = {'package_format': 1, 'collection': {'name': name, 'chunk_count': len(rows)},
                'embedding': {'model': settings.embed_model, 'dimensions': dimensions},
                'fidelity': 'with-sources', 'files': {str(p.relative_to(package)): 'sha256:' + packager.sha256_file(p)
                    for p in package.rglob('*') if p.is_file()}}
    (package / 'manifest.json').write_text(json.dumps(manifest))
    return manifest


def prepare(prefix, state_path):
    require(not state_path.exists(), 'no previous acceptance state is overwritten')
    client = wc.get_client()
    require(not any(name.startswith(prefix) for name in client.collections.list_all()), 'disposable names are unused')
    vector = asyncio.run(ollama_client.embed('synthetic recovery acceptance text'))
    rows = [dict(id=str(uuid.uuid4()), properties={'content': f'synthetic chunk {i}',
            'source_file': 'synthetic.txt', 'chunk_index': i, 'created_at': '2026-09-27T00:00:00Z'}, vector=vector)
            for i in range(2)]
    state = {'prefix': prefix, 'recoveries': [], 'names': [], 'archives': []}
    state_path.write_text(json.dumps(state))

    def save():
        state_path.write_text(json.dumps(state, indent=2, default=lambda value: value.isoformat()))

    def collection(suffix):
        name = prefix + suffix
        state['names'].append(name)
        save()
        wc._create_collection_sync(name, 'hnsw', 'cosine', {})
        batch_write.insert(client.collections.get(name), rows)
        return name

    rejected = collection('Reject')
    col = client.collections.get(rejected)
    invalid = [{**row, 'id': str(uuid.uuid4()), 'properties': {**row['properties'], 'chunk_index': 'not an integer'}} for row in rows]
    invalid[1]['properties'] = dict(rows[1]['properties'])
    try:
        batch_write.insert(col, invalid, exact=False)
    except RuntimeError:
        require(bool(col.batch.failed_objects), 'real completed batch rejection is reported')
        stored_ids = {str(obj.uuid) for obj in col.iterator()}
        require(invalid[0]['id'] not in stored_ids and invalid[1]['id'] in stored_ids,
                'real partial acceptance still fails the batch')
    else:
        raise AssertionError('real invalid batch reported success')

    before_attempt = list(packager.read_chunks(rejected))
    original_fetch = col.query.fetch_objects
    def fail_verification_read(*args, **kwargs):
        if kwargs.get('include_vector'):
            raise OSError('controlled post-write read fault')
        return original_fetch(*args, **kwargs)
    with patch.object(col.query, 'fetch_objects', side_effect=fail_verification_read):
        try:
            batch_write.insert(col, lambda: ({'properties': row['properties']} for row in rows),
                               exact=False, cleanup_owned=True)
        except OSError:
            pass
        else:
            raise AssertionError('post-write read fault reported success')
    require(batch_write.verify(col, before_attempt, exact=True) == len(before_attempt),
            'failed ingestion removes only its generated UUIDs and preserves prior records')

    def remember(name, detail):
        record_path = next(p for p in recovery._root().glob('*.json')
                           if json.loads(p.read_text()).get('staging') == detail['recovered_as'])
        record = json.loads(record_path.read_text())
        expected = list(packager.read_chunks(record['staging']))
        require(len(expected) == 2, name + ' retains every verified record')
        state['recoveries'].append({'record': record, 'rows': expected})
        save()

    tune_name = collection('Tune')
    sources.store(tune_name, 'synthetic.txt', b'synthetic retained source')
    real_create = wc._create_collection_sync
    def fail_create(name, *args, **kwargs):
        if name == tune_name:
            raise RuntimeError('controlled final-create fault')
        return real_create(name, *args, **kwargs)
    with patch.object(wc, '_create_collection_sync', side_effect=fail_create):
        try:
            tuning._rebuild(tune_name, [row['properties'] for row in rows], None, None, None)
        except packager.PackageError as exc:
            remember('tuning', exc.detail)
        else:
            raise AssertionError('tuning final-create fault reported success')

    import_name = collection('Import')
    package = Path(settings.upload_dir) / (prefix + '-package')
    package.mkdir()
    write_import_package(package, import_name, rows, len(vector))
    archive = packager.exports_dir() / (prefix + '-fixture.tar.gz')
    state['archives'].append(str(archive))
    save()
    with tarfile.open(archive, 'w:gz') as tar:
        tar.add(package, arcname='package')
    def fail_import_create(name, *args, **kwargs):
        if name == import_name:
            raise RuntimeError('controlled final-create fault')
        return real_create(name, *args, **kwargs)
    importer._jobs['live-acceptance'] = {'chunks_written': 0}
    with patch.object(wc, '_create_collection_sync', side_effect=fail_import_create):
        importer._run('live-acceptance', archive.name, 'replace')
    job = importer._jobs['live-acceptance']
    require(job['status'] == 'failed' and job['chunks_written'] == 0, 'replace failure reports zero confirmed target writes')
    remember('replace import', job['error_detail'])

    cleanup_record = recovery.begin(prefix + 'Cleanup', 'tune', client)
    wc._create_collection_sync(cleanup_record['staging'], 'hnsw', 'cosine', {})
    recovery.retain(cleanup_record)
    metadata = recovery._root() / cleanup_record['operation_id']
    actual_rmtree = recovery.shutil.rmtree
    def fail_metadata_cleanup(path, *args, **kwargs):
        if Path(path) == metadata:
            raise OSError('controlled metadata cleanup fault')
        return actual_rmtree(path, *args, **kwargs)
    with patch.object(recovery.shutil, 'rmtree', side_effect=fail_metadata_cleanup):
        try:
            recovery.discard(cleanup_record, client)
        except OSError:
            pass
        else:
            raise AssertionError('cleanup fault was not observed')
    require(cleanup_record['state'] == 'cleanup' and not client.collections.exists(cleanup_record['staging']),
            'cleanup intent survives filesystem failure after backend deletion')
    state['cleanup_record'] = cleanup_record
    save()

    unrelated = collection('__tuning_user_data')
    state['unrelated'] = unrelated
    scratch = recovery.begin(prefix + 'Scratch', 'tune', client)
    wc._create_collection_sync(scratch['staging'], 'hnsw', 'cosine', {})
    state['scratch'] = scratch['staging']
    save()
    print('READY restart the API before check')


def check(state):
    client = wc.get_client()
    for entry in state['recoveries']:
        record = entry['record']
        require(client.collections.exists(record['staging']), 'recovery survives API restart: ' + record['operation'])
        require(batch_write.verify(client.collections.get(record['staging']), entry['rows'], exact=True) == 2,
                'recovery UUIDs, properties and vectors match: ' + record['operation'])
        source_dir = sources.collection_dir(record['staging'])
        require(source_dir.is_dir() and any(source_dir.iterdir()), 'recovery sources survive: ' + record['operation'])
        require((recovery._root() / (record['operation_id'] + '.json')).is_file(), 'recovery ownership survives: ' + record['operation'])
    require(client.collections.exists(state['unrelated']), 'unowned marker-like collection survives restart')
    require(not client.collections.exists(state['scratch']), 'positively owned scratch is removed at startup')
    cleanup_record = state['cleanup_record']
    require(not (recovery._root() / (cleanup_record['operation_id'] + '.json')).exists(),
            'startup finishes interrupted recovery journal cleanup')
    require(not (recovery._root() / cleanup_record['operation_id']).exists(),
            'startup removes remaining cleanup metadata')


def cleanup(state, state_path):
    client = wc.get_client()
    for entry in state['recoveries']:
        recovery.discard(entry['record'], client)
    for name in state['names'] + [state.get('scratch', '')]:
        if name and client.collections.exists(name):
            wc._delete_collection_sync(name)
        elif name:
            sources.delete(name)
            wc.ingest_config.delete(name)
            wc.retrieval_config.delete(name)
    for archive in state['archives']:
        Path(archive).unlink(missing_ok=True)
    import shutil
    shutil.rmtree(Path(settings.upload_dir) / (state['prefix'] + '-package'), ignore_errors=True)
    state_path.unlink()
    print('PASS disposable recovery fixtures removed')


if __name__ == '__main__':
    args = argparse.ArgumentParser()
    args.add_argument('phase', choices=('prepare', 'check', 'cleanup'))
    args.add_argument('--prefix', default='VfyBatchRecovery')
    options = args.parse_args()
    if not re.fullmatch(r'Vfy[A-Za-z0-9_]+', options.prefix):
        args.error('prefix must be a safe Vfy collection prefix')
    state_path = Path(settings.upload_dir) / (options.prefix + '-acceptance.json')
    try:
        if options.phase == 'prepare':
            prepare(options.prefix, state_path)
        elif options.phase == 'check':
            check(json.loads(state_path.read_text()))
        else:
            cleanup(json.loads(state_path.read_text()), state_path)
    finally:
        wc.close_client()
