"""Deterministic contract/lexical tests, not evidence of LLM semantic accuracy."""
import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_llm import ProviderError, validate_evaluation, evaluate_answer, evaluation_schema
from interview_text_research import (prepare_text_answers, criteria_for_question,
                                      run_text_baseline, question_context, CONFIG_PATH)
from interview_timeline import _segment


def answer(text, aid='a1', revision=2):
    return {'answer_id': aid, 'transcript_revision': revision, 'transcript': text,
            'parent_question_id': None}


QUESTION = {'question_id': 'leadership_F3_Q1', 'factor_id': 'leadership',
            'text': '请讲一次主动组织协调的经历。', 'anchors': {'1': '低', '3': '中', '5': '高'}}


def fixture_value(answers):
    prepared = prepare_text_answers(answers)
    a, s = prepared[0], prepared[0]['segments'][0]
    ref = {'answer_id': a['answer_id'], 'transcript_revision': a['transcript_revision'],
           'segment_id': s['segment_id'], 'quote': s['text'], 'char_start': None, 'char_end': None}
    return {'schema_version': 'answer-evaluation.v2', 'question_id': QUESTION['question_id'],
        'rating': {'score': 3, 'reason': '合成结构测试，不是实际评分。', 'evidence': [ref]},
        'followup': {'status': 'content_sufficient', 'branch_id': None,
                     'reason': '合成停止路径。', 'evidence': [copy.deepcopy(ref)]},
        'evidence_units': [{'unit_id': 'e1', 'category': 'own_action',
            'interpretation': '参与者自述的行动；合成契约测试。', 'evidence': [copy.deepcopy(ref)]}],
        'criterion_matches': [{'criterion_id': criteria_for_question(QUESTION)[0]['criterion_id'],
                               'evidence_unit_ids': ['e1'], 'reason': '合成评价点对应。'}], 'gaps': []}


class BaselineTests(unittest.TestCase):
    def run_baseline(self, text):
        return run_text_baseline([answer(text)], QUESTION)

    def test_source_phrases_are_exact_original_prompts(self):
        config = json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        bank = json.loads(CONFIG_PATH.with_name('question-bank.v1.json').read_text(encoding='utf-8'))
        originals = {f['factor_id']: f for f in bank['factors']}
        self.assertEqual(len(config['factors']), 8)
        self.assertEqual(config['source']['workbook_sha256'], bank['sources'][0]['sha256'])
        for factor in config['factors']:
            f = originals[factor['factor_id']]
            for term in factor['terms']:
                field = 'positive_prompts' if term['source_label'] == 'positive_prompt' else 'risk_prompts'
                self.assertIn(term['phrase'], f[field].splitlines())
                self.assertEqual(term['source_cell'], f[field + '_source_cell'])

    def test_question_type_and_factor_context_use_source_without_new_scores(self):
        context = question_context({**QUESTION, 'source_cell': 'F3'})
        self.assertEqual(context['question_type'], 'behavioral')
        self.assertEqual(context['factor_context']['definition_source_cell'], 'C3')
        context = question_context({'question_id': 'results_K6', 'factor_id': 'results', 'source_cell': 'K6'})
        self.assertEqual(context['question_type'], 'situational')

    def test_keyword_stuffing_retains_raw_but_separates_presence(self):
        value = self.run_baseline('分工分工分工分工')
        term = next(t for t in value['terms'] if t['phrase'] == '分工')
        self.assertEqual(term['raw_count'], 4)
        self.assertEqual(term['answer_presence_count'], 1)
        self.assertEqual(term['per_100_alphanumeric_codepoints'], 50)
        self.assertNotIn('score', value)
        self.assertNotIn('followup', value)
        self.assertEqual(value['usage'], 'research_comparator_only')

    def test_negation_does_not_erase_raw_counts(self):
        value = self.run_baseline('我没有分工。')
        self.assertEqual(value['raw_counts_by_source_label']['positive_prompt'], 1)
        self.assertEqual(value['context_retained_total'], 0)
        self.assertIn('negation_cue', value['matches'][0]['flags'])

    def test_reported_speech_is_not_treated_as_unqualified_self_action(self):
        value = self.run_baseline('队友说他会分工。')
        self.assertEqual(len(value['matches']), 1)
        self.assertIn('reported_speech_cue', value['matches'][0]['flags'])
        self.assertFalse(value['matches'][0]['retained_by_context_rule'])
        quoted = self.run_baseline('“分工”是老师的原话。')
        self.assertIn('quoted_text', quoted['matches'][0]['flags'])

    def test_past_and_later_are_separate_contexts(self):
        value = self.run_baseline('以前我没有分工，后来我开始分工。')
        matches = value['matches']
        self.assertEqual([m['temporal_context'] for m in matches], ['past', 'later'])
        self.assertEqual([m['retained_by_context_rule'] for m in matches], [False, True])
        self.assertEqual(value['raw_counts_by_source_label']['positive_prompt'], 2)

    def test_synonymous_behavior_without_dictionary_phrase_has_no_invented_hit(self):
        value = self.run_baseline('我让小王查数据，让小李核算费用，每天下午一起检查完成情况。')
        self.assertEqual(value['matches'], [])
        self.assertTrue(any('同义表达' in limitation for limitation in value['limitations']))

    def test_offsets_emoji_version_denominator_and_no_input_mutation(self):
        answers = [answer('😀我分工。')]
        before = copy.deepcopy(answers)
        value = run_text_baseline(answers, QUESTION)
        match = value['matches'][0]
        self.assertEqual((match['char_start'], match['char_end']), (2, 4))
        self.assertEqual(value['denominator']['value'], 3)
        self.assertEqual(value['input_versions'], {'a1': 2})
        self.assertEqual(answers, before)
        self.assertEqual(value, run_text_baseline(answers, QUESTION))

    def test_empty_denominator_is_null_rate(self):
        value = self.run_baseline('😀。')
        self.assertEqual(value['denominator']['value'], 0)
        self.assertTrue(all(term['per_100_alphanumeric_codepoints'] is None for term in value['terms']))

    def test_invalid_factor_or_duplicate_answer_is_rejected(self):
        with self.assertRaises(ValueError):
            run_text_baseline([answer('分工')], {**QUESTION, 'factor_id': 'unknown'})
        with self.assertRaises(ValueError):
            run_text_baseline([answer('分工'), answer('分工')], QUESTION)


class V2ContractTests(unittest.TestCase):
    def setUp(self):
        self.answers = [answer('😀我分工。团队按计划完成。')]
        self.value = fixture_value(self.answers)

    def validate(self, value=None, answers=None):
        return validate_evaluation(value or self.value, QUESTION, answers or self.answers, [], require_v2=True)

    def test_unique_quote_offsets_are_computed_and_input_unchanged(self):
        before = copy.deepcopy(self.value)
        value = self.validate()
        ref = value['rating']['evidence'][0]
        self.assertEqual((ref['char_start'], ref['char_end']), (0, 5))
        self.assertEqual(self.value, before)

    def test_repeated_quote_needs_offsets_then_retains_selected_occurrence(self):
        answers = [answer('分工分工。')]
        value = fixture_value(answers)
        ref = value['rating']['evidence'][0]
        ref['quote'] = '分工'
        with self.assertRaises(ProviderError) as error:
            self.validate(value, answers)
        self.assertEqual(error.exception.code, 'ambiguous_quote')
        ref.update(char_start=2, char_end=4)
        result = self.validate(value, answers)
        self.assertEqual(result['rating']['evidence'][0]['char_start'], 2)

    def test_provider_cannot_invent_segment_time_or_offsets(self):
        for fields in [{'segment_id': 'invented'}, {'char_start': 1, 'char_end': 6},
                       {'start_ms': 1000}, {'char_start': True, 'char_end': 6}]:
            bad = copy.deepcopy(self.value)
            bad['rating']['evidence'][0].update(fields)
            with self.assertRaises(ProviderError):
                self.validate(bad)

    def test_quote_cannot_cross_single_segment(self):
        self.value['rating']['evidence'][0]['quote'] = '分工。团队'
        with self.assertRaises(ProviderError):
            self.validate()

    def test_current_versions_and_exact_source_segments_required(self):
        for field, bad_value in [('transcript_revision', 1), ('answer_id', 'other')]:
            bad = copy.deepcopy(self.value)
            bad['evidence_units'][0]['evidence'][0][field] = bad_value
            with self.assertRaises(ProviderError):
                self.validate(bad)
        prepared = prepare_text_answers(self.answers)
        prepared[0]['segments'][0]['text'] = '替换原文'
        with self.assertRaises(ValueError):
            prepare_text_answers(prepared)

    def test_real_asr_segment_ids_are_preserved_and_time_not_sent(self):
        a = answer('我分工和跟进。')
        a['segments'] = [_segment(a['answer_id'], 2, a['transcript'], 0, 3, (0, 600)),
                         _segment(a['answer_id'], 2, a['transcript'], 3, len(a['transcript']), (600, 1200))]
        prepared = prepare_text_answers([a])[0]
        self.assertEqual([s['segment_id'] for s in prepared['segments']], [s['segment_id'] for s in a['segments']])
        self.assertNotIn('start_ms', prepared['segments'][0])

    def test_unconfigured_criterion_unknown_unit_or_fabricated_category_rejected(self):
        variants = [copy.deepcopy(self.value) for _ in range(4)]
        variants[0]['criterion_matches'][0]['criterion_id'] = 'new-criterion'
        variants[1]['criterion_matches'][0]['evidence_unit_ids'] = ['missing']
        variants[2]['evidence_units'][0]['category'] = 'honesty'
        variants[3]['gaps'] = [{'criterion_id': 'new', 'description': '缺口', 'followup_relevance': '关联'}]
        for value in variants:
            with self.assertRaises(ProviderError):
                self.validate(value)

    def test_scored_result_needs_criterion_mapping_and_eligible_needs_gap(self):
        self.value['criterion_matches'] = []
        with self.assertRaises(ProviderError):
            self.validate()
        value = fixture_value(self.answers)
        value['followup'].update(status='eligible', branch_id='b')
        with self.assertRaises(ProviderError) as error:
            validate_evaluation(value, QUESTION, self.answers,
                                [{'branch_id': 'b', 'parent_question_id': QUESTION['question_id']}])
        self.assertEqual(error.exception.code, 'invalid_gap')

    def test_legacy_is_accepted_for_history_but_not_new_live_output(self):
        value = copy.deepcopy(self.value)
        for name in ['schema_version', 'evidence_units', 'criterion_matches', 'gaps']:
            del value[name]
        for part in ['rating', 'followup']:
            value[part]['evidence'] = [{k: v for k, v in e.items() if k in ['answer_id', 'transcript_revision', 'quote']}
                                       for e in value[part]['evidence']]
        validate_evaluation(value, QUESTION, self.answers, [])
        with self.assertRaises(ProviderError):
            self.validate(value)

    def test_schema_lists_fixed_criteria_and_required_offset_fields(self):
        ids = [c['criterion_id'] for c in criteria_for_question(QUESTION)]
        schema = evaluation_schema(QUESTION['question_id'], [], ids)
        self.assertEqual(schema['properties']['criterion_matches']['items']['properties']['criterion_id']['enum'], ids)
        self.assertIn('segment_id', schema['properties']['rating']['properties']['evidence']['items']['required'])

    def test_environment_key_cannot_be_sent_to_overridden_endpoint(self):
        with patch('interview_llm.provider_status', return_value={'status': 'configured_not_verified'}), \
                patch('interview_llm.public_settings', return_value={'base_url': 'https://bound.invalid/v1'}), \
                patch('interview_llm.get_api_key') as key, patch('requests.post') as request:
            with self.assertRaises(ProviderError) as error:
                evaluate_answer(QUESTION, self.answers, [], {'base_url': 'https://other.invalid/v1'})
            self.assertEqual(error.exception.code, 'endpoint_mismatch')
            key.assert_not_called()
            request.assert_not_called()


if __name__ == '__main__':
    unittest.main()
