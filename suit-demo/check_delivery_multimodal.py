"""Explicit local-only research on an existing repository-media QA session."""
import hashlib
import hmac
import json
from pathlib import Path
import time
import uuid
import requests
import argparse
from datetime import datetime, timezone

parser=argparse.ArgumentParser();parser.add_argument('--retry-local',action='store_true');args=parser.parse_args()
root=Path(__file__).resolve().parent
sid='2b5b702fee18446385994a8100a8bdcd'
secret=(root/'interview-data/.token-secret').read_bytes()
token=hmac.new(secret,(sid+':hr').encode(),hashlib.sha256).hexdigest()
base='http://127.0.0.1:8766/v1/interviews/'+sid
headers={'Authorization':'Bearer '+token}
def call(method,path,data=None):
    r=requests.request(method,base+path,json=data,headers=headers,timeout=20);r.raise_for_status();return r.json()
report=call('GET','/report')
assert report['candidate_ref']=='开发测试：Suit仓库样例媒体，不做招聘评分'
answer=next(a for a in report['answers']if a['current']and a['has_media'])
created=call('POST','/research/runs',{'version':report['version'],'selections':[{'answer_id':answer['answer_id'],'transcript_revision':answer['transcript_revision']}],
    'modules':['word_frequency','au','gaze','expression','audio'],'repeats':1 if args.retry_local else 2,'purpose':'仓库样例本地多模态交付验收，不发云','condition':'同原片同版本独立运行'+('，超时后显式新增一次'if args.retry_local else ''),'participant_group':'QA_REPOSITORY_SAMPLE','cloud_consent':False,'idempotency_key':str(uuid.uuid4())})
ids=[r['run_id']for r in created['runs']]
if args.retry_local:
    previous=json.loads((root/'local-data/interview-validation/delivery-multimodal-queue-check.json').read_text(encoding='utf-8'))
    assert previous['session_id']==sid and previous['transcript_revision']==answer['transcript_revision']
    ids=[r['run_id']for r in previous['runs']]+ids
print(json.dumps({'event':'queued','session_id':sid,'run_ids':ids}),flush=True)
deadline=time.monotonic()+600
while True:
    runs=[call('GET','/research/runs/'+rid)for rid in ids]
    if all(r['status']in('succeeded','partial','failed')for r in runs):break
    if time.monotonic()>deadline:raise RuntimeError('research_timeout')
    time.sleep(2)
proof={'checked_at':datetime.now(timezone.utc).isoformat(),'session_id':sid,'answer_id':answer['answer_id'],'transcript_revision':answer['transcript_revision'],'cloud_requests':0,'proof_kind':'real_repository_sample_local_queue','runs':[]}
for run in runs:
    proof['runs'].append({'run_id':run['run_id'],'status':run['status'],'duration_seconds':(run['result']or{}).get('duration_seconds'),'modules':[{name:{'status':m['status'],'observations':len(m['observations']),'model_id':m.get('model_id')}for name,m in mm['modules'].items()}for mm in(run['result']or{}).get('multimodal',[])]})
proof['complete_independent_runs']=sum(r['status']=='succeeded'for r in runs)
proof['partial_runs_retained']=sum(r['status']=='partial'for r in runs)
proof['passed']=proof['complete_independent_runs']>=2
if proof['passed']:
    report=call('GET','/report');old_version=report['version']
    new=call('POST','/research/publish',{'version':old_version,'run_ids':ids,'note':'仓库演示素材两次独立本地推理，供工程核验，不作能力评价。','idempotency_key':str(uuid.uuid4())})
    proof.update(report_version=new['version'],old_report_unchanged=call('GET','/report?version='+str(old_version))==report)
(root/'local-data/interview-validation/delivery-multimodal-queue-check.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(proof,ensure_ascii=False),flush=True)
assert proof['passed']
