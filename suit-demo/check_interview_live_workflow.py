"""Explicit synthetic HTTP -> worker -> live LLM -> persisted report check."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import time
import uuid

import requests


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True)
    parser.parse_args()
    base = 'http://127.0.0.1:8766'
    started = time.monotonic()
    created = requests.post(base + '/v1/interviews', json={
        'candidate_ref': '接口联调：合成文字，不是真实候选人', 'idempotency_key': str(uuid.uuid4())}, timeout=10)
    created.raise_for_status()
    data = created.json()
    endpoint = base + '/v1/interviews/' + data['session_id']
    text = '这是一条接口验收合成回答，不是真实经历。我负责一次社团活动，两名成员对分工有分歧。我分别了解可投入的时间，将任务按经验拆分，明确负责人和截止时间，每晚检查进度。物料延迟时，我联系备用供应商，活动如期完成。复盘时补充了备用供应商清单。'
    response = requests.post(endpoint + '/answers', headers={'Authorization': 'Bearer ' + data['candidate_token']},
        data={'round_id': data['current_round']['round_id'], 'attempt_id': str(uuid.uuid4()),
              'idempotency_key': str(uuid.uuid4()), 'transcript': text}, timeout=10)
    response.raise_for_status()
    result = {'checked_at_utc': datetime.now(timezone.utc).isoformat(), 'synthetic': True,
              'scope': 'One synthetic answer through HTTP, background worker, actual provider and report; not semantic accuracy validation',
              'session_id': data['session_id'], 'status': 'timeout'}
    while time.monotonic() - started < 120:
        response = requests.get(endpoint + '/report', headers={'Authorization': 'Bearer ' + data['hr_token']}, timeout=10)
        response.raise_for_status()
        report = response.json()
        jobs = [j for j in report['jobs'] if j['job_type'] == 'content']
        if jobs and jobs[-1]['status'] in ('succeeded', 'blocked', 'failed'):
            result.update(status=jobs[-1]['status'], jobs=jobs, model_evaluations=report['model_evaluations'],
                          automatic_scoring=report['automatic_scoring'], report_version=report['version'])
            break
        time.sleep(1)
    result['elapsed_seconds'] = round(time.monotonic() - started, 3)
    path = Path(__file__).resolve().parent / 'local-data/interview-validation/live-workflow-check.json'
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('status', 'elapsed_seconds', 'automatic_scoring') if k in result}, ensure_ascii=False))
    return 0 if result['status'] == 'succeeded' else 1


if __name__ == '__main__':
    raise SystemExit(main())
