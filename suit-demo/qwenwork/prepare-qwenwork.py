"""Prepare V1 local MCP settings and a reproducible, source-only skill package.

No service calls, credential reads, software installation or client settings edits.
Re-run on each target machine: the import JSON contains that checkout's paths.
"""
import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
import re
import sys
import zipfile

PACKAGE_VERSION = '1.0.0'
SKILL_NAME = 'zhenjian-research'


def _skill_bytes(root):
    source = root / SKILL_NAME / 'SKILL.md'
    if source.is_symlink() or not source.is_file():
        raise ValueError('技能入口不存在或不是普通文件。')
    data = source.read_bytes()
    text = data.decode('utf-8-sig')
    frontmatter = re.match(r'^---\r?\n(.*?)\r?\n---(?:\r?\n|$)', text, re.S)
    if not frontmatter or not re.search(r'^name:\s*' + SKILL_NAME + r'\s*$', frontmatter[1], re.M):
        raise ValueError('技能 frontmatter 的 name 必须与文件夹名称一致。')
    if not re.search(r'^description:\s*\S.+$', frontmatter[1], re.M):
        raise ValueError('技能缺少 description。')
    if (re.search(rb'\bsk-(?:ws-)?[A-Za-z0-9._-]{32,}', data)
            or re.search(rb'(?:token|access)=[a-f0-9]{64}\b', data)):
        raise ValueError('技能资料包含疑似凭据，未生成任何导入包。')
    return text.replace('\r\n', '\n').encode('utf-8')


def _skill_archive(data):
    stream = BytesIO()
    with zipfile.ZipFile(stream, 'w', zipfile.ZIP_DEFLATED) as bundle:
        entry = zipfile.ZipInfo(SKILL_NAME + '/SKILL.md', date_time=(1980, 1, 1, 0, 0, 0))
        entry.compress_type = zipfile.ZIP_DEFLATED
        entry.create_system = 3
        entry.external_attr = 0o100644 << 16
        bundle.writestr(entry, data)
    return stream.getvalue()


def prepare(output_dir=None, python_executable=None, port=8766):
    root = Path(__file__).resolve().parent
    output = Path(output_dir or root).resolve()
    executable = Path(python_executable or sys.executable).resolve()
    script = root.parent / 'interview_mcp.py'
    if not executable.is_file() or not script.is_file() or type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Python、连接器路径或端口无效。')
    if script.is_symlink() or not script.resolve().is_relative_to(root.parent):
        raise ValueError('连接器脚本须属于当前工程。')
    archive_bytes = _skill_archive(_skill_bytes(root))
    if len(archive_bytes) > 10 * 1024 * 1024:
        raise ValueError('技能 ZIP 超过官方 10 MiB 限制。')
    config = {'mcpServers': {'zhenjian-interview': {'type': 'stdio', 'command': str(executable),
                'args': [str(script), '--base-url', 'http://127.0.0.1:' + str(port)],
                'env': {'PYTHONUTF8': '1'}}}}
    config_bytes = (json.dumps(config, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    manifest = {'schema_version': 'qwenwork-package.v1', 'package_version': PACKAGE_VERSION,
        'transport': 'stdio', 'service': 'zhenjian-interview', 'runtime_api_config': 'interview.v4',
        'contains_credentials': False, 'contains_participant_data': False, 'contains_model_weights': False,
        'local_import_requires_regeneration_after_move': True, 'native_client_acceptance': 'not_verified',
        'official_docs_checked_on': '2026-10-27', 'files': [
            {'path': 'zhenjian-research.zip', 'sha256': hashlib.sha256(archive_bytes).hexdigest(),
             'bytes': len(archive_bytes), 'portable': True},
            {'path': 'mcp-import.local.json', 'sha256': hashlib.sha256(config_bytes).hexdigest(),
             'bytes': len(config_bytes), 'portable': False}]}
    output.mkdir(parents=True, exist_ok=True)
    config_path = output / 'mcp-import.local.json'
    config_path.write_bytes(config_bytes)
    archive = output / 'zhenjian-research.zip'
    archive.write_bytes(archive_bytes)
    manifest_path = output / 'qwenwork-package-manifest.json'
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return {'config': str(config_path), 'skill_package': str(archive),
            'manifest': str(manifest_path), 'package_version': PACKAGE_VERSION,
            'skill_sha256': hashlib.sha256(archive_bytes).hexdigest(), 'contains_credentials': False,
            'native_client_acceptance': 'not_verified'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--output-dir')
    args = parser.parse_args()
    print(json.dumps(prepare(args.output_dir, port=args.port), ensure_ascii=False))
