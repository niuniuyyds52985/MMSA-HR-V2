"""Synthetic media fault checks; engineering fixtures, no participant data.

FFmpeg here assembles deterministic test media, not edited user deliverables.
All outputs have their own local validation folder and fresh run IDs.
"""
from __future__ import annotations
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import time
import uuid
import wave

import numpy as np

from interview_multimodal import run_multimodal

ROOT = Path(__file__).resolve().parent


def ffmpeg(*args):
    subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-y', *map(str, args)],
        check=True, capture_output=True, timeout=45,
        creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))


def make_fixtures(directory):
    source = ROOT / 'data' / 'small-question-video.mp4'
    black = directory / 'synthetic-black-3s.mp4'
    ffmpeg('-f', 'lavfi', '-i', 'color=c=black:s=640x360:r=25:d=3',
           '-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-qp', '0', black)
    frame = directory / 'repository-frame-t1.png'
    ffmpeg('-ss', '1', '-i', source, '-frames:v', '1', '-vf', 'scale=640:-2', frame)
    frozen = directory / 'synthetic-frozen-4s.mp4'
    ffmpeg('-loop', '1', '-framerate', '25', '-i', frame, '-t', '4', '-an',
           '-c:v', 'libx264', '-preset', 'ultrafast', '-qp', '0', '-pix_fmt', 'yuv420p', frozen)
    double = directory / 'synthetic-two-face-regions-3s.mp4'
    ffmpeg('-i', source, '-t', '3', '-filter_complex',
           '[0:v]scale=480:-2,split=2[a][b];[a][b]hstack=inputs=2[v]',
           '-map', '[v]', '-an', '-r', '25', '-c:v', 'libx264', '-preset', 'ultrafast', '-qp', '0', double)
    sample_rate = 16000
    times = np.arange(sample_rate) / sample_rate
    normal = 0.2 * np.sin(2 * np.pi * 220 * times)
    clipped = np.clip(3 * np.sin(2 * np.pi * 220 * times), -1, 1)
    audio = {}
    for name, samples in {'silence': np.zeros(3 * sample_rate), 'clipping_only': np.tile(clipped, 3),
                          'clipping': np.concatenate((normal, clipped, np.zeros(sample_rate)))}.items():
        path = directory / ('synthetic-' + name + '-3s.wav')
        with wave.open(str(path), 'wb') as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(sample_rate)
            handle.writeframes(np.clip(np.round(samples * 32768), -32768, 32767).astype('<i2').tobytes())
        audio[name] = path
    return {'black': black, 'frozen': frozen, 'two_face_regions': double, **audio}


def summarize(result):
    output = {}
    for name, module in result['modules'].items():
        output[name] = {'status': module['status'], 'reason': module.get('reason'),
            'summary': module.get('summary'), 'capture_quality': module.get('capture_quality'),
            'windows': [{k: row.get(k) for k in ('start_ms', 'end_ms', 'quality', 'reason', 'quality_flags',
                       'f0_reason', 'input_frame_times_ms')} | {'values': row.get('values')}
                       for row in module.get('observations', [])]}
    return output


def assess(results):
    checks = {}
    for name in ('black', 'two_face_regions'):
        for module in ('au', 'gaze', 'expression', 'au_net'):
            rows = results[name]['modules'][module]['observations']
            checks[name + ':' + module + ':no_person_output'] = bool(rows) and all(
                item['quality'] == 'missing' and not item['values'] for item in rows)
        geometry = results[name]['modules']['gaze'].get('geometry', {}).get('observations', [])
        checks[name + ':geometry:no_points'] = bool(geometry) and all(
            not item.get('landmarks') and not item.get('gaze_arrows') for item in geometry)
    # After 2 s the offline detector backmarks the complete repeated interval.
    for module in ('au', 'gaze', 'expression'):
        rows = results['frozen']['modules'][module]['observations']
        checks['frozen:' + module + ':whole_repeated_interval_missing'] = bool(rows) and all(
            item['quality'] == 'missing' and not item['values'] for item in rows)
        checks['frozen:' + module + ':early_window_decision_time_honest'] = bool(rows) and rows[0]['last_input_ms'] >= 2000
    geometry = results['frozen']['modules']['gaze'].get('geometry', {}).get('observations', [])
    checks['frozen:geometry:no_points'] = bool(geometry) and all(
        not item.get('landmarks') and not item.get('gaze_arrows') for item in geometry)
    rows = results['frozen']['modules']['au_net']['observations']
    checks['frozen:au_net:no_identical_triplet_output'] = bool(rows) and all(
        row['quality'] == 'missing' and not row['values'] for row in rows)
    silent = results['silence']['modules']['audio']
    checks['silence:no_f0'] = all(row['values']['f0_hz'] is None for row in silent['observations'])
    checks['silence:no_speech_rate'] = silent['summary']['speech_ms'] == 0 and silent['summary']['speech_rate_units_per_min'] is None
    clip = results['clipping']['modules']['audio']
    damaged = [r for r in clip['observations'] if r['start_ms'] == 1000]
    checks['clipping:damaged_window_f0_suppressed'] = bool(damaged) and all(
        r['values']['f0_hz'] is None and r['values']['clipped_sample_fraction'] >= .05
        and 'audio_clipping' in r.get('quality_flags', []) and r.get('f0_reason') for r in damaged)
    # Summary availability must agree with published, quality-gated windows.
    has_pitch = any(r['values']['f0_hz'] is not None for r in clip['observations'])
    checks['clipping:summary_agrees_with_visible_f0'] = (clip['summary']['f0_status'] == 'available') == has_pitch
    severe = results['clipping_only']['modules']['audio']
    checks['clipping_only:no_f0_in_windows_or_summary'] = severe['summary']['f0_status'] == 'no_valid_pitch' and all(
        row['values']['f0_hz'] is None and row.get('f0_reason') for row in severe['observations'])
    for fixture, result in results.items():
        for module, data in result['modules'].items():
            checks[fixture + ':' + module + ':bounded_times'] = all(
                0 <= r['start_ms'] < r['end_ms'] <= result['duration_ms'] and
                r['start_ms'] <= r['last_input_ms'] <= result['duration_ms']
                for r in data['observations'])
    return checks


def main():
    run_id = uuid.uuid4().hex
    directory = ROOT / 'local-data' / 'interview-validation' / 'quality-synthetic' / run_id
    directory.mkdir(parents=True)
    started = time.perf_counter()
    fixtures = make_fixtures(directory)
    results = {}
    for name, source in fixtures.items():
        modules = ['audio'] if name in ('silence', 'clipping', 'clipping_only') else ['au', 'gaze', 'expression', 'au_net']
        results[name] = run_multimodal(source, {'answer_id': 'synthetic_quality_only',
            'transcript_revision': 1, 'transcript': ''}, directory / name, modules)
        print(json.dumps({'fixture': name, 'run_id': results[name]['run_id'],
            'modules': {k: v['status'] for k, v in results[name]['modules'].items()}}), flush=True)
    checks = assess(results)
    proof = {'schema_version': 'synthetic-quality-proof.v1', 'run_id': run_id,
        'created_at': datetime.now(timezone.utc).isoformat(),
        'status': 'passed' if all(checks.values()) else 'failed', 'checks': checks,
        'elapsed_seconds': round(time.perf_counter() - started, 3),
        'code_sha256': {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in
            ('check_multimodal_quality.py', 'interview_multimodal.py', 'interview_au_net.py')},
        'fixture_description': {'black': '3 seconds synthetic black frame, no audio',
            'frozen': '4 seconds exact repeated repository frame at t=1s, no audio',
            'two_face_regions': '3 seconds repository clip duplicated side-by-side; two regions, no identity claim',
            'silence': '3 seconds PCM zero samples, no transcript',
            'clipping': '0-1s 220Hz sine amplitude .2; 1-2s saturated sine; 2-3s silence',
            'clipping_only': '3 seconds saturated 220 Hz sine; approximately 78.25 percent clipped samples'},
        'fixtures': {name: {'file': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'result_artifact': name + '/' + results[name]['artifact'], 'result': summarize(results[name])}
            for name, path in fixtures.items()},
        'limitations': ['Synthetic engineering faults only; not real-participant quality accuracy.',
            'Exact repeats do not prove a camera hardware fault; lossily altered frozen frames may evade this check.',
            'Repeated interval is backmarked offline after the 2 second threshold; last_input_ms preserves that dependency.',
            'No cloud, participant sessions or credentials accessed.']}
    target = directory / 'check.json'
    target.write_text(json.dumps(proof, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps({'status': proof['status'], 'run_id': run_id,
        'passed': sum(checks.values()), 'total': len(checks),
        'failed': [k for k, v in checks.items() if not v]}))
    raise SystemExit(0 if all(checks.values()) else 1)


if __name__ == '__main__':
    main()
