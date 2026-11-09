"""Explicit isolated HTTP validation. Only repository media and synthetic answers.

Requires the local service and both workers pointed at product-upgrade-validation.
Never prints role credentials or reads participant data.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import time
import uuid
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'local-data' / 'product-upgrade-validation'
BASE = 'http://127.0.0.1:8767'

def key(): return str(uuid.uuid4())

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--resume-session')
    args = parser.parse_args()
    if args.resume_session:
        sid = args.resume_session
        secret = (DATA / '.token-secret').read_bytes()
        tokens = {r: hmac.new(secret, (sid + ':' + r).encode(), hashlib.sha256).hexdigest() for r in ('candidate', 'hr')}
    else:
        created = requests.post(BASE + '/v1/interviews', json={'candidate_ref': '开发验证｜产品升级', 'idempotency_key': key()}, timeout=10)
        created.raise_for_status(); created = created.json(); sid = created['session_id']
        tokens = {r: created[r + '_token'] for r in ('candidate', 'hr')}
    prefix = BASE + '/v1/interviews/' + sid
    def call(method, path, body=None, role='hr', form=None, raw=None, headers=None):
        response = requests.request(method, prefix + path, json=body, data=raw if raw is not None else form,
            headers={'Authorization': 'Bearer ' + tokens[role], **(headers or {})}, timeout=30)
        if not response.ok:
            detail = response.json().get('detail', {})
            raise RuntimeError(str(response.status_code) + ' ' + path + ' ' + (detail.get('code', 'request_error') if isinstance(detail, dict) else str(detail)))
        return response.json()
    state = call('GET', '')
    assert state['candidate_ref'] == '开发验证｜产品升级'
    proof = {'checked_at': datetime.now(timezone.utc).isoformat(), 'session_id': sid,
        'scope': 'isolated repository English video and synthetic Chinese text; real workers/provider; no participant or validity assessment', 'checks': {}}
    out = DATA / 'product-upgrade-check.json'
    def save(): out.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    def note(name, value=True):
        proof['checks'][name] = value; save(); print(name + ': ' + str(value), flush=True)
    media_uploaded = any(a.get('has_media') for a in state.get('answers', []))
    while True:
        state = call('GET', '')
        current = state.get('current_round')
        if not current: break
        answer = next((a for a in state['answers'] if a['round_id'] == current['round_id'] and a['current']), None)
        if not answer:
            if not media_uploaded:
                media = (ROOT / 'data' / 'small-question-video.mp4').read_bytes()
                size = 512 * 1024
                request = {'round_id': current['round_id'], 'attempt_id': key(), 'filename': 'repository-English-sample.mp4',
                    'total_size': len(media), 'chunk_size': size, 'sha256': hashlib.sha256(media).hexdigest(), 'idempotency_key': key()}
                upload = call('POST', '/uploads', request, role='candidate')
                uid = upload['upload_id']; chunks = [media[i:i + size] for i in range(0, len(media), size)]
                def send(index):
                    data = chunks[index]
                    return call('PUT', '/uploads/' + uid + '/chunks/' + str(index), role='candidate', raw=data,
                        headers={'Content-Type': 'application/octet-stream', 'X-Chunk-SHA256': hashlib.sha256(data).hexdigest()})
                send(0)
                recovered = call('GET', '/uploads/' + uid, role='candidate')
                assert 0 in recovered['received_chunks']
                for i in range(1, len(chunks)): send(i)
                send(0)
                complete = {'transcript': '', 'language': 'en', 'idempotency_key': key()}
                answer = call('POST', '/uploads/' + uid + '/complete', complete, role='candidate')
                again = call('POST', '/uploads/' + uid + '/complete', complete, role='candidate')
                assert again['answer_id'] == answer['answer_id']
                media_uploaded = True
                note('chunk_resume_duplicate_complete', {'chunks': len(chunks), 'answer_id': answer['answer_id']})
            else:
                text = ('这是合成流程验收回答，不是真人能力评定。我先确认任务目标和验收标准，拆出负责人、截止时间和每日检查点。'
                    '数据不足时先向同事核实，发现重复统计后增加去重检查，并用独立计算核对。资源冲突时与成员协调优先级，'
                    '记录自己的行动和团队分工。最终按期交付，复盘反馈后更新流程；新情境仍需先验证，不能照搬。')
                answer = call('POST', '/answers', role='candidate', form={'round_id': current['round_id'],
                    'attempt_id': key(), 'idempotency_key': key(), 'transcript': text})
        deadline = time.monotonic() + 240
        while True:
            report = call('GET', '/report')
            jobs = [j for j in report['jobs'] if j['job_type'] == 'content' and j['answer_id'] == answer['answer_id'] and j['status'] != 'superseded']
            if jobs and jobs[-1]['status'] in ('succeeded', 'failed', 'blocked'): break
            if time.monotonic() > deadline: raise RuntimeError('content_timeout')
            time.sleep(2)
        note('content_' + current['question_id'], jobs[-1]['status'])
        assert jobs[-1]['status'] == 'succeeded'
        call('POST', '/next', {'round_id': current['round_id'], 'idempotency_key': key()}, role='candidate')
    deadline = time.monotonic() + 240
    while True:
        report = call('GET', '/report'); auto = report['standard_analysis']
        if auto['runs'] and not auto['pending_count']: break
        if time.monotonic() > deadline: raise RuntimeError('multimodal_timeout')
        time.sleep(2)
    assert report['status'] == 'interview_finished'
    media_answers = [a for a in report['answers'] if a['current'] and a['has_media']]
    assert len(media_answers) == 1
    run = next(r for r in auto['runs'] if r['answer_id'] == media_answers[0]['answer_id'])
    assert run['status'] == 'succeeded', run.get('error')
    item = run['result']['multimodal'][0]
    geometry = item['modules']['gaze']['geometry']['observations']
    assert any(len(g['landmarks']) == 68 for g in geometry)
    note('automatic_real_multimodal', {m: v['status'] for m, v in item['modules'].items()})
    note('real_landmark_frames', len(geometry))
    status = call('GET', '/analysis-status')
    assert all('result' not in r for r in status['multimodal']['runs'])
    note('lightweight_poll')
    frozen = report; old_version = report['version']
    for i, q in enumerate(report['questions']):
        a = q['answers'][0]
        report = call('POST', '/reviews', {'version': report['version'], 'reason': '开发验收逐题保存；非真实评定。',
            'ratings': [{'question_id': q['question_id'], 'score': 3, 'reason': '合成验收记录，验证保存与版本。',
                'evidence': [{'answer_id': a['answer_id'], 'transcript_revision': a['transcript_revision'], 'quote': a['transcript'][:100]}]}],
            'reviewer': 'QA_SYNTHETIC', 'idempotency_key': key()})
        reviewed = sum(q['score_source'] == 'hr' and not q['review_stale'] for q in report['questions'])
        assert reviewed == i + 1
    assert call('GET', '/report?version=' + str(old_version)) == frozen
    note('partial_reviews_preserve_other_questions_and_frozen_report')
    response = requests.get(prefix + '/report', headers={'Authorization': 'Bearer ' + tokens['candidate']}, timeout=10)
    assert response.status_code == 403
    note('candidate_cannot_read_hr_report')
    proof.update(status='passed', main_count=sum(r['kind'] == 'main' for r in report['rounds']),
        followup_count=sum(r['kind'] == 'followup' for r in report['rounds']), report_version=report['version'])
    save(); print('product_upgrade: passed', flush=True)

if __name__ == '__main__': main()
