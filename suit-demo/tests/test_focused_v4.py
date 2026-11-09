"""Synthetic contracts for source requirements, full forms and gaze summaries."""
import copy,json,os,tempfile,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from interview_forms import build_form
from interview_core import InterviewCore,DomainError
from interview_llm import hydrate_segment_references,evaluate_answer,validate_evaluation,ProviderError,FOLLOWUP_CONTRACT_VERSION,evaluation_schema
from interview_text_research import prepare_text_answers
from interview_conclusion import potential_conclusion
from interview_observations import summarize_multimodal
BASE=Path(__file__).resolve().parents[1]
def key():return str(uuid.uuid4())
def fixture():
 c=build_form(json.loads((BASE/'config/interview.v4.json').read_text(encoding='utf8')),mode='full',seed='test')
 q=c['questions'][0]; a={'answer_id':'a','transcript_revision':1,'transcript':'合成案例中我明确了目标，分工并检查了进度。','parent_question_id':None}
 p=prepare_text_answers([a])[0]; s=p['segments'][0]
 ref={'answer_id':'a','transcript_revision':1,'segment_id':s['segment_id'],'quote':s['text'],'char_start':None,'char_end':None}
 mapped={rid for criterion in q['criteria'] for rid in criterion['requirement_ids']}
 value={'schema_version':'answer-evaluation.v2','followup_contract_version':FOLLOWUP_CONTRACT_VERSION,'question_id':q['question_id'],
  'evidence_units':[{'unit_id':'e1','category':'own_action','interpretation':'合成行动引用。','evidence':[ref]}],
  'criterion_matches':[{'criterion_id':x['criterion_id'],'evidence_unit_ids':['e1'],'reason':'结构验证。'} for x in q['criteria']],
  'criterion_judgments':[{'criterion_id':x['criterion_id'],'status':'demonstrated','evidence_unit_ids':['e1'],'reason':'合成契约，非效度测试。'} for x in q['criteria']],
  'requirement_judgments':[{'requirement_id':r['requirement_id'],'status':'partial' if r['requirement_id'] in mapped else 'not_measured','evidence_unit_ids':['e1'] if r['requirement_id'] in mapped else [],'reason':'只支持部分要求。'} for r in q['factor_requirements']],
  'gaps':[],'rating':{'score':3,'reason':'合成锚点校验。','evidence':[ref]},
  'followup':{'status':'content_sufficient','branch_id':None,'target_gap_id':None,'prerequisite_evidence_unit_ids':[],'reason':'合成停止测试。','evidence':[ref]}}
 return c,q,p,value
class SourceJudgments(unittest.TestCase):
 def setUp(self): self.c,self.q,self.a,self.v=fixture()
 def valid(self,v=None):return validate_evaluation(copy.deepcopy(v or self.v),self.q,[self.a],self.c['branches'],require_current=True)
 def report(self,v=None):
  ev=self.valid(v);ev['input_versions']={'a':1}
  return {'version':1,'questions':[{**self.q,'answers':[self.a],'model_evaluation':ev,'score':3}]}
 def test_compact_reference_uses_exact_canonical_sentence(self):
  s=self.a['segments'][0];ref={'answer_id':'a','transcript_revision':1,'segment_id':s['segment_id']}
  h=hydrate_segment_references({'evidence':[ref]},[self.a])['evidence'][0]
  self.assertEqual(h['quote'],s['text']);self.assertEqual(self.a['transcript'][h['char_start']:h['char_end']],s['text'])
  for bad in ({**ref,'transcript_revision':2},{**ref,'segment_id':'invented'},{**ref,'answer_id':'someone-else'}):
   with self.assertRaises(ProviderError):hydrate_segment_references({'evidence':[bad]},[self.a])
 def test_accept_and_schema(self):
  self.valid();self.assertIn('requirement_judgments',evaluation_schema(self.q['question_id'],[],[x['criterion_id'] for x in self.q['criteria']],self.q)['required'])
 def test_partial_requirement_not_inherited_from_high_criteria(self):
  r=potential_conclusion(self.report());f=next(f for f in r['factors'] if f['factor_id']==self.q['factor_id'])
  self.assertEqual(f['status'],'partial');self.assertNotIn('demonstrated',[x['status'] for x in f['criteria']])
 def test_no_judgment_fallback_cannot_claim_entire_requirement(self):
  r=self.report();r['questions'][0]['model_evaluation'].pop('requirement_judgments')
  f=next(f for f in potential_conclusion(r)['factors'] if f['factor_id']==self.q['factor_id'])
  self.assertNotEqual(f['status'],'demonstrated')
 def test_fabrication_and_stale_revision_rejected(self):
  for change in ({'quote':'从未说过的话'},{'transcript_revision':7}):
   v=copy.deepcopy(self.v);v['evidence_units'][0]['evidence'][0].update(change)
   with self.assertRaises(ProviderError):self.valid(v)
 def test_missing_or_duplicate_judgments_rejected(self):
  for field in ('criterion_judgments','requirement_judgments'):
   v=copy.deepcopy(self.v);v[field]=[]
   with self.assertRaises(ProviderError):self.valid(v)
   v=copy.deepcopy(self.v);v[field][-1]=v[field][0]
   if len(v[field])>1:
    with self.assertRaises(ProviderError):self.valid(v)
 def test_opposite_requires_quote(self):
  v=copy.deepcopy(self.v);v['criterion_judgments'][0].update(status='not_met',evidence_unit_ids=[])
  with self.assertRaises(ProviderError):self.valid(v)
 def test_stale_conclusion_drops_judgment(self):
  r=self.report();r['questions'][0]['answers'][0]['transcript_revision']=2
  f=next(f for f in potential_conclusion(r)['factors'] if f['factor_id']==self.q['factor_id'])
  self.assertFalse(any(x['evidence'] for x in f['criteria']));self.assertNotEqual(f['status'],'demonstrated')
 def test_multimodal_cannot_change_conclusion(self):
  r=self.report();expected=potential_conclusion(r);r['face']={'happy':1};r['voice']={'confident':1};self.assertEqual(expected,potential_conclusion(r))
 def test_old_unasked_factors_remain_unmeasured(self):
  r=potential_conclusion(self.report());self.assertEqual(sum(f['status']=='not_measured' for f in r['factors']),7)
 def test_transport_only_asks_mapped_requirements_and_server_restores_unmeasured(self):
  mapped={rid for c in self.q['criteria'] for rid in c['requirement_ids']}
  v=copy.deepcopy(self.v);v['requirement_judgments']=[j for j in v['requirement_judgments'] if j['requirement_id'] in mapped]
  def compact(x):
   if isinstance(x,list):return [compact(t) for t in x]
   if isinstance(x,dict):return {k:compact(y) for k,y in x.items() if not ('quote' in x and k in ('quote','char_start','char_end'))}
   return x
  v=compact(v)
  env={'INTERVIEW_LLM_BASE_URL':'https://pinned.invalid/v1','DASHSCOPE_API_KEY':'synthetic-only','INTERVIEW_LLM_MODEL':'fixture'}
  with patch.dict(os.environ,env,clear=True),patch('interview_credentials._record',return_value={}),patch('requests.post') as request:
   request.return_value.status_code=200;request.return_value.content=b'{}'
   request.return_value.json.return_value={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(v)}}]}
   r=evaluate_answer(self.q,[self.a],self.c['branches'],{'model':'fixture','base_url':'https://pinned.invalid/v1'})
   payload=json.loads(request.call_args.kwargs['json']['messages'][1]['content'])
   self.assertEqual({x['requirement_id'] for x in payload['factor_requirements']},mapped)
   self.assertEqual(len(r['evaluation']['requirement_judgments']),len(self.q['factor_requirements']))
   self.assertTrue(all(j['status']=='not_measured' and not j['evidence_unit_ids'] for j in r['evaluation']['requirement_judgments'] if j['requirement_id'] not in mapped))
class FullFlow(unittest.TestCase):
 def test_modes_review_counts_and_freeze(self):
  with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'INTERVIEW_CREDENTIALS_FILE':d+'/none','DASHSCOPE_API_KEY':''}),patch('interview_core.credential_available',return_value=False):
   c=InterviewCore(d,BASE/'config/interview.v4.json')
   for mode,count in [('quick',3),('full',8)]:
    idem=key();s=c.create_interview(idem,'synthetic',form_mode=mode);sid=s['session_id'];answers=[]
    self.assertEqual(c.create_interview(idem,'synthetic',form_mode=mode)['session_id'],sid)
    with self.assertRaises(DomainError):c.create_interview(idem,'synthetic',form_mode='full' if mode=='quick' else 'quick')
    while s['status']!='interview_finished':
     r=s['current_round'];a=c.submit_answer(sid,r['round_id'],key(),key(),'合成案例中我组织小组完成了任务。')
     if r['kind']=='main':answers.append(a)
     s=c.next_round(sid,key(),r['round_id'])
    report=c.get_report(sid);self.assertEqual(len(report['questions']),count);self.assertEqual(report['potential_conclusion']['coverage']['measured_factor_count'],count)
    row=next(r for r in c.list_workbench_sessions()['sessions'] if r['session_id']==sid);self.assertEqual(row['main_question_count'],count);self.assertEqual(row['form_mode'],mode)
    self.assertEqual(c.get_report(sid,report['version']),report)
class GazeSummary(unittest.TestCase):
 def sample(self):
  obs=[{'start_ms':i*200,'end_ms':(i+1)*200,'quality':'valid','values':{'gaze_angle_x':0 if i<30 else .4,'gaze_angle_y':0},'track_segment_id':'t'} for i in range(40)]
  return {'answer_id':'a','run_id':'r','modules':{'gaze':{'status':'complete','model_id':'OpenFace','observations':obs}}}
 def test_relative_event_and_no_camera_percentage(self):
  d=self.sample();snapshot=copy.deepcopy(d);r=summarize_multimodal(d);e=next(e for e in r['events'] if e['kind']=='gaze_direction')
  self.assertEqual(e['start_ms'],6000);self.assertIsNone(r['gaze_summary']['camera_facing_fraction']);self.assertEqual(d,snapshot)
 def test_missing_frames_break_continuity(self):
  d=self.sample()
  for i in range(30,40):
   if i%2:d['modules']['gaze']['observations'][i]['quality']='missing'
  self.assertFalse(summarize_multimodal(d)['events'])
 def test_constant_is_not_camera_facing(self):
  d=self.sample()
  for v in d['modules']['gaze']['observations']:v['values']['gaze_angle_x']=.9
  r=summarize_multimodal(d);self.assertFalse(r['events']);self.assertIsNone(r['gaze_summary']['camera_facing_fraction'])
 def test_new_track_does_not_compare_against_other_person(self):
  d=self.sample()
  for v in d['modules']['gaze']['observations'][30:]:v['track_segment_id']='different'
  self.assertFalse(summarize_multimodal(d)['events'])
if __name__=='__main__':unittest.main()
