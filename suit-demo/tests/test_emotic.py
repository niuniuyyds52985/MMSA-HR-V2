import copy
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import interview_emotic as emotic


class FakeDetector:
    def __init__(self, rows):
        self.rows = np.asarray(rows, dtype=np.float32).reshape(1, 1, -1, 7)

    def setInput(self, blob):
        self.blob = blob

    def forward(self):
        return self.rows


class EmoticContractTests(unittest.TestCase):
    def test_raw_scores_are_not_softmax_and_comparison_is_strict(self):
        values = emotic._project_outputs([-.2, 2.5, .3, .7], [-.2, 1.2, .6, .1])
        self.assertEqual(values['category_scores']['Happiness'], 2.5)
        self.assertEqual(values['category_scores']['Anger'], -.2)
        self.assertEqual(values['above_threshold'], ['Happiness', 'Surprise'])
        self.assertEqual(emotic.validate_public_values(values), values)

    def test_no_unreviewed_categories_or_continuous_values_can_serialize(self):
        base = emotic._project_outputs([.1, .2, .3, .4], [.2] * 4)
        for location in ('category_scores', 'category_thresholds'):
            candidate = copy.deepcopy(base)
            candidate[location]['Confidence'] = .8
            with self.assertRaises(ValueError):
                emotic.validate_public_values(candidate)
        candidate = copy.deepcopy(base)
        candidate['above_threshold'].append('Dominance')
        with self.assertRaises(ValueError):
            emotic.validate_public_values(candidate)
        for field in ('vad', 'raw_logits', 'all_categories', 'confidence'):
            with self.assertRaises(ValueError):
                emotic.validate_public_values({**base, field: [1, 2, 3]})
        serialized = json.dumps(base)
        self.assertNotIn('Confidence', serialized)
        self.assertNotIn('Dominance', serialized)

    def test_wrong_dimension_and_nonfinite_outputs_rejected(self):
        for values in ([.1] * 26, [.1] * 3, [.1, .2, float('nan'), .4], [.1, .2, float('inf'), .4]):
            with self.assertRaises(ValueError):
                emotic._project_outputs(values, [.2] * 4)
        with self.assertRaises(ValueError):
            emotic._project_outputs([.1] * 4, [.2] * 26)

    def test_body_input_is_exact_box_context_is_whole_frame(self):
        # Different quadrants make an accidental whole-image body crop visible.
        frame = np.zeros((80, 120, 3), np.uint8)
        frame[:] = [10, 20, 30]
        frame[0:40, 0:40] = [100, 150, 200]
        context, body = emotic._prepare_inputs(frame, [0, 0, 40, 40])
        self.assertEqual(tuple(context.shape), (1, 3, 224, 224))
        self.assertEqual(tuple(body.shape), (1, 3, 128, 128))
        expected = (np.asarray([200, 150, 100]) / 255 - emotic.BODY_MEAN) / emotic.BODY_STD
        np.testing.assert_allclose(body.numpy()[0, :, 64, 64], expected, rtol=1e-6)
        expected_context = (np.asarray([30, 20, 10]) / 255 - emotic.CONTEXT_MEAN) / emotic.CONTEXT_STD
        np.testing.assert_allclose(context.numpy()[0, :, -1, -1], expected_context, rtol=1e-6)

    def test_invalid_or_face_sized_boxes_are_not_silently_expanded(self):
        frame = np.zeros((100, 120, 3), np.uint8)
        for box in ([-1, 0, 50, 50], [0, 0, 20, 20], [100, 0, 60, 70], [0., 0, 50, 50]):
            with self.assertRaises(ValueError):
                emotic._prepare_inputs(frame, box)

    def test_detector_rejects_multiple_people_and_dark_frame(self):
        frame = np.tile(np.arange(240, dtype=np.uint8), (120, 1))
        frame = np.repeat(frame[:, :, None], 3, axis=2)
        rows = [[0, 15, .95, 0, 0, .4, 1], [0, 15, .92, .6, 0, 1, 1]]
        with self.assertRaisesRegex(ValueError, 'one_detected_person'):
            emotic._detect_person(frame, FakeDetector(rows))
        with self.assertRaisesRegex(ValueError, 'dark_or_flat'):
            emotic._detect_person(np.zeros_like(frame), FakeDetector(rows[:1]))
        box = emotic._detect_person(frame, FakeDetector(rows[:1]))
        self.assertEqual(box, [0, 0, 96, 120])

    def test_missing_assets_fail_before_model_execution(self):
        with patch.object(emotic, 'ASSETS', ROOT / 'models' / 'definitely-not-present-emotic-test'):
            with self.assertRaises(ValueError):
                emotic._verified_assets()

    def test_sampling_validation_rejects_boolean_and_unbounded_settings(self):
        for settings in ({'max_windows': 181}, {'max_windows': True}, {'sample_interval_ms': 1}, {'sample_interval_ms': 1000.5}):
            with self.assertRaisesRegex(ValueError, 'sampling_invalid'):
                emotic.run_emotic('unused.mp4', 'unused', **settings)


if __name__ == '__main__':
    unittest.main()
