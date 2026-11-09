import math
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_gaze360 import (direction_from_angles, _seven_requests, _display_windows,
    _prepare_sequence, _head_box, _verified_assets, _normalize_state)


class Gaze360Tests(unittest.TestCase):
    def test_known_camera_directions_and_unit_length(self):
        import numpy as np
        np.testing.assert_allclose(direction_from_angles(0, 0), [0, 0, -1], atol=1e-7)
        np.testing.assert_allclose(direction_from_angles(math.pi / 2, 0), [1, 0, 0], atol=1e-7)
        np.testing.assert_allclose(direction_from_angles(0, math.pi / 2), [0, 1, 0], atol=1e-7)
        np.testing.assert_allclose(direction_from_angles(math.pi, 0), [0, 0, 1], atol=1e-7)
        self.assertAlmostEqual(sum(x*x for x in direction_from_angles(.27, -.61)), 1)
        for values in ((math.nan, 0), (math.inf, 0), (4, 0), (0, 2)):
            with self.assertRaises(ValueError):
                direction_from_angles(*values)

    def test_original_eight_hz_window_and_no_boundary_padding(self):
        self.assertEqual(_seven_requests(500, 1000), [125, 250, 375, 500, 625, 750, 875])
        for centre in (0, 250, 800, 1000):
            with self.assertRaisesRegex(ValueError, 'boundary_missing'):
                _seven_requests(centre, 1000)

    def test_short_partial_display_window_keeps_its_centre_inside(self):
        windows = _display_windows(23333, 1000, 180)
        self.assertEqual(windows[-1], (23000, 23333, 23166.5))
        self.assertTrue(all(a <= c < b for a, b, c in windows))
        self.assertTrue(all(first[1] == second[0] for first, second in zip(windows, windows[1:])))

    def test_missing_or_modified_assets_fail_before_network_import(self):
        with tempfile.TemporaryDirectory() as directory, patch('interview_gaze360.ASSETS', Path(directory)):
            with self.assertRaisesRegex(ValueError, 'asset_missing'):
                _verified_assets()

    def test_complete_checkpoint_required_instead_of_silent_partial_load(self):
        import torch
        expected = {'first.weight': torch.zeros(2, 3), 'last.bias': torch.zeros(2)}
        valid = {'module.last.bias': torch.ones(2), 'module.first.weight': torch.ones(2, 3)}
        result = _normalize_state(valid, expected)
        self.assertEqual(set(result), set(expected))
        for state in ({'first.weight': torch.ones(2, 3)},
                      {**valid, 'unknown': torch.ones(1)},
                      {**valid, 'first.weight': torch.ones(2, 3)},
                      {'first.weight': torch.ones(3, 2), 'last.bias': torch.ones(2)},
                      {'first.weight': torch.full((2, 3), math.nan), 'last.bias': torch.ones(2)}):
            with self.subTest(keys=list(state)), self.assertRaises(ValueError):
                _normalize_state(state, expected)

    @staticmethod
    def fixtures(face_count=1):
        import numpy as np
        base = np.indices((240, 320)).sum(0).astype('uint8')
        frames = [np.stack([base, np.roll(base, i, axis=0), np.roll(base, i, axis=1)], axis=2) for i in range(7)]
        face = [100, 70, 80, 80, 120, 95, 160, 95, 140, 120, 125, 135, 155, 135, .99]
        detector = SimpleNamespace(setInputSize=lambda _: None,
            detect=lambda _: (None, np.array([face]*face_count, dtype=float) if face_count else None))
        return frames, [125*i for i in range(7)], detector

    def test_seven_distinct_temporal_frames_required(self):
        frames, times, detector = self.fixtures()
        for invalid in ([0]*7, times[:6], list(reversed(times))):
            with self.assertRaises(ValueError):
                _prepare_sequence(frames, invalid, 30, detector)
        with self.assertRaisesRegex(ValueError, 'identical_temporal_input'):
            _prepare_sequence([frames[0]]*7, times, 30, detector)

    def test_no_arbitrary_person_selection(self):
        for count in (0, 2):
            frames, times, detector = self.fixtures(count)
            with self.subTest(count=count), self.assertRaisesRegex(ValueError, 'one_reliable_face'):
                _prepare_sequence(frames, times, 30, detector)

    def test_dark_or_flat_frame_does_not_become_gaze(self):
        import numpy as np
        frames, times, detector = self.fixtures()
        frames[4] = np.zeros_like(frames[4])
        with self.assertRaisesRegex(ValueError, 'dark_or_flat_frame'):
            _prepare_sequence(frames, times, 30, detector)

    def test_preprocessed_input_and_saved_captures_agree(self):
        import numpy as np
        frames, times, detector = self.fixtures()
        tensor, captures = _prepare_sequence(frames, times, 30, detector)
        self.assertEqual(tensor.shape, (7, 3, 224, 224))
        self.assertTrue(np.isfinite(tensor).all())
        self.assertEqual([x['at_ms'] for x in captures], times)
        for capture in captures:
            box = capture['boxes'][0]
            self.assertEqual(box['kind'], 'head')
            self.assertGreater(box['width'], 80)
            self.assertGreater(box['height'], 80)
            self.assertTrue(0 <= box['x'] < box['x']+box['width'] <= capture['frame_width'])
            self.assertTrue(0 <= box['y'] < box['y']+box['height'] <= capture['frame_height'])
            self.assertFalse(capture['padding'])

    def test_bad_head_regions_fail_without_inventing_padding(self):
        for face in ([0, -70, 80, 80], [0, 0, 20, 20], [math.nan, 10, 80, 80]):
            with self.subTest(face=face), self.assertRaises(ValueError):
                _head_box(face, 320, 240)


if __name__ == '__main__':
    unittest.main()
