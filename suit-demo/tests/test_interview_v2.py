"""Synthetic per-answer checkpoints; no provider requests or real candidate data."""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_core import BASE, DomainError, InterviewCore


def key():
    return str(uuid.uuid4())


class PerAnswerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        env = patch.dict(os.environ, {'INTERVIEW_CREDENTIALS_FILE': str(Path(self.directory.name) / 'absent'), 'DASHSCOPE_API_KEY': ''})
        env.start()
        self.addCleanup(env.stop)
        self.core = InterviewCore(self.directory.name)
        self.created = self.core.create_interview(key(), 'synthetic-v2')
        self.sid = self.created['session_id']

    def answer(self):
        current = self.core.get_state(self.sid)['current_round']
        return self.core.submit_answer(self.sid, current['round_id'], key(), key(), '合成案例：我负责协调并逐日核对结果。')

    def advance(self, request_key=None, round_id=None):
        current = self.core.get_state(self.sid)['current_round']
        return self.core.next_round(self.sid, request_key or key(), round_id or (current['round_id'] if current else 'done'))

    def evaluate(self, answer, status='content_sufficient', branch=None):
        qid = answer['parent_question_id'] or answer['question_id']
        with self.core._db() as db:
            answers = self.core._content_inputs(db, self.sid, qid)
        evidence = [{'answer_id': a['answer_id'], 'transcript_revision': a['transcript_revision'], 'quote': a['transcript']} for a in answers]
        result = {'question_id': qid,
                  'rating': {'score': 4, 'reason': '合成测试评分锚点说明', 'evidence': evidence},
                  'followup': {'status': status, 'branch_id': branch, 'reason': '合成测试内容判断理由', 'evidence': evidence}}
        return self.core.apply_model_evaluation(self.sid, qid, result, {a['answer_id']: a['transcript_revision'] for a in answers}, {'model': 'synthetic-model'})

    def events(self):
        return self.core.get_report(self.sid)['decision_events']

    def test_q1_followup_is_immediate_then_q2_q3_with_four_total(self):
        first = self.answer()
        self.evaluate(first, 'eligible', 'leadership_F3_Q1_action')
        state = self.advance()
        self.assertEqual(state['current_round']['question_id'], 'leadership_F3_Q1_action')
        self.assertEqual(state['budget']['main'], 1)
        self.assertEqual(state['budget']['total'], 2)
        self.answer()
        self.assertEqual(self.advance()['current_round']['question_id'], 'results_K6')
        second = self.answer()
        self.evaluate(second, 'no_legal_branch')
        self.assertEqual(self.advance()['current_round']['question_id'], 'learning_F10_Q2')
        third = self.answer()
        self.evaluate(third, 'eligible', 'learning_F10_Q2_boundary')
        state = self.advance()
        self.assertEqual(state['status'], 'interview_finished')
        self.assertEqual(state['budget'], {'main': 3, 'followup': 1, 'total': 4, 'maximum': 4})
        events = self.events()
        self.assertEqual([e['action'] for e in events], ['ask_followup', 'continue_main', 'continue_main', 'finish'])
        self.assertEqual(events[-1]['stop_reason'], 'followup_budget_exhausted')
        self.assertEqual(events[0]['target_factor_id'], 'leadership')
        self.assertTrue(events[0]['used_evidence_ids'])
        self.assertTrue(events[0]['model_run_ids'])
        self.assertEqual(events[0]['input_versions'], {first['answer_id']: 1})
        self.assertEqual(events[0]['actual_next_round']['kind'], 'followup')

    def test_sufficient_answers_continue_fixed_order_then_finish(self):
        for qid in ('leadership_F3_Q1', 'results_K6', 'learning_F10_Q2'):
            self.assertEqual(self.core.get_state(self.sid)['current_round']['question_id'], qid)
            answer = self.answer()
            self.evaluate(answer)
            self.advance()
        self.assertEqual(self.core.get_state(self.sid)['status'], 'interview_finished')
        self.assertEqual([e['action'] for e in self.events()], ['continue_main', 'continue_main', 'finish'])
        self.assertTrue(all(e['stop_reason'] == 'content_sufficient' for e in self.events()))

    def test_q3_can_use_unspent_followup_and_finishes_after_answer(self):
        for _ in range(2):
            answer = self.answer()
            self.evaluate(answer)
            self.advance()
        third = self.answer()
        self.evaluate(third, 'eligible', 'learning_F10_Q2_boundary')
        self.assertEqual(self.advance()['current_round']['question_id'], 'learning_F10_Q2_boundary')
        self.answer()
        self.assertEqual(self.advance()['status'], 'interview_finished')
        self.assertEqual(len(self.events()), 4)

    def test_concurrent_same_next_key_seals_one_event_and_issues_one_round(self):
        answer = self.answer()
        self.evaluate(answer, 'eligible', 'leadership_F3_Q1_action')
        request_key = key()
        with ThreadPoolExecutor(max_workers=4) as executor:
            responses = list(executor.map(lambda _: self.advance(request_key, answer['round_id']), range(4)))
        self.assertTrue(all(r == responses[0] for r in responses))
        self.assertEqual(len(self.events()), 1)
        self.assertEqual(len(self.core.get_state(self.sid)['rounds']), 2)
        with self.assertRaises(DomainError):
            self.advance(key(), answer['round_id'])

    def test_wait_is_per_current_question_bounded_and_late_output_cannot_rewrite(self):
        answer = self.answer()
        request_key = key()
        with patch('interview_core.credential_available', return_value=True):
            state = self.advance(request_key, answer['round_id'])
            self.assertTrue(state['decision_pending'])
            self.assertEqual(len(self.events()), 0)
            with self.core._db(True) as db:
                db.execute('UPDATE sessions SET decision_started=? WHERE id=?', (time.time() - 61, self.sid))
            state = self.advance(request_key, answer['round_id'])
        self.assertEqual(state['current_round']['question_id'], 'results_K6')
        sealed = copy.deepcopy(self.events())
        self.assertEqual(sealed[0]['stop_reason'], 'content_evaluation_timeout')
        result = self.evaluate(answer, 'eligible', 'leadership_F3_Q1_action')
        self.assertTrue(result['decision_sealed'])
        self.assertEqual(self.events(), sealed)
        second = self.answer()
        self.evaluate(second, 'no_legal_branch')
        with patch('interview_core.credential_available', return_value=True):
            self.assertEqual(self.advance()['current_round']['question_id'], 'learning_F10_Q2')
        self.assertEqual(len(self.events()), 2)
        with self.core._db(True) as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE decision_events SET payload='{}' WHERE session_id=?", (self.sid,))

    def test_old_v1_session_keeps_after_all_main_snapshot(self):
        old = InterviewCore(self.directory.name, config_path=BASE / 'config' / 'interview.v1.json')
        created = old.create_interview(key(), 'synthetic-v1')
        sid = created['session_id']
        answer = old.submit_answer(sid, created['current_round']['round_id'], key(), key(), '合成旧版本回答。')
        advanced = self.core.next_round(sid, key(), answer['round_id'])
        self.assertEqual(advanced['config_version'], 'interview.v1')
        self.assertEqual(advanced['current_round']['question_id'], 'results_K6')
        self.assertEqual(self.core.get_report(sid)['decision_events'], [])

    def test_main_without_branch_advances_while_content_and_media_remain_pending(self):
        first = self.answer()
        self.evaluate(first)
        self.advance()
        second = self.answer()
        with self.core._db(True) as db:
            db.execute("INSERT INTO jobs(id,session_id,answer_id,status,attempts,input_revision,created_at,job_type) VALUES(?,?,?,'queued',0,1,'2026-01-01','media')",
                       (key(), self.sid, second['answer_id']))
        with patch('interview_core.credential_available', return_value=True):
            state = self.advance()
        self.assertEqual(state['current_round']['question_id'], 'learning_F10_Q2')
        self.assertFalse(state['decision_pending'])
        self.assertEqual(self.events()[-1]['stop_reason'], 'no_legal_branch')
        self.assertTrue(any(j['answer_id'] == second['answer_id'] and j['status'] == 'queued' for j in state['jobs']))
        # A later score is still accepted without changing the sealed next step.
        sealed = copy.deepcopy(self.events())
        self.assertTrue(self.evaluate(second)['decision_sealed'])
        self.assertEqual(self.events(), sealed)

    def test_spent_followup_budget_never_waits_for_later_main_scores(self):
        first = self.answer()
        self.evaluate(first, 'eligible', 'leadership_F3_Q1_action')
        self.advance()
        self.answer()
        self.advance()
        with patch('interview_core.credential_available', return_value=True):
            second = self.answer()
            state = self.advance()
            self.assertEqual(state['current_round']['question_id'], 'learning_F10_Q2')
            self.assertFalse(state['decision_pending'])
            third = self.answer()
            state = self.advance()
        self.assertEqual(state['status'], 'interview_finished')
        self.assertFalse(state['decision_pending'])
        self.assertEqual([e['stop_reason'] for e in self.events()[-2:]],
                         ['followup_budget_exhausted', 'followup_budget_exhausted'])
        pending_answers = {j['answer_id'] for j in state['jobs'] if j['status'] == 'queued'}
        self.assertTrue({second['answer_id'], third['answer_id']} <= pending_answers)

    def test_only_valid_measured_audio_is_an_observation_and_never_selects_branch(self):
        fixtures = [({'status': 'no_audio', 'windows': []}, False),
                    ({'status': 'ok', 'windows': [{'start_ms': 0, 'end_ms': 100, 'rms_dbfs': -20.5}]}, True),
                    ({'status': 'ok', 'windows': [{'start_ms': 10, 'end_ms': 0, 'rms_dbfs': -20}]}, False)]
        for audio, expected in fixtures:
            self.sid = self.core.create_interview(key(), 'synthetic-audio')['session_id']
            answer = self.answer()
            self.evaluate(answer)
            with self.core._db(True) as db:
                db.execute("INSERT INTO jobs(id,session_id,answer_id,status,attempts,input_revision,result,created_at,ready_at,job_type) VALUES(?,?,?,'succeeded',1,1,?,'2020-01-01','2020-01-01','media')", (key(), self.sid, answer['answer_id'], json.dumps({'audio': audio})))
            self.advance()
            event = self.events()[0]
            self.assertEqual(bool(event['observed_only']), expected)
            self.assertEqual('audio' in event['available_modalities'], expected)
            self.assertEqual(event['action'], 'continue_main')


if __name__ == '__main__':
    unittest.main()
