"""Bounded semantic routing: synthetic text, temporary DBs and mocked HTTP only."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from unittest.mock import Mock, patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_core import BASE, DomainError, InterviewCore
from interview_forms import compile_bank, load_bank, load_rubrics
from interview_llm import ProviderError
from interview_routing import evaluate_routing, lexical_check, validate_routing
from interview_text_research import prepare_text_answers
import interview_worker

COMPILED = compile_bank(load_bank(), load_rubrics())


def bank_question(qid='leadership_F3_Q1'):
    question = copy.deepcopy(next(q for q in COMPILED['questions'] if q['question_id'] == qid))
    branches = copy.deepcopy([b for b in COMPILED['branches'] if b['parent_question_id'] == qid])
    return question, branches


def text_answer(text, aid='synthetic-answer'):
    return {'answer_id': aid, 'transcript_revision': 1, 'transcript': text}


def reference(answer, quote=None):
    prepared = prepare_text_answers([answer])[0]
    segment = prepared['segments'][0]
    return {'answer_id': prepared['answer_id'], 'transcript_revision': prepared['transcript_revision'],
            'segment_id': segment['segment_id'], 'char_start': None, 'char_end': None,
            'quote': segment['text'] if quote is None else quote}


def semantic_value(answer, branches, selected=None):
    return {'selected_branch_id': selected, 'reason': '本题回答已有动作细节，仍需核实其结果。',
            'branches': [{'branch_id': b['branch_id'],
                          'status': 'gap' if b['branch_id'] == selected else 'covered',
                          'reason': '依据本题原话核对目标。', 'evidence': [reference(answer)],
                          'prerequisite_evidence': []} for b in branches]}


class SemanticValidationTests(unittest.TestCase):
    def setUp(self):
        self.question, self.branches = bank_question()
        self.answer = text_answer('🙂我安排了三名同学分别联系客户。最终项目尚未结束。')

    def validate(self, value):
        return validate_routing(value, self.question, [self.answer], self.branches)

    def test_valid_exact_unicode_reference_is_resolved_without_rewriting(self):
        value = semantic_value(self.answer, self.branches, self.branches[0]['branch_id'])
        quote = '我安排了三名同学分别联系客户'
        value['branches'][0]['evidence'] = [reference(self.answer, quote)]
        result = self.validate(value)
        ref = result['branches'][0]['evidence'][0]
        self.assertEqual(ref['char_start'], 1)  # Unicode codepoints, not UTF-16 units.
        self.assertEqual(self.answer['transcript'][ref['char_start']:ref['char_end']], quote)
        self.assertIsNone(value['branches'][0]['evidence'][0]['char_start'])

    def test_invalid_reference_variants_are_rejected(self):
        variants = [
            ('other_answer', {'answer_id': 'not-this-answer'}),
            ('stale_revision', {'transcript_revision': 0}),
            ('boolean_revision', {'transcript_revision': True}),
            ('other_segment', {'segment_id': 'invented-segment'}),
            ('invented_quote', {'quote': '我已获得一百万元'}),
            ('wrong_offset', {'char_start': 0, 'char_end': 3}),
            ('guessed_time', {'start_ms': 0}),
        ]
        for name, change in variants:
            with self.subTest(name=name):
                value = semantic_value(self.answer, self.branches, self.branches[0]['branch_id'])
                value['branches'][0]['evidence'][0].update(change)
                with self.assertRaises(ProviderError):
                    self.validate(value)

    def test_branch_allowlist_complete_unique_and_gap_selection(self):
        valid = semantic_value(self.answer, self.branches, self.branches[0]['branch_id'])
        variants = []
        wrong = copy.deepcopy(valid); wrong['selected_branch_id'] = 'learning_F10_Q2_boundary'; variants.append(wrong)
        duplicate = copy.deepcopy(valid); duplicate['branches'][1] = copy.deepcopy(duplicate['branches'][0]); variants.append(duplicate)
        missing = copy.deepcopy(valid); missing['branches'].pop(); variants.append(missing)
        covered = copy.deepcopy(valid); covered['branches'][0]['status'] = 'covered'; variants.append(covered)
        empty = copy.deepcopy(valid); empty['branches'][0]['evidence'] = []; variants.append(empty)
        for value in variants:
            with self.subTest(value=value['selected_branch_id'], count=len(value['branches'])):
                with self.assertRaises(ProviderError):
                    self.validate(value)

    def test_prerequisite_requires_current_verifiable_quote(self):
        self.question, self.branches = bank_question('learning_F10_Q2')
        self.answer = text_answer('我从失败中总结出先试验再投入的方法。')
        value = semantic_value(self.answer, self.branches, self.branches[0]['branch_id'])
        with self.assertRaises(ProviderError) as error:
            self.validate(value)
        self.assertEqual(error.exception.code, 'missing_prerequisite_evidence')
        value['branches'][0]['prerequisite_evidence'] = [reference(self.answer)]
        self.assertEqual(self.validate(value)['selected_branch_id'], self.branches[0]['branch_id'])
        value['branches'][0]['prerequisite_evidence'][0]['transcript_revision'] = 2
        with self.assertRaises(ProviderError):
            self.validate(value)

    def test_semantic_prerequisite_rejects_denial_other_actor_stuffing_and_unrelated_quotes(self):
        self.question, self.branches = bank_question('learning_F10_Q2')
        for text in ('我没有总结出先验证再投入的方法。', '我看到同学总结了先试验再投入的方法。',
                     '我总结了方法规律经验教训。', '我昨天去买了三本书。'):
            with self.subTest(text=text):
                self.answer = text_answer(text)
                value = semantic_value(self.answer, self.branches, self.branches[0]['branch_id'])
                value['branches'][0]['prerequisite_evidence'] = [reference(self.answer)]
                with self.assertRaises(ProviderError) as error:
                    self.validate(value)
                self.assertEqual(error.exception.code, 'missing_prerequisite_evidence')
        self.answer = text_answer('我昨天去买了三本书，然后总结了先试验再投入的方法。')
        value = semantic_value(self.answer, self.branches, self.branches[0]['branch_id'])
        value['branches'][0]['prerequisite_evidence'] = [reference(self.answer, '我昨天去买了三本书')]
        with self.assertRaises(ProviderError):
            self.validate(value)
        value['branches'][0]['prerequisite_evidence'] = [reference(self.answer, '先试验再投入')]
        self.assertEqual(self.validate(value)['selected_branch_id'], self.branches[0]['branch_id'])

    def test_simplified_and_traditional_guards_preserve_original_quotes(self):
        for text in ('我安排了三名同學分別聯繫客戶。', '我安排了三名同学分别联系客户。'):
            with self.subTest(text=text):
                result = lexical_check([text_answer(text)], self.question, self.branches)
                action = next(c for c in result['coverage'] if c['branch_id'].endswith('_action'))
                self.assertEqual(action['status'], 'lexical_cue_present')
                for hit in action['hits']:
                    self.assertEqual(text[hit['char_start']:hit['char_end']], hit['quote'])
        for text in ('我看到同學安排三名成員聯繫客戶。', '我負責分工溝通協調組織安排。'):
            with self.subTest(text=text):
                result = lexical_check([text_answer(text)], self.question, self.branches)
                self.assertEqual(result['branch_id'], 'leadership_F3_Q1_action')

    def test_compiled_literal_cues_are_not_double_escaped(self):
        question = {'question_id': 'synthetic', 'question_type': 'behavioral'}
        branch = {'branch_id': 'synthetic-result', 'parent_question_id': 'synthetic',
                  'target_evidence': '结果', 'rule': {'lexical_patterns': [r'A\+B'], 'gap_type': 'outcome_result'}}
        result = lexical_check([text_answer('最终完成了A+B两个工作包。')], question, [branch])
        self.assertEqual(result['coverage'][0]['status'], 'lexical_cue_present')

    def test_lexical_prerequisite_denial_other_actor_and_stuffing_remain_ineligible(self):
        q, branches = bank_question('learning_F10_Q2')
        for text in ('我没有总结出先验证再投入的方法。', '我看到同学总结了先试验再投入的方法。',
                     '我总结了方法规律经验教训。', '不知道，没有什么方法。'):
            with self.subTest(text=text):
                result = lexical_check([text_answer(text)], q, branches)
                self.assertIsNone(result['branch_id'])
                self.assertFalse(result['coverage'][0]['eligible'])

    def test_lexical_fallback_does_not_cover_someone_else_or_keyword_stuffing(self):
        for text in ('我的同学安排三名成员联系客户。', '我看到组长安排三名成员联系客户。',
                     '我负责分工沟通协调组织安排。', '我没有安排同学联系客户。'):
            with self.subTest(text=text):
                result = lexical_check([text_answer(text)], self.question, self.branches)
                action = next(c for c in result['coverage'] if c['branch_id'].endswith('_action'))
                self.assertEqual(action['status'], 'needs_clarification')
                self.assertEqual(result['branch_id'], 'leadership_F3_Q1_action')

    def test_provider_payload_is_text_only_bounded_and_non_streaming(self):
        value = semantic_value(self.answer, self.branches, self.branches[0]['branch_id'])
        envelope = {'id': 'synthetic-request', 'model': 'fixture-model', 'choices': [
            {'finish_reason': 'stop', 'message': {'content': json.dumps(value, ensure_ascii=False)}}]}
        response = Mock(status_code=200, content=b'{}'); response.json.return_value = envelope
        settings = {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture-model'}
        with patch('interview_routing.public_settings', return_value=settings), \
             patch('interview_routing.provider_status', return_value=settings), \
             patch('interview_routing.get_api_key', return_value='synthetic-no-secret'), \
             patch('requests.post', return_value=response) as post:
            result = evaluate_routing(self.question, [self.answer], self.branches)
        args = post.call_args.kwargs
        self.assertEqual(args['timeout'], (2, 5))
        self.assertFalse(args['allow_redirects'])
        self.assertFalse(args['json']['stream'])
        self.assertFalse(args['json']['enable_thinking'])
        self.assertEqual(args['json']['max_tokens'], 1800)
        self.assertEqual(args['json']['response_format']['type'], 'json_schema')
        self.assertTrue(all(isinstance(m['content'], str) for m in args['json']['messages']))
        payload = json.loads(args['json']['messages'][1]['content'])
        self.assertEqual(set(payload['question']), {'question_id', 'text', 'question_type'})
        self.assertNotIn('anchors', json.dumps(payload))
        self.assertNotIn('source_raw_text', json.dumps(payload))
        self.assertNotIn('source_file_hash', json.dumps(payload))
        self.assertEqual(payload['answers'], prepare_text_answers([self.answer]))
        self.assertTrue(all(set(c) == {'criterion_id', 'name', 'description'} for c in payload['criteria']))
        self.assertIn('不超过40字', args['json']['messages'][0]['content'])
        self.assertIn('不超过30字', args['json']['messages'][0]['content'])
        self.assertEqual(result['input_versions'], {self.answer['answer_id']: 1})
        self.assertEqual(result['provider']['validation'], 'passed')

    def test_provider_timeout_and_incomplete_output_are_not_success(self):
        import requests
        settings = {'base_url': 'https://fixture.invalid/v1', 'model': 'fixture-model'}
        response = Mock(status_code=200, content=b'{}')
        response.json.return_value = {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}
        for outcome in (requests.Timeout('synthetic'), response):
            with self.subTest(outcome=type(outcome).__name__), \
                 patch('interview_routing.public_settings', return_value=settings), \
                 patch('interview_routing.provider_status', return_value=settings), \
                 patch('interview_routing.get_api_key', return_value='synthetic-no-secret'), \
                 patch('requests.post', side_effect=outcome if isinstance(outcome, Exception) else None,
                       return_value=outcome):
                with self.assertRaises(ProviderError) as error:
                    evaluate_routing(self.question, [self.answer], self.branches)
                self.assertIn(error.exception.code, ('routing_timeout', 'invalid_routing'))


class RoutingFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'INTERVIEW_CREDENTIALS_FILE': str(Path(self.tmp.name)/'absent'),
                                     'DASHSCOPE_API_KEY': ''})
        env.start(); self.addCleanup(env.stop)
        self.core = InterviewCore(self.tmp.name, BASE/'config'/'interview.v4.json')
        # Freeze deterministic real bank questions; never change production or participant DBs.
        self.core.config.pop('form_generation', None)
        ids = ('leadership_F3_Q1', 'results_K6', 'learning_F10_Q2')
        self.core.config['questions'] = [bank_question(qid)[0] for qid in ids]
        self.core.config['branches'] = [b for qid in ids for b in bank_question(qid)[1]]
        self.sid = self.core.create_interview(str(uuid.uuid4()), 'synthetic-routing-v4')['session_id']

    def answer(self, text='我安排了三名同学分别联系客户。项目还在进行中。'):
        current = self.core.get_state(self.sid)['current_round']
        return self.core.submit_answer(self.sid, current['round_id'], str(uuid.uuid4()), str(uuid.uuid4()), text)

    def advance(self, answer, key=None):
        return self.core.next_round(self.sid, key or str(uuid.uuid4()), answer['round_id'])

    def events(self):
        with self.core._db() as db:
            return [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM decision_events WHERE session_id=? ORDER BY rowid', (self.sid,))]

    def result(self, job, selected='leadership_F3_Q1_result'):
        return {'routing': semantic_value(job['answers'][0], job['branches'], selected),
                'input_versions': job['input_versions'], 'provider': {'model': 'fixture-model', 'validation': 'passed'}}

    def age_routing(self, seconds=10):
        created = datetime.fromtimestamp(time.time()-seconds, timezone.utc).isoformat()
        with self.core._db(True) as db:
            db.execute("UPDATE jobs SET created_at=? WHERE session_id=? AND job_type='routing'", (created, self.sid))
        return created

    def test_submit_queues_short_routing_and_content_without_starting_models(self):
        self.answer()
        with self.core._db() as db:
            jobs = [dict(row) for row in db.execute('SELECT job_type,status FROM jobs WHERE session_id=?', (self.sid,))]
        self.assertCountEqual(jobs, [{'job_type': 'routing', 'status': 'queued'}, {'job_type': 'content', 'status': 'queued'}])

    def test_pending_semantic_returns_immediately_and_same_key_can_poll(self):
        answer = self.answer(); key = str(uuid.uuid4())
        with patch('interview_core.credential_available', return_value=True):
            started = time.perf_counter(); waiting = self.advance(answer, key)
            self.assertLess(time.perf_counter()-started, 1)
            self.assertEqual(waiting['current_round']['round_id'], answer['round_id'])
            self.assertEqual(waiting['decision_stage'], 'semantic_check')
            self.assertGreater(waiting['decision_wait_remaining'], 0)
            self.assertLessEqual(waiting['decision_wait_remaining'], 6)
            self.assertEqual(self.events(), [])
            job = self.core.claim_job(job_type='routing')
            self.core.complete_routing_job(job['job_id'], job['lease_token'], self.result(job))
            advanced = self.advance(answer, key)
            self.assertEqual(advanced['current_round']['question_id'], 'leadership_F3_Q1_result')
            self.assertEqual(advanced, self.advance(answer, key))
        event = self.events()[0]
        self.assertEqual(event['decision_source'], 'semantic-bank.v1')
        self.assertEqual(event['model_run_ids'], [job['job_id']])
        self.assertTrue(event['decision_summary']['model_involved_in_selection'])
        self.assertEqual(event['used_evidence'][0]['quote'], job['answers'][0]['segments'][0]['text'])
        self.assertEqual(event['input_versions'], job['input_versions'])
        self.assertEqual(len(self.events()), 1)

    def test_no_credentials_does_not_wait_for_queued_semantic(self):
        answer = self.answer()
        with patch('interview_core.credential_available', return_value=False):
            started = time.perf_counter(); state = self.advance(answer)
        self.assertLess(time.perf_counter()-started, 1)
        self.assertNotEqual(state['current_round']['round_id'], answer['round_id'])
        self.assertEqual(self.events()[0]['decision_source'], 'dictionary-bank.v2-fallback')

    def test_deadline_fallback_seals_dictionary_decision_without_scoring_wait(self):
        answer = self.answer(); self.age_routing()
        with patch('interview_core.credential_available', return_value=True):
            started = time.perf_counter(); state = self.advance(answer)
        self.assertLess(time.perf_counter()-started, 1)
        self.assertNotEqual(state['current_round']['round_id'], answer['round_id'])
        event = self.events()[0]
        self.assertEqual(event['decision_source'], 'dictionary-bank.v2-fallback')
        self.assertEqual(event['decision_summary']['fallback_reason'], 'semantic_deadline')
        self.assertFalse(event['model_run_ids'])
        self.assertFalse(event['decision_summary']['model_involved_in_selection'])
        self.assertIn('本地规则', event['reason_for_hr'])

    def test_failed_semantic_falls_back_without_waiting(self):
        answer = self.answer(); job = self.core.claim_job(job_type='routing')
        self.core.fail_job(job['job_id'], job['lease_token'], 'synthetic timeout', retryable=False, code='routing_timeout')
        with patch('interview_core.credential_available', return_value=True):
            self.advance(answer)
        self.assertEqual(self.events()[0]['decision_summary']['fallback_reason'], 'routing_timeout')

    def test_late_running_job_cannot_change_sealed_decision_or_next_question(self):
        answer = self.answer(); job = self.core.claim_job(job_type='routing'); self.age_routing()
        with patch('interview_core.credential_available', return_value=True):
            state = self.advance(answer)
        before = copy.deepcopy(self.events())
        applied = self.core.complete_routing_job(job['job_id'], job['lease_token'], self.result(job))
        self.assertTrue(applied['decision_already_sealed'])
        self.assertEqual(self.events(), before)
        self.assertEqual(self.core.get_state(self.sid)['current_round'], state['current_round'])
        self.assertEqual(before[0]['decision_source'], 'dictionary-bank.v2-fallback')

    def test_queued_job_is_not_claimed_after_sealed_decision(self):
        answer = self.answer(); self.age_routing()
        with patch('interview_core.credential_available', return_value=True):
            self.advance(answer)
        self.assertIsNone(self.core.claim_job(job_type='routing'))
        with self.core._db() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE session_id=? AND job_type='routing'", (self.sid,)).fetchone()[0], 'superseded')

    def test_old_revision_is_not_applied_and_new_revision_can_be_claimed(self):
        answer = self.answer(); old = self.core.claim_job(job_type='routing')
        self.core.revise_transcript(self.sid, answer['answer_id'], 1, '我补充了与客户核对交付日期的过程。',
                                    '合成修订', str(uuid.uuid4()))
        with self.assertRaises(DomainError) as error:
            self.core.complete_routing_job(old['job_id'], old['lease_token'], self.result(old))
        self.assertEqual(error.exception.code, 'stale_model_input')
        new = self.core.claim_job(job_type='routing')
        self.assertEqual(new['input_versions'], {answer['answer_id']: 2})
        self.assertEqual(self.events(), [])

    def test_success_no_selected_branch_continues_main_with_model_attribution(self):
        answer = self.answer(); job = self.core.claim_job(job_type='routing')
        self.core.complete_routing_job(job['job_id'], job['lease_token'], self.result(job, None))
        state = self.advance(answer)
        self.assertEqual(state['current_round']['question_id'], 'results_K6')
        self.assertEqual(self.events()[0]['decision_source'], 'semantic-bank.v1')
        self.assertIsNone(self.events()[0]['selected_branch_id'])

    def test_followup_answer_is_scored_but_never_gets_recursive_routing(self):
        main = self.answer()
        with patch('interview_core.credential_available', return_value=False):
            self.advance(main)
        followup = self.answer('我把具体交付时间逐一发给团队成员确认。')
        with self.core._db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs WHERE answer_id=? AND job_type='routing'", (followup['answer_id'],)).fetchone()[0], 0)
        state = self.advance(followup)
        self.assertEqual(state['current_round']['kind'], 'main')
        self.assertEqual(self.events()[-1]['decision_source'], 'workflow-budget')

    def test_result_ready_after_deadline_is_not_adopted_even_before_next_poll(self):
        answer = self.answer(); job = self.core.claim_job(job_type='routing'); self.age_routing()
        self.core.complete_routing_job(job['job_id'], job['lease_token'], self.result(job))
        with patch('interview_core.credential_available', return_value=True):
            self.advance(answer)
        event = self.events()[0]
        self.assertEqual(event['decision_source'], 'dictionary-bank.v2-fallback')
        self.assertFalse(event['decision_summary']['model_involved_in_selection'])

    def test_sealed_audit_deadline_matches_six_second_semantic_budget(self):
        answer = self.answer()
        with patch('interview_core.credential_available', return_value=True):
            self.advance(answer)
        with self.core._db() as db:
            started = db.execute('SELECT decision_started FROM sessions WHERE id=?', (self.sid,)).fetchone()[0]
        with patch('interview_core.credential_available', return_value=True), patch('interview_core.time.time', return_value=started+7):
            self.advance(answer)
        event = self.events()[0]
        elapsed = datetime.fromisoformat(event['decision_deadline']).timestamp()-datetime.fromisoformat(event['wait_started_at']).timestamp()
        self.assertAlmostEqual(elapsed, 6, places=2)


class RoutingWorkerTests(unittest.TestCase):
    def test_worker_routing_timeout_is_eight_seconds_and_not_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Mock(data_dir=Path(directory))
            job = {'job_type': 'routing', 'job_id': 'synthetic', 'lease_token': 'fixture',
                   'question': {}, 'answers': [], 'branches': [], 'model_config': {}}
            proc = Mock(); proc.poll.return_value = None
            with patch('interview_worker.subprocess.Popen', return_value=proc) as popen, \
                 patch('interview_worker.time.monotonic', side_effect=[0, 9]), \
                 patch('interview_worker.stop_process') as stop:
                interview_worker.process_job(core, job, 300)
            self.assertIn('--routing-input', popen.call_args.args[0])
            self.assertEqual(core.fail_job.call_args.kwargs['code'], 'worker_timeout')
            self.assertFalse(core.fail_job.call_args.kwargs['retryable'])
            core.complete_routing_job.assert_not_called()
            self.assertTrue(stop.called)
            self.assertEqual(list((Path(directory)/'worker-tmp').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
