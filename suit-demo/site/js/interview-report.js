(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const params = new URLSearchParams(location.hash.slice(1));
  const sessionId = params.get('session') || '', token = params.get('token') || '';
  const factors = {leadership:'领导意愿',develop_others:'成就他人',balance_people_task:'平衡人际与任务',results:'结果导向',systems_thinking:'全局思维',adaptability:'适应力',feedback:'接纳反馈',learning:'学习敏锐度'};
  let activeSection="overview",activeQuestion=params.get("question")||"",summaryQuestion="";
  let report, pendingReport=null, pollTimer=null, loading=false, disposed=false, focusIntent=0;
  const retryingQuestions=new Set(),retryOperations=new Map();
  const visualModules = new Set(), timelineViews = new Map(), multimodalViews=new Set(),observationViews=new Map();
  const el = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = window.SuitTextDisplay?.simplify(String(text))??String(text); return node; };
  const date = value => value ? new Date(value).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai',hour12:false}) : '暂无';
  const source = q => q.review_stale ? '转写已更新，需重新复核' : q.score_source === 'hr' ? 'HR / 专家复核' : q.score_source === 'model_draft' ? '模型草稿 · 待人工复核' : '尚未评分';
  const score = value => value == null ? 'N/A' : String(value) + ' / 5';
  const actionName = value => ({ask_followup:'选择题库追问',continue_main:'继续下一道主问',continue:'继续下一道主问',finish:'结束提问'})[value] || '已记录流程决定';

  function selectSection(name,scroll=false) {
    const changed=activeSection!==name;activeSection=name;
    for(const key of ['overview','evidence','process','history']) { $(''+key+'Panel').hidden=key!==name; $('section'+key[0].toUpperCase()+key.slice(1)).setAttribute('aria-pressed',String(key===name)); }
    if(changed)timelineViews.forEach(view=>view.pause?.());
    if(scroll)$(''+name+'Panel').scrollIntoView({behavior:'smooth',block:'start'});
  }
  function selectQuestion(id,scroll=true) {
    if(activeQuestion!==id)timelineViews.forEach(view=>view.pause?.());activeQuestion=id;selectSection('evidence');
    for(const q of report?.questions||[]) $('question-'+q.question_id).hidden=q.question_id!==id;
    for(const button of $('readerQuestionNav').children)button.setAttribute('aria-pressed',String(button.getAttribute('data-question')===id));
    if(scroll)$('question-'+id)?.scrollIntoView({behavior:'smooth',block:'start'});
  }
  function answerCountSafe(data){return (data.answers||[]).some(a=>a.current!==false);}
  function questionState(q) {
    const answers=(q.answers||[]).filter(a=>a.current!==false),ids=new Set(answers.map(a=>a.answer_id));
    const jobs=(report?.jobs||[]).filter(j=>j.status!=='superseded'&&(j.question_id===q.question_id||ids.has(j.answer_id))),content=window.SuitReportReader?.contentContext(report||{},q).job;
    if(q.review_stale)return '依据已更新，需复核';
    if(q.score_source==='hr'&&q.score==null)return '老师暂缓判断';
    if(q.score!=null)return q.score_source==='hr'?'老师已复核':'模型初评';
    if(!answers.length)return '尚未作答';
    if(jobs.some(j=>j.job_type==='media'&&['queued','running'].includes(j.status)))return '正在转写';
    if(!answers.some(a=>a.transcript?.trim()))return '未取得可用转写';
    if(content&&['queued','running'].includes(content.status))return '正在分析回答';
    if(q.model_evaluation)return '当前证据不足以评分';
    if(content&&['failed','blocked'].includes(content.status))return '内容分析暂不可用';
    return '待内容评价';
  }
  function modelOpinion(q,text) {
    if(q.score_source!=='hr'&&!q.review_stale)text=window.SuitReportReader?.modelReason(text)??text;
    if(q.score!=null||!q.model_evaluation||q.score_source==='hr'||q.review_stale)return text;
    const hasMedia=(q.answers||[]).some(a=>a.current!==false&&a.has_media);
    return (hasMedia?'基于当前转写的模型意见（待核对原片）':'基于当前文字的模型意见（待核对）')+'：'+text;
  }
  function readableReason(q) {
    const state=questionState(q);
    if(state==='未取得可用转写')return '已保存录音录像，但没有可用的回答文字。请先回听本题，核对录音与转写。';
    if(state==='正在转写')return '原始回答已保存，文字正在生成；可先回放录音录像。';
    if(state==='正在分析回答')return '已取得回答文字，内容评价正在后台生成。';
    if(state==='内容分析暂不可用'){const context=window.SuitReportReader?.contentContext(report,q);return (context?.prior?'主问与追答均已保存，合并评价尚未完成。':'回答文字已保存，本题内容分析尚未完成。')+(context?.job?'原因：'+window.SuitReportReader.contentError(context.job)+'。':'')+(params.get('version')?'这是历史报告，可返回最新报告查看恢复情况。':'可重新分析本题，或先核对已有材料。');}
    if(state==='尚未作答')return '本题尚无回答，不能形成评价。';
    return q.reason?modelOpinion(q,q.reason):'尚无可核验的评分依据。';
  }

  async function retryContent(question,job){
    if(params.get('version'))throw new Error('当前为历史报告，请返回本场最新报告后重试。');
    if(retryingQuestions.has(question.question_id))return;
    const key=question.question_id+':'+job.job_id;
    if(!retryOperations.has(key))retryOperations.set(key,{question_id:question.question_id,version:report.version,idempotency_key:crypto.randomUUID()});
    retryingQuestions.add(question.question_id);
    try{
      const response=await fetch('/v1/interviews/'+encodeURIComponent(sessionId)+'/content-analysis/retry',{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify(retryOperations.get(key))});
      const data=await response.json();
      if(!response.ok){if(response.status===409)retryOperations.delete(key);throw new Error(response.status===409?'本场已有更新，请先刷新结果，再核对本题重试。':data.detail?.message||'未能提交本题分析，请稍后重试。');}
      retryOperations.delete(key);await load();notice('本题文字分析已重新排队，已有回答和观察可以继续查看。');
    }finally{retryingQuestions.delete(question.question_id);}
  }

  function briefButton(label,fn) {const b=el('button','reader-evidence no-print',label);b.type='button';b.addEventListener('click',()=>Promise.resolve().then(fn).catch(error=>notice(error.message||'暂时无法打开这条依据','warning')));return b;}
  function renderBriefs(data) {
    const qs=data.questions||[],valid=qs.filter(q=>q.score!=null&&!q.review_stale),unreviewed=qs.filter(q=>q.score_source!=='hr'||q.review_stale);
    const pending=(data.jobs||[]).some(j=>['queued','running'].includes(j.status)),followups=((data.decision_events?.length?data.decision_events:data.decisions)||[]).filter(d=>d.action==='ask_followup');
    summaryQuestion=(qs.find(q=>q.score==null||q.review_stale)||unreviewed[0]||qs[0])?.question_id||'';
    $('summaryTitle').textContent=!qs.some(q=>q.answers?.length)?'尚未提交回答':valid.length===qs.length&&qs.length?'本场各题已有内容评价':valid.length?'部分题目已有评价，其余需要核对':pending?'回答已保存，评价正在生成':'本场暂不能形成内容评分';
    $('summaryDescription').textContent=qs.length?'本轮考察 '+qs.map(q=>factors[q.factor_id]||q.factor_id).join('、')+'；实际追加 '+followups.length+' 道追问。'+(unreviewed.length?'以下为当前依据，等待老师核对。':'老师复核已保存。'):'请先完成面试，再查看本场评价。';
    $('summaryAction').hidden=!summaryQuestion;
    $('summaryAction').textContent=valid.length===qs.length?'查看各题评分依据 →':'先核对尚无有效评分的题目 →';
    $('openReview').href='/hr-review#'+new URLSearchParams({session:sessionId,token,...(summaryQuestion?{question:summaryQuestion}:{})});
    $('openReview').textContent=params.get('version')?'转到最新报告复核':unreviewed.length?'开始逐题复核':'查看已保存复核';
    const holder=$('factorScores');holder.replaceChildren();
    for(const [i,q]of qs.entries()) { const card=el('article','report-factor-brief'),head=el('div','report-factor-title');head.append(el('h3','',factors[q.factor_id]||q.factor_id),el('strong','',q.score==null?'—':q.score+' / 5'));card.append(el('span','eyebrow','第 '+(i+1)+' 题'),head,el('p','report-factor-state',questionState(q)),el('p','report-factor-reason',readableReason(q)));
      const context=window.SuitReportReader?.contentContext(data,q),model=q.model_evaluation||context?.prior,refs=(q.score_source==='hr'||q.review_stale)?q.evidence||[]:model?.rating?.evidence||q.evidence||[];
      if(refs[0]?.quote){const basis=el('div','report-factor-evidence');basis.append(el('span','small muted',context?.prior?'主问已有材料':'一条回答依据'),el('p','','“'+Array.from(refs[0].quote).slice(0,70).join('')+(Array.from(refs[0].quote).length>70?'…':'')+'”'));card.append(basis);}
      const previous=window.SuitReportReader?.previousDraft(data,q,{compact:true});if(previous)card.append(previous);
      card.append(briefButton(q.score==null?'核对回答 →':'查看评价依据 →',()=>selectQuestion(q.question_id)));holder.append(card); }
    const attention=$('attentionItems');attention.replaceChildren();
    for(const q of qs) { const gap=!q.review_stale&&q.score_source!=='hr'?q.model_evaluation?.gaps?.[0]?.description:null; if(q.score==null||q.review_stale||gap){const row=el('div','report-attention-row');row.append(el('strong','',factors[q.factor_id]||q.factor_id),el('p','',gap?modelOpinion(q,gap):readableReason(q)),briefButton('去核对',()=>selectQuestion(q.question_id)));attention.append(row);}}
    $('attentionSection').hidden=!attention.children.length;
    const obs=$('observationBriefs');obs.replaceChildren();
    for(const q of qs) {const row=el('div','report-observation-brief');row.append(el('strong','',factors[q.factor_id]||q.factor_id));let found=null,media=false;
      for(const a of q.answers||[]){media=media||a.has_media;const selected=window.SuitReportReader?.selectAnalysis(data,a);for(const src of selected?.sources||[]){const evs=src.item.reader_summary?.events||[];const event=[...evs.filter(e=>e.kind==='apparent_expression')].sort((a,b)=>(b.measurements?.sampled_frames||0)-(a.measurements?.sampled_frames||0))[0]||evs.find(e=>e.kind?.startsWith('audio_'))||evs[0];if(event){found={event,answer:a};break;}}if(found)break;}
      if(found){const {event,answer}=found;row.append(el('p','',event.title+' · '+window.SuitReportReader.stamp(event.start_ms)+'–'+window.SuitReportReader.stamp(event.end_ms)),el('span','small muted','一个可回放的观察片段，不代表整场状态'),briefButton('查看观察与原片 →',async()=>{selectQuestion(q.question_id,false);const timeline=timelineFor(answer.answer_id,answer.transcript_revision,$('question-'+q.question_id));const channel=event.kind==='gaze_direction'?'gaze':event.evidence_refs?.[0]?.module||event.evidence_refs?.[0]?.module_id;const metric=event.kind==='apparent_expression'?'raw_scores.'+event.label:event.kind==='audio_pitch'?'f0_hz':event.kind==='audio_energy'?'rms_dbfs':null;observationViews.get(answer.answer_id+':'+answer.transcript_revision)?.setChannel(channel,metric);await locateObservation(timeline,event.anchor_ms??event.start_ms,{play:false});}));}
      else {row.append(el('p','',media?'暂无可读观察，进入本题查看处理情况或回放原片。':'本题未采集音视频。'));if(media)row.append(briefButton('查看本题',()=>selectQuestion(q.question_id)));}obs.append(row);
    }
  }

  function disposeMedia() {
    focusIntent++;
    visualModules.forEach(view => view.dispose()); visualModules.clear();
    multimodalViews.forEach(view=>view.dispose());multimodalViews.clear();observationViews.clear();
    timelineViews.forEach(view => view.dispose()); timelineViews.clear();
  }
  async function fetchAnswerResource(path, asBlob=false) {
    const response = await fetch('/v1/interviews/' + encodeURIComponent(sessionId) + path, {headers:{Authorization:'Bearer ' + token},cache:'no-store'});
    if (!response.ok) throw new Error(response.status === 403 ? '需要本场 HR 访问权限。' : '无法读取指定版本的证据或原始媒体，请稍后重试。');
    return asBlob ? response.blob() : response.json();
  }
  function timelineFor(answerId, revision, host) {
    if (!Number.isInteger(revision) || revision < 1) throw new Error('引用缺少明确转写版本，不能以最新转写替代。');
    const key = answerId + ':' + revision;
    if (timelineViews.has(key)) return timelineViews.get(key);
    const answer = [...(report.answers || []),...(report.questions || []).flatMap(q => q.answers || [])].find(item => item.answer_id === answerId) || {answer_id:answerId};
    const view = window.SuitEvidenceTimeline.mount({answer,sessionId,revision,showReferences:false,
      fetchJSON:version => fetchAnswerResource('/answers/' + encodeURIComponent(answerId) + '/timeline?transcript_revision=' + version),
      fetchMedia:() => fetchAnswerResource('/media/' + encodeURIComponent(answerId),true),
      onOpenRevision:version => {
        try { const historic = timelineFor(answerId,version,host); historic.element.parentElement.open = true; historic.load().catch(error => notice(error.message,'warning')); historic.element.scrollIntoView({behavior:'smooth',block:'nearest'}); }
        catch (error) { notice(error.message,'warning'); }
      }
    });
    view.element.addEventListener('click',()=>{focusIntent++;});
    timelineViews.set(key,view);
    const detail = el('details','evidence-history-view'); detail.open = true;
    detail.append(el('summary','','报告引用转写 v' + revision + ' · 只读回放'),view.element);
    (host || $('decisionRecords')).append(detail); return view;
  }
  function reveal(view) { for(let n=view.element.parentElement;n;n=n.parentElement){if(n.classList?.contains('report-question'))selectQuestion(n.id.slice('question-'.length),false);if(n.tagName==='DETAILS')n.open=true;} view.element.scrollIntoView({behavior:'smooth',block:'nearest'}); return view; }
  async function openEvidence(ref,host) { const intent=++focusIntent,view=reveal(timelineFor(ref.answer_id,ref.transcript_revision,host));await view.openQuote(ref);if(intent===focusIntent&&view.element.isConnected)reveal(view); }
  async function locateObservation(view,ms,options) { const intent=++focusIntent;reveal(view);await view.seek(ms,options);if(intent===focusIntent&&view.element.isConnected)reveal(view); }
  function referenceLink(evidence, host) {
    const holder = el('div'), locate = el('button','evidence-reference-link no-print','定位原话与片段'), status = el('span','evidence-reference-error'); locate.type = 'button'; status.setAttribute('role','status');
    locate.addEventListener('click',async () => {
      status.textContent = '';
      try {
        if (!evidence.answer_id) throw new Error('此历史引用缺少回答标识，无法定位。');
        await openEvidence(evidence,host);
      } catch (error) { status.textContent = error.message; }
    });
    holder.append(locate,status); return holder;
  }
  function appendEvidence(holder, evidence, label) {
    for (const item of evidence || []) {
      const quote = el('blockquote','report-quote');
      quote.append(el('p','','“' + item.quote + '”'),el('cite','',label + ' · 转写 v' + item.transcript_revision),referenceLink(item,holder));
      holder.append(quote);
    }
  }
  function renderDecision(decision, data) {
    if(window.SuitReportReader)return window.SuitReportReader.decision(decision,{question:data.questions?.find(q=>q.question_id===decision.parent_question_id),factor:data.potential_profile?.factors?.find(f=>f.question_ids?.includes(decision.parent_question_id)),onEvidence:ref=>openEvidence(ref,$('decisionRecords'))});
    const block = el('div','report-record report-decision');
    const parent = (data.questions || []).find(q => q.question_id === decision.parent_question_id);
    const branch = (data.rounds || []).find(r => r.question_id === decision.selected_branch_id);
    block.append(el('strong','',actionName(decision.action)),el('p','small muted',date(decision.decision_time) + (parent ? ' · ' + (factors[parent.factor_id] || parent.factor_id) : '')));
    if (parent) block.append(el('p','small','对应主问：' + parent.text));
    block.append(el('p','',decision.reason_for_hr || '具体理由未记录。'));
    if (branch) block.append(el('p','report-selected-branch','追问：' + branch.text));
    if (decision.target_evidence) block.append(el('p','small','待补充证据：' + decision.target_evidence));
    appendEvidence(block, decision.used_evidence, '决策时引用');
    const evidenceCount = (decision.used_evidence || []).length;
    if (!evidenceCount) block.append(el('p','small muted','当时未取得可用于本次决定的内容引用；该记录不构成候选人能力或诚信判断。'));
    const details = el('details','report-decision-detail');
    details.append(el('summary','','查看决策来源与可用信息'));
    details.append(el('p','small','规则版本：' + (decision.policy_version || '未记录') + ' · 截止时间：' + date(decision.evidence_cutoff_time)));
    const allowed = decision.allowed_branch_ids || decision.candidate_branch_ids || [];
    details.append(el('p','small','记录中的可选分支：' + (allowed.length ? allowed.join('、') : '无')));
    const runs = decision.model_run_ids || [];
    details.append(el('p','small','参与本次决定的模型运行：' + (runs.length ? runs.join('、') : '无可用模型运行')));
    const used = Object.entries(decision.input_versions || {}).map(([id, version]) => id.slice(0,8) + ' / v' + version);
    details.append(el('p','small','回答版本：' + (used.join('；') || '未记录')));
    const hasEnergy = (decision.observed_only || []).some(item => item.audio?.status === 'ok' && item.audio?.windows?.some(window => Number.isFinite(window.rms_dbfs)));
    details.append(el('p','small','音视频信息：' + (hasEnergy ? '已记录本地录音能量观测；没有用于选择追问。' : '本次决定没有可用的录音能量观测。') + ' 面部与声线不参与人格、心理或诚信判断。'));
    block.append(details,el('p','small muted','记录保留面试当时的输入与决定。后续分析或转写修订不会改写它。'));
    return block;
  }
  function notice(text, kind='') { $('notice').textContent = text; $('notice').className = 'notice ' + kind; $('notice').hidden = !text; }
  const workbenchUrl = '/workbench#session=' + encodeURIComponent(sessionId);
  $('backWorkbench').href = workbenchUrl;
  $('openReview').href = '/hr-review#' + new URLSearchParams({session:sessionId,token}).toString();
  $('openResearch').href = '/research#'+new URLSearchParams({session:sessionId,token});
  function renderQuestion(q, index) {
    const card=el('section','card report-question');card.id='question-'+q.question_id;
    const head=el('div','report-question-head'),heading=el('div');heading.append(el('div','eyebrow','主问题 '+(index+1)+' · '+(factors[q.factor_id]||q.factor_id)),el('h3','',q.text));head.append(heading,el('div','report-question-score',score(q.score)));card.append(head);
    card.append(el('p','small '+(q.review_stale?'report-missing':'muted'),questionState(q)));
    if(q.score_source==='model_draft'&&(q.answers||[]).some(a=>a.has_media))card.append(el('p','reader-transcript-note','初评依据当前自动转写。数字、专有词和可疑错词请先回听核对，再保存老师复核。'));
    const recovery=window.SuitReportReader?.contentRecovery(report,q,{onRetry:params.get('version')?null:retryContent});if(recovery)card.append(recovery);
    const previous=window.SuitReportReader?.previousDraft(report,q,{onEvidence:ref=>openEvidence(ref,card)});if(previous)card.append(previous);
    const story=window.SuitReportReader?.questionStory(report,q,{onEvidence:ref=>openEvidence(ref,card),currentReason:readableReason(q)});
    if(story){card.append(story);appendEvidence(story.querySelector('.reader-story-conclusion'),q.evidence,'当前评分引用');}else{card.append(el('p','report-question-reason',readableReason(q)));appendEvidence(card,q.evidence,'评分引用');}
    const decisions=(report.decision_events?.length?report.decision_events:report.decisions)||[];
    const ownDecisions=decisions.filter(d=>d.parent_question_id===q.question_id);if(ownDecisions.length){const detail=el('details','reader-secondary');detail.append(el('summary','','当时的追问依据与推进记录（'+ownDecisions.length+' 条）'));for(const decision of ownDecisions)detail.append(renderDecision(decision,report));card.append(detail);}
    for(const answer of q.answers||[]){
      const block=el('div','report-answer');block.append(el('h3','',answer.parent_question_id?'补充追问回答':'主问回答'));
      const details=el('details','reader-answer-detail');details.setAttribute('data-reader-key',answer.answer_id);details.append(el('summary','','查看原话、录影片段与采集点'));
      details.append(el('p','small muted','提交 '+date(answer.created_at)+' · 原话 v'+answer.transcript_revision),el('p','report-answer-text',answer.transcript||'尚无可读文字，已保存原片可回放。'));
      const timeline=timelineFor(answer.answer_id,answer.transcript_revision,details);
      const analysis=window.SuitReportReader?.selectAnalysis(report,answer);
      let view=null;
      if(analysis&&window.SuitMultimodalView){view=window.SuitMultimodalView.mount({data:analysis.item,timeline,sourceLabel:'本场冻结观察 · 按模块保留来源'});multimodalViews.add(view);observationViews.set(answer.answer_id+':'+answer.transcript_revision,view);details.append(view.element);}
      if(window.SuitReportReader)block.append(window.SuitReportReader.observations({analysis,answer,status:report.standard_analysis?.answers?.find(a=>a.answer_id===answer.answer_id)?.status,onChannel:(key,metric)=>view?.setChannel(key,metric),getTimeline:()=>timeline.load(),onPreview:async(event,host)=>{timeline.pause();host.append(timeline.element);const face=event.kind==='apparent_expression',gaze=event.kind==='gaze_direction',key=face?'geometry':gaze?'gaze':'audio',metric=event.kind==='audio_pitch'?'f0_hz':'rms_dbfs';view?.setChannel(key,face||gaze?undefined:metric);await locateObservation(timeline,face?(event.geometry?.anchor_ms??event.anchor_ms??event.start_ms):(event.anchor_ms??event.start_ms),{play:false});},onSeek:(ms,options)=>locateObservation(timeline,ms,options)}));
      if(answer.has_media&&window.SuitVisualEvidence&&!analysis){const legacy=window.SuitVisualEvidence.mount({answer,fetchJSON:()=>fetchAnswerResource('/visual/'+encodeURIComponent(answer.answer_id)),fetchFrame:i=>fetchAnswerResource('/visual/'+encodeURIComponent(answer.answer_id)+'/frames/'+i,true),onSeek:ms=>{reveal(timeline);return timeline.seek(ms);}});visualModules.add(legacy);details.append(legacy.element);}
      block.append(details);card.append(block);
    }
    if(q.model_evaluation?.evidence_units){const semantic=el('details','reader-secondary');semantic.append(el('summary','','查看内容证据与锚点对应'),window.SuitResearchSummary.semantic(q.model_evaluation,ref=>openEvidence(ref,card)));card.append(semantic);}
    const review=el('a','button subtle no-print','复核本题');review.href='/hr-review#'+new URLSearchParams({session:sessionId,token,question:q.question_id});card.append(review);return card;
  }
  function renderPotentialConclusion(data){
    const renderer=window.SuitPotentialProfile?.mountConclusion;
    for(const [id,compact]of [['potentialConclusionOverview',true],['potentialConclusion',false]]){
      const host=$(id);if(!host)continue;
      if(renderer)host.replaceChildren(renderer({conclusion:data.potential_conclusion,compact,onEvidence:ref=>openEvidence(ref,host)}).element);
      else host.replaceChildren(el('p','muted','本报告尚未提供逐评价点潜力结论，请核对已保存的原话与题级依据。'));
    }
  }
  function questionCount(data){const count=Number(data.main_question_count);return Number.isInteger(count)&&count>0?count:(data.questions||[]).length||3;}
  function render(data) {
    pendingReport=null;$('reportUpdates').hidden=true;
    const opened=[...document.querySelectorAll('[data-reader-key]')].filter(n=>n.open).map(n=>n.getAttribute('data-reader-key'));
    disposeMedia();
    report = data;
    renderPotentialConclusion(data);
    const profileVersion=data.version;
    (data.potential_profile?Promise.resolve(data.potential_profile):fetchAnswerResource('/factor-profile?report_version='+data.version)).then(profile=>{if(report.version===profileVersion)$('potentialProfile').replaceChildren(window.SuitPotentialProfile.mount({profile,onEvidence:ref=>openEvidence(ref,$('potentialProfile'))}).element);}).catch(error=>notice(error.message,'warning'));
    $('researchAppendix').replaceChildren(window.SuitResearchSummary.mount({appendix:data.research_appendix,onEvidence:ref=>openEvidence(ref,$('researchAppendix')),onSeek:(aid,rev,ms)=>reveal(timelineFor(aid,rev,$('researchAppendix'))).seek(ms)}).element);
    $('reportDocument').hidden = false; $('accessCard').hidden = true;
    $('candidateLabel').textContent = window.SuitTextDisplay?.simplify(data.candidate_ref || '本场面试报告') ?? (data.candidate_ref || '本场面试报告');
    $('reportVersion').textContent = '报告版本 v' + data.version;
    $('createdAt').textContent = '面试创建：' + date(data.created_at);
    $('generatedAt').textContent = '报告生成：' + date(data.generated_at);
    const questions = data.questions || [];
    const reviewed = questions.length > 0 && questions.length === questionCount(data) && questions.every(q => q.score_source === 'hr' && !q.review_stale);
    const pending = (data.jobs || []).some(j => ['queued','running'].includes(j.status));
    const blocked = (data.jobs || []).some(j => j.job_type === 'content' && ['blocked','failed'].includes(j.status));
    const hasModel = questions.some(q => q.model_evaluation);
    $('reviewBadge').textContent = reviewed ? '已人工复核' : pending ? '分析处理中' : hasModel && questions.some(q=>q.score!=null) ? '模型初评 · 待复核' : answerCountSafe(data) ? '需核对回答'  : '待 HR / 专家复核';
    $('reviewBadge').className = 'pill' + (reviewed ? '' : ' report-draft');
    const answerCount=(data.answers||[]).filter(a=>a.current!==false).length;
    const rs=data.reader_summary||{}, overview=$('readerOverview');overview.replaceChildren();
    for(const [label,value]of [['已保存回答',rs.answered_count??answerCount],['内容待完成',rs.content_pending_count??(data.jobs||[]).filter(j=>j.job_type==='content'&&['queued','running'].includes(j.status)).length],['观察处理中',rs.multimodal_pending_count??data.standard_analysis?.pending_count??0],['存在观察缺失',rs.multimodal_failed_count??data.standard_analysis?.failed_count??0]]){const cell=el('div');cell.append(el('strong','',value),el('span','',label));overview.append(cell);}
    const standardRuns=data.standard_analysis?.runs||[];
    const researchState=standardRuns.some(r=>r.result)?'逐题依据中可查看本场声音、表情与面部动作，并定位原片。':standardRuns.some(r=>['queued','running'].includes(r.status))?'本场多模态仍在处理中，已有内容结果可先核对。':data.research_appendix?.selected_runs?.length?'逐题依据与研究附录中可查看已选多模态结果。':'本场暂无可用多模态结果，逐题会说明处理状态或缺失。';
    $('reportState').textContent = params.get('version')?'历史报告 v'+data.version+' · 只读快照，不包含此后完成的分析或复核。':!answerCount?'当前场次还没有提交回答。':('已保存 '+answerCount+' 条回答。'+(reviewed?'老师复核已保存。':pending||standardRuns.some(r=>['queued','running'].includes(r.status))?'报告正在逐步完善，已有内容可先查看。':'按题查看内容依据、观察与未完成项。'));
    $('totalScore').textContent = data.total_score == null ? '—' : Number(data.total_score).toFixed(1);
    $('scoreExplanation').textContent = data.total_score == null ? '本场 '+questionCount(data)+' 道主问均有有效评分且面试结束后生成' : questionCount(data)+' 道主问等权换算 · '+(reviewed?'人工复核':'尚待人工复核');
    $('scoredCount').textContent = '有效评价 '+(data.scorable_main_count || 0)+' / '+questions.length+' 题';
    const planned = data.coverage?.planned_factor_ids || [], uncovered = data.coverage?.uncovered_factor_ids || [];
    $('factorCoverage').textContent = '本轮覆盖 '+planned.length+' / '+(planned.length+uncovered.length)+' 个因子';
    $('uncovered').textContent = uncovered.length?'本轮未测：' + uncovered.map(f => factors[f] || f).join('、') + '。':'已安排全部因子；是否已有充分证据仍需逐评价点核对。';
    $('questionOverviewTitle').textContent='本场 '+questions.length+' 道主问，分别核对内容依据';
    $('reportCoverageLimit').textContent='本轮安排 '+planned.length+' / '+(planned.length+uncovered.length||8)+' 个因子。'+(uncovered.length?'这是局部覆盖，不能作为完整八因子评估。':'题目覆盖不等于所有评价点均已体现。')+'未测或无法判断的项目留空；声音、表情和视线不计入内容评分。';
    $('reportScoreLimit').textContent='本场 '+questionCount(data)+' 道主问均可评分且面试结束后，按（题分 − 1）× 25 等权平均。锚点仍待老师校准；不等同于岗位匹配率。';
    $('nextStep').textContent = reviewed ? '已保存人工复核。可以打印、保存 PDF 或下载报告数据；总分为 N/A 时，请阅读逐题的缺失说明。' : pending ? '分析仍在后台继续，本页会自动更新。现在即可阅读已完成内容，点击原话或观察定位原片。' : blocked && !hasModel ? '回答已保存。逐题依据会说明内容分析未完成的原因，可只重新分析对应题目；老师也可先核对已有材料。' : '阅读下方逐题报告。需要修改或确认评分时，点击该题的“复核本题”；研究实验在高级工具中。';
    if(!answerCount)$('nextStep').textContent='这是一场空面试，不是分析加载失败。请点击“面试工作台”，按创建时间和已答题数选择正确场次。';
    renderBriefs(data);
    $('questionReports').replaceChildren(...questions.map(renderQuestion));
    for(const node of document.querySelectorAll('[data-reader-key]'))node.open=opened.includes(node.getAttribute('data-reader-key'));
    const nav=$('readerQuestionNav');nav.replaceChildren();if(!questions.some(q=>q.question_id===activeQuestion))activeQuestion=questions[0]?.question_id||'';
    for(const [i,q]of questions.entries()){const button=el('button','reader-question-link','第 '+(i+1)+' 题 · '+(factors[q.factor_id]||q.factor_id));button.type='button';button.setAttribute('data-question',q.question_id);button.setAttribute('aria-pressed',String(activeQuestion===q.question_id));button.addEventListener('click',()=>selectQuestion(q.question_id));nav.append(button);$('question-'+q.question_id).hidden=q.question_id!==activeQuestion;}
    selectSection(activeSection);
    const decisions = $('decisionRecords'); decisions.replaceChildren();
    const events = data.decision_events?.length ? data.decision_events : data.decisions || [];
    if (!events.length) decisions.append(el('p','muted','尚未到达追问决策环节。'));
    for (const [i,decision] of events.entries()){const q=questions.find(q=>q.question_id===decision.parent_question_id),step=el('article','report-process-step');step.append(el('h3','',q?(factors[q.factor_id]||q.factor_id)+' · '+(decision.action==='ask_followup'?'追加追问':'推进记录'):'流程记录 '+(i+1)),renderDecision(decision,data));decisions.append(step);}
    const history = $('reviewRecords'); history.replaceChildren();
    if (!data.reviews?.length) history.append(el('p','muted','尚未保存人工复核。请在复核页完成评分、引用和理由。'));
    for (const review of [...(data.reviews || [])].reverse()) {
      const block = el('div','report-record');
      block.append(el('strong','','人工复核 · 基于报告 v' + review.parent_version),el('p','small muted',date(review.created_at)),el('p','',review.reason));
      appendEvidence(block,(review.ratings || []).flatMap(rating => rating.evidence || []),'保存时引用');
      history.append(block);
    }
  }
  async function load() {
    if(loading||disposed)return;loading=true;
    if (!sessionId || !token) {loading=false; $('accessCard').hidden = false; notice('请从面试工作台进入本场报告。','warning'); return; }
    $('refreshReport').disabled = true;
    try {
      const requestedVersion=params.get('version');
      const response = await fetch('/v1/interviews/' + encodeURIComponent(sessionId) + '/report'+(requestedVersion?'?version='+encodeURIComponent(requestedVersion):''),{headers:{Authorization:'Bearer ' + token},cache:'no-store'});
      const data = await response.json();
      if (!response.ok) { const error = new Error(data.detail?.message || '无法读取报告，请确认 HR 访问权限。'); error.status = response.status; throw error; }
      if(disposed)return;
      if(report&&[...timelineViews.values()].some(view=>view.isPlaying?.())){pendingReport=data;$('reportUpdates').hidden=false;}else{render(data);notice('');}
    } catch (error) { notice(error.message || '本机服务暂时无法连接，请稍后重试。','error'); if (!report) $('accessCard').hidden = false; }
    finally { loading=false;$('refreshReport').disabled = false; }
  }
  for(const key of ['overview','evidence','process','history'])$('section'+key[0].toUpperCase()+key.slice(1)).addEventListener('click',()=>selectSection(key));
  $('summaryAction').addEventListener('click',()=>{if(summaryQuestion)selectQuestion(summaryQuestion);});
  $('refreshReport').addEventListener('click',load);
  $('showConclusion').addEventListener('click',()=>{$('conclusionFooter').scrollIntoView({behavior:'smooth',block:'start'});$('conclusionTitle').focus({preventScroll:true});});
  window.addEventListener('beforeunload',disposeMedia);
  window.addEventListener('pagehide',()=>{disposed=true;clearInterval(pollTimer);disposeMedia();});
  $('applyReportUpdate').addEventListener('click',()=>{if(pendingReport){render(pendingReport);pendingReport=null;$('reportUpdates').hidden=true;}});
  window.addEventListener('hashchange',()=>{const next=new URLSearchParams(location.hash.slice(1));if((next.has('session')||next.has('token'))&&(next.get('session')!==sessionId||next.get('token')!==token||next.get('version')!==params.get('version')))location.reload();});
  let printURL = null, printDetails = [];
  // Also applies to browser-menu printing and Ctrl+P, not only our button.
  window.addEventListener('beforeprint',() => {
    if (printURL === null) printURL = location.href;
    history.replaceState(null,'',location.pathname);
    printDetails = [...document.querySelectorAll('#reportDocument details')].filter(node => !node.open);
    printDetails.forEach(node => { node.open = true; });
  });
  window.addEventListener('afterprint',() => {
    if (printURL !== null) history.replaceState(null,'',printURL);
    printURL = null; printDetails.forEach(node => { node.open = false; }); printDetails = [];
  });
  $('savePdf').addEventListener('click',async()=>{if(!report)return;const version=report.version,button=$('savePdf');button.disabled=true;button.textContent='正在生成 PDF…';try{const response=await fetch('/v1/interviews/'+encodeURIComponent(sessionId)+'/report.pdf?version='+encodeURIComponent(version),{headers:{Authorization:'Bearer '+token},cache:'no-store'});if(!response.ok){let body={};try{body=await response.json();}catch(_){}throw new Error(body.detail?.message||'PDF 生成失败，请稍后重试。');}const blob=await response.blob();if(!blob.size||!String(response.headers.get('content-type')).includes('application/pdf'))throw new Error('返回内容不是有效 PDF，请重新生成。');const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='面试报告-v'+version+'.pdf';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),60000);notice('PDF 已生成并开始下载，请查看浏览器下载列表。');}catch(error){notice(error.message,'warning');}finally{button.disabled=false;button.textContent='下载 PDF 报告';}});
  $('printReport').addEventListener('click',() => { if (report) window.print(); });
  $('downloadReport').addEventListener('click',() => {
    if (!report) return;
    const data = {purpose:'模拟测试／方法验证'};
    data.standard_analysis=report.standard_analysis;data.reader_summary=report.reader_summary;data.potential_profile=report.potential_profile;data.potential_conclusion=report.potential_conclusion;data.research_appendix=report.research_appendix;data.model_evaluations=report.questions.map(q=>q.model_evaluation).filter(Boolean);
    for (const key of ['session_id','candidate_ref','status','version','generated_at','created_at','score_name','total_score','total_status','scorable_main_count','coverage']) data[key] = report[key];
    data.questions = (report.questions || []).map(q => ({question_id:q.question_id,factor_id:q.factor_id,text:q.text,score:q.score,score_source:q.score_source,review_stale:q.review_stale,reason:q.reason,evidence:q.evidence,answers:(q.answers || []).map(a => ({answer_id:a.answer_id,transcript:a.transcript,transcript_revision:a.transcript_revision,parent_question_id:a.parent_question_id,created_at:a.created_at,has_media:a.has_media}))}));
    data.reviews = (report.reviews || []).map(r => ({review_id:r.review_id,parent_version:r.parent_version,created_at:r.created_at,reason:r.reason,ratings:r.ratings}));
    data.decisions = (report.decisions || []).map(d => ({decision_time:d.decision_time,action:d.action,selected_branch_id:d.selected_branch_id,reason:d.reason_for_hr}));
    data.decision_events = (report.decision_events?.length ? report.decision_events : report.decisions || []).map(d => ({decision_id:d.decision_id,decision_time:d.decision_time,evidence_cutoff_time:d.evidence_cutoff_time,action:d.action,parent_question_id:d.parent_question_id,selected_branch_id:d.selected_branch_id,policy_version:d.policy_version,reason_for_hr:d.reason_for_hr,used_evidence:d.used_evidence,input_versions:d.input_versions,model_run_ids:d.model_run_ids,allowed_branch_ids:d.allowed_branch_ids,candidate_branch_ids:d.candidate_branch_ids,budget_before:d.budget_before,budget_after:d.budget_after,decision_summary:d.decision_summary,model_comparison:d.model_comparison}));
    const url = URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json;charset=utf-8'}));
    const link = el('a'); link.href = url; link.download = 'interview-report-' + sessionId.slice(0,12) + '-v' + report.version + '.json'; link.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
  });
  load();
  if(typeof setInterval==='function'&&!params.get('version'))pollTimer=setInterval(async()=>{if(disposed||document.hidden||loading||!report)return;try{const state=await fetchAnswerResource('/analysis-status');if(!disposed&&state.version!==report.version)await load();}catch(_){/* Keep the readable report during a temporary disconnect. */}},5000);
})();
