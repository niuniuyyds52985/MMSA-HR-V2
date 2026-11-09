"""Bounded live v3 scoring check: four synthetic text answers, three parent jobs.

Only the new dedicated cloud-validation database is used. The transcript of
every outbound answer must exactly match a literal fixture below. No media,
old participants, credentials, answer text or model-generated text are printed
or copied into the proof. Existing user-bound DPAPI credentials are read by the
existing provider adapter; no credential configuration is written.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.parse import urlsplit
import uuid


ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'local-data' / 'interview-v3-cloud-validation'
MARKER = 'synthetic-v3-cloud-text-only'
TEXTS = {
    'leadership_F3_Q1': '这是一条接口验收的合成回答，不是真实候选人经历。团队准备一次社团活动时，大家意见不同，讨论了很久。最后活动完成，但这一段没有说明我本人的行动。',
    'leadership_F3_Q1_action': '这是一条接口验收的合成追答，不是真实候选人经历。我先分别了解四名成员的时间和经验，把物料、场地、报名和宣传四项任务分开，明确每项负责人和截止时间。我每天检查进度；物料延期时，我联系备用供应商并承担加急协调。最难的是两名成员都希望负责宣传，我让他们分别负责文案和渠道，并约定每晚共同核对报名数。最终活动按期举行，八十名报名者中七十二名到场。复盘时我补充了备用供应商和交接清单。',
    'results_K6': '这是一条接口验收的合成回答，不是真实候选人经历。两周还差八千人，先核对转化漏斗，查清是触达少还是转化低。我把剩余目标拆到每天，暂停低转化渠道，在原预算内选两个已有社群做两天小试验，比较每个渠道的有效注册数和成本。每天检查留存，避免把重复注册算成新增。若两天后转化没有改善，就调整话术和分工；预算不变且进度仍不足时，我会向负责人说明差距和可达目标，给出替代渠道及风险。',
    'learning_F10_Q2': '这是一条接口验收的合成回答，不是真实候选人经历。一次活动采购中，我没先核对实际人数就按上次规模下单，剩余物料很多。我总结出先验证需求、设检查点再扩大投入的方法。后来做线上读书会时，我先招募十人试行一周，根据实际出席调整时间，再扩大到四十人，四周平均出席率达到八成。新方法适合需求不明的任务；若是固定期限的紧急任务，我会压缩试验到一天，并保留最低交付方案。'}
PARENTS = {'leadership_F3_Q1', 'results_K6', 'learning_F10_Q2'}


def require(value, code):
    if not value:
        raise RuntimeError(code)


def key():
    return uuid.uuid4().hex


def json_sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def decision_bytes(core, sid):
    with core._db() as db:
        return [row[0].encode('utf-8') for row in db.execute('SELECT payload FROM decision_events WHERE session_id=? ORDER BY rowid', (sid,))]


def decisions_sha(values):
    return hashlib.sha256(b'\x00'.join(values)).hexdigest()


def write_proof(proof):
    target = DATA / ('proof-' + proof['session_id'] + '.json')
    temporary = target.with_suffix('.writing')
    temporary.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(target)
    return target


def verify_synthetic(core, sid):
    state = core.get_state(sid)
    require(state['candidate_ref'] == MARKER, 'wrong_synthetic_session')
    require(state['config_version'] == 'interview.v3', 'wrong_config')
    require(state['status'] == 'interview_finished', 'interview_not_sealed')
    require(state['budget']['main'] == 3 and state['budget']['followup'] == 1 and state['budget']['total'] == 4, 'wrong_round_count')
    require(len(state['answers']) == 4, 'wrong_answer_count')
    for answer in state['answers']:
        require(answer['current'] and not answer['has_media'] and answer['transcript_revision'] == 1, 'unexpected_answer_or_media')
        require(answer['transcript'] == TEXTS.get(answer['question_id']), 'outbound_text_not_exact_synthetic_fixture')
    return state


def verify_completed(core, sid, proof):
    state = verify_synthetic(core, sid)
    report = core.get_report(sid)
    require(decisions_sha(decision_bytes(core, sid)) == proof['sealed_decision_payload_sha256'], 'sealed_decision_bytes_changed')
    require(json_sha(core.get_report(sid, proof['pre_model_report_version'])) == proof['pre_model_report_sha256'], 'historical_report_changed')
    evaluations = report['model_evaluations']
    require(len(evaluations) == 3 and {e['question_id'] for e in evaluations} == PARENTS, 'not_three_parent_evaluations')
    for evaluation in evaluations:
        require(evaluation.get('schema_version') == 'answer-evaluation.v2', 'structured_schema_missing')
        require(evaluation['provider'].get('validation') == 'passed', 'provider_contract_not_validated')
        require(type(evaluation['rating'].get('score')) is int and 1 <= evaluation['rating']['score'] <= 5, 'missing_scored_answer')
        require(evaluation['rating'].get('evidence') and evaluation.get('evidence_units') and evaluation.get('criterion_matches'), 'missing_structured_evidence')
        expected = {a['answer_id']: 1 for a in state['answers']
                    if evaluation['question_id'] in (a['question_id'], a['parent_question_id'])}
        require(evaluation['input_versions'] == expected, 'wrong_current_parent_inputs')
        require(evaluation['lifecycle'] == 'draft', 'model_output_not_draft')
    main_plus_followup = next(e for e in evaluations if e['question_id'] == 'leadership_F3_Q1')
    require(len(main_plus_followup['input_versions']) == 2, 'followup_not_merged')
    require(main_plus_followup['followup']['branch_id'] is None, 'second_followup_requested')
    require(all(j['job_type'] == 'content' for j in state['jobs']), 'unexpected_media_job')
    require(sum(j['status'] == 'succeeded' for j in state['jobs']) == 3, 'wrong_success_count')
    require(sum(j['status'] == 'superseded' for j in state['jobs']) == 1, 'old_parent_job_not_superseded')
    require(not any(j['status'] in ('queued', 'running') for j in state['jobs']), 'content_still_pending')
    require(report['standard_analysis']['runs'] == [] and all(a['status'] == 'not_applicable' for a in report['standard_analysis']['answers']), 'multimodal_unexpected')
    require(all(q['score_source'] == 'model_draft' for q in report['questions']), 'non_text_score_source')
    comparisons = []
    for event in report['decision_events']:
        require(event['decision_source'] == 'dictionary-bank.v1' and not event['model_run_ids'], 'model_changed_decision_source')
        require(event.get('model_comparison'), 'later_model_comparison_missing')
        for comparison in event['model_comparison']:
            require(comparison['role'] == 'later_comparison_not_decision_input', 'wrong_comparison_role')
            require(comparison['created_at'] > event['decision_time'], 'comparison_not_after_decision')
            comparisons.append(comparison)
    profile = report['potential_profile']
    require(len(profile['factors']) == 8 and profile['planned_count'] == 3 and profile['scored_count'] == 3, 'profile_not_three_of_eight')
    require(sum(f['score'] is None and f['state'] == 'not_measured' for f in profile['factors']) == 5, 'unmeasured_factors_filled')
    require(report['reader_summary']['scorable_main_count'] == 3 and report['reader_summary']['planned_main_count'] == 3, 'reader_score_coverage_wrong')
    require(report['reader_summary']['content_pending_count'] == 0, 'reader_content_pending')
    require(report['reviewed_question_count'] == 0 and report['report_lifecycle'] == 'draft', 'model_draft_misrepresented_as_reviewed')
    return {'report_version': report['version'], 'structured_evaluation_count': len(evaluations),
            'superseded_job_count': 1, 'succeeded_job_count': 3, 'merged_parent_answer_count': 2,
            'decision_event_count': len(report['decision_events']), 'later_comparison_count': len(comparisons),
            'sealed_decision_bytes_unchanged': True, 'historical_report_preserved': True,
            'profile_total_factors': 8, 'profile_scored_factors': 3, 'profile_unmeasured_factors': 5,
            'reader_scored_main_count': 3, 'report_lifecycle': report['report_lifecycle'],
            'media_count': 0, 'multimodal_run_count': 0,
            'providers': [{'model': e['provider'].get('model'), 'returned_model': e['provider'].get('returned_model'),
                'elapsed_ms': e['provider'].get('elapsed_ms'), 'attempt_count': e['provider'].get('attempt_count'),
                'schema_version': e['schema_version'], 'prompt_version': e['provider'].get('prompt_version'),
                'validation': e['provider']['validation']} for e in evaluations]}


def run():
    from interview_core import InterviewCore
    from interview_credentials import public_settings, get_api_key, cloud_inputs_allowed
    from interview_worker import process_job

    # Preflight is deliberately before creating a session or making a paid call.
    # Sandbox accounts cannot always decrypt the original user's DPAPI record.
    try:
        settings = public_settings()
    except Exception:
        print(json.dumps({'status': 'preflight_failed', 'error_code': 'user_bound_credential_metadata_unavailable', 'cloud_calls': 0}), flush=True)
        return 2
    host = urlsplit(settings['base_url']).hostname or ''
    require(host in {'dashscope.aliyuncs.com', 'dashscope-intl.aliyuncs.com', 'dashscope-us.aliyuncs.com'}, 'not_existing_bailian_endpoint')
    try:
        ready = bool(get_api_key(settings['base_url']))
    except Exception:
        print(json.dumps({'status': 'preflight_failed', 'error_code': 'user_bound_credential_unavailable', 'cloud_calls': 0}), flush=True)
        return 2
    require(ready, 'empty_credential')
    core = InterviewCore(DATA, ROOT / 'config' / 'interview.v3.json')
    with core._db() as db:
        require(not db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running')").fetchone(), 'qa_queue_not_empty')
        require(not db.execute('SELECT 1 FROM sessions WHERE candidate_ref!=?', (MARKER,)).fetchone(), 'foreign_session_in_qa_database')
    session = core.create_interview(key(), MARKER)
    sid, start = session['session_id'], time.perf_counter()
    proof = {'schema_version': 'interview-v3-cloud-proof.v1', 'status': 'running', 'session_id': sid,
        'checked_at': datetime.now(timezone.utc).isoformat(), 'scope': 'Four exact synthetic Chinese text fixtures; three existing Bailian structured evaluations after dictionary decisions. Not scoring accuracy or personnel validation.',
        'configured_model': settings['model'], 'content_jobs_started': 0, 'next_round_seconds': [], 'jobs': []}
    try:
        state = session
        while state['status'] == 'answering':
            current = state['current_round']
            require(current['question_id'] in TEXTS, 'unexpected_question')
            core.submit_answer(sid, current['round_id'], key(), key(), TEXTS[current['question_id']])
            checkpoint = time.perf_counter()
            state = core.next_round(sid, key(), current['round_id'])
            require(not state['decision_pending'], 'dictionary_waited_for_llm')
            proof['next_round_seconds'].append(round(time.perf_counter() - checkpoint, 4))
        state = verify_synthetic(core, sid)
        require(all(j['status'] == 'queued' and j['attempts'] == 0 for j in state['jobs']), 'model_ran_before_decision_sealed')
        frozen = decision_bytes(core, sid)
        require(len(frozen) == 4, 'missing_sealed_decisions')
        require(json.loads(frozen[0])['selected_branch_id'] == 'leadership_F3_Q1_action', 'wrong_first_followup')
        before = core.get_report(sid)
        proof.update(candidate_flow_seconds=round(time.perf_counter() - start, 3),
                     sealed_decision_payload_sha256=decisions_sha(frozen),
                     pre_model_report_version=before['version'], pre_model_report_sha256=json_sha(before),
                     decision_event_count_before_models=4, content_jobs_before_models=len(state['jobs']))
        write_proof(proof)
        print(json.dumps({'stage': 'four_synthetic_rounds_sealed', 'session_id': sid,
                          'seconds': proof['candidate_flow_seconds'], 'model_jobs_started': 0}), flush=True)
        seen_parents = set()
        for _ in range(3):
            job = core.claim_job('synthetic-v3-cloud-check', job_type='content')
            require(job and job['session_id'] == sid and job['job_type'] == 'content', 'foreign_or_missing_content_job')
            require(job['question_id'] in PARENTS and job['question_id'] not in seen_parents, 'duplicate_parent_call')
            # Check every actual outbound transcript immediately before the worker.
            require(all(a['transcript'] == TEXTS.get(a['question_id']) and not a['has_media'] for a in job['answers']), 'outbound_answer_not_exact_synthetic_fixture')
            require(cloud_inputs_allowed(job['answers']), 'historical_data_cloud_gate')
            require(job['model_config']['base_url'] == settings['base_url'] and job['model_config']['model'] == settings['model'], 'provider_binding_changed')
            seen_parents.add(job['question_id'])
            proof['content_jobs_started'] += 1
            write_proof(proof)
            began = time.perf_counter()
            process_job(core, job, timeout_seconds=150)
            with core._db() as db:
                completed = dict(db.execute('SELECT status,attempts FROM jobs WHERE id=?', (job['job_id'],)).fetchone())
            proof['jobs'].append({'job_id': job['job_id'], 'parent_question_id': job['question_id'],
                'input_answer_count': len(job['answers']), 'status': completed['status'], 'worker_attempts': completed['attempts'],
                'elapsed_seconds': round(time.perf_counter() - began, 3)})
            write_proof(proof)
            print(json.dumps({'stage': 'content_worker_completed', **proof['jobs'][-1]}), flush=True)
            require(completed['status'] == 'succeeded', 'provider_or_writeback_failed_no_extra_job_retry')
            require(decision_bytes(core, sid) == frozen, 'sealed_decision_bytes_changed')
        # Inspect rather than claim again: verification cannot initiate a paid retry.
        proof.update(verify_completed(core, sid, proof))
        proof.update(status='passed', elapsed_seconds=round(time.perf_counter() - start, 3),
                     api_attempt_count=sum(p['attempt_count'] for p in proof['providers']))
        target = write_proof(proof)
        (DATA / 'v3-cloud-current.json').write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        print(json.dumps({'status': 'passed', 'session_id': sid, 'model': settings['model'],
            'content_jobs_started': 3, 'api_attempt_count': proof['api_attempt_count'],
            'profile_scored_factors': 3, 'profile_total_factors': 8,
            'elapsed_seconds': proof['elapsed_seconds'], 'proof': str(target.relative_to(ROOT))}), flush=True)
        return 0
    except Exception as error:
        proof.update(status='failed', error_code=str(error) if isinstance(error, RuntimeError) else type(error).__name__,
                     elapsed_seconds=round(time.perf_counter() - start, 3))
        write_proof(proof)
        print(json.dumps({'status': 'failed', 'session_id': sid, 'error_code': proof['error_code'],
                          'content_jobs_started': proof['content_jobs_started']}), flush=True)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--run', action='store_true', help='Three synthetic text evaluation jobs; existing adapter may retry a request once')
    modes.add_argument('--verify-session', help='Read-only verification; no credential decryption or API request')
    args = parser.parse_args()
    from interview_credentials import DEFAULT_FILE
    # Use only the already configured, endpoint-bound DPAPI credential, not an
    # inherited alternate key, host or model environment override.
    os.environ['INTERVIEW_CREDENTIALS_FILE'] = str(DEFAULT_FILE)
    for name in ('DASHSCOPE_API_KEY', 'INTERVIEW_LLM_BASE_URL', 'INTERVIEW_LLM_MODEL'):
        os.environ.pop(name, None)
    if args.verify_session:
        require(re.fullmatch(r'[a-f0-9]{32}', args.verify_session) is not None, 'invalid_session_id')
        from interview_core import InterviewCore
        proof = json.loads((DATA / ('proof-' + args.verify_session + '.json')).read_text(encoding='utf-8'))
        checked = verify_completed(InterviewCore(DATA, ROOT / 'config' / 'interview.v3.json'), args.verify_session, proof)
        print(json.dumps({'status': 'passed', 'mode': 'read_only', 'session_id': args.verify_session,
                          'evaluations': checked['structured_evaluation_count'],
                          'profile_scored_factors': checked['profile_scored_factors'],
                          'profile_total_factors': checked['profile_total_factors']}), flush=True)
        return 0
    return run()


if __name__ == '__main__':
    raise SystemExit(main())
