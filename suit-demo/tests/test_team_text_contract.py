"""Synthetic source/transport contracts; never a semantic accuracy benchmark."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_llm import (ProviderError, evaluate_answer, validate_evaluation, evaluation_schema,
                           followup_contract, FOLLOWUP_CONTRACT_VERSION)
from interview_text_research import prepare_text_answers, criteria_for_question, run_text_baseline


ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((Path(__file__).parent / 'fixtures' / 'team-text-contract.v1.json').read_text(encoding='utf-8'))['cases']
CONFIG = json.loads((ROOT / 'config' / 'interview.v2.json').read_text(encoding='utf-8'))
QUESTIONS = {q['question_id']: q for q in CONFIG['questions']}


def inputs(case):
    return [{'answer_id': 'synthetic-' + case['case_id'], 'transcript_revision': 2,
             'transcript': case['transcript'], 'parent_question_id': None}]


def draft(question, answers):
    # The category is deliberately neutral and score is null. These fixtures do
    # not pretend to supply correct semantic labels for the synthetic statements.
    refs = [{'answer_id': a['answer_id'], 'transcript_revision': a['transcript_revision'],
             'segment_id': s['segment_id'], 'quote': s['text'], 'char_start': None, 'char_end': None}
            for a in prepare_text_answers(answers) for s in a['segments']]
    return {'schema_version': 'answer-evaluation.v2', 'followup_contract_version': FOLLOWUP_CONTRACT_VERSION,
            'question_id': question['question_id'],
            'evidence_units': [{'unit_id': 'source', 'category': 'background',
                                'interpretation': '合成输入原文，仅验证引用保护。', 'evidence': copy.deepcopy(refs)}],
            'criterion_matches': [], 'gaps': [],
            'rating': {'score': None, 'reason': '契约夹具不提供能力评分。', 'evidence': copy.deepcopy(refs)},
            'followup': {'status': 'no_legal_branch', 'branch_id': None,
                         'target_gap_id': None, 'prerequisite_evidence_unit_ids': [],
                         'reason': '本测试未开放执行分支，不代表语义证据充分。', 'evidence': []}}


class TeamTextContractTests(unittest.TestCase):
    def test_exact_sources_and_offsets_survive_all_six_counterexamples(self):
        for case in CASES:
            with self.subTest(case=case['case_id']):
                answers, question = inputs(case), QUESTIONS[case['question_id']]
                before = copy.deepcopy(answers)
                value = validate_evaluation(draft(question, answers), question, answers, [], require_v2=True)
                for ref in value['rating']['evidence']:
                    self.assertEqual(case['transcript'][ref['char_start']:ref['char_end']], ref['quote'])
                    self.assertEqual(ref['transcript_revision'], 2)
                self.assertEqual(answers, before)
                self.assertIsNone(value['rating']['score'])

    def test_changed_subject_negation_number_result_or_tense_is_not_an_exact_quote(self):
        for case in CASES:
            with self.subTest(case=case['case_id']):
                answers, question = inputs(case), QUESTIONS[case['question_id']]
                bad = draft(question, answers)
                bad['rating']['evidence'][0]['quote'] = case['fabricated_quote']
                with self.assertRaises(ProviderError) as failure:
                    validate_evaluation(bad, question, answers, [], require_v2=True)
                self.assertEqual(failure.exception.code, 'invented_quote')

    def test_model_input_preserves_text_but_discards_biometrics_and_arbitrary_instructions(self):
        for case in CASES:
            with self.subTest(case=case['case_id']):
                question, answers = QUESTIONS[case['question_id']], inputs(case)
                answers[0].update(multimodal_signals={'AU4': 0.9}, pitch_hz=250,
                                  image_url='https://synthetic.invalid/frame', override_score=5)
                response = draft(question, answers)
                settings = {'model': 'synthetic-model', 'base_url': 'https://synthetic.invalid/v1'}
                with patch('interview_llm.provider_status', return_value={**settings, 'status': 'configured_not_verified'}), \
                     patch('interview_llm.public_settings', return_value=settings), \
                     patch('interview_llm.get_api_key', return_value='synthetic-placeholder'), \
                     patch('requests.post') as request:
                    request.return_value.status_code = 200
                    request.return_value.content = b'{}'
                    request.return_value.json.return_value = {'id': 'synthetic-request', 'model': 'synthetic-model',
                        'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(response)}}]}
                    result = evaluate_answer(question, answers, [])
                    body = request.call_args.kwargs['json']
                    payload = json.loads(body['messages'][1]['content'])
                    sent = payload['answers'][0]
                    self.assertEqual(sent['transcript'], case['transcript'])
                    self.assertEqual(set(sent), {'answer_id', 'transcript_revision', 'transcript', 'parent_question_id', 'segments'})
                    self.assertEqual(payload['allowed_branches'], [])
                    self.assertEqual(payload['followup_contract']['version'], FOLLOWUP_CONTRACT_VERSION)
                    self.assertFalse(body['enable_thinking'])
                    self.assertIsNone(result['evaluation']['rating']['score'])
                    self.assertEqual(result['provider']['followup_contract_snapshot'], payload['followup_contract'])

    def test_vague_and_specific_answers_never_get_scores_from_lexical_comparator(self):
        for case in [c for c in CASES if c['case_id'] in {'vague_answer', 'specific_behavior_without_keyword'}]:
            with self.subTest(case=case['case_id']):
                value = run_text_baseline(inputs(case), QUESTIONS[case['question_id']])
                self.assertEqual(value['matches'], [])
                self.assertEqual(value['usage'], 'research_comparator_only')
                self.assertNotIn('score', value)
                self.assertNotIn('followup', value)
                self.assertNotIn('recommendation', value)

    def test_demo_dimension_and_skill_fractional_score_cannot_be_imported_as_current_rating(self):
        case = CASES[0]
        answers, question = inputs(case), QUESTIONS[case['question_id']]
        value = draft(question, answers)
        value['rating']['score'] = 3
        value['criterion_matches'] = [{'criterion_id': 'planning:goal_decomposition',
                                       'evidence_unit_ids': ['source'], 'reason': '外部维度不能冒充本题评价点。'}]
        with self.assertRaises(ProviderError) as failure:
            validate_evaluation(value, question, answers, [], require_v2=True)
        self.assertEqual(failure.exception.code, 'invalid_criterion')
        value['criterion_matches'][0]['criterion_id'] = criteria_for_question(question)[0]['criterion_id']
        value['rating']['score'] = 3.5
        with self.assertRaises(ProviderError) as failure:
            validate_evaluation(value, question, answers, [], require_v2=True)
        self.assertEqual(failure.exception.code, 'invalid_score')

    def test_exact_quote_check_is_not_a_claim_to_understand_negation(self):
        # Document the actual boundary: a literal substring is valid evidence
        # location even if its unsupported semantic interpretation would be bad.
        # Semantic correctness needs independent human reference evaluation.
        case = CASES[0]
        answers, question = inputs(case), QUESTIONS[case['question_id']]
        value = draft(question, answers)
        value['rating']['evidence'][0]['quote'] = '负责分工'
        checked = validate_evaluation(value, question, answers, [], require_v2=True)
        self.assertEqual(checked['rating']['evidence'][0]['quote'], '负责分工')
        self.assertIsNone(checked['rating']['score'])


class GapBranchContractTests(unittest.TestCase):
    def setUp(self):
        self.question = QUESTIONS['leadership_F3_Q1']
        self.answers = inputs(CASES[3])
        self.value = draft(self.question, self.answers)

    def validate(self, value=None, question=None, answers=None, branches=None):
        return validate_evaluation(value if value is not None else self.value,
            question or self.question, answers or self.answers,
            CONFIG['branches'] if branches is None else branches, require_v2=True, require_current=True)

    def gap(self, kind='own_coordination_action', criterion='coordination', gid='gap1'):
        return {'gap_id': gid, 'gap_type': kind, 'criterion_id': self.question['question_id'] + ':' + criterion,
                'description': '合成缺口，仅验证类型和分支对应。', 'followup_relevance': '合成目标信息。'}

    def eligible(self, branch='leadership_F3_Q1_action', target='gap1'):
        self.value['followup'].update(status='eligible', branch_id=branch, target_gap_id=target,
                                     evidence=copy.deepcopy(self.value['rating']['evidence']))

    def test_each_leadership_branch_requires_its_exact_gap_type_and_criterion(self):
        for kind, criterion, suffix in [('own_coordination_action', 'coordination', 'action'),
                                        ('outcome', 'outcome_cost', 'result'),
                                        ('difficulty_or_tradeoff', 'outcome_cost', 'difficulty')]:
            with self.subTest(branch=suffix):
                self.value['gaps'] = [self.gap(kind, criterion)]
                self.eligible('leadership_F3_Q1_' + suffix)
                self.assertEqual(self.validate()['followup']['target_gap_id'], 'gap1')

    def test_action_gap_cannot_select_result_branch_even_if_another_result_gap_exists(self):
        self.value['gaps'] = [self.gap(), self.gap('outcome', 'outcome_cost', 'gap2')]
        self.eligible('leadership_F3_Q1_result', target='gap1')
        with self.assertRaises(ProviderError) as failure:
            self.validate()
        self.assertEqual(failure.exception.code, 'invalid_gap_branch')
        self.value['followup']['target_gap_id'] = 'gap2'
        self.assertEqual(self.validate()['followup']['branch_id'], 'leadership_F3_Q1_result')

    def test_gap_labels_do_not_bypass_criterion_parent_or_unique_id_constraints(self):
        self.value['gaps'] = [self.gap()]
        self.eligible()
        variants = []
        for field, content in [('gap_type', 'method_boundary'), ('criterion_id', 'leadership_F3_Q1:outcome_cost'),
                               ('gap_id', {'bad': 'id'})]:
            bad = copy.deepcopy(self.value);bad['gaps'][0][field] = content;variants.append(bad)
        duplicate = copy.deepcopy(self.value);duplicate['gaps'] *= 2;variants.append(duplicate)
        unknown = copy.deepcopy(self.value);unknown['followup']['target_gap_id'] = 'missing';variants.append(unknown)
        foreign = copy.deepcopy(self.value);foreign['followup']['branch_id'] = 'learning_F10_Q2_boundary';variants.append(foreign)
        for value in variants:
            with self.subTest(value=value), self.assertRaises(ProviderError):
                self.validate(value)

    def learning_case(self):
        self.question = QUESTIONS['learning_F10_Q2']
        self.answers = [{'answer_id': 'synthetic-method', 'transcript_revision': 2,
                         'transcript': '我总结的方法是先用小样本核对需求，再决定是否扩大。', 'parent_question_id': None}]
        self.value = draft(self.question, self.answers)
        self.value['gaps'] = [self.gap('method_boundary', 'effect_boundary')]
        self.eligible('learning_F10_Q2_boundary')

    def test_boundary_requires_method_criterion_with_explicit_reflection_source_units(self):
        self.learning_case()
        with self.assertRaises(ProviderError) as failure:
            self.validate()
        self.assertEqual(failure.exception.code, 'missing_prerequisite_evidence')
        self.value['followup']['prerequisite_evidence_unit_ids'] = ['source']
        self.value['criterion_matches'] = [{'criterion_id': 'learning_F10_Q2:method',
                                           'evidence_unit_ids': ['source'], 'reason': '合成方法对应，不声称语义准确。'}]
        with self.assertRaises(ProviderError) as failure:
            self.validate()  # background cannot masquerade as method evidence
        self.assertEqual(failure.exception.code, 'missing_prerequisite_evidence')
        self.value['evidence_units'][0]['category'] = 'reflection_transfer'
        self.assertEqual(self.validate()['followup']['prerequisite_evidence_unit_ids'], ['source'])
        self.value['followup']['prerequisite_evidence_unit_ids'] = ['unknown']
        with self.assertRaises(ProviderError):
            self.validate()

    def test_missing_method_is_retained_without_forcing_boundary_question_or_low_score(self):
        self.learning_case()
        self.value['gaps'] = [self.gap('learning_method', 'method')]
        with self.assertRaises(ProviderError) as failure:
            self.validate()
        self.assertEqual(failure.exception.code, 'invalid_gap_branch')
        self.value['followup'].update(status='no_legal_branch', branch_id=None, target_gap_id=None,
                                     prerequisite_evidence_unit_ids=[])
        checked = self.validate()
        self.assertIsNone(checked['rating']['score'])
        self.assertEqual(checked['gaps'][0]['gap_type'], 'learning_method')
        self.value['followup']['status'] = 'content_sufficient'
        with self.assertRaises(ProviderError) as failure:
            self.validate()
        self.assertEqual(failure.exception.code, 'invalid_gap')

    def test_no_legal_branch_and_post_followup_preserve_unresolved_gaps(self):
        self.question = QUESTIONS['results_K6']
        self.answers = inputs(CASES[2])
        self.value = draft(self.question, self.answers)
        self.value['gaps'] = [self.gap('plan_checkpoints', 'plan_checkpoints')]
        checked = self.validate(branches=[])
        self.assertEqual(checked['gaps'], self.value['gaps'])
        self.assertIsNone(checked['rating']['score'])
        self.learning_case()
        self.answers[0]['parent_question_id'] = self.question['question_id']
        self.value['followup'].update(status='no_legal_branch', branch_id=None, target_gap_id=None)
        self.assertEqual(self.validate(branches=[])['gaps'][0]['gap_type'], 'method_boundary')
        self.eligible('learning_F10_Q2_boundary')
        with self.assertRaises(ProviderError) as failure:
            self.validate()
        self.assertEqual(failure.exception.code, 'budget_exhausted')

    def test_unmapped_gap_is_retained_but_cannot_execute_branch(self):
        self.value['gaps'] = [self.gap('other', 'coordination')]
        checked = self.validate()
        self.assertEqual(checked['gaps'][0]['gap_type'], 'other')
        self.eligible()
        with self.assertRaises(ProviderError) as failure:
            self.validate()
        self.assertEqual(failure.exception.code, 'invalid_gap_branch')

    def test_legacy_v2_remains_readable_but_is_rejected_as_a_new_output(self):
        legacy = copy.deepcopy(self.value)
        legacy.pop('followup_contract_version')
        legacy['followup'].pop('target_gap_id')
        legacy['followup'].pop('prerequisite_evidence_unit_ids')
        before = copy.deepcopy(legacy)
        self.assertEqual(validate_evaluation(legacy, self.question, self.answers, [], require_v2=True)['gaps'], [])
        self.assertEqual(legacy, before)
        with self.assertRaises(ProviderError) as failure:
            self.validate(legacy)
        self.assertEqual(failure.exception.code, 'invalid_schema')
        bad = copy.deepcopy(self.value);bad['followup_contract_version'] = 'unknown'
        with self.assertRaises(ProviderError):
            self.validate(bad)

    def test_schema_and_policy_only_expose_explicitly_configured_parent_branches(self):
        for question in CONFIG['questions']:
            ids = [c['criterion_id'] for c in criteria_for_question(question)]
            own = [b['branch_id'] for b in CONFIG['branches'] if b['parent_question_id'] == question['question_id']]
            schema = evaluation_schema(question['question_id'], own + ['arbitrary_new_branch'], ids)
            contract = followup_contract(question['question_id'], own, ids)
            self.assertEqual(set(contract['branch_rules']), set(own))
            self.assertEqual(schema['properties']['followup']['properties']['branch_id']['enum'], [None, *contract['branch_rules']])
            self.assertIn('followup_contract_version', schema['required'])
            self.assertIn('gap_type', schema['properties']['gaps']['items']['required'])
        schema = evaluation_schema('unknown-question', ['unknown-question_action'], ['unknown-question:anchor_3'])
        self.assertNotIn('eligible', schema['properties']['followup']['properties']['status']['enum'])

    def provider_response(self, value):
        return {'id': 'synthetic-request', 'model': 'synthetic-model',
                'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(value)}}]}

    def test_new_provider_request_rejects_unversioned_output_instead_of_silently_downgrading(self):
        legacy = copy.deepcopy(self.value)
        legacy.pop('followup_contract_version')
        legacy['followup'].pop('target_gap_id')
        legacy['followup'].pop('prerequisite_evidence_unit_ids')
        settings = {'model': 'synthetic-model', 'base_url': 'https://synthetic.invalid/v1'}
        with patch('interview_llm.provider_status', return_value={**settings, 'status': 'configured_not_verified'}), \
             patch('interview_llm.public_settings', return_value=settings), \
             patch('interview_llm.get_api_key', return_value='synthetic-placeholder'), \
             patch('interview_llm.time.sleep'), patch('requests.post') as request:
            request.return_value.status_code = 200
            request.return_value.content = b'{}'
            request.return_value.json.return_value = self.provider_response(legacy)
            with self.assertRaises(ProviderError) as failure:
                evaluate_answer(self.question, self.answers, CONFIG['branches'])
            self.assertEqual(failure.exception.code, 'invalid_schema')
            self.assertEqual(request.call_count, 2)

    def test_provider_retries_wrong_branch_and_only_returns_a_validated_mapping(self):
        self.value['gaps'] = [self.gap()]
        self.eligible('leadership_F3_Q1_result')
        corrected = copy.deepcopy(self.value)
        corrected['followup']['branch_id'] = 'leadership_F3_Q1_action'
        settings = {'model': 'synthetic-model', 'base_url': 'https://synthetic.invalid/v1'}
        with patch('interview_llm.provider_status', return_value={**settings, 'status': 'configured_not_verified'}), \
             patch('interview_llm.public_settings', return_value=settings), \
             patch('interview_llm.get_api_key', return_value='synthetic-placeholder'), \
             patch('interview_llm.time.sleep'), patch('requests.post') as request:
            request.return_value.status_code = 200
            request.return_value.content = b'{}'
            request.return_value.json.side_effect = [self.provider_response(self.value), self.provider_response(corrected)]
            result = evaluate_answer(self.question, self.answers, CONFIG['branches'])
            self.assertEqual(result['evaluation']['followup']['branch_id'], 'leadership_F3_Q1_action')
            self.assertEqual(result['provider']['attempt_count'], 2)
            payload = json.loads(request.call_args.kwargs['json']['messages'][1]['content'])
            self.assertEqual(set(payload['followup_contract']['branch_rules']),
                             {b['branch_id'] for b in CONFIG['branches'] if b['parent_question_id'] == self.question['question_id']})
            self.assertEqual(self.value['followup']['branch_id'], 'leadership_F3_Q1_result')

    def test_new_contract_version_survives_storage_without_rewriting_legacy_evaluation(self):
        from interview_core import InterviewCore
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                'INTERVIEW_CREDENTIALS_FILE': str(Path(directory) / 'absent'), 'DASHSCOPE_API_KEY': ''}):
            core = InterviewCore(directory)
            session = core.create_interview(str(uuid.uuid4()), 'SIM-TEXT-CONTRACT')
            sid = session['session_id']
            current = core.get_state(sid)['current_round']
            core.submit_answer(sid, current['round_id'], str(uuid.uuid4()), str(uuid.uuid4()), CASES[3]['transcript'])
            with core._db() as db:
                answers = core._content_inputs(db, sid, self.question['question_id'])
            value = draft(self.question, answers)
            legacy = copy.deepcopy(value)
            legacy.pop('followup_contract_version')
            legacy['followup'].pop('target_gap_id')
            legacy['followup'].pop('prerequisite_evidence_unit_ids')
            versions = {a['answer_id']: a['transcript_revision'] for a in answers}
            old = core.apply_model_evaluation(sid, self.question['question_id'], legacy, versions, {'model': 'synthetic-legacy'})
            with core._db() as db:
                original = db.execute('SELECT payload FROM model_evaluations WHERE id=?', (old['evaluation_id'],)).fetchone()[0]
            core.apply_model_evaluation(sid, self.question['question_id'], value, versions,
                                        {'model': 'synthetic-current', 'followup_contract_version': FOLLOWUP_CONTRACT_VERSION})
            with core._db() as db:
                unchanged = db.execute('SELECT payload FROM model_evaluations WHERE id=?', (old['evaluation_id'],)).fetchone()[0]
            self.assertEqual(unchanged, original)
            self.assertNotIn('followup_contract_version', json.loads(original))
            stored = core.get_report(sid)['questions'][0]['model_evaluation']
            self.assertEqual(stored['followup_contract_version'], FOLLOWUP_CONTRACT_VERSION)
            self.assertIn('target_gap_id', stored['followup'])
            self.assertEqual(stored['input_versions'], versions)


if __name__ == '__main__':
    unittest.main()
