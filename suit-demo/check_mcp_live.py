"""Real STDIO checks; read-only by default, optional explicit synthetic creation.

Never emits report contents, recovery keys or capabilities. The write smoke
creates only a labelled empty QA session and retries its same creation request.
"""
import argparse
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent


def check(base_url='http://127.0.0.1:8766', session_id=None, create_simulation=False):
    process = subprocess.Popen([sys.executable, str(ROOT / 'interview_mcp.py'), '--base-url', base_url],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    lines = queue.Queue()
    def read_lines():
        for line in process.stdout:
            lines.put(line)
    thread = threading.Thread(target=read_lines, daemon=True)
    thread.start()
    counter = 0
    invoked = []
    def call(method, params=None):
        nonlocal counter
        counter += 1
        request = {'jsonrpc': '2.0', 'id': counter, 'method': method, 'params': params or {}}
        process.stdin.write((json.dumps(request, ensure_ascii=False) + '\n').encode('utf-8'))
        process.stdin.flush()
        value = json.loads(lines.get(timeout=45))
        if value.get('id') != counter or 'error' in value or value.get('result', {}).get('isError'):
            raise RuntimeError('MCP protocol/tool check failed; no payload was printed.')
        return value['result']
    def tool(name, arguments=None):
        invoked.append(name)
        return call('tools/call', {'name': name, 'arguments': arguments or {}})['structuredContent']
    try:
        initialized = call('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                           'clientInfo': {'name': 'zhenjian-delivery-smoke', 'version': '1'}})
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.stdin.flush()
        tools = call('tools/list')['tools']
        status = tool('status')
        if not status.get('authenticated'):
            raise RuntimeError('MCP local authentication unavailable; run as the configured Windows user.')
        sessions = tool('list_sessions')['sessions']
        creation = {'checked': False}
        if create_simulation:
            args = {'label': 'QA-STDIO-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ'),
                    'request_id': str(uuid.uuid4()), 'confirm_create': True}
            first = tool('create_simulation_session', args)
            second = tool('create_simulation_session', args)
            if not first.get('session_id') or first['session_id'] != second.get('session_id'):
                raise RuntimeError('Synthetic creation idempotency did not preserve one session.')
            state = tool('get_session', {'session_id': first['session_id']})
            if state.get('session_id') != first['session_id'] or not str(state.get('candidate_ref', '')).startswith('SIM-MCP-QA-STDIO-'):
                raise RuntimeError('Synthetic creation returned an unexpected session.')
            if not tool('status').get('authenticated'):
                raise RuntimeError('Local authentication check failed after synthetic creation.')
            creation = {'checked': True, 'synthetic_session_id': first['session_id'],
                        'synthetic_label': state['candidate_ref'], 'same_request_same_session': True,
                        'get_session_checked': True, 'empty_session_only': not state.get('answers')}
        selected = next((s for s in sessions if s['session_id'] == session_id), None) if session_id else next(
            (s for s in sessions if str(s.get('candidate_ref', '')).startswith(('SIM-', 'synthetic-', 'delivery-synthetic-', '验收合成'))), None)
        report_result = {'checked': False, 'reason': 'No explicitly marked synthetic session found.'}
        if selected:
            # Explicit --session still must select a declared synthetic label.
            label = str(selected.get('candidate_ref', ''))
            if not label.startswith(('SIM-', 'synthetic-', 'delivery-synthetic-', '验收合成')):
                raise RuntimeError('Report smoke check only accepts an explicitly marked synthetic session.')
            report = tool('get_report', {'session_id': selected['session_id']})
            report_result = {'checked': True, 'synthetic_session_id': selected['session_id'],
                             'report_version': report.get('version'), 'has_questions': bool(report.get('questions'))}
        elif session_id:
            raise RuntimeError('Requested synthetic session does not exist.')
        return {'schema_version': 'mcp-live-check.v2', 'checked_at': datetime.now(timezone.utc).isoformat(),
                'mode': 'confirmed_synthetic_creation' if create_simulation else 'read_only',
                'protocol': initialized['protocolVersion'],
                'tool_count': len(tools), 'authenticated': True, 'session_count': len(sessions),
                'report': report_result, 'paid_cloud_tools_exposed': status['paid_cloud_tools_exposed'],
                'creation': creation, 'tools_invoked': sorted(set(invoked)),
                'mutating_tool_calls': 2 if create_simulation else 0, 'unique_sessions_created': 1 if create_simulation else 0,
                'cloud_calls': 0, 'credentials_output': False,
                'passed': report_result['checked'] and (not create_simulation or creation['checked'])}
    finally:
        process.stdin.close()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()
        thread.join(timeout=2)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:8766')
    parser.add_argument('--session')
    parser.add_argument('--create-simulation', action='store_true', help='Explicitly create one empty SIM-MCP-QA session and retry its same request; never queues analysis.')
    args = parser.parse_args()
    try:
        result = check(args.base_url, args.session, args.create_simulation)
        output = ROOT / 'local-data/interview-validation' / ('mcp-live-write-check.json' if args.create_simulation else 'mcp-live-check.json')
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        print(json.dumps({'passed': False, 'error': 'MCP smoke check did not complete. No response payload or credentials were printed.'}))
        raise SystemExit(1)
