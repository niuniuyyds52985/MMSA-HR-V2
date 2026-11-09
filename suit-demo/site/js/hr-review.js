(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const params = new URLSearchParams(location.hash.slice(1));
  const sessionId = params.get('session'), token = params.get('token');
  const fields = new Map(), visualViews = new Set(), timelineViews = new Map(), multimodalViews = new Set();
  let report = null, dirty = false, busy = false, reviewOperation = null, assessmentOperation = null, assessmentSource = null;
  let activeQuestion = params.get('question') || '', activeTab = 'review', incomingReport = null, pollTimer = null, disposed = false, profileVersionLoaded = null;
  const draftKey = 'zhenjian-review-draft-v2:' + sessionId;
  const touchedQuestions = new Set(),contentRetrying=new Set(),contentRetryOperations=new Map();
  let restoredDraft = null;
  try { restoredDraft = JSON.parse(sessionStorage.getItem(draftKey) || 'null'); activeQuestion = activeQuestion || restoredDraft?.activeQuestion || ''; if(!restoredDraft?.touched?.length)restoredDraft=null; } catch (_) { /* A corrupt draft cannot change server state. */ }
  const factorNames = {leadership:'领导意愿',results:'结果导向',learning:'学习敏锐度'};
  const uuid = () => { if (crypto.randomUUID) return crypto.randomUUID(); const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128; const hex = [...bytes].map(value => value.toString(16).padStart(2,'0')).join(''); return hex.slice(0,8) + '-' + hex.slice(8,12) + '-' + hex.slice(12,16) + '-' + hex.slice(16,20) + '-' + hex.slice(20); };
  const endpoint = suffix => '/v1/interviews/' + encodeURIComponent(sessionId) + suffix;
  function updateNavigation(verified = false) {
    document.querySelectorAll('[data-research-link]').forEach(link => {link.hidden = !verified; if(verified) link.href='/research#'+new URLSearchParams({session:sessionId,token});});
    document.querySelectorAll('[data-workbench-link]').forEach(link => { link.href = '/workbench' + (sessionId ? '#session=' + encodeURIComponent(sessionId) : ''); });
    document.querySelectorAll('[data-report-link]').forEach(link => { link.hidden = !verified; if (verified) link.href = '/interview-report#session=' + encodeURIComponent(sessionId) + '&token=' + encodeURIComponent(token); });
    if (verified) { try { const saved = JSON.parse(sessionStorage.getItem('suit-workbench-hr-links-v1') || '{}'); saved[sessionId] = {token}; sessionStorage.setItem('suit-workbench-hr-links-v1',JSON.stringify(saved)); } catch (_) { /* Keep the verified link usable on this page. */ } }
  }
  const element = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = window.SuitTextDisplay?.simplify(String(text))??text; return node; };
  function notice(message, kind = '') { $('notice').textContent = message; $('notice').className = 'notice' + (kind ? ' ' + kind : ''); $('notice').hidden = !message; }
  function time(value) { if (!value) return '时间未记录'; const date = new Date(value); return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN',{hour12:false}); }
  function stateName(value) { return ({answering:'候选人作答中',interview_finished:'面试已完成',completed:'面试已完成',complete:'面试已完成',queued:'等待转写',running:'正在转写',processing:'正在转写',failed:'转写失败',complete_with_errors:'需人工核对',done:'转写完成',succeeded:'转写完成',not_requested:'文字回答'})[value] || value || '待复核'; }
  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {}); headers.set('Authorization','Bearer ' + token);
    if (options.body) headers.set('Content-Type','application/json');
    let response;
    try { response = await fetch(url,{cache:'no-store',...options,headers}); }
    catch (_) { throw new Error('无法连接本地服务。当前填写内容仍在页面中，请检查服务后重试。'); }
    if (options.media && response.ok) return response.blob();
    let payload;
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) {
      const error = new Error(typeof payload.detail === 'string' ? payload.detail : payload.detail?.message || (response.status === 401 || response.status === 403 ? '访问凭证无效或无 HR 权限，请使用创建时生成的 HR 专用链接。' : '请求失败（' + response.status + '），请重试。'));
      error.status = response.status; error.code = payload.detail?.code; throw error;
    }
    return payload;
  }
  function answerText(answer) { return answer.transcript || ''; }
  function contentAnalysisMessage(data) {
    if(!(data.answers||[]).some(a=>a.current!==false))return '当前场次还没有提交回答。若已完成面试，请返回工作台选择有回答的那场；刷新不会从其他场次搬入结果。';
    const latest = new Map();
    for (const job of (data.jobs || []).filter(item => item.job_type === 'content').sort((a,b) => String(a.created_at || '').localeCompare(String(b.created_at || '')))) latest.set(job.question_id || job.answer_id || job.job_id,job);
    const statuses = latest.size ? [...latest.values()].map(job => job.status).filter(status => status !== 'superseded') : (data.answers || []).filter(answer => answer.current !== false).map(answer => answer.content_status);
    const pending = statuses.some(status => ['queued','running','processing'].includes(status));
    const blocked = statuses.includes('blocked'), failed = statuses.includes('failed');
    if (pending) return (blocked || failed ? '部分内容分析正在等待或处理中，另有回答分析受阻或失败。' : '自动内容分析正在等待或处理中。') + '评分草稿就绪后仍需 HR 复核。';
    if (blocked) return '回答已保存。对应题目会说明分析受阻原因，可在该题重新分析或先人工复核。';
    if (failed) return '回答已保存。可在失败题目旁重新分析；已有结果与复核草稿仍可使用。';
    if(data.questions?.length&&data.questions.every(q=>q.score_source==='hr'&&!q.review_stale))return '本场各题已由老师复核；原模型草稿、原话和历次修改仍可查看。';
    if (data.automatic_scoring?.status === 'draft_results_available') return '已有模型评分草稿，需 HR 对照原文复核。题库锚点尚待业务校准。';
    if (data.automatic_scoring?.status === 'not_configured') return '自动内容模型未配置；当前由 HR 核验回答证据，不生成模拟模型分数。';
    return '回答已保存，当前没有正在处理的内容分析任务；可返回工作台生成评分草稿，或直接人工复核。';
  }
  function answerLabel(answer, index) { return (answer.parent_question_id ? '追问回答' : '主问题回答') + (index == null ? '' : ' ' + (index + 1)) + ' · 转写 v' + answer.transcript_revision; }
  function draftValues() {
    const values = {};
    fields.forEach((field, id) => { if(touchedQuestions.has(id))values[id] = {score:field.root.querySelector('input[type="radio"]:checked')?.value || '',reason:field.reason.value,evidence:field.evidenceRows.map(row => ({answer_id:row.answer.value,quote:row.quote.value,transcript_revision:row.revision}))}; });
    return {values,reason:$('reviewReason').value,activeQuestion,source_version:report?.version,touched:[...touchedQuestions]};
  }
  function persistDraft() {
    if (!report) return;
    try { sessionStorage.setItem(draftKey,JSON.stringify(draftValues())); $('saveStatus').textContent = '复核草稿已保存在本标签页，正式评分需点击保存'; }
    catch (_) { $('saveStatus').textContent = '本机草稿存储不可用，请保存本题后再离开'; }
  }
  function changed(id = activeQuestion) { dirty = true; if(id)touchedQuestions.add(id); persistDraft(); $('savedReviewActions').hidden = true; updateQuestionNav(); }
  function setTab(name) {
    activeTab = name;
    for (const key of ['review','overview','research','history']) {
      const title=key[0].toUpperCase()+key.slice(1), active=key===name;
      $('panel'+title).hidden=!active; $('tab'+title).setAttribute('aria-selected',String(active)); $('tab'+title).tabIndex=active?0:-1;
    }
    if (name!=='review') timelineViews.forEach(view=>view.pause?.());
    if(name==='overview')loadPotentialProfile();
  }
  function activateQuestion(id, load = true) {
    if (!fields.has(id)) return;
    const old=activeQuestion; activeQuestion=id;
    fields.forEach((field,key)=>{field.root.hidden=key!==id;});
    if(old!==id)timelineViews.forEach(view=>view.pause?.());
    updateQuestionNav();
    const field=fields.get(id), answer=field.answers[0];
    if(load&&answer){const v=timelineViews.get(answer.answer_id+':'+answer.transcript_revision);v?.load().catch(error=>notice(error.message,'warning'));}
  }
  function reviewComplete(field) { return field.question.score_source==='hr'&&!field.question.review_stale&&!touchedQuestions.has(field.question.question_id); }
  function nextReviewQuestion(fromId) {
    const list=[...fields.values()],index=list.findIndex(field=>field.question.question_id===fromId);
    return [...list.slice(index+1),...list.slice(0,index)].find(field=>field.answers.length&&!reviewComplete(field));
  }
  function updateReviewNextSteps() {
    const allReviewed=fields.size>0&&[...fields.values()].every(reviewComplete);
    fields.forEach((field,id)=>{
      const holder=field.nextStep;if(!holder)return;
      holder.replaceChildren();holder.hidden=!reviewComplete(field);
      if(holder.hidden)return;
      const next=nextReviewQuestion(id),copy=element('p','question-next-copy',next?'本题已保存。继续核对下一题，已填写的草稿会保留。':allReviewed?'本场各题复核均已保存，可查看整场报告。':'本题已保存，其余题目尚待回答。可先查看已有报告。'),actions=element('div','actions');
      if(next){const button=element('button','primary','下一道待复核');button.type='button';button.addEventListener('click',()=>{setTab('review');activateQuestion(next.question.question_id);persistDraft();next.heading.focus({preventScroll:true});next.root.scrollIntoView({behavior:'smooth',block:'start'});});actions.append(button);}
      const reportLink=element('a','button '+(next?'subtle':'primary'),next?'查看本场报告':'查看报告');reportLink.href='/interview-report#'+new URLSearchParams({session:sessionId,token});actions.append(reportLink);holder.append(copy,actions);
    });
  }
  function updateQuestionNav() {
    const nav=$('questionNav'); nav.replaceChildren();
    fields.forEach((field,id)=>{
      const b=element('button','question-nav-button'),copy=element('span');b.type='button';b.setAttribute('aria-current',String(id===activeQuestion));
      const q=field.question, reviewed=reviewComplete(field);
      copy.append(element('strong','',factorNames[q.factor_id]||'主问题 '+(field.index+1)),element('small','',touchedQuestions.has(id)?'已修改，待保存':reviewed?'已复核':q.review_stale?'回答更新，待核对':field.reason?.value?'已填写，待保存':field.answers.length?'待复核':'等待回答'));
      b.append(copy,element('span','question-nav-number',String(field.index+1).padStart(2,'0')));b.addEventListener('click',()=>{activateQuestion(id);persistDraft();});nav.append(b);
    });
    updateReviewNextSteps();
  }
  function revealAnswer(answerId) {
    const field=[...fields.values()].find(f=>f.answers.some(a=>a.answer_id===answerId));
    if(field){setTab('review');activateQuestion(field.question.question_id,false);field.selectAnswer?.(answerId);}else setTab('history');
  }
  function analysisFor(answer) {
    if(window.SuitReportReader)return window.SuitReportReader.selectAnalysis(report,answer);
    const automatic=report?.standard_analysis;
    const runs=[...(automatic?.runs||automatic?.selected_runs||[]),...(report?.research_appendix?.selected_runs||[])];
    for(const run of runs){
      const item=(run.result?.multimodal||run.multimodal||[]).find(mm=>mm.answer_id===answer.answer_id&&mm.transcript_revision===answer.transcript_revision);
      if(item)return {item,run,automatic:(automatic?.runs||automatic?.selected_runs||[]).includes(run)};
    }
    return null;
  }
  function progress() {
    const answers=(report.answers||[]).filter(a=>a.current!==false), holder=$('analysisProgress');holder.replaceChildren();
    const processing=answers.some(a=>['queued','running','processing'].includes(a.content_status));
    const runs=report.standard_analysis?.runs||report.standard_analysis?.selected_runs||[];
    const mmCount=answers.filter(a=>analysisFor(a)?.item).length, media=answers.filter(a=>a.has_media), failures=runs.filter(r=>['failed','partial'].includes(r.status));
    const reviewed=(report.questions||[]).filter(q=>q.score_source==='hr'&&!q.review_stale).length;
    const contentReady=(report.questions||[]).filter(q=>q.model_evaluation).length;
    const contentFailed=answers.some(a=>['failed','blocked'].includes(a.content_status));
    const paused=report.standard_analysis?.paused;
    const labels=[['已保存 '+answers.length+' 条回答',answers.length?'complete':'pending'],[processing?'内容分析中':contentFailed?'部分内容分析需处理':contentReady?'内容草稿可核对':'内容等待核对',!processing&&!contentFailed&&contentReady?'complete':'pending'],[paused?'迁移后分析已暂停':!media.length?'无音视频素材':mmCount?('多模态 '+mmCount+'/'+media.length+' 段可查看'+(failures.length?' · 有未完成模块':'')):runs.some(r=>['queued','running'].includes(r.status))?'多模态分析中':'多模态尚无结果',!paused&&media.length&&mmCount===media.length&&!failures.length?'complete':'pending'],['老师复核 '+reviewed+'/'+(report.questions||[]).length,reviewed===(report.questions||[]).length?'complete':'pending']];
    for(const [text,state]of labels){const node=element('div','progress-item '+state);node.append(element('span','progress-dot'),element('span','',text));holder.append(node);}
    if(paused){const b=element('button','primary','启用本场本地分析');b.type='button';b.disabled=busy;b.title='恢复副本保留历史结果；点击后在当前环境处理本场待分析音视频。';b.addEventListener('click',async()=>{b.disabled=true;try{await api(endpoint('/standard-analysis/resume'),{method:'POST',body:JSON.stringify({version:report.version,idempotency_key:uuid()})});render(await api(endpoint('/report')),dirty);notice('本场本地分析已启用，等待分析进程处理。');}catch(e){notice(e.message,'warning');}finally{b.disabled=false;}});holder.append(b);}
    if(failures.length){const b=element('button','danger','重试 '+failures.length+' 项未完成分析');b.type='button';b.disabled=busy;b.addEventListener('click',async()=>{b.disabled=true;try{for(const run of failures){await api(endpoint('/standard-analysis/retry'),{method:'POST',body:JSON.stringify({run_id:run.run_id,version:report.version,idempotency_key:uuid()})});const next=await api(endpoint('/report'));render(next,dirty);}notice('已重试未完成的本地分析，原片和已完成结果保留。');}catch(e){notice(e.message,'warning');}finally{b.disabled=false;}});holder.append(b);}
  }
  function timelineFor(answerId, revision, host) {
    if (!Number.isInteger(revision) || revision < 1) throw new Error('引用缺少明确的转写版本，不能用当前版本替代。');
    const key = answerId + ':' + revision;
    if (timelineViews.has(key)) return timelineViews.get(key);
    const answer = [...(report.answers || []),...(report.questions || []).flatMap(q => q.answers || [])].find(item => item.answer_id === answerId) || {answer_id:answerId};
    const view = window.SuitEvidenceTimeline.mount({answer,sessionId,revision,
      fetchJSON:version => api(endpoint('/answers/' + encodeURIComponent(answerId) + '/timeline?transcript_revision=' + version)),
      fetchMedia:() => api(endpoint('/media/' + encodeURIComponent(answerId)),{media:true}),
      onOpenRevision:version => {
        try { const historic = timelineFor(answerId,version,host); historic.element.parentElement.open = true; historic.load().catch(error => notice(error.message,'warning')); historic.element.scrollIntoView({behavior:'smooth',block:'nearest'}); }
        catch (error) { notice(error.message,'warning'); }
      }
    });
    timelineViews.set(key,view);
    const detail = element('details','evidence-history-view'); detail.open = true;
    detail.append(element('summary','','指定版本原话 · v' + revision + ' · 只读回放'),view.element);
    (host || $('reviewHistory')).append(detail); return view;
  }
  function referenceLink(getEvidence, host) {
    const holder = element('div'), locate = element('button','evidence-reference-link','定位原话与片段'), status = element('span','evidence-reference-error'); locate.type = 'button'; status.setAttribute('role','status');
    locate.addEventListener('click',async () => {
      status.textContent = '';
      try {
        const evidence = getEvidence();
        if (!evidence?.answer_id || !evidence.quote) throw new Error('请先选择来源回答并填写原文引用。');
        revealAnswer(evidence.answer_id);
        const view = timelineFor(evidence.answer_id,evidence.transcript_revision,host);
        view.element.parentElement.open = true; await view.openQuote(evidence);
      } catch (error) { status.textContent = error.message; }
    });
    holder.append(locate,status); return holder;
  }
  function addEvidence(field, existing = {}) {
    const row = element('div','quote-item');
    const number = field.evidenceRows.length + 1;
    const answerId = 'evidence-answer-' + field.index + '-' + uuid();
    const quoteId = 'evidence-quote-' + field.index + '-' + uuid();
    const answerLabelNode = element('label','', '引用 ' + number + ' · 来源回答'); answerLabelNode.htmlFor = answerId;
    const select = element('select'); select.id = answerId;
    for (const [i, answer] of field.answers.entries()) { const option = element('option','',answerLabel(answer,i)); option.value = answer.answer_id; select.append(option); }
    if (!field.answers.length) { const option = element('option','','尚无可用回答'); option.value = ''; select.append(option); select.disabled = true; }
    if (existing.answer_id) {
      if (!field.answers.some(answer => answer.answer_id === existing.answer_id)) { const historical = element('option','','原引用回答已被替换，请重新选择当前回答'); historical.value = existing.answer_id; historical.disabled = true; select.append(historical); }
      select.value = existing.answer_id;
    }
    const quoteLabel = element('label','','原文引用'); quoteLabel.htmlFor = quoteId;
    const quote = element('textarea','quote-field'); quote.id = quoteId; quote.rows = 3; quote.maxLength = 5000; quote.placeholder = '选择左侧文字后点击“引用选中文字”，或粘贴与当前转写一致的原文。'; quote.value = existing.quote || '';
    const remove = element('button','subtle','移除此引用'); remove.type = 'button';
    const entry = {root:row,answer:select,quote,revision:existing.transcript_revision ?? field.answers.find(answer => answer.answer_id === select.value)?.transcript_revision};
    const versionLabel = element('p','small muted evidence-quote-version'), confirmVersion = element('button','subtle confirm-evidence-version','已核对：采用当前转写版本'); confirmVersion.type = 'button';
    entry.updateVersion = () => {
      const current = field.answers.find(answer => answer.answer_id === select.value);
      const stale = !current || entry.revision !== current.transcript_revision;
      versionLabel.textContent = '引用依据：转写 v' + (entry.revision || '未记录') + (stale ? '；当前回答已变化。请核对原话后重新编辑，或确认采用当前版本。' : ' · 当前版本');
      confirmVersion.hidden = !current || !stale;
    };
    const adoptCurrent = () => { entry.revision = field.answers.find(answer => answer.answer_id === select.value)?.transcript_revision; entry.updateVersion(); };
    quote.addEventListener('input',adoptCurrent); select.addEventListener('change',adoptCurrent);
    confirmVersion.addEventListener('click',() => { adoptCurrent(); changed(field.question.question_id); }); entry.updateVersion();
    remove.addEventListener('click',() => { row.remove(); field.evidenceRows = field.evidenceRows.filter(item => item !== entry); changed(field.question.question_id); });
    row.append(answerLabelNode,select,quoteLabel,quote,versionLabel,referenceLink(() => ({answer_id:select.value,quote:quote.value.trim(),transcript_revision:entry.revision}),field.evidenceColumn),confirmVersion,remove); field.evidenceContainer.append(row); field.evidenceRows.push(entry); return entry;
  }
  function selectEvidence(field, answer, transcriptNode) {
    const selection = window.getSelection();
    const selected = selection?.toString().trim() || '';
    const resolved=selected?(window.SuitTextDisplay?.resolveOriginalQuote?window.SuitTextDisplay.resolveOriginalQuote(answerText(answer),selected):answerText(answer).includes(selected)?{quote:selected}:null):null;
    if (!resolved || !transcriptNode.contains(selection.anchorNode) || !transcriptNode.contains(selection.focusNode)) { notice('请在该条回答中选择能唯一对应原文的一段文字；重复短语请多选前后文。','warning'); return; }
    const target = field.evidenceRows.find(row => !row.quote.value.trim()) || addEvidence(field);
    target.answer.value = answer.answer_id; target.quote.value = resolved.quote; target.revision = answer.transcript_revision; target.updateVersion(); changed();
    notice('原文已加入本题评分证据。请结合锚点填写分数和理由。');
  }
  function renderAnswer(answer, field) {
    const block = element('article','answer-evidence');
    const meta = element('div','answer-meta'); meta.append(element('h3','',answer.parent_question_id ? '补充追问回答' : '主问题回答'),element('span','pill quiet',stateName(answer.job_status)));
    const sourceName = ({asr:'自动转写',candidate:'候选人提供',candidate_input:'候选人输入',human:'人工校正'})[answer.transcript_source] || answer.transcript_source;
    const source = element('p','small muted','当前转写 v' + answer.transcript_revision + (sourceName ? ' · ' + sourceName : '')+' · 简体阅读，引用保留原始文字');
    const transcript = element('div','answer-text',answerText(answer) || '暂无可用转写。可回放原始材料，并等待转写或候选人校正。');
    const actions = element('div','evidence-actions'), quoteButton = element('button','','引用选中文字'); quoteButton.type = 'button'; quoteButton.disabled = !answerText(answer);
    quoteButton.addEventListener('mousedown',event => event.preventDefault()); quoteButton.addEventListener('click',() => selectEvidence(field,answer,transcript)); actions.append(quoteButton);
    const fullText=element('details','full-transcript');fullText.append(element('summary','','完整原话与选中引用'),source,transcript,actions);
    block.append(meta);
    const timeline = timelineFor(answer.answer_id,answer.transcript_revision,block);
    const analysis=analysisFor(answer);
    if(analysis && window.SuitMultimodalView){
      const mm=window.SuitMultimodalView.mount({data:analysis.item,timeline,run:analysis.run,sourceLabel:analysis.automatic?'本场自动分析':'已选研究观察'});
      multimodalViews.add(mm);
      if(window.SuitReportReader)block.append(window.SuitReportReader.observations({analysis,answer,getTimeline:()=>timeline.load(),onPreview:async(event,host)=>{timeline.pause();host.append(timeline.element);mm.setChannel(event.kind==='apparent_expression'?'geometry':event.kind==='gaze_direction'?'gaze':'audio',event.kind==='apparent_expression'?undefined:event.kind==='audio_pitch'?'f0_hz':'rms_dbfs');await timeline.seek(event.kind==='apparent_expression'?(event.geometry?.anchor_ms??event.anchor_ms??event.start_ms):(event.anchor_ms??event.start_ms),{play:false});timeline.element.scrollIntoView({behavior:'smooth',block:'nearest'});},onChannel:(key,metric)=>{mm.setChannel(key,metric);for(let n=mm.element.parentElement;n;n=n.parentElement)if(n.tagName==='DETAILS')n.open=true;},onSeek:async(ms,options)=>{for(let n=timeline.element.parentElement;n;n=n.parentElement)if(n.tagName==='DETAILS')n.open=true;await timeline.seek(ms,options);timeline.element.scrollIntoView({behavior:'smooth',block:'start'});}}));
      const details=element('details','reader-secondary');details.append(element('summary','','模型曲线、采集点与原始数值'),mm.element);block.append(details);
    } else {
      block.append(element('p','review-status-note',!answer.has_media?'此条为文字回答，可核对内容证据。':'本条尚无对应版本的多模态结果。新场次按分析方案自动处理；已有场次可从“研究对照”显式启动。'));
    }
    block.append(fullText);
    if (answer.has_media && window.SuitVisualEvidence && !analysis) {
      const visualPath = endpoint('/visual/' + encodeURIComponent(answer.answer_id));
      const visual = window.SuitVisualEvidence.mount({answer,
        fetchJSON:() => api(visualPath),
        fetchFrame:index => api(visualPath + '/frames/' + encodeURIComponent(index),{media:true}),
        onSeek:milliseconds => timeline.seek(milliseconds)
      });
      visualViews.add(visual);const quality=element('details');quality.append(element('summary','','采集画面与质量检查'),visual.element);block.append(quality);
    }
    if (answer.analysis?.transcription) {
      const asr = answer.analysis.transcription, detail = element('details'), summary = element('summary','','原始 ASR 输出（不代表当前修订的时间对齐）'); detail.append(summary);
      detail.append(element('p','small muted',asr.model ? asr.model + ' · ' + (asr.status === 'ok' ? '自动转写，需人工核对' : asr.status) : '状态：' + asr.status));
      detail.append(element('div','answer-text',asr.text || asr.reason || '未生成有效自动转写。'));
      detail.append(element('p','small muted','原始 ASR 时间仅关联其生成时的版本。请载入上方时间线核对版本；人工修订不会自动继承旧时间。'));
      block.append(detail);
    }
    if (answer.revisions?.length) {
      const details = element('details'), summary = element('summary','','查看原始文字与修订记录（' + answer.revisions.length + '）'); details.append(summary);
      for (const revision of answer.revisions) {
        const revisionBlock = element('div','history-entry');
        revisionBlock.append(element('strong','','v' + revision.version + ' · ' + time(revision.created_at) + ' · ' + (revision.actor || '来源未记录')));
        revisionBlock.append(element('p','',revision.reason || '原始内容'));
        revisionBlock.append(element('div','answer-text',revision.text || '（该版本没有文字）'));
        details.append(revisionBlock);
      }
      block.append(details);
    }
    return block;
  }

  async function retryContent(question,job){
    if(contentRetrying.has(question.question_id))return;
    const key=question.question_id+':'+job.job_id;
    if(!contentRetryOperations.has(key))contentRetryOperations.set(key,{question_id:question.question_id,version:report.version,idempotency_key:uuid()});
    contentRetrying.add(question.question_id);
    try{await api(endpoint('/content-analysis/retry'),{method:'POST',body:JSON.stringify(contentRetryOperations.get(key))});contentRetryOperations.delete(key);await refresh(true);notice('本题文字分析已重新排队，未保存的复核草稿保留。');}
    catch(error){if(error.status===409)contentRetryOperations.delete(key);throw error;}
    finally{contentRetrying.delete(question.question_id);}
  }

  function renderQuestion(question, index, saved) {
    const card = element('section','card review-question'), header = element('div','review-question-header');
    header.append(element('div','eyebrow','QUESTION ' + String(index+1).padStart(2,'0') + ' / ' + (factorNames[question.factor_id] || question.factor_id || '答案证据')));
    const heading=element('h2','',question.text);heading.tabIndex=-1;header.append(heading);
    const scoreSource = question.score_source === 'model_draft' ? '模型草稿' : question.score_source === 'hr' ? 'HR 复核' : '当前评分';
    header.append(element('p','small muted','题目来源 ' + (question.source_cell || question.question_id) + ' · ' + scoreSource + ' ' + (question.score == null ? 'N/A' : question.score + ' / 5') + (question.review_stale ? ' · 回答已更新，原评分需重新复核' : '')));
    const body = element('div','review-question-body'), evidenceColumn = element('div','evidence-column'), ratingColumn = element('div','rating-column');
    const field = {root:card,heading,index,question,evidenceColumn,answers:(question.answers || []).filter(answer => answer.current !== false),evidenceRows:[]}; fields.set(question.question_id,field);
    const recordedDecisions=(report.decision_events||report.decisions||[]).filter(event=>{const round=(report.rounds||[]).find(item=>item.round_id===event.round_id);return (event.parent_question_id||round?.parent_question_id||round?.question_id||event.question_id)===question.question_id;});
    const decisionBlock=element('section','review-question-decisions');
    const brief=value=>{const chars=Array.from(String(value||'').replace(/\s+/g,' ').trim());return chars.length>120?chars.slice(0,120).join('')+'…':chars.join('');};
    const asked=recordedDecisions.find(event=>['ask_followup','followup'].includes(event.action)),primary=asked||recordedDecisions.find(event=>event.checkpoint_kind!=='followup')||recordedDecisions[0],summary=primary?.decision_summary||{},branch=summary.selected_branch;
    const branchText=typeof branch==='string'?branch:branch?.text||branch?.question||primary?.actual_next_round?.text;
    const compact=asked?(branchText?'实际追问：“'+brief(branchText)+'”':'本题已追加补充追问，文案详见当时记录。'):primary?'本题未追加追问。'+brief(summary.brief_reason||primary.reason_for_hr||primary.reason||primary.stop_reason||'具体依据见当时记录。'):'本题尚无已封存追问决定，不根据当前模型草稿补写当时依据。';
    decisionBlock.append(element('p','review-decision-summary',compact));
    if(recordedDecisions.length&&window.SuitReportReader){const details=element('details','review-decision-detail');details.open=false;details.append(element('summary','','当时的追问决定与推进记录（'+recordedDecisions.length+' 条）'));for(const event of recordedDecisions)details.append(window.SuitReportReader.decision(event,{question,factor:report.potential_profile?.factors?.find(f=>f.question_ids?.includes(event.parent_question_id)),onEvidence:ref=>{revealAnswer(ref.answer_id);return timelineFor(ref.answer_id,ref.transcript_revision,evidenceColumn).openQuote(ref);}}));decisionBlock.append(details);}
    header.append(decisionBlock);
    evidenceColumn.append(element('h3','','原片 · 原话 · 同步观察'));
    const recovery=window.SuitReportReader?.contentRecovery(report,question,{onRetry:retryContent});if(recovery)ratingColumn.append(recovery);
    const previous=window.SuitReportReader?.previousDraft(report,question,{onEvidence:ref=>{revealAnswer(ref.answer_id);return timelineFor(ref.answer_id,ref.transcript_revision,evidenceColumn).openQuote(ref);}});if(previous)ratingColumn.append(previous);
    if (question.model_evaluation) {
      const model = question.model_evaluation, modelCard = element('details','review-model-draft'); modelCard.open = true;
      modelCard.append(element('summary','','模型评分草稿 · ' + (question.ai_score == null ? 'N/A' : question.ai_score + ' / 5') + ' · 待 HR 核验'));
      modelCard.append(element('p','small muted',model.rating?.reason || '请核对下方原始回答、引用与评分锚点。'));
      if (model.evidence_units) modelCard.append(window.SuitResearchSummary.semantic(model,ref=>{revealAnswer(ref.answer_id);return timelineFor(ref.answer_id,ref.transcript_revision,evidenceColumn).openQuote(ref);}));
      for (const evidence of model.rating?.evidence || []) modelCard.append(element('div','answer-text','“' + evidence.quote + '”'),referenceLink(() => evidence,evidenceColumn));
      if (model.followup) {
        const comparison=element('details','review-model-followup');comparison.append(element('summary','','模型追问意见（对照）'),element('p','small muted','这是当前模型草稿的意见；实际采用的分支与依据以上方“当时的追问决定”为准。'),element('p','small',model.followup.reason||model.followup.status||'未记录理由'));
        for(const evidence of model.followup.evidence||[])comparison.append(element('div','answer-text','“'+evidence.quote+'”'),referenceLink(()=>evidence,evidenceColumn));
        modelCard.append(comparison);
      }
      modelCard.append(element('p','small muted','模型输出仅为待核验草稿。请自主选择本题评分并填写复核理由。'));
      ratingColumn.append(modelCard);
    }
    if (!field.answers.length) evidenceColumn.append(element('div','empty-state','候选人尚未提交此题回答。请保持 N/A，并说明待补充的内容。'));
    const answerNodes=new Map(),answerTabs=element('div','answer-selector');
    field.selectAnswer=id=>{answerNodes.forEach((node,key)=>node.hidden=key!==id);for(const b of answerTabs.children)b.setAttribute('aria-selected',String(b.dataset.answer===id));};
    for (const [i,answer] of field.answers.entries()) {const node=renderAnswer(answer,field);answerNodes.set(answer.answer_id,node);node.hidden=i!==0;evidenceColumn.append(node);if(field.answers.length>1){const b=element('button','',answer.parent_question_id?'追问回答':'主问回答');b.type='button';b.dataset.answer=answer.answer_id;b.setAttribute('aria-selected',String(i===0));b.addEventListener('click',()=>{timelineViews.forEach(v=>v.pause?.());field.selectAnswer(answer.answer_id);timelineViews.get(answer.answer_id+':'+answer.transcript_revision)?.load().catch(e=>notice(e.message,'warning'));});answerTabs.append(b);}}
    if(field.answers.length>1)evidenceColumn.prepend(answerTabs);
    const anchorDetails=element('details');anchorDetails.append(element('summary','','本题评分锚点（草案）'),element('p','small muted','核对具体行动与结果，1–5 分均须有内容依据。'));
    const anchors = element('ol','anchor-list');
    for (const [score, anchor] of Object.entries(question.anchors || {})) { const li = element('li'); li.append(element('span','anchor-score',score),element('span','',typeof anchor === 'string' ? anchor : anchor.text || anchor.description || '')); anchors.append(li); }
    anchorDetails.append(anchors);ratingColumn.append(anchorDetails,element('h3','','本题复核'));
    const decisions=element('div','review-decision-actions');
    const adopt=element('button','','采用模型草稿'),defer=element('button','','暂缓判断');adopt.type=defer.type='button';adopt.disabled=!question.model_evaluation;
    adopt.addEventListener('click',()=>{const rating=question.model_evaluation?.rating;if(!rating)return;const score=rating.score??question.ai_score;for(const input of field.root.querySelectorAll('input[type="radio"]'))input.checked=input.value===(score==null?'':String(score));field.reason.value=rating.reason||'';field.evidenceContainer.replaceChildren();field.evidenceRows=[];for(const ref of rating.evidence||[])addEvidence(field,ref);if(!field.evidenceRows.length)addEvidence(field);changed();notice('已将模型草稿放入本题复核。请核对原话与锚点，再点击“保存本题复核”。');});
    defer.addEventListener('click',()=>{for(const input of field.root.querySelectorAll('input[type="radio"]'))input.checked=input.value==='';field.reason.value='暂缓判断：';field.reason.focus();changed();});decisions.append(adopt,defer);ratingColumn.append(decisions);
    const options = element('div','score-options');
    const scoreValue = saved ? saved.score : question.score == null || question.score_source === 'model_draft' ? '' : String(question.score);
    for (const value of ['',1,2,3,4,5]) { const label = element('label','score-option'), input = element('input'), span = element('span','',value === '' ? 'N/A' : String(value)); input.type = 'radio'; input.name = 'score-' + index; input.value = String(value); input.checked = String(value) === scoreValue; label.append(input,span); options.append(label); }
    ratingColumn.append(options);
    field.evidenceContainer = element('div','evidence-inputs'); ratingColumn.append(field.evidenceContainer);
    const evidence = saved ? saved.evidence : question.evidence || [];
    if (evidence.length) evidence.forEach(item => addEvidence(field,item)); else addEvidence(field);
    const add = element('button','','＋ 添加一条证据'); add.type = 'button'; add.addEventListener('click',() => { addEvidence(field); changed(); }); ratingColumn.append(add);
    const reasonId = 'rating-reason-' + index, reasonLabel = element('label','','评分 / N/A 理由'); reasonLabel.htmlFor = reasonId;
    field.reason = element('textarea','rating-reason'); field.reason.id = reasonId; field.reason.rows = 4; field.reason.maxLength = 2000; field.reason.required = true; field.reason.placeholder = '对应评分锚点，说明原文支持什么、还缺什么。N/A 也需要说明原因。'; field.reason.value = saved?.reason ?? (question.score_source !== 'model_draft' && (question.evidence?.length || question.score != null) ? question.reason || '' : '');
    const saveRow=element('div','question-save-row'),save=element('button','primary','保存本题复核');save.type='button';save.disabled=!field.answers.length;save.addEventListener('click',()=>saveQuestion(question.question_id));
    saveRow.append(save,element('span','question-save-state',question.score_source==='hr'&&!question.review_stale?'本题已有老师复核结果，可修改后重新保存。':'每题独立保存，其他题可以稍后完成。'));
    field.nextStep=element('div','question-review-next');field.nextStep.hidden=true;field.nextStep.setAttribute('aria-live','polite');
    ratingColumn.append(reasonLabel,field.reason,saveRow,field.nextStep); body.append(evidenceColumn,ratingColumn); card.append(header,body); return card;
  }
  function renderHistory() {
    const holder = $('reviewHistory'); holder.replaceChildren();
    const entries = [];
    for (const review of report.reviews || []) entries.push({at:review.created_at,title:'HR 复核 · 基于报告 v' + review.parent_version,text:review.reason,evidence:(review.ratings || []).flatMap(rating => rating.evidence || [])});
    for (const assessment of report.assessments || []) entries.push({at:assessment.created_at,title:'追问内容判定 · ' + ({gap:'存在证据缺口',sufficient:'证据充分',no_legal_branch:'无合法追问分支'})[assessment.status],text:assessment.reason,evidence:assessment.evidence || []});
    for (const decision of report.decision_events || report.decisions || []) {
      const action = ({continue_main:'继续下一道主问题',ask_followup:'追加补充追问',followup:'追加补充追问',stop:'结束提问',finish:'结束提问'})[decision.action] || '已完成判定';
      const question = (report.questions || []).find(item => item.question_id === decision.parent_question_id);
      const decisionEvidence = decision.used_evidence || decision.evidence || [];
      const evidence = decisionEvidence.map(item => typeof item === 'string' ? item : item.quote || '').filter(Boolean);
      const text = [question ? '依据主问题：' + question.text : '',decision.reason_for_hr || decision.reason || decision.stop_reason || '',decision.branch_id ? '追问分支：' + decision.branch_id : '',...evidence.map(quote => '引用：' + quote),'截止后的分析结果仅用于复核，不改写当时的追问决策。'].filter(Boolean).join('\n');
      entries.push({at:decision.evidence_cutoff_time || decision.created_at,title:'追问决策已封存 · ' + action,text,evidence:decisionEvidence.filter(item => typeof item === 'object')});
    }
    entries.sort((a,b) => String(b.at || '').localeCompare(String(a.at || '')));
    if (!entries.length) holder.append(element('div','empty-state','暂无复核记录。首次保存后将在这里保留版本与理由。'));
    for (const entry of entries) {
      const item = element('div','history-entry'); item.append(element('strong','',entry.title),element('div','small muted',time(entry.at)),element('p','',entry.text));
      for (const evidence of entry.evidence || []) item.append(element('p','small','引用 v' + evidence.transcript_revision + '：“' + evidence.quote + '”'),referenceLink(() => evidence,item));
      holder.append(item);
    }
  }
  function updateAssessmentVersion(adopt = false) {
    const answer = (report?.answers || []).find(item => item.answer_id === $('assessmentAnswer').value && item.current !== false);
    if (adopt || !$('assessmentQuote').value.trim()) assessmentSource = answer ? {answer_id:answer.answer_id,transcript_revision:answer.transcript_revision} : null;
    const stale = answer && (assessmentSource?.answer_id !== answer.answer_id || assessmentSource?.transcript_revision !== answer.transcript_revision);
    $('assessmentVersion').textContent = '判定引用依据：转写 v' + (assessmentSource?.transcript_revision || '未记录') + (stale ? '；所选回答已更新，请核对后采用当前版本。' : ' · 当前版本');
    $('confirmAssessmentVersion').hidden = !stale;
  }
  function loadPotentialProfile() {
    if(!report||activeTab!=='overview'||!$('profileDetails').open||profileVersionLoaded===report.version)return;
    const version=report.version;profileVersionLoaded=version;$('potentialProfile').textContent='正在读取本场证据轮廓…';
    (report.potential_profile?Promise.resolve(report.potential_profile):api(endpoint('/factor-profile?report_version='+version))).then(profile=>{if(!disposed&&report.version===version)$('potentialProfile').replaceChildren(window.SuitPotentialProfile.mount({profile,onEvidence:ref=>{revealAnswer(ref.answer_id);return timelineFor(ref.answer_id,ref.transcript_revision,$('potentialProfile')).openQuote(ref);}}).element);}).catch(error=>{profileVersionLoaded=null;notice(error.message,'warning');});
  }
  function render(nextReport, preserveDraft = false, draftOverride = null) {
    const previous = draftOverride || (preserveDraft ? draftValues() : restoredDraft);
    if(restoredDraft){for(const id of restoredDraft.touched||[])touchedQuestions.add(id);dirty=touchedQuestions.size>0;restoredDraft=null;}
    report = nextReport;
    $('researchAppendix').replaceChildren(window.SuitResearchSummary.mount({appendix:report.research_appendix,onEvidence:ref=>{revealAnswer(ref.answer_id);return timelineFor(ref.answer_id,ref.transcript_revision,$('reviewHistory')).openQuote(ref);},onSeek:(aid,rev,ms)=>{revealAnswer(aid);return timelineFor(aid,rev,$('reviewHistory')).seek(ms);}}).element);
    updateNavigation(true);
    visualViews.forEach(view => view.dispose()); visualViews.clear();
    multimodalViews.forEach(view=>view.dispose());multimodalViews.clear();
    timelineViews.forEach(view => view.dispose()); timelineViews.clear(); fields.clear();
    $('reportView').hidden = false; $('accessCard').hidden = true;
    $('sessionLabel').textContent = (window.SuitTextDisplay?.simplify(report.candidate_ref || '未命名模拟面试') ?? (report.candidate_ref || '未命名模拟面试'))+' · '+sessionId.slice(0,10);
    const savedAnswers=(report.answers||[]).filter(a=>a.current!==false);
    $('sessionContext').textContent='创建于 '+time(report.created_at)+' · 已保存 '+savedAnswers.length+' 条回答，'+savedAnswers.filter(a=>a.has_media).length+' 段音视频。'+(savedAnswers.length?' 选择一道题，核对原话、观察与评分依据。':' 当前还没有回答，请返回工作台继续作答或选择已完成的面试。');
    $('reviewForm').hidden=!savedAnswers.length;$('saveReview').disabled=busy||!savedAnswers.length;
    $('reportVersion').textContent = '报告 v' + report.version;
    $('sessionStatus').textContent = stateName(report.status);
    $('modelStatus').textContent = contentAnalysisMessage(report);
    const planned = report.coverage?.planned_factor_ids?.length || 3;
    $('coverage').textContent = planned + ' / ' + (planned + (report.coverage?.uncovered_factor_ids?.length || 5));
    $('totalScore').textContent = report.total_score == null ? '暂未形成' : Number(report.total_score).toFixed(1);
    const reviewed = (report.questions || []).length > 0 && report.questions.every(question => question.score_source === 'hr' && !question.review_stale);
    $('scoreHeading').textContent = reviewed ? '短面试答案证据总分 · 人工复核' : '短面试答案证据总分 · 待复核';
    $('totalExplanation').textContent = report.total_score == null ? '已评分 ' + (report.scorable_main_count || 0) + '/3 题；不足 3 题不计算总分' + (report.provisional_scored_mean == null ? '' : '。已评分项暂均分 ' + Number(report.provisional_scored_mean).toFixed(1) + '（非总分）') : '三道主问题等权换算为 0–100 分；八维测评仍不完整';
    const questions = $('questions'); questions.replaceChildren();
    for (const [i, question] of (report.questions || []).entries()) questions.append(renderQuestion(question,i,previous?.values[question.question_id]));
    $('reviewReason').value = previous?.reason || '';
    if(!fields.has(activeQuestion))activeQuestion=[...fields.values()].find(f=>f.answers.length&&f.question.score_source!=='hr')?.question.question_id||[...fields.keys()][0]||'';
    if(activeQuestion)activateQuestion(activeQuestion);progress();setTab(activeTab);$('newResults').hidden=true;incomingReport=null;
    const currentRound = report.current_round;
    const currentQuestion = currentRound?.parent_question_id || currentRound?.question_id;
    const answers = (report.answers || []).filter(answer => answer.current !== false && answerText(answer) && (!currentQuestion || (answer.parent_question_id || answer.question_id) === currentQuestion));
    const answerOptions = $('assessmentAnswer'); const oldAnswer = answerOptions.value; answerOptions.replaceChildren();
    for (const answer of answers) { const option = element('option','',(factorNames[(report.questions || []).find(q => q.question_id === (answer.parent_question_id || answer.question_id))?.factor_id] || answer.question_id) + ' · ' + answerLabel(answer)); option.value = answer.answer_id; answerOptions.append(option); }
    if (answers.some(answer => answer.answer_id === oldAnswer)) answerOptions.value = oldAnswer;
    updateAssessmentVersion();
    const decisions = report.decision_events || report.decisions || [];
    const sealed = decisions.some(decision => (decision.round_id && decision.round_id === currentRound?.round_id) || (decision.parent_question_id && decision.parent_question_id === currentQuestion));
    const followupUsed = (report.rounds || []).some(round => round.kind === 'followup');
    $('followupPanel').hidden = !currentRound || currentRound.kind === 'followup' || sealed || followupUsed || ['interview_finished','complete','completed'].includes(report.status);
    $('saveAssessment').disabled = !answers.length || busy;
    renderHistory();
    if (!preserveDraft && !previous) { dirty = false; touchedQuestions.clear(); reviewOperation = null; }
  }
  async function refresh(preserveDraft = true) {
    $('refreshReport').disabled = true;
    try { const next = await api(endpoint('/report')); const kept = preserveDraft && dirty; render(next,kept); notice(kept ? '报告已刷新，保留了尚未保存的评分草稿。请核对最新转写与原文引用，再保存。' : ''); }
    catch (error) { notice(error.message,'error'); if (error.status === 401 || error.status === 403) { $('reportView').hidden = true; $('accessCard').hidden = false; } }
    finally { $('refreshReport').disabled = false; }
  }
  function setBusy(value) { busy = value; $('reviewForm').inert = value; $('assessmentForm').inert = value; $('saveReview').disabled = value||!(report?.answers||[]).some(a=>a.current!==false); $('saveAssessment').disabled = value; $('refreshReport').disabled = value; $('saveReview').textContent = value ? '正在保存…' : '保存证据与评分'; }
  function collectRatings(ids = null) {
    const ratings = [];
    for (const [questionId, field] of fields) {
      if(ids&&!ids.includes(questionId))continue;
      const value = field.root.querySelector('input[type="radio"]:checked')?.value || '';
      const score = value === '' ? null : Number(value), reason = field.reason.value.trim();
      if (!reason) { activateQuestion(questionId,false);field.reason.focus(); throw new Error('请填写本题评分或暂缓判断的理由。'); }
      const evidence = [];
      for (const row of field.evidenceRows) {
        const quote = row.quote.value.trim(); if (!quote) continue;
        const answer = field.answers.find(item => item.answer_id === row.answer.value);
        if (!answer || !answerText(answer).includes(quote)) { row.quote.focus(); throw new Error('引用必须与所选回答的当前转写原文一致。请重新选择或复制原文。'); }
        if (row.revision !== answer.transcript_revision) { row.quote.focus(); throw new Error('这条引用仍基于旧转写版本。请核对原话并点击“采用当前转写版本”，或重新编辑引用后保存。'); }
        evidence.push({answer_id:answer.answer_id,quote,transcript_revision:answer.transcript_revision});
      }
      if (score !== null && !evidence.length) throw new Error('每项有效评分至少需要一条该题回答的原文证据。');
      ratings.push({question_id:questionId,score,reason,evidence});
    }
    return ratings;
  }
  async function saveQuestion(id) {
    if(busy)return;
    let payload;
    try{const ratings=collectRatings([id]);payload={version:report.version,reason:'逐题复核：'+ratings[0].reason,ratings};}catch(error){notice(error.message,'warning');return;}
    await commitReview(payload,[id]);
  }
  async function commitReview(payload, ids) {
    const signature=JSON.stringify(payload);if(reviewOperation?.signature!==signature)reviewOperation={signature,key:uuid()};
    setBusy(true);$('saveStatus').textContent='正在保存到本场报告…';
    try{
      const result=await api(endpoint('/reviews'),{method:'POST',body:JSON.stringify({...payload,idempotency_key:reviewOperation.key})});
      const keep=draftValues();for(const id of ids){delete keep.values[id];touchedQuestions.delete(id);}dirty=touchedQuestions.size>0;
      render(result,true,keep);reviewOperation=null;persistDraft();$('saveStatus').textContent='已保存 '+ids.length+' 题 · '+time(result.generated_at||new Date().toISOString());$('savedReviewActions').hidden=false;notice('本题评分与证据已保存。可以继续下一题，或查看本场报告。');
    }catch(error){$('saveStatus').textContent='尚未确认保存，草稿已保留';notice(error.status===409?'本场数据已有更新。请载入新结果，核对保留的草稿和引用后重试。':error.message,'error');}
    finally{setBusy(false);}
  }
  async function saveReview(event) {
    event.preventDefault(); if (busy) return;
    let payload;
    const ids=[...fields].filter(([id,f])=>f.reason.value.trim()&&(touchedQuestions.has(id)||f.question.score_source!=='hr')).map(([id])=>id);
    try {if(!ids.length)throw new Error('没有待保存的修改。请选择一道题填写复核。');payload = {version:report.version,reason:$('reviewReason').value.trim(),ratings:collectRatings(ids)}; if (!payload.reason) throw new Error('请填写本次复核说明，或在题目右侧直接保存本题。'); }
    catch (error) { notice(error.message,'warning'); return; }
    await commitReview(payload,ids);
  }
  async function saveAssessment(event) {
    event.preventDefault(); if (busy) return;
    const answer = (report.answers || []).find(item => item.answer_id === $('assessmentAnswer').value && item.current !== false);
    const quote = $('assessmentQuote').value.trim(), reason = $('assessmentReason').value.trim(), status = $('assessmentStatus').value;
    if (!answer || !quote || !answerText(answer).includes(quote) || !reason) { notice('请填写判定理由，并引用所选回答当前转写中完全一致的原文。','warning'); return; }
    if (assessmentSource?.answer_id !== answer.answer_id || assessmentSource?.transcript_revision !== answer.transcript_revision) { notice('判定引用仍基于旧回答或转写版本，请核对后采用当前版本。','warning'); return; }
    const branchId = $('assessmentBranch').value;
    if (status === 'gap' && !(answer.parent_question_id || answer.question_id).startsWith(branchId.startsWith('leadership') ? 'leadership_' : 'learning_')) { notice('选择的追问分支与证据所属主问题不一致。领导意愿分支须引用领导意愿回答，学习敏锐度分支须引用学习敏锐度回答。','warning'); return; }
    const payload = {version:report.version,reason,status,evidence:[{answer_id:answer.answer_id,quote,transcript_revision:answer.transcript_revision}]};
    if (status === 'gap') payload.branch_id = branchId;
    const signature = JSON.stringify(payload); if (assessmentOperation?.signature !== signature) assessmentOperation = {signature,key:uuid()};
    setBusy(true);
    try { await api(endpoint('/followup-assessments'),{method:'POST',body:JSON.stringify({...payload,idempotency_key:assessmentOperation.key})}); await refresh(true); assessmentOperation = null; notice('内容判定已保存。系统会在当前主问题的决策截止点核对证据版本，并判断是否需要补充追问。'); }
    catch (error) { notice(error.status === 409 ? '会话版本已更新或追问决策已封存。请刷新报告，核对当前状态后再操作。' : error.message,'error'); }
    finally { setBusy(false); }
  }
  $('profileDetails').addEventListener('toggle',loadPotentialProfile);
  $('reviewForm').addEventListener('submit',saveReview);
  $('reviewForm').addEventListener('input',event => {if(event.target.closest?.('.evidence-timeline')||event.target.closest?.('.multimodal-view'))return;changed();});
  document.querySelectorAll('[data-workspace-tab]').forEach(button=>{button.addEventListener('click',()=>setTab(button.dataset.workspaceTab));button.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;event.preventDefault();const tabs=[...document.querySelectorAll('[data-workspace-tab]')],index=tabs.indexOf(button),next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length;tabs[next].click();tabs[next].focus();});});
  document.querySelectorAll('[data-section-target]').forEach(link => link.addEventListener('click',event => { event.preventDefault(); const target = $(link.dataset.sectionTarget); target?.scrollIntoView({behavior:'smooth',block:'start'}); }));
  $('refreshReport').addEventListener('click',() => refresh(true));
  $('applyNewResults').addEventListener('click',()=>{if(incomingReport){render(incomingReport,dirty);persistDraft();notice('已载入新结果，未保存的复核草稿保留。请核对引用版本。');}else refresh(true);});
  $('assessmentForm').addEventListener('submit',saveAssessment);
  $('assessmentStatus').addEventListener('change',() => $('branchField').hidden = $('assessmentStatus').value !== 'gap');
  $('assessmentQuote').addEventListener('input',() => updateAssessmentVersion(true));
  $('assessmentAnswer').addEventListener('change',() => updateAssessmentVersion(true));
  $('confirmAssessmentVersion').addEventListener('click',() => updateAssessmentVersion(true));
  window.addEventListener('beforeunload',event => { if (dirty || busy) { event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('hashchange',()=>{const next=new URLSearchParams(location.hash.slice(1));if((next.has('session')||next.has('token'))&&(next.get('session')!==sessionId||next.get('token')!==token))location.reload();});
  $('locateAssessmentQuote').addEventListener('click',async () => {
    try {
      const answer = (report?.answers || []).find(item => item.answer_id === $('assessmentAnswer').value && item.current !== false);
      if (!answer) throw new Error('请选择当前有效的来源回答。');
      const source = assessmentSource || {answer_id:answer.answer_id,transcript_revision:answer.transcript_revision};
      const view = timelineFor(source.answer_id,source.transcript_revision);
      view.element.parentElement.open = true;
      await view.openQuote({...source,quote:$('assessmentQuote').value.trim()});
    } catch (error) { notice(error.message,'warning'); }
  });
  window.addEventListener('pagehide',() => {disposed=true;if(pollTimer)clearInterval(pollTimer);visualViews.forEach(view => view.dispose()); visualViews.clear();multimodalViews.forEach(view=>view.dispose());multimodalViews.clear(); timelineViews.forEach(view => view.dispose()); timelineViews.clear(); });
  updateNavigation();
  if (!sessionId || !token) { $('accessCard').hidden = false; notice('HR 链接不完整，请从工作台打开，或使用创建面试时生成的完整 HR 专用链接。','warning'); }
  else { notice('正在读取本场回答与分析…'); refresh(false);
    if(typeof setInterval==='function')pollTimer=setInterval(async()=>{if(disposed||document.hidden||busy||!report)return;try{const state=await api(endpoint('/analysis-status'));if(disposed)return;if(state.version!==report.version){const next=await api(endpoint('/report'));if(disposed)return;if(dirty||[...timelineViews.values()].some(view=>view.isPlaying?.())){incomingReport=next;$('newResults').hidden=false;}else render(next,false);}}catch(_){/* Keep visible evidence and drafts during a temporary disconnect. */}},6000);
  }
})();
