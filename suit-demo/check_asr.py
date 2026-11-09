"""Reproducible CPU timing on the bundled public sample, never participant media."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import difflib
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import time
import uuid

from interview_asr import FAST_ENGINE, LEGACY_ENGINE
from interview_media import analyze_media, _run

ROOT = Path(__file__).resolve().parent
SAMPLE = ROOT / 'data' / 'small-question-video.mp4'
OUTPUT = ROOT / 'local-data' / 'interview-validation' / 'asr-v3-performance.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', choices=('both', 'fast', 'legacy'), default='both')
    parser.add_argument('--long', action='store_true', help='Repeat the same public sample seven times (~163 s), not new participant material')
    parser.add_argument('--worker-smoke', action='store_true', help='Verify two actual jobs reuse one child and commit to an isolated database')
    args = parser.parse_args()
    if args.worker_smoke:
        worker_smoke()
        return
    if args.engine == 'both':
        records = []
        for engine in ('legacy', 'fast'):
            subprocess.run([sys.executable, str(Path(__file__).resolve()), '--engine', engine,
                            *(['--long'] if args.long else [])], check=True)
            records.append(json.loads(proof_path(engine, args.long).read_text(encoding='utf-8')))
        evidence = {**records[0], 'runs': records[0]['runs'] + records[1]['runs'],
                    'cold_process_isolation': 'Each engine cold/hot pair runs in a fresh Python process.'}
        first, second = evidence['runs'][0]['transcription']['text'], evidence['runs'][2]['transcription']['text']
        evidence['output_comparison'] = {'identical_text': first == second,
            'character_sequence_similarity': round(difflib.SequenceMatcher(None, first, second, autojunk=False).ratio(), 5),
            'interpretation': 'Output difference only; neither engine is an independent ground-truth reference.'}
        evidence['hot_speedup'] = round(evidence['runs'][1]['wall_seconds'] / evidence['runs'][3]['wall_seconds'], 3)
        output = proof_path('both', args.long)
        output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'proof': str(output), 'runs': len(evidence['runs'])}), flush=True)
        return
    output = proof_path(args.engine, args.long)
    sample = SAMPLE
    if args.long:
        sample = OUTPUT.parent / 'asr-public-repeat-7.wav'
        sample.parent.mkdir(parents=True, exist_ok=True)
        _run(['ffmpeg', '-v', 'error', '-nostdin', '-y', '-stream_loop', '6', '-i', str(SAMPLE),
              '-t', '179', '-vn', '-ac', '1', '-ar', '16000', '-c:a', 'pcm_s16le', str(sample)], timeout=120)
    engines = [LEGACY_ENGINE, FAST_ENGINE] if args.engine == 'both' else [FAST_ENGINE if args.engine == 'fast' else LEGACY_ENGINE]
    evidence = {'schema_version': 'asr-performance.v1', 'created_at': datetime.now(timezone.utc).isoformat(),
        'sample': 'data/small-question-video.mp4 (bundled public repository sample)',
        'sample_sha256': hashlib.sha256(SAMPLE.read_bytes()).hexdigest(),
        'repeat_count': 7 if args.long else 1, 'analyzed_media_sha256': hashlib.sha256(sample.read_bytes()).hexdigest(),
        'environment': {'platform': platform.platform(), 'python': platform.python_version()}, 'runs': [],
        'limitations': ['One public English sample, not Chinese interview-domain accuracy validation.',
            'No independent reference transcript; output difference is not a WER/accuracy measure.',
            'CPU threads=4; fast uses greedy beam=1 with the same temperature fallback / best_of=5 and error thresholds as legacy defaults.',
            'Cold includes package/model load; hot repeats in one process. No result reuse.']}
    for engine in engines:
        for repetition in ('cold', 'hot'):
            started = time.perf_counter()
            result = analyze_media(sample, 'en', asr_engine=engine)
            elapsed = time.perf_counter() - started
            row = {'requested_engine': engine, 'repetition': repetition, 'wall_seconds': round(elapsed, 3),
                'media_duration_ms': result['duration_ms'], 'real_time_factor': round(elapsed / (result['duration_ms'] / 1000), 4),
                'transcription': result['transcription'], 'model_runs': result['model_runs']}
            evidence['runs'].append(row)
            print(json.dumps({'engine': engine, 'repetition': repetition, 'seconds': row['wall_seconds'],
                'actual_engine': result['transcription'].get('provenance', {}).get('engine'),
                'status': result['transcription']['status'], 'segment_count': len(result['transcription'].get('segments', []))}), flush=True)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    if len(engines) == 2:
        first, second = evidence['runs'][0]['transcription']['text'], evidence['runs'][2]['transcription']['text']
        evidence['output_comparison'] = {'identical_text': first == second,
            'character_sequence_similarity': round(difflib.SequenceMatcher(None, first, second, autojunk=False).ratio(), 5),
            'interpretation': 'Output difference only; neither engine is an independent ground-truth reference.'}
        evidence['hot_speedup'] = round(evidence['runs'][1]['wall_seconds'] / evidence['runs'][3]['wall_seconds'], 3)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'proof': str(output), 'runs': len(evidence['runs'])}), flush=True)


def proof_path(engine, long_sample=False):
    name = 'asr-v3-' + ('long-' if long_sample else '') + 'performance'
    return OUTPUT.with_name(name + ('' if engine == 'both' else '-' + engine) + '.json')


def worker_smoke():
    import shutil
    from unittest.mock import patch
    from interview_core import InterviewCore
    from interview_worker import PersistentMediaProcess, process_job
    rows = []
    with tempfile.TemporaryDirectory(prefix='zhenjian-ASR-公开样片-') as folder, \
         patch.dict(os.environ, {'DASHSCOPE_API_KEY': '', 'INTERVIEW_CREDENTIALS_FILE': str(ROOT / 'local-data' / 'asr-test-no-credentials')}):
        core = InterviewCore(folder, ROOT / 'config' / 'interview.v2.json')
        core.config['asr_engine'] = FAST_ENGINE
        media = core.data_dir / 'media' / SAMPLE.name
        shutil.copyfile(SAMPLE, media)
        process = PersistentMediaProcess()
        try:
            process.start()
            for i in range(2):
                session = core.create_interview(str(uuid.uuid4()), 'public-asr-fixture')
                answer = core.submit_answer(session['session_id'], session['current_round']['round_id'],
                    str(uuid.uuid4()), str(uuid.uuid4()), path=media,
                    media_hash=hashlib.sha256(media.read_bytes()).hexdigest(), language='en',
                    media_meta={'duration_ms': 23333, 'mime_type': 'video/mp4'})
                job = core.claim_job('public-asr-check', job_type='media')
                started = time.perf_counter()
                process_job(core, job, 60, media_process=process)
                state = core.get_report(session['session_id'])
                current = next(a for a in state['answers'] if a['answer_id'] == answer['answer_id'])
                if not current.get('analysis'):
                    raise RuntimeError(json.dumps([{'status': j['status'], 'error': j.get('error')} for j in state['jobs']], ensure_ascii=False))
                rows.append({'answer_number': i + 1, 'child_pid': process.proc.pid,
                    'seconds': round(time.perf_counter() - started, 3),
                    'transcript_revision': current['transcript_revision'],
                    'status': current['analysis']['transcription']['status'],
                    'engine': current['analysis']['transcription']['provenance']['engine'],
                    'temp_directory_empty': not any((core.data_dir / 'worker-tmp').iterdir())})
        finally:
            process.close()
    evidence = {'schema_version': 'asr-worker-smoke.v1', 'created_at': datetime.now(timezone.utc).isoformat(),
        'sample': 'public bundled sample only; isolated sessions deleted after validation', 'runs': rows,
        'same_child_reused': len({r['child_pid'] for r in rows}) == 1}
    output = OUTPUT.with_name('asr-v3-worker-smoke.json')
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(evidence), flush=True)


if __name__ == '__main__':
    main()
