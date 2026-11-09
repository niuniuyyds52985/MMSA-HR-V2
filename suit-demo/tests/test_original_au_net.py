import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_au_net import _similarity, _crop_triplet, _verified_assets, _map_predictor_keys


class OriginalAUNetTests(unittest.TestCase):
    def test_similarity_recovers_known_rotation_scale_translation(self):
        import numpy as np
        source = np.array([[1, 2], [6, 2], [3, 4], [2, 7], [5, 7]], dtype=float)
        actual = np.array([[1.5, -.4, 13], [.4, 1.5, -9]])
        expected = np.column_stack((source, np.ones(5))) @ actual.T
        np.testing.assert_allclose(_similarity(source, expected), actual, atol=1e-10)

    def test_invalid_alignment_cannot_invent_a_face_transform(self):
        import numpy as np
        with self.assertRaises(ValueError):
            _similarity(np.zeros((5, 2)), np.zeros((5, 2)))
        with self.assertRaises(ValueError):
            _similarity(np.full((5, 2), np.nan), np.zeros((5, 2)))

    def test_missing_neighbour_face_cannot_be_replaced_with_centre(self):
        import numpy as np
        frame = np.zeros((160, 200, 3), dtype=np.uint8)
        detector = SimpleNamespace(setInputSize=lambda _: None, detect=lambda _: (None, None))
        with self.assertRaisesRegex(ValueError, 'one_reliable_face'):
            _crop_triplet([frame, frame, frame], detector)

    def test_multiple_faces_not_selected_arbitrarily(self):
        import numpy as np
        frame = np.zeros((160, 200, 3), dtype=np.uint8)
        face = [50, 30, 80, 80, 70, 50, 110, 50, 90, 70, 75, 95, 105, 95, .99]
        detector = SimpleNamespace(setInputSize=lambda _: None,
            detect=lambda _: (None, np.array([face, face], dtype=float)))
        with self.assertRaisesRegex(ValueError, 'one_reliable_face'):
            _crop_triplet([frame, frame, frame], detector)

    def test_identical_temporal_frames_are_rejected_even_with_one_face(self):
        import numpy as np
        frame = np.zeros((160, 200, 3), dtype=np.uint8)
        face = [50, 30, 80, 80, 70, 50, 110, 50, 90, 70, 75, 95, 105, 95, .99]
        detector = SimpleNamespace(setInputSize=lambda _: None,
            detect=lambda _: (None, np.array([face], dtype=float)))
        with self.assertRaisesRegex(ValueError, 'temporal_input_identical'):
            _crop_triplet([frame, frame.copy(), frame.copy()], detector)

    def test_unverified_source_is_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as directory, patch('interview_au_net.ASSETS', Path(directory)):
            with self.assertRaisesRegex(ValueError, 'asset_missing'):
                _verified_assets()

    def test_legacy_checkpoint_names_map_by_name_not_insertion_order(self):
        import numpy as np
        first, second = np.zeros((2, 3)), np.ones((3, 4))
        state = {'module.emo_fc.0.weight': second, 'emo_net_1.0.conv1.weight': first}
        expected = {'ConvNet_1.0.conv1.weight': first, 'predictor.0.weight': second}
        actual = _map_predictor_keys(state, expected)
        self.assertIs(actual['ConvNet_1.0.conv1.weight'], first)
        self.assertIs(actual['predictor.0.weight'], second)

    def test_unknown_or_wrong_shaped_checkpoint_keys_are_rejected(self):
        import numpy as np
        expected = {'predictor.0.weight': np.zeros((2, 3))}
        for state in ({'emo_fc.0.weight': np.zeros((3, 2))},
                      {'unknown.0.weight': np.zeros((2, 3))}, {}):
            with self.subTest(state=list(state)):
                with self.assertRaisesRegex(ValueError, 'keys_mismatch'):
                    _map_predictor_keys(state, expected)

    def test_au_net_routes_explicitly_without_openface_or_resampled_input(self):
        from interview_multimodal import run_multimodal
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.mp4'
            source.write_bytes(b'synthetic routing only')
            module = {'model_id': 'official-fixture', 'schema_version': 'fixture.v1',
                      'status': 'complete', 'observations': [], 'summary': {}}
            with patch('interview_multimodal.inspect_media', return_value={
                    'has_video': True, 'has_audio': False, 'duration_ms': 1000}), \
                 patch('interview_au_net.run_au_net', return_value=module) as run, \
                 patch('interview_multimodal._openface') as openface, \
                 patch('interview_multimodal._run') as resample:
                result = run_multimodal(source, {}, Path(directory) / 'out', ['au_net'])
            self.assertEqual(list(result['modules']), ['au_net'])
            self.assertEqual(run.call_args.args[0], source.resolve())
            self.assertEqual(run.call_args.kwargs['max_windows'], 180)
            openface.assert_not_called()
            resample.assert_not_called()

    def test_au_net_is_never_default_and_missing_asset_does_not_substitute(self):
        from interview_multimodal import run_multimodal
        with tempfile.TemporaryDirectory() as directory:
            self.assertNotIn('au_net', run_multimodal(None, {}, directory)['modules'])
            source = Path(directory) / 'fixture.mp4'
            source.write_bytes(b'synthetic routing only')
            with patch('interview_multimodal.inspect_media', return_value={
                    'has_video': True, 'has_audio': False, 'duration_ms': 1000}), \
                 patch('interview_au_net.run_au_net', side_effect=ValueError('PRIVATE_PATH')), \
                 patch('interview_multimodal._openface') as openface:
                result = run_multimodal(source, {}, Path(directory) / 'out', ['au_net'])
            self.assertEqual(result['modules']['au_net']['status'], 'unavailable')
            self.assertEqual(result['modules']['au_net']['observations'], [])
            self.assertNotIn('PRIVATE_PATH', str(result))
            openface.assert_not_called()


if __name__ == '__main__':
    unittest.main()
