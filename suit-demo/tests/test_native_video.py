import json
from fractions import Fraction
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_native_video import NativeVideoSampler
from interview_multimodal import _model_failure, run_multimodal


class Frame:
    def __init__(self, at):
        self.pts = at
        self.time_base = Fraction(1, 1000)

    def to_ndarray(self, format):
        return ('native-frame', self.pts)


class NativeVideoTests(unittest.TestCase):
    def fixture(self, times, rate=1000, origin=None):
        stream = SimpleNamespace(base_rate=rate, average_rate=None)
        container = SimpleNamespace(streams=SimpleNamespace(video=[stream]), start_time=origin,
                                    decode=lambda _: iter(Frame(at) for at in times), close=lambda: None)
        with patch('av.open', return_value=container):
            return NativeVideoSampler('fixture.webm')

    def test_fake_1000_fps_and_absent_average_use_real_pts(self):
        sampler = self.fixture(range(0, 1101, 33))
        samples = sampler.sample([125, 250, 375, 500, 625, 750, 875],
                                 max_frame_gap_ms=125)
        self.assertEqual([v['at_ms'] for v in samples], [132, 264, 363, 495, 627, 759, 891])
        self.assertEqual(len(set(v['frame'] for v in samples)), 7)
        self.assertTrue(all(v['duration_ms'] == 33 for v in samples))
        self.assertFalse(sampler.provenance['container_fps_used_for_sampling'])
        self.assertEqual(sampler.provenance['reported_base_rate'], '1000')
        sampler.close()

    def test_missing_frame_rate_metadata_is_allowed_with_valid_timestamps(self):
        sampler = self.fixture([0, 30, 68, 102], rate=None)
        self.assertEqual(sampler.sample([68])[0]['at_ms'], 68)
        self.assertIsNone(sampler.provenance['reported_base_rate'])

    def test_eight_hz_half_frame_phase_uses_seven_distinct_nearest_frames(self):
        sampler = self.fixture([Fraction(125, 2) + 125 * i for i in range(10)], rate=8)
        requested = [125, 250, 375, 500, 625, 750, 875]
        selected = sampler.sample(requested, max_frame_gap_ms=125)
        times = [item['at_ms'] for item in selected]
        self.assertEqual(times, [62.5, 187.5, 312.5, 437.5, 562.5, 687.5, 812.5])
        self.assertEqual(len(set(item['frame'] for item in selected)), 7)
        self.assertTrue(all(b - a == 125 for a, b in zip(times, times[1:])))
        self.assertTrue(all(abs(at - request) == 62.5 for at, request in zip(times, requested)))
        self.assertTrue(all(item['duration_ms'] == 125 for item in selected))

    def test_normal_thirty_fps_legacy_even_ties_are_unchanged(self):
        sampler = self.fixture([Fraction(1000, 30) * i for i in range(40)], rate=30)
        requested = [125, 250, 375, 500, 625, 750, 875]
        selected = sampler.sample(requested, max_frame_gap_ms=125)
        original_times = [round(float(Fraction(1000, 30) * round(at * 30 / 1000)), 3)
                          for at in requested]
        self.assertEqual([item['at_ms'] for item in selected], original_times)
        self.assertEqual(selected[1]['at_ms'], 266.667)
        self.assertEqual(selected[5]['at_ms'], 733.333)

    def test_duplicate_unique_nearest_never_selects_a_more_distant_frame(self):
        sampler = self.fixture([0, 125, 250, 375], rate=8)
        with self.assertRaisesRegex(ValueError, 'temporal_density_insufficient'):
            sampler.sample([10, 20], max_frame_gap_ms=125)

    def test_equal_nearest_repair_rejects_an_infeasible_distinct_sequence(self):
        sampler = self.fixture([0, 100, 200, 300])
        with self.assertRaisesRegex(ValueError, 'temporal_density_insufficient'):
            sampler.sample([50, 60, 70])

    def test_request_count_bounds_tie_resolution_before_decoding(self):
        sampler = self.fixture(range(0, 10000, 33))
        with self.assertRaisesRegex(ValueError, 'request_order_invalid'):
            sampler.sample(range(0, 800, 100))
        self.assertEqual(sampler.decoded_count, 0)

    def test_container_origin_preserves_relative_offsets(self):
        sampler = self.fixture([500, 530, 560, 590], origin=500000)
        self.assertEqual(sampler.sample([60])[0]['at_ms'], 60)
        self.assertEqual(sampler.sample([90])[0]['duration_source'], 'unknown_at_stream_end')
        self.assertIsNone(sampler.sample([90])[0]['duration_ms'])

    def test_true_low_rate_cannot_pass_with_false_high_metadata(self):
        sampler = self.fixture([0, 200, 400, 600])
        with self.assertRaisesRegex(ValueError, 'density_insufficient'):
            sampler.sample([200], max_frame_gap_ms=125)

    def test_missing_window_and_end_are_never_filled(self):
        sampler = self.fixture([0, 33, 66, 3000])
        with self.assertRaisesRegex(ValueError, 'sampling_gap'):
            sampler.sample([1000])
        with self.assertRaisesRegex(ValueError, 'sampling_gap'):
            sampler.sample([4000])

    def test_overlapping_gaze_windows_keep_native_frames_and_bounded_memory(self):
        sampler = self.fixture(range(0, 11000, 33))
        first = sampler.sample([125, 250, 375, 500, 625, 750, 875])
        second = sampler.sample([325, 450, 575, 700, 825, 950, 1075])
        self.assertLess(second[0]['at_ms'], first[-1]['at_ms'])
        sampler.sample([9000])
        self.assertLess(len(sampler.frames), 10)
        self.assertLess(sampler.decoded_count, 300)

    def test_nonmonotonic_or_missing_pts_are_rejected(self):
        for times in ([0, 33, 33, 66], [0, 33, None], [0, -2]):
            sampler = self.fixture(times)
            with self.subTest(times=times), self.assertRaises(ValueError):
                sampler.sample([100])

    def test_nonmonotonic_requests_are_rejected(self):
        sampler = self.fixture(range(0, 500, 33))
        sampler.sample([100])
        for requests in ([50], [300, 200], [-1], [float('nan')]):
            with self.assertRaises(ValueError):
                sampler.sample(requests)

    def test_failure_diagnostics_do_not_serialize_arbitrary_details(self):
        for error in (ValueError('PRIVATE_PATH PRIVATE_TEXT'), RuntimeError('secret_key=PRIVATE')):
            diagnostic = _model_failure(error, 1.23)
            self.assertEqual(diagnostic['code'], 'model_execution_failed')
            self.assertNotIn('PRIVATE', json.dumps(diagnostic))
        diagnostic = _model_failure(ValueError('native_video_timestamp_missing'), 1.23)
        self.assertEqual(diagnostic['category'], 'media_timeline')
        self.assertEqual(diagnostic['code'], 'native_video_timestamp_missing')
        self.assertFalse(diagnostic['automatic_retry'])

    def test_module_failure_persists_safe_diagnostic_without_replacing_other_model(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.webm'
            source.write_bytes(b'synthetic contract fixture')
            with patch('interview_multimodal.inspect_media', return_value={
                    'has_video': True, 'has_audio': False, 'duration_ms': 1000}), \
                 patch('interview_gaze360.run_gaze360', side_effect=ValueError('native_video_timestamp_missing')), \
                 patch('interview_emotic.run_emotic', side_effect=RuntimeError('PRIVATE_PATH')):
                result = run_multimodal(source, {}, Path(directory) / 'result', ['gaze360', 'emotic'])
        gaze, emotic = (result['modules'][name] for name in ['gaze360', 'emotic'])
        self.assertEqual(gaze['status'], 'unavailable')
        self.assertEqual(gaze['provenance']['execution']['category'], 'media_timeline')
        self.assertEqual(emotic['provenance']['execution']['code'], 'model_execution_failed')
        self.assertEqual(emotic['observations'], [])
        self.assertNotIn('PRIVATE_PATH', json.dumps(result))


if __name__ == '__main__':
    unittest.main()
