"""Run the real pinned Gaze360 network on the public repository media fixture."""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import time

from interview_gaze360 import run_gaze360, CHECKPOINT_SHA256

ROOT = Path(__file__).resolve().parent


def main():
    source = ROOT / 'data' / 'small-question-video.mp4'
    output = ROOT / 'local-data' / 'interview-validation' / 'gaze360'
    before = time.perf_counter()
    result = run_gaze360(source, output, max_windows=180)
    valid = [x for x in result['observations'] if x['quality'] == 'valid']
    missing = [x for x in result['observations'] if x['quality'] != 'valid']
    assert len(valid) > 3 and not result['summary']['truncated_by_window_limit']
    for item in valid:
        times, requested = item['input_frame_times_ms'], item['requested_frame_times_ms']
        assert len(set(times)) == len(requested) == 7
        assert all(b-a == 125 for a, b in zip(requested, requested[1:]))
        assert item['last_input_ms'] == times[-1] > item['centre_ms'] == times[3]
        assert abs(sum(v*v for v in item['values']['direction_xyz']) - 1) < 1e-5
        assert item['capture'] == item['input_captures'][3]
        assert item['ready_elapsed_ms'] >= 0
        assert all(0 <= value <= math.pi for value in item['values']['angular_interval_radians'])
    assert all(not item['values'] for item in missing)
    proof = {'schema_version': 'gaze360-real-proof.v1', 'checked_at': datetime.now(timezone.utc).isoformat(),
        'status': 'passed', 'module_status': result['status'], 'run_id': result['run_id'],
        'fixture': 'data/small-question-video.mp4', 'media_sha256': result['media_sha256'],
        'checkpoint_sha256': CHECKPOINT_SHA256, 'model_id': result['model_id'],
        'summary': result['summary'], 'missing_reasons': sorted({x['reason'] for x in missing}),
        'state_dict_keys': result['provenance']['state_dict_keys'],
        'model_sample_hz': result['provenance']['model_sample_hz'],
        'max_native_sampling_error_ms': max(x['max_input_frame_error_ms'] for x in valid),
        'elapsed_seconds': round(time.perf_counter()-before, 3),
        'artifact': result['artifact'],
        'code_sha256': hashlib.sha256((ROOT/'interview_gaze360.py').read_bytes()).hexdigest(),
        'limitations': result['notices'], 'participant_data_used': False, 'cloud_calls': 0}
    path = output / ('check-' + result['run_id'] + '.json')
    path.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(proof, ensure_ascii=False))


if __name__ == '__main__':
    main()
