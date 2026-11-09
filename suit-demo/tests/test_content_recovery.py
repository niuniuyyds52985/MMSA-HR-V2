"""Content recovery and safe transport diagnostics; synthetic inputs, no network."""
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch, Mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_core import BASE, DomainError, InterviewCore
from interview_llm import evaluate_answer, ProviderError, REQUEST_TIMEOUT
from interview_job_errors import safe_diagnostics
from test_interview_workbench import ASGIClient


def key():
    return str(uuid.uuid4())


class TransportDiagnosticsTests(unittest.TestCase):
    def test_transport_categories_are_single_attempt_and_do_not_leak(self):
        import requests
        env = {'INTERVIEW_LLM_BASE_URL': 'https://pinned.invalid/v1', 'DASHSCOPE_API_KEY': 'private-fixture'}
        question = {'question_id': 'q1', 'text': '合成题', 'anchors': {str(i): str(i) for i in range(1, 6)}}
        answers = [{'answer_id': 'a1', 'transcript_revision': 1, 'transcript': '我安排同学核对任务。'}]
        types = [(requests.exceptions.ConnectTimeout, 'connect_timeout'),
                 (requests.exceptions.ReadTimeout, 'read_timeout'),
                 (requests.exceptions.SSLError, 'tls_error'),
                 (requests.exceptions.ProxyError, 'proxy_error'),
                 (requests.exceptions.ConnectionError, 'connection_error')]
        for exception, code in types:
            with self.subTest(code=code), patch.dict(os.environ, env, clear=True), \
                 patch('interview_credentials._record', return_value={}), \
                 patch('requests.post', side_effect=exception('private-fixture sensitive-url transcript')) as request:
                with self.assertRaises(ProviderError) as caught:
                    evaluate_answer(question, answers, [])
                request.assert_called_once()
                self.assertEqual(request.call_args.kwargs['timeout'], REQUEST_TIMEOUT)
                self.assertEqual(caught.exception.code, code)
                self.assertEqual(caught.exception.details['attempt_count'], 1)
                self.assertEqual(caught.exception.details['attempt_events'][0]['code'], code)
                self.assertNotIn('private-fixture', str(caught.exception) + json.dumps(caught.exception.details))

    def test_http_failure_is_distinct_and_no_immediate_double_retry(self):
        env = {'INTERVIEW_LLM_BASE_URL': 'https://pinned.invalid/v1', 'DASHSCOPE_API_KEY': 'fixture'}
        with patch.dict(os.environ, env, clear=True), patch('interview_credentials._record', return_value={}), patch('requests.post') as request:
            request.return_value.status_code = 429
            with self.assertRaises(ProviderError) as caught:
                evaluate_answer({'question_id': 'q1', 'anchors': {str(i): str(i) for i in range(1, 6)}}, [{'answer_id': 'a1', 'transcript_revision': 1, 'transcript': '合成回答。'}], [])
            request.assert_called_once()
            self.assertEqual(caught.exception.code, 'api_http_error')
            self.assertEqual(caught.exception.details['http_status'], 429)

    def test_malformed_envelopes_are_retryable_invalid_responses(self):
        env = {'INTERVIEW_LLM_BASE_URL': 'https://pinned.invalid/v1', 'DASHSCOPE_API_KEY': 'private-fixture'}
        question = {'question_id': 'q1', 'anchors': {str(i): str(i) for i in range(1, 6)}}
        answers = [{'answer_id': 'a1', 'transcript_revision': 1, 'transcript': '合成回答。'}]
        payloads = [None, [], {'choices': []}, {'choices': None}, {'choices': 'private-fixture'},
                    {'choices': [None]}, {'choices': [{'finish_reason': 'stop', 'message': None}]},
                    {'choices': [{'finish_reason': 'stop', 'message': []}]},
                    {'choices': [{'finish_reason': 'stop', 'message': {'content': None}}]},
                    {'choices': [{'finish_reason': 'stop', 'message': {'content': []}}]}]
        for payload in payloads:
            response = Mock(status_code=200, content=b'private-fixture')
            response.json.return_value = payload
            with self.subTest(payload=payload), patch.dict(os.environ, env, clear=True), \
                 patch('interview_credentials._record', return_value={}), \
                 patch('requests.post', return_value=response) as request, patch('interview_llm.time.sleep'):
                with self.assertRaises(ProviderError) as caught:
                    evaluate_answer(question, answers, [])
                error = caught.exception
                self.assertEqual(error.code, 'invalid_response')
                self.assertTrue(error.retryable)
                self.assertEqual(request.call_count, 2)
                self.assertEqual(error.details['attempt_count'], 2)
                self.assertEqual([event['code'] for event in error.details['attempt_events']],
                                 ['invalid_response', 'invalid_response'])
                self.assertNotIn('private-fixture', str(error) + json.dumps(error.details))

    def test_requests_json_decode_error_is_not_a_transport_failure(self):
        import requests
        env = {'INTERVIEW_LLM_BASE_URL': 'https://pinned.invalid/v1', 'DASHSCOPE_API_KEY': 'private-fixture'}
        response = Mock(status_code=200, content=b'invalid json')
        response.json.side_effect = requests.exceptions.JSONDecodeError('private-fixture', 'private-fixture', 0)
        with patch.dict(os.environ, env, clear=True), patch('interview_credentials._record', return_value={}), \
             patch('requests.post', return_value=response) as request, patch('interview_llm.time.sleep'):
            with self.assertRaises(ProviderError) as caught:
                evaluate_answer({'question_id': 'q1', 'anchors': {str(i): str(i) for i in range(1, 6)}},
                                [{'answer_id': 'a1', 'transcript_revision': 1, 'transcript': '合成回答。'}], [])
            self.assertEqual(caught.exception.code, 'invalid_response')
            self.assertTrue(caught.exception.retryable)
            self.assertEqual(request.call_count, 2)
            self.assertNotIn('private-fixture', str(caught.exception) + json.dumps(caught.exception.details))

    def test_non_retryable_provider_failures_keep_their_attempt_event(self):
        env = {'INTERVIEW_LLM_BASE_URL': 'https://pinned.invalid/v1', 'DASHSCOPE_API_KEY': 'private-fixture'}
        cases = [(Mock(status_code=401), 'api_http_error'),
                 (Mock(status_code=200, content=b'x' * (1024 * 1024 + 1)), 'oversized_response'),
                 (Mock(status_code=200, content=b'{}'), 'incomplete_response')]
        cases[-1][0].json.return_value = {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}
        for response, code in cases:
            with self.subTest(code=code), patch.dict(os.environ, env, clear=True), \
                 patch('interview_credentials._record', return_value={}), \
                 patch('requests.post', return_value=response) as request:
                with self.assertRaises(ProviderError) as caught:
                    evaluate_answer({'question_id': 'q1', 'anchors': {str(i): str(i) for i in range(1, 6)}},
                                    [{'answer_id': 'a1', 'transcript_revision': 1, 'transcript': '合成回答。'}], [])
                self.assertEqual(caught.exception.code, code)
                self.assertFalse(caught.exception.retryable)
                request.assert_called_once()
                self.assertEqual(caught.exception.details['attempt_count'], 1)
                self.assertEqual(caught.exception.details['attempt_events'][0]['code'], code)

    def test_diagnostics_allowlist(self):
        result = safe_diagnostics({'elapsed_ms': 1200, 'read_timeout_seconds': 120,
            'headers': {'Authorization': 'secret'}, 'exception': 'private text',
            'attempt_events': [{'code': 'read_timeout', 'attempt': 1, 'elapsed_ms': 1200, 'url': 'secret'}]})
        self.assertEqual(set(result), {'elapsed_ms', 'read_timeout_seconds', 'attempt_events'})
        self.assertNotIn('secret', json.dumps(result))


class ContentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'INTERVIEW_DATA_DIR': self.tmp.name,
            'INTERVIEW_CREDENTIALS_FILE': str(Path(self.tmp.name) / 'absent'), 'DASHSCOPE_API_KEY': ''})
        env.start()
        self.addCleanup(env.stop)
        self.core = InterviewCore(self.tmp.name, BASE / 'config' / 'interview.v3.json')
        self.created = self.core.create_interview(key(), 'synthetic-recovery')
        self.sid = self.created['session_id']
        current = self.core.get_state(self.sid)['current_round']
        self.qid = current['question_id']
        self.answer = self.core.submit_answer(self.sid, current['round_id'], key(), key(), '我安排三名同学核对清单并完成项目。')
        self.fail_current()

    def fail_current(self):
        job = self.core.claim_job(job_type='content')
        self.assertIsNotNone(job)
        self.core.fail_job(job['job_id'], job['lease_token'], '读取响应超时。', retryable=False,
                           code='read_timeout', details={'elapsed_ms': 40001, 'read_timeout_seconds': 40, 'headers': 'secret'})
        return job

    def retry(self, **changes):
        args = {'session_id': self.sid, 'question_id': self.qid,
                'version': self.core.get_state(self.sid)['version'], 'idempotency_key': key()}
        args.update(changes)
        return self.core.retry_content_analysis(**args)

    def test_retry_is_idempotent_preserves_original_report_and_answers(self):
        before = self.core.get_report(self.sid)
        idem = key()
        receipt = self.retry(idempotency_key=idem)
        again = self.retry(version=before['version'], idempotency_key=idem)
        self.assertEqual(receipt, again)
        after = self.core.get_report(self.sid)
        self.assertEqual(before, self.core.get_report(self.sid, before['version']))
        self.assertEqual(before['answers'][0]['transcript'], after['answers'][0]['transcript'])
        self.assertIsNone(after['questions'][0]['score'])
        old = next(j for j in after['jobs'] if j['job_id'] == receipt['retry_of'])
        self.assertEqual(old['status'], 'superseded')
        self.assertEqual(old['error_code'], 'read_timeout')
        self.assertNotIn('headers', old['error_details'])
        with self.core._db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM job_failures').fetchone()[0], 1)

    def test_duplicate_running_and_stale_version_are_rejected(self):
        version = self.core.get_state(self.sid)['version']
        self.retry()
        for changes, code in [({'version': version}, 'version_conflict'), ({}, 'invalid_retry')]:
            with self.assertRaises(DomainError) as caught:
                self.retry(**changes)
            self.assertEqual(caught.exception.code, code)

    def test_new_transcript_cannot_retry_old_failure(self):
        self.core.revise_transcript(self.sid, self.answer['answer_id'], self.answer['transcript_revision'],
                                   '我核对了四个任务。', '合成修订', key(), actor='hr')
        with self.assertRaises(DomainError) as caught:
            self.retry()
        self.assertEqual(caught.exception.code, 'invalid_retry')

    def test_manual_retry_budget_and_wrong_parent(self):
        with self.assertRaises(DomainError) as caught:
            self.retry(question_id='another-question')
        self.assertEqual(caught.exception.code, 'invalid_question')
        for count in range(1, 4):
            self.assertEqual(self.retry()['manual_retry_count'], count)
            self.fail_current()
        with self.assertRaises(DomainError) as caught:
            self.retry()
        self.assertEqual(caught.exception.code, 'retry_limit')

    def test_api_rejects_candidate_cross_session_and_accepts_hr(self):
        entry = importlib.import_module('interview_entry')
        with patch.object(entry, 'core', self.core):
            client = ASGIClient(entry.app)
            path = '/v1/interviews/' + self.sid + '/content-analysis/retry'
            body = {'question_id': self.qid, 'version': self.core.get_state(self.sid)['version'], 'idempotency_key': key()}
            other = self.core.create_interview(key(), 'synthetic-other')
            for token in (self.core._token(self.sid, 'candidate'), self.core._token(other['session_id'], 'hr')):
                response = client.request('POST', path, headers={'Authorization': 'Bearer ' + token}, json=body)
                self.assertEqual(response.status_code, 403)
            response = client.request('POST', path, headers={'Authorization': 'Bearer ' + self.core._token(self.sid, 'hr')}, json=body)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(json.loads(response.text)['status'], 'queued')


if __name__ == '__main__':
    unittest.main()
