"""Synthetic business flow; no provider or private media."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_core import BASE, InterviewCore
from interview_followup import select_followup

class QuickFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'INTERVIEW_CREDENTIALS_FILE': str(Path(self.tmp.name)/'absent'), 'DASHSCOPE_API_KEY': ''})
        env.start()
        self.addCleanup(env.stop)
        self.core = InterviewCore(self.tmp.name, BASE/'config'/'interview.v3.json')
        self.sid = self.core.create_interview(str(uuid.uuid4()), 'synthetic-v3')['session_id']
    def answer(self, text):
        r = self.core.get_state(self.sid)['current_round']
        return self.core.submit_answer(self.sid, r['round_id'], str(uuid.uuid4()), str(uuid.uuid4()), text)
    def advance(self, answer, key=None):
        return self.core.next_round(self.sid, key or str(uuid.uuid4()), answer['round_id'])
    def events(self):
        with self.core._db() as db:
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM decision_events WHERE session_id=? ORDER BY rowid', (self.sid,))]
    def test_first_answer_asks_bank_followup_without_model_wait(self):
        a = self.answer('当时大家意见很多，最后项目还在进行中。')
        start = time.perf_counter()
        key = str(uuid.uuid4())
        state = self.advance(a, key)
        self.assertLess(time.perf_counter()-start, 1)
        self.assertEqual(state['current_round']['question_id'], 'leadership_F3_Q1_action')
        self.assertEqual(state, self.advance(a, key))
        event = self.events()[0]
        self.assertEqual(event['decision_source'], 'dictionary-bank.v1')
        self.assertFalse(event['model_run_ids'])
        self.assertFalse(event['decision_summary']['model_involved_in_selection'])
        self.assertEqual(event['input_versions'], {a['answer_id']:1})
        self.assertIn(event['used_evidence'][0]['quote'], a['transcript'])
    def test_full_four_round_interview_without_scoring_dependency(self):
        for text in ('大家讨论过这个问题。', '我把工作分成三部分并安排每个人的交付时间。', '先计算剩余目标，尝试两种获客方法。', '我从失败中总结出先验证再投入的办法。'):
            self.advance(self.answer(text))
        state = self.core.get_state(self.sid)
        self.assertEqual(state['status'], 'interview_finished')
        self.assertEqual(state['budget']['total'], 4)
        self.assertEqual(state['budget']['followup'], 1)
        self.assertTrue(all(j['status']=='queued' for j in state['jobs']))
    def test_missing_result_after_personal_action(self):
        self.advance(self.answer('我安排了三个同学分别联系客户，并每天核对进度。'))
        event = self.events()[0]
        self.assertEqual(event['selected_branch_id'], 'leadership_F3_Q1_result')
        self.assertIn('未命中不等于', event['reason_for_hr'])
    def test_all_cues_save_budget_for_learning_question(self):
        self.advance(self.answer('我安排三名同学分别联系客户。最终按时完成了项目。难点是两个人意见不同，我们开会解决。'))
        self.assertEqual(self.core.get_state(self.sid)['current_round']['question_id'], 'results_K6')
        self.advance(self.answer('我会细分目标并设置每两天的检查点。'))
        self.advance(self.answer('我总结了先试验再投入的方法，在另一个项目使用了。'))
        self.assertEqual(self.core.get_state(self.sid)['current_round']['question_id'], 'learning_F10_Q2_boundary')
    def test_negated_hits_auditable_without_being_positive_action(self):
        self.advance(self.answer('我没有安排同学联系客户，也没有分工。老师说要协调。'))
        event = self.events()[0]
        self.assertEqual(event['selected_branch_id'], 'leadership_F3_Q1_action')
        summary = event['decision_summary']
        matches = summary['dictionary_snapshot']['matches']
        self.assertTrue(matches)
        self.assertTrue(any(not m['retained'] for m in matches))
        self.assertEqual(summary['dictionary_snapshot']['usage'], 'comparator_only')
    def test_media_plan_starts_on_save(self):
        plan = self.core.get_state(self.sid)['analysis_plan']
        self.assertEqual(plan['trigger'], 'after_media_saved')
        self.assertEqual([p['id'] for p in plan['phases']], ['core', 'extended'])
    def test_other_person_and_internal_negation_do_not_cover_own_action(self):
        question = self.core.config['questions'][0]
        branches = [b for b in self.core.config['branches'] if b['parent_question_id'] == question['question_id']]
        for own_action in ('我的同学安排三名成员联系客户', '我们安排三名成员联系客户',
                           '我看到组长安排三名成员联系客户', '我不是负责安排任务的人',
                           '我不负责安排任务给同学', '我从来没有安排同学联系客户',
                           '我希望安排三名同学联系客户'):
            with self.subTest(text=own_action):
                text = own_action + '。最终按时完成了项目。难点是两个人意见不同。'
                routed = select_followup([{'answer_id':'synthetic', 'transcript_revision':1, 'transcript':text}], question, branches)
                self.assertEqual(routed['branch_id'], 'leadership_F3_Q1_action')
                self.assertEqual(routed['summary']['coverage_rules'][0]['status'], 'needs_clarification')
    def test_keyword_stuffing_does_not_exhaust_evidence_gaps(self):
        text = '我负责分工沟通协调组织安排。最终完成了提升降低获得。困难冲突分歧阻力。'
        self.advance(self.answer(text))
        event = self.events()[0]
        self.assertEqual(event['selected_branch_id'], 'leadership_F3_Q1_action')
        rules = event['decision_summary']['coverage_rules']
        self.assertTrue(all(r['status'] == 'needs_clarification' for r in rules))
        self.assertTrue(all(any('keyword_stuffing_or_no_concrete_complement' in h['flags'] for h in r['hits']) for r in rules))
    def test_learning_boundary_requires_a_method_cue(self):
        question = self.core.config['questions'][2]
        branches = [b for b in self.core.config['branches'] if b['parent_question_id'] == question['question_id']]
        for text in ('不知道，没有什么方法。', '我总结了方法规律经验教训。',
                     '我看到同学总结了先试验再投入的方法。', '我没有总结出先验证再投入的方法。'):
            with self.subTest(text=text):
                routed = select_followup([{'answer_id':'synthetic', 'transcript_revision':1, 'transcript':text}], question, branches)
                self.assertIsNone(routed['branch_id'])
                self.assertEqual(routed['stop_reason'], 'prerequisite_not_met')
                prerequisite = routed['summary']['coverage_rules'][0]['prerequisite']
                self.assertEqual(prerequisite['criterion_id'], 'learning_F10_Q2:method')
                self.assertEqual(prerequisite['category'], 'reflection_transfer')
                self.assertEqual(prerequisite['status'], 'needs_clarification')
    def test_concrete_past_action_and_method_remain_eligible(self):
        question = self.core.config['questions'][0]
        branches = [b for b in self.core.config['branches'] if b['parent_question_id'] == question['question_id']]
        text = '过去我安排了三名同学分别联系客户，并每天核对进度。最终按时完成了项目。难点是两个人意见不同，我们开会解决。'
        routed = select_followup([{'answer_id':'synthetic', 'transcript_revision':1, 'transcript':text}], question, branches)
        self.assertIsNone(routed['branch_id'])
        self.assertTrue(all(r['status'] == 'lexical_cue_present' for r in routed['summary']['coverage_rules']))
        question = self.core.config['questions'][2]
        branches = [b for b in self.core.config['branches'] if b['parent_question_id'] == question['question_id']]
        for text in ('我从失败中总结出先验证再投入的方法。', '方法是先调查客户需求，再选择服务方案。'):
            routed = select_followup([{'answer_id':'synthetic', 'transcript_revision':1, 'transcript':text}], question, branches)
            self.assertEqual(routed['branch_id'], 'learning_F10_Q2_boundary')
            self.assertEqual(routed['summary']['coverage_rules'][0]['prerequisite']['status'], 'lexical_cue_present')

if __name__ == '__main__':
    unittest.main()
