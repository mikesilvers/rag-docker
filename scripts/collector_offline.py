"""Offline collector identity validation; package metadata never selects an image.

The tracked pin/config-ID allow-list is the trust root. Reconstruct a minimal
Docker archive from hash-checked config and layers before loading: untrusted OCI
indexes, repository tags, and unrelated image records never reach docker load.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile

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


def sanitize(source, destination, architecture):
    try:
        return _sanitize(source, destination, architecture)
    except tarfile.TarError:
        raise ValueError("invalid collector TAR archive") from None


def _sanitize(source, destination, architecture):
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
        for name, digest in zip(image['Layers'], diff_ids):
            data = read_member(archive, name)
            if sha(data) != digest:
                raise ValueError('collector layer differs from trusted image config')
            layers.append(data)
    clean = {'Config': 'config.json', 'RepoTags': None,
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
    parser.add_argument('mode', choices=['package', 'prepare'])
    parser.add_argument('folder', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.mode == 'package':
        inspected = json.loads(subprocess.check_output(['docker', 'image', 'inspect', PIN['image']]))[0]
        arch = inspected['Architecture']
        if inspected['Id'] != PIN['config_ids'].get(arch) or inspected['Os'] != 'linux':
            raise ValueError('local collector does not match trusted platform pin')
        target = args.folder / 'collector.tar'
        subprocess.run(['docker', 'image', 'save', '-o', str(target), PIN['image']], check=True)
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
        print(sanitize(target, args.output, architecture))


if __name__ == '__main__':
    main()
