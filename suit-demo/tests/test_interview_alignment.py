"""Synthetic manual-timing audit, version and citation-position invariants."""
import copy
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_alignment import AlignmentError, overlay, validate_mapping
from interview_core import InterviewCore, DomainError
from interview_research import ResearchStore
from interview_timeline import build_timeline, _evidence_occurrences


def key():
    return str(uuid.uuid4())


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {'DASHSCOPE_API_KEY': '', 'INTERVIEW_CREDENTIALS_FILE': str(Path(self.tmp.name) / 'missing')})
        env.start()
        self.addCleanup(env.stop)
        self.core = InterviewCore(self.tmp.name)
        self.store = ResearchStore(self.core)
        self.sid = self.core.create_interview(key(), 'alignment-fixture')['session_id']
        self.path = Path(self.tmp.name) / 'media' / 'synthetic.wav'
        self.path.write_bytes(b'synthetic-only-no-decoder')
        state = self.core.get_state(self.sid)
        self.answer = self.core.submit_answer(self.sid, state['current_round']['round_id'], key(), key(), '重复。重复。尾句。',
            path=self.path, media_meta={'duration_ms': 5000, 'mime_type': 'audio/wav'}, media_hash=hashlib.sha256(self.path.read_bytes()).hexdigest())
        self.base = self.core.get_answer_timeline(self.sid, self.answer['answer_id'], 1)
        self.segments = [{'segment_id': item['segment_id'], 'start_ms': i * 1000, 'end_ms': i * 1000 + 900}
                         for i, item in enumerate(self.base['segments'])]
        self.request = {'answer_id': self.answer['answer_id'], 'revision': 1, 'segments': self.segments,
                        'reviewer': 'fixture-rater', 'reason': '合成校准，仅测版本和边界', 'idempotency_key': key()}

    def test_append_is_idempotent_immutable_and_keeps_report_version(self):
        before = self.core.get_report(self.sid)
        result = self.store.alignment(self.sid, **self.request)
        self.assertEqual(result, self.store.alignment(self.sid, **self.request))
        self.assertEqual(result['version'], 1)
        self.assertEqual(self.core.get_report(self.sid), before)
        with self.core._db(True) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM alignment_reviews').fetchone()[0], 1)
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute('UPDATE alignment_reviews SET payload=?', ('{}',))

    def test_next_review_is_complete_mapping_snapshot(self):
        first = self.store.alignment(self.sid, **self.request)
        second = self.store.alignment(self.sid, **{**self.request, 'segments': self.segments[1:2], 'idempotency_key': key()})
        self.assertEqual(second['version'], 2)
        self.assertEqual(second['payload']['supersedes_review_id'], first['alignment_review_id'])
        changed = overlay(self.base, second)
        self.assertEqual(changed['alignment']['status'], 'partial')
        self.assertIsNone(changed['segments'][0]['start_ms'])
        self.assertEqual(changed['segments'][1]['start_ms'], 1000)
        self.assertIsNone(changed['segments'][2]['start_ms'])

    def test_overlay_copies_input_and_recalculates_repeated_citations(self):
        review = self.store.alignment(self.sid, **self.request)
        timeline = copy.deepcopy(self.base)
        timeline['references'] = [{'quote': '重复。', 'transcript_revision': 1, 'occurrences': []}]
        changed = overlay(timeline, review)
        self.assertIsNone(timeline['segments'][0]['start_ms'])
        self.assertEqual([o['start_ms'] for o in changed['references'][0]['occurrences']], [0, 1000])
        self.assertEqual(changed['references'][0]['match_status'], 'ambiguous')
        self.assertEqual(changed['segments'][0]['timestamp_source'], 'human_review')

    def test_explicit_quote_position_survives_manual_overlay(self):
        review = self.store.alignment(self.sid, **self.request)
        timeline = copy.deepcopy(self.base)
        second = timeline['segments'][1]
        timeline['references'] = [{'quote': '重复。', 'transcript_revision': 1, 'segment_id': second['segment_id'],
                                    'char_start': second['char_start'], 'char_end': second['char_end']}]
        changed = overlay(timeline, review)
        self.assertEqual(changed['references'][0]['match_status'], 'unique')
        self.assertEqual(changed['references'][0]['occurrences'][0]['start_ms'], 1000)
        timeline['references'][0]['char_start'] = 0
        self.assertEqual(overlay(timeline, review)['references'][0]['match_status'], 'not_found')

    def test_invalid_times_duplicate_unknown_segments_rejected(self):
        variants = [[], [{**self.segments[0], 'start_ms': True}], [{**self.segments[0], 'end_ms': 5001}],
                    [{**self.segments[0], 'start_ms': -1}], [{**self.segments[0], 'end_ms': 0}],
                    [self.segments[0], self.segments[0]], [{**self.segments[0], 'segment_id': 'other-version'}],
                    [self.segments[0], {**self.segments[1], 'start_ms': 800}],
                    [{**self.segments[0], 'start_ms': 2000, 'end_ms': 2900}, self.segments[1]]]
        for mapping in variants:
            with self.subTest(mapping=mapping), self.assertRaises(DomainError):
                self.store.alignment(self.sid, **{**self.request, 'segments': mapping, 'idempotency_key': key()})

    def test_revision_and_media_hash_cannot_be_borrowed(self):
        result = self.store.alignment(self.sid, **self.request)
        for field, value in [('transcript_revision', 2), ('answer_id', 'another'), ('session_id', 'another')]:
            changed = copy.deepcopy(self.base)
            changed[field] = value
            with self.assertRaises(AlignmentError):
                overlay(changed, result)
        changed = copy.deepcopy(self.base)
        changed['media']['media_sha256'] = '0' * 64
        with self.assertRaises(AlignmentError):
            overlay(changed, result)
        other = self.core.create_interview(key())['session_id']
        with self.assertRaises(DomainError):
            self.store.alignment(other, **self.request)

    def test_same_key_different_mapping_is_conflict(self):
        self.store.alignment(self.sid, **self.request)
        with self.assertRaises(DomainError) as caught:
            self.store.alignment(self.sid, **{**self.request, 'segments': self.segments[1:]})
        self.assertEqual(caught.exception.code, 'idempotency_conflict')

    def test_equal_text_new_revision_requires_its_own_segment_ids(self):
        old = self.store.alignment(self.sid, **self.request)
        self.core.revise_transcript(self.sid, self.answer['answer_id'], 1, self.base['text'], '同文核对新版本', key())
        with self.assertRaises(DomainError):
            self.store.alignment(self.sid, **{**self.request, 'revision': 2, 'idempotency_key': key()})
        current = self.core.get_answer_timeline(self.sid, self.answer['answer_id'], 2)
        self.assertIsNone(current['segments'][0]['start_ms'])
        self.assertEqual(overlay(self.base, old)['segments'][0]['start_ms'], 0)

    def test_missing_media_or_missing_audit_refused(self):
        self.path.unlink()
        with self.assertRaises(DomainError):
            self.store.alignment(self.sid, **self.request)
        with self.assertRaises(DomainError):
            self.store.alignment(self.sid, **{**self.request, 'reason': ' '})

    def test_timeline_builder_keeps_v2_disambiguation_fields(self):
        snapshot = {'answer': {'id': 'a', 'session_id': 's', 'question_id': 'q', 'revision': 1, 'current': True, 'path': None},
                    'revision': {'version': 1, 'text': '重复。重复。'}, 'revisions': [{'version': 1, 'text': '重复。重复。'}], 'jobs': [], 'sources': []}
        timeline = build_timeline(snapshot)
        second = timeline['segments'][1]
        evidence = {'answer_id': 'a', 'transcript_revision': 1, 'quote': '重复。', 'segment_id': second['segment_id'], 'char_start': 3, 'char_end': 6}
        snapshot['sources'] = [{'source': 'model_rating', 'source_id': 'e', 'reason': 'fixture', 'evidence': [evidence]}]
        reference = build_timeline(snapshot)['references'][0]
        self.assertEqual(reference['char_start'], 3)
        self.assertEqual(reference['segment_id'], second['segment_id'])
        self.assertEqual(len(reference['occurrences']), 1)


if __name__ == '__main__':
    unittest.main()
