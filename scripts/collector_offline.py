"""Offline collector identity validation; package metadata never selects an image.

The tracked pin/config-ID allow-list is the trust root. Reconstruct a minimal
Docker archive from hash-checked config and layers before loading: untrusted OCI
indexes, repository tags, and unrelated image records never reach docker load.
"""
import argparse
import hashlib
import gzip
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
PIN = json.loads((ROOT / 'telemetry/image.json').read_text())
LIMIT = 512 * 1024 * 1024


def sha(data):
    return 'sha256:' + hashlib.sha256(data).hexdigest()


def read_member(archive, name, limit=LIMIT):
    if not isinstance(name, str) or not name:
        raise ValueError('invalid collector member reference')
    try:
        member = archive.getmember(name)
    except KeyError:
        raise ValueError('missing collector archive member') from None
    if not member.isfile() or member.size > limit:
        raise ValueError('collector archive member is not a bounded regular file')
    return archive.extractfile(member).read()


def sanitize(source, destination, architecture, *, tag=None):
    try:
        return _sanitize(source, destination, architecture, tag=tag)
    except tarfile.TarError:
        raise ValueError("invalid collector TAR archive") from None


def _sanitize(source, destination, architecture, *, tag=None):
    expected = PIN['config_ids'][architecture]
    if Path(source).stat().st_size > LIMIT:
        raise ValueError('collector archive is oversized')
    with tarfile.open(source, 'r:') as archive:
        entries, total = [], 0
        for member in archive:
            entries.append(member)
            total += member.size
            if len(entries) > 40 or total > LIMIT:
                raise ValueError('collector archive is oversized')
        # tarfile accepts EOF without end markers and ignores bytes after them.
        # Docker save produces an uncompressed TAR with two zero end blocks.
        with Path(source).open('rb') as raw:
            raw.seek(archive.offset)
            tail = raw.read()
        if len(tail) < 1024 or len(tail) % 512 or any(tail):
            raise ValueError('invalid collector archive terminator/trailing bytes')
        if len({e.name for e in entries}) != len(entries):
            raise ValueError('duplicate collector archive members')
        manifest = json.loads(read_member(archive, 'manifest.json', 65536))
        if not isinstance(manifest, list) or len(manifest) != 1:
            raise ValueError('expected exactly one collector image')
        image = manifest[0]
        if (not isinstance(image, dict) or not isinstance(image.get('Config'), str)
                or not image['Config'] or not isinstance(image.get('Layers'), list)
                or not all(isinstance(name, str) and name for name in image['Layers'])):
            raise ValueError('invalid collector image manifest')
        config = read_member(archive, image['Config'], 65536)
        if sha(config) != expected:
            raise ValueError('collector image differs from trusted platform pin')
        parsed = json.loads(config)
        if parsed['architecture'] != architecture or parsed['os'] != 'linux':
            raise ValueError('collector platform mismatch')
        diff_ids = parsed['rootfs']['diff_ids']
        if len(image['Layers']) != len(diff_ids):
            raise ValueError('collector layer count mismatch')
        # Verify all bytes before producing an archive Docker will see.
        layers = []
        expanded = 0
        for name, digest in zip(image['Layers'], diff_ids):
            data = read_member(archive, name)
            # containerd saves compressed OCI blobs; classic Docker saves TARs.
            # diff_ids authenticate uncompressed layer bytes in either format.
            if data.startswith(b'\x1f\x8b'):
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(data)) as compressed:
                        data = compressed.read(LIMIT - expanded + 1)
                except (OSError, EOFError):
                    raise ValueError('invalid compressed collector layer') from None
            expanded += len(data)
            if expanded > LIMIT:
                raise ValueError('expanded collector layers are oversized')
            if sha(data) != digest:
                raise ValueError('collector layer differs from trusted image config')
            layers.append(data)
    clean = {'Config': 'config.json', 'RepoTags': [tag] if tag else None,
             'Layers': [f'layer-{i}.tar' for i in range(len(layers))]}
    with tarfile.open(destination, 'w') as out:
        for name, data in [('config.json', config), ('manifest.json', json.dumps([clean]).encode()),
                           *zip(clean['Layers'], layers)]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            out.addfile(info, io.BytesIO(data))
    return expected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['package', 'prepare', 'install'])
    parser.add_argument('folder', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.mode == 'package':
        inspected = json.loads(subprocess.check_output(['docker', 'image', 'inspect', PIN['image']]))[0]
        arch = inspected['Architecture']
        if arch not in PIN['config_ids'] or inspected['Os'] != 'linux':
            raise ValueError('local collector does not match trusted platform pin')
        target = args.folder / 'collector.tar'
        # Local Id can be a config or index digest, depending on Docker's store.
        # Only the saved config/layer bytes establish the trusted identity.
        with tempfile.TemporaryDirectory() as stage:
            saved = Path(stage) / 'saved.tar'
            subprocess.run(['docker', 'image', 'save', '-o', str(saved), PIN['image']], check=True)
            sanitize(saved, target, arch)
        with target.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        metadata = {'architecture': arch, 'archive_sha256': digest}
        (args.folder / 'collector.json').write_text(json.dumps(metadata) + '\n')
    else:
        if args.output is None:
            parser.error('--output is required for prepare')
        architecture = subprocess.check_output(['docker', 'info', '--format', '{{.Architecture}}'], text=True).strip()
        architecture = {'aarch64': 'arm64', 'x86_64': 'amd64'}.get(architecture, architecture)
        if architecture not in PIN['config_ids']:
            raise ValueError('offline collector supports linux amd64/arm64 only')
        metadata_path = args.folder / 'collector.json'
        if metadata_path.stat().st_size > 4096:
            raise ValueError('collector metadata is oversized')
        metadata = json.loads(metadata_path.read_text())
        if not isinstance(metadata, dict):
            raise ValueError('invalid collector metadata object')
        target = args.folder / 'collector.tar'
        if target.stat().st_size > LIMIT:
            raise ValueError('collector archive is oversized')
        with target.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if metadata.get('architecture') != architecture or metadata.get('archive_sha256') != digest:
            raise ValueError('collector archive checksum/platform mismatch')
        if args.mode == 'prepare':
            print(sanitize(target, args.output, architecture))
        else:
            # A locally generated tag locates the loaded image on both stores.
            # Never retain a tag supplied by the offline archive.
            tag = 'rag-offline-collector:' + PIN['config_ids'][architecture].split(':')[1]
            sanitize(target, args.output, architecture, tag=tag)
            subprocess.run(['docker', 'load', '-i', str(args.output)], check=True, stdout=sys.stderr)
            identity = subprocess.check_output(
                ['docker', 'image', 'inspect', tag, '--format', '{{.Id}}'], text=True).strip()
            if not re.fullmatch(r'sha256:[0-9a-f]{64}', identity):
                raise ValueError('invalid loaded collector identity')
            # Check the exact immutable reference Compose will use, not the tag.
            with tempfile.TemporaryDirectory() as stage:
                saved = Path(stage) / 'loaded.tar'
                subprocess.run(['docker', 'image', 'save', '-o', str(saved), identity], check=True)
                sanitize(saved, Path(stage) / 'checked.tar', architecture)
            print(identity)


if __name__ == '__main__':
    main()
