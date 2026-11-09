"""Read-only aggregate audit of recent local expression outputs; no images or quotes."""
from pathlib import Path
import argparse,json,sqlite3,collections,statistics
ROOT=Path(__file__).resolve().parent
LABELS=['angry','disgust','fearful','happy','neutral','sad','surprised']
def audit(data_dir,limit=3):
 c=sqlite3.connect('file:'+str((Path(data_dir)/'interviews.sqlite3').resolve())+'?mode=ro',uri=True);c.row_factory=sqlite3.Row
 output=[]
 try:
  sessions=c.execute("select id,version from sessions where status='interview_finished' order by created_at desc limit ?",(limit,)).fetchall()
  for index,session in enumerate(sessions):
   frozen=c.execute('select payload from reports where session_id=? order by version desc limit 1',(session['id'],)).fetchone()
   if not frozen:continue
   report=json.loads(frozen[0]);seen=set();counts=collections.Counter();margins=[];frames=0;invalid_label=0;answers=0
   for run in report.get('standard_analysis',{}).get('runs',[]):
    for item in (run.get('result') or {}).get('multimodal',[]):
     module=item.get('modules',{}).get('expression')
     if not module or item['answer_id'] in seen:continue
     seen.add(item['answer_id']);answers+=1
     for obs in module.get('observations',[]):
      frames+=1
      if obs.get('quality')!='valid':continue
      values=obs.get('values',{});scores=values.get('raw_scores',{});label=values.get('label')
      if label not in LABELS or set(scores)!=set(LABELS) or scores[label]!=max(scores.values()):invalid_label+=1;continue
      counts[label]+=1;ordered=sorted(scores.values(),reverse=True);margins.append(ordered[0]-ordered[1])
   output.append({'recent_session_index':index+1,'report_version':session['version'],'answer_count':answers,'sampled_frames':frames,'valid_argmax_frames':sum(counts.values()),'argmax_or_mapping_inconsistencies':invalid_label,'label_counts':{k:counts[k] for k in LABELS},'median_top_two_logit_gap':round(statistics.median(margins),3) if margins else None,'logit_gap_not_probability':True})
 finally:c.close()
 return {'audit_version':'expression-report-audit.v1','reads_media':False,'runs_inference':False,'writes_participant_data':False,'scope':'model outputs and presentation; no annotated ground truth','sessions':output}
if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--data-dir',default=ROOT/'interview-data');parser.add_argument('--limit',type=int,default=3);args=parser.parse_args();print(json.dumps(audit(args.data_dir,max(1,min(10,args.limit))),ensure_ascii=False,indent=2))
