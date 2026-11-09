import copy
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_mcp import MCPServer, WorkbenchClient, ToolError, TOOLS, safe_output


class FixtureHandler(BaseHTTPRequestHandler):
    calls = []
    report_payload = None

    def log_message(self, *args):
        pass

    def respond(self, payload, status=200):
        raw = json.dumps(payload).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.calls.append(('GET', self.path, None))
        if self.path == '/health':
            return self.respond({'service': 'zhenjian-interview', 'ok': True})
        if self.path == '/v1/workbench/sessions':
            if self.headers.get('Authorization') != 'Bearer fixture-operator':
                return self.respond({}, 403)
            return self.respond({'sessions': [{'session_id': 's1', 'version': 2, 'candidate_ref': 'SIM-fixture'}]})
        if self.headers.get('Authorization') != 'Bearer fixture-hr-private':
            return self.respond({}, 403)
        if self.path == '/v1/interviews/s1':
            return self.respond({'session_id': 's1', 'version': 2, 'answers': []})
        if self.path == '/v1/interviews/s1/analysis-status':
            return self.respond({'session_id': 's1', 'version': 2, 'reviewed_question_count': 1,
                                 'multimodal': {'paused': False, 'runs': []}})
        if self.path.startswith('/v1/interviews/s1/report'):
            return self.respond(self.report_payload if self.report_payload is not None else {
                'session_id': 's1', 'version': 2, 'total': None, 'token': 'fixture-hr-private',
                'status': 'answering', 'report_completeness': 'incomplete', 'report_lifecycle': 'pending',
                'questions': [{'question_id': 'q1', 'score': 4},
                              {'question_id': 'q2', 'score': None}, {'question_id': 'q3', 'score': None}]})
        if self.path == '/v1/interviews/s1/research/runs':
            return self.respond({'runs': [{'run_id': 'r1', 'status': 'succeeded'}]})
        if self.path == '/v1/interviews/s1/research/runs/r1':
            return self.respond({'run_id': 'r1', 'status': 'succeeded', 'input': {'_media_path': 'private-media'}})
        if self.path == '/v1/interviews/s1/research/export':
            return self.respond({'runs': [], 'contains_raw_media': False})
        return self.respond({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length', 0))) or b'{}')
        self.calls.append(('POST', self.path, body))
        if self.path == '/v1/workbench/sessions/s1/open':
            if self.headers.get('Authorization') != 'Bearer fixture-operator':
                return self.respond({}, 403)
            return self.respond({'hr_url': '/hr-review#session=s1&token=fixture-hr-private'})
        if self.path == '/v1/interviews':
            return self.respond({'session_id': 's2', 'candidate_ref': body['candidate_ref'], 'hr_token': 'fixture-hr-private'})
        if self.path == '/v1/interviews/s1/research/runs':
            return self.respond({'runs': [{'run_id': 'r2', 'status': 'queued'}]})
        return self.respond({}, 404)


class ConnectorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = 'http://127.0.0.1:' + str(cls.server.server_port)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=3)

    def setUp(self):
        FixtureHandler.calls.clear()
        self.temp = tempfile.TemporaryDirectory()
        self.client = WorkbenchClient(self.url, lambda: 'fixture-operator', self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_status_and_list_require_local_operator_not_exposed(self):
        self.assertTrue(self.client.call('status', {})['authenticated'])
        self.assertEqual(self.client.call('list_sessions', {})['sessions'][0]['session_id'], 's1')
        failed = WorkbenchClient(self.url, lambda: 'wrong')
        self.assertFalse(failed.call('status', {})['authenticated'])
        with self.assertRaises(ToolError):
            failed.call('list_sessions', {})

    def test_read_tools_use_same_api_and_never_return_capability(self):
        cases = [('get_session', {'session_id': 's1'}), ('get_report', {'session_id': 's1', 'version': 1}),
                 ('get_analysis_status', {'session_id': 's1'}), ('list_research_runs', {'session_id': 's1'}), ('get_research_run', {'session_id': 's1', 'run_id': 'r1'})]
        for name, args in cases:
            output = json.dumps(self.client.call(name, args))
            self.assertNotIn('fixture-hr-private', output)
            self.assertNotIn('private-media', output)
        self.assertTrue(any(c[1].endswith('/report?compact=true&version=1') for c in FixtureHandler.calls))

    def test_compact_is_optional_and_status_is_read_only(self):
        self.client.call('get_report', {'session_id': 's1', 'compact': False})
        self.assertTrue(FixtureHandler.calls[-1][1].endswith('/report?compact=false'))
        result = self.client.call('get_analysis_status', {'session_id': 's1'})
        self.assertEqual(result['reviewed_question_count'], 1)
        self.assertEqual(FixtureHandler.calls[-1][:2], ('GET', '/v1/interviews/s1/analysis-status'))
        self.assertTrue(next(t for t in TOOLS if t['name'] == 'get_analysis_status')['annotations']['readOnlyHint'])

    def test_report_summary_is_bounded_and_counts_question_scores_not_answer_jobs(self):
        # Reproduce the real shape: old main-answer jobs may fail or become
        # superseded while a main+followup evaluation supplies the valid score.
        large = 'synthetic-evidence-must-not-escape-summary-' * 2000
        report = {'session_id': 's1', 'version': 58, 'status': 'interview_finished',
            'report_completeness': 'incomplete', 'report_lifecycle': 'draft',
            'main_question_count': 999, 'reader_summary': {'scorable_main_count': 999},
            'questions': [{'question_id': 'q' + str(i), 'score': score,
                           'answers': [{'content_status': 'failed', 'transcript': large},
                                       {'content_status': 'succeeded', 'transcript': large}]}
                          for i, score in enumerate((4, 5, 5, None, None, 5, 5, 4))],
            'answers': [{'content_status': 'failed', 'transcript': large}],
            'decisions': [large], 'decision_events': [large], 'model_evaluations': [large],
            'potential_conclusion': {'large_fixture': large}, 'token': 'fixture-hr-private',
            'candidate_token': 'fixture-candidate-private'}
        expected_fields = {'schema_version', 'session_id', 'version', 'main_question_count',
            'scored_main_question_count', 'report_completeness', 'report_lifecycle', 'interview_status'}
        server = MCPServer(self.client)
        server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
        with patch.object(FixtureHandler, 'report_payload', report):
            response = server.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'get_report', 'arguments': {
                    'session_id': 's1', 'version': 58, 'summary_only': True}}})
        self.assertFalse(response['result']['isError'])
        summary = response['result']['structuredContent']
        self.assertEqual(set(summary), expected_fields)
        self.assertTrue(summary['schema_version'])
        self.assertEqual(summary['version'], 58)
        self.assertEqual(summary['main_question_count'], 8)
        self.assertEqual(summary['scored_main_question_count'], 6)
        self.assertEqual(summary['interview_status'], 'interview_finished')
        self.assertEqual(summary['report_completeness'], 'incomplete')
        self.assertEqual(summary['report_lifecycle'], 'draft')
        self.assertEqual(json.loads(response['result']['content'][0]['text']), summary)
        # The complete duplicated MCP envelope also fits, so host users do not
        # need a shell or large-file extraction just to read eight scalar fields.
        serialized = json.dumps(response, ensure_ascii=False).encode('utf-8')
        self.assertLess(len(serialized), 2048)
        self.assertNotIn(b'fixture-hr-private', serialized)
        self.assertNotIn(b'fixture-candidate-private', serialized)
        self.assertNotIn(b'synthetic-evidence-must-not-escape-summary', serialized)
        self.assertEqual(FixtureHandler.calls, [
            ('POST', '/v1/workbench/sessions/s1/open', {}),
            ('GET', '/v1/interviews/s1/report?compact=true&version=58', None)])

    def test_summary_only_false_preserves_default_detailed_response(self):
        default = self.client.call('get_report', {'session_id': 's1'})
        explicit = self.client.call('get_report', {'session_id': 's1', 'summary_only': False})
        self.assertEqual(default, explicit)
        self.assertIn('questions', default)
        self.assertIn('total', default)
        self.assertNotIn('scored_main_question_count', default)

    def test_summary_only_requires_boolean_before_any_http(self):
        for value in (None, 0, 1, 'true', 'false', [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ToolError):
                    self.client.call('get_report', {'session_id': 's1', 'summary_only': value})
        self.assertEqual(FixtureHandler.calls, [])
        definition = next(t for t in TOOLS if t['name'] == 'get_report')
        self.assertEqual(definition['inputSchema']['properties']['summary_only']['type'], 'boolean')
        self.assertNotIn('summary_only', definition['inputSchema']['required'])
        self.assertEqual(len(TOOLS), 12)

    def test_summary_rejects_malformed_or_mismatched_report_instead_of_guessing_counts(self):
        valid = {'session_id': 's1', 'version': 58, 'status': 'interview_finished',
            'report_completeness': 'incomplete', 'report_lifecycle': 'draft',
            'questions': [{'question_id': 'q1', 'score': 4}, {'question_id': 'q2', 'score': None}]}
        cases = []
        for score in (True, False, 0, 6, 2.5, '4'):
            bad = copy.deepcopy(valid)
            bad['questions'][0]['score'] = score
            cases.append(('invalid_score_' + repr(score), bad))
        missing_score = copy.deepcopy(valid)
        del missing_score['questions'][0]['score']
        cases.append(('missing_score', missing_score))
        for field in ('session_id', 'version', 'status', 'report_completeness', 'report_lifecycle', 'questions'):
            bad = copy.deepcopy(valid)
            del bad[field]
            cases.append(('missing_' + field, bad))
        cases.extend([('wrong_session', {**valid, 'session_id': 'different-session'}),
                      ('wrong_historical_version', {**valid, 'version': 59}),
                      ('boolean_version', {**valid, 'version': True}),
                      ('invalid_questions_type', {**valid, 'questions': {}}),
                      ('invalid_question_item', {**valid, 'questions': [None]}),
                      ('empty_lifecycle', {**valid, 'report_lifecycle': ''}),
                      ('invalid_completeness_type', {**valid, 'report_completeness': {}}),
                      ('oversized_status', {**valid, 'status': 'x' * 101})])
        for name, payload in cases:
            with self.subTest(name=name), patch.object(FixtureHandler, 'report_payload', payload):
                with self.assertRaises(ToolError) as error:
                    self.client.call('get_report', {'session_id': 's1', 'version': 58, 'summary_only': True})
                self.assertEqual(error.exception.code, 'invalid_report_summary')

    def test_export_is_confined_unique_and_redacted(self):
        for fmt in ('json', 'markdown'):
            result = self.client.call('export_research_report', {'session_id': 's1', 'format': fmt})
            path = Path(result['export_file'])
            self.assertTrue(path.is_relative_to(Path(self.temp.name).resolve()))
            content = path.read_text(encoding='utf-8')
            self.assertNotIn('fixture-hr-private', content)
            self.assertFalse(result['contains_raw_media'])
        self.assertEqual(len(list(Path(self.temp.name).iterdir())), 2)

    def test_malicious_id_extra_path_and_unknown_tool_rejected_before_http(self):
        for name, args in [('get_report', {'session_id': '../s1'}), ('get_report', {'session_id': 's1', 'path': 'C:/'}),
                           ('shell', {'command': 'whoami'})]:
            with self.assertRaises(ToolError):
                self.client.call(name, args)
        self.assertEqual(FixtureHandler.calls, [])

    def test_write_requires_confirmation_and_keeps_idempotency(self):
        args = {'label': 'test', 'request_id': 'request-001', 'confirm_create': True}
        result = self.client.call('create_simulation_session', args)
        self.assertEqual(result['candidate_ref'], 'SIM-MCP-test')
        self.assertNotIn('hr_token', result)
        request = FixtureHandler.calls[-1][2]
        creation_key = request['idempotency_key']
        parsed = uuid.UUID(creation_key)
        self.assertEqual(parsed.version, 4)
        self.assertEqual(parsed.variant, uuid.RFC_4122)
        self.assertNotIn(args['request_id'], creation_key)
        self.client.call('create_simulation_session', args)
        self.assertEqual(FixtureHandler.calls[-1][2]['idempotency_key'], creation_key)
        self.client.call('create_simulation_session', {**args, 'request_id': 'request-003'})
        self.assertNotEqual(FixtureHandler.calls[-1][2]['idempotency_key'], creation_key)
        with self.assertRaises(ToolError):
            self.client.call('create_simulation_session', {**args, 'confirm_create': False})

    def test_local_queue_cannot_enable_semantic_or_cloud(self):
        args = {'session_id': 's1', 'version': 2, 'selections': [{'answer_id': 'a1', 'transcript_revision': 1}],
                'modules': ['word_frequency'], 'repeats': 1, 'purpose': '测试', 'condition': '原文',
                'participant_group': 'fixture', 'request_id': 'request-002', 'confirm_run': True}
        self.client.call('queue_local_research', args)
        self.assertFalse(FixtureHandler.calls[-1][2]['cloud_consent'])
        for bad in [{**args, 'modules': ['semantic']}, {**args, 'cloud_consent': True}, {**args, 'repeats': True}]:
            with self.assertRaises(ToolError):
                self.client.call('queue_local_research', bad)

    def test_remote_url_proxy_credentials_and_path_are_rejected(self):
        for url in ['https://example.com', 'http://127.0.0.1:8766/path', 'http://token@localhost', 'http://localhost?x=y']:
            with self.assertRaises(ToolError):
                WorkbenchClient(url, lambda: '')

    def test_read_write_annotations_are_explicit(self):
        by_name = {t['name']: t for t in TOOLS}
        self.assertTrue(by_name['get_report']['annotations']['readOnlyHint'])
        self.assertFalse(by_name['queue_local_research']['annotations']['readOnlyHint'])
        self.assertFalse(by_name['export_research_report']['annotations']['idempotentHint'])

    def test_recursive_redaction(self):
        value = safe_output({'api_key': 'secret', 'nested': [{'hr_token': 'secret',
                 'reason': 'Bearer private-token https://local/#token=private sk-test-abcdefghijklmnop'}]})
        self.assertNotIn('secret', json.dumps(value))
        self.assertNotIn('private-token', json.dumps(value))
        self.assertNotIn('sk-test-', json.dumps(value))

    def test_stdio_process_initialize_tools_status_and_report(self):
        # Real subprocess + STDIO + loopback HTTP against a synthetic in-memory
        # fixture. No live DB, media, DPAPI key or model provider is involved.
        source = ("import sys;from interview_mcp import WorkbenchClient,MCPServer;"
                  "MCPServer(WorkbenchClient(sys.argv[1],lambda:'fixture-operator')).serve(sys.stdin.buffer,sys.stdout.buffer)")
        messages = [
            {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {'protocolVersion': '2025-06-18', 'capabilities': {}, 'clientInfo': {'name': 'fixture', 'version': '1'}}},
            {'jsonrpc': '2.0', 'method': 'notifications/initialized'},
            {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'},
            {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'status', 'arguments': {}}},
            {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'get_report', 'arguments': {'session_id': 's1'}}},
            {'jsonrpc': '2.0', 'id': 5, 'method': 'tools/call', 'params': {'name': 'get_report', 'arguments': {
                'session_id': 's1', 'version': 2, 'summary_only': True}}}]
        result = subprocess.run([sys.executable, '-c', source, self.url],
            cwd=Path(__file__).resolve().parents[1], input=''.join(json.dumps(m) + '\n' for m in messages).encode(),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15, check=True)
        output = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual([r['id'] for r in output], [1, 2, 3, 4, 5])
        self.assertEqual(output[0]['result']['protocolVersion'], '2025-06-18')
        self.assertEqual(len(output[1]['result']['tools']), len(TOOLS))
        self.assertTrue(output[2]['result']['structuredContent']['authenticated'])
        self.assertIsNone(output[3]['result']['structuredContent']['total'])
        self.assertFalse(output[4]['result']['isError'])
        self.assertEqual(output[4]['result']['structuredContent']['version'], 2)
        self.assertEqual(output[4]['result']['structuredContent']['main_question_count'], 3)
        self.assertEqual(output[4]['result']['structuredContent']['scored_main_question_count'], 1)
        self.assertLess(len(json.dumps(output[4]).encode('utf-8')), 2048)
        self.assertNotIn(b'fixture-hr-private', result.stdout)
        self.assertEqual(result.stderr, b'')

    def test_bad_json_unknown_method_and_notification_produce_correct_protocol(self):
        server = MCPServer(self.client)
        messages = b'bad\n' + json.dumps({'jsonrpc': '2.0', 'method': 'notifications/initialized'}).encode() + b'\n'
        output = io.BytesIO()
        server.serve(io.BytesIO(messages), output)
        rows = output.getvalue().splitlines()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0])['error']['code'], -32700)
        self.assertEqual(server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'})['error']['code'], -32002)


if __name__ == '__main__':
    unittest.main()
