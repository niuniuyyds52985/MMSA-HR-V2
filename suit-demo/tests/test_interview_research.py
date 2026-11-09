"""Synthetic research isolation, immutable references and report publication."""
import copy
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
from interview_core import InterviewCore, DomainError
from interview_research import ResearchStore, execute_run


def key():
    return str(uuid.uuid4())


class ResearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'DASHSCOPE_API_KEY': '', 'INTERVIEW_CREDENTIALS_FILE': str(Path(self.tmp.name) / 'missing'), 'INTERVIEW_DATA_DIR':self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        self.core = InterviewCore(self.tmp.name)
        self.store = ResearchStore(self.core)
        self.created = self.core.create_interview(key(), 'synthetic-research')
        self.sid = self.created['session_id']
        state = self.core.get_state(self.sid)
        self.answer = self.core.submit_answer(self.sid, state['current_round']['round_id'], key(), key(), '我主动分配任务。我主动分配任务。最后团队完成了计划。')
        self.rev = self.core.get_state(self.sid)['version']
        self.request = dict(version=self.rev, selections=[{'answer_id': self.answer['answer_id'], 'transcript_revision': 1}], modules=['word_frequency'],
                            repeats=2, purpose='合成流程验证', condition='固定输入', participant_group='fixture-only', cloud_consent=False, idempotency_key=key())

    def completed(self):
        created = self.store.create_runs(self.sid, **self.request)
        for _ in created['runs']:
            job = self.store.claim()
            result = execute_run(job['input'], Path(self.tmp.name) / job['run_id'])
            self.store.complete(job['run_id'], job['lease_token'], result)
        return created

    def test_runs_are_independent_and_do_not_mutate_main_report(self):
        before = self.core.get_report(self.sid)
        created = self.completed()
        self.assertNotEqual(*[x['run_id'] for x in created['runs']])
        self.assertEqual(self.core.get_report(self.sid), before)
        for run in created['runs']:
            result = self.store.get_run(self.sid, run['run_id'])
            self.assertEqual(result['status'], 'succeeded')
            self.assertFalse(result['result']['result_cache_reused'])
            self.assertNotIn('_media_path', json.dumps(result))
        self.assertEqual(self.store.create_runs(self.sid, **self.request), created)

    def test_original_au_net_is_an_explicit_research_choice_not_default_plan(self):
        before = self.core.get_report(self.sid)
        created = self.store.create_runs(self.sid, **{**self.request, 'modules': ['au_net'], 'repeats': 1})
        job = self.store.claim()
        self.assertEqual(job['input']['modules'], ['au_net'])
        result = execute_run(job['input'], Path(self.tmp.name) / job['run_id'])
        self.assertEqual(result['multimodal'][0]['modules']['au_net']['status'], 'no_media')
        self.assertEqual(self.core.get_report(self.sid), before)
        self.assertEqual(len(created['runs']), 1)

    def test_publish_creates_new_report_without_overwriting_old_or_score(self):
        old = self.core.get_report(self.sid)
        created = self.completed()
        new = self.store.publish(self.sid, version=self.rev, run_ids=[created['runs'][0]['run_id']], note='合成运行显式附录', idempotency_key=key())
        self.assertEqual(new['version'], self.rev + 1)
        self.assertEqual(new['total_score'], old['total_score'])
        self.assertEqual(self.core.get_report(self.sid, self.rev), old)
        self.assertEqual(new['research_appendix']['usage'], 'observed_only')

    def test_reference_missing_is_not_zero_and_freezes(self):
        old = self.core.get_report(self.sid)
        new = self.store.reference(self.sid, version=self.rev, targets={'leadership': 4}, title='老师草案', source='合成测试，无效度主张', status='draft', idempotency_key=key())['report']
        self.assertEqual(len(new['potential_profile']['factors']), 8)
        self.assertEqual(new['potential_profile']['planned_count'], 3)
        self.assertTrue(all(x['score'] is None for x in new['potential_profile']['factors']))
        self.assertEqual([x['target'] for x in new['potential_profile']['factors'] if x['factor_id'] == 'leadership'], [4])
        self.assertEqual(self.core.get_report(self.sid, self.rev), old)

    def test_cloud_is_explicit_and_lease_is_fenced(self):
        request = {**self.request, 'modules': ['semantic']}
        with self.assertRaises(DomainError):
            self.store.create_runs(self.sid, **request)
        self.store.create_runs(self.sid, **self.request)
        job = self.store.claim()
        with self.assertRaises(DomainError):
            self.store.complete(job['run_id'], 'wrong', error='fixture')
        with self.assertRaises(DomainError):
            self.store.complete(job['run_id'], job['lease_token'], result={'run_id': job['run_id'], 'input_versions': {}, 'status': 'succeeded'})

    def test_annotation_is_independent_and_cross_session_denied(self):
        created = self.completed()
        ref = {k: self.answer[k] for k in ('answer_id', 'transcript_revision')}
        ref['quote'] = '我主动分配任务。'
        note = self.store.annotate(self.sid, experiment_id=created['experiment']['experiment_id'], rater='fixture-rater', exposure='blind_self_declared',
                                   ratings=[{'question_id': self.answer['question_id'], 'score': 3, 'reason': '合成标注', 'evidence': [ref]}], note='验证独立标注非评分覆盖', idempotency_key=key())
        self.assertEqual(self.core.get_state(self.sid)['version'], self.rev)
        with self.core._db(True) as db:
            with self.assertRaises(Exception):
                db.execute('UPDATE reference_annotations SET payload=? WHERE id=?', ('{}', note['annotation_id']))
        other = self.core.create_interview(key())['session_id']
        with self.assertRaises(DomainError):
            self.store.get_run(other, created['runs'][0]['run_id'])
        comparison = self.store.compare(self.sid, [r['run_id'] for r in created['runs']])
        self.assertIsNone(comparison['blind_reference_mae'])

    def test_v2_position_separates_identical_quotes(self):
        with self.core._db() as db:
            answers = self.core._content_inputs(db, self.sid, self.answer['question_id'])
            segment = answers[0]['segments'][1]
            evidence = {'answer_id': self.answer['answer_id'], 'transcript_revision': 1, 'quote': segment['text'],
                        'segment_id': segment['segment_id'], 'char_start': segment['char_start'], 'char_end': segment['char_end']}
            refs = self.core._validate_evidence(db, self.sid, [evidence])
            self.assertEqual(refs[0]['char_start'], len('我主动分配任务。'))
            evidence['char_start'] = 0
            with self.assertRaises(DomainError):
                self.core._validate_evidence(db, self.sid, [evidence])

    def test_http_research_permissions_and_strict_cloud_consent(self):
        from test_interview_workbench import ASGIClient
        entry=importlib.import_module('interview_entry')
        with patch.object(entry,'core',self.core):
            client=ASGIClient(entry.app)
            base='/v1/interviews/'+self.sid
            candidate={'Authorization':'Bearer '+self.created['candidate_token']}
            hr={'Authorization':'Bearer '+self.created['hr_token']}
            for path in ('/research/runs','/research/export','/factor-profile','/research/annotations'):
                self.assertEqual(client.get(base+path,headers=candidate).status_code,403)
                self.assertEqual(client.get(base+path,headers=hr).status_code,200)
            invalid=client.post(base+'/research/runs',headers=hr,json={**self.request,'cloud_consent':'yes'})
            self.assertEqual(invalid.status_code,422)
            created=client.post(base+'/research/runs',headers=hr,json=self.request)
            self.assertEqual(created.status_code,200)
            other=self.core.create_interview(key())
            wrong={'Authorization':'Bearer '+other['hr_token']}
            self.assertEqual(client.get(base+'/research/runs',headers=wrong).status_code,403)

    def test_export_includes_alignment_and_reference_history(self):
        self.completed()
        self.store.reference(self.sid,version=self.rev,targets={'leadership':3},title='合成参考',source='fixture',status='draft',idempotency_key=key())
        data=self.store.export(self.sid)
        self.assertEqual(len(data['reference_profiles']),1)
        self.assertIn('alignment_reviews',data)
        self.assertEqual(len(data['runs']),2)
        self.assertNotIn('_media_path',json.dumps(data))


if __name__ == '__main__':
    unittest.main()
