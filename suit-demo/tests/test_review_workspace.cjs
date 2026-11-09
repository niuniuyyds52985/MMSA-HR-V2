/* Multi-question review interaction fixtures. No real browser, participant, or model. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const harness=fs.readFileSync(path.join(__dirname,'test_evidence_timeline.cjs'),'utf8').split('(async () => {')[0].replace("vm.runInContext(source('js/report-reader.js'),context);","vm.runInContext(source('js/observation-context.js'),context); vm.runInContext(source('js/report-reader.js'),context);");
const shared={require,__dirname,console,URLSearchParams,Headers,Blob,Response,Date,Set,Map,Number,Array,Math,Promise,setTimeout,clearTimeout,queueMicrotask};
vm.runInNewContext(harness+'\nglobalThis.makeEnvironment=environment;',shared);
const tick=async()=>{for(let i=0;i<90;i++)await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));for(let i=0;i<50;i++)await Promise.resolve();};
const store=()=>{const values=new Map();return{values,getItem:key=>values.get(key)||null,setItem:(key,value)=>values.set(key,String(value)),removeItem:key=>values.delete(key)};};
function fixture(){
  const makeAnswer=(id,q,text,parent)=>({answer_id:id,question_id:q,parent_question_id:parent||null,round_id:'r-'+id,current:true,transcript_revision:1,transcript:text,has_media:true,media_type:'audio/wav',job_status:'succeeded',content_status:'succeeded',revisions:[]});
  const a1=makeAnswer('a1','leadership_F3_Q1','我主动把任务拆成三组。'),follow=makeAnswer('af','leadership_F3_Q1_action','我每天汇总小组进度。','leadership_F3_Q1'),a2=makeAnswer('a2','results_K6','我先检查渠道的转化率。'),a3=makeAnswer('a3','learning_F10_Q2','我把复盘的方法用于新项目。');
  const reference=a=>({answer_id:a.answer_id,transcript_revision:a.transcript_revision,quote:a.transcript});
  const makeQuestion=(a,factor,answers)=>({question_id:a.question_id,factor_id:factor,text:'合成问题 '+factor,answers,score:3,ai_score:3,score_source:'model_draft',reason:'合成评价',evidence:[reference(a)],anchors:{1:'证据有限',3:'有行动说明',5:'行动和检查充分'},model_evaluation:{rating:{score:3,reason:'合成模型理由',evidence:[reference(a)]},evidence_units:[{category:'own_action',interpretation:'合成行为依据',evidence:[reference(factor==='leadership'?follow:a)]}],criterion_matches:[],gaps:[],followup:{reason:'合成追问记录',status:'sufficient',evidence:[]}}});
  return{session_id:'synthetic-session',candidate_ref:'SIM-REVIEW-FIXTURE',created_at:'2026-10-25T00:00:00Z',version:10,status:'interview_finished',questions:[makeQuestion(a1,'leadership',[a1,follow]),makeQuestion(a2,'results',[a2]),makeQuestion(a3,'learning',[a3])],answers:[a1,follow,a2,a3],rounds:[],current_round:null,jobs:[],reviews:[],assessments:[],decision_events:[],coverage:{planned_factor_ids:['leadership','results','learning'],uncovered_factor_ids:['develop_others','balance_people_task','systems_thinking','adaptability','feedback']},automatic_scoring:{status:'draft_results_available'},research_appendix:{note:'合成研究附录',selected_runs:[{run_id:'research-fixture',status:'succeeded',result:{semantic:[{question_id:a1.question_id,evaluation:{rating:{score:3,reason:'合成独立结果',evidence:[reference(follow)]}}}],multimodal:[{answer_id:follow.answer_id,transcript_revision:1,modules:{audio:{status:'complete',observations:[{start_ms:1000,end_ms:2000,quality:'valid',values:{rms_dbfs:-25}}]}}}]}}]}};
}
function environment(data,storage=store()){
  const e=shared.makeEnvironment('hr-review.html'),s=e.sandbox;e.get=id=>e.document.getElementById(id);e.requests=[];e.posts=[];e.retryPosts=[];e.storage=storage;
  const proto=Object.getPrototypeOf(e.document.body);
  if(!proto.prepend)proto.prepend=function(...nodes){const existing=[...this.children];this.replaceChildren(...nodes,...existing);};
  if(!proto.closest)proto.closest=function(selector){for(let node=this;node;node=node.parentElement)if(node.matches(selector))return node;return null;};
  e.get('reportView').append(e.get('panelReview'),e.get('panelOverview'),e.get('panelResearch'),e.get('panelHistory'));
  e.get('panelReview').append(e.get('reviewForm'));e.get('reviewForm').append(e.get('questions'),e.get('reviewReason'),e.get('saveReview'),e.get('saveStatus'),e.get('savedReviewActions'));
  e.get('panelOverview').append(e.get('researchAppendix'),e.get('potentialProfile'));e.get('panelHistory').append(e.get('reviewHistory'));
  for(const name of ['Review','Overview','Research','History']){const b=e.get('tab'+name);b.dataset.workspaceTab=name.toLowerCase();b.setAttribute('data-workspace-tab',name.toLowerCase());}
  s.sessionStorage=storage;s.setInterval=()=>1;s.clearInterval=()=>{};s.document.hidden=false;
  s.fetch=async(url,options={})=>{
    const method=options.method||'GET';e.requests.push({url,method});
    if(url.endsWith('/report'))return new Response(JSON.stringify(data),{status:200});
    if(url.includes('/factor-profile?'))return new Response(JSON.stringify({factors:[]}),{status:200});
    if(url.includes('/timeline?')){
      const match=url.match(/\/answers\/([^/]+)\/timeline\?transcript_revision=(\d+)/),answer=data.answers.find(a=>a.answer_id===match[1]),revision=Number(match[2]);
      return new Response(JSON.stringify({schema_version:'interview-timeline.v1',session_id:data.session_id,answer_id:answer.answer_id,transcript_revision:revision,current_transcript_revision:answer.transcript_revision,is_current_revision:revision===answer.transcript_revision,answer_current:true,text:answer.transcript,media:{available:true},alignment:{status:'aligned',available_asr_revisions:[1]},segments:[{segment_id:answer.answer_id+'-segment',text:answer.transcript,char_start:0,char_end:Array.from(answer.transcript).length,start_ms:1000,end_ms:2000,alignment_status:'aligned'}],observations:{audio:{status:'not_available'}},references:[]}),{status:200});
    }
    if(url.includes('/media/'))return new Response(new Blob(['synthetic audio'],{type:'audio/wav'}),{status:200});
    if(url.endsWith('/content-analysis/retry')&&method==='POST'){
      const body=JSON.parse(options.body);e.retryPosts.push(body);if(e.retryResponse)return e.retryResponse(body);
      const old=data.jobs.find(j=>j.question_id===body.question_id&&j.status==='failed');old.status='superseded';data.jobs.push({...old,job_id:'retry-fixture',retry_of:old.job_id,status:'queued',created_at:'2026-01-03',manual_retry_count:1});data.version++;
      return new Response(JSON.stringify({job_id:'retry-fixture',status:'queued',version:data.version}),{status:200});
    }
    if(url.endsWith('/reviews')&&method==='POST'){
      const body=JSON.parse(options.body);e.posts.push(body);
      if(body.version!==data.version)return new Response(JSON.stringify({detail:{message:'stale version',code:'version_conflict'}}),{status:409});
      for(const rating of body.ratings){const q=data.questions.find(q=>q.question_id===rating.question_id);Object.assign(q,{score:rating.score,reason:rating.reason,evidence:rating.evidence,score_source:'hr',review_stale:false});}
      data.reviews.push({parent_version:data.version,created_at:'2026-10-25T00:01:00Z',reason:body.reason,ratings:body.ratings});data.version++;
      return new Response(JSON.stringify(data),{status:200});
    }
    throw new Error('Unexpected fixture request '+method+' '+url);
  };
  e.card=index=>e.get('questions').children[index];e.nav=index=>e.get('questionNav').children[index];
  e.edit=(index,reason,score)=>{e.nav(index).click();const card=e.card(index);card.querySelector('.rating-reason').value=reason;for(const radio of card.querySelectorAll('input[type="radio"]'))radio.checked=radio.value===String(score);e.get('reviewForm').fire('input',{target:card.querySelector('.rating-reason')});};
  e.save=index=>e.card(index).querySelector('.question-save-row').querySelector('button').click();
  e.run('js/multimodal-view.js');e.channels=[];const mountMultimodal=s.SuitMultimodalView.mount;s.SuitMultimodalView.mount=options=>{const view=mountMultimodal(options),setChannel=view.setChannel;view.setChannel=(...args)=>{e.channels.push(args);return setChannel(...args);};return view;};
  e.run('js/hr-review.js');return e;
}
const visible=node=>{if(!node)return false;for(let n=node;n;n=n.parentElement)if(n.hidden)return false;return true;};
let passed=0;async function check(name,fn){await fn();passed++;console.log('PASS '+name);}
(async()=>{
  await check('saving one question preserves another question draft and does not submit unreviewed questions',async()=>{
    const data=fixture(),e=environment(data);await tick();assert.equal(e.get('questions').children.length,3);
    e.edit(0,'第一题已对照证据',4);e.edit(1,'第二题尚未保存的理由',5);e.nav(0).click();e.save(0);await tick();
    assert.equal(e.posts.length,1);assert.deepEqual(e.posts[0].ratings.map(x=>x.question_id),['leadership_F3_Q1']);assert.equal(data.questions[0].score_source,'hr');assert.equal(data.questions[1].score_source,'model_draft');assert.equal(data.questions[2].score_source,'model_draft');
    assert.equal(e.card(1).querySelector('.rating-reason').value,'第二题尚未保存的理由');assert.equal(e.card(1).querySelector('input[type="radio"]:checked').value,'5');
    const draft=JSON.parse(e.storage.getItem('zhenjian-review-draft-v2:synthetic-session'));assert.deepEqual(draft.touched,['results_K6']);assert.equal(draft.values.results_K6.reason,'第二题尚未保存的理由');
    e.get('reviewReason').value='完成已填写的题目';e.get('reviewForm').fire('submit');await tick();assert.equal(e.posts.length,2);assert.deepEqual(e.posts[1].ratings.map(x=>x.question_id),['results_K6']);assert.equal(data.questions[2].score_source,'model_draft');e.fire('pagehide');
    assert.match(fs.readFileSync(path.join(__dirname,'../site/hr-review.html'),'utf8'),/<form\b[^>]*id="reviewForm"[^>]*novalidate/);
  });
  await check('refresh and reload preserve draft citation version; stale citation cannot be saved silently',async()=>{
    const data=fixture(),e=environment(data);await tick();e.edit(1,'等待核对新版原话',4);const oldQuote=e.card(1).querySelector('.quote-field').value;
    data.version++;data.answers.find(a=>a.answer_id==='a2').transcript_revision=2;
    e.get('refreshReport').click();await tick();assert.equal(e.card(1).querySelector('.rating-reason').value,'等待核对新版原话');assert.equal(e.card(1).querySelector('.quote-field').value,oldQuote);assert.match(e.card(1).querySelector('.evidence-quote-version').textContent,/v1.*已变化/);
    e.save(1);await tick();assert.equal(e.posts.length,0);assert.match(e.get('notice').textContent,/旧转写版本/);e.fire('pagehide');
    const reload=environment(data,e.storage);await tick();assert.equal(reload.card(1).querySelector('.rating-reason').value,'等待核对新版原话');assert.match(reload.card(1).querySelector('.evidence-quote-version').textContent,/v1.*已变化/);
    reload.card(1).querySelector('.confirm-evidence-version').click();reload.save(1);await tick();assert.equal(reload.posts.length,1);assert.equal(reload.posts[0].ratings[0].evidence[0].transcript_revision,2);reload.fire('pagehide');
  });
  await check('semantic evidence reveals hidden follow-up answer and plays the requested revision',async()=>{
    const data=fixture(),e=environment(data);await tick();const answers=e.card(0).querySelectorAll('.answer-evidence');assert.equal(answers.length,2);assert.equal(answers[1].hidden,true);
    const locate=e.card(0).querySelector('.research-evidence-unit').querySelector('button');locate.click();await tick();assert.equal(answers[1].hidden,false);assert.equal(answers[0].hidden,true);assert.ok(visible(answers[1].querySelector('audio')));assert.equal(answers[1].querySelector('audio').currentTime,1);assert.ok(e.requests.some(r=>r.url.endsWith('/answers/af/timeline?transcript_revision=1')));e.fire('pagehide');
  });
  await check('research reference navigates from overview to the correct question and follow-up',async()=>{
    const data=fixture(),e=environment(data);await tick();e.nav(2).click();e.get('tabOverview').click();assert.equal(e.get('panelReview').hidden,true);
    const locate=e.get('researchAppendix').querySelector('button');locate.click();await tick();assert.equal(e.get('panelReview').hidden,false);assert.equal(e.get('panelOverview').hidden,true);assert.equal(e.card(0).hidden,false);assert.equal(e.card(2).hidden,true);const follow=e.card(0).querySelectorAll('.answer-evidence')[1];assert.ok(visible(follow.querySelector('audio')));assert.equal(follow.querySelector('audio').currentTime,1);e.fire('pagehide');
  });
  await check('sealed decisions stay with their parent question and are distinct from the current model opinion',async()=>{
    const data=fixture(),ref={answer_id:'a1',transcript_revision:1,quote:data.answers[0].transcript};
    data.decision_events=[{parent_question_id:'leadership_F3_Q1',action:'ask_followup',decision_summary:{brief_reason:'当时缺少结果证据，所以追问结果。',target_evidence:'结果证据',selected_branch:{branch_id:'leadership_F3_Q1_result',text:'当时的题库追问：结果如何？'},used_evidence:[ref]}},{parent_question_id:'leadership_F3_Q1',action:'continue_main',checkpoint_kind:'followup',reason_for_hr:'追答保存后继续下一主问。'},{parent_question_id:'results_K6',action:'continue_main',reason_for_hr:'第二题无合法追问分支。'}];
    const e=environment(data);await tick();const chosen=e.card(0).querySelector('.review-question-decisions');assert.match(chosen.textContent,/当时缺少结果证据.*所以追问结果/);assert.match(chosen.textContent,/当时的题库追问：结果如何/);assert.doesNotMatch(chosen.textContent,/第二题无合法/);const brief=chosen.querySelector('.review-decision-summary'),details=chosen.querySelector('.review-decision-detail');assert.match(brief.textContent,/实际追问.*当时的题库追问：结果如何/);assert.doesNotMatch(brief.textContent,/追答保存后|当时缺少结果证据/);assert.equal(details.open,false);assert.equal(details.querySelectorAll('.reader-decision').length,2);assert.match(details.textContent,/追答保存后继续下一主问/);assert.match(e.card(1).querySelector('.review-decision-summary').textContent,/本题未追加追问.*第二题无合法追问分支/);details.open=true;
    const comparison=e.card(0).querySelector('.review-model-followup');assert.notEqual(comparison.open,true);assert.match(comparison.textContent,/模型追问意见（对照）.*当前模型草稿.*当时的追问决定/);assert.doesNotMatch(e.card(0).textContent,/追问建议：/);chosen.querySelector('button').click();await tick();assert.ok(e.requests.some(r=>r.url.endsWith('/answers/a1/timeline?transcript_revision=1')));
    assert.equal(e.requests.filter(r=>r.url.includes('/factor-profile?')).length,0);e.get('tabOverview').click();await tick();assert.equal(e.requests.filter(r=>r.url.includes('/factor-profile?')).length,0);e.get('profileDetails').open=true;e.get('profileDetails').fire('toggle');await tick();assert.equal(e.requests.filter(r=>r.url.includes('/factor-profile?')).length,1);e.fire('pagehide');
  });
  await check('saving a question offers the next pending review without losing drafts and closes with the same-session report',async()=>{
    const data=fixture();data.questions[1].score_source='hr';const e=environment(data);await tick();e.edit(1,'已经复核题的新修改尚未保存',5);e.edit(0,'第一题保存',4);e.save(0);await tick();
    const next=e.card(0).querySelector('.question-review-next');assert.equal(next.hidden,false);assert.equal(next.querySelector('button').textContent,'下一道待复核');next.querySelector('button').click();await tick();assert.equal(e.card(1).hidden,false);assert.equal(e.card(1).querySelector('.rating-reason').value,'已经复核题的新修改尚未保存');assert.equal(e.posts.length,1);assert.ok(e.card(1).scrolled);e.save(1);await tick();e.card(1).querySelector('.question-review-next').querySelector('button').click();await tick();assert.equal(e.card(2).hidden,false);e.edit(2,'第三题保存',3);e.save(2);await tick();
    const done=e.card(2).querySelector('.question-review-next');assert.equal(done.hidden,false);assert.equal(done.querySelector('button'),null);assert.match(done.textContent,/本场各题复核均已保存/);const link=done.querySelector('a');assert.equal(link.textContent,'查看报告');assert.match(link.href,/^\/interview-report#session=synthetic-session&token=synthetic-token$/);assert.equal(e.posts.length,3);assert.ok(e.posts.every(p=>p.ratings.length===1));e.fire('pagehide');
    const skip=fixture();skip.questions[1].score_source='hr';const s=environment(skip);await tick();s.edit(0,'保存后跳过已复核题',3);s.save(0);await tick();s.card(0).querySelector('.question-review-next').querySelector('button').click();await tick();assert.equal(s.card(2).hidden,false);assert.equal(s.card(1).hidden,true);s.fire('pagehide');
  });

  await check('recovery keeps dirty review drafts and makes old main-only rating available only as reference',async()=>{
    const data=fixture(),q=data.questions[0],prior={...structuredClone(q.model_evaluation),question_id:q.question_id,input_versions:{a1:1},created_at:'2026-01-01'};data.model_evaluations=[prior];q.score=null;q.ai_score=null;q.model_evaluation=null;q.score_source=null;
    data.jobs=[{job_id:'failed-fixture',question_id:q.question_id,job_type:'content',status:'failed',created_at:'2026-01-02',error_code:'read_timeout',input_versions:{a1:1,af:1},attempts:3}];
    const e=environment(data);await tick();e.edit(1,'第二题仍在核对的草稿',4);e.edit(0,'第一题人工记录尚未保存',2);const version=data.version,card=e.card(0);assert.match(card.querySelector('.reader-previous-draft').textContent,/尚未纳入追答.*不计入当前总分/);assert.equal(card.querySelector('.review-model-draft'),null);
    const retry=card.querySelector('.reader-content-recovery').querySelector('button');retry.click();retry.click();await tick();assert.equal(e.retryPosts.length,1);assert.equal(e.retryPosts[0].question_id,q.question_id);assert.equal(e.retryPosts[0].version,version);assert.ok(e.retryPosts[0].idempotency_key);assert.equal(e.card(1).querySelector('.rating-reason').value,'第二题仍在核对的草稿');assert.equal(e.card(0).querySelector('.rating-reason').value,'第一题人工记录尚未保存');assert.equal(e.posts.length,0);assert.equal(q.score,null);assert.match(e.card(0).querySelector('.reader-content-recovery').textContent,/正在更新/);e.fire('pagehide');
  });
  await check('a retry conflict is local, preserves drafts and does not imply success',async()=>{
    const data=fixture(),q=data.questions[0];q.score=null;q.model_evaluation=null;q.score_source=null;data.jobs=[{job_id:'failed',question_id:q.question_id,job_type:'content',status:'failed',input_versions:{a1:1,af:1},error_code:'connect_timeout'}];
    const e=environment(data);await tick();e.edit(0,'等待恢复时记录的核对意见',3);e.retryResponse=()=>new Response(JSON.stringify({detail:{message:'报告已更新，请刷新后重试。',code:'version_conflict'}}),{status:409});const recovery=e.card(0).querySelector('.reader-content-recovery');recovery.querySelector('button').click();await tick();assert.match(recovery.textContent,/报告已更新，请刷新后重试/);assert.equal(recovery.querySelector('button').disabled,false);assert.equal(e.card(0).querySelector('.rating-reason').value,'等待恢复时记录的核对意见');assert.equal(data.jobs[0].status,'failed');assert.equal(e.posts.length,0);e.fire('pagehide');
  });
  await check('simplified reading maps selected quotations to canonical original text and rejects ambiguous selection',async()=>{
    const data=fixture(),q=data.questions[0],a=q.answers[0];a.transcript='我帶領各組核對進度。我每天檢查。我每天檢查。';q.evidence=[];const e=environment(data);e.run('js/text-display.js');await tick();const block=e.card(0).querySelector('.answer-evidence'),text=block.querySelector('.answer-text'),quoteButton=block.querySelector('.evidence-actions').querySelector('button');assert.match(text.textContent,/带领各组核对进度/);assert.match(a.transcript,/帶領各組核對進度/);
    e.sandbox.getSelection=()=>({toString:()=> '带领各组核对进度',anchorNode:text,focusNode:text});quoteButton.click();await tick();const quote=e.card(0).querySelector('.quote-field');assert.equal(quote.value,'帶領各組核對進度');e.edit(0,'基于原文核对',3);e.save(0);await tick();assert.equal(e.posts[0].ratings[0].evidence[0].quote,'帶領各組核對進度');
    const newBlock=e.card(0).querySelector('.answer-evidence'),newText=newBlock.querySelector('.answer-text'),count=e.card(0).querySelectorAll('.quote-field').length;e.sandbox.getSelection=()=>({toString:()=> '我每天检查',anchorNode:newText,focusNode:newText});newBlock.querySelector('.evidence-actions').querySelector('button').click();await tick();assert.equal(e.card(0).querySelectorAll('.quote-field').length,count);assert.match(e.get('notice').textContent,/唯一对应原文/);e.fire('pagehide');
  });


  await check('HR observations show exact speech and reuse one paused inline player without disturbing review drafts',async()=>{
    const data=fixture(),a=data.answers[0],labels=['happy','neutral','disgust','sad','angry','fearful','surprised'],scores=Object.fromEntries(labels.map(k=>[k,k==='happy'?12:-3]));
    const audio={id:'audio-1',kind:'audio_pitch',title:'合成低音片段',description:'本题声音频率变化',start_ms:1100,end_ms:1300,anchor_ms:1100,measurements:{median:120,unit:'Hz'},evidence_refs:[{module:'audio',observation_indices:[0]}]};
    const item={answer_id:a.answer_id,transcript_revision:1,modules:{audio:{status:'complete',observations:[{quality:'valid',start_ms:1000,end_ms:2000,values:{f0_hz:120,rms_dbfs:-25}}]},geometry:{status:'complete',observations:[]},expression:{status:'complete',observations:[{quality:'valid',start_ms:1600,end_ms:1700,values:{label:'happy',raw_scores:scores}}]}},reader_summary:{events:[audio,{...structuredClone(audio),id:'audio-2',kind:'audio_energy',title:'合成声音增强片段',start_ms:1400,end_ms:1500,anchor_ms:1400}]}};
    data.standard_analysis={enabled:true,runs:[{run_id:'synthetic-main-core',phase:'core',status:'succeeded',result:{multimodal:[item]}}]};
    const e=environment(data);await tick();e.edit(0,'尚未保存的老师理由',4);const card=e.card(0),events=card.querySelectorAll('.reader-event'),first=events.find(n=>n.textContent.includes('合成低音片段')),second=events.find(n=>n.textContent.includes('合成声音增强片段'));
    assert.ok(first&&second);assert.match(first.querySelector('.reader-spoken-context').textContent,/我主动把任务拆成三组/);assert.equal(e.document.querySelector('audio'),null);first.querySelector('button').click();await tick();const player=e.document.querySelector('audio');assert.ok(first.querySelector('.reader-event-replay').contains(player));assert.equal(player.currentTime,1.1);assert.equal(player.paused,true);
    second.querySelector('button').click();await tick();assert.equal(e.document.querySelectorAll('audio').length,1);assert.equal(e.document.querySelector('audio'),player);assert.ok(second.querySelector('.reader-event-replay').contains(player));assert.equal(player.currentTime,1.4);assert.equal(player.paused,true);
    const happy=card.querySelectorAll('.reader-expression-row').find(row=>row.textContent.includes('开心样式'));happy.querySelector('button').click();await tick();assert.equal(player.currentTime,1.6);assert.equal(e.channels.at(-1)[0],'geometry');assert.equal(e.document.querySelectorAll('audio').length,1);assert.equal(player.paused,true);assert.equal(card.querySelector('.rating-reason').value,'尚未保存的老师理由');assert.equal(e.requests.filter(r=>r.url.includes('/media/')).length,1);assert.ok(e.requests.filter(r=>r.url.includes('/timeline?')).every(r=>r.url.endsWith('transcript_revision=1')));assert.ok(e.requests.every(r=>r.method==='GET'));e.fire('pagehide');
  });
  await check('HR passes the configured potential factor separately from the sealed dictionary branch and later gaps',async()=>{
    const data=fixture(),q=data.questions[0];data.potential_profile={factors:[{factor_id:'leadership',dimension_name:'领导倾向',name:'领导意愿',definition:'愿意协调影响他人',question_ids:[q.question_id]}]};q.model_evaluation.gaps=[{description:'后续新增观点'}];
    data.decision_events=[{parent_question_id:q.question_id,action:'ask_followup',decision_summary:{target_evidence:'当时待补的行动',selected_branch:{text:'当时选题：具体怎样协调？'},dictionary_snapshot:{involved_in_selection:false,matches:[]}}}];
    const e=environment(data);await tick();const card=e.card(0),theory=card.querySelector('.reader-potential-reasoning');assert.ok(theory);assert.match(theory.textContent,/领导倾向 \/ 领导意愿/);assert.match(theory.textContent,/当时待补的证据：当时待补的行动/);assert.equal(card.querySelector('.reader-routing-heading').textContent,'实际选题与词典依据');assert.match(card.querySelector('.reader-decision-audit').textContent,/不参与本次选题/);assert.match(theory.querySelector('details').textContent,/后续内容模型核对.*不改写当时决定.*后续新增观点/);assert.ok(!theory.querySelector('details').open);assert.match(card.querySelector('.reader-branch').textContent,/当时选题/);e.fire('pagehide');
  });
  console.log('Completed '+passed+' workspace interaction fixtures; real browser validation is separate.');
})().catch(error=>{console.error(error);process.exitCode=1;});
