"""Local runtime identity and narrowly scoped Windows process control.

No application/database initialization, model imports or credential reads.
"""
from __future__ import annotations

import argparse
import base64
import ctypes
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
ROLES = {
    'server': (None, None), 'worker': ('interview_worker.py', 'media'),
    'content_worker': ('interview_worker.py', 'content'),
    'content_worker2': ('interview_worker.py', 'content'),
    'routing_worker': ('interview_worker.py', 'routing'),
    'research_worker': ('interview_research_worker.py', 'core'),
    'extended_worker': ('interview_research_worker.py', 'extended'),
}


class RuntimeErrorSafe(RuntimeError):
    def __init__(self, message, *, diagnostics=None):
        super().__init__(message)
        self.diagnostics = diagnostics or {}


def build_identity():
    files = {p for p in ROOT.glob('interview_*.py') if p.name != 'interview_mcp.py'}
    files.update(ROOT.glob('requirements*.txt'))
    def source_input(path):
        parts = path.relative_to(ROOT).parts
        return (not any(part.startswith('.') or part.lower() in {'secrets', 'models', 'local-data'} for part in parts)
                and '.local.' not in path.name.lower()
                and not any(word in path.name.lower() for word in ('credential', 'secret')))
    files.update(p for p in (ROOT / 'config').rglob('*.json') if source_input(p))
    files.update(p for p in (ROOT / 'prompts').rglob('*') if p.is_file() and source_input(p))
    version_file = ROOT.parent / 'VERSION'
    version = version_file.read_text(encoding='utf-8-sig').strip()
    digest = hashlib.sha256()
    digest.update(('product_version\0' + version + '\0').encode('utf-8'))
    for path in sorted(files, key=lambda p: p.relative_to(ROOT).as_posix()):
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise RuntimeErrorSafe('Build input must remain inside this project.')
        data = path.read_bytes()
        digest.update(path.relative_to(ROOT).as_posix().encode('utf-8') + b'\0')
        digest.update(str(len(data)).encode('ascii') + b'\0' + data)
    return {'product_version': version, 'backend_build_id': digest.hexdigest()}


def maintenance_path(data_dir):
    return Path(data_dir).resolve() / 'maintenance.json'


def maintenance_active(data_dir):
    # Fail closed even when an interrupted writer left an unreadable marker.
    return os.path.lexists(maintenance_path(data_dir))


def _path(value):
    return os.path.normcase(str(Path(value).resolve()))


def _powershell(script):
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    encoded = base64.b64encode(script.encode('utf-16le')).decode('ascii')
    result = subprocess.run([str(executable), '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        raise RuntimeErrorSafe('Windows process inspection failed; no ownership assumption was made.')
    return json.loads(result.stdout.decode('utf-8-sig'))


def process_inventory(port):
    if type(port) is not int or not 1 <= port <= 65535:
        raise RuntimeErrorSafe('Invalid local port.')
    return _powershell("""$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false)
$items=@(Get-CimInstance Win32_Process -ErrorAction Stop | ForEach-Object {
    @{pid=[int]$_.ProcessId;parent_pid=[int]$_.ParentProcessId;executable=$_.ExecutablePath;
      command_line=$_.CommandLine;created_at=$(if ($_.CreationDate) {$_.CreationDate.ToUniversalTime().ToString('o')} else {$null})}
})
$listeners=@(Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object LocalPort -eq """ + str(port) + """ | ForEach-Object { [int]$_.OwningProcess })
@{processes=$items;listeners=$listeners} | ConvertTo-Json -Depth 5 -Compress
""")


def command_arguments(command):
    if not isinstance(command, str) or not command:
        return []
    shell = ctypes.WinDLL('shell32', use_last_error=True)
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    count = ctypes.c_int()
    pointer = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not pointer:
        raise RuntimeErrorSafe('Cannot parse a process command line.')
    try:
        return [pointer[i] for i in range(count.value)]
    finally:
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree(pointer)


def _arguments(item):
    return item.get('argv') if isinstance(item.get('argv'), list) else command_arguments(item.get('command_line'))


def _allowed_executables():
    python = ROOT.parent / '.venv-suit/Scripts/python.exe'
    values = {_path(python)}
    config = (ROOT.parent / '.venv-suit/pyvenv.cfg').read_text(encoding='utf-8-sig')
    for line in config.splitlines():
        if line.startswith('executable = '):
            values.add(_path(line.split(' = ', 1)[1]))
    return values


def _matches_role(item, role, port):
    if not item.get('executable') or _path(item['executable']) not in _allowed_executables():
        return False
    args = _arguments(item)[1:]
    if role == 'server':
        return (len(args) == 9 and args[:3] == ['-m', 'uvicorn', 'interview_entry:app']
                and args[3] == '--app-dir' and _path(args[4]) == _path(ROOT)
                and args[5:] == ['--host', '127.0.0.1', '--port', str(port)])
    script, lane = ROLES[role]
    return len(args) == 3 and _path(args[0]) == _path(ROOT / script) and args[1:] == ['--lane', lane]


def _project_script(item):
    args = _arguments(item)[1:]
    if args[:2] == ['-X', 'utf8']:
        args = args[2:]
    names = ('interview_worker.py', 'interview_research_worker.py')
    return bool(args and Path(args[0]).name.lower() in names and _path(args[0]) in {_path(ROOT / p) for p in names})


def _project_api(item):
    if not item.get('executable') or _path(item['executable']) not in _allowed_executables():
        return False
    args = _arguments(item)[1:]
    return (len(args) >= 5 and args[:3] == ['-m', 'uvicorn', 'interview_entry:app']
            and args[3] == '--app-dir' and _path(args[4]) == _path(ROOT))


def _same_time(left, right):
    try:
        return datetime.fromisoformat(left.replace('Z', '+00:00')) == datetime.fromisoformat(right.replace('Z', '+00:00'))
    except (ValueError, TypeError, AttributeError):
        return False


def load_process_record(data_dir):
    path = Path(data_dir) / 'processes.json'
    if not path.is_file() or path.is_symlink():
        raise RuntimeErrorSafe('Managed process record is missing or unsafe.')
    record = json.loads(path.read_text(encoding='utf-8-sig'))
    if _path(record.get('project', '')) != _path(ROOT):
        raise RuntimeErrorSafe('Process record belongs to another project.')
    if type(record.get('port')) is not int or not 1 <= record['port'] <= 65535:
        raise RuntimeErrorSafe('Process record has an invalid port.')
    return record


def managed_processes(record, inventory, *, allow_legacy=False, require_all=False):
    """Validate the entire plan before stopping anything. Return root processes."""
    if _path(record.get('project', '')) != _path(ROOT):
        raise RuntimeErrorSafe('Process project mismatch.')
    by_pid = {p['pid']: p for p in inventory['processes']}
    roots = {}
    for role in ROLES:
        pid = record.get(role + '_pid')
        if role + '_pid' not in record and not require_all:
            continue
        if type(pid) is not int or pid < 1:
            raise RuntimeErrorSafe('A managed role has no valid PID: ' + role)
        item = by_pid.get(pid)
        if item is None:
            if require_all:
                raise RuntimeErrorSafe('Managed process is absent: ' + role)
            continue
        created = record.get(role + '_created_at')
        if (not created and not allow_legacy) or (created and not _same_time(created, item['created_at'])):
            raise RuntimeErrorSafe('Managed PID creation time mismatch: ' + role)
        if not _matches_role(item, role, record['port']) or pid in roots:
            raise RuntimeErrorSafe('Managed process identity mismatch: ' + role)
        roots[pid] = item
    owned = set(roots)
    allowed = _allowed_executables()
    while True:
        children = [p for p in by_pid.values() if p['parent_pid'] in owned and p['pid'] not in owned]
        if not children:
            break
        for child in children:
            executable = child.get('executable')
            known_python = executable and _path(executable) in allowed and (
                _project_script(child) or _matches_role(child, 'server', record['port']))
            known_media = executable and Path(executable).name.lower() in {'ffmpeg.exe', 'ffprobe.exe'}
            console_host = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/conhost.exe'
            known_console = executable and _path(executable) == _path(console_host)
            if not known_python and not known_media and not known_console:
                raise RuntimeErrorSafe('Unexpected child process; refusing a process-tree stop.')
            owned.add(child['pid'])
    for item in by_pid.values():
        if item['pid'] not in owned and (_project_script(item) or _project_api(item)):
            raise RuntimeErrorSafe('Unregistered project process; refusing to mix runtimes.')
    listeners = set(inventory['listeners'])
    if listeners - owned or (roots and record['server_pid'] in roots and not listeners):
        raise RuntimeErrorSafe('Listening port does not match the managed API process tree.')
    return list(roots.values())


def stop_managed(data_dir, *, allow_legacy=False):
    record = load_process_record(data_dir)
    if allow_legacy:
        marker = json.loads(maintenance_path(data_dir).read_text(encoding='utf-8'))
        if marker.get('bootstrap') is not True:
            raise RuntimeErrorSafe('Legacy process adoption is allowed only during explicit bootstrap.')
    snapshot = process_inventory(record['port'])
    roots = managed_processes(record, snapshot, allow_legacy=allow_legacy)
    stop_results = []
    # Validate all identities before the first stop, then recheck each root to
    # avoid acting on a PID reused since the snapshot. Never enumerate by name.
    for item in roots:
        current = process_inventory(record['port'])
        managed_processes(record, current, allow_legacy=allow_legacy)
        live = next((p for p in current['processes'] if p['pid'] == item['pid']), None)
        if live is None:
            continue
        if not _same_time(live['created_at'], item['created_at']) or live.get('command_line') != item.get('command_line') or live.get('executable') != item.get('executable'):
            raise RuntimeErrorSafe('Process identity changed during stop; refusing to continue.')
        target_tree = {item['pid']: live}
        while True:
            children = [p for p in current['processes'] if p['parent_pid'] in target_tree and p['pid'] not in target_tree]
            if not children:
                break
            target_tree.update({p['pid']: p for p in children})
        result = subprocess.run(['taskkill.exe', '/PID', str(item['pid']), '/T', '/F'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        # taskkill /T can return a nonzero code when a console host or child
        # exits during tree traversal. Accept only independently verified exit
        # of the complete original tree; never repeat or broaden termination.
        after_stop = process_inventory(record['port'])
        remaining = [p for p in after_stop['processes'] if p['pid'] in target_tree]
        changed = [p for p in remaining if not _same_time(p.get('created_at'), target_tree[p['pid']].get('created_at'))
                   or p.get('executable') != target_tree[p['pid']].get('executable')
                   or p.get('command_line') != target_tree[p['pid']].get('command_line')]
        stop_results.append({'pid': item['pid'], 'exit_code': result.returncode,
            'verified_tree_size': len(target_tree), 'remaining_count': len(remaining),
            'identity_changed_count': len(changed), 'tree_exited': not remaining})
        if remaining:
            raise RuntimeErrorSafe('A verified process tree is still present or its identity changed; no further stop was attempted.',
                                   diagnostics={'stop_results': stop_results})
        managed_processes(record, after_stop, allow_legacy=allow_legacy)
    after = process_inventory(record['port'])
    if any(_project_script(p) or _project_api(p) for p in after['processes']) or after['listeners']:
        raise RuntimeErrorSafe('Managed service did not fully stop.')
    return {'stopped_process_count': len(roots), 'stop_results': stop_results}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('build', 'verify', 'assert-stopped', 'stop'))
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--legacy-record', action='store_true')
    args = parser.parse_args()
    data_dir = ROOT / 'interview-data'
    if args.command == 'build':
        result = build_identity()
    elif args.command == 'assert-stopped':
        inventory = process_inventory(args.port)
        if inventory['listeners'] or any(_project_script(p) or _project_api(p) for p in inventory['processes']):
            raise RuntimeErrorSafe('Project process or port is already active; use the controlled updater.')
        result = {'stopped': True}
    elif args.command == 'stop':
        result = stop_managed(data_dir, allow_legacy=args.legacy_record)
    else:
        record = load_process_record(data_dir)
        items = managed_processes(record, process_inventory(record['port']), require_all=True)
        result = {'verified': True, 'managed_process_count': len(items)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'ok': False, 'error_type': type(error).__name__,
            'error': str(error) if isinstance(error, RuntimeErrorSafe) else type(error).__name__,
            'diagnostics': error.diagnostics if isinstance(error, RuntimeErrorSafe) else {}}))
        raise SystemExit(1)
