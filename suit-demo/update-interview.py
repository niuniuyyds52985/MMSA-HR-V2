"""Apply already-present local code to an idle, identity-checked runtime.

Does not fetch code, install dependencies, retry jobs or modify interview data.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler
import uuid

from interview_runtime import (ROOT, RuntimeErrorSafe, build_identity, load_process_record,
    maintenance_active, maintenance_path, managed_processes, process_inventory)

DATA = ROOT / 'interview-data'


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeErrorSafe('Health endpoint redirects are not allowed.')


def health(port):
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open('http://127.0.0.1:' + str(port) + '/health', timeout=3) as response:
            value = json.loads(response.read(65537).decode('utf-8'))
    except (URLError, TimeoutError, OSError):
        return None
    if not isinstance(value, dict) or value.get('service') != 'zhenjian-interview':
        raise RuntimeErrorSafe('The local port is not the interview API.')
    fields = ('service', 'status', 'config_version', 'product_version', 'backend_build_id',
              'maintenance_supported', 'maintenance_active', 'active_requests', 'active_write_requests')
    return {key: value[key] for key in fields if key in value}


def connect_database(*, readonly=False):
    path = DATA / 'interviews.sqlite3'
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(DATA.resolve()):
        raise RuntimeErrorSafe('Existing project database is missing or unsafe.')
    return sqlite3.connect(path.as_uri() + ('?mode=ro' if readonly else '?mode=rw'), uri=True,
                           timeout=2, isolation_level=None)


def pending_counts(db):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'jobs' not in tables or 'sessions' not in tables:
        raise RuntimeErrorSafe('Database schema is not the expected interview store.')
    result = {}
    for table in ('jobs', 'research_runs'):
        result[table] = {state: 0 for state in ('queued', 'running')}
        if table in tables:
            for state, count in db.execute(f"SELECT status,count(*) FROM {table} WHERE status IN ('queued','running') GROUP BY status"):
                result[table][state] = count
    result['unfinished_sessions'] = db.execute("SELECT count(*) FROM sessions WHERE status!='interview_finished'").fetchone()[0]
    return result


def is_busy(counts):
    return any(counts[table][state] for table in ('jobs', 'research_runs') for state in ('queued', 'running'))


def read_counts():
    db = connect_database(readonly=True)
    try:
        db.execute('BEGIN')
        return pending_counts(db)
    finally:
        db.close()


def status(port):
    current = health(port)
    target = build_identity()
    result = {'target': target, 'running': current, 'maintenance_active': maintenance_active(DATA),
              'pending': read_counts(), 'processes_verified': False}
    try:
        record = load_process_record(DATA)
        if record['port'] != port:
            raise RuntimeErrorSafe('Requested port differs from the managed process record.')
        roots = managed_processes(record, process_inventory(port), require_all=True)
        result.update(processes_verified=True, managed_process_count=len(roots))
    except RuntimeErrorSafe as error:
        result['process_error'] = str(error)
    result['update_needed'] = not current or current.get('backend_build_id') != target['backend_build_id']
    return result


def create_marker(owner, bootstrap):
    path = maintenance_path(DATA)
    if path.parent != DATA.resolve():
        raise RuntimeErrorSafe('Maintenance marker is outside the runtime directory.')
    with path.open('x', encoding='utf-8') as stream:
        json.dump({'owner': owner, 'pid': os.getpid(), 'bootstrap': bootstrap, 'project': str(ROOT),
                   'created_at': datetime.now(timezone.utc).isoformat()}, stream)


def clear_marker(owner):
    path = maintenance_path(DATA)
    try:
        if not path.is_symlink() and json.loads(path.read_text(encoding='utf-8')).get('owner') == owner:
            path.unlink()
    except FileNotFoundError:
        pass


def requests_idle(value):
    for field in ('active_requests', 'active_write_requests'):
        if type(value.get(field)) is not int or value[field] < 0:
            raise RuntimeErrorSafe('API does not expose reliable active request counters.')
    if value.get('maintenance_active') is not True:
        raise RuntimeErrorSafe('API did not acknowledge the maintenance marker.')
    return value['active_requests'] == 0 and value['active_write_requests'] == 0


def run_script(name, *arguments):
    powershell = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    # Start-Process descendants can retain inherited handles after PowerShell
    # exits. Regular files let us wait for the script itself, without waiting
    # for PIPE EOF from long-lived API/worker processes (including on timeout).
    with tempfile.TemporaryFile(mode='w+b') as output, tempfile.TemporaryFile(mode='w+b') as errors:
        result = subprocess.run([str(powershell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
            '-File', str(ROOT / name), *arguments], stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
            timeout=150, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        output.seek(0)
        captured = output.read(64 * 1024)
    helper = None
    for line in captured.decode('utf-8-sig', errors='replace').splitlines():
        try:
            value = json.loads(line)
        except (ValueError, TypeError):
            continue
        if isinstance(value, dict):
            helper = value
    if result.returncode:
        if (helper and helper.get('ok') is False and helper.get('error_type') == 'RuntimeErrorSafe'
                and isinstance(helper.get('error'), str) and len(helper['error']) <= 500):
            raise RuntimeErrorSafe(name + ' failed: ' + helper['error'], diagnostics={
                'script_exit_code': result.returncode,
                **safe_stop_diagnostics(helper.get('diagnostics'))})
        raise RuntimeErrorSafe(name + ' failed; existing runtime logs and process record are retained.',
                               diagnostics={'script_exit_code': result.returncode})
    return safe_stop_diagnostics(helper)


def safe_stop_diagnostics(value):
    if not isinstance(value, dict) or not isinstance(value.get('stop_results'), list):
        return {}
    fields = {'pid', 'exit_code', 'verified_tree_size', 'remaining_count', 'identity_changed_count', 'tree_exited'}
    return {'stop_results': [{key: entry[key] for key in fields if key in entry and type(entry[key]) in (int, bool)}
                             for entry in value['stop_results'] if isinstance(entry, dict)]}


def apply(port, *, wait_seconds=0, bootstrap=False):
    owner = uuid.uuid4().hex
    marker_owned = False
    stop_started = False
    deadline = time.monotonic() + wait_seconds
    evidence = {'schema_version': 'interview-runtime-update.v1', 'started_at': datetime.now(timezone.utc).isoformat(),
                'target': build_identity(), 'bootstrap': bootstrap, 'status': 'failed', 'events': []}
    try:
        if maintenance_active(DATA):
            raise RuntimeErrorSafe('Another maintenance marker exists; it was not removed.')
        current = health(port)
        if current is None:
            raise RuntimeErrorSafe('Start or diagnose the existing API before applying an update.')
        evidence['before'] = current
        supported = current.get('maintenance_supported') is True
        if not supported and not bootstrap:
            raise RuntimeErrorSafe('Legacy API has no maintenance gate. Explicit first-time --bootstrap is required.')
        if bootstrap and supported:
            raise RuntimeErrorSafe('Bootstrap is only for a legacy API without a maintenance gate.')
        record = load_process_record(DATA)
        if record['port'] != port:
            raise RuntimeErrorSafe('Requested port differs from the managed process record.')
        managed_processes(record, process_inventory(port), allow_legacy=bootstrap, require_all=True)
        while True:
            counts = read_counts()
            evidence['pending'] = counts
            if bootstrap and counts['unfinished_sessions']:
                raise RuntimeErrorSafe('Bootstrap requires every existing session to be finished.')
            if is_busy(counts):
                if time.monotonic() >= deadline:
                    raise RuntimeErrorSafe('Queued or running jobs exist; nothing was stopped or retried.')
                time.sleep(min(1, max(0, deadline - time.monotonic())))
                continue
            create_marker(owner, bootstrap)
            marker_owned = True
            if supported:
                while True:
                    observed = health(port)
                    if observed is None:
                        raise RuntimeErrorSafe('API became unavailable before the protected stop.')
                    if requests_idle(observed):
                        break
                    if time.monotonic() >= deadline:
                        raise RuntimeErrorSafe('API requests are still active; nothing was stopped.')
                    time.sleep(min(.25, max(0, deadline - time.monotonic())))
            db = connect_database()
            retry = False
            try:
                # Keep the same write lock from the final busy check until all
                # verified old processes are gone. Never lock while waiting for
                # jobs to finish: their heartbeats/writebacks need this lock.
                db.execute('BEGIN IMMEDIATE')
                counts = pending_counts(db)
                evidence['pending'] = counts
                if bootstrap and counts['unfinished_sessions']:
                    raise RuntimeErrorSafe('A session became unfinished; bootstrap was refused.')
                if is_busy(counts):
                    if time.monotonic() >= deadline:
                        raise RuntimeErrorSafe('A task appeared during preflight; nothing was stopped.')
                    retry = True
                else:
                    if build_identity() != evidence['target']:
                        raise RuntimeErrorSafe('Source files changed during update; retry after editing is finished.')
                    stop_started = True
                    evidence['events'].append('stop_started')
                    stopped = run_script('stop-interview.ps1', *(['-BootstrapRecord'] if bootstrap else []))
                    if isinstance(stopped, dict):
                        evidence.update(stopped)
                    evidence['events'].append('old_processes_stopped_under_database_lock')
            finally:
                db.rollback()
                db.close()
            if not retry:
                break
            clear_marker(owner)
            marker_owned = False
        run_script('start-interview.ps1', '-Port', str(port), '-MaintenanceOwner', owner)
        current = health(port)
        if (current is None or current.get('backend_build_id') != evidence['target']['backend_build_id']
                or current.get('maintenance_supported') is not True or not requests_idle(current)):
            raise RuntimeErrorSafe('New API did not confirm the expected build and maintenance gate.')
        record = load_process_record(DATA)
        roots = managed_processes(record, process_inventory(port), require_all=True)
        if record.get('backend_build_id') != evidence['target']['backend_build_id']:
            raise RuntimeErrorSafe('Managed process record has a different build identity.')
        evidence.update(status='succeeded', after=current, managed_process_count=len(roots))
    except Exception as error:
        evidence['error'] = str(error) if isinstance(error, RuntimeErrorSafe) else type(error).__name__
        if isinstance(error, RuntimeErrorSafe) and error.diagnostics:
            evidence['diagnostics'] = error.diagnostics
    finally:
        if marker_owned and (evidence['status'] == 'succeeded' or not stop_started):
            try:
                clear_marker(owner)
            except Exception:
                evidence.update(status='failed', marker_cleanup_failed=True)
        elif marker_owned:
            evidence['recovery_required'] = True
            evidence['maintenance_retained'] = True
        evidence['finished_at'] = datetime.now(timezone.utc).isoformat()
        folder = ROOT / 'local-data/runtime-updates'
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + owner[:8] + '.json')
        path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        evidence['evidence_file'] = str(path)
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('status', 'apply'), nargs='?', default='status')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--wait-seconds', type=int, default=0)
    parser.add_argument('--bootstrap', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 0 <= args.wait_seconds <= 600:
        parser.error('Port must be 1..65535 and wait-seconds must be 0..600.')
    if args.command == 'status' and (args.bootstrap or args.wait_seconds):
        parser.error('Bootstrap and waiting apply only to the apply command.')
    try:
        result = status(args.port) if args.command == 'status' else apply(args.port, wait_seconds=args.wait_seconds, bootstrap=args.bootstrap)
    except Exception as error:
        result = {'status': 'failed', 'error': str(error) if isinstance(error, RuntimeErrorSafe) else type(error).__name__}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get('status') == 'failed' else 0


if __name__ == '__main__':
    raise SystemExit(main())
