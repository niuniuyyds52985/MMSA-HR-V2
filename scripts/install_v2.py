"""Explicit Windows V2 setup. Plan/doctor are read-only; never starts the app.

No credentials, resumes, recordings or databases are read by this installer.
All writes stay in the selected project. Package code is obtained from pinned
PyPI versions; large binary/model assets have independently pinned SHA-256s.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import urllib.request
import uuid
import zipfile

PROJECT = Path(__file__).resolve().parents[1]


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def inside(root, relative):
    """Reject Windows escapes even when tested on another OS."""
    value = str(relative).replace('\\', '/')
    path = PurePosixPath(value)
    if path.is_absolute() or ':' in value or '..' in path.parts:
        raise ValueError('Unsafe project-relative path: ' + value)
    target = (root / Path(*path.parts)).resolve()
    if not target.is_relative_to(root.resolve()):
        raise ValueError('Path escapes project')
    return target


def load_manifest(root):
    record = json.loads((root / 'config/v2-install-assets.json').read_text(encoding='utf-8'))
    for item in record['assets']:
        if not item['url'].startswith('https://') or not re.fullmatch('[a-f0-9]{64}', item['sha256']):
            raise ValueError('Invalid pinned asset')
        for name in ('target', 'extract_to', 'source_target'):
            if item.get(name):
                inside(root, item[name])
    return record


def valid_file(path, expected):
    return path.is_file() and digest(path) == expected


def download_verified(root, asset, opener=None):
    target = inside(root, asset['target'])
    if valid_file(target, asset['sha256']):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.download')
    opener = opener or urllib.request.build_opener()
    req = urllib.request.Request(asset['url'], headers={'User-Agent': 'MMSA-HR-V2-local-installer'})
    sha, count = hashlib.sha256(), 0
    try:
        with opener.open(req, timeout=120) as response, temporary.open('wb') as output:
            if not response.geturl().startswith('https://'):
                raise ValueError('Asset redirected away from HTTPS')
            while block := response.read(1024 * 1024):
                count += len(block)
                if count > asset['max_bytes']:
                    raise ValueError('Asset exceeded pinned size limit: ' + asset['id'])
                output.write(block)
                sha.update(block)
        if sha.hexdigest() != asset['sha256']:
            raise ValueError('Asset checksum mismatch: ' + asset['id'])
        temporary.replace(target)
        return target
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def verify_extracted(root, asset):
    source = inside(root, asset['source_target'])
    try:
        recorded = json.loads(source.read_text(encoding='utf-8'))
        expected = asset.get('source_record', {}).get('files') or recorded['files']
        if recorded.get('archive_sha256') != asset['sha256'] or not expected:
            return False
        folder = inside(root, asset['extract_to'])
        return all(valid_file(inside(folder, name), sha) for name, sha in expected.items())
    except (OSError, KeyError, ValueError):
        return False


def extract_verified(root, asset, archive):
    folder = inside(root, asset['extract_to'])
    expected = asset.get('source_record', {}).get('files', {})
    files = {}
    with zipfile.ZipFile(archive) as zipped:
        for member in zipped.infolist():
            name = member.filename.replace('\\', '/')
            parts = PurePosixPath(name).parts
            if not parts or parts[0] != asset['strip_prefix']:
                raise ValueError('Unexpected archive root')
            relative = '/'.join(parts[1:])
            if not relative or member.is_dir():
                continue
            # Links are never followed. A ZIP slip or Windows drive name is rejected.
            if (member.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError('Archive contains a symlink')
            destination = inside(folder, relative)
            if expected and relative not in expected:
                raise ValueError('Unexpected archive member')
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(destination.name + '.installing')
            try:
                with zipped.open(member) as stream, temporary.open('wb') as output:
                    shutil.copyfileobj(stream, output)
                sha = digest(temporary)
                if expected and expected[relative] != sha:
                    raise ValueError('Extracted asset checksum mismatch')
                temporary.replace(destination)
                files[relative] = sha
            finally:
                temporary.unlink(missing_ok=True)
    if expected and files != expected:
        raise ValueError('Archive missing required files')
    source = {**asset.get('source_record', {}), 'archive_sha256': asset['sha256'], 'files': files}
    write_json(inside(root, asset['source_target']), source)


def install_assets(root, manifest):
    for item in manifest['assets']:
        print('Checking asset: ' + item['id'], flush=True)
        if item.get('extract_to') and verify_extracted(root, item):
            continue
        path = download_verified(root, item)
        if item.get('extract_to'):
            extract_verified(root, item, path)
        elif item.get('source_target'):
            write_json(inside(root, item['source_target']), item['source_record'])


def venv_python(root):
    return root / '.venv-suit/Scripts/python.exe'


def run(args, root, timeout=None, capture=False):
    options = dict(cwd=root, timeout=timeout, check=True,
                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if capture:
        options.update(capture_output=True, text=True, encoding='utf-8', errors='replace')
    return subprocess.run([str(x) for x in args], **options)


def runtime_probe(root, python):
    probe = "import sys,struct,json; print(json.dumps({'version':list(sys.version_info[:2]),'bits':struct.calcsize('P')*8}))"
    result = run([python, '-B', '-X', 'utf8', '-c', probe], root, timeout=30, capture=True)
    info = json.loads(result.stdout)
    if info != {'version': [3, 12], 'bits': 64}:
        raise RuntimeError('Python 3.12 x64 is required; current environment was not replaced.')


def dependency_probe(root, python, manifest):
    # Import success detects missing DLLs without loading model weights or data.
    names = ['fastapi', 'uvicorn', 'multipart', 'requests', 'numpy', 'cv2', 'PIL', 'whisper',
             'librosa', 'soundfile', 'scipy', 'webrtcvad', 'faster_whisper', 'ctranslate2',
             'av', 'onnxruntime', 'yaml', 'reportlab', 'pypdf', 'torch']
    code = ('import sys,importlib; ' +
            '; '.join('importlib.import_module(' + repr(name) + ')' for name in names) +
            '; sys.path.insert(0,' + repr(str(root / '.runtime/emotion2vec')) + ')' +
            '; import omegaconf,antlr4; import torch; assert torch.__version__==' + repr(manifest['torch']))
    run([python, '-B', '-X', 'utf8', '-c', code], root, timeout=180, capture=True)


def probe_openface(root):
    folder = root / 'suit-demo/models/openface-2.2.0'
    missing = 'install-probe-' + uuid.uuid4().hex + '.avi'
    if (folder / missing).exists():
        raise RuntimeError('Unexpected probe file')
    # This release ignores -help and attempts its default CEN model, which is
    # deliberately not used by our app. Use the production CLNF model and an
    # explicitly absent input, never the webcam or an interview recording.
    result = run([folder / 'FeatureExtraction.exe', '-mloc', 'model/main_clnf_general.txt',
                  '-f', missing, '-q'], folder, timeout=30, capture=True)
    if 'Reading the AU predictors' not in result.stdout or 'Failed to open the video file' not in result.stdout:
        raise RuntimeError('OpenFace CLNF/AU startup did not complete')


def install_dependencies(root, manifest):
    python = venv_python(root)
    if not python.is_file():
        run([sys.executable, '-m', 'venv', root / '.venv-suit'], root)
    runtime_probe(root, python)
    print('Installing pinned CPU packages (first run can take several minutes).', flush=True)
    run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--index-url', manifest['torch_index'],
         'torch==' + manifest['torch']], root)
    run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--index-url', manifest['pypi_index'],
         '-r', root / 'suit-demo/requirements-v2.txt'], root)
    deps = root / '.runtime/emotion2vec'
    # OmegaConf is intentionally isolated from any legacy environment requirements.
    helpers = "import sys,importlib.metadata as m; sys.path.insert(0," + repr(str(deps)) + "); import omegaconf,antlr4; "
    helpers += "d={x.metadata['Name'].lower().replace('_','-'):x.version for x in m.distributions(path=[" + repr(str(deps)) + "])}; "
    helpers += "assert d.get('omegaconf')=='2.4.0' and d.get('antlr4-python3-runtime')=='4.13.2'"
    try:
        run([python, '-B', '-X', 'utf8', '-c', helpers], root, timeout=30, capture=True)
    except (subprocess.SubprocessError, OSError):
        run([python, '-m', 'pip', 'install', '--disable-pip-version-check', '--index-url', manifest['pypi_index'],
             '--target', deps, '--no-deps', '--upgrade', 'omegaconf==2.4.0', 'antlr4-python3-runtime==4.13.2'], root)
    dependency_probe(root, python, manifest)


def doctor(root, manifest):
    checks = []
    def check(name, action):
        try:
            action()
            checks.append({'name': name, 'status': 'ok'})
        except Exception as error:
            # Do not echo commands, subprocess output, credentials or environment values.
            checks.append({'name': name, 'status': 'missing_or_failed', 'error_type': type(error).__name__})
    def assert_valid(condition):
        if not condition:
            raise ValueError('Missing or mismatched asset')
    python = venv_python(root)
    check('Python 3.12 x64', lambda: runtime_probe(root, python))
    check('Python imports and CPU Torch', lambda: dependency_probe(root, python, manifest))
    for asset in manifest['assets']:
        check(asset['id'], lambda a=asset: assert_valid(verify_extracted(root, a) if a.get('extract_to')
              else valid_file(inside(root, a['target']), a['sha256'])))
    for name in ('ffmpeg', 'ffprobe'):
        check(name + ' executable', lambda n=name: run([root / '.runtime/ffmpeg/bin' / (n + '.exe'), '-version'],
                                                    root, timeout=30, capture=True))
    check('OpenFace CLNF/AU startup / VC++ runtime', lambda: probe_openface(root))
    result = {'schema': 1, 'checked_at': datetime.now(timezone.utc).isoformat(),
              'status': 'ready' if all(c['status'] == 'ok' for c in checks) else 'incomplete', 'checks': checks,
              'not_checked': ['Bailian credentials/network/billing', 'Qwen host installation',
                              'real interview inference', 'another Windows computer'],
              'next_steps': ['Configure your own Bailian API key locally.',
                             'Import delivery/qwenwork-v2 Skill and MCP JSON in Qwen Work.',
                             'If OpenFace cannot start, install Microsoft Visual C++ 2015-2022 x64 Redistributable.']}
    return result


def plan(root, manifest):
    return {'mode': 'plan', 'platform': manifest['platform'], 'python_prerequisite': manifest['python'],
            'writes_only_to': ['.venv-suit', '.runtime', 'suit-demo/models', 'delivery/qwenwork-v2'],
            'steps': ['Check Python 3.12 x64', 'Create/reuse project virtual environment',
                      'Install pinned CPU dependencies', 'Download/check model and tool SHA-256',
                      'Generate machine-specific Qwen import files', 'Run read-only readiness checks'],
            'assets': [{'id': a['id'], 'url': a['url'], 'sha256': a['sha256']} for a in manifest['assets']],
            'does_not': ['start service', 'read/write credentials', 'read/write interview data',
                         'modify system PATH', 'install into Qwen host']}


def prepare_optional_qwen(root):
    """Connector import files are optional; their failure cannot block local HR."""
    try:
        run([venv_python(root), '-X', 'utf8', root / 'scripts/prepare_qwenwork_v2.py', '--project-root', root], root)
        return {'status': 'ready'}
    except Exception as error:
        return {'status': 'failed', 'error_type': type(error).__name__,
                'retry': 'Run scripts/prepare_qwenwork_v2.py later to regenerate optional Qwen import files.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, default=PROJECT)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plan', action='store_true')
    modes.add_argument('--doctor', action='store_true')
    parser.add_argument('--report', type=Path, help='Optional report path inside this project')
    args = parser.parse_args(argv)
    root = args.project_root.resolve()
    manifest = load_manifest(root)
    if args.plan:
        print(json.dumps(plan(root, manifest), ensure_ascii=False, indent=2))
        return 0
    if args.report:
        report = args.report.resolve()
        if not report.is_relative_to(root):
            parser.error('Report must stay inside the project')
    else:
        report = None
    if args.doctor:
        result = doctor(root, manifest)
    else:
        if os.name != 'nt' or sys.version_info[:2] != (3, 12) or struct.calcsize('P') != 8:
            parser.error('Install requires Windows x64 and Python 3.12 x64. See https://www.python.org/downloads/windows/')
        report = report or root / '.runtime/install-v2-report.json'
        try:
            install_dependencies(root, manifest)
            install_assets(root, manifest)
            qwen_preparation = prepare_optional_qwen(root)
            result = doctor(root, manifest)
            result['qwen_import_preparation'] = qwen_preparation
        except Exception as error:
            result = {'status': 'failed', 'error_type': type(error).__name__,
                      'next_steps': ['Fix the reported installation step, then rerun. Existing data is preserved.']}
            write_json(report, result)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            # Details for installation errors contain public asset paths, never user data.
            print(str(error), file=sys.stderr)
            return 1
    if report:
        write_json(report, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'ready' else 1


if __name__ == '__main__':
    raise SystemExit(main())
