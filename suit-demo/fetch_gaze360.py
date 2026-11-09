"""Fetch pinned Gaze360 research assets, with explicit backup provenance.

The author endpoint currently returns HTTP 403. The selected public backup
contains a download script naming that endpoint, but the author publishes no
checksum against which its equivalence can be independently proved. We retain
that limitation instead of silently treating the backup as an official release.
No dataset, participant data or credentials are accessed.
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / 'models' / 'gaze360'
REPO = 'erkil1452/gaze360'
COMMIT = '546762ef1373dae13569afdfbe501a834040e8c8'
SOURCES = {
    'LICENSE.md': ('43a21e16a3e2dee656e5849b2011cd0034bffa6c', 6836),
    'README.md': ('15812fcabbbd74b83eb4a8de7e01eef1abb042ab', 1709),
    'code/README.md': ('29b749d16a2ed050cf5b9f50dd6a4d5993cd562b', 2202),
    'code/model.py': ('ab6c5a0612d6cbe3d3ffa0b4748cc9c6a3232ca6', 2077),
    'code/resnet.py': ('c2e64a4c64a96cfd2a33fc0f9d0bffd0466c0dbe', 9150),
    'code/data_loader.py': ('355dd809d33c98a647c5950077d78fc46b325fdb', 2982),
    'code/run.py': ('365689d80dd8e6631a5b5cd65da575021265757c', 8057),
    'dataset/README.md': ('908ac129c6bec836e5b260d2fb642a5e77944789', 10556),
}
OFFICIAL_WEIGHT_URL = 'https://gaze360.csail.mit.edu/files/gaze360_model.pth.tar'
BACKUP_REPO = 'm01ali/Classroom-Attention-Monitoring-Sys'
BACKUP_COMMIT = '66d66415ad8c4da88ebd9ba9c8477ac5983b2ec2'
WEIGHT_BLOB = 'aa079ec2bee6e8fc505d3484c56cdc1613fb8841'
WEIGHT_BYTES = 58370849
BACKUP_SCRIPT_BLOB = 'd14d25c830262c49134afd19947274cf9342f281'
WEIGHT_FILE = 'gaze360_model.pth.tar'


def git_blob(data):
    return hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest()


def _open(url):
    return urllib.request.urlopen(urllib.request.Request(url, headers={
        'User-Agent': 'zhenjian-research-gaze360-setup'}), timeout=30)


def _blob(repo, blob, size):
    with _open(f'https://api.github.com/repos/{repo}/git/blobs/{blob}') as response:
        value = json.loads(response.read(size * 2 + 8192))
    data = base64.b64decode(value['content'])
    if len(data) != size or git_blob(data) != blob:
        raise ValueError('gaze360_source_blob_mismatch')
    return data


def _download_weight(url):
    chunks, received = [], 0
    started = last_notice = time.monotonic()
    with _open(url) as response:
        while received <= WEIGHT_BYTES:
            chunk = response.read(min(256 * 1024, WEIGHT_BYTES + 1 - received))
            if not chunk:
                break
            chunks.append(chunk)
            received += len(chunk)
            now = time.monotonic()
            if now - started > 1800:
                raise TimeoutError('gaze360_download_time_limit')
            if now - last_notice >= 15:
                print(json.dumps({'asset': WEIGHT_FILE, 'received_bytes': received,
                                  'expected_bytes': WEIGHT_BYTES}), flush=True)
                last_notice = now
    data = b''.join(chunks)
    if len(data) != WEIGHT_BYTES or git_blob(data) != WEIGHT_BLOB:
        raise ValueError('gaze360_weight_does_not_match_pinned_backup')
    return data


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--official-only', action='store_true',
                        help='Do not use the explicitly documented community backup.')
    args = parser.parse_args()
    ASSETS.mkdir(parents=True, exist_ok=True)
    records = {}
    for relative, (blob, size) in SOURCES.items():
        path = ASSETS / relative
        existing = path.read_bytes() if path.is_file() else b''
        data = existing if len(existing) == size and git_blob(existing) == blob else _blob(REPO, blob, size)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        records[relative] = {'bytes': size, 'git_blob_sha1': blob, 'sha256': hashlib.sha256(data).hexdigest()}
    backup_script = _blob(BACKUP_REPO, BACKUP_SCRIPT_BLOB, 563)
    (ASSETS / 'backup-download-source.txt').write_bytes(backup_script)
    path = ASSETS / WEIGHT_FILE
    existing = path.read_bytes() if path.is_file() else b''
    attempts = []
    provenance = 'previously_verified_pinned_backup'
    if len(existing) == WEIGHT_BYTES and git_blob(existing) == WEIGHT_BLOB:
        data = existing
    else:
        try:
            data = _download_weight(OFFICIAL_WEIGHT_URL)
            provenance = 'official_endpoint_matching_pinned_backup'
            attempts.append({'url': OFFICIAL_WEIGHT_URL, 'status': 'download_verified'})
        except (urllib.error.URLError, TimeoutError, ValueError) as error:
            attempts.append({'url': OFFICIAL_WEIGHT_URL, 'status': 'unavailable',
                             'http_status': getattr(error, 'code', None), 'error_type': type(error).__name__})
            if args.official_only:
                raise
            url = f'https://raw.githubusercontent.com/{BACKUP_REPO}/{BACKUP_COMMIT}/models/{WEIGHT_FILE}'
            data = _download_weight(url)
            provenance = 'community_backup_with_documented_official_download_script'
            attempts.append({'url': url, 'status': 'download_verified'})
        path.write_bytes(data)
    records[WEIGHT_FILE] = {'bytes': len(data), 'git_blob_sha1': WEIGHT_BLOB,
                            'sha256': hashlib.sha256(data).hexdigest()}
    manifest = {'schema_version': 'gaze360-assets.v1', 'checked_at': datetime.now(timezone.utc).isoformat(),
        'source': f'https://github.com/{REPO}/tree/{COMMIT}', 'commit': COMMIT,
        'files': records, 'checkpoint_provenance': provenance, 'download_attempts': attempts,
        'backup_repository': BACKUP_REPO, 'backup_commit': BACKUP_COMMIT,
        'backup_download_script_git_blob': BACKUP_SCRIPT_BLOB,
        'official_weight_url': OFFICIAL_WEIGHT_URL,
        'origin_limitation': 'Backup repository download script names the official URL. No author checksum available; identical official origin is not independently verified.',
        'license': 'Official Gaze360 non-commercial research-only LICENSE.md. Do not redistribute downloaded code, weights or dataset in the application source package.',
        'runtime_loading': 'weights_only=True on CPU; full strict key/shape check; never run backup application code'}
    (ASSETS / 'assets.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'status': 'assets_verified', 'weight_bytes': len(data),
                      'checkpoint_sha256': records[WEIGHT_FILE]['sha256'],
                      'provenance': provenance}), flush=True)


if __name__ == '__main__':
    main()
