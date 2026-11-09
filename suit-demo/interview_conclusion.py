"""Source-linked potential requirements, never derived from voice/face or score cutoffs."""
from __future__ import annotations
import json
from pathlib import Path
from interview_text_research import criteria_for_question
BANK = json.loads((Path(__file__).parent/'config/question-bank.v1.json').read_text(encoding='utf-8'))
STATES = {'demonstrated','partial','not_met','insufficient_evidence','not_measured'}
LABELS = {'demonstrated':'已有证据支持','partial':'部分体现','not_met':'有相反证据，待复核',
          'insufficient_evidence':'证据不足','not_measured':'本轮未测'}

def _refs(values, answers):
    valid=[]
    for ref in values or []:
        if not isinstance(ref,dict): continue
        a=answers.get(ref.get('answer_id'))
        if not a or ref.get('transcript_revision')!=a.get('transcript_revision'): continue
        quote=ref.get('quote'); text=a.get('transcript','')
        if not isinstance(quote,str) or not quote.strip() or quote not in text: continue
        start,end=ref.get('char_start'),ref.get('char_end')
        if start is not None and (type(start)!=int or type(end)!=int or not 0<=start<end<=len(text) or text[start:end]!=quote): continue
        if ref not in valid: valid.append(dict(ref))
    return valid

def _combine(states):
    measured=[s for s in states if s!='not_measured']
    if not measured: return 'not_measured'
    if len(measured)==len(states) and all(s=='demonstrated' for s in measured): return 'demonstrated'
    if len(measured)==len(states) and all(s=='not_met' for s in measured): return 'not_met'
    if any(s in ('demonstrated','partial','not_met') for s in measured): return 'partial'
    return 'insufficient_evidence'

def potential_conclusion(report):
    factors=[]
    for f in BANK['factors']:
        qs=[q for q in report.get('questions',[]) if q.get('factor_id')==f['factor_id']]
        criteria=[]
        source_judgments={}
        for q in qs:
            answers={a['answer_id']:a for a in q.get('answers',[]) if a.get('current',True)}
            ev=q.get('model_evaluation') or {}
            actual={aid:a['transcript_revision'] for aid,a in answers.items()}
            fresh=bool(ev) and ev.get('input_versions')==actual and not q.get('review_stale')
            units={u.get('unit_id'):u for u in ev.get('evidence_units',[])} if fresh else {}
            judgments={j['criterion_id']:j for j in ev.get('criterion_judgments',[])} if fresh else {}
            matches={m['criterion_id']:m for m in ev.get('criterion_matches',[])} if fresh else {}
            for j in ev.get('requirement_judgments', []) if fresh else []:
                refs=_refs([r for uid in j.get('evidence_unit_ids',[]) for r in units.get(uid,{}).get('evidence',[])], answers)
                state=j.get('status','insufficient_evidence')
                if state not in STATES or state in ('demonstrated','partial','not_met') and not refs:
                    state='insufficient_evidence'
                source_judgments.setdefault(j['requirement_id'],[]).append({'status':state,'reason':j.get('reason',''), 'evidence':refs})
            for c in criteria_for_question(q):
                cid=c['criterion_id']; j=judgments.get(cid); m=matches.get(cid)
                evidence=_refs([r for uid in (j or m or {}).get('evidence_unit_ids',[]) for r in units.get(uid,{}).get('evidence',[])],answers)
                state='not_measured' if not answers else 'insufficient_evidence'
                reason='本轮未取得该评价点的可核对内容判断。'
                if j:
                    state=j.get('status','insufficient_evidence');reason=j.get('reason',reason)
                    if state not in STATES or state in ('demonstrated','partial','not_met') and not evidence:
                        state='insufficient_evidence'
                elif evidence:
                    state='partial';reason='旧版模型关联了以下材料，但未逐项判断满足程度，仍需老师核对。'
                gaps=[g['description'] for g in ev.get('gaps',[]) if fresh and g.get('criterion_id')==cid]
                criteria.append({'id':cid,'text':c.get('name') or c.get('description') or cid,
                    'description':c.get('description',''),'status':state,'reason':reason,'evidence':evidence,'gaps':gaps,
                    'requirement_ids':c.get('requirement_ids',[]),'source_refs':c.get('source_refs',[]),
                    'question_id':q['question_id'],'judgment_source':'model_draft' if j else 'legacy_reference' if evidence else 'unavailable'})
        # Each original factor requirement remains visible even when its question
        # or one-criterion mapping was not sampled by this form.
        frozen=next((q.get('factor_requirements') for q in qs if q.get('factor_requirements')),None)
        requirements=frozen or [{'requirement_id':f['factor_id']+':'+f['assessment_content_source_cell']+':'+str(i+1),
            'text':s,'source_cell':f['assessment_content_source_cell']} for i,s in enumerate(f['assessment_content'].splitlines()) if s.strip()]
        items=[]
        for r in requirements:
            rid=r.get('requirement_id',r.get('id'))
            related=[c for c in criteria if rid in c['requirement_ids']]
            source=source_judgments.get(rid, [])
            status=_combine([j['status'] for j in source]) if source else _combine([c['status'] for c in related]) if related else 'not_measured'
            if not source and status=='demonstrated': status='partial'
            evidence_items=source or related
            items.append({'id':rid,'text':r.get('text',r.get('description','')),'status':status,
                'evidence':list({json.dumps(e,sort_keys=True,ensure_ascii=False):e for c in evidence_items for e in c['evidence']}.values()),
                'gaps':list(dict.fromkeys(g for c in related for g in c['gaps'])),
                'reason':'；'.join(dict.fromkeys(c['reason'] for c in evidence_items)) or '本场题目尚未覆盖此要求。',
                'judgment_source':'source_requirement_model_draft' if source else 'criterion_reference_only',
                'source_refs':[{'source_cell':r.get('source_cell',f['assessment_content_source_cell'])}],
                'criterion_ids':[c['id'] for c in related]})
        # Legacy questions have no frozen requirement mapping. Keep their precise
        # criterion evidence, but do not claim the entire factor was demonstrated.
        shown=items if any(c['requirement_ids'] for c in criteria) or not qs else criteria
        status=_combine([c['status'] for c in shown])
        factors.append({'factor_id':f['factor_id'],'name':f['name'],'dimension':f['dimension_name'],
            'definition':f['definition'],'status':status,'summary':LABELS[status],
            'criteria':shown,'question_criteria':criteria,'requirements':items,
            'question_ids':[q['question_id'] for q in qs],
            'review_state':'stale' if any(q.get('review_stale') for q in qs) else 'question_reviewed' if qs and all(q.get('review_status')=='reviewed' for q in qs) else 'draft',
            'notice':'逐评价点结论为内容模型草稿；题目总分复核不等于逐评价点已由老师确认。'})
    supported=[f['name'] for f in factors if f['status']=='demonstrated']
    partial=[f['name'] for f in factors if f['status']=='partial']
    missing=[f['name'] for f in factors if f['status']=='not_measured']
    parts=[]
    if supported: parts.append('本场已有证据支持：'+'、'.join(supported)+'。')
    if partial: parts.append('部分要求已有体现、仍有待核对项：'+'、'.join(partial)+'。')
    if not parts: parts.append('本场尚未形成足够的逐评价点内容判断，请核对已保存回答。')
    if missing: parts.append('本轮未测：'+'、'.join(missing)+'。')
    return {'schema_version':'potential-conclusion.v1','source_version':report['version'],
        'summary':''.join(parts),'factors':factors,
        'coverage':{'total_factor_count':8,'measured_factor_count':len({q['factor_id'] for q in report.get('questions',[]) if q.get('answers')}),
                    'planned_factor_count':len({q['factor_id'] for q in report.get('questions',[])}),
                    'scored_factor_count':len({q['factor_id'] for q in report.get('questions',[]) if q.get('score') is not None})},
        'basis':'本场文本行为证据与原表要求；声画不参与因子判断。',
        'reference_status':'draft_requirements_not_validated_job_threshold',
        'teacher_review_note':'模型结论待指导老师核对；未设定岗位参考标准时不生成岗位匹配率或录用建议。'}
