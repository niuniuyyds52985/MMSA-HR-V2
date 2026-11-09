"""Maintenance admission and draining, using only isolated synthetic stores."""
import asyncio
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_core import BASE, InterviewCore
from interview_runtime import maintenance_path


def http_scope(method='GET', path='/v1/test', headers=()):
    return {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
            'http_version': '1.1', 'method': method, 'scheme': 'http',
            'path': path, 'raw_path': path.encode(), 'query_string': b'', 'root_path': '',
            'headers': [(b'host', b'127.0.0.1'), *headers],
            'client': ('127.0.0.1', 12345), 'server': ('127.0.0.1', 80)}


async def request(app, method='GET', path='/v1/test', body=b'', headers=(), send_hook=None):
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
        if send_hook:
            await send_hook(message)

    await app(http_scope(method, path, headers), receive, send)
    start = next(m for m in messages if m['type'] == 'http.response.start')
    payload = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
    return start['status'], dict(start['headers']), payload


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        env = patch.dict(os.environ, {
            'INTERVIEW_DATA_DIR': directory.name,
            'INTERVIEW_CREDENTIALS_FILE': str(self.directory / 'absent'),
            'DASHSCOPE_API_KEY': 'synthetic-test-key',
            'INTERVIEW_LLM_BASE_URL': 'https://fixture.invalid/compatible-mode/v1',
            'INTERVIEW_LLM_MODEL': 'synthetic-model',
        })
        env.start()
        self.addCleanup(env.stop)
        self.core = InterviewCore(directory.name, config_path=BASE / 'config/interview.v1.json')
        self.entry = importlib.import_module('interview_entry')
        for name, value in [('core', self.core), ('maintenance_state', self.entry.MaintenanceState())]:
            replacement = patch.object(self.entry, name, value)
            replacement.start()
            self.addCleanup(replacement.stop)
        self.state = self.entry.maintenance_state

    def mark(self):
        # Even an interrupted/invalid marker must block new work.
        maintenance_path(self.directory).write_text('{', encoding='utf-8')

    def database_snapshot(self):
        with self.core._db() as db:
            return '\n'.join(db.iterdump())

    def assert_idle(self):
        self.assertEqual(self.state.active_requests, 0)
        self.assertEqual(self.state.active_write_requests, 0)

    def test_new_interview_is_rejected_without_database_changes(self):
        before = self.database_snapshot()
        self.mark()
        with patch.object(self.core, 'create_interview', side_effect=AssertionError('must not execute')):
            status, headers, body = asyncio.run(request(self.entry.app, 'POST', '/v1/interviews',
                json.dumps({'idempotency_key': str(uuid.uuid4())}).encode(),
                [(b'content-type', b'application/json')]))
        self.assertEqual(status, 503)
        self.assertEqual(headers[b'retry-after'], b'3')
        self.assertEqual(json.loads(body)['detail']['code'], 'maintenance')
        self.assertEqual(before, self.database_snapshot())
        self.assert_idle()

    def test_get_report_is_gated_before_cache_or_database_writes(self):
        created = self.core.create_interview(str(uuid.uuid4()), 'synthetic-maintenance')
        before = self.database_snapshot()
        self.mark()
        with patch.object(self.core, 'get_report', side_effect=AssertionError('must not execute')):
            for suffix in ('report', 'report.pdf'):
                with self.subTest(suffix=suffix):
                    status, _, _ = asyncio.run(request(self.entry.app, path=
                        '/v1/interviews/' + created['session_id'] + '/' + suffix,
                        headers=[(b'authorization', ('Bearer ' + created['hr_token']).encode())]))
                    self.assertEqual(status, 503)
        self.assertEqual(before, self.database_snapshot())
        self.assert_idle()

    def test_health_stays_readable_and_reports_import_time_build(self):
        frozen = dict(self.entry.BUILD_ID)
        self.mark()
        with patch.object(self.entry, 'build_identity', side_effect=AssertionError('must not recalculate')):
            status, _, body = asyncio.run(request(self.entry.app, path='/health'))
        self.assertEqual(status, 200)
        value = json.loads(body)
        for key, expected in frozen.items():
            self.assertEqual(value[key], expected)
        self.assertTrue(value['maintenance_active'])
        self.assertEqual(value['active_requests'], 0)
        self.assertEqual(value['active_write_requests'], 0)
        self.assert_idle()

    def test_non_api_write_is_gated_but_static_get_is_available(self):
        self.mark()

        async def downstream(scope, receive, send):
            await send({'type': 'http.response.start', 'status': 204, 'headers': []})
            await send({'type': 'http.response.body', 'body': b''})

        gate = self.entry.MaintenanceGate(downstream)
        for method, path, expected in [('PATCH', '/outside-api', 503), ('GET', '/interview', 204),
                                       ('HEAD', '/interview', 204), ('GET', '/v1/workbench/sessions', 503)]:
            with self.subTest(method=method, path=path):
                self.assertEqual(asyncio.run(request(gate, method, path))[0], expected)
        self.assert_idle()

    def test_inflight_response_is_counted_until_final_body_send_completes(self):
        async def scenario(method):
            final_body_entered = asyncio.Event()
            release = asyncio.Event()

            async def downstream(scope, receive, send):
                await send({'type': 'http.response.start', 'status': 200, 'headers': []})
                await send({'type': 'http.response.body', 'body': b'first', 'more_body': True})
                await send({'type': 'http.response.body', 'body': b'last', 'more_body': False})

            async def slow_send(message):
                if message['type'] == 'http.response.body' and not message.get('more_body', False):
                    final_body_entered.set()
                    await release.wait()

            gate = self.entry.MaintenanceGate(downstream)
            task = asyncio.create_task(request(gate, method, send_hook=slow_send))
            try:
                await asyncio.wait_for(final_body_entered.wait(), 2)
                self.assertEqual(self.state.active_requests, 1)
                self.assertEqual(self.state.active_write_requests, int(method == 'POST'))
                self.mark()
                self.assertEqual((await request(gate, method))[0], 503)
                self.assertEqual(self.state.active_requests, 1)
            finally:
                release.set()
                await asyncio.wait_for(task, 2)
            self.assert_idle()

        for method in ('GET', 'POST'):
            with self.subTest(method=method):
                maintenance_path(self.directory).unlink(missing_ok=True)
                asyncio.run(scenario(method))

    def test_real_middleware_stack_counts_until_client_response_completes(self):
        async def scenario():
            final_body_entered = asyncio.Event()
            release = asyncio.Event()

            async def slow_send(message):
                if message['type'] == 'http.response.body' and not message.get('more_body', False):
                    final_body_entered.set()
                    await release.wait()

            # A gated unknown route exercises the production middleware without
            # auth, models, or endpoint writes; the transport delays its last send.
            task = asyncio.create_task(request(self.entry.app, path='/v1/nonexistent-maintenance-fixture',
                                               send_hook=slow_send))
            try:
                await asyncio.wait_for(final_body_entered.wait(), 2)
                # Let the inner application finish if a middleware buffered it.
                for _ in range(5):
                    await asyncio.sleep(0)
                self.assertEqual(self.state.active_requests, 1)
            finally:
                release.set()
                await asyncio.wait_for(task, 2)
            self.assert_idle()

        asyncio.run(scenario())

    def test_downstream_failure_and_cancellation_release_counters(self):
        async def failing(scope, receive, send):
            self.assertEqual(self.state.active_requests, 1)
            self.assertEqual(self.state.active_write_requests, 1)
            raise ValueError('synthetic failure')

        with self.assertRaisesRegex(ValueError, 'synthetic failure'):
            asyncio.run(request(self.entry.MaintenanceGate(failing), 'POST'))
        self.assert_idle()

        async def scenario():
            started = asyncio.Event()

            async def waiting(scope, receive, send):
                started.set()
                await asyncio.Event().wait()

            task = asyncio.create_task(request(self.entry.MaintenanceGate(waiting), 'POST'))
            await asyncio.wait_for(started.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assert_idle()

        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
