"""Synthetic video quality and role-bound APIs; no biometric inference or cloud calls."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import cv2
import numpy as np
from interview_core import InterviewCore
import interview_visual as visual
from test_interview_workbench import ASGIClient


def key():
    return str(uuid.uuid4())


def make_video(path):
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*'mp4v'), 5, (128, 96))
    if not writer.isOpened():
        raise RuntimeError('Synthetic local video encoder unavailable')
    try:
        for index in range(20):
            frame = np.full((96, 128, 3), 40 + index * 5, dtype=np.uint8)
            frame[:, 64:] = 220
            writer.write(frame)
    finally:
        writer.release()


class VisualQualityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.video = self.folder / 'synthetic.mp4'
        make_video(self.video)

    def test_real_video_measurements_bounded_sampling_jpeg_and_cache(self):
        before = self.video.read_bytes()
        with patch('requests.post') as network, patch('interview_media._load_model') as asr:
            result = visual.visual_quality(self.video, self.folder)
        network.assert_not_called()
        asr.assert_not_called()
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['analysis_scope'], 'capture_quality_only')
        self.assertEqual(result['sampling']['requested_frame_count'], 2)
        self.assertEqual(result['summary']['sampled_frame_count'], 2)
        self.assertEqual(result['detector']['status'], 'available')
        for frame in result['frames']:
            self.assertTrue(0 <= frame['brightness_mean'] <= 255)
            self.assertGreaterEqual(frame['laplacian_variance'], 0)
            self.assertLessEqual(max(frame['width'], frame['height']), 640)
            self.assertTrue(0 <= frame['timestamp_ms'] < result['video']['duration_ms'])
            decoded = cv2.imdecode(np.frombuffer(visual.visual_frame(self.video, self.folder, frame['index']).read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertEqual((decoded.shape[1], decoded.shape[0]), (frame['width'], frame['height']))
        with patch.object(visual, '_build', side_effect=AssertionError('cached analysis must not rerun')):
            self.assertEqual(visual.visual_quality(self.video, self.folder), result)
        self.assertEqual(self.video.read_bytes(), before)
        self.assertNotIn(str(self.video), json.dumps(result))
        self.assertNotIn('media_hash', result)

    def test_audio_only_returns_no_video_and_null_metrics_without_detector(self):
        audio = self.folder / 'audio.wav'
        with wave.open(str(audio), 'wb') as stream:
            stream.setnchannels(1)
            stream.setsampwidth(2)
            stream.setframerate(16000)
            stream.writeframes(b'\0\0' * 16000)
        with patch('cv2.VideoCapture') as capture, patch.object(visual, '_load_detector') as detector:
            result = visual.visual_quality(audio, self.folder)
        capture.assert_not_called()
        detector.assert_not_called()
        self.assertEqual(result['status'], 'no_video')
        self.assertEqual(result['frames'], [])
        self.assertIsNone(result['summary']['mean_brightness'])
        self.assertIsNone(result['summary']['frames_with_face'])
        self.assertEqual(result['detector']['status'], 'not_run')

    def test_missing_detector_keeps_quality_but_does_not_fabricate_zero_faces(self):
        with patch.object(visual, '_load_detector', return_value=(None, 'synthetic missing detector')):
            result = visual.visual_quality(self.video, self.folder)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['detector']['status'], 'unavailable')
        self.assertIsNone(result['summary']['frames_with_face'])
        self.assertIsNotNone(result['summary']['mean_brightness'])
        self.assertTrue(all(f['face_count'] is None and f['face_boxes'] == [] for f in result['frames']))

    def test_detector_failure_uses_no_mixed_denominator_and_releases_capture(self):
        detector = Mock()
        detector.detect.side_effect = [(1, np.array([[10, 11, 30, 31, *([999] * 10), 0.99]])), RuntimeError('synthetic')]
        with patch.object(visual, '_load_detector', return_value=(detector, None)):
            result = visual.visual_quality(self.video, self.folder)
        self.assertEqual(result['status'], 'partial')
        self.assertIsNone(result['summary']['frames_with_face'])
        self.assertTrue(all(f['face_count'] is None for f in result['frames']))

    def test_zero_detections_are_observed_counts_not_missing_video(self):
        detector = Mock()
        detector.detect.return_value = (1, None)
        with patch.object(visual, '_load_detector', return_value=(detector, None)):
            result = visual.visual_quality(self.video, self.folder)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['summary']['frames_with_face'], 0)
        self.assertEqual(len(result['frames']), 2)
        self.assertTrue(any('未检出不代表' in message for message in result['notices']))

    def test_corrupt_and_decode_failures_are_explicit_and_release_resources(self):
        bad = self.folder / 'corrupt.mp4'
        bad.write_bytes(b'not a valid video')
        self.assertEqual(visual.visual_quality(bad, self.folder)['status'], 'failed')
        capture = Mock()
        capture.isOpened.return_value = True
        capture.get.side_effect = lambda prop: 128 if prop == cv2.CAP_PROP_FRAME_WIDTH else 96
        capture.grab.return_value = False
        with patch('cv2.VideoCapture', return_value=capture):
            result = visual.visual_quality(self.video, self.folder)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['frames'], [])
        self.assertIsNone(result['summary']['mean_laplacian_variance'])
        capture.release.assert_called_once()

    def test_sample_cap_source_resolution_and_frame_index_boundaries(self):
        for duration in (1, 2999, 3000, 3001, 179999, 180000):
            interval, times = visual.sample_times(duration)
            self.assertLessEqual(len(times), 60)
            self.assertGreaterEqual(interval, 3000)
            self.assertTrue(all(0 <= t < duration for t in times))
        for duration in (0, -1, 180001, float('nan')):
            with self.assertRaises(visual.VisualError):
                visual.sample_times(duration)
        capture = Mock()
        capture.isOpened.return_value = True
        capture.get.return_value = 9000
        with patch('cv2.VideoCapture', return_value=capture):
            result = visual.visual_quality(self.video, self.folder)
        self.assertEqual(result['status'], 'failed')
        capture.grab.assert_not_called()
        capture.release.assert_called_once()
        for index in (-1, 60, '../quality.json', True):
            with self.assertRaises(visual.VisualError):
                visual._frame_path(self.folder, index)

    def test_changed_source_uses_new_hash_and_frame_get_does_not_start_analysis(self):
        visual.visual_quality(self.video, self.folder)
        old_hash = visual._media_digest(self.video)
        self.video.write_bytes(b'changed synthetic source')
        self.assertNotEqual(old_hash, visual._media_digest(self.video))
        with patch.object(visual, '_build') as build:
            with self.assertRaises(visual.VisualError):
                visual.visual_frame(self.video, self.folder, 0)
        build.assert_not_called()

    def test_sequential_sampling_labels_missing_timestamps_as_approximate(self):
        capture = Mock()
        capture.isOpened.return_value = True
        values = {cv2.CAP_PROP_FRAME_WIDTH: 128, cv2.CAP_PROP_FRAME_HEIGHT: 96, cv2.CAP_PROP_FPS: 4, cv2.CAP_PROP_POS_MSEC: 0}
        capture.get.side_effect = lambda prop: values[prop]
        capture.grab.return_value = True
        capture.retrieve.return_value = (True, np.zeros((96, 128, 3), dtype=np.uint8))
        detector = Mock()
        detector.detect.return_value = (1, None)
        with patch('cv2.VideoCapture', return_value=capture), patch.object(visual, '_load_detector', return_value=(detector, None)):
            result = visual.visual_quality(self.video, self.folder)
        self.assertEqual(result['status'], 'succeeded')
        self.assertEqual(result['frames'][1]['timestamp_source'], 'frame_rate_approximate')
        self.assertEqual(result['frames'][1]['timestamp_ms'], 3000)
        self.assertEqual(capture.grab.call_count, 13)
        self.assertEqual(capture.retrieve.call_count, 2)
        capture.set.assert_not_called()
        capture.release.assert_called_once()

    def test_soft_deadline_is_checked_during_unsampled_frame_decode(self):
        capture = Mock()
        capture.isOpened.return_value = True
        values = {cv2.CAP_PROP_FRAME_WIDTH: 128, cv2.CAP_PROP_FRAME_HEIGHT: 96, cv2.CAP_PROP_FPS: 30, cv2.CAP_PROP_POS_MSEC: 0}
        capture.get.side_effect = lambda prop: values[prop]
        capture.grab.return_value = True
        capture.retrieve.return_value = (True, np.zeros((96, 128, 3), dtype=np.uint8))
        detector = Mock()
        detector.detect.return_value = (1, None)
        # First sample succeeds; the unsampled decode loop reaches the deadline.
        times = iter((0, 0, 0, 0, 1, 26))
        with patch('cv2.VideoCapture', return_value=capture), \
             patch.object(visual, '_load_detector', return_value=(detector, None)), \
             patch.object(visual.time, 'monotonic', side_effect=lambda: next(times)):
            result = visual.visual_quality(self.video, self.folder)
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(len(result['frames']), 1)
        self.assertIn('时限', result['reason'])
        capture.release.assert_called_once()

    def test_yunet_missing_or_changed_weights_are_rejected_without_fallback(self):
        fake_cv = Mock()
        fake_path = self.folder / 'missing.onnx'
        with patch.object(visual, 'MODEL_PATH', fake_path):
            detector, reason = visual._load_detector(fake_cv)
            self.assertIsNone(detector)
            self.assertIn('不可用', reason)
            fake_path.write_bytes(b'not the approved ONNX weights')
            detector, reason = visual._load_detector(fake_cv)
            self.assertIsNone(detector)
            self.assertIn('校验不匹配', reason)
        fake_cv.FaceDetectorYN.create.assert_not_called()
        fake_cv.CascadeClassifier.assert_not_called()

    def test_yunet_output_keeps_only_bounded_rectangles_discards_landmarks_and_score(self):
        detector = Mock()
        detector.detect.return_value = (1, np.array([
            [-2.5, 10.2, 34.0, 120.0, *([900001] * 10), 0.95],
            [1, 1, 20, 20, *([900001] * 10), 0.1],
            [1, 1, float('nan'), 20, *([900001] * 10), 0.99],
        ]))
        frame = np.zeros((96, 128, 3), dtype=np.uint8)
        boxes = visual._detect_boxes(detector, frame)
        self.assertEqual(boxes, [{'x': 0, 'y': 10, 'width': 32, 'height': 86}])
        detector.setInputSize.assert_called_once_with((128, 96))
        self.assertNotIn('900001', json.dumps(boxes))
        self.assertNotIn('score', json.dumps(boxes))


class VisualAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        env = patch.dict(os.environ, {'INTERVIEW_DATA_DIR': self.temp.name,
                                     'INTERVIEW_CREDENTIALS_FILE': str(self.folder / 'absent'), 'DASHSCOPE_API_KEY': ''})
        env.start()
        self.addCleanup(env.stop)
        self.core = InterviewCore(self.folder)
        self.entry = importlib.import_module('interview_entry')
        core_patch = patch.object(self.entry, 'core', self.core)
        core_patch.start()
        self.addCleanup(core_patch.stop)
        self.client = ASGIClient(self.entry.app)
        self.created = self.core.create_interview(key(), 'synthetic-visual')
        self.other = self.core.create_interview(key(), 'synthetic-other')
        self.sid = self.created['session_id']
        path = self.folder / 'media' / 'synthetic.mp4'
        make_video(path)
        self.answer = self.core.submit_answer(self.sid, self.created['current_round']['round_id'], key(), key(),
                                             '合成权限测试文字。', path=path,
                                             media_meta={'mime_type': 'video/mp4', 'duration_ms': 4000},
                                             media_hash=hashlib.sha256(path.read_bytes()).hexdigest())
        self.url = f'/v1/interviews/{self.sid}/visual/{self.answer["answer_id"]}'

    def headers(self, role='hr', created=None):
        return {'Authorization': 'Bearer ' + (created or self.created)[role + '_token']}

    def test_visual_and_frames_require_this_session_hr_token(self):
        for suffix in ('', '/frames/0'):
            self.assertEqual(self.client.get(self.url + suffix).status_code, 401)
            self.assertEqual(self.client.get(self.url + suffix, headers=self.headers('candidate')).status_code, 403)
            self.assertEqual(self.client.get(self.url + suffix, headers=self.headers(created=self.other)).status_code, 403)
        self.assertFalse((self.folder / 'visual-cache').exists())
        other_url = f'/v1/interviews/{self.other["session_id"]}/visual/{self.answer["answer_id"]}'
        self.assertEqual(self.client.get(other_url, headers=self.headers(created=self.other)).status_code, 404)

    def test_hr_read_returns_frames_no_store_and_changes_no_business_records(self):
        report = self.core.get_report(self.sid)
        with self.core._db() as db:
            before = {name: [tuple(r) for r in db.execute(f'SELECT * FROM {name}')] for name in ('sessions', 'jobs', 'reports', 'decisions', 'decision_events', 'model_evaluations')}
        self.assertEqual(self.client.get(self.url + '/frames/0', headers=self.headers()).status_code, 404)
        with patch('requests.post') as network, patch('interview_media._load_model') as asr:
            response = self.client.get(self.url, headers=self.headers())
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data['answer_id'], self.answer['answer_id'])
            self.assertEqual(data['status'], 'succeeded')
            frame = self.client.get(data['frames'][0]['frame_url'], headers=self.headers())
        self.assertEqual(frame.status_code, 200)
        self.assertEqual(frame.headers['content-type'], 'image/jpeg')
        self.assertEqual(frame.headers['cache-control'], 'no-store')
        self.assertTrue(frame.content.startswith(b'\xff\xd8'))
        self.assertEqual(self.client.get(self.url + '/frames/-1', headers=self.headers()).status_code, 404)
        self.assertEqual(self.client.get(self.url + '/frames/60', headers=self.headers()).status_code, 404)
        network.assert_not_called()
        asr.assert_not_called()
        with self.core._db() as db:
            after = {name: [tuple(r) for r in db.execute(f'SELECT * FROM {name}')] for name in before}
        self.assertEqual(after, before)
        self.assertEqual(self.core.get_report(self.sid), report)
        self.assertNotIn('capture_quality_only', json.dumps(report))
        self.assertNotIn('frames_with_face', json.dumps(report))


if __name__ == '__main__':
    unittest.main()
