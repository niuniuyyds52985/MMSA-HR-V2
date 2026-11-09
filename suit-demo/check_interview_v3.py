"""Explicit v3 local integration check using only the pinned public video.

Runs one new synthetic session in a dedicated QA database, real local ASR and
both observation worker phases. It never starts a content/cloud worker. Tokens,
transcripts and original observations are excluded from the proof/stdout.
The QA HTTP service must already run on 127.0.0.1:8767 with the same data dir.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
import uuid

import requests


ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'local-data' / 'interview-v3-validation'
BASE = 'http://127.0.0.1:8767'
PUBLIC_VIDEO = ROOT / 'data' / 'small-question-video.mp4'
PUBLIC_SHA256 = '0c4b2671d2da224c3beda2120b01b4305698349a4ef07f9bfb7c782adbcd593d'
ALLOWED_MODULES = {'core': {'au', 'gaze', 'expression', 'audio'},
                   'extended': {'gaze360', 'emotic'}}


def key():
    return uuid.uuid4().hex


def require(condition, code):
    if not condition:
        # Codes only: failure messages must not dump transcripts or credentials.
        raise RuntimeError(code)


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write_proof(value):
    folder = DATA / 'proofs'
    folder.mkdir(exist_ok=True)
    path = folder / ('v3-' + value['session_id'] + '.json')
    temporary = path.with_suffix('.writing')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)
    return path


def http(core, sid, route='', *, role='hr', expected=200):
    response = requests.get(BASE + '/v1/interviews/' + sid + route,
        headers={'Authorization': 'Bearer ' + core._token(sid, role)}, timeout=60)
    require(response.status_code == expected, 'http_status_' + str(response.status_code))
    return response.json()


def own_queues_only(core, sid):
    with core._db() as db:
        require(not db.execute("SELECT 1 FROM jobs WHERE job_type='media' AND status IN ('queued','running') AND session_id!=?", (sid,)).fetchone(), 'foreign_media_queue')
        require(not db.execute("SELECT 1 FROM research_runs WHERE status IN ('queued','running') AND session_id!=?", (sid,)).fetchone(), 'foreign_research_queue')


def module_proof(name, module):
    rows = module.get('observations', [])
    valid = [row for row in rows if row.get('quality') == 'valid']
    require(module.get('status') in ('complete', 'partial'), 'module_unavailable_' + name)
    require(module.get('model_id') and module.get('provenance'), 'module_source_missing_' + name)
    require(valid, 'no_valid_observations_' + name)
    if name in ('gaze360', 'emotic'):
        require(all(row.get('capture', {}).get('boxes') for row in valid), 'capture_missing_' + name)
    if name == 'emotic':
        require(all(set(row.get('values', {})) <= {'category_scores', 'category_thresholds', 'above_threshold'} for row in rows), 'emotic_output_not_filtered')
        require(all(set(row['values'].get('category_scores', {})) <= {'Anger', 'Happiness', 'Sadness', 'Surprise'} for row in rows), 'emotic_category_not_filtered')
    return {'model_id': module['model_id'], 'status': module['status'],
            'valid_observations': len(valid), 'sampled_observations': len(rows),
            'source_metadata_sha256': sha(module['provenance']),
            'elapsed_seconds': module.get('elapsed_seconds')}


def verify_completed(core, store, sid):
    """Read-only checks also usable after a report/UI fix without inference."""
    state = core.get_state(sid)
    require(state['candidate_ref'] == 'synthetic-v3-public-video', 'not_own_synthetic_session')
    require(state['config_version'] == 'interview.v3', 'wrong_config')
    require(state['status'] == 'interview_finished', 'interview_not_finished')
    require(state['budget']['main'] == 3 and state['budget']['followup'] == 1 and state['budget']['total'] == 4, 'wrong_round_budget')
    answers = [a for a in state['answers'] if a['current']]
    require(len(answers) == 4, 'wrong_answer_count')
    first = next(a for a in answers if a['has_media'])
    require(first['media_hash'] == PUBLIC_SHA256, 'not_public_video')
    require(first['transcript_revision'] == 2 and first['transcript_source'] == 'asr', 'asr_did_not_create_rev2')
    require(all(a['transcript_source'] == 'candidate' for a in answers if not a['has_media']), 'non_synthetic_later_input')
    content = [j for j in state['jobs'] if j['job_type'] == 'content']
    require(len(content) >= 4 and all(j['status'] == 'queued' and j['attempts'] == 0 for j in content), 'content_worker_ran')
    report = http(core, sid, '/report')
    require(report['session_id'] == sid, 'wrong_http_database')
    standard = report['standard_analysis']
    require(standard['pending_count'] == 0, 'observation_phases_pending')
    require(len(standard['runs']) == 2, 'phases_duplicated')
    result = {'round_budget': state['budget'], 'report_version': report['version'],
              'content_queued_without_blocking': len(content), 'automatic_report_publication': True,
              'media_revision': {'original': 1, 'current': 2}, 'phases': {}}
    for run in standard['runs']:
        phase = run['phase']
        require(phase in ALLOWED_MODULES, 'unknown_phase')
        require(run['status'] in ('succeeded', 'partial'), 'phase_not_successful_' + phase)
        require(run['analysis_input_transcript_revision'] == 1 and run['transcript_revision'] == 2, 'wrong_revision_projection')
        require(run['attempt'] == 1, 'phase_repeated')
        outputs = run['result']['multimodal']
        require(len(outputs) == 1, 'wrong_phase_answer_count')
        output = outputs[0]
        require(set(output['modules']) == ALLOWED_MODULES[phase], 'wrong_phase_modules')
        summary = output.get('reader_summary') or {}
        require(summary.get('source_run_id') == run['run_id'] and summary.get('answer_id') == first['answer_id'], 'reader_summary_source_missing')
        require(summary.get('transcript_revision') == 2, 'reader_summary_wrong_revision')
        require(summary.get('validation', {}).get('status') == 'unvalidated_for_current_domain', 'unsupported_accuracy_claim')
        require(summary.get('events'), 'no_readable_events_' + phase)
        require(all(e.get('evidence_refs') and e.get('last_input_ms') is not None for e in summary['events']), 'reader_event_evidence_missing')
        raw = store.get_run(sid, run['run_id'])['result']['multimodal'][0]
        require(raw['transcript_revision'] == 1 and 'reader_summary' not in raw, 'frozen_raw_result_changed')
        if phase == 'core':
            require(raw['modules']['audio']['summary']['transcript_units'] in (0, None), 'raw_rev1_rate_changed')
            require(output['modules']['audio']['summary']['transcript_units'] > 0, 'current_text_rate_not_projected')
            require(any(e.get('geometry') for e in summary['events']), 'actual_geometry_missing')
        result['phases'][phase] = {'run_id': run['run_id'], 'status': run['status'],
            'analysis_revision': 1, 'projected_revision': 2, 'attempt': 1,
            'modules': {name: module_proof(name, module) for name, module in output['modules'].items()},
            'readable_events': len(summary['events']),
            'events_with_actual_geometry': sum(bool(e.get('geometry')) for e in summary['events'])}
    with core._db() as db:
        require(db.execute('SELECT count(*) FROM research_publications WHERE session_id=?', (sid,)).fetchone()[0] == 0, 'manual_publish_was_used')
        require(db.execute('SELECT count(*) FROM standard_media_runs WHERE session_id=?', (sid,)).fetchone()[0] == 2, 'media_phases_repeated')
        events = [json.loads(r[0]) for r in db.execute('SELECT payload FROM decision_events WHERE session_id=? ORDER BY rowid', (sid,))]
    require(len(events) == 4 and all(not e.get('model_run_ids') for e in events), 'decision_model_dependency')
    require(any(e.get('decision_source') == 'dictionary-bank.v1' and e.get('selected_branch_id') for e in events), 'no_dictionary_followup')
    result['decisions'] = [{k: e.get(k) for k in ('question_id', 'checkpoint_kind', 'decision_source', 'action', 'selected_branch_id', 'stop_reason')} for e in events]
    result['formal_score_unchanged'] = report.get('total_score') is None
    require(result['formal_score_unchanged'], 'observations_became_scores')
    http(core, sid, '/report', role='candidate', expected=403)
    http(core, sid, '/research/runs/' + standard['runs'][0]['run_id'], role='candidate', expected=403)
    result['candidate_report_and_raw_run_http_403'] = True
    compact = http(core, sid, '/report?compact=true')
    require(all(o.get('reader_summary', {}).get('events') for r in compact['standard_analysis']['runs'] for o in r['result']['multimodal']), 'compact_reader_summary_missing')
    result['compact_report_has_readable_events'] = True
    return result


def run():
    from interview_core import InterviewCore
    from interview_media import inspect_media
    from interview_research import ResearchStore
    from interview_research_worker import process_research_job
    from interview_worker import PersistentMediaProcess, process_job

    require(DATA.resolve().parent == (ROOT / 'local-data').resolve(), 'wrong_qa_path')
    require(hashlib.sha256(PUBLIC_VIDEO.read_bytes()).hexdigest() == PUBLIC_SHA256, 'public_video_hash_changed')
    health = requests.get(BASE + '/health', timeout=10)
    require(health.status_code == 200 and health.json().get('config_version') == 'interview.v3', 'qa_service_unavailable_or_wrong_config')
    core = InterviewCore(DATA, ROOT / 'config' / 'interview.v3.json')
    store = ResearchStore(core)
    start = time.perf_counter()
    session = core.create_interview(key(), 'synthetic-v3-public-video')
    sid = session['session_id']
    proof = {'schema_version': 'interview-v3-integration-proof.v1', 'status': 'running',
        'checked_at': datetime.now(timezone.utc).isoformat(), 'session_id': sid,
        'public_media_sha256': PUBLIC_SHA256, 'cloud_calls': 0,
        'scope': 'One pinned public clip and three synthetic text answers; real local worker subprocesses and HTTP report. Not model accuracy validation.',
        'performance_conditions': 'ASR, core observations and extended observations overlap on this CPU; other machine activity is uncontrolled. Not an isolated latency benchmark.',
        'stages': {}, 'next_round_seconds': []}
    write_proof(proof)
    media_process = PersistentMediaProcess()
    try:
        own_queues_only(core, sid)
        require(http(core, sid, role='candidate')['candidate_ref'] == 'synthetic-v3-public-video', 'qa_database_mismatch')
        path = DATA / 'media' / (key() + '.mp4')
        shutil.copyfile(PUBLIC_VIDEO, path)
        metadata = inspect_media(path)
        proof['media_duration_ms'] = metadata['duration_ms']
        media_process.start()
        first = core.submit_answer(sid, session['current_round']['round_id'], key(), key(),
            path=path, media_hash=PUBLIC_SHA256, media_meta=metadata, language='en')
        require(first['transcript_revision'] == 1 and not first['transcript'], 'first_answer_not_pending_asr')
        proof['answer_id'] = first['answer_id']
        scheduled = store.enqueue_standard()
        require(len(scheduled) == 2 and store.enqueue_standard() == [], 'enqueue_not_idempotent')
        claimed = {phase: store.claim(lane=phase) for phase in ('core', 'extended')}
        require(all(j and j['input']['input_versions'] == {first['answer_id']: 1} for j in claimed.values()), 'phase_not_claimed_before_asr')
        require(all(not j['input']['cloud_authorized'] for j in claimed.values()), 'cloud_authorized')
        require(all(store.claim(lane=phase) is None for phase in claimed), 'phase_claim_not_idempotent')
        proof['queued_before_asr'] = {'phases': list(claimed), 'input_revision': 1,
            'media_status': core.get_state(sid)['answers'][0]['job_status'], 'same_phase_idempotent': True}
        media = core.claim_job('synthetic-v3-fastlane', job_type='media')
        require(media and media['session_id'] == sid, 'foreign_media_claim')
        require(media.get('asr_engine') == 'faster-whisper-base-int8.v1', 'wrong_asr_engine')
        before = http(core, sid, '/report')
        before_digest, before_version = sha(before), before['version']
        first_next_key = key()
        waiting = core.next_round(sid, first_next_key, first['round_id'])
        require(waiting['decision_pending'] and waiting['current_round']['round_id'] == first['round_id'], 'empty_asr_did_not_wait')
        proof['asr_wait_cap_seconds'] = round(waiting['decision_wait_remaining'])
        concurrent_start = time.perf_counter()

        def measured(name, function, *args, **kwargs):
            began = time.perf_counter()
            function(*args, **kwargs)
            return {'started_after_seconds': round(began - concurrent_start, 3),
                    'elapsed_seconds': round(time.perf_counter() - began, 3)}

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {'media': executor.submit(measured, 'media', process_job, core, media, 300, media_process)}
            for phase, job in claimed.items():
                futures[phase] = executor.submit(measured, phase, process_research_job, core, store, job, timeout=900)
            # Match candidate polling: only the local ASR checkpoint may wait.
            while True:
                time.sleep(.4)
                next_started = time.perf_counter()
                state = core.next_round(sid, first_next_key, first['round_id'])
                if not state['decision_pending']:
                    proof['next_round_seconds'].append({'question': 'first_media_answer',
                        'checkpoint_wall_seconds': round(time.perf_counter() - concurrent_start, 3),
                        'final_call_seconds': round(time.perf_counter() - next_started, 4)})
                    break
                require(time.perf_counter() - concurrent_start < 45, 'asr_checkpoint_exceeded_cap')
            require(core.next_round(sid, first_next_key, first['round_id']) == state, 'next_not_idempotent')
            # Every later input is explicit synthetic text. No repeated video.
            while state['status'] == 'answering':
                current = state['current_round']
                text = {'results_K6': '这是合成测试回答。我先计算剩余目标，分两组尝试不同获客方法，每两天检查实际结果，在原预算内调整。',
                        'learning_F10_Q2': '这是合成测试回答。我从失败中总结出先做小规模试验再投入的方法，并在另一个项目中使用。'}.get(current['question_id'],
                        '这是合成测试回答。我把工作分成三部分，给每个人安排交付时间，每天核对进度，最终按时完成。')
                answer = core.submit_answer(sid, current['round_id'], key(), key(), text)
                require(answer['content_status'] == 'queued', 'synthetic_content_not_queued')
                next_started, advance_key = time.perf_counter(), key()
                state = core.next_round(sid, advance_key, current['round_id'])
                latency = time.perf_counter() - next_started
                require(not state['decision_pending'], 'content_queue_blocked_next')
                require(core.next_round(sid, advance_key, current['round_id']) == state, 'later_next_not_idempotent')
                proof['next_round_seconds'].append({'question': current['question_id'], 'seconds': round(latency, 4), 'content_status_at_advance': 'queued'})
            proof['candidate_flow_finished_after_seconds'] = round(time.perf_counter() - concurrent_start, 3)
            write_proof(proof)
            print(json.dumps({'stage': 'candidate_flow_finished', 'session_id': sid, 'seconds': proof['candidate_flow_finished_after_seconds']}, ensure_ascii=False), flush=True)
            seen = set()
            last_notice = time.monotonic()
            while len(seen) < len(futures):
                for name, future in futures.items():
                    if name in seen or not future.done():
                        continue
                    proof['stages'][name] = future.result()
                    seen.add(name)
                    if name != 'media':
                        interim = core.get_report(sid)['standard_analysis']
                        proof['stages'][name]['automatically_visible_at_completion'] = any(r['run_id'] == claimed[name]['run_id'] and r.get('result') for r in interim['runs'])
                        proof['stages'][name]['remaining_observation_phases'] = interim['pending_count']
                    write_proof(proof)
                    print(json.dumps({'stage': name + '_completed', **proof['stages'][name]}, ensure_ascii=False), flush=True)
                if time.monotonic() - last_notice >= 20:
                    print(json.dumps({'stage': 'local_workers_running', 'completed': sorted(seen), 'elapsed_seconds': round(time.perf_counter() - concurrent_start, 1)}), flush=True)
                    last_notice = time.monotonic()
                if len(seen) < len(futures):
                    time.sleep(.5)
        require(sha(http(core, sid, '/report?version=' + str(before_version))) == before_digest, 'historical_report_changed')
        require(store.enqueue_standard() == [], 'asr_revision_requeued_media')
        require(all(store.claim(lane=phase) is None for phase in claimed), 'completed_phase_reclaimed')
        proof.update(verify_completed(core, store, sid))
        proof.update(status='passed', historical_report_preserved=True,
                     no_repeat_after_asr_revision=True, elapsed_seconds=round(time.perf_counter() - start, 3))
        path = write_proof(proof)
        current = DATA / 'v3-integration-current.json'
        current.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': proof['status'], 'session_id': sid, 'proof': str(path.relative_to(ROOT)),
                          'elapsed_seconds': proof['elapsed_seconds'], 'phases': list(proof['phases'])}), flush=True)
        return 0
    except Exception as error:
        proof.update(status='failed', error_code=str(error) if isinstance(error, RuntimeError) else type(error).__name__,
                     elapsed_seconds=round(time.perf_counter() - start, 3))
        write_proof(proof)
        print(json.dumps({'status': 'failed', 'session_id': sid, 'error_code': proof['error_code']}, ensure_ascii=False), flush=True)
        return 1
    finally:
        media_process.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--run', action='store_true', help='Create one synthetic session and execute real local models')
    group.add_argument('--verify-session', help='Read-only final checks; never execute models again')
    args = parser.parse_args()
    os.environ['INTERVIEW_CREDENTIALS_FILE'] = str(DATA / 'intentionally-absent-no-cloud.json')
    os.environ['DASHSCOPE_API_KEY'] = ''
    os.environ['HF_HUB_OFFLINE'] = '1'
    os.environ['TRANSFORMERS_OFFLINE'] = '1'
    if args.verify_session:
        require(re.fullmatch(r'[a-f0-9]{32}', args.verify_session) is not None, 'invalid_session_id')
        from interview_core import InterviewCore
        from interview_research import ResearchStore
        core = InterviewCore(DATA, ROOT / 'config' / 'interview.v3.json')
        result = verify_completed(core, ResearchStore(core), args.verify_session)
        print(json.dumps({'status': 'passed', 'mode': 'read_only', 'session_id': args.verify_session,
                          'phases': list(result['phases'])}), flush=True)
        return 0
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
