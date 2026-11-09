"""Frozen evidence projections: bounded, scoped and read-only; no live services."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_mcp import (MCPServer, PROJECTION_PAGE_BYTES, TOOLS, ToolError, WorkbenchClient,
                           question_evidence, report_index, safe_output)


def fixture():
    text = '【工程合成】我帮助同学检查数据，再请他独立验证。'
    ref = {'answer_id': 'a1', 'transcript_revision': 1, 'quote': text[6:],
           'char_start': 6, 'char_end': len(text), 'segment_id': 'segment:fixture'}
    answer = {'answer_id': 'a1', 'round_id': 'round1', 'question_id': 'q1', 'parent_question_id': None,
              'transcript_revision': 1, 'transcript': text, 'current': True,
              'transcript_source': 'candidate', 'has_media': False}
    q = {'question_id': 'q1', 'factor_id': 'help', 'dimension_id': 'leadership',
         'question_type': 'behavioral', 'stem': '讲一次帮助他人的经历。', 'score': 4, 'ai_score': 4,
         'score_source': 'model_draft', 'reason': 'MODEL-REASON', 'review_status': 'not_reviewed',
         'review_stale': False, 'rubric_status': 'proposed', 'rubric_version': 'draft-v1',
         'anchors': {'1': '低锚点', '3': '中锚点', '5': '高锚点'},
         'criteria': [{'criterion_id': 'q1:c1', 'name': '帮助方法', 'description': '核对帮助行为',
                       'requirement_ids': ['f:r1']}],
         'factor_requirements': [{'requirement_id': 'f:r1', 'text': '是否帮助他人提升', 'source_cell': 'E4'}],
         'answers': [answer], 'evidence': [ref],
         'model_evaluation': {'question_id': 'q1', 'evaluation_id': 'eval1', 'input_versions': {'a1': 1},
             'provider': {'provider': 'fixture', 'model': 'synthetic', 'api_key': 'must-not-escape',
                          'criteria_snapshot': 'DO-NOT-DUPLICATE-SNAPSHOT'},
             'evidence_units': [{'unit_id': 'eu1', 'interpretation': 'MODEL-OBSERVATION', 'evidence': [ref]}],
             'criterion_judgments': [{'criterion_id': 'q1:c1', 'status': 'partial', 'evidence_unit_ids': ['eu1']}],
             'requirement_judgments': [{'requirement_id': 'f:r1', 'status': 'partial', 'evidence_unit_ids': ['eu1']}],
             'gaps': [{'criterion_id': 'q1:c1', 'description': '仍需独立结果'}]}}
    qs = [q]
    for i in range(2, 9):
        qs.append({'question_id': 'q' + str(i), 'factor_id': 'f' + str(i), 'score': 3 if i < 8 else None,
                   'score_source': 'model_draft' if i < 8 else 'unavailable', 'answers': [],
                   'reason': '尚无有效评分' if i == 8 else '已有评分'})
    return {'session_id': 's1', 'version': 64, 'status': 'interview_finished', 'form_mode': 'full',
            'report_completeness': 'incomplete', 'report_lifecycle': 'draft', 'final_score': None,
            'score_status': 'incomplete', 'questions': qs, 'answers': [answer],
            'rounds': [{'round_id': 'round1', 'question_id': 'q1', 'parent_question_id': None,
                        'kind': 'main', 'text': '实际已发主问', 'issued_at': '2026-10-31', 'ordinal': 1}],
            'jobs': [{'job_id': 'old', 'job_type': 'content', 'question_id': 'q1', 'status': 'failed'},
                     {'job_id': 'new', 'job_type': 'content', 'question_id': 'q8', 'status': 'failed',
                      'error_code': 'invalid_branch', 'error': '未接受模型结果', 'created_at': '2026-10-31'}],
            'potential_conclusion': {'factors': [{'factor_id': 'help', 'name': '成就他人', 'dimension': '领导力',
                                                'definition': '帮助他人提升'}]},
            'standard_analysis': {'enabled': True, 'pending_count': 0, 'failed_count': 0,
                                  'answers': [{'answer_id': 'a1', 'status': 'not_applicable'}]},
            'decision_events': [{'decision_id': 'd1', 'question_id': 'q1', 'parent_question_id': 'q1',
                'input_versions': {'a1': 1}, 'decision_source': 'dictionary-bank.v2-fallback',
                'selected_branch_id': 'q1_f1', 'target_evidence': '帮助后的变化', 'used_evidence': [ref],
                'model_comparison': ['DO-NOT-IMPLY-THIS-WAS-DECISION-INPUT'],
                'decision_summary': {'brief_reason': '词典没有定位结果', 'fallback_reason': 'timeout',
                                     'model_involved_in_selection': False,
                                     'coverage': [{'branch_id': 'q1_f1', 'status': 'needs_clarification', 'hits': []}],
                                     'dictionary_coverage': ['DO-NOT-DUPLICATE-COVERAGE']}},
                {'decision_id': 'foreign', 'question_id': 'q2', 'parent_question_id': 'q2', 'used_evidence': []}]}


def records_by_kind(response, kind):
    return [r['data'] for r in response['records'] if r['kind'] == kind]


class EvidenceProjectionTests(unittest.TestCase):
    def setUp(self):
        self.report = fixture()

    def test_index_counts_effective_scores_and_explains_unscored_without_transcripts(self):
        result = report_index(self.report, 's1', 64)
        self.assertEqual(result['scored_main_question_count'], 7)
        self.assertIsNone(result['final_score'])
        items = records_by_kind(result, 'question_index')
        self.assertNotIn('latest_content_job', items[0])
        self.assertEqual(items[-1]['latest_content_job']['error_code'], 'invalid_branch')
        self.assertNotIn(self.report['questions'][0]['answers'][0]['transcript'], json.dumps(result, ensure_ascii=False))
        self.assertEqual(records_by_kind(result, 'modality_status')[0]['answer_states'][0]['status'], 'not_applicable')

    def test_source_includes_parent_and_followup_but_no_model_verdict(self):
        follow = {**self.report['questions'][0]['answers'][0], 'answer_id': 'a1f',
                  'round_id': 'round2', 'question_id': 'q1_f1', 'parent_question_id': 'q1', 'transcript': '合成追答'}
        self.report['questions'][0]['answers'].append(follow)
        self.report['rounds'].append({'round_id': 'round2', 'question_id': 'q1_f1', 'parent_question_id': 'q1',
                                      'kind': 'followup', 'text': '具体后来怎么做？', 'issued_at': '2026-10-31', 'ordinal': 2})
        self.report['questions'][0]['followup_rules'] = {'unused': {'text': 'UNASKED-QUESTION-BANK-TEXT'}}
        before = copy.deepcopy(self.report)
        result = question_evidence(self.report, 's1', 64, 'q1')
        self.assertEqual(result, question_evidence(self.report, 's1', 64, 'q1', 'source', 0))
        self.assertEqual(result['input_versions'], {'a1': 1, 'a1f': 1})
        self.assertEqual([a['answer_id'] for a in records_by_kind(result, 'answer')], ['a1', 'a1f'])
        self.assertEqual([x['text'] for x in records_by_kind(result, 'issued_round')], ['实际已发主问', '具体后来怎么做？'])
        self.assertEqual(records_by_kind(result, 'answer')[1]['round_id'], 'round2')
        self.assertNotIn('UNASKED-QUESTION-BANK-TEXT', json.dumps(result))
        self.assertNotIn('MODEL-', json.dumps(result))
        self.assertNotIn('recorded_score', json.dumps(result))
        self.assertEqual(self.report, before)
        self.assertTrue(result['pagination']['all_data_in_this_response'])

    def test_quick_index_lists_unmeasured_factors_from_same_frozen_report(self):
        self.report['questions'] = self.report['questions'][:3]
        self.report['form_mode'] = 'quick'
        factors = self.report['potential_conclusion']['factors']
        factors.extend({'factor_id': 'f' + str(i), 'name': '冻结因子' + str(i)} for i in range(2, 9))
        result = report_index(self.report, 's1', 64)
        coverage = records_by_kind(result, 'factor_coverage')[0]
        self.assertTrue(coverage['factor_catalog_complete'])
        self.assertEqual(len(coverage['factors']), 8)
        self.assertEqual([f['factor_id'] for f in coverage['factors'] if f['coverage_state'] == 'not_measured'],
                         ['f4', 'f5', 'f6', 'f7', 'f8'])
        self.assertEqual(coverage['factors'][-1]['name'], '冻结因子8')
        self.assertEqual(result['version'], 64)

    def test_source_rejects_missing_wrong_parent_and_unissued_rounds(self):
        for mutation in ('missing', 'wrong_id', 'wrong_parent', 'wrong_session', 'not_issued', 'duplicate'):
            bad = copy.deepcopy(self.report)
            if mutation == 'missing':
                bad['rounds'] = []
            elif mutation == 'wrong_id':
                bad['questions'][0]['answers'][0]['round_id'] = 'other'
            elif mutation == 'wrong_parent':
                bad['rounds'][0]['parent_question_id'] = 'q2'
            elif mutation == 'wrong_session':
                bad['rounds'][0]['session_id'] = 'other'
            elif mutation == 'not_issued':
                del bad['rounds'][0]['issued_at']
            else:
                bad['rounds'].append(copy.deepcopy(bad['rounds'][0]))
            with self.subTest(mutation=mutation), self.assertRaises(ToolError):
                question_evidence(bad, 's1', 64, 'q1')
        self.report['questions'][-1]['stem'] = 'NOT-YET-ASKED'
        not_asked = question_evidence(self.report, 's1', 64, 'q8')
        self.assertFalse(records_by_kind(not_asked, 'question')[0]['issued'])
        self.assertEqual(records_by_kind(not_asked, 'issued_round'), [])
        self.assertNotIn('NOT-YET-ASKED', json.dumps(not_asked))

    def test_assessment_keeps_evidence_and_versions_without_provider_snapshots(self):
        result = question_evidence(self.report, 's1', 64, 'q1', 'assessment')
        self.assertEqual(records_by_kind(result, 'recorded_score')[0]['score'], 4)
        self.assertEqual(records_by_kind(result, 'model_metadata')[0]['input_versions'], {'a1': 1})
        self.assertEqual(records_by_kind(result, 'evidence_unit')[0]['evidence'][0]['char_start'], 6)
        serialized = json.dumps(result)
        for forbidden in ('must-not-escape', 'DO-NOT-DUPLICATE-SNAPSHOT', 'api_key'):
            self.assertNotIn(forbidden, serialized)
        unscored = question_evidence(self.report, 's1', 64, 'q8', 'assessment')
        self.assertIsNone(records_by_kind(unscored, 'recorded_score')[0]['score'])
        self.assertTrue(records_by_kind(unscored, 'model_assessment_unavailable'))

    def test_routing_uses_actual_parent_event_not_later_model_comparison(self):
        result = question_evidence(self.report, 's1', 64, 'q1', 'routing')
        events = records_by_kind(result, 'decision')
        self.assertEqual([e['decision_id'] for e in events], ['d1'])
        self.assertFalse(events[0]['summary']['model_involved_in_selection'])
        self.assertEqual(len(records_by_kind(result, 'routing_coverage')), 1)
        self.assertNotIn('DO-NOT-', json.dumps(result))

    def test_frozen_version_parent_and_quote_validation_reject_wrong_scope(self):
        for sid, version in [('other', 64), ('s1', 65)]:
            with self.subTest(sid=sid, version=version), self.assertRaises(ToolError):
                question_evidence(self.report, sid, version, 'q1')
        with self.assertRaises(ToolError):
            question_evidence(self.report, 's1', 64, 'q1_f1')
        for mutation in ('foreign_answer', 'foreign_ref', 'wrong_offset', 'wrong_eval_version', 'duplicate_qid'):
            bad = copy.deepcopy(self.report)
            q = bad['questions'][0]
            if mutation == 'foreign_answer':
                q['answers'][0]['question_id'] = 'q2'
            elif mutation == 'foreign_ref':
                q['evidence'][0]['answer_id'] = 'foreign-answer'
            elif mutation == 'wrong_offset':
                q['evidence'][0]['char_start'] = 5
            elif mutation == 'wrong_eval_version':
                q['model_evaluation']['input_versions']['a1'] = 2
            else:
                bad['questions'][1]['question_id'] = 'q1'
            with self.subTest(mutation=mutation), self.assertRaises(ToolError):
                question_evidence(bad, 's1', 64, 'q1', 'assessment')

    def test_long_records_are_bounded_lossless_and_redacted_before_fragmentation(self):
        q = self.report['questions'][0]
        q['answers'][0]['transcript'] = ('长原话😀\\\"\n' * 6000 + ' Bearer secret-across-fragment sk-secret-abcdefghijklmnop')
        original = copy.deepcopy(self.report)
        pages, n = [], 0
        while n is not None:
            result = question_evidence(self.report, 's1', 64, 'q1', page=n)
            raw = json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            self.assertLessEqual(len(raw), PROJECTION_PAGE_BYTES)
            self.assertNotIn(b'secret-across-fragment', raw)
            self.assertNotIn(b'sk-secret-', raw)
            self.assertTrue(result['redactions_applied'])
            self.assertFalse(result['pagination']['all_data_in_this_response'])
            pages.append(result)
            n = result['pagination']['next_page']
        fragments = [r for p in pages for r in p['records'] if r['kind'] == 'record_fragment']
        self.assertGreater(len(fragments), 1)
        self.assertEqual([r['fragment_index'] for r in fragments], list(range(len(fragments))))
        restored = json.loads(''.join(r['json_fragment'] for r in fragments))
        self.assertEqual(restored['data']['transcript'], safe_output(q['answers'][0]['transcript']))
        self.assertEqual(self.report, original)
        with self.assertRaises(ToolError):
            question_evidence(self.report, 's1', 64, 'q1', page=len(pages))

    def test_new_tools_are_strict_read_only_and_use_only_frozen_report_route(self):
        client = WorkbenchClient(token_loader=lambda: 'operator')
        calls = []
        def request(method, route, token=None, body=None):
            calls.append((method, route))
            if route.endswith('/open'):
                return {'hr_url': '/hr-review#session=s1&token=private-capability'}
            return copy.deepcopy(self.report)
        client._request = request
        server = MCPServer(client)
        initialized = server.dispatch({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {}})
        self.assertEqual(initialized['result']['serverInfo']['version'], '1.2.0')
        for name in ('get_report_index', 'get_question_evidence'):
            args = {'session_id': 's1', 'version': 64}
            if name == 'get_question_evidence':
                args['question_id'] = 'q1'
            result = server.dispatch({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                                      'params': {'name': name, 'arguments': args}})['result']
            self.assertFalse(result['isError'])
            self.assertEqual(json.loads(result['content'][0]['text']), result['structuredContent'])
            self.assertNotIn('private-capability', json.dumps(result))
            self.assertEqual(calls[-2:], [('POST', '/v1/workbench/sessions/s1/open'),
                                         ('GET', '/v1/interviews/s1/report?compact=true&version=64')])
            tool = next(t for t in TOOLS if t['name'] == name)
            self.assertTrue(tool['annotations']['readOnlyHint'])
            for bad in ({k: v for k, v in args.items() if k != 'version'}, {**args, 'version': True},
                        {**args, 'page': -1}, {**args, 'page': True}, {**args, 'path': 'C:/private'}):
                before = list(calls)
                with self.assertRaises(ToolError):
                    client.call(name, bad)
                self.assertEqual(calls, before)
        with self.assertRaises(ToolError):
            client.call('get_question_evidence', {'session_id': 's1', 'version': 64,
                                                'question_id': 'q1', 'section': 'recompute'})


if __name__ == '__main__':
    unittest.main()
