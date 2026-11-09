import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_observations import summarize_multimodal, EXPRESSION


def observation(start, end, values, **extra):
    return {'start_ms': start, 'end_ms': end, 'quality': 'valid', 'reason': None,
            'values': values, 'track_segment_id': 'visible-face-run-001', **extra}


def answer():
    return {'run_id': 'a' * 32, 'answer_id': 'answer-1', 'transcript_revision': 3,
            'media_sha256': 'b' * 64, 'duration_ms': 10000, 'modules': {}}


def audio():
    return {'status': 'complete', 'model_id': 'WebRTC-VAD2+pYIN+RMS', 'summary': {
            'duration_ms': 10000, 'speech_ms': 8000, 'pause_count': 2, 'pause_ms': 800,
            'transcript_units': 20, 'speech_rate_units_per_min': 120,
            'articulation_rate_units_per_speech_min': 150},
            'observations': [observation(i * 1000, (i + 1) * 1000,
                {'f0_hz': 120 if i < 6 else 80 if i < 8 else 180,
                 'f0_voiced_frames': 40, 'speech_fraction': .8,
                 'rms_dbfs': -30 if i < 6 else -40 if i < 8 else -20}) for i in range(10)]}


def face_fixture(label='happy'):
    data = answer()
    raw = {name: (2.0 if name == label else -.5) for name in EXPRESSION}
    expressions = [observation(i * 200, (i + 1) * 200,
                     {'label': label, 'raw_scores': copy.deepcopy(raw)}, face_count=1) for i in range(4)]
    aus = [observation(i * 200, (i + 1) * 200,
                      {'intensity': {'AU12': 2.4, 'AU06': .3, 'AU25': 1.2,
                                     'AU04': 2.0, 'AU15': 1.5, 'AU01': .1},
                       'presence': {'AU12': 1, 'AU06': 0, 'AU25': 1,
                                    'AU04': 1, 'AU15': 1, 'AU01': 0}}) for i in range(4)]
    points = [[.5, .5] for _ in range(68)]
    for i in range(36, 42): points[i] = [.35, .4]
    for i in range(42, 48): points[i] = [.65, .4]
    points[48], points[54], points[62], points[66] = [.3, .65], [.7, .65], [.5, .6], [.5, .7]
    geometry = {'coordinate_space': 'normalized_sample_frame', 'observations': [
        observation(0, 200, {}, landmarks=points, frame_width=640, frame_height=480)]}
    data['modules'] = {'expression': {'model_id': 'MobileFaceNet', 'status': 'complete', 'observations': expressions},
                       'au': {'model_id': 'OpenFace', 'status': 'complete', 'observations': aus, 'geometry': geometry}}
    return data


def context_fixture(count=4):
    data = answer()
    allowed = ['Anger', 'Happiness', 'Sadness', 'Surprise']
    rows = []
    for index in range(count):
        scores = dict.fromkeys(allowed, -.1)
        scores['Happiness'] = .4 + index / 100
        rows.append(observation(index * 1000, (index + 1) * 1000,
            {'category_scores': scores, 'category_thresholds': dict.fromkeys(allowed, .2)},
            capture={'at_ms': index * 1000, 'duration_ms': 33.333}))
    data['modules']['emotic'] = {'model_id': 'Tandon-A/emotic', 'status': 'complete', 'observations': rows}
    return data


class ObservationSummaryTests(unittest.TestCase):
    def test_actual_answer_item_contract_and_source_immutability(self):
        data = face_fixture()
        frozen = copy.deepcopy(data)
        result = summarize_multimodal(data, 'research-run')
        self.assertEqual(data, frozen)
        self.assertEqual(result['source_run_id'], 'research-run')
        self.assertEqual(result['multimodal_run_id'], data['run_id'])
        self.assertEqual(result['transcript_revision'], 3)
        self.assertFalse(result['events_truncated'])
        self.assertEqual(result['validation']['status'], 'unvalidated_for_current_domain')
        json.dumps(result, allow_nan=False)
        result['events'][0]['geometry']['landmarks'][0][0] = 0
        self.assertEqual(data, frozen)

    def test_parent_research_run_is_not_accepted_as_answer(self):
        with self.assertRaises(ValueError):
            summarize_multimodal({'multimodal': [face_fixture()]})

    def test_pitch_and_energy_changes_have_other_window_references(self):
        data = answer(); data['modules']['audio'] = audio()
        result = summarize_multimodal(data)
        low = next(v for v in result['events'] if v['kind'] == 'audio_pitch' and v['start_ms'] == 6000)
        self.assertEqual(low['title'], '音调偏低')
        self.assertEqual(low['measurements']['median'], 80)
        self.assertAlmostEqual(low['measurements']['difference'], -7.020, places=3)
        self.assertEqual(low['reference']['median'], 120)
        self.assertNotIn(6, low['reference']['observation_indices'])
        energy = next(v for v in result['events'] if v['kind'] == 'audio_energy' and v['start_ms'] == 8000)
        self.assertEqual(energy['measurements']['difference'], 10)
        self.assertEqual(energy['evidence_refs'][0]['observation_indices'], [8, 9])
        self.assertEqual(result['audio_summary']['speech_rate_units_per_min'], 120)

    def test_identical_pitch_and_energy_do_not_generate_change(self):
        data = answer(); data['modules']['audio'] = audio()
        for item in data['modules']['audio']['observations']:
            item['values'].update(f0_hz=100, rms_dbfs=-30)
        result = summarize_multimodal(data)
        self.assertEqual(result['events'], [])
        self.assertEqual(result['audio_summary']['pitch']['p10_p90_span_semitones'], 0)

    def test_clipping_silence_and_short_reference_cannot_create_comparison(self):
        data = answer(); data['modules']['audio'] = audio()
        for item in data['modules']['audio']['observations'][:6]:
            item['quality_flags'] = ['audio_clipping']
        result = summarize_multimodal(data)
        self.assertEqual(result['events'], [])
        self.assertEqual(result['coverage']['audio']['excluded_observations'], 6)
        for item in data['modules']['audio']['observations']:
            item['values']['speech_fraction'] = 0
        self.assertEqual(summarize_multimodal(data)['events'], [])

    def test_happy_with_independent_au_and_actual_pixel_corrected_geometry(self):
        result = summarize_multimodal(face_fixture())
        event = result['events'][0]
        self.assertEqual(event['label'], 'happy')
        self.assertEqual(event['relation'], 'co_occurring')
        self.assertEqual(event['measurements']['au'][0]['code'], 'AU12')
        self.assertEqual(event['measurements']['au'][0]['intensity'], 2.4)
        metrics = event['geometry']['metrics']
        self.assertAlmostEqual(metrics['mouth_width_over_eye_distance'], 4 / 3, places=4)
        # 0.1 * 480 pixels / (0.3 * 640 pixels) = 0.25, not 0.333.
        self.assertEqual(metrics['inner_lip_gap_over_eye_distance'], .25)
        self.assertEqual([r['module'] for r in event['evidence_refs']], ['expression', 'au', 'au'])
        self.assertIn('不是分类器', event['limitations'][0])

    def test_sad_not_rejected_when_au01_absent(self):
        event = summarize_multimodal(face_fixture('sad'))['events'][0]
        self.assertEqual(event['label'], 'sad')
        self.assertEqual(next(v for v in event['measurements']['au'] if v['code'] == 'AU01')['presence'], 0)

    def test_bad_light_freeze_and_track_change_break_expression_groups(self):
        for flag in ('low_light', 'overexposed', 'repeated_identical_frames'):
            data = face_fixture()
            data['modules']['expression']['observations'][1]['quality_flags'] = [flag]
            self.assertEqual(summarize_multimodal(data)['events'], [])
        data = face_fixture()
        for item in data['modules']['expression']['observations'][2:]:
            item['track_segment_id'] = 'another-track'
        self.assertEqual(summarize_multimodal(data)['events'], [])

    def test_other_track_or_time_cannot_be_expression_support(self):
        data = face_fixture()
        for item in data['modules']['au']['observations']:
            item['track_segment_id'] = 'another-track'
        geometry = data['modules']['au']['geometry']['observations'][0]
        geometry['start_ms'], geometry['end_ms'] = 2000, 2200
        event = summarize_multimodal(data)['events'][0]
        self.assertEqual(event['measurements']['au'], [])
        self.assertIsNone(event['geometry'])
        self.assertEqual(len(event['evidence_refs']), 1)

    def test_missing_geometry_is_not_fabricated(self):
        data = face_fixture()
        data['modules']['au']['geometry']['observations'][0]['landmarks'] = []
        event = summarize_multimodal(data)['events'][0]
        self.assertIsNone(event['geometry'])
        self.assertEqual(event['measurements']['au'][0]['intensity'], 2.4)

    def test_nonfinite_or_wrong_class_scores_are_excluded(self):
        for value in (float('nan'), True):
            data = face_fixture()
            for item in data['modules']['expression']['observations']:
                item['values']['raw_scores']['happy'] = value
            self.assertEqual(summarize_multimodal(data)['events'], [])
        data = face_fixture()
        for item in data['modules']['expression']['observations']:
            item['values']['label'] = 'sad'
        self.assertEqual(summarize_multimodal(data)['events'], [])

    def test_no_media_is_explicit_and_no_fabricated_accuracy(self):
        data = answer(); data['modules']['audio'] = {'status': 'no_media', 'observations': []}
        result = summarize_multimodal(data)
        self.assertEqual(result['status'], 'insufficient_data')
        self.assertIsNone(result['coverage']['audio']['valid_fraction'])
        self.assertEqual(result['events'], [])
        self.assertEqual(result['validation']['independent_reference_samples'], 0)

    def test_unavailable_module_does_not_reuse_stale_observations(self):
        data = face_fixture()
        data['modules']['expression']['status'] = 'unavailable'
        self.assertEqual(summarize_multimodal(data)['events'], [])
        data['modules']['audio'] = None
        self.assertEqual(summarize_multimodal(data)['status'], 'insufficient_data')

    def test_whole_recording_future_context_remains_explicit(self):
        data = answer(); data['modules']['audio'] = audio()
        for item in data['modules']['audio']['observations']:
            item['last_input_ms'] = 10000
        event = summarize_multimodal(data)['events'][0]
        self.assertEqual(event['last_input_ms'], 10000)

    def test_context_emotion_only_allows_four_original_categories(self):
        data = answer()
        allowed = ['Anger', 'Happiness', 'Sadness', 'Surprise']
        data['modules']['emotic'] = {'model_id': 'Tandon-A/emotic', 'status': 'complete',
            'observations': [observation(0, 1000, {'category_scores': dict.fromkeys(allowed, .4),
                'category_thresholds': dict.fromkeys(allowed, .2)}, capture={'at_ms': 0})]}
        event = summarize_multimodal(data)['events'][0]
        self.assertEqual(event['kind'], 'context_expression')
        self.assertEqual(event['relation'], 'model_estimate')
        data['modules']['emotic']['observations'][0]['values']['category_scores']['Confidence'] = .9
        self.assertEqual(summarize_multimodal(data)['events'], [])

    def test_extended_only_has_truthful_overview_and_one_group_of_actual_samples(self):
        data = context_fixture(24)
        frozen = copy.deepcopy(data)
        result = summarize_multimodal(data, 'extended-run')
        self.assertEqual(result['status'], 'available')
        self.assertEqual(len(result['events']), 1)
        self.assertNotIn('尚无足够', ' '.join(result['overview']))
        self.assertIn('24 个有效采集帧', result['overview'][0])
        self.assertIn('开心 24 帧', result['overview'][0])
        event = result['events'][0]
        self.assertEqual(event['labels'], ['Happiness'])
        self.assertEqual(event['measurements']['sampled_frames'], 24)
        self.assertEqual(event['evidence_refs'][0]['observation_indices'], list(range(24)))
        self.assertEqual(event['last_input_ms'], 24000)
        self.assertEqual(event['measurements']['samples'][23]['category_scores']['Happiness'], .63)
        self.assertEqual(event['measurements']['samples'][23]['category_thresholds']['Happiness'], .2)
        self.assertEqual(event['measurements']['samples'][23]['at_ms'], 23000)
        self.assertEqual(event['measurements']['samples'][23]['duration_ms'], 33.333)
        self.assertIn('未采样间隔', ' '.join(event['limitations']))
        event['measurements']['samples'][0]['category_scores']['Happiness'] = 99
        self.assertEqual(data, frozen)

    def test_context_group_cannot_bridge_rejected_capture_or_quality(self):
        for mutation in ('low_light', 'overexposed', 'repeated_identical_frames', 'invalid_time'):
            data = context_fixture(5)
            row = data['modules']['emotic']['observations'][2]
            if mutation == 'invalid_time':
                row['capture']['at_ms'] = 9000
            else:
                row['quality_flags'] = [mutation]
            result = summarize_multimodal(data)
            self.assertEqual([e['evidence_refs'][0]['observation_indices'] for e in result['events']], [[0, 1], [3, 4]])
            self.assertEqual(result['coverage']['emotic']['excluded_observations'], 1)
            self.assertEqual(result['context_expression_summary']['valid_capture_frames'], 4)

    def test_context_group_breaks_at_labels_track_and_unsampled_gaps(self):
        for mutation in ('label', 'track', 'gap'):
            data = context_fixture(4)
            for row in data['modules']['emotic']['observations'][2:]:
                if mutation == 'label':
                    row['values']['category_scores']['Sadness'] = .5
                elif mutation == 'track':
                    row['track_segment_id'] = 'different-track'
                else:
                    row['start_ms'] += 1000
                    row['end_ms'] += 1000
                    row['capture']['at_ms'] += 1000
            events = summarize_multimodal(data)['events']
            self.assertEqual([e['evidence_refs'][0]['observation_indices'] for e in events], [[0, 1], [2, 3]])

    def test_valid_context_below_threshold_is_explicit_and_breaks_positive_groups(self):
        data = context_fixture(3)
        data['modules']['emotic']['observations'][1]['values']['category_scores']['Happiness'] = .2
        result = summarize_multimodal(data)
        self.assertEqual([e['evidence_refs'][0]['observation_indices'] for e in result['events']], [[0], [2]])
        self.assertEqual(result['context_expression_summary']['label_frame_counts']['Happiness'], 2)
        for row in data['modules']['emotic']['observations']:
            row['values']['category_scores']['Happiness'] = .1
        result = summarize_multimodal(data)
        self.assertEqual(result['events'], [])
        self.assertEqual(result['status'], 'available')
        self.assertIn('四类标签均未超过', result['overview'][0])
        self.assertNotIn('尚无足够', result['overview'][0])

    def test_invalid_context_capture_never_claims_valid_scene_observations(self):
        data = context_fixture(2)
        data['modules']['emotic']['observations'][0]['capture'] = 'invalid'
        data['modules']['emotic']['observations'][1]['capture']['at_ms'] = True
        result = summarize_multimodal(data)
        self.assertEqual(result['events'], [])
        self.assertEqual(result['coverage']['emotic']['valid_observations'], 0)
        self.assertEqual(result['status'], 'insufficient_data')


if __name__ == '__main__':
    unittest.main()
