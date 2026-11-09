"""Fetch official, pinned research assets. Never called by the web service.

OpenFace is non-commercial research software: retained license applies.
This setup command does not change any interview, score, or credential.
"""
from __future__ import annotations
import hashlib
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import shutil
import time
import zipfile
import requests

ROOT = Path(__file__).resolve().parent / 'models'
OF_TAG = 'OpenFace_2.2.0'
OF_NAME = 'OpenFace_2.2.0_win_x64.zip'
OF_SHA256 = '4201e96f3bc561a0e3a7fa50d2e12ce782b8a685746d63062572cd5296478dec'
ZOO_COMMIT = '48b10fa505aeb51fbd127fc60a5c660040e823b7'


def fetch(url, target, maximum, headers=None):
    with requests.get(url, timeout=(20, 120), stream=True, headers=headers) as response:
        response.raise_for_status()
        temporary = target.with_suffix(target.suffix + '.download')
        size = 0
        try:
            with temporary.open('wb') as stream:
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > maximum:
                        raise ValueError('Official asset exceeds expected size limit')
                    stream.write(chunk)
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    return hashlib.sha256(target.read_bytes()).hexdigest()


def github_text(repo, commit, path):
    response = requests.get(f'https://api.github.com/repos/{repo}/contents/{path}',
                            params={'ref': commit}, timeout=30)
    response.raise_for_status()
    return base64.b64decode(response.json()['content'])


def fetch_release_ranges(asset, target):
    """Bounded byte ranges avoid long idle connections on Windows networks."""
    response = requests.get(asset['url'], params={'download': 1, 't': int(time.time())},
        headers={'Accept': 'application/octet-stream', 'Range': 'bytes=0-0'}, timeout=(15, 45))
    response.raise_for_status()
    if response.status_code == 200 and len(response.content) == asset['size']:
        target.write_bytes(response.content)
        return _digest(target)
    if response.status_code != 206:
        raise ValueError('Release server does not support checked byte ranges')
    url = response.url
    block = 2 * 1024 * 1024
    ranges = [(begin, min(begin + block - 1, asset['size'] - 1)) for begin in range(0, asset['size'], block)]
    def get_range(bounds):
        begin, end = bounds
        for attempt in range(3):
            try:
                item = requests.get(url, headers={'Range': f'bytes={begin}-{end}'}, timeout=(15, 50))
                item.raise_for_status()
                if item.headers.get('Content-Range') != f'bytes {begin}-{end}/{asset["size"]}' or len(item.content) != end - begin + 1:
                    raise ValueError('Release byte range mismatch')
                return item.content
            except requests.RequestException:
                if attempt == 2:
                    raise
        raise ValueError('Release download failed')
    temporary = target.with_suffix('.download')
    with ThreadPoolExecutor(max_workers=6) as pool, temporary.open('wb') as stream:
        for content in pool.map(get_range, ranges):
            stream.write(content)
    temporary.replace(target)
    return _digest(target)


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def setup_openface():
    api = 'https://api.github.com/repos/TadasBaltrusaitis/OpenFace'
    response = requests.get(api + '/releases/tags/' + OF_TAG, timeout=30)
    response.raise_for_status()
    release = response.json()
    asset = next(item for item in release['assets'] if item['name'] == OF_NAME)
    archive = ROOT / OF_NAME
    if not archive.is_file() or archive.stat().st_size != asset['size']:
        digest = fetch_release_ranges(asset, archive)
    else:
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    if archive.stat().st_size != asset['size']:
        raise ValueError('OpenFace release size mismatch')
    if digest != OF_SHA256:
        raise ValueError('OpenFace release differs from the reviewed research asset')
    official_digest = asset.get('digest')
    if official_digest and official_digest != 'sha256:' + digest:
        raise ValueError('OpenFace release digest mismatch')
    target = ROOT / 'openface-2.2.0'
    target.mkdir(exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        for member in source.infolist():
            relative = Path(member.filename)
            # Official release has one top-level directory.
            if relative.parts and relative.parts[0] == 'OpenFace_2.2.0_win_x64':
                relative = Path(*relative.parts[1:])
            destination = target / relative
            if not destination.resolve().is_relative_to(target.resolve()):
                raise ValueError('Unsafe archive path')
            if member.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with source.open(member) as src, destination.open('wb') as dst:
                    shutil.copyfileobj(src, dst)
    files = {str(path.relative_to(target)).replace('\\', '/'): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(target.rglob('*')) if path.is_file()}
    record = {'model_id': 'OpenFace/2.2.0', 'source': release['html_url'],
              'download_url': asset['browser_download_url'], 'release_tag': OF_TAG,
              'archive_sha256': digest, 'archive_bytes': archive.stat().st_size,
              'official_digest_available': bool(official_digest), 'files': files,
              'license_scope': 'noncommercial internal research; see bundled Copyright.txt',
              'verification': 'official HTTPS release, exact release asset size, local SHA-256 manifest'}
    (ROOT / 'openface-2.2.0.source.json').write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'model_id': record['model_id'], 'bytes': archive.stat().st_size, 'sha256': digest, 'files': len(files)}), flush=True)


def setup_expression():
    api = 'https://api.github.com/repos/opencv/opencv_zoo'
    commit = ZOO_COMMIT
    if not re.fullmatch('[0-9a-f]{40}', commit):
        raise ValueError('Invalid source revision')
    name = 'facial_expression_recognition_mobilefacenet_2022july.onnx'
    directory = 'models/facial_expression_recognition/'
    raw = f'https://raw.githubusercontent.com/opencv/opencv_zoo/{commit}/'
    pointer = github_text('opencv/opencv_zoo', commit, directory + name).decode()
    digest = re.search(r'^oid sha256:([0-9a-f]{64})$', pointer, re.M).group(1)
    size = int(re.search(r'^size (\d+)$', pointer, re.M).group(1))
    url = f'https://media.githubusercontent.com/media/opencv/opencv_zoo/{commit}/{directory}{name}'
    target = ROOT / name
    actual = _digest(target) if target.is_file() else None
    if actual != digest:
        # A moving transport ref is still checked against the fixed commit LFS digest.
        actual = fetch(url.replace('/' + commit + '/', '/main/'), target, 6_000_000)
    if actual != digest or (ROOT / name).stat().st_size != size:
        raise ValueError('Expression model does not match official LFS pointer')
    # Preserve official inference implementation / provenance as inspectable sources.
    for source_name, target_name in [('LICENSE', name + '.LICENSE'),
                                     (directory + 'README.md', name + '.README.md'),
                                     (directory + 'facial_fer_model.py', name + '.upstream.py')]:
        (ROOT / target_name).write_bytes(github_text('opencv/opencv_zoo', commit, source_name))
    record = {'model_id': 'opencv-zoo/MobileFaceNet-ProgressiveTeacher/2022july',
              'source': 'https://github.com/opencv/opencv_zoo/tree/' + commit + '/' + directory,
              'source_commit': commit, 'download_url': url, 'sha256': digest, 'bytes': size,
              'license_source': raw + 'LICENSE', 'use': 'apparent expression class research only; observed_only'}
    (ROOT / (name + '.source.json')).write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(json.dumps({'model_id': record['model_id'], 'sha256': digest, 'bytes': size}), flush=True)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--module', choices=['all', 'openface', 'expression'], default='all')
    args = parser.parse_args()
    ROOT.mkdir(exist_ok=True)
    if args.module in {'all', 'openface'}:
        setup_openface()
    if args.module in {'all', 'expression'}:
        setup_expression()
