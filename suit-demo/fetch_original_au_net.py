"""Fetch a pinned, bounded official AU-Net inference set, never any dataset.

Small source files are retained for inspection. Git blob IDs from the pinned
official tree verify every download; SHA-256 hashes are recorded for runtime.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / 'models' / 'original-au-net'
REPO = 'jingyang2017/AU-Net'
COMMIT = 'd447837d1eb4631ff473ccb61f13aefeb23844cd'
FILES = ('LICENSE', 'README.md', 'models/FAN.py', 'models/aunet.py',
         'models/__init__.py', 'test.py', 'demo.ipynb', 'models/2dfan2.pth',
         'checkpoints/cross_model.pth')


def _get(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        'User-Agent': 'zhenjian-consented-research-asset-setup'}), timeout=45)


def _download(url, size, relative):
    """Bound the response and report progress for slow official downloads."""
    chunks, received = [], 0
    started = last_notice = time.monotonic()
    with _get(url) as response:
        while received <= size:
            chunk = response.read(min(128 * 1024, size + 1 - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            now = time.monotonic()
            if now - started > 1800:
                raise TimeoutError('Official asset download exceeded 30 minutes')
            if now - last_notice > 15:
                print(json.dumps({'asset': relative, 'received_bytes': received,
                                  'expected_bytes': size}), flush=True)
                last_notice = now
    return b''.join(chunks)


def main():
    with _get(f'https://api.github.com/repos/{REPO}/git/trees/{COMMIT}?recursive=1') as response:
        tree = json.load(response)
    items = {item['path']: item for item in tree['tree'] if item['type'] == 'blob'}
    total = sum(items[path]['size'] for path in FILES)
    if total > 65 * 1024 * 1024:
        raise ValueError('Pinned inference asset set unexpectedly exceeds 65 MiB')
    ASSETS.mkdir(parents=True, exist_ok=True)
    records = {}
    for relative in FILES:
        record = items[relative]
        destination = ASSETS / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        def verify(data):
            return len(data) == record['size'] and hashlib.sha1(
                f'blob {len(data)}\0'.encode() + data).hexdigest() == record['sha']
        data = destination.read_bytes() if destination.exists() else b''
        if not destination.is_file() or not verify(data):
            url = f'https://raw.githubusercontent.com/{REPO}/{COMMIT}/{relative}'
            data = _download(url, record['size'], relative)
            if not verify(data):
                raise ValueError('Official pinned Git blob does not match: ' + relative)
            destination.write_bytes(data)
        records[relative] = {'bytes': len(data), 'git_blob_sha1': record['sha'],
                             'sha256': hashlib.sha256(data).hexdigest()}
        print(json.dumps({'asset': relative, 'bytes': len(data), 'verified': True}), flush=True)
    manifest = {'schema_version': 'original-au-net-assets.v1', 'repository': REPO,
        'commit': COMMIT, 'source': f'https://github.com/{REPO}/tree/{COMMIT}',
        'files': records, 'total_bytes': total,
        'license': 'Official repository MIT; upstream components and research use documented separately',
        'selected_head': 'cross_model.pth: 15 AU binary-detection outputs from official demo',
        'labels': [1, 2, 4, 6, 7, 9, 10, 12, 14, 15, 17, 23, 24, 25, 26]}
    (ASSETS / 'assets.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'verified_bytes': total, 'no_dataset_downloaded': True}), flush=True)


if __name__ == '__main__':
    main()
