"""Synthetic script-normalization contracts; no ASR/model/participant execution."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_text_normalization import DATA_PATH, DATA_SHA256, VERSION, normalize_for_match, normalization_metadata
from interview_text_research import prepare_text_answers, run_text_baseline, CONFIG_PATH
from interview_followup import select_followup

ROOT = Path(__file__).resolve().parents[1]
FLOW = json.loads((ROOT/'config/interview.v3.json').read_text(encoding='utf-8'))
QUESTION = FLOW['questions'][0]
BRANCHES = [b for b in FLOW['branches'] if b['parent_question_id']==QUESTION['question_id']]


def answer(text):
    return {'answer_id':'synthetic-script', 'transcript_revision':3, 'transcript':text}


def route(text, question=QUESTION):
    branches=[b for b in FLOW['branches'] if b['parent_question_id']==question['question_id']]
    return select_followup([answer(text)],question,branches)


class ScriptNormalizationTests(unittest.TestCase):
    def test_pinned_complete_dictionary_and_identity_offsets(self):
        data=json.loads(DATA_PATH.read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256(DATA_PATH.read_bytes()).hexdigest(),DATA_SHA256)
        self.assertEqual(len(data['characters']),4113)
        self.assertEqual(len(data['phrases']),735)
        for key,value in [*data['characters'].items(),*data['phrases'].items()]:
            self.assertEqual(len(key),len(value))
        self.assertEqual(normalization_metadata()['offset_mapping'],'identity_unicode_codepoints')

    def test_mixed_script_and_phrase_exceptions(self):
        raw='我協調了三位同學，完成乾清宮附近的任務。乾燥的天氣不影響進度。'
        expected='我协调了三位同学，完成乾清宫附近的任务。干燥的天气不影响进度。'
        self.assertEqual(normalize_for_match(raw),expected)
        self.assertEqual(normalize_for_match(expected),expected)
        self.assertEqual(normalize_for_match('乾清宫、乾清宮、乾坤一掷、乾坤一擲'),'乾清宫、乾清宫、乾坤一掷、乾坤一掷')

    def test_every_derived_phrase_is_stable(self):
        data=json.loads(DATA_PATH.read_text(encoding='utf-8'))
        for key,value in data['phrases'].items():
            with self.subTest(phrase=key):
                self.assertEqual(normalize_for_match(key),value)
                self.assertEqual(normalize_for_match(value),value)

    def test_no_regional_rewording_or_case_number_change(self):
        raw='😀API v2：資訊、軟體，25.5%！我沒有協調。'
        self.assertEqual(normalize_for_match(raw),'😀API v2：资讯、软体，25.5%！我没有协调。')
        self.assertEqual(len(normalize_for_match(raw)),len(raw))

    def test_canonical_preparation_and_segment_ids_are_unchanged(self):
        inputs=[answer('😀我協調了場地。後來隊友跟進。')]
        before=copy.deepcopy(inputs)
        prepared=prepare_text_answers(inputs)
        baseline=run_text_baseline(inputs,QUESTION)
        self.assertEqual(inputs,before)
        self.assertEqual(prepared[0]['transcript'],before[0]['transcript'])
        self.assertEqual(prepare_text_answers(inputs),prepared)
        self.assertEqual(baseline['text_normalization']['version'],VERSION)
        self.assertEqual(baseline['matching']['normalization'],VERSION)

    def test_baseline_simplified_and_traditional_match_counts_and_guards(self):
        raw='😀我沒有協調。後來我協調場地。隊友說他會組織活動。'
        config=json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
        factor=next(f for f in config['factors'] if f['factor_id']=='leadership')
        factor['terms']=[{'term_id':'test-coordination','phrase':'协调','source_label':'positive_prompt','source_cell':'synthetic'},
                         {'term_id':'test-organize','phrase':'组织','source_label':'positive_prompt','source_cell':'synthetic'}]
        original=run_text_baseline([answer(raw)],QUESTION,config)
        simple=run_text_baseline([answer(normalize_for_match(raw))],QUESTION,config)
        self.assertEqual(original['raw_counts_by_source_label'],simple['raw_counts_by_source_label'])
        self.assertEqual(original['context_retained_total'],simple['context_retained_total'])
        self.assertEqual([m['flags'] for m in original['matches']],[m['flags'] for m in simple['matches']])
        for match in original['matches']:
            self.assertEqual(normalize_for_match(raw[match['char_start']:match['char_end']]),match['phrase'])
            self.assertEqual(raw[match['context_char_start']:match['context_char_end']],match['context'])
        self.assertIn('negation_cue',original['matches'][0]['flags'])
        self.assertIn('reported_speech_cue',original['matches'][-1]['flags'])

    def test_non_bmp_offsets_use_original_unicode_points(self):
        raw='😀我協調了場地與物料。'
        result=route(raw)
        hit=result['summary']['coverage_rules'][0]['hits'][0]
        self.assertEqual(raw[hit['char_start']:hit['char_end']],hit['quote'])
        self.assertEqual(hit['char_start'],1)
        self.assertIn('協調',hit['quote'])
        self.assertEqual(result['summary']['text_normalization']['offset_mapping'],'identity_unicode_codepoints')

    def test_own_action_cues_choose_same_branch_and_cite_original(self):
        raw='我協調三名同學分別聯繫客戶，並每天核對進度。'
        original=route(raw);simple=route(normalize_for_match(raw))
        self.assertEqual(original['branch_id'],simple['branch_id'])
        self.assertNotEqual(original['branch_id'],'leadership_F3_Q1_action')
        self.assertEqual(original['summary']['coverage_rules'][0]['status'],'lexical_cue_present')
        for rule in original['summary']['coverage_rules']:
            for hit in rule['hits']:
                self.assertEqual(raw[hit['char_start']:hit['char_end']],hit['quote'])
        for ref in original['evidence']:
            self.assertEqual(raw[ref['char_start']:ref['char_end']],ref['quote'])

    def test_negation_reported_speech_and_group_subject_still_block_own_action(self):
        for text in ['我沒有協調三位同學的具體工作','我看到組長協調三位同學的具體工作',
                     '我們協調三位同學的具體工作','我希望協調三位同學的具體工作',
                     '我負責分工溝通協調組織安排']:
            with self.subTest(text=text):
                a=route(text+'。最終按時完成了項目。難點是兩個人意見不同。')
                b=route(normalize_for_match(text)+'。最终按时完成了项目。难点是两个人意见不同。')
                self.assertEqual(a['branch_id'],'leadership_F3_Q1_action')
                self.assertEqual(a['branch_id'],b['branch_id'])
                self.assertEqual(a['summary']['coverage_rules'][0]['status'],'needs_clarification')

    def test_learning_prerequisite_guards_survive_traditional_input(self):
        q=FLOW['questions'][2]
        for raw in ['我沒有總結出先驗證再投入的方法。','我看到同學總結了先試驗再投入的方法。']:
            with self.subTest(raw=raw):
                result=route(raw,q)
                self.assertIsNone(result['branch_id'])
                self.assertEqual(result['stop_reason'],'prerequisite_not_met')
        self.assertEqual(route('我總結了先試驗再投入的方法，在另一個項目使用了。',q)['branch_id'],'learning_F10_Q2_boundary')

    def test_empty_is_empty_and_non_strings_are_rejected(self):
        self.assertEqual(normalize_for_match(''),'')
        with self.assertRaises(TypeError):normalize_for_match(None)


if __name__=='__main__':unittest.main()
