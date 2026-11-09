import csv
import importlib.util
import json
import subprocess
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_multimodal import (AU_INTENSITY, AU_PRESENCE, analyze_audio,
    intervals_from_flags, parse_openface_csv, run_multimodal, speech_units, _openface,
    _camera_for_frame, _expression_and_quality, MAX_GEOMETRY_OBSERVATIONS)


class MultimodalTests(unittest.TestCase):
    def _csv(self, directory, mutate=None):
        row = {'frame': 1, 'timestamp': '0', 'success': '1', 'confidence': '.95'}
        row.update({f'AU{i:02d}_r': '1.25' for i in AU_INTENSITY})
        row.update({f'AU{i:02d}_c': '1' for i in AU_PRESENCE})
        row.update({k: '.1' for k in ['gaze_angle_x', 'gaze_angle_y', 'gaze_0_x', 'gaze_0_y',
                                     'gaze_0_z', 'gaze_1_x', 'gaze_1_y', 'gaze_1_z']})
        row.update(gaze_0_z='-.989949', gaze_1_z='-.989949')
        if mutate:
            mutate(row)
        path = Path(directory) / 'fixture.csv'
        with path.open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=row.keys())
            writer.writeheader()
            writer.writerow(row)
        return path

    def _geometry_csv(self, directory, mutate=None, count=1):
        def add_geometry(row):
            # Synthetic contract fixture, never an inference proof or UI demo.
            row.update({f'x_{i}': str(180 + i * 3) for i in range(68)})
            row.update({f'y_{i}': str(150 + i * 2) for i in range(68)})
            for offset, x in ((0, -30), (28, 30)):
                for i in range(offset, offset + 8):
                    row.update({f'eye_lmk_X_{i}': str(x), f'eye_lmk_Y_{i}': '-15',
                                f'eye_lmk_Z_{i}': '600'})
            if mutate:
                mutate(row)
        path = self._csv(directory, add_geometry)
        if count > 1:
            with path.open(newline='', encoding='utf-8') as stream:
                row = next(csv.DictReader(stream))
            with path.open('w', newline='', encoding='utf-8') as stream:
                writer = csv.DictWriter(stream, fieldnames=row.keys())
                writer.writeheader()
                for index in range(count):
                    writer.writerow({**row, 'frame': index + 1, 'timestamp': index / 5})
        return path

    def test_geometry_is_normalized_from_real_columns_and_camera_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            data = parse_openface_csv(self._geometry_csv(directory), 1000, frame_size=(640, 480),
                                      camera_intrinsics=_camera_for_frame(640, 480))
        geometry = data['geometry']['observations'][0]
        self.assertEqual(len(geometry['landmarks']), 68)
        self.assertEqual(geometry['landmarks'][0], [0.28125, 0.3125])
        ray = geometry['gaze_arrows'][0]
        self.assertEqual(ray['eye'], 0)
        self.assertAlmostEqual(ray['origin'][0], (500 * -30 / 600 + 320) / 640, delta=0.000001)
        self.assertAlmostEqual(ray['tip'][0], (500 * (-30 + .1 * 50) / (600 - .989949 * 50) + 320) / 640, delta=0.000001)
        self.assertEqual(geometry['end_ms'], 200)
        self.assertIn('not camera/screen calibrated', data['geometry']['camera_intrinsics']['calibration'])

    def test_geometry_missing_track_or_multiple_face_never_draws_points(self):
        with tempfile.TemporaryDirectory() as directory:
            data = parse_openface_csv(self._geometry_csv(directory), 1000,
                {0: {'face_count': 2, 'reason': 'multiple faces'}}, frame_size=(640, 480))
        geometry = data['geometry']['observations'][0]
        self.assertEqual(geometry['quality'], 'missing')
        self.assertEqual(geometry['landmarks'], [])
        self.assertEqual(geometry['gaze_arrows'], [])

    def test_bad_landmark_does_not_get_clamped_or_fabricated(self):
        with tempfile.TemporaryDirectory() as directory:
            data = parse_openface_csv(self._geometry_csv(directory, lambda row: row.update(x_4='nan')),
                                      1000, frame_size=(640, 480))
        self.assertEqual(data['geometry']['observations'][0]['landmarks'], [])
        # Valid AU values remain independently usable.
        self.assertEqual(data['au'][0]['quality'], 'valid')

    def test_missing_eye_geometry_retains_face_points_without_fake_ray(self):
        with tempfile.TemporaryDirectory() as directory:
            data = parse_openface_csv(self._geometry_csv(directory, lambda row: row.update(eye_lmk_Z_0='-99999', eye_lmk_Z_28='nan')),
                1000, frame_size=(640, 480), camera_intrinsics=_camera_for_frame(640, 480))
        geometry = data['geometry']['observations'][0]
        self.assertEqual(len(geometry['landmarks']), 68)
        self.assertEqual(geometry['gaze_arrows'], [])
        self.assertTrue(geometry['gaze_reason'])

    def test_geometry_payload_is_bounded_without_filling_unsampled_gaps(self):
        with tempfile.TemporaryDirectory() as directory:
            data = parse_openface_csv(self._geometry_csv(directory, count=900), 180000,
                frame_size=(640, 480), camera_intrinsics=_camera_for_frame(640, 480))
        self.assertEqual(len(data['au']), 900)
        self.assertLessEqual(len(data['geometry']['observations']), MAX_GEOMETRY_OBSERVATIONS)
        self.assertEqual(data['geometry']['sample_stride'], 5)
        first, second = data['geometry']['observations'][:2]
        self.assertEqual(first['end_ms'], 200)
        self.assertEqual(second['start_ms'], 1000)
        self.assertLess(first['end_ms'], second['start_ms'])

    @unittest.skipUnless(importlib.util.find_spec('cv2'), 'OpenCV unavailable')
    def test_repeated_identical_frames_and_multiface_interrupt_track(self):
        import numpy as np
        face = np.array([[50, 50, 80, 80, 70, 70, 105, 70, 90, 90, 75, 110, 100, 110, .99]], dtype=np.float32)
        frames = [np.full((180, 240, 3), 100, dtype=np.uint8) for _ in range(12)]
        frames[0].fill(98)
        frames[1].fill(99)
        state = {'index': -1}
        def read():
            state['index'] += 1
            return (True, frames[state['index']]) if state['index'] < len(frames) else (False, None)
        def detect(_):
            return None, np.vstack([face, face]) if state['index'] == 1 else face
        capture = SimpleNamespace(read=read, release=lambda: None)
        detector = SimpleNamespace(setInputSize=lambda _: None, detect=detect)
        with patch('cv2.VideoCapture', return_value=capture), \
             patch('interview_visual._load_detector', return_value=(detector, None)):
            _, quality = _expression_and_quality('synthetic-fixture.avi', 2400, False)
        self.assertIsNone(quality[1]['track_segment_id'])
        self.assertNotEqual(quality[0]['track_segment_id'], quality[2]['track_segment_id'])
        self.assertIn('repeated_identical_frames', quality[11]['quality_flags'])
        self.assertIsNotNone(quality[11]['reason'])
        self.assertIsNotNone(quality[2]['reason'])
        self.assertEqual(quality[2]['quality_last_input_ms'], 2400)

    @unittest.skipUnless(importlib.util.find_spec('webrtcvad'), 'local VAD unavailable')
    def test_clipped_window_keeps_energy_but_hides_unreliable_pitch(self):
        import numpy as np
        with patch('librosa.pyin', return_value=(np.array([120.] * 10), np.array([True] * 10), np.array([1.] * 10))):
            data = analyze_audio(np.full(16000, 32767, dtype='<i2').tobytes(), {}, 1000)
        window = data['observations'][0]
        self.assertIn('audio_clipping', window['quality_flags'])
        self.assertEqual(window['values']['clipped_sample_fraction'], 1)
        self.assertIsNone(window['values']['f0_hz'])
        self.assertAlmostEqual(window['values']['rms_dbfs'], 0, places=2)
        self.assertEqual(data['summary']['f0_status'], 'no_valid_pitch')
        self.assertTrue(data['summary']['f0_reason'])

    def test_documented_au_units_and_offline_context(self):
        with tempfile.TemporaryDirectory() as directory:
            data = parse_openface_csv(self._csv(directory), 1000, {0: {'face_count': 1, 'reason': None}})
        self.assertEqual(data['au'][0]['values']['intensity']['AU12'], 1.25)
        self.assertEqual(data['au'][0]['last_input_ms'], 1000)
        self.assertEqual(data['gaze'][0]['last_input_ms'], 200)

    def test_failed_track_is_missing_not_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            data = parse_openface_csv(self._csv(directory, lambda row: row.update(success='0')), 1000)
        self.assertEqual(data['au'][0]['quality'], 'missing')
        self.assertEqual(data['au'][0]['values'], {})
        self.assertEqual(data['gaze'][0]['values'], {})

    def test_multiple_faces_not_arbitrarily_attributed(self):
        with tempfile.TemporaryDirectory() as directory:
            data = parse_openface_csv(self._csv(directory), 1000, {0: {'face_count': 2, 'reason': 'multiple'}})
        self.assertEqual(data['au'][0]['quality'], 'missing')

    def test_nan_and_out_of_range_are_not_measurements(self):
        with tempfile.TemporaryDirectory() as directory:
            data = parse_openface_csv(self._csv(directory, lambda row: row.update(AU01_r='nan', gaze_angle_x='10')), 1000)
        self.assertEqual(data['au'][0]['values'], {})
        self.assertEqual(data['gaze'][0]['values'], {})

    def test_out_of_media_times_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                parse_openface_csv(self._csv(directory, lambda row: row.update(timestamp='2')), 1000)

    def test_fractional_presence_and_invalid_direction_are_missing(self):
        with tempfile.TemporaryDirectory() as directory:
            data = parse_openface_csv(self._csv(directory, lambda row: row.update(AU01_c='.5', gaze_0_z='0')), 1000)
        self.assertEqual(data['au'][0]['values'], {})
        self.assertEqual(data['gaze'][0]['values'], {})

    def test_text_only_records_missing_without_fake_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            one = run_multimodal(None, {'answer_id': 'a', 'transcript_revision': 3}, directory)
            two = run_multimodal(None, {'answer_id': 'a', 'transcript_revision': 3}, directory)
            self.assertNotEqual(one['run_id'], two['run_id'])
            self.assertEqual(one['transcript_revision'], 3)
            self.assertTrue(all(v['status'] == 'no_media' and v['observations'] == [] for v in one['modules'].values()))
            self.assertFalse(one['result_cache_reused'])
            self.assertTrue((Path(directory) / one['artifact']).is_file())

    def test_media_hash_mismatch_refuses_analysis(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'test.wav'
            source.write_bytes(b'changed')
            with patch('interview_multimodal.inspect_media', return_value={'has_audio': True, 'has_video': False, 'duration_ms': 1000}):
                with self.assertRaises(ValueError):
                    run_multimodal(source, {'media_sha256': '0' * 64}, directory)

    def test_unsupported_module_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                run_multimodal(None, {}, directory, ['honesty'])

    def test_interval_logic_keeps_leading_and_trailing_silence(self):
        flags = [False, False, True, True, False, True, False]
        self.assertEqual(intervals_from_flags(flags, 20, 140), [{'start_ms': 40, 'end_ms': 80}, {'start_ms': 100, 'end_ms': 120}])
        self.assertEqual(intervals_from_flags(flags, 20, 140, False)[-1], {'start_ms': 120, 'end_ms': 140})

    def test_rate_unit_definition(self):
        self.assertEqual(speech_units('我完成了 three tasks，2026年。😊'), 8)

    def test_openface_timeout_audits_phase_without_paths_or_raw_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest = {'source': 'https://example.test/official-fixture', 'archive_sha256': 'fixture'}
            with patch('interview_multimodal._verified_openface', return_value=(root, root / 'test.exe', manifest)), \
                 patch('interview_multimodal.subprocess.run', side_effect=subprocess.TimeoutExpired('PRIVATE_PATH_OR_COMMAND', 240)):
                data = _openface(root / 'sample.avi', root, 1000, ['au', 'gaze'], {})
        for module in data.values():
            self.assertEqual(module['status'], 'unavailable')
            self.assertEqual(module['observations'], [])
            execution = module['provenance']['execution']
            self.assertEqual(execution['phase'], 'process_execution')
            self.assertEqual(execution['outcome'], 'timeout')
            self.assertEqual(execution['error_type'], 'TimeoutExpired')
            self.assertFalse(execution['automatic_retry'])
            self.assertGreaterEqual(execution['elapsed_seconds'], 0)
            self.assertNotIn('PRIVATE_PATH', json.dumps(module))

    def test_openface_asset_failure_is_distinct_from_timeout(self):
        with patch('interview_multimodal._verified_openface', side_effect=ValueError('PRIVATE_PATH')):
            module = _openface(Path('sample'), Path('.'), 1000, ['au'], {})['au']
        self.assertEqual(module['provenance']['execution']['outcome'], 'asset_verification_failed')
        self.assertNotIn('PRIVATE_PATH', json.dumps(module))

    def test_openface_unavailable_does_not_discard_other_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'synthetic.mp4'
            source.write_bytes(b'synthetic mock; not real media')
            audio = {'status': 'complete', 'observations': [], 'summary': {'fixture': True}}
            expression = {'status': 'complete', 'observations': [], 'summary': {'fixture': True}}
            failed = {name: {'status': 'unavailable', 'reason': 'synthetic timeout', 'observations': []} for name in ['au', 'gaze']}
            with patch('interview_multimodal.inspect_media', return_value={'has_audio': True, 'has_video': True, 'duration_ms': 1000}), \
                 patch('interview_multimodal._run', return_value=b''), \
                 patch('interview_multimodal.analyze_audio', return_value=audio), \
                 patch('interview_multimodal._expression_and_quality', return_value=(expression, {})), \
                 patch('interview_multimodal._openface', return_value=failed):
                result = run_multimodal(source, {}, Path(directory) / 'output')
        self.assertEqual(result['status'], 'partial')
        self.assertEqual(result['modules']['audio']['summary'], {'fixture': True})
        self.assertEqual(result['modules']['expression']['summary'], {'fixture': True})
        self.assertEqual(result['modules']['au']['status'], 'unavailable')

    @unittest.skipUnless(importlib.util.find_spec('webrtcvad'), 'local VAD dependency unavailable')
    def test_silence_has_no_pitch_and_no_invented_rate(self):
        data = analyze_audio(b'\x00\x00' * 16000, {'transcript': '有人写了字'}, 1000)
        self.assertEqual(data['summary']['speech_ms'], 0)
        self.assertEqual(data['summary']['pause_count'], 0)
        self.assertIsNone(data['summary']['speech_rate_units_per_min'])
        self.assertIsNone(data['observations'][0]['values']['f0_hz'])
        self.assertEqual(data['observations'][0]['values']['rms_dbfs'], -160.0)


if __name__ == '__main__':
    unittest.main()
