"""Fetch fixed EMOTIC community-preserved checkpoints and audited sources.

The original Tandon Drive release is unavailable. These checkpoints are a
community snapshot, not a claimed byte-identical copy of that lost release.
Downloads are verified against Git object identities before being installed.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / 'models' / 'emotic'
TANDON_COMMIT = '862a1cad48256efeb32fb6ec66046d9a9bd51758'
MIRROR_COMMIT = 'bc9e70b6cd5583368e8a995b02dded2f4ae91c54'
DETECTOR_COMMIT = 'bb17b6c3eef36d80be441ae8e5339be66e8e3b7a'
# filename: (repository, fixed commit, repository path, Git blob SHA1, bytes)
PINNED = {
    'model_body1.pth': ('cdfmlr/murecom-verse-1', MIRROR_COMMIT, 'emopic/models/model_body1.pth', 'ffc6c38db82b454aa3835e56fd4bee496b00b72d', 44792327),
    'model_context1.pth': ('cdfmlr/murecom-verse-1', MIRROR_COMMIT, 'emopic/models/model_context1.pth', '500701b02a38d13647d5774633cb8b3a6cbceda0', 44792191),
    'model_emotic1.pth': ('cdfmlr/murecom-verse-1', MIRROR_COMMIT, 'emopic/models/model_emotic1.pth', '7351a8c11688bae804d19518430cb12921943e9d', 1088422),
    'val_thresholds.npy': ('cdfmlr/murecom-verse-1', MIRROR_COMMIT, 'emopic/results/val_thresholds.npy', '2841bcf42705b18926e5b2e8b4e1164233eafbd8', 232),
    'mirror-README.md': ('cdfmlr/murecom-verse-1', MIRROR_COMMIT, 'README.md', 'da261146be20f16c743b8d96e1deb52cd6ca60fd', 28985),
    'mirror-LICENSE': ('cdfmlr/murecom-verse-1', MIRROR_COMMIT, 'LICENSE', '8ef2a4ca4cba0ca524e4c26d3c28adeb5a8e1f9d', 8136),
    'upstream-emotic.py': ('Tandon-A/emotic', TANDON_COMMIT, 'emotic.py', '29e730f02b8b7486067506dffee27a4c399950e3', 1019),
    'upstream-inference.py': ('Tandon-A/emotic', TANDON_COMMIT, 'inference.py', 'b61d889432a55b1e9c7b7f7b432ff837998058a3', 6479),
    'upstream-main.py': ('Tandon-A/emotic', TANDON_COMMIT, 'main.py', '47f54bd559e8893608776cb1b271f6c6a870dcb4', 5777),
    'upstream-LICENSE': ('Tandon-A/emotic', TANDON_COMMIT, 'LICENSE', 'ab6cd4162511dea8b8f64ef2951b932a711f61d2', 1073),
    'person-deploy.prototxt': ('chuanqi305/MobileNet-SSD', DETECTOR_COMMIT, 'deploy.prototxt', '88021a94bca5c22753a23664d388d4922b2186bb', 44667),
    'person-mobilenet.caffemodel': ('chuanqi305/MobileNet-SSD', DETECTOR_COMMIT, 'mobilenet_iter_73000.caffemodel', '253e5012dd6a17666fd7ae16cfa14a6860280ff7', 23306119),
    'person-LICENSE': ('chuanqi305/MobileNet-SSD', DETECTOR_COMMIT, 'LICENSE', '2e1b91c92dfedb16126e6d376ace80cda4e2204a', 1067),
    'person-README.md': ('chuanqi305/MobileNet-SSD', DETECTOR_COMMIT, 'README.md', '3d6f513af5f1384db86f5e6cc17b6f6a6680ef78', 3242),
}


def verify(path, blob, size):
    path = Path(path)
    if not path.is_file() or path.stat().st_size != size:
        raise ValueError('emotic_asset_missing_or_size_mismatch')
    data = path.read_bytes()
    if hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest() != blob:
        raise ValueError('emotic_asset_hash_mismatch')
    return hashlib.sha256(data).hexdigest()


def download_one(item):
    filename, (repo, commit, relative, blob, size) = item
    target = ASSETS / filename
    if target.is_file():
        try:
            return filename, verify(target, blob, size)
        except ValueError:
            pass
    url = f'https://raw.githubusercontent.com/{repo}/{commit}/{relative}'
    temporary = target.with_suffix(target.suffix + '.download')
    with requests.get(url, stream=True, timeout=(15, 60)) as response:
        response.raise_for_status()
        total = 0
        with temporary.open('wb') as output:
            for data in response.iter_content(256 * 1024):
                total += len(data)
                if total > size:
                    raise ValueError('emotic_download_size_exceeded')
                output.write(data)
    digest = verify(temporary, blob, size)
    temporary.replace(target)
    print(json.dumps({'asset': filename, 'verified': True, 'bytes': size}), flush=True)
    return filename, digest


def main(check_only=False):
    ASSETS.mkdir(parents=True, exist_ok=True)
    if check_only:
        hashes = {name: verify(ASSETS / name, entry[3], entry[4]) for name, entry in PINNED.items()}
    else:
        with ThreadPoolExecutor(max_workers=3) as pool:
            hashes = dict(pool.map(download_one, PINNED.items()))
    record = {
        'schema_version': 'emotic-assets.v1', 'checked_at': datetime.now(timezone.utc).isoformat(),
        'implementation': 'Tandon-A/emotic third-party PyTorch implementation linked by rkosti/emotic',
        'implementation_commit': TANDON_COMMIT,
        'checkpoint_source': 'community-preserved Tandon-compatible checkpoint; original release identity cannot be independently compared',
        'checkpoint_repository': 'https://github.com/cdfmlr/murecom-verse-1',
        'checkpoint_commit': MIRROR_COMMIT,
        'original_release_status': 'Drive returned HTTP 404 on 2026-10-26; owner acknowledged deletion in issue 31',
        'owner_statement': 'https://github.com/Tandon-A/emotic/issues/31#issuecomment-2308547548',
        'official_lua_model': 'separate architecture; its public OneDrive landing page resolved but download API required authentication; not substituted',
        'output_allowlist': ['Anger', 'Happiness', 'Sadness', 'Surprise'],
        'excluded_outputs': 'other 22 categories and all continuous dimensions are not returned or persisted',
        'license_scope': 'Code license texts retained; EMOTIC dataset is non-commercial research/education; weight redistribution permission not inferred from mirror availability',
        'assets_sha256': hashes,
        'assets': {name: {'repository': row[0], 'commit': row[1], 'path': row[2], 'git_blob': row[3], 'size': row[4]} for name, row in PINNED.items()},
    }
    (ASSETS / 'source.json').write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'verified_assets': len(hashes), 'checkpoint_origin': 'community_snapshot', 'ready_for_loading_check': True}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    main(args.check_only)
