"""MCP -> real ASGI -> temporary DB contract, with no model or live data calls."""
import hashlib
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
from interview_core import InterviewCore
from interview_mcp import WorkbenchClient, ToolError
from interview_research import ResearchStore
from test_interview_workbench import ASGIClient


def key():
    return str(uuid.uuid4())


class MCPContractTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.env = patch.dict(os.environ, {'INTERVIEW_DATA_DIR': temp.name, 'DASHSCOPE_API_KEY': '',
            'INTERVIEW_CREDENTIALS_FILE': str(Path(temp.name) / 'missing')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.core = InterviewCore(temp.name)
        self.store = ResearchStore(self.core)
        self.entry = importlib.import_module('interview_entry')
        for target, name, value in [(self.entry, 'core', self.core)]:
            p = patch.object(target, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch('interview_workbench.load_operator_token', return_value='fixture-operator')
        p.start()
        self.addCleanup(p.stop)
        self.http = ASGIClient(self.entry.app)
        self.client = WorkbenchClient('http://127.0.0.1:8766', lambda: 'fixture-operator')
        self.calls = []
        self.client._request = self.request
        self.created = self.core.create_interview(key(), 'synthetic-mcp-contract')
        self.sid = self.created['session_id']

    def request(self, method, route, token=None, body=None):
        self.calls.append((method, route))
        response = self.http.request(method, route,
            headers={'Authorization': 'Bearer ' + token} if token else {}, json=body)
        if response.status_code >= 400:
            raise ToolError('http_failure', response.text)
        return response.json()

    def read(self, tool, **args):
        return self.client.call(tool, {'session_id': self.sid, **args})

    def test_create_simulation_forwards_quick_full_and_preserves_legacy_default(self):
        # Legacy tests deliberately retain the constructor's original fixture
        # config; exercise v4 via the same explicit config used by the API entry.
        self.core = InterviewCore(self.core.data_dir / 'v4-mode-contract',
                                  Path(__file__).resolve().parents[1] / 'config/interview.v4.json')
        active = patch.object(self.entry, 'core', self.core)
        active.start()
        self.addCleanup(active.stop)
        for mode, expected_count in ((None, 3), ('quick', 3), ('full', 8)):
            args = {'label': '合成模式契约', 'request_id': 'synthetic-form-' + str(mode), 'confirm_create': True}
            if mode is not None:
                args['form_mode'] = mode
            with patch.object(self.client, '_request', side_effect=self.request) as request:
                created = self.client.call('create_simulation_session', args)
            create_call = next(c for c in request.call_args_list if c.args[:2] == ('POST', '/v1/interviews'))
            payload = create_call.kwargs['body']
            self.assertEqual(payload.get('form_mode'), mode)
            if mode is None:
                self.assertNotIn('form_mode', payload)
            state = self.core.get_state(created['session_id'])
            self.assertEqual(state['form_mode'], mode or 'quick')
            self.assertEqual(state['main_question_count'], expected_count)
            self.assertEqual(state['max_followups'], 4 if mode == 'full' else 1)
            self.assertEqual(state['analysis_plan']['modules'], ['au', 'gaze', 'expression', 'audio'])
            repeated = self.client.call('create_simulation_session', args)
            self.assertEqual(repeated['session_id'], created['session_id'])
            report = self.client.call('get_report', {'session_id': created['session_id']})
            self.assertEqual(len(report['questions']), expected_count)
            self.assertEqual(report['potential_conclusion']['source_version'], report['version'])
            self.assertNotIn('hr_token', created)

    def test_create_simulation_rejects_unknown_mode_before_api_call(self):
        before = list(self.calls)
        with self.assertRaises(ToolError):
            self.client.call('create_simulation_session', {'label': '合成无效模式',
                'request_id': 'synthetic-invalid-mode', 'confirm_create': True, 'form_mode': 'other'})
        self.assertEqual(self.calls, before)

    def test_partial_review_and_historical_report_use_same_authority(self):
        answers = []
        for n in range(3):
            state = self.core.get_state(self.sid)
            answers.append(self.core.submit_answer(self.sid, state['current_round']['round_id'], key(), key(),
                f'合成第{n}题：我拆分任务并检查结果。'))
            self.core.next_round(self.sid, key(), state['current_round']['round_id'])
        def review(answer, score):
            return self.core.submit_review(self.sid, self.core.get_state(self.sid)['version'], '契约老师', [{
                'question_id': answer['question_id'], 'score': score, 'reason': '核对合成原话',
                'evidence': [{'answer_id': answer['answer_id'], 'transcript_revision': 1, 'quote': answer['transcript']}]}], key())
        first = review(answers[0], 4)
        second = review(answers[1], 3)
        got = self.read('get_report')
        self.assertEqual(got['version'], second['version'])
        self.assertEqual(got['reviewed_question_count'], 2)
        self.assertEqual([q['score'] for q in got['questions']], [4, 3, None])
        old = self.read('get_report', version=first['version'], compact=False)
        self.assertEqual(old['questions'], first['questions'])
        self.assertEqual(old['reviewed_question_count'], 1)
        self.assertEqual(self.read('get_session')['version'], second['version'])
        self.assertEqual(self.read('get_analysis_status')['reviewed_question_count'], 2)
        self.assertNotIn(self.created['hr_token'], json.dumps(got))
        self.assertEqual(self.read('get_report', summary_only=False), got)

        # The frozen older report must supply every summary field, even after
        # a later review changes the effective score count for this session.
        state_before = self.core.get_state(self.sid)
        runs_before = self.store.list_runs(self.sid)
        call_start = len(self.calls)
        historical = self.read('get_report', version=first['version'], summary_only=True)
        current = self.read('get_report', summary_only=True)
        self.assertEqual(historical['session_id'], self.sid)
        self.assertEqual(historical['version'], first['version'])
        self.assertEqual(historical['main_question_count'], 3)
        self.assertEqual(historical['scored_main_question_count'], 1)
        self.assertEqual(current['version'], second['version'])
        self.assertEqual(current['scored_main_question_count'], 2)
        for summary, report in ((historical, old), (current, got)):
            for field in ('report_completeness', 'report_lifecycle'):
                self.assertEqual(summary[field], report[field])
            self.assertEqual(summary['interview_status'], report['status'])
            self.assertLess(len(json.dumps(summary, ensure_ascii=False).encode('utf-8')), 2048)
            self.assertNotIn(self.created['hr_token'], json.dumps(summary))
        self.assertEqual(self.calls[call_start:], [
            ('POST', f'/v1/workbench/sessions/{self.sid}/open'),
            ('GET', f'/v1/interviews/{self.sid}/report?compact=true&version={first["version"]}'),
            ('POST', f'/v1/workbench/sessions/{self.sid}/open'),
            ('GET', f'/v1/interviews/{self.sid}/report?compact=true')])
        after = self.core.get_state(self.sid)
        for field in ('version', 'status', 'jobs', 'rounds', 'answers'):
            self.assertEqual(after[field], state_before[field])
        self.assertEqual(self.store.list_runs(self.sid), runs_before)

        # New evidence readers always use this exact historical report, even
        # after later HR reviews alter the current effective score count.
        projection_calls = len(self.calls)
        index = self.read('get_report_index', version=first['version'])
        self.assertEqual(index['scored_main_question_count'], 1)
        qid = answers[0]['question_id']
        source = self.read('get_question_evidence', version=first['version'], question_id=qid)
        source_answers = [r['data'] for r in source['records'] if r['kind'] == 'answer']
        self.assertEqual(source_answers[0]['transcript'], answers[0]['transcript'])
        self.assertEqual(source['input_versions'], {answers[0]['answer_id']: 1})
        self.assertNotIn('recorded_score', json.dumps(source))
        assessment = self.read('get_question_evidence', version=first['version'], question_id=qid,
                               section='assessment')
        score = next(r['data'] for r in assessment['records'] if r['kind'] == 'recorded_score')
        self.assertEqual(score['score'], 4)
        self.assertEqual(score['score_source'], 'hr')
        for output in (index, source, assessment):
            self.assertEqual(output['version'], first['version'])
            self.assertNotIn(self.created['hr_token'], json.dumps(output))
        expected_route = f'/v1/interviews/{self.sid}/report?compact=true&version={first["version"]}'
        self.assertTrue(all(route == expected_route if method == 'GET' else route.endswith('/open')
                            for method, route in self.calls[projection_calls:]))
        with self.assertRaises(ToolError):
            self.read('get_question_evidence', version=first['version'], question_id='not-in-this-form')
        final_state = self.core.get_state(self.sid)
        for field in ('version', 'status', 'jobs', 'rounds', 'answers'):
            self.assertEqual(final_state[field], state_before[field])
        self.assertEqual(self.store.list_runs(self.sid), runs_before)
        self.assertEqual(self.core.get_report(self.sid, first['version']), first)

    def test_automatic_runs_compact_projection_and_reads_do_not_schedule(self):
        path = self.core.data_dir / 'media' / 'fixture.wav'
        path.write_bytes(b'synthetic undecodable fixture; model execution is mocked')
        answer = self.core.submit_answer(self.sid, self.created['current_round']['round_id'], key(), key(),
            path=path, media_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
            media_meta={'duration_ms': 1000, 'mime_type': 'audio/wav'})
        job = self.core.claim_job('fixture-worker')
        self.core.complete_job(job['job_id'], job['lease_token'], {'transcription': {'status': 'ok', 'text': '合成原话',
            'segments': [{'start_ms': 0, 'end_ms': 1000, 'text': '合成原话'}]}})
        before = self.core.get_state(self.sid)['version']
        waiting = self.read('get_analysis_status')
        self.assertEqual(waiting['multimodal']['answers'][0]['status'], 'waiting_worker')
        self.assertEqual(self.core.get_state(self.sid)['version'], before)
        self.assertEqual(self.store.list_runs(self.sid)['runs'], [])
        run = self.store.claim()
        aid, rev = next(iter(run['input']['input_versions'].items()))
        observation = {'start_ms': 0, 'end_ms': 1000, 'quality': 'valid', 'values': {'synthetic': 1}}
        result = {'run_id': run['run_id'], 'input_versions': {aid: rev}, 'status': 'succeeded',
            'multimodal': [{'answer_id': aid, 'transcript_revision': rev, 'modules': {'audio': {
                'status': 'complete', 'model_id': 'synthetic', 'summary': {'valid_windows': 1},
                'observations': [observation]}}}]}
        self.store.complete(run['run_id'], run['lease_token'], result)
        full = self.core.get_report(self.sid)
        compact = self.read('get_report', version=full['version'])
        module = compact['standard_analysis']['runs'][0]['result']['multimodal'][0]['modules']['audio']
        self.assertEqual(module['observation_count'], 1)
        self.assertTrue(module['observations_omitted'])
        self.assertNotIn('observations', module)
        self.assertEqual(compact['questions'], full['questions'])
        self.assertEqual(self.core.get_report(self.sid, full['version']), full)
        detail = self.read('get_research_run', run_id=run['run_id'])
        self.assertEqual(detail['result'], result)
        status = self.read('get_analysis_status')
        self.assertNotIn('result', status['multimodal']['runs'][0])
        self.assertEqual(status['version'], full['version'])
        jobs_before = self.core.get_state(self.sid)['jobs']
        runs_before = self.store.list_runs(self.sid)
        summary = self.read('get_report', version=full['version'], summary_only=True)
        self.assertEqual(summary['version'], full['version'])
        self.assertEqual(summary['main_question_count'], len(full['questions']))
        self.assertEqual(summary['scored_main_question_count'], 0)
        self.assertEqual(self.core.get_state(self.sid)['version'], full['version'])
        self.assertEqual(self.core.get_state(self.sid)['jobs'], jobs_before)
        self.assertEqual(self.store.list_runs(self.sid), runs_before)
        self.assertEqual(self.core.get_report(self.sid, full['version']), full)
        # Every read used existing operator recovery plus an HR GET only.
        self.assertTrue(all(method == 'GET' or route.endswith('/open') for method, route in self.calls))
        forbidden = self.http.get(f'/v1/interviews/{self.sid}/report?compact=true',
            headers={'Authorization': 'Bearer ' + self.created['candidate_token']})
        self.assertEqual(forbidden.status_code, 403)


if __name__ == '__main__':
    unittest.main()
