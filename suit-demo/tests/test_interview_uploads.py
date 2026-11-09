"""Risk tests for persisted chunks, fencing, integrity and atomic answer submission."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_core import InterviewCore, DomainError
from interview_uploads import UploadStore, MIN_CHUNK


def key():
    return str(uuid.uuid4())


class UploadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {'INTERVIEW_DATA_DIR': self.tmp.name, 'DASHSCOPE_API_KEY': '',
                                           'INTERVIEW_CREDENTIALS_FILE': str(Path(self.tmp.name) / 'missing')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.core = InterviewCore(self.tmp.name)
        self.store = UploadStore(self.core)
        self.session = self.core.create_interview(key(), 'synthetic-upload-risk')
        self.sid = self.session['session_id']
        self.data = b'bounded synthetic media' * 4000
        self.request = {'round_id': self.session['current_round']['round_id'], 'attempt_id': key(),
            'filename': 'answer.webm', 'total_size': len(self.data), 'chunk_size': MIN_CHUNK,
            'sha256': hashlib.sha256(self.data).hexdigest(), 'idempotency_key': key()}
        self.upload = self.store.create(self.sid, **self.request)
        self.upload_id = self.upload['upload_id']
        self.metadata = {'has_audio': True, 'has_video': True, 'duration_ms': 4000, 'mime_type': 'video/webm'}

    def chunks(self):
        for index, start in enumerate(range(0, len(self.data), MIN_CHUNK)):
            yield index, self.data[start:start + MIN_CHUNK]

    def put_all(self):
        for index, chunk in self.chunks():
            self.store.put_chunk(self.sid, self.upload_id, index, chunk, hashlib.sha256(chunk).hexdigest())

    def test_restart_missing_only_and_duplicate_chunks(self):
        index, chunk = next(self.chunks())
        self.store.put_chunk(self.sid, self.upload_id, index, chunk, hashlib.sha256(chunk).hexdigest())
        restarted = UploadStore(InterviewCore(self.tmp.name))
        state = restarted.create(self.sid, **self.request)
        self.assertEqual(state['received_chunks'], [0])
        with ThreadPoolExecutor(max_workers=2) as pool:
            values = list(pool.map(lambda _: restarted.put_chunk(self.sid, self.upload_id, index, chunk, hashlib.sha256(chunk).hexdigest()), range(2)))
        self.assertEqual(values[0]['received_bytes'], len(chunk))
        self.assertEqual(values[1]['received_bytes'], len(chunk))
        changed = b'x' * len(chunk)
        with self.assertRaisesRegex(DomainError, '不同内容'):
            restarted.put_chunk(self.sid, self.upload_id, index, changed, hashlib.sha256(changed).hexdigest())

    def test_incomplete_then_complete_exactly_once(self):
        completion = {'transcript': '合成回答证据', 'idempotency_key': key()}
        with self.assertRaises(DomainError):
            self.store.complete(self.sid, self.upload_id, **completion)
        self.assertEqual(self.core.get_state(self.sid)['answers'], [])
        self.put_all()
        with patch('interview_media.inspect_media', return_value=self.metadata):
            result = self.store.complete(self.sid, self.upload_id, **completion)
            self.assertEqual(self.store.complete(self.sid, self.upload_id, **completion), result)
        self.assertEqual(len(self.core.get_state(self.sid)['answers']), 1)
        path, _ = self.core.media_path(self.sid, result['answer_id'])
        self.assertEqual(path.read_bytes(), self.data)
        self.assertEqual(self.store.status(self.sid, self.upload_id)['status'], 'completed')
        with self.assertRaises(DomainError):
            self.store.complete(self.sid, self.upload_id, **{**completion, 'transcript': 'different'})

    def test_whole_hash_and_decoder_reject_without_answer(self):
        with self.assertRaises(DomainError):
            self.store.create(self.sid, **{**self.request, 'sha256': '0' * 64, 'idempotency_key': key()})
        self.put_all()
        with patch('interview_media.inspect_media', side_effect=ValueError('not a container')):
            with self.assertRaisesRegex(DomainError, '解码检查'):
                self.store.complete(self.sid, self.upload_id, idempotency_key=key())
        self.assertFalse(self.core.get_state(self.sid)['answers'])
        self.assertFalse(list((self.core.data_dir / 'media').iterdir()))

    def test_foreign_session_and_chunk_bounds(self):
        other = self.core.create_interview(key())['session_id']
        with self.assertRaises(DomainError):
            self.store.status(other, self.upload_id)
        index, chunk = next(self.chunks())
        with self.assertRaises(DomainError):
            self.store.put_chunk(self.sid, self.upload_id, 99, chunk, hashlib.sha256(chunk).hexdigest())
        with self.assertRaises(DomainError):
            self.store.put_chunk(self.sid, self.upload_id, index, chunk, '0' * 64)

    def test_missing_or_corrupt_chunk_can_be_resumed(self):
        self.put_all()
        part = self.store.root / self.upload_id / '0.part'
        part.unlink()
        self.assertNotIn(0, self.store.status(self.sid, self.upload_id)['received_chunks'])
        self.put_all()
        part.write_bytes(b'X' * MIN_CHUNK)
        complete_key = key()
        with self.assertRaisesRegex(DomainError, '校验失败'):
            self.store.complete(self.sid, self.upload_id, idempotency_key=complete_key)
        self.assertNotIn(0, self.store.status(self.sid, self.upload_id)['received_chunks'])
        self.put_all()
        with patch('interview_media.inspect_media', return_value=self.metadata):
            result = self.store.complete(self.sid, self.upload_id, idempotency_key=complete_key)
        self.assertTrue(result['answer_id'])

    def test_full_hash_mismatch_never_commits(self):
        request = {**self.request, 'attempt_id': key(), 'idempotency_key': key(), 'sha256': '0' * 64}
        other = self.store.create(self.sid, **request)
        for index, chunk in self.chunks():
            self.store.put_chunk(self.sid, other['upload_id'], index, chunk, hashlib.sha256(chunk).hexdigest())
        with self.assertRaisesRegex(DomainError, '完整媒体摘要'):
            self.store.complete(self.sid, other['upload_id'], idempotency_key=key())
        self.assertFalse(self.core.get_state(self.sid)['answers'])

    def test_concurrent_complete_returns_one_answer_and_one_media(self):
        self.put_all()
        completion = {'transcript': '并发完成fixture', 'idempotency_key': key()}
        with patch('interview_media.inspect_media', return_value=self.metadata), ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.store.complete(self.sid, self.upload_id, **completion), range(2)))
        self.assertEqual(results[0]['answer_id'], results[1]['answer_id'])
        self.assertEqual(len(self.core.get_state(self.sid)['answers']), 1)
        self.assertEqual(len(list((self.core.data_dir / 'media').iterdir())), 1)

    def test_crash_after_answer_commit_recovers_original(self):
        self.put_all()
        completion = {'transcript': '崩溃恢复fixture', 'idempotency_key': key()}
        with patch('interview_media.inspect_media', return_value=self.metadata):
            result = self.store.complete(self.sid, self.upload_id, **completion)
            # Simulate crash between answer commit and recording completion.
            with self.core._db(True) as db:
                db.execute("UPDATE media_uploads SET status='uploading',result=NULL WHERE id=?", (self.upload_id,))
            recovered = self.store.complete(self.sid, self.upload_id, **completion)
        self.assertEqual(recovered['answer_id'], result['answer_id'])
        self.assertEqual(len(list((self.core.data_dir / 'media').iterdir())), 1)

    def test_api_requires_candidate_and_bounds_stream(self):
        from tests.test_interview_workbench import ASGIClient
        entry = importlib.import_module('interview_entry')
        with patch.object(entry, 'core', self.core):
            client = ASGIClient(entry.app)
            base = f'/v1/interviews/{self.sid}/uploads/{self.upload_id}'
            hr = {'Authorization': 'Bearer ' + self.session['hr_token']}
            candidate = {'Authorization': 'Bearer ' + self.session['candidate_token']}
            self.assertEqual(client.get(base, headers=hr).status_code, 403)
            self.assertEqual(client.get(base, headers=candidate).status_code, 200)
            index, chunk = next(self.chunks())
            response = client.put(base + f'/chunks/{index}', content=chunk,
                                  headers={**candidate, 'X-Chunk-SHA256': hashlib.sha256(chunk).hexdigest()})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['received_chunks'], [0])


if __name__ == '__main__':
    unittest.main()
