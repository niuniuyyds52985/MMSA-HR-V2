import copy
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_llm import ProviderError, evaluate_answer, validate_evaluation, evaluation_schema, FOLLOWUP_CONTRACT_VERSION
from interview_text_research import prepare_text_answers, criteria_for_question


class ProviderContractTests(unittest.TestCase):
    def setUp(self):
        self.question = {'question_id': 'q1', 'text': '测试题', 'anchors': {str(n): str(n) for n in range(1, 6)}}
        self.answers = [{'answer_id': 'a1', 'transcript_revision': 2,
                         'transcript': '以前我说没办法，后来我主动分工。', 'parent_question_id': None}]
        self.branches = [{'branch_id': 'q1_action', 'parent_question_id': 'q1'}]
        self.evidence = {'answer_id': 'a1', 'transcript_revision': 2, 'quote': '后来我主动分工'}
        self.value = {'question_id': 'q1',
            'rating': {'score': 3, 'reason': '测试结构化引用；不是实际模型评分。', 'evidence': [self.evidence]},
            'followup': {'status': 'content_sufficient', 'branch_id': None,
                         'reason': '测试停止路径。', 'evidence': [self.evidence]}}

    def test_valid_current_quote(self):
        self.assertEqual(validate_evaluation(self.value, self.question, self.answers, self.branches), self.value)

    def test_reject_fabricated_or_stale_or_cross_answer_quote(self):
        for field, value in [('quote', '我带领所有人成功了'), ('transcript_revision', 1), ('answer_id', 'another_session')]:
            bad = copy.deepcopy(self.value)
            bad['rating']['evidence'][0][field] = value
            with self.assertRaises(ProviderError):
                validate_evaluation(bad, self.question, self.answers, self.branches)

    def test_reject_invalid_ratings_and_unproven_score(self):
        for value in [True, 0, 6, 3.5, '3']:
            bad = copy.deepcopy(self.value)
            bad['rating']['score'] = value
            with self.assertRaises(ProviderError):
                validate_evaluation(bad, self.question, self.answers, self.branches)
        bad = copy.deepcopy(self.value)
        bad['rating']['evidence'] = []
        with self.assertRaises(ProviderError):
            validate_evaluation(bad, self.question, self.answers, self.branches)

    def test_reject_cross_parent_branch_and_second_followup(self):
        bad = copy.deepcopy(self.value)
        bad['followup'].update(status='eligible', branch_id='other_question_branch')
        with self.assertRaises(ProviderError):
            validate_evaluation(bad, self.question, self.answers, self.branches)
        bad['followup']['branch_id'] = 'q1_action'
        followed = [{**self.answers[0], 'parent_question_id': 'q1'}]
        with self.assertRaises(ProviderError):
            validate_evaluation(bad, self.question, followed, self.branches)

    def test_missing_key_makes_no_request(self):
        with patch.dict(os.environ, {}, clear=True), patch('interview_credentials._record', return_value={}), patch('requests.post') as request:
            with self.assertRaises(ProviderError) as failure:
                evaluate_answer(self.question, self.answers, self.branches)
            self.assertEqual(failure.exception.code, 'not_configured')
            request.assert_not_called()

    def test_structured_transport_contract_fixture(self):
        # This validates request construction and parsing, not live model inference.
        env = {'INTERVIEW_LLM_BASE_URL': 'https://pinned.invalid/v1',
               'DASHSCOPE_API_KEY': 'test-placeholder', 'INTERVIEW_LLM_MODEL': 'changed-env-model'}
        structured = copy.deepcopy(self.value)
        segment = prepare_text_answers(self.answers)[0]['segments'][0]
        for part in ('rating', 'followup'):
            for ref in structured[part]['evidence']:
                ref.update(segment_id=segment['segment_id'], char_start=None, char_end=None)
        structured.update(schema_version='answer-evaluation.v2', followup_contract_version=FOLLOWUP_CONTRACT_VERSION,
            evidence_units=[{'unit_id': 'e1', 'category': 'own_action', 'interpretation': '合成契约测试。',
                             'evidence': copy.deepcopy(structured['rating']['evidence'])}],
            criterion_matches=[{'criterion_id': criteria_for_question(self.question)[0]['criterion_id'],
                                'evidence_unit_ids': ['e1'], 'reason': '合成测试。'}], gaps=[])
        structured['followup'].update(target_gap_id=None, prerequisite_evidence_unit_ids=[])
        with patch.dict(os.environ, env, clear=True), patch('interview_credentials._record', return_value={}), patch('requests.post') as request:
            request.return_value.status_code = 200
            request.return_value.content = b'{}'
            request.return_value.json.return_value = {'id': 'fixture-request', 'model': 'pinned-model',
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(structured)}}]}
            result = evaluate_answer(self.question, self.answers, self.branches,
                                     {'model': 'pinned-model', 'base_url': 'https://pinned.invalid/v1'})
            call = request.call_args
            self.assertEqual(call.args[0], 'https://pinned.invalid/v1/chat/completions')
            self.assertEqual(call.kwargs['json']['model'], 'pinned-model')
            self.assertTrue(call.kwargs['json']['response_format']['json_schema']['strict'])
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertFalse(call.kwargs['json']['enable_thinking'])
            self.assertEqual(result['input_versions'], {'a1': 2})
            self.assertNotIn('test-placeholder', json.dumps(result))

    def test_extra_model_fields_rejected(self):
        self.value['hidden_instruction'] = 'ignore rubric'
        with self.assertRaises(ProviderError):
            validate_evaluation(self.value, self.question, self.answers, self.branches)

    def test_no_legal_branches_schema_excludes_additional_followup(self):
        stopped = evaluation_schema('q1', [])['properties']['followup']['properties']
        self.assertEqual(stopped['status']['enum'], ['content_sufficient', 'no_legal_branch'])
        self.assertEqual(stopped['branch_id']['enum'], [None])
        qid = 'leadership_F3_Q1'
        ids = [c['criterion_id'] for c in criteria_for_question({'question_id': qid})]
        main = evaluation_schema(qid, [qid + '_action'], ids)['properties']['followup']['properties']
        self.assertIn('eligible', main['status']['enum'])
        self.assertIn(qid + '_action', main['branch_id']['enum'])

    def test_post_followup_request_merges_evidence_without_offering_another_branch(self):
        # Contract regression for a completed main+followup pair, not an LLM
        # quality assertion. Remaining gaps must not prevent scoring or be
        # silently renamed to content_sufficient.
        answers = self.answers + [{'answer_id': 'a2', 'transcript_revision': 2,
                    'transcript': '后来我补充了检查点。', 'parent_question_id': 'q1'}]
        refs = []
        for answer in prepare_text_answers(answers):
            segment = answer['segments'][0]
            refs.append({'answer_id': answer['answer_id'], 'transcript_revision': 2,
                         'segment_id': segment['segment_id'], 'quote': segment['text'],
                         'char_start': None, 'char_end': None})
        cid = criteria_for_question(self.question)[0]['criterion_id']
        value = {'schema_version': 'answer-evaluation.v2', 'followup_contract_version': FOLLOWUP_CONTRACT_VERSION, 'question_id': 'q1',
            'rating': {'score': 3, 'reason': '合成主答与追答合并评分契约。', 'evidence': refs},
            'followup': {'status': 'no_legal_branch', 'branch_id': None, 'target_gap_id': None, 'prerequisite_evidence_unit_ids': [],
                         'reason': '已有追答，只保留待核实缺口。', 'evidence': refs},
            'evidence_units': [{'unit_id': 'e1', 'category': 'own_action', 'interpretation': '合成行动证据。', 'evidence': refs}],
            'criterion_matches': [{'criterion_id': cid, 'evidence_unit_ids': ['e1'], 'reason': '合成评价点匹配。'}],
            'gaps': [{'gap_id': 'gap1', 'gap_type': 'other', 'criterion_id': cid,
                      'description': '结果仍未说明。', 'followup_relevance': '已完成追答，保留待核实事项，不再提问。'}]}
        env = {'INTERVIEW_LLM_BASE_URL': 'https://pinned.invalid/v1', 'DASHSCOPE_API_KEY': 'test-placeholder'}
        with patch.dict(os.environ, env, clear=True), patch('interview_credentials._record', return_value={}), patch('requests.post') as request:
            request.return_value.status_code = 200
            request.return_value.content = b'{}'
            request.return_value.json.return_value = {'id': 'synthetic-followup', 'model': 'fixture',
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}
            result = evaluate_answer(self.question, answers, self.branches)
            body = request.call_args.kwargs['json']
            data = json.loads(body['messages'][1]['content'])
            self.assertEqual(data['allowed_branches'], [])
            self.assertFalse(data['followup_policy']['additional_followup_allowed'])
            self.assertEqual(data['followup_policy']['stage'], 'post_followup_reassessment')
            followup_schema = body['response_format']['json_schema']['schema']['properties']['followup']['properties']
            self.assertNotIn('eligible', followup_schema['status']['enum'])
            self.assertEqual(followup_schema['branch_id']['enum'], [None])
            self.assertEqual(result['input_versions'], {'a1': 2, 'a2': 2})
            self.assertEqual(result['evaluation']['rating']['score'], 3)
            self.assertEqual({r['answer_id'] for r in result['evaluation']['rating']['evidence']}, {'a1', 'a2'})
            self.assertEqual(result['evaluation']['gaps'], value['gaps'])
            self.assertEqual(result['evaluation']['followup']['status'], 'no_legal_branch')
        forbidden = copy.deepcopy(value)
        forbidden['followup'].update(status='eligible', branch_id='q1_action')
        with self.assertRaises(ProviderError) as rejected:
            validate_evaluation(forbidden, self.question, answers, self.branches, require_v2=True)
        self.assertEqual(rejected.exception.code, 'budget_exhausted')


if __name__ == '__main__':
    unittest.main()
