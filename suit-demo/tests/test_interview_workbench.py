"""Synthetic fixtures for operator capabilities, scoped authorization and recovery."""
import contextlib
import asyncio
import importlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_core import BASE, DomainError, InterviewCore
import interview_workbench


def key():
    return str(uuid.uuid4())


class ASGIClient:
    """Exercise the entire ASGI app without an optional HTTP client dependency."""
    def __init__(self, app):
        self.app = app

    def request(self, method, path, headers=None, json=None, content=None):
        async def run():
            target = urlsplit(path)
            body = globals()['json'].dumps(json).encode() if json is not None else content or b''
            request_headers = {'host': '127.0.0.1', **(headers or {})}
            if json is not None:
                request_headers.update({'content-type': 'application/json', 'content-length': str(len(body))})
            sent = False
            messages = []

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type': 'http.request', 'body': body, 'more_body': False}
                await asyncio.Event().wait()

            async def send(message):
                messages.append(message)

            scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                     'http_version': '1.1', 'method': method, 'scheme': 'http',
                     'path': target.path, 'raw_path': target.path.encode(), 'query_string': target.query.encode(), 'root_path': '',
                     'headers': [(k.lower().encode(), v.encode()) for k, v in request_headers.items()],
                     'client': ('127.0.0.1', 12345), 'server': ('127.0.0.1', 80)}
            await self.app(scope, receive, send)
            start = next(m for m in messages if m['type'] == 'http.response.start')
            response_bytes = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
            response_text = response_bytes.decode(errors='replace')
            return type('Response', (), {'status_code': start['status'], 'text': response_text, 'content': response_bytes,
                                         'headers': {k.decode(): v.decode() for k, v in start['headers']},
                                         'json': lambda self: globals()['json'].loads(self.text)})()
        return asyncio.run(run())

    def get(self, path, **kwargs):
        return self.request('GET', path, **kwargs)

    def post(self, path, **kwargs):
        return self.request('POST', path, **kwargs)

    def put(self, path, **kwargs):
        return self.request('PUT', path, **kwargs)


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {
            'INTERVIEW_DATA_DIR': self.directory.name,
            'INTERVIEW_CREDENTIALS_FILE': str(Path(self.directory.name) / 'absent'),
            'DASHSCOPE_API_KEY': 'synthetic-test-key',
            'INTERVIEW_LLM_BASE_URL': 'https://fixture.invalid/compatible-mode/v1',
            'INTERVIEW_LLM_MODEL': 'synthetic-model',
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.core = InterviewCore(self.directory.name, config_path=BASE / 'config' / 'interview.v1.json')
        self.entry = importlib.import_module('interview_entry')
        self.core_patch = patch.object(self.entry, 'core', self.core)
        self.core_patch.start()
        self.addCleanup(self.core_patch.stop)
        self.operator = 'synthetic-operator-capability-distinct-from-session-tokens'
        self.token_patch = patch('interview_workbench.load_operator_token', return_value=self.operator)
        self.token_patch.start()
        self.addCleanup(self.token_patch.stop)
        self.client = ASGIClient(self.entry.app)
        self.created = self.core.create_interview(key(), 'synthetic-candidate')
        self.sid = self.created['session_id']
        self.other = self.core.create_interview(key(), 'synthetic-other')

    def headers(self, token=None):
        return {'Authorization': 'Bearer ' + (token or self.operator)}

    def answer(self, created=None):
        created = created or self.created
        return self.core.submit_answer(created['session_id'], created['current_round']['round_id'], key(), key(), '合成测试回答，仅用于权限与幂等验证。')

    def analyze(self, sid=None, **body):
        return self.client.post(f'/v1/workbench/sessions/{sid or self.sid}/analyze', json=body or {'idempotency_key': key(), 'reason': '明确授权合成测试本场内容草稿'}, headers=self.headers())

    def test_missing_and_session_tokens_cannot_list_open_or_authorize(self):
        endpoints = [('get', '/v1/workbench/sessions', None),
                     ('post', f'/v1/workbench/sessions/{self.sid}/open', None),
                     ('post', f'/v1/workbench/sessions/{self.sid}/analyze', {'idempotency_key': key(), 'reason': 'test'})]
        for method, url, body in endpoints:
            options = {'json': body} if body else {}
            self.assertEqual(getattr(self.client, method)(url, **options).status_code, 401)
            for token in (self.created['candidate_token'], self.created['hr_token'], 'wrong'):
                self.assertEqual(getattr(self.client, method)(url, headers=self.headers(token), **options).status_code, 403)
        self.assertFalse(self.core.cloud_analysis_authorized(self.sid))

    def test_operator_list_is_read_only_and_contains_no_role_credentials(self):
        self.answer()
        with self.core._db() as db:
            before = db.execute('SELECT COUNT(*) FROM reports').fetchone()[0]
        response = self.client.get('/v1/workbench/sessions', headers=self.headers())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['total'], 2)
        item = next(s for s in data['sessions'] if s['session_id'] == self.sid)
        self.assertEqual(item['answer_count'], 1)
        self.assertEqual(item['content_status'], 'queued')
        for forbidden in ('transcript', self.created['candidate_token'], self.created['hr_token'], self.operator):
            self.assertNotIn(forbidden, response.text)
        with self.core._db() as db:
            self.assertEqual(before, db.execute('SELECT COUNT(*) FROM reports').fetchone()[0])

    def test_operator_recovers_existing_roles_without_changing_creation_contract(self):
        result = self.client.post(f'/v1/workbench/sessions/{self.sid}/open', headers=self.headers()).json()
        self.assertEqual(result['candidate_url'], self.created['candidate_url'])
        self.assertEqual(result['hr_url'], self.created['report_url'])
        self.assertTrue(result['report_url'].startswith('/interview-report#'))
        self.assertEqual(self.client.get(f'/v1/interviews/{self.other["session_id"]}/report', headers=self.headers(self.created['hr_token'])).status_code, 403)
        self.assertEqual(self.client.get(f'/v1/interviews/{self.sid}/report', headers=self.headers(self.created['candidate_token'])).status_code, 403)
        self.assertEqual(self.client.get(f'/v1/interviews/{self.sid}/report', headers=self.headers()).status_code, 403)

    def test_analyze_only_requeues_current_session_and_is_idempotent(self):
        first = self.answer()
        self.answer(self.other)
        # Preserve an obsolete job to prove it is not retried.
        self.core.revise_transcript(self.sid, first['answer_id'], 1, '当前合成文字版本。', '合成修订', key())
        with self.core._db(True) as db:
            db.execute("UPDATE jobs SET status='blocked' WHERE job_type='content'")
        request_key = key()
        body = {'idempotency_key': request_key, 'reason': '本场当前文字明确授权'}
        first_response = self.analyze(**body)
        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json()['requeued'], 1)
        self.assertEqual(self.analyze(**body).json(), first_response.json())
        self.assertEqual(self.analyze(idempotency_key=request_key, reason='changed').status_code, 409)
        self.assertTrue(self.core.cloud_analysis_authorized(self.sid))
        self.assertFalse(self.core.cloud_analysis_authorized(self.other['session_id']))
        with self.core._db() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM cloud_authorizations').fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE session_id=? AND status='blocked'", (self.sid,)).fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE session_id=?", (self.other['session_id'],)).fetchone()[0], 'blocked')
        self.core.revise_transcript(self.sid, first['answer_id'], 2, '再次合成修订。', '修订需新授权', key())
        self.assertFalse(self.core.cloud_analysis_authorized(self.sid))

    def test_authorization_does_not_retry_successful_jobs_or_change_sealed_decisions(self):
        with patch('interview_core.credential_available', return_value=False):
            for _ in range(3):
                state = self.core.get_state(self.sid)
                self.core.submit_answer(self.sid, state['current_round']['round_id'], key(), key(), '合成回答。')
                self.core.next_round(self.sid, key(), state['current_round']['round_id'])
        before = self.core.get_report(self.sid)['decisions']
        with self.core._db(True) as db:
            db.execute("UPDATE jobs SET status='succeeded' WHERE session_id=?", (self.sid,))
        self.assertEqual(self.analyze().json()['requeued'], 0)
        self.assertEqual(self.core.get_report(self.sid)['decisions'], before)

    def test_analyze_rejects_blank_text_unconfigured_provider_and_snapshot_changes(self):
        self.assertEqual(self.analyze().status_code, 422)
        self.answer()
        with patch('interview_llm.provider_status', return_value={'status': 'not_configured'}):
            self.assertEqual(self.analyze().json()['detail']['code'], 'provider_not_configured')
        with patch.dict(os.environ, {'INTERVIEW_LLM_BASE_URL': 'https://other.invalid/v1'}):
            self.assertEqual(self.analyze().json()['detail']['code'], 'model_configuration_changed')
        self.assertFalse(self.core.cloud_analysis_authorized(self.sid))

    def test_public_pages_and_static_cannot_expose_operator_secret(self):
        for path in ('/interview-report', '/static/js/interview-report.js'):
            self.assertEqual(self.client.get(path).status_code, 200)
        for path in ('/static/.secrets-user/workbench-token.dpapi', '/static/interview_workbench.py', '/static/../interview-data/interviews.sqlite3'):
            self.assertEqual(self.client.get(path).status_code, 404)
        self.assertEqual(self.client.get('/v1/workbench/sessions', headers={**self.headers(), 'Origin': 'https://external.invalid'}).status_code, 403)

    def test_legacy_report_created_at_is_added_without_rewriting_snapshot(self):
        report = self.core.get_report(self.sid)
        created_at = report.pop('created_at')
        raw = json.dumps(report)
        with self.core._db(True) as db:
            db.execute('UPDATE reports SET payload=? WHERE session_id=? AND version=?', (raw, self.sid, report['version']))
        self.assertEqual(self.core.get_report(self.sid)['created_at'], created_at)
        self.assertEqual(self.core.get_report(self.sid, report['version'])['created_at'], created_at)
        with self.core._db() as db:
            self.assertEqual(db.execute('SELECT payload FROM reports WHERE session_id=? AND version=?', (self.sid, report['version'])).fetchone()[0], raw)

    def test_cli_opens_fragment_without_printing_secret(self):
        stream = io.StringIO()
        with patch.object(sys, 'argv', ['interview_workbench.py', 'open', '--session', self.sid]), \
             patch.object(interview_workbench, 'ensure_operator_token', return_value=self.operator), \
             patch.object(interview_workbench.webbrowser, 'open', return_value=True) as opened, \
             contextlib.redirect_stdout(stream):
            self.assertEqual(interview_workbench.main(), 0)
        self.assertNotIn(self.operator, stream.getvalue())
        self.assertIn('/workbench#access=', opened.call_args[0][0])
        self.assertIn('session=' + self.sid, opened.call_args[0][0])
        with self.assertRaises(ValueError):
            interview_workbench.workbench_url('https://external.invalid', self.operator)


if __name__ == '__main__':
    unittest.main()
