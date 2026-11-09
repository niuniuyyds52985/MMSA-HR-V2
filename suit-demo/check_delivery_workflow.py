"""Explicit synthetic full HTTP -> real provider -> review -> research -> report check."""
import argparse
from datetime import datetime, timezone
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
from contextlib import closing
import time
import uuid
import requests

ROOT=Path(__file__).resolve().parent
def key():return str(uuid.uuid4())
def original_fingerprint():
    with closing(sqlite3.connect((ROOT/'interview-data/interviews.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        rows={t:db.execute('SELECT * FROM '+t+' WHERE session_id=? ORDER BY rowid',('bb2a95d644164d008be579b71f97befe',)).fetchall() for t in ('answers','reports','reviews','model_evaluations','decisions','decision_events')}
        rows['session']=db.execute('SELECT * FROM sessions WHERE id=?',('bb2a95d644164d008be579b71f97befe',)).fetchall()
    return hashlib.sha256(json.dumps(rows,ensure_ascii=False).encode()).hexdigest()
def main():
    parser=argparse.ArgumentParser();parser.add_argument('--live',action='store_true',required=True);parser.add_argument('--resume-session');args=parser.parse_args()
    before=original_fingerprint();base='http://127.0.0.1:8766'
    if args.resume_session:
        sid=args.resume_session;secret=(ROOT/'interview-data/.token-secret').read_bytes()
        created={'session_id':sid,**{r+'_token':hmac.new(secret,(sid+':'+r).encode(),hashlib.sha256).hexdigest()for r in ('candidate','hr')}}
        r=requests.get(base+'/v1/interviews/'+sid,headers={'Authorization':'Bearer '+created['hr_token']},timeout=10);r.raise_for_status()
        assert r.json()['candidate_ref']=='交付验收｜合成完整流程（非真人评定）'
        from interview_workbench import load_operator_token
        r=requests.post(base+'/v1/workbench/sessions/'+sid+'/analyze',headers={'Authorization':'Bearer '+load_operator_token()},json={'reason':'开发验收明确重试本合成会话失败内容任务，检查新版引用约束。','idempotency_key':key()},timeout=10);r.raise_for_status()
    else:
        created=requests.post(base+'/v1/interviews',json={'candidate_ref':'交付验收｜合成完整流程（非真人评定）','idempotency_key':key()},timeout=10);created.raise_for_status();created=created.json()
    sid=created['session_id'];prefix=base+'/v1/interviews/'+sid
    def call(method,path,body=None,role='hr',form=None):
        r=requests.request(method,prefix+path,json=body,data=form,headers={'Authorization':'Bearer '+created[role+'_token']},timeout=20)
        if not r.ok:raise RuntimeError(str(r.status_code)+':'+r.json().get('detail',{}).get('code','request_error'))
        return r.json()
    proof={'checked_at':datetime.now(timezone.utc).isoformat(),'session_id':sid,'scope':'synthetic answers; real API/worker/provider; no measurement-validity claim','rounds':[]}
    texts={'leadership_F3_Q1':'这是流程验收的合成案例。我组织了一次社团活动，大家配合得很好，活动顺利完成。',
           'results_K6':'这是合成情景回答。我先向负责人确认本周交付目标和验收标准，按紧急程度列出任务；发现关键数据不足，我当天约同事核实并建立看板。每天核对进度，遇到延期及时调整资源并报告风险。最后按标准检查质量，复盘偏差，避免只追求数量。',
           'learning_F10_Q2':'这是合成案例。我刚接手表格分析时不会透视表，先做小样本核对，再学习官方教程并请同事指出错误。第一次把重复数据算进结果，收到反馈后我加了去重和检查步骤，用独立计算核对总数。后来把模板分享给组员；涉及非结构化访谈时不能直接套用，我会先重新定义分类和人工抽查。'}
    while True:
        state=call('GET','');round=state.get('current_round')
        if not round:break
        text=texts.get(round['question_id'],'这是合成追问补充。我先分别询问成员可投入的时间，把物料与宣传拆给有经验的成员，列出负责人及截止日期，每晚检查。供应延误时我联系备用供应商，让活动按期完成，并留下备用清单。')
        answer=next((a for a in state['answers']if a['round_id']==round['round_id']and a['current']),None)
        if not answer:answer=call('POST','/answers',role='candidate',form={'round_id':round['round_id'],'attempt_id':key(),'idempotency_key':key(),'transcript':text})
        deadline=time.monotonic()+180
        while True:
            report=call('GET','/report');jobs=[j for j in report['jobs'] if j['job_type']=='content' and j['answer_id']==answer['answer_id']]
            if jobs and jobs[-1]['status'] in ('succeeded','failed','blocked'):break
            if time.monotonic()>deadline:raise RuntimeError('content_timeout')
            time.sleep(1)
        if jobs[-1]['status']!='succeeded':
            proof.update(status='failed',failed_job_status=jobs[-1]['status']);write(proof);raise RuntimeError('content did not succeed; inspect safe worker job status')
        evaluation=next(e for e in reversed(report['model_evaluations']) if answer['answer_id'] in e['input_versions'])
        assert evaluation['schema_version']=='answer-evaluation.v2'
        proof['rounds'].append({'question_id':round['question_id'],'kind':round['kind'],'answer_id':answer['answer_id'],'evaluation_id':evaluation['evaluation_id'],'evidence_units':len(evaluation['evidence_units']),'followup_status':evaluation['followup']['status']})
        call('POST','/next',{'round_id':round['round_id'],'idempotency_key':key()},role='candidate')
    report=call('GET','/report');assert report['status']=='interview_finished';assert len(report['rounds']) in (3,4)
    proof['rounds']=[{'question_id':r['question_id'],'kind':r['kind']}for r in report['rounds']]
    old_version=report['version'];old_report=report
    ratings=[]
    for q in report['questions']:
        a=q['answers'][0];ratings.append({'question_id':q['question_id'],'score':3,'reason':'合成流程测试参考，非真实老师测量结论。','evidence':[{'answer_id':a['answer_id'],'transcript_revision':a['transcript_revision'],'quote':a['transcript']}]})
    report=call('POST','/reviews',{'version':report['version'],'reason':'交付验收合成复核，验证版本与独立复算。','ratings':ratings,'reviewer':'QA_SYNTHETIC','idempotency_key':key()})
    assert report['total_score']==50
    assert call('GET','/report?version='+str(old_version))==old_report
    a=report['questions'][0]['answers'][0]
    request={'version':report['version'],'selections':[{'answer_id':a['answer_id'],'transcript_revision':a['transcript_revision']}],'modules':['word_frequency'],'repeats':2,'purpose':'交付验收固定合成输入','condition':'词频重复对照','participant_group':'QA_SYNTHETIC','cloud_consent':False,'idempotency_key':key()}
    research=call('POST','/research/runs',request);ids=[r['run_id']for r in research['runs']]
    until=time.monotonic()+60
    while True:
        runs=[call('GET','/research/runs/'+rid)for rid in ids]
        if all(r['status']in('succeeded','partial','failed')for r in runs):break
        if time.monotonic()>until:raise RuntimeError('research_timeout')
        time.sleep(1)
    assert all(r['status']=='succeeded'for r in runs)
    call('POST','/research/annotations',{'experiment_id':research['experiment']['experiment_id'],'rater':'QA_SYNTHETIC','exposure':'ai_seen','ratings':[ratings[0]],'note':'合成参考，不作为盲评准确率。','idempotency_key':key()})
    comparison=call('POST','/research/compare',{'run_ids':ids});assert comparison['blind_reference_mae']is None
    reference=call('POST','/reference-profiles',{'version':report['version'],'targets':{'leadership':3,'results':4,'learning':3},'title':'QA研究参考草案','source':'合成验收参考值；非测量结论','status':'draft','idempotency_key':key()})
    report=reference['report'];report=call('POST','/research/publish',{'version':report['version'],'run_ids':ids,'note':'合成词频独立运行对照；不改写内容分。','idempotency_key':key()})
    assert report['total_score']==50 and report['potential_profile']['scored_count']==3
    candidate=requests.get(prefix+'/research/export',headers={'Authorization':'Bearer '+created['candidate_token']},timeout=10);assert candidate.status_code==403
    assert original_fingerprint()==before
    proof.update(status='passed',main_count=sum(r['kind']=='main'for r in report['rounds']),followup_count=sum(r['kind']=='followup'for r in report['rounds']),report_version=report['version'],old_report_unchanged=True,review_score_recomputed=50,research_runs=ids,independent_runs_succeeded=True,profile_measured=3,profile_total=8,candidate_research_denied=True,original_session_unchanged=True)
    write(proof)
def write(proof):
    (ROOT/'local-data/interview-validation/delivery-workflow-check.json').write_text(json.dumps(proof,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:proof[k]for k in ('status','session_id','main_count','followup_count','report_version')if k in proof}),flush=True)
if __name__=='__main__':main()
