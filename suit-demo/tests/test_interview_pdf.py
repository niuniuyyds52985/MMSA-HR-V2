"""Frozen-version PDF content and authenticated download; synthetic data only."""
import copy
from io import BytesIO
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
from pypdf import PdfReader
from interview_pdf import build_report_pdf, PdfExportError
from interview_core import BASE, InterviewCore
from test_interview_workbench import ASGIClient


def fixture():
    answer = {'answer_id': 'synthetic-answer', 'current': True, 'transcript_revision': 2,
              'transcript': '我帶領各組核對進度。我把任務分成三部分，每天核對清單。', 'has_media': True}
    ref = {'answer_id': answer['answer_id'], 'transcript_revision': 2, 'quote': '我帶領各組核對進度。'}
    question = {'question_id': 'leadership_F3_Q1', 'factor_id': 'leadership', 'text': '请描述一次团队协作经历。',
        'score': 3, 'score_source': 'model_draft', 'reason': '已说明本人组织行动；实际结果仍待核实。', 'evidence': [ref], 'answers': [answer],
        'model_evaluation': {'gaps': [{'description': '还需补充具体结果。'}]}}
    item = {'answer_id': answer['answer_id'], 'transcript_revision': 2,
        'modules': {'audio': {'status': 'complete'}, 'expression': {'status': 'complete'}},
        'reader_summary': {'source_run_id': 'synthetic-run', 'coverage': {'audio': {'valid_observations': 8, 'sampled_observations': 10}},
            'events': [{'kind': 'audio_pitch', 'title': '音调较低的片段', 'description': '相对本题其他有效片段低 2 个半音。',
                'start_ms': 1000, 'end_ms': 2500, 'anchor_ms': 1000, 'measurements': {'median': 120, 'unit': 'Hz', 'difference': -2, 'difference_unit': 'semitones'},
                'evidence_refs': [{'module': 'audio'}]},
                {'kind': 'apparent_expression', 'title': '微笑外观的片段', 'description': '模型将此采样归为微笑外观类别，可核对同期嘴角位置。', 'start_ms': 3000, 'end_ms': 4000, 'anchor_ms': 3000, 'evidence_refs': [{'module': 'expression'}]}]}}
    return {'version': 7, 'candidate_ref': '合成 PDF 验收场次', 'created_at': '2026-10-27T03:00:00Z', 'generated_at': '2026-10-27T03:10:00Z',
        'total_score': None, 'questions': [question], 'coverage': {'planned_factor_ids': ['leadership']},
        'decision_events': [{'parent_question_id': question['question_id'], 'action': 'ask_followup',
            'decision_summary': {'brief_reason': '主问缺少具体结果，因此选择结果追问。', 'selected_branch': {'text': '结果怎样核实？'}, 'used_evidence': [ref],
                                 'dictionary_snapshot': {'matches': [{'term': '核对', 'retained': True}]}}}],
        'standard_analysis': {'runs': [{'run_id': 'synthetic-run', 'status': 'succeeded', 'completed_at': '2026-10-27T03:05:00Z', 'result': {'multimodal': [item]}}]},
        'model_config': {'api_key': 'NEVER-EXPORT-CONFIG-KEY'}, 'token': 'NEVER-EXPORT-CAPABILITY'}


def extracted(content):
    return '\n'.join(page.extract_text() or '' for page in PdfReader(BytesIO(content)).pages)


def full_fixture():
    """Eight source questions with unmistakably synthetic content and judgments."""
    from interview_forms import build_form
    form = build_form({'version': 'interview.v4', 'policy_version': 'semantic-bank.v1'}, mode='full', seed='pdf-synthetic')
    report = {'version': 91, 'candidate_ref': '合成完整八题 PDF 验收',
              'created_at': '2026-10-27T06:00:00Z', 'generated_at': '2026-10-27T06:10:00Z',
              'form': form['form'], 'questions': [], 'total_score': None, 'decision_events': [],
              'potential_conclusion': {'schema_version': 'potential-conclusion.v1', 'source_version': 91,
                  'summary': '合成报告：分别核对每条潜力模型要求，保留已有证据、待补证据与本轮未测项。',
                  'teacher_review_note': '合成测试数据。模型结论待指导老师核对；无岗位匹配率。', 'factors': []}}
    states = ['demonstrated', 'partial', 'insufficient_evidence', 'not_met', 'not_measured']
    for index, source_question in enumerate(form['questions']):
        question = copy.deepcopy(source_question)
        text = f'这是第{index+1}题的合成回答。我先核对目标，整理清单，安排检查点，再根据反馈调整。'
        answer = {'answer_id': 'synthetic-full-answer-' + str(index), 'current': True,
                  'transcript_revision': 1, 'transcript': text, 'has_media': False}
        ref = {'answer_id': answer['answer_id'], 'transcript_revision': 1, 'quote': '我先核对目标，整理清单，安排检查点，再根据反馈调整。'}
        question.update({'answers': [answer], 'score': 3, 'score_source': 'model_draft',
                         'reason': '合成材料仅用于检验排版，不构成参与者评价。', 'evidence': [ref]})
        report['questions'].append(question)
        requirements = []
        for ordinal, requirement in enumerate(question['factor_requirements']):
            state = states[(index + ordinal) % len(states)]
            requirements.append({'id': requirement['requirement_id'], 'text': requirement['text'], 'status': state,
                'reason': '合成逐要求理由：该回答只说明部分行动，真实应用须核对原话是否充分支持整条要求。',
                'evidence': [ref] if state in ('demonstrated', 'partial', 'not_met') else [],
                'gaps': ['补充具体情境和结果依据。'] if state in ('partial', 'insufficient_evidence') else [],
                'source_refs': [{'source_cell': requirement['source_cell']}]})
        report['potential_conclusion']['factors'].append({'factor_id': question['factor_id'], 'status': 'partial',
            'requirements': requirements, 'question_ids': [question['question_id']],
            'notice': '逐要求内容判断为草稿；题目总分复核不等于逐条确认。'})
    return report


class PdfContentTests(unittest.TestCase):
    def test_chinese_content_frozen_inputs_and_no_external_actions(self):
        data = fixture()
        original = copy.deepcopy(data)
        with patch('urllib.request.urlopen', side_effect=AssertionError('No network in PDF renderer')), \
             patch('requests.get', side_effect=AssertionError('No network in PDF renderer')):
            content = build_report_pdf(data)
        text = extracted(content)
        self.assertTrue(content.startswith(b'%PDF-'))
        for phrase in ('管培生面试报告', '冻结报告 v7', '我带领各组核对进度', '结果怎样核实', '音调较低的片段', '微笑外观', '核对采集点 00:01.0', '八因子覆盖', '本轮未测'):
            self.assertIn(phrase, text)
        self.assertNotIn('NEVER-EXPORT', text)
        self.assertEqual(data, original)
        reader = PdfReader(BytesIO(content))
        self.assertNotIn('/OpenAction', reader.trailer['/Root'])
        self.assertTrue(all(not page.get('/Annots') for page in reader.pages))
        fonts = [font.get_object() for page in reader.pages for font in page['/Resources']['/Font'].values()]
        self.assertTrue(any(font.get('/FontDescriptor') and font['/FontDescriptor'].get('/FontFile2') for font in fonts))
        self.assertEqual(content, build_report_pdf(data))

    def test_stale_observation_and_old_rating_do_not_become_current(self):
        data = fixture()
        q = data['questions'][0]
        q.update(score=None, review_stale=True, score_source='hr', reason='复核依据已更新')
        data['model_evaluations'] = [{'rating': {'score': 5, 'reason': 'OLD-RATING-NOT-CURRENT'}}]
        data['standard_analysis']['runs'][0]['result']['multimodal'][0]['transcript_revision'] = 1
        text = extracted(build_report_pdf(data))
        self.assertIn('依据已更新', text)
        self.assertIn('没有与此回答版本相符', text)
        self.assertNotIn('OLD-RATING-NOT-CURRENT', text)
        self.assertNotIn('音调较低的片段', text)

    def test_long_and_hostile_free_text_is_plain_redacted_content(self):
        data = fixture()
        q = data['questions'][0]
        q['reason'] = '安全引用 & <img src="https://example.invalid/a"> ' + ('待核实具体行动和结果。' * 500)
        q['answers'][0]['transcript'] += ' sk-' + 'A' * 40 + ' token=' + 'f' * 64
        content = build_report_pdf(data)
        text = extracted(content)
        self.assertIn('安全引用 & <img', text)
        self.assertNotIn('A' * 40, text)
        self.assertNotIn('f' * 64, text)
        self.assertIn('已隐藏', text)
        self.assertGreater(len(PdfReader(BytesIO(content)).pages), 3)

    def test_extended_observations_do_not_hide_voice_and_face(self):
        data = fixture()
        item = copy.deepcopy(data['standard_analysis']['runs'][0]['result']['multimodal'][0])
        item['modules'] = {'emotic': {'status': 'complete'}}
        item['reader_summary']['events'] = [{'kind': 'context_expression', 'title': '场景标签'+str(i),
            'description': '合成场景类别', 'start_ms': i*1000, 'end_ms': (i+1)*1000, 'evidence_refs': [{'module': 'emotic'}]} for i in range(12)]
        data['standard_analysis']['runs'].append({'status': 'complete', 'created_at': '2026-10-28', 'result': {'multimodal': [item]}})
        text = extracted(build_report_pdf(data))
        self.assertIn('音调较低的片段', text)
        self.assertIn('微笑外观的片段', text)
        self.assertIn('场景标签0', text)
        self.assertNotIn('场景标签11', text)

    def test_missing_and_text_only_inputs_are_explicit(self):
        data = fixture()
        data['questions'][0]['answers'][0].update(has_media=False, transcript='')
        text = extracted(build_report_pdf(data))
        self.assertIn('尚无可读转写', text)
        self.assertIn('未采集音视频', text)
        self.assertNotIn('音调较低的片段', text)
        for bad in (None, {}, {'version': 0}, {'version': True}):
            with self.assertRaises(PdfExportError):
                build_report_pdf(bad)

    def test_full_eight_question_report_has_requirement_evidence_and_dynamic_coverage(self):
        data = full_fixture()
        frozen = copy.deepcopy(data)
        content = build_report_pdf(data)
        text = extracted(content)
        self.assertIn('本场安排 8 道主问，覆盖 8 / 8 个因子', text)
        self.assertIn('当前有效评价 8 / 8 题', text)
        self.assertIn('第 8 题', text)
        self.assertNotIn('短面试只覆盖', text)
        for phrase in ('潜力模型要求与本场证据', '已有证据支持', '部分体现', '证据不足',
                       '有相反证据，待复核', '本轮未测', '原表考察内容：E10', '待补材料',
                       '题目总分复核不等于逐条确认', '不同题卷尚未完成等值验证'):
            self.assertIn(phrase, text)
        self.assertEqual(data, frozen)
        self.assertGreaterEqual(len(PdfReader(BytesIO(content)).pages), 9)

    def test_requirement_conclusion_must_match_frozen_report_version(self):
        data = full_fixture()
        data['potential_conclusion']['source_version'] = 90
        data['potential_conclusion']['summary'] = 'STALE-CONCLUSION-MUST-NOT-APPEAR'
        text = extracted(build_report_pdf(data))
        self.assertIn('要求结论版本与本报告不一致', text)
        self.assertNotIn('STALE-CONCLUSION-MUST-NOT-APPEAR', text)
        self.assertNotIn('合成逐要求理由', text)
        self.assertIn('第 8 题', text)


class PdfApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'INTERVIEW_DATA_DIR': self.tmp.name,
            'INTERVIEW_CREDENTIALS_FILE': str(Path(self.tmp.name) / 'absent'), 'DASHSCOPE_API_KEY': ''})
        env.start(); self.addCleanup(env.stop)
        self.core = InterviewCore(self.tmp.name, BASE / 'config/interview.v3.json')
        created = self.core.create_interview(str(uuid.uuid4()), '合成 PDF 接口测试')
        self.sid = created['session_id']
        self.frozen = self.core.get_report(self.sid)
        self.entry = importlib.import_module('interview_entry')
        active = patch.object(self.entry, 'core', self.core)
        active.start(); self.addCleanup(active.stop)
        self.client = ASGIClient(self.entry.app)
        self.url = '/v1/interviews/' + self.sid + '/report.pdf?version=' + str(self.frozen['version'])
        self.hr = {'Authorization': 'Bearer ' + self.core._token(self.sid, 'hr')}

    def test_hr_download_requires_existing_explicit_version(self):
        response = self.client.request('GET', self.url, headers=self.hr)
        self.assertEqual(response.status_code, 200, response.text[:100])
        self.assertTrue(response.content.startswith(b'%PDF-'))
        self.assertEqual(response.headers['content-type'], 'application/pdf')
        self.assertIn('attachment', response.headers['content-disposition'])
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(self.client.request('GET', self.url.split('?')[0], headers=self.hr).status_code, 422)
        self.assertEqual(self.client.request('GET', self.url.split('?')[0]+'?version=9999', headers=self.hr).status_code, 404)
        self.assertEqual(self.frozen, self.core.get_report(self.sid, self.frozen['version']))

    def test_candidate_other_session_and_anonymous_cannot_download(self):
        other = self.core.create_interview(str(uuid.uuid4()), '另一个合成场次')
        tokens = [self.core._token(self.sid, 'candidate'), self.core._token(other['session_id'], 'hr')]
        for token in tokens:
            response = self.client.request('GET', self.url, headers={'Authorization': 'Bearer ' + token})
            self.assertEqual(response.status_code, 403)
        self.assertEqual(self.client.request('GET', self.url).status_code, 401)

    def test_frozen_version_is_used_after_session_changes_and_dependency_error_is_safe(self):
        current = self.core.get_state(self.sid)['current_round']
        self.core.submit_answer(self.sid, current['round_id'], str(uuid.uuid4()), str(uuid.uuid4()), '此文字只属于新版本。')
        response = self.client.request('GET', self.url, headers=self.hr)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('此文字只属于新版本', extracted(response.content))
        with patch('interview_pdf.build_report_pdf', side_effect=PdfExportError('PDF 导出依赖未安装。')):
            response = self.client.request('GET', self.url, headers=self.hr)
        self.assertEqual(response.status_code, 503)
        self.assertIn('pdf_unavailable', response.text)
        self.assertNotIn(self.hr['Authorization'], response.text)


if __name__ == '__main__':
    unittest.main()
