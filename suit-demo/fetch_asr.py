"""Explicit installation of pinned official Systran CTranslate2 Whisper base.

Inference only verifies these local assets; it never downloads models.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / 'models' / 'faster-whisper-base'
REPO = 'Systran/faster-whisper-base'
COMMIT = 'ebe41f70d5b6dfa9166e2c581c45c9c0cfc57b66'
FILES = {
    'README.md': {'bytes': 1991, 'git_blob': 'cddfec65ffb3e3df45d2234f09f3d261bf9a9c0a',
                  'sha256': 'b922acef3616e27b1d416260b63ba7114c732f0315012006781af2da69cd1ebf'},
    'config.json': {'bytes': 2309, 'git_blob': '867cf1a0fece1394e01d55e287ba2f09a577c046',
                    'sha256': '56a6d8110d311f19c8f0471e562832c7527f146b567275bfca59fcf7c184da9a'},
    'model.bin': {'bytes': 145217532, 'sha256': 'd01c3014881c9c6f3133c182f3d2887eb6ca1c789a7538c5c007196857a0a6a9'},
    'tokenizer.json': {'bytes': 2203239, 'git_blob': '7818adb6de9fa3064d3ff81226fdd675be1f6344',
                       'sha256': 'fb7b63191e9bb045082c79fd742a3106a12c99513ab30df4a0d47fa6cb6fd0ab'},
    'vocabulary.txt': {'bytes': 459861, 'git_blob': 'c9074644d9d1205686f16d411564729461324b75',
                       'sha256': '34ce3fe1c5041027b3f8d42912270993f986dbc4bb34cf27f951e34a1e453913'},
}


def verify_file(path, record):
    path = Path(path)
    if not path.is_file() or path.stat().st_size != record['bytes']:
        raise ValueError('asr_asset_missing_or_wrong_size')
    sha = hashlib.sha256()
    blob = hashlib.sha1(f"blob {record['bytes']}\0".encode())
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            sha.update(chunk)
            blob.update(chunk)
    if record.get('sha256') and sha.hexdigest() != record['sha256']:
        raise ValueError('asr_asset_checksum_mismatch')
    if record.get('git_blob') and blob.hexdigest() != record['git_blob']:
        raise ValueError('asr_asset_git_blob_mismatch')
    return sha.hexdigest()


def verify_assets(directory=ASSETS):
    return {name: verify_file(Path(directory) / name, record) for name, record in FILES.items()}


def fetch(directory=ASSETS):
    import requests
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    for name, record in FILES.items():
        target = directory / name
        try:
            verify_file(target, record)
            continue
        except ValueError:
            pass
        temporary = target.with_suffix(target.suffix + '.download')
        try:
            with requests.get(f'https://huggingface.co/{REPO}/resolve/{COMMIT}/{name}', stream=True, timeout=(15, 60)) as response:
                response.raise_for_status()
                size = 0
                with temporary.open('wb') as stream:
                    for chunk in response.iter_content(1024 * 1024):
                        size += len(chunk)
                        if size > record['bytes']:
                            raise ValueError('asr_download_size_exceeded')
                        stream.write(chunk)
            verify_file(temporary, record)
            temporary.replace(target)
            print(json.dumps({'asset': name, 'status': 'verified', 'bytes': size}), flush=True)
        finally:
            temporary.unlink(missing_ok=True)
    hashes = verify_assets(directory)
    manifest = {'repository': REPO, 'commit': COMMIT, 'source': f'https://huggingface.co/{REPO}/tree/{COMMIT}',
        'files_sha256': hashes, 'license': 'MIT (official model card)',
        'installed_at': datetime.now(timezone.utc).isoformat(),
        'conversion': 'official Systran FP16 CTranslate2 conversion; int8 selected at CPU runtime'}
    (directory / 'SOURCE.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    print(json.dumps({'status': 'verified', 'files_sha256': verify_assets()} if args.verify_only else fetch(), ensure_ascii=False))


if __name__ == '__main__':
    main()
