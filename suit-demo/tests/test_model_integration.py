"""New optional models share the research pipeline without score side effects."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_multimodal import DEFAULT_MODULES, MODULES, run_multimodal
from interview_mcp import TOOLS, validate_arguments


class ModelIntegrationTests(unittest.TestCase):
    def test_missing_media_is_explicit_and_models_are_not_implicitly_scheduled(self):
        with tempfile.TemporaryDirectory() as directory:
            result = run_multimodal(None, {}, directory, ['gaze360', 'emotic'])
            self.assertEqual(set(result['modules']), {'gaze360', 'emotic'})
            for module in result['modules'].values():
                self.assertEqual(module['status'], 'no_media')
                self.assertEqual(module['observations'], [])
            self.assertTrue({'gaze360', 'emotic'} <= set(MODULES))
            self.assertFalse({'gaze360', 'emotic'} & set(DEFAULT_MODULES))

    def test_no_video_cannot_produce_visual_values(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.wav'
            source.write_bytes(b'fixture only')
            with patch('interview_multimodal.inspect_media', return_value={
                    'has_video': False, 'has_audio': True, 'duration_ms': 1000}):
                result = run_multimodal(source, {}, directory, ['gaze360', 'emotic'])
            self.assertTrue(all(m['status'] == 'no_video' and not m['observations']
                                for m in result['modules'].values()))

    def test_native_source_routing_and_canonical_nested_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.mp4'
            source.write_bytes(b'synthetic routing fixture, not real inference')
            for name in ('gaze360', 'emotic'):
                fixture = {'model_id': name + '-synthetic-contract', 'schema_version': 'fixture.v1',
                    'status': 'complete', 'run_id': 'a' * 32, 'artifact': name + '-' + 'a' * 32 + '.json',
                    'observations': [{'start_ms': 0, 'end_ms': 40, 'values': {}, 'quality': 'valid'}]}
                with self.subTest(module=name), patch('interview_multimodal.inspect_media', return_value={
                        'has_video': True, 'has_audio': False, 'duration_ms': 1000}), \
                     patch('interview_' + name + '.run_' + name, return_value=fixture) as run, \
                     patch('interview_multimodal._openface') as fallback, \
                     patch('interview_multimodal._run') as resample:
                    result = run_multimodal(source, {}, directory, [name])
                self.assertEqual(run.call_args.args[0], source.resolve())
                self.assertEqual(run.call_args.kwargs['max_windows'], 180)
                fallback.assert_not_called()
                resample.assert_not_called()
                module = result['modules'][name]
                saved = json.loads((Path(directory) / name / module['artifact']).read_text(encoding='utf-8'))
                self.assertEqual(saved, module)
                self.assertFalse(result['sampling'][name]['resampled_5fps_input'])

    def test_model_failure_does_not_replace_results_or_expose_private_error(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'fixture.mp4'
            source.write_bytes(b'synthetic routing fixture')
            with patch('interview_multimodal.inspect_media', return_value={
                    'has_video': True, 'has_audio': False, 'duration_ms': 1000}), \
                 patch('interview_gaze360.run_gaze360', side_effect=ValueError('PRIVATE_PATH')), \
                 patch('interview_emotic.run_emotic', side_effect=ValueError('PRIVATE_PATH')), \
                 patch('interview_multimodal._openface') as fallback:
                result = run_multimodal(source, {}, directory, ['gaze360', 'emotic'])
            fallback.assert_not_called()
            self.assertTrue(all(m['status'] == 'unavailable' and not m['observations']
                                for m in result['modules'].values()))
            self.assertNotIn('PRIVATE_PATH', json.dumps(result))

    def test_mcp_offers_models_only_through_explicit_local_research_write(self):
        tool = next(t for t in TOOLS if t['name'] == 'queue_local_research')
        request = {'session_id': 'synthetic-session', 'version': 1,
            'selections': [{'answer_id': 'synthetic-answer', 'transcript_revision': 1}],
            'modules': ['gaze360', 'emotic'], 'repeats': 1, 'purpose': 'routing test',
            'condition': 'synthetic', 'participant_group': 'fixture', 'request_id': 'fixture-request',
            'confirm_run': True}
        validate_arguments(request, tool['inputSchema'])
        self.assertFalse(tool['annotations']['readOnlyHint'])
        with self.assertRaises(Exception):
            validate_arguments({**request, 'confirm_run': False}, tool['inputSchema'])


if __name__ == '__main__':
    unittest.main()
