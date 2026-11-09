"""Bounded real EMOTIC check on the repository's public video sample."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time

from interview_emotic import ALLOWED_LABELS, run_emotic, validate_public_values

ROOT = Path(__file__).resolve().parent


def check(max_windows=24):
    output = ROOT / 'local-data' / 'interview-validation' / 'emotic'
    started = time.perf_counter()
    result = run_emotic(ROOT / 'data' / 'small-question-video.mp4', output, max_windows=max_windows)
    valid = [row for row in result['observations'] if row['quality'] == 'valid']
    for row in valid:
        validate_public_values(row['values'])
        assert {box['kind'] for box in row['capture']['boxes']} == {'person', 'context'}
        assert row['capture']['at_ms'] == row['centre_ms']
        assert row['capture']['duration_ms'] > 0
        assert set(row['values']['category_scores']) == set(ALLOWED_LABELS)
    record = {'schema_version': 'emotic-real-check.v1', 'checked_at': datetime.now(timezone.utc).isoformat(),
              'input': 'repository public small-question-video.mp4', 'run_id': result['run_id'],
              'status': result['status'], 'summary': result['summary'],
              'elapsed_with_import_seconds': round(time.perf_counter() - started, 3),
              'artifact': result['artifact'], 'real_weights_loaded': True,
              'checkpoint_origin': 'community-preserved Tandon-compatible snapshot, not original Lua',
              'strict_loading': True, 'public_categories': list(ALLOWED_LABELS),
              'unreviewed_categories_exposed': False, 'continuous_head_executed': False,
              'cloud_calls': 0, 'participant_data_read': False, 'passed': len(valid) > 0}
    (output / ('check-' + result['run_id'] + '.json')).write_text(
        json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return record


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--max-windows', type=int, default=24)
    args = parser.parse_args()
    print(json.dumps(check(args.max_windows), ensure_ascii=False))
