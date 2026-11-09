"""Source packaging, read-only diagnosis, and non-overwriting data migration.

Source bundles never contain runtime data or credentials. Private data backups
are a separate, explicit command. Restore only creates a new directory and
rotates session capabilities; it cannot overwrite an existing workbench.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import hmac
from importlib import metadata
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
from urllib.request import urlopen
import uuid
import zipfile

ROOT = Path(__file__).resolve().parent
WORKSPACE = ROOT.parent
MAX_BACKUP_BYTES = 20 * 1024 ** 3


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def stamp():
    return datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:6]


def source_files(workspace=WORKSPACE):
    workspace = Path(workspace).resolve()
    app = workspace / 'suit-demo'
    files = set()
    for pattern in ('interview_*.py', 'fetch_*model*.py', 'fetch_original_au_net.py', 'check_original_au_net.py',
                    'fetch_gaze360.py', 'check_gaze360.py', 'fetch_asr.py', 'check_asr.py', 'validate_observation_accuracy.py', 'fetch_emotic.py', 'check_emotic.py', 'check_model_integration.py',
                    'validate_interview.py', 'check_interview_*.py', 'check_expression_report.py', 'check_delivery_*.py',
                    'delivery_tools.py', 'check_mcp_live.py', 'check_product_upgrade.py', 'check_upgrade_contracts_live.py', 'check_multimodal_quality.py', 'setup-delivery.ps1', 'start-interview.ps1', 'stop-interview.ps1',
                    'update-interview.py', 'update-interview.ps1',
                    'configure-interview.ps1', 'requirements-interview.txt', 'requirements-local.lock.txt', 'DELIVERY_README.md', 'UPSTREAM.json'):
        files.update(app.glob(pattern))
    files.update((app / 'config').glob('*.json'))
    for directory, patterns in (
        ('src/mmsa_v2', ('*.py',)),
        ('src/mmsa_v2/vendor/emotion2vec', ('*.py', 'LICENSE', 'SOURCE.json')),
        ('config/v2', ('bank.json', 'choice_wording.json', 'compile_bank.py')),
        ('web/v2', ('*.html','*.js','*.css','interview-opening.mp4')),
        ('web/v2/assets', ('tongji-qwen-challenge-*.jpg', 'tongji-qwen-challenge-source.json')),
        ('scripts', ('prepare_speech_emotion.py', 'prepare_visual_emotion.py', 'validate_multimodal_local.py',
                     'install_v2.py', 'install-v2.ps1', 'prepare_qwenwork_v2.py',
                     'first-run-v2.ps1', 'configure-bailian-gui.ps1')),
        ('config', ('v2-install-assets.json',)),
        ('tests/v2', ('test_*.py', 'browser_*.cjs')),
        ('tests/v2/fixtures', ('simulated_state_report.json', 'short_oral_replies.json')),
        ('docs/qwenwork-v2-staging/zhenjian-v2', ('SKILL.md',)),
        ('docs', ('v2-local-check.md','v2-scoring-multimodal-update-20261105.md','v2-development-blueprint-20261104.md',
                  'speech-emotion-runtime.md', 'v2-local-install.md', 'bailian-user-setup.md')),
    ):
        for pattern in patterns:
            files.update((workspace / directory).glob(pattern))
    for name in ('interview', 'hr-review', 'interview-report', 'workbench', 'research'):
        files.add(app / 'site' / (name + '.html'))
    for name in ('interview', 'hr-review', 'interview-report', 'workbench', 'research',
                 'text-display', 'observation-context', 'report-reader', 'research-summary', 'potential-profile', 'visual-evidence', 'evidence-timeline', 'alignment-editor',
                 'resumable-upload', 'candidate-flow', 'review-workspace', 'multimodal-view'):
        for folder, suffix in [('js', '.js'), ('css', '.css')]:
            files.add(app / 'site' / folder / (name + suffix))
    for name in ('NotoSansSC-Regular.ttf', 'OFL.txt', 'SOURCE.json'):
        files.add(app / 'vendor' / 'noto-sans-sc' / name)
    for name in ('LICENSE', 'README.md', 'SOURCE.json', 'TSCharacters.txt', 'TSPhrases.txt', 't2s.json'):
        files.add(app / 'vendor' / 'opencc-1.1.9' / name)
    for suffix in ('*.py', '*.cjs', '*.mjs'):
        files.update((app / 'tests').glob(suffix))
    files.add(app / 'tests' / 'fixtures' / 'team-text-contract.v1.json')
    for name in ('README.md', 'prepare-qwenwork.py', 'prepare-qwenwork.ps1',
                 'mcp-import.example.json', 'zhenjian-research/SKILL.md'):
        files.add(app / 'qwenwork' / name)
    for pattern in ('*.LICENSE', '*.source.json', '*.onnx.README.md', 'research-models.manifest.json'):
        files.update((app / 'models').glob(pattern))
    for name in ('Copyright.txt', 'OpenFace-license.txt'):
        files.add(app / 'models' / 'openface-2.2.0' / name)
    for name in ('assets.json', 'LICENSE', 'README.md'):
        files.add(app / 'models' / 'original-au-net' / name)
    for model in ('gaze360', 'emotic'):
        for name in ('assets.json', 'LICENSE', 'LICENSE.md', 'source.json', 'upstream-LICENSE', 'person-LICENSE', 'mirror-LICENSE'):
            files.add(app / 'models' / model / name)
    for name in ('SOURCE.json', 'README.md'):
        files.add(app / 'models' / 'faster-whisper-base' / name)
    for name in ('LICENSE', '.gitignore', 'README.md', 'VERSION', 'CHANGELOG.md', 'NOTICE.md',
                 'DEVELOPMENT.md', 'QWEN_HANDOFF.md', '启动面试工作台.cmd', '应用面试工作台更新.cmd',
                 '安装真才实鉴.cmd', '配置百炼API.cmd', '开始使用真才实鉴.cmd', '开始使用.txt'):
        files.add(workspace / name)
    files.add(app / 'requirements-v2.txt')
    result = []
    for path in sorted(files):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(workspace) or path.is_symlink():
            raise ValueError('发布文件越出工程目录或是符号链接。')
        result.append(path)
    return result


def build_source(workspace=WORKSPACE, output_dir=None):
    workspace = Path(workspace).resolve()
    output = Path(output_dir or workspace / 'delivery').resolve()
    output.mkdir(parents=True, exist_ok=True)
    files = source_files(workspace)
    if not files:
        raise ValueError('没有找到可发布源文件。')
    version_file = workspace / 'VERSION'
    product_version = version_file.read_text(encoding='utf-8').strip() if version_file.is_file() else None
    if product_version is not None and not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', product_version):
        raise ValueError('VERSION 必须为主版本.次版本.补丁版本，例如 1.0.0。')
    manifest = {'format': 'zhenjian-source-delivery.v1', 'product_version': product_version,
                'created_at': datetime.now(timezone.utc).isoformat(),
                'contains_runtime_data': False, 'contains_credentials': False,
                'weights_included': False, 'python_environment_included': False,
                'files': []}
    for path in files:
        content = path.read_bytes()
        # Fail closed on accidental live keys/capability links in documentation.
        if re.search(rb'\bsk-(?:ws-)?[A-Za-z0-9._-]{32,}', content) or re.search(rb'(?:token|access)=[a-f0-9]{64}\b', content):
            raise ValueError('源文件包含疑似访问凭据，已停止打包：' + str(path.relative_to(workspace)))
        manifest['files'].append({'path': path.relative_to(workspace).as_posix(),
                                  'sha256': hashlib.sha256(content).hexdigest(), 'bytes': len(content)})
    name = ('MMSA-HR-v' + product_version + '-source-' if product_version else 'zhenjian-source-') + stamp()
    archive = output / (name + '.zip')
    with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as bundle:
        for item, path in zip(manifest['files'], files):
            bundle.write(path, item['path'])
        bundle.writestr('DELIVERY-MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    manifest_path = output / (name + '.manifest.json')
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    checksum = digest(archive)
    archive.with_suffix('.sha256').write_text(checksum + '  ' + archive.name + '\n', encoding='ascii')
    return {'archive': str(archive), 'manifest': str(manifest_path), 'sha256': checksum,
            'files': len(files), 'contains_runtime_data': False, 'contains_credentials': False}


def doctor(verify_models=False):
    dependencies = {}
    for package in ['fastapi', 'uvicorn', 'python-multipart', 'requests', 'numpy', 'opencv-python',
                    'reportlab', 'torch', 'Pillow', 'openai-whisper', 'faster-whisper', 'ctranslate2', 'av', 'librosa', 'soundfile', 'scipy', 'webrtcvad-wheels']:
        try:
            dependencies[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            dependencies[package] = None
    models = {name: (ROOT / 'models' / name).is_file() for name in
              ['base.pt', 'face_detection_yunet_2023mar.onnx',
               'facial_expression_recognition_mobilefacenet_2022july.onnx',
               'openface-2.2.0/FeatureExtraction.exe']}
    from interview_au_net import PINNED, _verified_assets
    original_files = {name: (ROOT / 'models' / 'original-au-net' / name).is_file() for name in PINNED}
    optional_models = {'original_au_net': {'default_enabled': False, 'assets_present': all(original_files.values()),
        'files_present': original_files, 'verification': 'not_requested',
        'setup_switch': '-DownloadOriginalAuNet', 'execution': 'explicit_independent_research_only'}}
    for model, switch in [('gaze360', '-DownloadGaze360'), ('emotic', '-DownloadEmotic')]:
        optional_models[model] = {'default_enabled': False, 'default_config': 'interview.v4',
            'adapter_present': (ROOT / ('interview_' + model + '.py')).is_file(),
            'asset_manifest_present': (ROOT / 'models' / model / ('assets.json' if model == 'gaze360' else 'source.json')).is_file(),
            'verification': 'not_requested', 'setup_switch': switch,
            'execution': 'explicit_independent_research_only'}
    verified = None
    if verify_models:
        verified = {}
        from interview_multimodal import _verified_expression, _verified_openface
        for name, function in [('expression', _verified_expression), ('openface', _verified_openface)]:
            try:
                function()
                verified[name] = 'verified'
            except Exception:
                verified[name] = 'missing_or_hash_mismatch'
        yunet = ROOT / 'models' / 'face_detection_yunet_2023mar.onnx'
        try:
            source = json.loads(yunet.with_name(yunet.name + '.source.json').read_text(encoding='utf-8'))
            verified['yunet'] = 'verified' if digest(yunet) == source['sha256'] else 'hash_mismatch'
        except Exception:
            verified['yunet'] = 'missing_or_hash_mismatch'
        try:
            _verified_assets()
            verified['original_au_net'] = 'verified'
        except Exception:
            verified['original_au_net'] = 'missing_or_hash_mismatch'
        optional_models['original_au_net']['verification'] = verified['original_au_net']
        from importlib import import_module
        for model in ('gaze360', 'emotic'):
            try:
                import_module('interview_' + model)._verified_assets()
                verified[model] = 'verified'
            except Exception:
                verified[model] = 'missing_or_hash_mismatch'
            optional_models[model]['verification'] = verified[model]
    from fetch_asr import ASSETS as ASR_ASSETS, FILES as ASR_FILES, verify_assets as verify_asr_assets
    fast_asr = {'default_config': 'interview.v4', 'engine': 'faster-whisper-base-int8.v1',
        'assets_present': all((ASR_ASSETS / name).is_file() for name in ASR_FILES),
        'verification': 'not_requested', 'setup_switch': '-DownloadFastAsr',
        'fallback': 'openai-whisper-base.v1; explicitly recorded in each result'}
    if verify_models:
        try:
            verify_asr_assets()
            fast_asr['verification'] = 'verified'
        except Exception:
            fast_asr['verification'] = 'missing_or_hash_mismatch'
    try:
        from interview_credentials import public_settings, credential_available
        settings = public_settings()
        credential = {'configured': credential_available(), 'model': settings['model'],
                      'base_url_present': bool(settings['base_url']), 'decryption_checked': False}
    except Exception:
        credential = {'configured': False, 'status': 'unreadable', 'decryption_checked': False}
    return {'schema_version': 'interview-doctor.v1', 'python': sys.version.split()[0],
            'platform': sys.platform, 'dependencies': dependencies,
            'ffmpeg': bool(shutil.which('ffmpeg')), 'ffprobe': bool(shutil.which('ffprobe')),
            'models_present': models, 'model_hash_verification': verified, 'optional_models': optional_models, 'fast_local_asr': fast_asr,
            'credential': credential, 'performs_inference': False, 'calls_cloud': False,
            'note': '依赖存在不等于模型已运行或测量已验证；新机器须完成合成流程验收。'}


def _clear_operation_cache(db):
    """Drop cached responses, not canonical business records.

    Old create responses contained capabilities and their caller-supplied
    idempotency key itself can be a recovery capability. Do not migrate any
    operation row. Callers vacuum the disposable DB after committing so freed
    SQLite pages cannot retain the removed response bytes.
    """
    if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='operations'").fetchone():
        return 0
    count = db.execute('SELECT count(*) FROM operations').fetchone()[0]
    db.execute('DELETE FROM operations')
    return count


def backup_data(data_dir=None, output_dir=None):
    source = Path(data_dir or ROOT / 'interview-data').resolve()
    db_path = source / 'interviews.sqlite3'
    if not db_path.is_file():
        raise ValueError('所选数据目录没有面试数据库。')
    output = Path(output_dir or ROOT / 'private-backups').resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest = {'format': 'zhenjian-private-backup.v1', 'created_at': datetime.now(timezone.utc).isoformat(),
                'contains_participant_data': True, 'contains_credentials': False, 'files': [], 'media_rebase': {},
                'pending_upload_chunks': [], 'research_artifacts': [], 'missing_research_artifacts': []}
    with tempfile.TemporaryDirectory(prefix='zhenjian-backup-') as temporary:
        snapshot = Path(temporary) / 'interviews.sqlite3'
        origin = sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)
        target = sqlite3.connect(snapshot)
        try:
            origin.backup(target)
            target.execute('PRAGMA journal_mode=DELETE')
            target.execute('PRAGMA secure_delete=ON')
            if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ValueError('数据库快照完整性检查失败。')
            media = target.execute('SELECT DISTINCT path,media_hash FROM answers WHERE path IS NOT NULL').fetchall()
            tables = {row[0] for row in target.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            pending_chunks = []
            artifacts = _research_artifacts(target) if 'research_runs' in tables else []
            if {'media_uploads', 'media_upload_chunks'} <= tables:
                pending_chunks = target.execute('''SELECT c.upload_id,c.chunk_index,c.size,c.sha256
                    FROM media_upload_chunks c JOIN media_uploads u ON u.id=c.upload_id WHERE u.status!='completed'
                    ORDER BY c.upload_id,c.chunk_index''').fetchall()
            removed_operations = _clear_operation_cache(target)
            for table in ('jobs', 'research_runs'):
                if table in tables:
                    target.execute(f'UPDATE {table} SET lease_token=NULL,lease_until=NULL')
            target.commit()
            target.execute('VACUUM')
            manifest['operation_cache'] = {'policy': 'cleared_in_backup_copy', 'removed_rows': removed_operations,
                                           'freed_pages_removed': True, 'business_records_preserved': True}
        finally:
            target.close()
            origin.close()
        members = [('interviews.sqlite3', snapshot)]
        occupied = set()
        for stored_path, stored_hash in media:
            path = Path(stored_path).resolve()
            if not path.is_relative_to(source / 'media') or not path.is_file() or Path(stored_path).is_symlink():
                raise ValueError('数据库引用的原始媒体不在本项目媒体目录或不存在。')
            relative = path.relative_to(source).as_posix()
            actual_hash = digest(path)
            if stored_hash and actual_hash != stored_hash:
                raise ValueError('原始媒体与数据库摘要不一致。')
            manifest['media_rebase'][stored_path] = relative
            if relative not in occupied:
                members.append((relative, path))
                occupied.add(relative)
        for upload_id, index, size, checksum in pending_chunks:
            relative = f'uploads/{upload_id}/{index}.part'
            if not _safe_member(relative) or type(index) is not int or type(size) is not int or not 0 < size <= 4 * 1024 * 1024:
                raise ValueError('上传分片记录包含不安全路径或大小。')
            path = source / relative
            if path.is_symlink() or not path.resolve().is_relative_to(source / 'uploads') or not path.is_file():
                raise ValueError('未完成上传引用的分片缺失或越出上传目录；请先修复再备份。')
            if path.stat().st_size != size or digest(path) != checksum:
                raise ValueError('未完成上传分片与数据库摘要不符；未创建不完整备份。')
            members.append((relative, path))
            manifest['pending_upload_chunks'].append(relative)
        for relative, expected in artifacts:
            if not _safe_member(relative):
                raise ValueError('观测文件记录包含不安全路径。')
            path = source / relative
            if path.is_symlink() or not path.resolve().is_relative_to(source / 'research-work'):
                raise ValueError('观测文件越出研究目录。')
            if not path.exists():
                # Older deliveries kept embedded results but not this separate
                # diagnostic artifact. Record the gap rather than invent bytes.
                manifest['missing_research_artifacts'].append(relative)
                continue
            if not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
                raise ValueError('观测文件不是普通文件或超过32MiB。')
            from interview_research import public
            value = public(json.loads(path.read_text(encoding='utf-8')))
            # Legacy AU-Net diagnostics recorded the adapter completion time;
            # the outer pipeline added/overwrote only completed_at fields. Keep
            # both original files, comparing all measurement/timing values other
            # than those explicitly different completion stamps.
            legacy_au = '/original-au-net/' in relative and 'artifact' not in expected
            def diagnostic_projection(record):
                projected = {key: item for key, item in record.items() if key != 'completed_at'}
                if isinstance(projected.get('observations'), list):
                    projected['observations'] = [{key: item for key, item in row.items() if key != 'completed_at'}
                                                 for row in projected['observations']]
                return projected
            checked_value = diagnostic_projection(value) if legacy_au and isinstance(value, dict) else value
            checked_expected = diagnostic_projection(expected) if legacy_au else expected
            mismatch = (not isinstance(value, dict) or
                        ('artifact' in expected and value != expected) or
                        ('artifact' not in expected and (not {'run_id', 'observations', 'status', 'summary', 'model_id'} <= set(value)
                         or any(checked_expected.get(key) != item for key, item in checked_value.items()))))
            if mismatch:
                raise ValueError('观测文件与数据库冻结结果不一致。')
            members.append((relative, path))
            manifest['research_artifacts'].append(relative)
        for relative, path in members:
            manifest['files'].append({'path': relative, 'sha256': digest(path), 'bytes': path.stat().st_size})
        if sum(x['bytes'] for x in manifest['files']) > MAX_BACKUP_BYTES:
            raise ValueError('备份超过20GiB限制，请按会话安排数据归档。')
        archive = output / ('interview-backup-' + stamp() + '.zip')
        with zipfile.ZipFile(archive, 'x', zipfile.ZIP_DEFLATED) as bundle:
            for relative, path in members:
                bundle.write(path, relative)
            bundle.writestr('BACKUP-MANIFEST.json', json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
    return {'archive': str(archive), 'sha256': digest(archive), 'media_files': len(occupied), 'pending_upload_chunks': len(pending_chunks),
            'research_artifacts': len(manifest['research_artifacts']), 'missing_research_artifacts': len(manifest['missing_research_artifacts']),
            'contains_participant_data': True, 'contains_credentials': False,
            'operation_cache_cleared': True, 'removed_operation_rows': removed_operations,
            'source_database_modified': False, 'restore_rotates_session_tokens': True}


def _research_artifacts(db):
    """Only declared, canonical JSON observation artifacts, never a folder walk."""
    if 'result' not in {row[1] for row in db.execute('PRAGMA table_info(research_runs)')}:
        return []
    files = {}
    for run_id, result in db.execute('SELECT id,result FROM research_runs WHERE result IS NOT NULL'):
        for output in json.loads(result).get('multimodal', []):
            if output.get('artifact'):
                relative = f"research-work/{run_id}/{output.get('answer_id')}/{output['artifact']}"
                if not _safe_member(relative):
                    raise ValueError('观测文件引用无效。')
                files[relative] = output
            original = output.get('modules', {}).get('au_net') or {}
            if original.get('run_id'):
                relative = f"research-work/{run_id}/{output.get('answer_id')}/original-au-net/au-net-{original['run_id']}.json"
                if not _safe_member(relative):
                    raise ValueError('原始AU-Net文件引用无效。')
                files[relative] = original
            for name in ('gaze360', 'emotic'):
                model = output.get('modules', {}).get(name) or {}
                if model.get('artifact') and model.get('run_id'):
                    relative = f"research-work/{run_id}/{output.get('answer_id')}/{name}/{model['artifact']}"
                    if not _safe_member(relative) or model['artifact'] != f"{name}-{model['run_id']}.json":
                        raise ValueError('模型观测文件引用无效。')
                    files[relative] = model
    return sorted(files.items())


def _safe_member(name):
    path = PurePosixPath(name)
    return (bool(name) and str(path) == name and not path.is_absolute() and '\\' not in name and ':' not in name
            and all(part not in {'..', '.'} for part in path.parts)
            and (name == 'interviews.sqlite3' or name == 'BACKUP-MANIFEST.json' or name.startswith('media/')
                 or bool(re.fullmatch(r'uploads/[a-f0-9]{32}/(?:0|[1-9][0-9]*)\.part', name))
                 or bool(re.fullmatch(r'research-work/[a-f0-9]{32}/[a-f0-9]{32}/(?:multimodal-[a-f0-9]{32}\.json|original-au-net/au-net-[a-f0-9]{32}\.json|gaze360/gaze360-[a-f0-9]{32}\.json|emotic/emotic-[a-f0-9]{32}\.json)', name))))


def _rebase_private_paths(value, mapping, target):
    if isinstance(value, list):
        return [_rebase_private_paths(v, mapping, target) for v in value]
    if isinstance(value, dict):
        return {key: str(target / mapping[item]) if key in {'_media_path', 'media_path', 'path'} and isinstance(item, str) and item in mapping
                else _rebase_private_paths(item, mapping, target) for key, item in value.items()}
    return value


def restore_data(archive, target):
    archive, target = Path(archive).resolve(), Path(target).resolve()
    if target.exists() or not target.parent.is_dir():
        raise ValueError('恢复目标必须是父目录已存在的全新目录；拒绝覆盖现有数据。')
    # Verify every byte before creating the target directory. Path names from
    # archives are data, never shell code or permission to access arbitrary paths.
    with zipfile.ZipFile(archive) as bundle:
        infos = bundle.infolist()
        names = [i.filename for i in infos]
        if len(names) != len(set(names)) or any(not _safe_member(n) for n in names):
            raise ValueError('备份包含重复或不安全路径。')
        if any((i.external_attr >> 16) & 0o170000 == 0o120000 for i in infos) or sum(i.file_size for i in infos) > MAX_BACKUP_BYTES:
            raise ValueError('备份含符号链接或超出大小限制。')
        manifest = json.loads(bundle.read('BACKUP-MANIFEST.json'))
        if manifest.get('format') != 'zhenjian-private-backup.v1':
            raise ValueError('备份格式不受支持。')
        listed = {item['path']: item for item in manifest['files']}
        if set(listed) != set(names) - {'BACKUP-MANIFEST.json'} or 'interviews.sqlite3' not in listed:
            raise ValueError('备份文件清单不完整。')
        mapping = manifest['media_rebase']
        if not isinstance(mapping, dict) or any(not isinstance(k, str) or v not in listed or not v.startswith('media/') for k, v in mapping.items()):
            raise ValueError('媒体迁移映射无效。')
        for name, record in listed.items():
            with bundle.open(name) as stream:
                actual = hashlib.file_digest(stream, 'sha256').hexdigest()
            if actual != record['sha256'] or bundle.getinfo(name).file_size != record['bytes']:
                raise ValueError('备份摘要或字节数不一致。')
        # Stage beside the destination, then publish by one directory rename.
        staging = target.parent / ('.restore-' + uuid.uuid4().hex)
        staging.mkdir()
        try:
            for name in listed:
                destination = staging / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(name) as source, destination.open('xb') as output:
                    shutil.copyfileobj(source, output)
            secret = secrets.token_bytes(32)
            (staging / '.token-secret').write_bytes(secret)
            db = sqlite3.connect(staging / 'interviews.sqlite3')
            try:
                db.execute('PRAGMA journal_mode=DELETE')
                db.execute('PRAGMA secure_delete=ON')
                if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                    raise ValueError('恢复数据库完整性检查失败。')
                tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                removed_operations = _clear_operation_cache(db)
                for sid, in db.execute('SELECT id FROM sessions').fetchall():
                    hashes = [hashlib.sha256(hmac.new(secret, f'{sid}:{role}'.encode(), hashlib.sha256).hexdigest().encode()).hexdigest()
                              for role in ('candidate', 'hr')]
                    db.execute('UPDATE sessions SET candidate_hash=?,hr_hash=? WHERE id=?', (*hashes, sid))
                for original, relative in mapping.items():
                    db.execute('UPDATE answers SET path=? WHERE path=?', (str(target / relative), original))
                if 'jobs' in tables:
                    db.execute("UPDATE jobs SET status='blocked',error='迁移恢复后暂停：请显式重新运行。',lease_token=NULL,lease_until=NULL WHERE status IN ('queued','running')")
                if 'research_runs' in tables:
                    # Frozen research inputs/results remain historical records.
                    # Their old private media path is never exposed by the API;
                    # a new run takes a fresh snapshot from rebased answers.
                    db.execute("UPDATE research_runs SET status='failed',error='迁移恢复后暂停：请显式新建研究运行。',lease_token=NULL,lease_until=NULL WHERE status IN ('queued','running')")
                paused_standard_sessions = 0
                columns = {row[1] for row in db.execute('PRAGMA table_info(sessions)')}
                if 'config' in columns:
                    db.execute('CREATE TABLE IF NOT EXISTS standard_analysis_controls(session_id TEXT PRIMARY KEY,paused INTEGER NOT NULL,reason TEXT,updated_at TEXT)')
                    for sid, config in db.execute('SELECT id,config FROM sessions').fetchall():
                        if (json.loads(config).get('analysis_plan') or {}).get('enabled'):
                            db.execute('INSERT INTO standard_analysis_controls VALUES(?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET paused=excluded.paused,reason=excluded.reason,updated_at=excluded.updated_at',
                                       (sid, 1, '迁移后暂停默认观测；须由老师在新环境显式启用本场分析。', datetime.now(timezone.utc).isoformat()))
                            if 'version' in columns:
                                db.execute('UPDATE sessions SET version=version+1 WHERE id=?', (sid,))
                            paused_standard_sessions += 1
                # Check canonical pending chunk references as well as archive hashes.
                # Old v1 backups lacking chunks remain recoverable, but their missing
                # chunks must be reuploaded; they cannot appear as received on restore.
                if {'media_uploads', 'media_upload_chunks'} <= tables:
                    for upload_id, index, size, checksum in db.execute('''SELECT c.upload_id,c.chunk_index,c.size,c.sha256
                        FROM media_upload_chunks c JOIN media_uploads u ON u.id=c.upload_id WHERE u.status!='completed' ''').fetchall():
                        relative = f'uploads/{upload_id}/{index}.part'
                        if not _safe_member(relative):
                            raise ValueError('恢复数据库中的上传分片路径无效。')
                        if relative not in listed:
                            db.execute('DELETE FROM media_upload_chunks WHERE upload_id=? AND chunk_index=?', (upload_id, index))
                        elif listed[relative]['bytes'] != size or listed[relative]['sha256'] != checksum:
                            raise ValueError('恢复的上传分片与数据库校验信息不符。')
                db.commit()
                db.execute('VACUUM')
                count = db.execute('SELECT count(*) FROM sessions').fetchone()[0]
            finally:
                db.close()
            (staging / 'media').mkdir(exist_ok=True)
            (staging / 'RESTORE-RECORD.json').write_text(json.dumps({
                'format': 'zhenjian-restore.v1', 'restored_at': datetime.now(timezone.utc).isoformat(),
                'source_backup_sha256': digest(archive), 'session_tokens_rotated': True,
                'operation_cache_cleared': True, 'removed_operation_rows': removed_operations,
                'unfinished_jobs_paused': True, 'standard_analysis_sessions_paused': paused_standard_sessions,
                'pending_upload_chunks_preserved': len([n for n in listed if n.startswith('uploads/')]),
                'research_artifacts_preserved': len([n for n in listed if n.startswith('research-work/')]),
                'missing_research_artifacts': manifest.get('missing_research_artifacts', []),
                'credentials_restored': False}, ensure_ascii=False, indent=2), encoding='utf-8')
            if target.exists():
                raise ValueError('恢复期间目标目录被创建，拒绝覆盖。')
            staging.rename(target)
        except BaseException:
            # Only this freshly-created, verified staging directory is removed.
            if staging.exists() and staging.parent == target.parent and staging.name.startswith('.restore-'):
                shutil.rmtree(staging)
            raise
    return {'target': str(target), 'sessions': count, 'session_tokens_rotated': True,
            'operation_cache_cleared': True, 'removed_operation_rows': removed_operations,
            'credentials_restored': False, 'unfinished_jobs_paused': True,
            'standard_analysis_sessions_paused': paused_standard_sessions,
            'pending_upload_chunks_preserved': len([n for n in listed if n.startswith('uploads/')]),
            'research_artifacts_preserved': len([n for n in listed if n.startswith('research-work/')]),
            'missing_research_artifacts': len(manifest.get('missing_research_artifacts', []))}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('build').add_argument('--output-dir')
    sub.add_parser('doctor').add_argument('--verify-models', action='store_true')
    backup = sub.add_parser('backup')
    backup.add_argument('--data-dir')
    backup.add_argument('--output-dir')
    restore = sub.add_parser('restore')
    restore.add_argument('--archive', required=True)
    restore.add_argument('--target', required=True)
    args = parser.parse_args()
    try:
        result = build_source(output_dir=args.output_dir) if args.command == 'build' else doctor(args.verify_models) if args.command == 'doctor' else backup_data(args.data_dir, args.output_dir) if args.command == 'backup' else restore_data(args.archive, args.target)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        print(json.dumps({'error': str(error) if isinstance(error, ValueError) else '操作未完成；未覆盖已有数据或输出凭据。'}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
