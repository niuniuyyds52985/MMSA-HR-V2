"""v3 media-bound phases. Fixtures only: no model execution or cloud traffic."""
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


PLAN = {'version': 'local-observations.v2', 'enabled': True, 'trigger': 'after_media_saved',
    'modules': ['au', 'gaze', 'expression', 'audio', 'gaze360', 'emotic'],
    'phases': [{'id': 'core', 'modules': ['au', 'gaze', 'expression', 'audio']},
               {'id': 'extended', 'modules': ['gaze360', 'emotic']}],
    'usage': 'observed_only', 'automatic_report_snapshot': True, 'cloud_media': False}


class MediaStandardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'DASHSCOPE_API_KEY': '', 'INTERVIEW_CREDENTIALS_FILE': str(Path(self.tmp.name) / 'missing')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.core = InterviewCore(self.tmp.name, Path(__file__).resolve().parents[1] / 'config' / 'interview.v2.json')
        self.core.config['analysis_plan'] = json.loads(dumps(PLAN))
        self.store = ResearchStore(self.core)
        self.session = self.core.create_interview(key(), 'synthetic-media-v3')
        self.sid = self.session['session_id']

    def media_answer(self, replace=None):
        path = self.core.data_dir / 'media' / (key() + '.wav')
        path.write_bytes(b'synthetic-not-playable-media')
        return self.core.submit_answer(self.sid, self.session['current_round']['round_id'], key(), key(), path=path,
            media_hash=hashlib.sha256(path.read_bytes()).hexdigest(), media_meta={'duration_ms': 2000, 'mime_type': 'audio/wav'},
            replace_answer_id=replace)['answer_id']

    def asr(self, text='合成原话'):
        job = self.core.claim_job('test-media', job_type='media')
        self.core.complete_job(job['job_id'], job['lease_token'], {'transcription': {'status': 'ok', 'text': text,
            'segments': [{'start_ms': 0, 'end_ms': 2000, 'text': text}]}})

    def result(self, job, status='succeeded'):
        aid, rev = next(iter(job['input']['input_versions'].items()))
        modules = {m: {'status': 'complete', 'observations': [], 'fixture': True} for m in job['input']['modules']}
        if 'audio' in modules:
            modules['audio']['summary'] = {'duration_ms': 2000, 'speech_ms': 1000,
                'transcript_units': 0, 'speech_rate_units_per_min': None, 'articulation_rate_units_per_speech_min': None}
        return {'run_id': job['run_id'], 'input_versions': {aid: rev}, 'status': status,
            'media_identity': job['input']['media_identity'], 'multimodal': [{'answer_id': aid,
                'transcript_revision': rev, 'media_sha256': job['input']['media_identity']['media_sha256'], 'modules': modules}]}

    def test_queues_on_saved_media_before_asr_and_separates_lanes(self):
        aid = self.media_answer()
        status = self.store.analysis_status(self.sid)
        self.assertEqual(status['media']['pending'], 1)
        self.assertEqual(status['multimodal']['pending_count'], 2)
        self.assertEqual(len(self.store.enqueue_standard()), 2)
        self.assertEqual(self.store.enqueue_standard(), [])
        core = self.store.claim(lane='core')
        extended = self.store.claim(lane='extended')
        self.assertEqual(core['input']['analysis_phase'], 'core')
        self.assertEqual(extended['input']['analysis_phase'], 'extended')
        self.assertEqual(core['input']['input_versions'], {aid: 1})
        self.assertFalse(core['input']['cloud_authorized'])
        self.assertEqual(self.store.analysis_status(self.sid)['media']['pending'], 1)
        self.assertIsNone(self.store.claim(lane='core'))
        self.assertIsNone(self.store.claim(lane='extended'))
        with self.core._db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM standard_analysis_runs').fetchone()[0], 0)

    def test_asr_revision_does_not_cancel_queued_or_repeat_completed_media(self):
        aid = self.media_answer()
        original_ids = set(self.store.enqueue_standard())
        self.asr()
        core = self.store.claim(lane='core')
        self.assertIn(core['run_id'], original_ids)
        self.assertEqual(core['input']['input_versions'][aid], 1)
        before = self.core.get_state(self.sid)['version']
        self.store.complete(core['run_id'], core['lease_token'], self.result(core))
        self.assertGreater(self.core.get_state(self.sid)['version'], before)
        report = self.core.get_report(self.sid)
        run = report['standard_analysis']['runs'][0]
        self.assertEqual(run['transcript_revision'], 2)
        self.assertEqual(run['analysis_input_transcript_revision'], 1)
        output = run['result']['multimodal'][0]
        self.assertEqual(output['projection']['basis'], 'same_answer_and_media_sha256')
        self.assertEqual(output['modules']['audio']['summary']['transcript_units'], 4)
        self.assertEqual(output['modules']['audio']['summary']['speech_rate_units_per_min'], 120)
        raw = self.store.get_run(self.sid, core['run_id'])['result']['multimodal'][0]
        self.assertEqual(raw['transcript_revision'], 1)
        self.assertEqual(raw['modules']['audio']['summary']['transcript_units'], 0)
        self.assertEqual(self.store.enqueue_standard(), [])
        self.core.revise_transcript(self.sid, aid, 2, '修订', 'fixture', key(), actor='hr')
        self.assertEqual(self.store.enqueue_standard(), [])
        latest = self.core.get_report(self.sid)['standard_analysis']['runs'][0]
        self.assertEqual(latest['result']['multimodal'][0]['modules']['audio']['summary']['transcript_units'], 2)
        self.assertEqual(self.core.get_report(self.sid, report['version']), report)

    def test_completed_core_remains_visible_while_extended_runs(self):
        self.media_answer()
        core = self.store.claim(lane='core')
        extended = self.store.claim(lane='extended')
        self.store.complete(core['run_id'], core['lease_token'], self.result(core))
        state = self.core.get_report(self.sid)['standard_analysis']
        self.assertEqual(state['pending_count'], 1)
        self.assertEqual(len(state['runs']), 2)
        self.assertEqual({r['phase']: r['status'] for r in state['runs']}, {'core': 'succeeded', 'extended': 'running'})
        self.store.complete(extended['run_id'], extended['lease_token'], self.result(extended))
        self.assertEqual(self.core.get_report(self.sid)['standard_analysis']['pending_count'], 0)
        self.assertEqual(self.core.get_report(self.sid)['standard_analysis']['answers'][0]['status'], 'succeeded')

    def test_replaced_answer_hides_old_phases_and_old_completion_does_not_bump(self):
        aid = self.media_answer()
        running = self.store.claim(lane='core')
        queued = next(r['run_id'] for r in self.store.list_runs(self.sid)['runs'] if r['run_id'] != running['run_id'])
        replacement = self.media_answer(aid)
        before = self.core.get_state(self.sid)['version']
        self.store.complete(running['run_id'], running['lease_token'], self.result(running))
        self.assertEqual(self.core.get_state(self.sid)['version'], before)
        current = self.store.claim(lane='extended')
        self.assertEqual(current['input']['media_identity']['answer_id'], replacement)
        self.assertEqual(self.store.get_run(self.sid, queued)['status'], 'failed')
        self.assertTrue(all(r['answer_id'] == replacement for r in self.core.get_report(self.sid)['standard_analysis']['runs']))

    def test_explicit_retry_rebinds_text_but_only_repeats_failed_phase(self):
        aid = self.media_answer()
        core = self.store.claim(lane='core')
        self.store.complete(core['run_id'], core['lease_token'], error='fixture failure')
        self.asr()
        request = {'run_id': core['run_id'], 'version': self.core.get_state(self.sid)['version'], 'idempotency_key': key()}
        retried = self.store.retry_standard(self.sid, **request)
        self.assertEqual(self.store.retry_standard(self.sid, **request), retried)
        next_run = self.store.claim(lane='core')
        self.assertEqual(next_run['run_id'], retried['run_id'])
        self.assertEqual(next_run['input']['input_versions'], {aid: 2})
        runs = self.core.get_report(self.sid)['standard_analysis']['runs']
        self.assertEqual({r['phase']: r['attempt'] for r in runs}, {'core': 2, 'extended': 1})
        self.assertEqual(self.store.enqueue_standard(), [])

    def test_restore_preserves_media_rows_pauses_and_requires_explicit_retry(self):
        from delivery_tools import backup_data, restore_data
        self.media_answer()
        self.store.enqueue_standard()
        archived = backup_data(self.tmp.name, Path(self.tmp.name) / 'packages')
        target = Path(self.tmp.name) / 'restored'
        restore_data(archived['archive'], target)
        other = InterviewCore(target, Path(__file__).resolve().parents[1] / 'config' / 'interview.v2.json')
        store = ResearchStore(other)
        status = store.analysis_status(self.sid)
        self.assertTrue(status['multimodal']['paused'])
        self.assertEqual(len(status['multimodal']['runs']), 2)
        self.assertEqual(store.enqueue_standard(), [])
        self.assertIsNone(store.claim(lane='core'))
        failed = next(r for r in status['multimodal']['runs'] if r['phase'] == 'core')
        with self.assertRaises(DomainError) as error:
            store.retry_standard(self.sid, run_id=failed['run_id'], version=status['version'], idempotency_key=key())
        self.assertEqual(error.exception.code, 'analysis_paused')
        resumed = store.resume_standard(self.sid, version=status['version'], idempotency_key=key())
        self.assertEqual(store.enqueue_standard(), [])
        retried = store.retry_standard(self.sid, run_id=failed['run_id'], version=resumed['version'], idempotency_key=key())
        job = store.claim(lane='core')
        self.assertEqual(job['run_id'], retried['run_id'])
        self.assertTrue(Path(job['input']['answers'][0]['_media_path']).resolve().is_relative_to(target.resolve()))

    def test_expired_phase_marks_failed_without_repeating(self):
        self.media_answer()
        core = self.store.claim(lane='core')
        with self.core._db(True) as db:
            db.execute('UPDATE research_runs SET lease_until=0 WHERE id=?', (core['run_id'],))
        before = self.core.get_state(self.sid)['version']
        self.assertIsNone(self.store.claim(lane='core'))
        self.assertGreater(self.core.get_state(self.sid)['version'], before)
        self.assertEqual(self.store.get_run(self.sid, core['run_id'])['status'], 'failed')
        self.assertEqual(self.store.enqueue_standard(), [])

    def test_independent_research_only_enters_extended_lane(self):
        aid = self.media_answer()
        self.store.enqueue_standard()
        with self.core._db(True) as db:
            db.execute("UPDATE research_runs SET status='succeeded' WHERE session_id=?", (self.sid,))
        created = self.store.create_runs(self.sid, version=self.core.get_state(self.sid)['version'],
            selections=[{'answer_id': aid, 'transcript_revision': 1}], modules=['word_frequency'], repeats=1,
            purpose='synthetic lane regression', condition='fixture', participant_group='synthetic',
            cloud_consent=False, idempotency_key=key())
        self.assertIsNone(self.store.claim(lane='core'))
        job = self.store.claim(lane='extended')
        self.assertEqual(job['run_id'], created['runs'][0]['run_id'])

    def test_rejects_foreign_media_result(self):
        self.media_answer()
        job = self.store.claim(lane='core')
        result = self.result(job)
        result['multimodal'][0]['media_sha256'] = '0' * 64
        with self.assertRaises(DomainError) as error:
            self.store.complete(job['run_id'], job['lease_token'], result)
        self.assertEqual(error.exception.code, 'wrong_research_output')


if __name__ == '__main__':
    unittest.main()
