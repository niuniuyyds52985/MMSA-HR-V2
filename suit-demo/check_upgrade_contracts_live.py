"""Explicit final live check in the isolated 8767 validation service only."""
import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import time
import uuid
import requests

ROOT=Path(__file__).resolve().parent
DATA=ROOT/'local-data/product-upgrade-validation'
BASE='http://127.0.0.1:8767'
def key():return str(uuid.uuid4())
def request(method,path,token=None,body=None,form=None):
    response=requests.request(method,BASE+path,headers={'Authorization':'Bearer '+token}if token else {},json=body,data=form,timeout=30)
    if not response.ok:raise RuntimeError(str(response.status_code)+' '+path.rsplit('/',1)[-1])
    return response.json()
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true',required=True);parser.add_argument('--media-session',required=True);args=parser.parse_args()
    secret=(DATA/'.token-secret').read_bytes();media_sid=args.media_session
    media_token=hmac.new(secret,(media_sid+':hr').encode(),hashlib.sha256).hexdigest()
    mp='/v1/interviews/'+media_sid
    media_report=request('GET',mp+'/report',media_token)
    assert media_report['candidate_ref']=='开发验证｜产品升级'
    media_answer=next(a for a in media_report['answers']if a['current']and a['has_media'])
    au=request('POST',mp+'/research/runs',media_token,{'version':media_report['version'],
        'selections':[{'answer_id':media_answer['answer_id'],'transcript_revision':media_answer['transcript_revision']}],
        'modules':['au_net'],'repeats':1,'purpose':'原AU-Net HTTP接入工程验证','condition':'仓库英文样片，非真人能力评定',
        'participant_group':'QA_SYNTHETIC','cloud_consent':False,'idempotency_key':key()})
    run_id=au['runs'][0]['run_id']
    created=request('POST','/v1/interviews',body={'candidate_ref':'开发验证｜产品升级｜追问契约','idempotency_key':key()})
    sid=created['session_id'];cp='/v1/interviews/'+sid
    state=request('GET',cp,created['candidate_token']);r=state['current_round']
    answer=request('POST',cp+'/answers',created['candidate_token'],form={'round_id':r['round_id'],'attempt_id':key(),'idempotency_key':key(),
        'transcript':'这是合成测试案例。我在一次社团迎新活动中主动报名担任组织者，因为原负责人临时请假。团队最后按计划完成了活动，参与者反馈不错。我还没有具体说明自己怎样安排成员任务。'})
    deadline=time.monotonic()+240
    while True:
        report=request('GET',cp+'/report',created['hr_token']);jobs=[j for j in report['jobs']if j['job_type']=='content'and j['answer_id']==answer['answer_id']]
        if jobs and jobs[-1]['status']in('succeeded','failed','blocked'):break
        if time.monotonic()>deadline:raise RuntimeError('content timeout')
        time.sleep(2)
    assert jobs[-1]['status']=='succeeded',jobs[-1]['status']
    evaluation=report['model_evaluations'][-1]
    assert evaluation['followup_contract_version']=='gap-branch.zh.v1'
    print('new_followup_contract: passed',flush=True)
    deadline=time.monotonic()+180
    while True:
        run=request('GET',mp+'/research/runs/'+run_id,media_token)
        if run['status']in('succeeded','partial','failed'):break
        if time.monotonic()>deadline:raise RuntimeError('au_net timeout')
        time.sleep(2)
    assert run['status']=='succeeded',run.get('error')
    module=run['result']['multimodal'][0]['modules']['au_net']
    assert module['status']=='complete'
    assert any(len(o['values']['activation_scores'])==15 for o in module['observations']if o['quality']=='valid')
    proof={'status':'passed','checked_at':datetime.now(timezone.utc).isoformat(),'scope':'isolated synthetic Chinese text and repository English video; real API/provider/workers',
        'new_content_session_id':sid,'followup_contract_version':evaluation['followup_contract_version'],
        'followup_status':evaluation['followup']['status'],'selected_branch':evaluation['followup'].get('branch_id'),
        'au_net_run_id':run_id,'au_net_windows':len(module['observations']),'au_net_status':module['status'],
        'media_session_id':media_sid,'frozen_old_report_preserved':request('GET',mp+'/report?version='+str(media_report['version']),media_token)==media_report}
    (DATA/'upgrade-contracts-live-check.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(proof,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
