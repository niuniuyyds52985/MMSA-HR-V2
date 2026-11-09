"""Explicit local HTTP/model/report/migration check on an existing QA fixture.

The service must use product-upgrade-validation on port 8767. Only the fixed
repository-video session is accepted. No content worker or cloud call is used.
"""
from datetime import datetime, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import time
import uuid
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'local-data' / 'product-upgrade-validation'
BASE = 'http://127.0.0.1:8767'
SID = 'f0bfaab0d3574e87af0565cb3748c2c6'


def main():
    os.environ['INTERVIEW_CREDENTIALS_FILE'] = str(DATA / 'qa-no-cloud-credentials.json')
    os.environ['DASHSCOPE_API_KEY'] = ''
    from interview_core import InterviewCore
    from interview_research import ResearchStore
    from interview_research_worker import process_research_job
    from delivery_tools import backup_data, restore_data
    start = time.perf_counter()
    secret = (DATA / '.token-secret').read_bytes()
    tokens = {role: hmac.new(secret, (SID + ':' + role).encode(), hashlib.sha256).hexdigest()
              for role in ('candidate', 'hr')}
    def call(method, route, body=None, role='hr', status=200):
        response = requests.request(method, BASE + '/v1/interviews/' + SID + route,
            headers={'Authorization': 'Bearer ' + tokens[role]}, json=body, timeout=30)
        if response.status_code != status:
            raise RuntimeError('QA HTTP request failed: ' + str(response.status_code) + ' ' + route)
        return response.json()
    before = call('GET', '/report')
    assert before['candidate_ref'] == '开发验证｜产品升级'
    answer = next(a for a in before['answers'] if a['current'] and a['has_media'])
    assert answer['media_hash'] == hashlib.sha256((ROOT / 'data/small-question-video.mp4').read_bytes()).hexdigest()
    core = InterviewCore(DATA)
    store = ResearchStore(core)
    with core._db() as db:
        assert not db.execute("SELECT 1 FROM research_runs WHERE status IN ('queued','running')").fetchone(), 'QA queue must be idle'
    request = {'version': before['version'], 'selections': [{'answer_id': answer['answer_id'],
        'transcript_revision': answer['transcript_revision']}], 'modules': ['gaze360', 'emotic'], 'repeats': 1,
        'purpose': 'Gaze360与EMOTIC接入工程验证', 'condition': '仓库公开视频样片，非真人能力评定',
        'participant_group': 'QA_SYNTHETIC_MODELS', 'cloud_consent': False, 'idempotency_key': str(uuid.uuid4())}
    created = call('POST', '/research/runs', request)
    run_id = created['runs'][0]['run_id']
    repeated = call('POST', '/research/runs', request)
    assert repeated == created
    job = store.claim()
    assert job and job['run_id'] == run_id
    print('qa_run_created_and_idempotent', flush=True)
    process_research_job(core, store, job, timeout=900)
    run = call('GET', '/research/runs/' + run_id)
    assert run['status'] in ('succeeded', 'partial'), run.get('error')
    modules = run['result']['multimodal'][0]['modules']
    counts = {}
    for name in ('gaze360', 'emotic'):
        module = modules[name]
        valid = [o for o in module['observations'] if o['quality'] == 'valid']
        assert valid, name + ' has no valid real inference'
        counts[name] = {'status': module['status'], 'valid_windows': len(valid),
            'total_windows': len(module['observations']), 'model_id': module['model_id']}
        assert all(o.get('capture', {}).get('boxes') for o in valid)
        assert all(o['last_input_ms'] >= o['centre_ms'] for o in valid)
    for observation in modules['emotic']['observations']:
        values = observation['values']
        assert set(values) <= {'category_scores', 'category_thresholds', 'above_threshold'}
        assert set(values.get('category_scores', {})) <= {'Anger', 'Happiness', 'Sadness', 'Surprise'}
    assert call('GET', '/report') == before, 'Independent run changed formal report'
    published = call('POST', '/research/publish', {'version': before['version'], 'run_ids': [run_id],
        'note': '两个指定模型的独立方法验证；输入为仓库公开视频样片，输出不用于能力评分。',
        'idempotency_key': str(uuid.uuid4())})
    assert published['version'] == before['version'] + 1
    assert published['total_score'] == before['total_score']
    assert call('GET', '/report?version=' + str(before['version'])) == before
    call('GET', '/research/runs/' + run_id, role='candidate', status=403)
    backup = backup_data(DATA, ROOT / 'local-data' / 'model-integration-backups')
    target = ROOT / 'local-data' / ('model-integration-restore-' + uuid.uuid4().hex[:10])
    restored = restore_data(backup['archive'], target)
    assert restored['research_artifacts_preserved'] >= 3
    proof = {'schema_version': 'model-integration-proof.v1', 'status': 'passed',
        'checked_at': datetime.now(timezone.utc).isoformat(), 'session_id': SID, 'run_id': run_id,
        'answer_id': answer['answer_id'], 'media_sha256': answer['media_hash'], 'modules': counts,
        'report_version': published['version'], 'historical_report_preserved': True,
        'formal_score_unchanged': True, 'request_idempotency': True, 'candidate_report_access_denied': True,
        'migration_artifacts_preserved': restored['research_artifacts_preserved'],
        'restore_directory': str(target.relative_to(ROOT)), 'cloud_calls': 0,
        'elapsed_seconds': round(time.perf_counter() - start, 3),
        'scope': 'Real HTTP, worker subprocess, CPU models, report publication and data copy; repository media only. Not accuracy or psychological measurement validation.'}
    output = ROOT / 'local-data/interview-validation/model-integration-current.json'
    output.write_text(json.dumps(proof, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(proof, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
