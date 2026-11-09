"""Synthetic immutable timeline projections. No media decoder or cloud requests."""
import asyncio
import copy
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_core import DomainError, InterviewCore


def key():
    return str(uuid.uuid4())


class QueryASGIClient:
    """Small local ASGI harness that actually carries query_string."""
    def __init__(self, app):
        self.app = app

    def get(self, url, headers=None):
        async def run():
            target = urlsplit(url)
            sent, messages = False, []

            async def receive():
                nonlocal sent
                if not sent:
                    sent = True
                    return {'type': 'http.request', 'body': b'', 'more_body': False}
                await asyncio.Event().wait()

            async def send(message):
                messages.append(message)

            scope = {'type': 'http', 'asgi': {'version': '3.0', 'spec_version': '2.4'},
                     'http_version': '1.1', 'method': 'GET', 'scheme': 'http',
                     'path': target.path, 'raw_path': target.path.encode(),
                     'query_string': target.query.encode(), 'root_path': '',
                     'headers': [(k.lower().encode(), v.encode()) for k, v in {'host': '127.0.0.1', **(headers or {})}.items()],
                     'client': ('127.0.0.1', 12345), 'server': ('127.0.0.1', 80)}
            await self.app(scope, receive, send)
            start = next(m for m in messages if m['type'] == 'http.response.start')
            body = b''.join(m.get('body', b'') for m in messages if m['type'] == 'http.response.body')
            return type('Response', (), {'status_code': start['status'], 'json': lambda self: json.loads(body),
                                         'headers': {k.decode(): v.decode() for k, v in start['headers']}})()
        return asyncio.run(run())


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        env = patch.dict(os.environ, {'INTERVIEW_DATA_DIR': str(self.folder), 'DASHSCOPE_API_KEY': '',
                                     'INTERVIEW_CREDENTIALS_FILE': str(self.folder / 'absent')})
        env.start()
        self.addCleanup(env.stop)
        self.core = InterviewCore(self.folder)
        self.created = self.core.create_interview(key(), 'synthetic-timeline')
        self.sid = self.created['session_id']
        self.entry = importlib.import_module('interview_entry')
        core_patch = patch.object(self.entry, 'core', self.core)
        core_patch.start()
        self.addCleanup(core_patch.stop)
        self.client = QueryASGIClient(self.entry.app)

    def answer(self, text='', media=True, replace=None):
        state = self.core.get_state(self.sid)
        # A registered synthetic path suffices; timeline must never open it.
        return self.core.submit_answer(self.sid, state['current_round']['round_id'], key(), key(), text,
                                       path=self.folder / 'media' / (key() + '.webm') if media else None,
                                       media_meta={'duration_ms': 5000, 'mime_type': 'video/webm'} if media else {},
                                       media_hash='a' * 64 if media else None, replace_answer_id=replace)

    def complete(self, answer, text='我负责协调。后来复盘。', segments=None, audio=None):
        job = self.core.claim_job(job_type='media')
        self.assertEqual(job['answer_id'], answer['answer_id'])
        result = {'duration_ms': 5000, 'has_audio': True, 'has_video': True,
                  'transcription': {'status': 'ok', 'text': text, 'segments': segments if segments is not None else [
                      {'text': '我负责协调。', 'start_ms': 0, 'end_ms': 1000},
                      {'text': '后来复盘。', 'start_ms': 2000, 'end_ms': 3500}]},
                  'audio': audio if audio is not None else {'status': 'ok', 'windows': [
                      {'start_ms': 0, 'end_ms': 1000, 'rms_dbfs': -21.5},
                      {'start_ms': 1000, 'end_ms': 2000, 'rms_dbfs': -18.3}]}}
        self.core.complete_job(job['job_id'], job['lease_token'], result)
        return job

    def timeline(self, answer, revision):
        return self.core.get_answer_timeline(self.sid, answer['answer_id'], revision)

    def request(self, answer, revision=2, token=None, session_id=None):
        url = f'/v1/interviews/{session_id or self.sid}/answers/{answer["answer_id"]}/timeline'
        if revision is not None:
            url += '?transcript_revision=' + str(revision)
        headers = {'Authorization': 'Bearer ' + token} if token else {}
        return self.client.get(url, headers)

    def evaluate(self, answer, revision=2, quote='我负责协调。', eligible=False):
        evidence = [{'answer_id': answer['answer_id'], 'transcript_revision': revision, 'quote': quote}]
        result = {'question_id': answer['question_id'],
                  'rating': {'score': 4, 'reason': '合成评分依据，仅验证链接', 'evidence': evidence},
                  'followup': {'status': 'eligible' if eligible else 'content_sufficient',
                               'branch_id': 'leadership_F3_Q1_action' if eligible else None,
                               'reason': '合成追问依据', 'evidence': evidence}}
        return self.core.apply_model_evaluation(self.sid, answer['question_id'], result,
                                                {answer['answer_id']: revision}, {'model': 'synthetic'})

    def test_exact_asr_source_has_stable_segments_rms_and_explicit_output_revision(self):
        answer = self.answer()
        job = self.complete(answer)
        first = self.timeline(answer, 2)
        self.assertEqual(first, self.timeline(answer, 2))
        self.assertEqual(first['alignment']['status'], 'aligned')
        self.assertEqual(first['alignment']['source_job_id'], job['job_id'])
        self.assertEqual(first['alignment']['asr_transcript_revision'], 2)
        self.assertEqual([s['text'] for s in first['segments']], ['我负责协调。', '后来复盘。'])
        self.assertEqual([(s['start_ms'], s['end_ms']) for s in first['segments']], [(0, 1000), (2000, 3500)])
        self.assertEqual(first['observations']['audio']['status'], 'available')
        self.assertTrue(all(w['usage'] == 'observed_only' for w in first['observations']['audio']['windows']))
        with self.core._db() as db:
            stored = json.loads(db.execute('SELECT result FROM jobs WHERE id=?', (job['job_id'],)).fetchone()[0])
        self.assertEqual(stored['output_transcript_revision'], 2)

    def test_http_explicit_revision_permissions_and_cross_session_isolation(self):
        answer = self.answer()
        self.complete(answer)
        other = self.core.create_interview(key(), 'synthetic-other')
        self.assertEqual(self.request(answer).status_code, 401)
        self.assertEqual(self.request(answer, token=self.created['candidate_token']).status_code, 403)
        self.assertEqual(self.request(answer, token=other['hr_token']).status_code, 403)
        self.assertEqual(self.request(answer, token=other['hr_token'], session_id=other['session_id']).status_code, 404)
        for revision in (None, 0, -1, 'invalid'):
            self.assertEqual(self.request(answer, revision, self.created['hr_token']).status_code, 422)
        self.assertEqual(self.request(answer, 99, self.created['hr_token']).status_code, 404)
        old = self.request(answer, 1, self.created['hr_token'])
        current = self.request(answer, 2, self.created['hr_token'])
        self.assertEqual(old.json()['text'], '')
        self.assertEqual(old.json()['transcript_revision'], 1)
        self.assertEqual(current.json()['text'], '我负责协调。后来复盘。')
        self.assertEqual(current.headers['cache-control'], 'no-store')

    def test_human_same_text_revision_never_inherits_time_but_history_remains(self):
        answer = self.answer()
        self.complete(answer)
        self.evaluate(answer)
        old = self.timeline(answer, 2)
        self.core.revise_transcript(self.sid, answer['answer_id'], 2, old['text'], '人工确认同文新版本', key(), 'hr')
        current = self.timeline(answer, 3)
        self.assertEqual(current['alignment']['status'], 'stale')
        self.assertEqual(current['alignment']['available_asr_revisions'], [2])
        self.assertTrue(all(s['start_ms'] is None for s in current['segments']))
        self.assertEqual(current['references'], [])
        self.assertEqual(current['observations']['audio']['status'], 'available')
        historical = self.timeline(answer, 2)
        self.assertFalse(historical['is_current_revision'])
        self.assertEqual(historical['segments'], old['segments'])
        self.assertEqual(historical['references'], old['references'])

    def test_replacement_preserves_old_answer_and_does_not_mix_new_recording(self):
        old = self.answer()
        self.complete(old)
        newer = self.answer('新录制人工文字。', replace=old['answer_id'])
        historical = self.timeline(old, 2)
        current = self.timeline(newer, 1)
        self.assertFalse(historical['answer_current'])
        self.assertEqual(historical['alignment']['status'], 'aligned')
        self.assertTrue(current['answer_current'])
        self.assertEqual(current['alignment']['status'], 'unaligned')
        self.assertEqual(current['observations']['audio']['windows'], [])
        self.assertNotEqual(historical['media']['url'], current['media']['url'])

    def test_repeated_quotes_are_disambiguated_and_emoji_offsets_are_codepoints(self):
        answer = self.answer()
        sentence = '🙂我做了。'
        self.complete(answer, sentence * 2, [
            {'text': sentence, 'start_ms': 0, 'end_ms': 1000},
            {'text': sentence, 'start_ms': 2000, 'end_ms': 3500}])
        self.evaluate(answer, quote='我做了')
        timeline = self.timeline(answer, 2)
        self.assertEqual(timeline['segments'][1]['char_start'], len(sentence))
        self.assertNotEqual(timeline['segments'][0]['segment_id'], timeline['segments'][1]['segment_id'])
        reference = next(r for r in timeline['references'] if r['source'] == 'model_rating')
        self.assertEqual(reference['match_status'], 'ambiguous')
        self.assertEqual([o['char_start'] for o in reference['occurrences']], [1, len(sentence) + 1])
        self.assertEqual([(o['start_ms'], o['end_ms']) for o in reference['occurrences']], [(0, 1000), (2000, 3500)])
        for occurrence in reference['occurrences']:
            self.assertEqual(timeline['text'][occurrence['char_start']:occurrence['char_end']], reference['quote'])

    def test_incomplete_repeated_asr_segments_do_not_pick_first_occurrence(self):
        answer = self.answer()
        self.complete(answer, '同一句。同一句。', [{'text': '同一句。', 'start_ms': 0, 'end_ms': 1000}])
        timeline = self.timeline(answer, 2)
        self.assertEqual(timeline['alignment']['status'], 'unaligned')
        self.assertTrue(all(s['start_ms'] is None for s in timeline['segments']))

    def test_legacy_asr_requires_actor_freshness_empty_input_and_exact_text(self):
        answer = self.answer()
        job = self.complete(answer)
        with self.core._db(True) as db:
            stored = json.loads(db.execute('SELECT result FROM jobs WHERE id=?', (job['job_id'],)).fetchone()[0])
            stored.pop('output_transcript_revision')
            db.execute('UPDATE jobs SET result=? WHERE id=?', (json.dumps(stored), job['job_id']))
        self.assertEqual(self.timeline(answer, 2)['alignment']['status'], 'aligned')
        with self.core._db(True) as db:
            db.execute("UPDATE revisions SET actor='hr' WHERE answer_id=? AND version=2", (answer['answer_id'],))
        self.assertEqual(self.timeline(answer, 2)['alignment']['status'], 'unaligned')

    def test_late_asr_and_equal_manual_text_have_no_output_version_or_borrowed_times(self):
        answer = self.answer()
        self.core.revise_transcript(self.sid, answer['answer_id'], 1, '我负责协调。后来复盘。', '人工先行修订', key(), 'hr')
        job = self.complete(answer)
        timeline = self.timeline(answer, 2)
        self.assertEqual(timeline['alignment']['status'], 'unaligned')
        with self.core._db() as db:
            stored = json.loads(db.execute('SELECT result FROM jobs WHERE id=?', (job['job_id'],)).fetchone()[0])
        self.assertIsNone(stored['output_transcript_revision'])
        self.assertFalse(stored['input_current_at_completion'])
        self.assertEqual(timeline['text'], '我负责协调。后来复盘。')

    def test_invalid_time_and_nonfinite_rms_are_not_clamped_or_serialized_as_values(self):
        answer = self.answer()
        self.complete(answer, segments=[{'text': '我负责协调。', 'start_ms': -10, 'end_ms': 6000},
                                        {'text': '后来复盘。', 'start_ms': 2000, 'end_ms': 3500}],
                      audio={'status': 'ok', 'windows': [{'start_ms': 0, 'end_ms': 6000, 'rms_dbfs': float('nan')}]})
        timeline = self.timeline(answer, 2)
        self.assertEqual(timeline['alignment']['status'], 'partial')
        self.assertIsNone(timeline['segments'][0]['start_ms'])
        self.assertIsNone(timeline['segments'][0]['end_ms'])
        self.assertEqual(timeline['observations']['audio']['status'], 'invalid')
        self.assertEqual(timeline['observations']['audio']['windows'], [])
        self.assertEqual(self.request(answer, 2, self.created['hr_token']).status_code, 200)

    def test_overlap_and_reversed_asr_times_are_partial_without_simultaneous_alignment(self):
        answer = self.answer()
        self.complete(answer, '甲。乙。丙。丁。', [
            {'text': '甲。', 'start_ms': 1000, 'end_ms': 2000},
            {'text': '乙。', 'start_ms': 1500, 'end_ms': 2500},
            {'text': '丙。', 'start_ms': 500, 'end_ms': 900},
            {'text': '丁。', 'start_ms': 2000, 'end_ms': 3000}],
            audio={'status': 'ok', 'windows': [
                {'start_ms': 0, 'end_ms': 1000, 'rms_dbfs': -20},
                {'start_ms': 500, 'end_ms': 1500, 'rms_dbfs': -19}]})
        timeline = self.timeline(answer, 2)
        self.assertEqual(timeline['alignment']['status'], 'partial')
        self.assertEqual([(s['start_ms'], s['end_ms']) for s in timeline['segments']],
                         [(1000, 2000), (None, None), (None, None), (2000, 3000)])
        self.assertEqual(timeline['observations']['audio']['status'], 'invalid')
        self.assertEqual(timeline['observations']['audio']['windows'], [])

    def test_text_only_preserves_spaces_and_returns_null_media_times(self):
        text = '🙂第一句。\n  第二句， 保留空格。'
        answer = self.answer(text, media=False)
        timeline = self.timeline(answer, 1)
        self.assertEqual(timeline['text'], text)
        self.assertEqual(''.join(s['text'] for s in timeline['segments']), text)
        self.assertEqual(timeline['alignment']['status'], 'no_media')
        self.assertIsNone(timeline['media']['url'])
        self.assertFalse(timeline['media']['available'])
        for segment in timeline['segments']:
            self.assertEqual(text[segment['char_start']:segment['char_end']], segment['text'])
            self.assertIsNone(segment['start_ms'])

    def test_existing_scores_decisions_and_all_business_tables_remain_unchanged(self):
        answer = self.answer()
        self.complete(answer)
        self.evaluate(answer, eligible=True)
        self.core.next_round(self.sid, key(), answer['round_id'])
        state = self.core.get_state(self.sid)
        ratings = [{'question_id': q['question_id'], 'score': 4 if index == 0 else None,
                    'reason': '合成复核', 'evidence': [{'answer_id': answer['answer_id'], 'transcript_revision': 2, 'quote': '我负责协调。'}] if index == 0 else []}
                   for index, q in enumerate(self.core.config['questions'])]
        self.core.submit_review(self.sid, state['version'], '合成复核版本', ratings, key())
        tables = ('sessions', 'rounds', 'answers', 'revisions', 'jobs', 'operations', 'decisions',
                  'decision_events', 'assessments', 'model_evaluations', 'cloud_authorizations', 'reviews', 'reports')
        with self.core._db() as db:
            before = {t: [tuple(row) for row in db.execute(f'SELECT * FROM {t}')] for t in tables}
        with patch('requests.post') as request, patch('pathlib.Path.open', side_effect=AssertionError('timeline must not open media')), \
                patch.object(self.core, '_report', side_effect=AssertionError('GET must not create cached reports')):
            timeline = self.timeline(answer, 2)
        request.assert_not_called()
        sources = {r['source'] for r in timeline['references']}
        self.assertEqual(sources, {'model_rating', 'model_followup', 'followup_assessment', 'hr_rating', 'decision'})
        self.assertTrue(all(r['match_status'] == 'unique' for r in timeline['references']))
        self.assertEqual(timeline['reference_scope'], 'all_records_for_requested_transcript_revision')
        with self.core._db() as db:
            after = {t: [tuple(row) for row in db.execute(f'SELECT * FROM {t}')] for t in tables}
        self.assertEqual(after, before)


if __name__ == '__main__':
    unittest.main()
