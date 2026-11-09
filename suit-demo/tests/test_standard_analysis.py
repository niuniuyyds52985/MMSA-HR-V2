"""Local-default orchestration and per-question review are version fenced.

All observations here are fixtures; tests never execute models or cloud calls.
"""
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_core import InterviewCore, DomainError, dumps
from interview_research import ResearchStore


def key():
    return str(uuid.uuid4())


class StandardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'DASHSCOPE_API_KEY': '', 'INTERVIEW_CREDENTIALS_FILE': str(Path(self.tmp.name) / 'missing')})
        self.env.start()
        self.addCleanup(self.env.stop)
        # This suite freezes the historical v2 transcript-bound contract.
        self.core = InterviewCore(self.tmp.name, Path(__file__).resolve().parents[1] / 'config' / 'interview.v2.json')
        self.store = ResearchStore(self.core)
        self.session = self.core.create_interview(key(), 'synthetic-standard')
        self.sid = self.session['session_id']

    def media_answer(self):
        path = self.core.data_dir / 'media' / 'fixture.wav'
        path.write_bytes(b'fixture-not-playable')
        answer = self.core.submit_answer(self.sid, self.session['current_round']['round_id'], key(), key(), path=path,
            media_hash=hashlib.sha256(path.read_bytes()).hexdigest(), media_meta={'duration_ms': 1000, 'mime_type': 'audio/wav'})
        job = self.core.claim_job('test-worker')
        self.core.complete_job(job['job_id'], job['lease_token'], {'transcription': {'status': 'ok', 'text': '合成原话',
            'segments': [{'start_ms': 0, 'end_ms': 1000, 'text': '合成原话'}]}})
        return answer['answer_id']

    def result(self, job, status='succeeded'):
        aid, rev = next(iter(job['input']['input_versions'].items()))
        return {'run_id': job['run_id'], 'input_versions': {aid: rev}, 'status': status,
            'multimodal': [{'answer_id': aid, 'transcript_revision': rev, 'modules': {
                m: {'status': 'complete', 'observations': [], 'fixture': True} for m in job['input']['modules']}}]}

    def test_default_new_session_runs_once_and_enters_new_report(self):
        aid = self.media_answer()
        before = self.core.get_report(self.sid)
        self.assertEqual(before['standard_analysis']['answers'][0]['status'], 'waiting_worker')
        job = self.store.claim()
        self.assertEqual(job['input']['run_kind'], 'standard_local')
        self.assertFalse(job['input']['cloud_authorized'])
        self.assertNotIn('semantic', job['input']['modules'])
        self.assertEqual(self.store.enqueue_standard(), [])
        self.store.complete(job['run_id'], job['lease_token'], self.result(job))
        after = self.core.get_report(self.sid)
        self.assertEqual(after['standard_analysis']['runs'][0]['answer_id'], aid)
        self.assertEqual(after['standard_analysis']['runs'][0]['status'], 'succeeded')
        self.assertIsNone(after['research_appendix'])
        self.assertEqual(self.core.get_report(self.sid, before['version']), before)
        self.assertIsNone(self.store.claim())

    def test_read_never_schedules_or_changes_old_session(self):
        self.media_answer()
        with self.core._db(True) as db:
            config = json.loads(db.execute('SELECT config FROM sessions WHERE id=?', (self.sid,)).fetchone()[0])
            config.pop('analysis_plan')
            db.execute('UPDATE sessions SET config=? WHERE id=?', (dumps(config), self.sid))
        before = self.core.get_state(self.sid)['version']
        self.assertFalse(self.store.analysis_status(self.sid)['multimodal']['enabled'])
        self.assertIsNone(self.store.claim())
        self.assertEqual(self.core.get_state(self.sid)['version'], before)

    def test_late_run_does_not_overwrite_current_input(self):
        aid = self.media_answer()
        job = self.store.claim()
        self.core.revise_transcript(self.sid, aid, 2, '修订后的合成原话', 'fixture', key(), actor='hr')
        before = self.core.get_state(self.sid)['version']
        self.store.complete(job['run_id'], job['lease_token'], self.result(job))
        self.assertEqual(self.core.get_state(self.sid)['version'], before)
        self.assertFalse(self.core.get_report(self.sid)['standard_analysis']['runs'])
        new = self.store.claim()
        self.assertEqual(new['input']['input_versions'][aid], 3)

    def test_obsolete_queued_default_is_not_executed(self):
        aid = self.media_answer()
        old_id = self.store.enqueue_standard()[0]
        self.core.revise_transcript(self.sid, aid, 2, '变更后的合成原话', 'fixture', key(), actor='hr')
        job = self.store.claim()
        self.assertNotEqual(job['run_id'], old_id)
        self.assertEqual(job['input']['input_versions'][aid], 3)
        self.assertEqual(self.store.get_run(self.sid, old_id)['status'], 'failed')

    def test_explicit_retry_is_idempotent_and_fenced(self):
        self.media_answer()
        job = self.store.claim()
        self.store.complete(job['run_id'], job['lease_token'], error='fixture failure')
        request = {'run_id': job['run_id'], 'version': self.core.get_state(self.sid)['version'], 'idempotency_key': key()}
        retried = self.store.retry_standard(self.sid, **request)
        self.assertEqual(self.store.retry_standard(self.sid, **request), retried)
        self.assertNotEqual(retried['run_id'], job['run_id'])
        with self.assertRaises(DomainError):
            self.store.retry_standard(self.sid, **{**request, 'idempotency_key': key()})
        self.assertEqual(self.core.get_report(self.sid)['standard_analysis']['runs'][0]['attempt'], 2)

    def test_migration_preserves_pending_chunks_and_pauses_new_environment(self):
        from delivery_tools import backup_data, restore_data
        from interview_uploads import UploadStore, MIN_CHUNK
        self.media_answer()
        uploads = UploadStore(self.core)
        data = b'x' * (MIN_CHUNK + 20)
        created = uploads.create(self.sid, round_id=self.session['current_round']['round_id'], attempt_id=key(),
            filename='pending.webm', total_size=len(data), chunk_size=MIN_CHUNK, sha256=hashlib.sha256(data).hexdigest(), idempotency_key=key())
        uploads.put_chunk(self.sid, created['upload_id'], 0, data[:MIN_CHUNK], hashlib.sha256(data[:MIN_CHUNK]).hexdigest())
        before = self.core.get_state(self.sid)
        archive = backup_data(self.tmp.name, Path(self.tmp.name) / 'packages')
        self.assertEqual(archive['pending_upload_chunks'], 1)
        target = Path(self.tmp.name) / 'restored'
        restored = restore_data(archive['archive'], target)
        self.assertEqual(restored['standard_analysis_sessions_paused'], 1)
        migrated_core = InterviewCore(target)
        migrated_store = ResearchStore(migrated_core)
        status = migrated_store.analysis_status(self.sid)
        self.assertTrue(status['multimodal']['paused'])
        self.assertEqual(migrated_store.enqueue_standard(), [])
        self.assertEqual(UploadStore(migrated_core).status(self.sid, created['upload_id'])['received_chunks'], [0])
        resumed = migrated_store.resume_standard(self.sid, version=status['version'], idempotency_key=key())
        self.assertEqual(resumed['status'], 'enabled')
        self.assertEqual(len(migrated_store.enqueue_standard()), 1)
        self.assertEqual(self.core.get_state(self.sid), before)

    def test_analysis_progress_is_lightweight_and_reports_review_count(self):
        self.media_answer()
        job = self.store.claim()
        self.store.complete(job['run_id'], job['lease_token'], self.result(job))
        progress = self.store.analysis_status(self.sid)
        self.assertNotIn('result', progress['multimodal']['runs'][0])
        self.assertTrue(progress['multimodal']['runs'][0]['has_result'])
        self.assertIn('result', self.store.analysis_status(self.sid, include_results=True)['multimodal']['runs'][0])

    def test_partial_review_keeps_other_questions_and_stales_only_changed_question(self):
        answers = []
        for n in range(3):
            state = self.core.get_state(self.sid)
            answer = self.core.submit_answer(self.sid, state['current_round']['round_id'], key(), key(), f'合成第{n}题：我拆分任务并检查结果。')
            answers.append(answer)
            self.core.next_round(self.sid, key(), state['current_round']['round_id'])
        def rating(answer, score):
            return {'question_id': answer['question_id'], 'score': score, 'reason': '合成锚点复核',
                'evidence': [{'answer_id': answer['answer_id'], 'transcript_revision': 1, 'quote': answer['transcript']}]}
        first = self.core.submit_review(self.sid, self.core.get_state(self.sid)['version'], '逐题保存', [rating(answers[0], 4)], key())
        self.assertEqual(first['reviewed_question_count'], 1)
        self.assertEqual(first['questions'][1]['review_status'], 'not_reviewed')
        second = self.core.submit_review(self.sid, first['version'], '逐题保存', [rating(answers[1], 3)], key())
        third = self.core.submit_review(self.sid, second['version'], '逐题保存', [rating(answers[2], 5)], key())
        self.assertEqual([q['score'] for q in third['questions']], [4, 3, 5])
        self.assertEqual(third['reviewed_question_count'], 3)
        summary = next(s for s in self.core.list_workbench_sessions()['sessions'] if s['session_id'] == self.sid)
        self.assertEqual(summary['review_status'], 'reviewed')
        self.assertEqual(summary['reviewed_question_count'], 3)
        self.core.revise_transcript(self.sid, answers[0]['answer_id'], 1, '更新的合成内容。', '核对原话', key(), actor='hr')
        revised = self.core.get_report(self.sid)
        self.assertEqual([q['review_status'] for q in revised['questions']], ['stale', 'reviewed', 'reviewed'])
        self.assertEqual(self.core.get_report(self.sid, first['version']), first)


if __name__ == '__main__':
    unittest.main()
