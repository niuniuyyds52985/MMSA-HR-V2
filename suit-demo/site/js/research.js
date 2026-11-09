(() => {
  'use strict';
  const displayText = text => window.SuitTextDisplay?.simplify(text) ?? String(text ?? '');
  const $ = id => document.getElementById(id), p = new URLSearchParams(location.hash.slice(1));
  const sid = p.get('session'), token = p.get('token'), base = '/v1/interviews/' + encodeURIComponent(sid);
  const names = {word_frequency:'原词表词频对照',semantic:'千问语义证据',au:'OpenFace 面部动作',au_net:'AU-Net 原模型（离线时序）',gaze:'OpenFace 视线几何',gaze360:'Gaze360 视线（独立研究）',emotic:'EMOTIC 感知情绪（4类研究）',expression:'基础外显表情',audio:'声音客观指标'};
  const states = {queued:'排队中',running:'运行中',succeeded:'完成',partial:'部分完成',failed:'失败',complete:'完成',no_media:'没有原片',no_video:'没有视频',no_audio:'没有音轨',unavailable:'不可用'};
  const labels = {angry:'愤怒样表情',disgust:'厌恶样表情',fearful:'恐惧样表情',happy:'开心样表情',neutral:'中性表情',sad:'悲伤样表情',surprised:'惊讶样表情'};
  let report, listed = {runs:[],experiments:[]}, annotationInput, currentRun;
  let runRequest = 0, runsRequest = 0, annotationRequest = 0, annotationExperimentId = '', annotationLoading = false;
  const selected = new Set(), timelines = new Map(), operations = new Map(), multimodalViews = new Set();
  const localModules = ['au','gaze','expression','audio'];
  let quickBusy = false;
  const annotationDrafts = new Map(), dirtyAnnotations = new Set(), seenExperiments = new Set();
  const el = (tag, text, cls) => { const n = document.createElement(tag); if (text != null) n.textContent = tag === 'pre' ? String(text) : displayText(text); if (cls) n.className = cls; return n; };
  const button = (text, fn) => { const b = el('button', text); b.type = 'button'; b.addEventListener('click', () => Promise.resolve().then(fn).catch(error => notice(error.message, true))); return b; };
  const stamp = v => v ? new Date(v).toLocaleString('zh-CN',{hour12:false}) : '—';
  const short = id => (id || '').slice(0,10);
  function notice(text, error=false) { $('notice').textContent = text; $('notice').className = 'notice ' + (error ? 'warning' : 'neutral'); $('notice').hidden = !text; }
  async function api(path, options={}) {
    const headers = {Authorization:'Bearer ' + token}; if (options.body) headers['Content-Type'] = 'application/json';
    const r = await fetch(base + path, {...options,headers,cache:'no-store'});
    if (options.blob && r.ok) return r.blob();
    const data = await r.json(); if (!r.ok) throw new Error(data.detail?.message || '请求失败，请核对本场 HR 凭证与输入。'); return data;
  }
  async function post(path, value, operation) {
    if (operation) { const signature = JSON.stringify(value); const old = operations.get(operation); if (!old || old.signature !== signature) operations.set(operation,{signature,key:crypto.randomUUID()}); value = {...value,idempotency_key:operations.get(operation).key}; }
    const result = await api(path,{method:'POST',body:JSON.stringify(value)}); if (operation) operations.delete(operation); return result;
  }
  function submit(form, work) { $(form).addEventListener('submit',async event => { event.preventDefault(); const buttons = [...event.target.querySelectorAll('button[type=submit]')]; buttons.forEach(b => b.disabled = true); try { await work(); } catch(error) { notice(error.message,true); } finally { buttons.forEach(b => b.disabled = form === 'annotationForm' && (annotationLoading || !annotationInput)); } }); }
  function table(headers, rows) { const t = el('table'), head = el('tr'); headers.forEach(h => head.append(el('th',h))); const h = el('thead');h.append(head);t.append(h);const body=el('tbody'); rows.forEach(cells=>{const row=el('tr');cells.forEach(c=>{const d=el('td');d.append(c instanceof Node?c:document.createTextNode(c==null?'—':String(c)));row.append(d);});body.append(row);});t.append(body);const wrap=el('div',null,'table-scroll');wrap.append(t);return wrap; }
  function rawDetails(title, value) { const d = el('details'); d.append(el('summary',title),el('pre',JSON.stringify(value,null,2))); return d; }
  function saveJSON(value, name) { const url = URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json;charset=utf-8'})); const a=el('a');a.href=url;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000); }
  function view(answer, revision, host) {
    const k = answer.answer_id + ':' + revision; if (timelines.has(k)) { const existing=timelines.get(k);host.append(existing.element);return existing; }
    const v = window.SuitEvidenceTimeline.mount({answer,sessionId:sid,revision,showReferences:false,
      fetchJSON:r=>api('/answers/'+answer.answer_id+'/timeline?transcript_revision='+r),fetchMedia:()=>api('/media/'+answer.answer_id,{blob:true}),
      onOpenRevision:r=>view(answer,r,host).load()});timelines.set(k,v);host.append(v.element);return v;
  }
  async function locate(ref, host) { const answer=(report.answers||[]).find(a=>a.answer_id===ref.answer_id)||{answer_id:ref.answer_id};const v=view(answer,ref.transcript_revision,host);await v.openQuote(ref); }
  function quote(ref,host) { const q=el('blockquote');q.append(el('p','“'+ref.quote+'”'),button('定位转写 v'+ref.transcript_revision+' 原话',()=>locate(ref,host)));return q; }
  function disposeViews() { multimodalViews.forEach(v=>v.dispose());multimodalViews.clear();timelines.forEach(v=>v.dispose());timelines.clear(); }
  const currentAnswers = () => (report?.answers || []).filter(a=>a.current!==false);
  const currentMedia = () => currentAnswers().filter(a=>a.has_media);
  function currentLocalRun() {
    const answers=currentMedia();if(!answers.length)return null;
    const automaticIds=new Set((report.standard_analysis?.runs||[]).map(r=>r.run_id));
    return listed.runs.find(r=>!automaticIds.has(r.run_id)&&localModules.every(m=>r.modules.includes(m))&&Object.keys(r.input_versions||{}).length===answers.length&&answers.every(a=>r.input_versions[a.answer_id]===a.transcript_revision));
  }
  function renderQuick() {
    if(!report)return;const answers=currentAnswers(),media=currentMedia(),run=currentLocalRun(),pending=run&&['queued','running'].includes(run.status),ready=run&&['succeeded','partial'].includes(run.status);
    $('quickSessionTitle').textContent='独立重复实验 · '+(report.candidate_ref||'未命名模拟面试')+' · '+short(sid);
    const automatic=$('automaticRuns'),auto=report.standard_analysis;automatic.replaceChildren();
    if(auto?.runs?.length){for(const r of auto.runs){const answer=answers.find(a=>a.answer_id===r.answer_id),row=el('div',null,'research-run');row.append(el('span',(answer?.parent_question_id?'追问':'主问')+' · '+short(r.answer_id)+' · 转写 v'+r.transcript_revision+' · '+(states[r.status]||r.status)));if(['succeeded','partial','failed'].includes(r.status))row.append(button('查看这段自动分析',()=>showRun(r.run_id)));automatic.append(row);}}
    else automatic.append(el('p',auto?.paused?'恢复副本已暂停分析。请在专家工作区启用本场。':media.length?(auto?.enabled?'本场自动分析等待转写完成或分析进程处理。':'这场创建时未启用自动分析，可在下方明确发起本地运行。'):'当前没有已保存的音视频；文字回答仍可进行内容方法对照。','small muted'));
    $('quickSessionSummary').textContent='创建于 '+stamp(report.created_at)+' · 已保存 '+answers.length+' 条回答，其中 '+media.length+' 段音视频 · 报告 v'+report.version;
    $('researchDetails').hidden=!answers.length;
    $('quickAnalyze').hidden=!media.length;$('quickAnalyze').disabled=quickBusy||!media.length||pending;
    $('quickAnalyze').textContent=pending?'独立实验正在分析…':'新增一次本地重复实验';
    $('quickView').hidden=!ready;$('quickView').disabled=!ready||quickBusy;
    $('quickPublish').hidden=!ready;$('quickPublish').disabled=!ready||quickBusy;
    const published=ready&&report.research_appendix?.selected_runs?.some(r=>r.run_id===run.run_id);
    $('quickPublish').textContent=published?'已加入当前报告':'加入本场报告';$('quickPublish').disabled=Boolean(published)||!ready||quickBusy;
    $('quickStatus').textContent=!answers.length?'当前是尚未作答的空场次，没有可生成的分析。若刚完成面试，请点“选择其他面试”打开有回答的那场。':!media.length?'回答已经保存，但本场只有文字，没有可分析的面部或声音。可查看内容证据；自选实验中仍能运行词频对照。':pending?'已保存回答，正在本机分析音视频。本页自动更新状态；无需重复提交或重新录制。':published?'本场音视频分析'+(run.status==='partial'?'部分完成':'已完成')+'，已加入报告 v'+report.version+'。可查看本场分析结果，或从上方“本场报告”查看汇总。':ready?(run.status==='partial'?'本场分析部分完成，失败模块会显示原因；可先查看已有结果。':'本场音视频分析已完成。点“查看本场分析结果”，再决定是否加入报告。'):run?.status==='failed'?'上次本地分析失败，原片仍保留。可在下方查看原因，再点击上方按钮显式重试。':'尚无整场独立实验。上方自动分析照常可用；需要检验重复性时，再新增一次运行。';
  }
  async function quickAnalyze() {
    if(quickBusy)return;quickBusy=true;renderQuick();
    try {
      await loadReport();await refreshRuns();const existing=currentLocalRun();
      if(existing&&['queued','running'].includes(existing.status)){notice('本场已有音视频分析任务，请等待完成。');return;}
      const answers=currentMedia();if(!answers.length)throw new Error('本场没有已保存的音视频，请先选择有音视频回答的面试。');
      const created=await post('/research/runs',{version:report.version,selections:answers.map(a=>({answer_id:a.answer_id,transcript_revision:a.transcript_revision})),modules:localModules,repeats:1,purpose:'本场音视频基础分析，供本人及指导老师复核',condition:'本场原始录制；采集条件未另行标注',participant_group:'session-'+short(sid),cloud_consent:false},'quick-analyze');
      notice('已开始本场 '+answers.length+' 段音视频的本地分析；完成后点击查看结果。');await refreshRuns();
    } finally {quickBusy=false;renderQuick();}
  }
  async function loadReport() {
    report=await api('/report');$('sessionMeta').textContent=(report.candidate_ref||short(sid))+' · 报告 v'+report.version;
    const profile=report.potential_profile||await api('/factor-profile?report_version='+report.version);
    $('potentialProfile').replaceChildren(window.SuitPotentialProfile.mount({profile,onEvidence:ref=>locate(ref,$('potentialProfile')).catch(e=>notice(e.message,true))}).element);
    if (!$('referenceTargets').children.length) for(const f of profile.factors) { const label=el('label',f.name), s=el('select');s.dataset.factor=f.factor_id; for(const value of ['',1,2,3,4,5]){const o=el('option',value===''?'未设置':value);o.value=value;s.append(o);} label.append(s);$('referenceTargets').append(label); }
    renderQuick();
  }
  function renderInputs() {
    for(const a of report.answers||[]) {
      const card=el('div',null,'research-answer'), label=el('label',null,'checkline'), check=el('input');check.type='checkbox';check.checked=a.current!==false;check.dataset.answer=a.answer_id;
      label.append(check,document.createTextNode((a.parent_question_id?'追问':'主问')+' · '+short(a.answer_id)+(a.current===false?' · 已重录替换':'')));
      const rev=el('select');rev.dataset.revision=a.answer_id;rev.setAttribute('aria-label','选择 '+short(a.answer_id)+' 转写版本');
      const revisions=a.revisions?.length?a.revisions:[{version:a.transcript_revision,text:a.transcript}];for(const item of revisions){const o=el('option','转写 v'+item.version+(item.version===a.transcript_revision?' · 当前':''));o.value=item.version;rev.append(o);}rev.value=String(a.transcript_revision);
      const transcript=el('p',a.transcript||'尚无转写');rev.addEventListener('change',()=>transcript.textContent=displayText(revisions.find(r=>r.version===Number(rev.value))?.text||''));
      const alignmentHost=el('div');let editor=null,editorRevision=null;
      const calibration=button('校准此版本句段时间',async()=>{
        const revision=Number(rev.value);
        if(!editor||editorRevision!==revision){
          if(!window.SuitAlignmentEditor)throw new Error('人工校准组件未加载，请刷新页面。');
          editorRevision=revision;editor=window.SuitAlignmentEditor.mount({answerId:a.answer_id,revision,
            fetchTimeline:r=>api('/answers/'+a.answer_id+'/timeline?transcript_revision='+r),
            save:body=>post('/answers/'+a.answer_id+'/alignment-reviews',body),
            onSaved:()=>{const key=a.answer_id+':'+revision,old=timelines.get(key);if(old){old.dispose();old.element.remove();timelines.delete(key);}notice('人工时间映射已保存，请重新打开此版本时间线核对。');}});
          alignmentHost.replaceChildren(editor.element);
        }
        editor.element.open=true;await editor.load();
      });
      card.append(label,rev,transcript,button('打开此版本时间线',()=>view(a,Number(rev.value),card).load()),calibration,alignmentHost);$('answerSelections').append(card);
    }
    if(!report.answers?.length)$('answerSelections').append(el('p','本场还没有回答，请先完成至少一道题。','research-empty'));
    for(const [key,name]of Object.entries(names)){const l=el('label'),c=el('input');c.type='checkbox';c.value=key;c.checked=!['semantic','gaze360','emotic'].includes(key);c.name='module';l.append(c,document.createTextNode(name));$('moduleSelections').append(l);}
  }
  async function refreshRuns() {
    const request=++runsRequest, data=await api('/research/runs');if(request!==runsRequest)return;
    listed=data;const holder=$('runList');holder.replaceChildren();
    for(const r of listed.runs){const isAutomatic=(report.standard_analysis?.runs||[]).some(a=>a.run_id===r.run_id);const row=el('div',null,'research-run'),c=el('input');c.type='checkbox';c.checked=selected.has(r.run_id);c.setAttribute('aria-label','选择运行 '+short(r.run_id));c.addEventListener('change',()=>c.checked?selected.add(r.run_id):selected.delete(r.run_id));const info=el('div');info.append(el('strong',(states[r.status]||r.status)+' · '+r.condition),el('p',r.modules.map(m=>names[m]).join(' / ')),el('small',stamp(r.created_at)+(isAutomatic?' · 本场自动运行 ':' · 独立运行 ')+short(r.run_id)+(isAutomatic?'':' · 重复 '+r.repeat_index)));const actions=el('div',null,'actions');actions.append(button('查看结果',()=>showRun(r.run_id)));if(r.experiment_id)actions.append(button('先标注',async()=>{$('annotationExperiment').value=r.experiment_id;await loadAnnotation();$('annotationForm').scrollIntoView({behavior:'smooth'});}));row.append(c,info,actions);holder.append(row);}
    if(!listed.runs.length)holder.append(el('p','尚无独立研究运行。选择素材后创建。','research-empty'));
    const old=$('annotationExperiment').value;$('annotationExperiment').replaceChildren();for(const e of listed.experiments){const option=el('option',short(e.experiment_id)+' · '+e.purpose);option.value=e.experiment_id;$('annotationExperiment').append(option);}if(listed.experiments.some(e=>e.experiment_id===old))$('annotationExperiment').value=old;
    if(!annotationInput&&!annotationLoading&&listed.experiments.length)await loadAnnotation();
    renderQuick();
  }
  function flattened(obj,prefix='') { const result={};for(const [k,v]of Object.entries(obj||{})){if(typeof v==='number'&&Number.isFinite(v))result[prefix+k]=v;else if(v&&typeof v==='object'&&!Array.isArray(v))Object.assign(result,flattened(v,prefix+k+'.'));}return result; }
  function observationChart(observations, timeline) {
    const wrapper=el('div'),label=el('label','选择观测指标'),select=el('select');const keys=[...new Set(observations.flatMap(o=>Object.keys(flattened(o.values))))];if(!keys.length)return wrapper;
    keys.forEach(k=>{const o=el('option',k);o.value=k;select.append(o);});label.append(select);wrapper.append(label);const ns='http://www.w3.org/2000/svg',svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 900 140');svg.setAttribute('role','img');svg.setAttribute('aria-label','按原片时间排列的观测曲线；点击点位回放');svg.classList.add('research-chart');wrapper.append(svg);
    function draw(){svg.replaceChildren();const values=observations.map(o=>({o,y:o.quality==='valid'?flattened(o.values)[select.value]:undefined})),valid=values.filter(x=>Number.isFinite(x.y));if(!valid.length)return;const maxTime=Math.max(...observations.map(o=>o.end_ms),1),ys=valid.map(x=>x.y),lo=Math.min(...ys),hi=Math.max(...ys),spread=hi-lo||1;let previous=null;
      values.forEach(item=>{if(!Number.isFinite(item.y)){previous=null;return;}const x=20+850*item.o.start_ms/maxTime,y=115-90*(item.y-lo)/spread;if(previous){const line=document.createElementNS(ns,'line');for(const[k,v]of Object.entries({x1:previous.x,y1:previous.y,x2:x,y2:y,stroke:'#73917b','stroke-width':1.5}))line.setAttribute(k,v);svg.append(line);}const point=document.createElementNS(ns,'circle');for(const[k,v]of Object.entries({cx:x,cy:y,r:3.2,fill:'#285f43',tabindex:0}))point.setAttribute(k,v);const title=document.createElementNS(ns,'title');title.textContent=(item.o.start_ms/1000).toFixed(2)+'s · '+select.value+' = '+item.y;point.append(title);const seek=()=>timeline.seek(item.o.start_ms).catch(e=>notice(e.message,true));point.addEventListener('click',seek);point.addEventListener('keydown',e=>{if(e.key==='Enter')seek();});svg.append(point);previous={x,y};});const t=document.createElementNS(ns,'text');t.setAttribute('x','20');t.setAttribute('y','135');t.setAttribute('font-size','11');t.textContent='范围 '+lo.toFixed(3)+' – '+hi.toFixed(3)+' · 0 – '+(maxTime/1000).toFixed(1)+' 秒（缺失窗口断开）';svg.append(t);}
    select.addEventListener('change',draw);draw();return wrapper;
  }
  async function showRun(rid) {
    const request=++runRequest,run=await api('/research/runs/'+rid);if(request!==runRequest)return;
    currentRun=run;disposeViews();const host=$('runDetail');host.replaceChildren();host.hidden=false;
    host.append(el('h2','运行 '+short(rid)+' · '+(states[run.status]||run.status)),el('p',run.input.condition+' · '+stamp(run.created_at)+' · '+(run.result?.duration_seconds==null?'耗时待记录':run.result.duration_seconds+' 秒')));
    host.append(el('p','冻结输入：'+Object.entries(run.input.input_versions).map(([id,v])=>short(id)+'/v'+v).join('，'),'small muted'));
    if(run.error)host.append(el('p',run.error,'notice warning'));
    const result=run.result;if(!result){host.append(el('p','结果尚未就绪；运行完成后重新打开。'));host.scrollIntoView({behavior:'smooth'});return;}
    seenExperiments.add(run.experiment_id);
    if(annotationExperimentId===run.experiment_id){$('exposure').value='ai_seen';rememberAnnotation();}
    for(const semantic of result.semantic||[]){const ev=semantic.evaluation,section=el('div',null,'research-observation');section.append(el('h3','文字语义 · '+ev.question_id),el('p','评分草稿 '+(ev.rating.score??'N/A')+' / 5 · '+ev.rating.reason),el('p','追问：'+ev.followup.reason));for(const ref of ev.rating.evidence||[])section.append(quote(ref,section));for(const unit of ev.evidence_units||[]){const box=el('div',null,'research-evidence-unit');box.append(el('strong',unit.category+' · '+unit.interpretation));for(const ref of unit.evidence)box.append(quote(ref,section));section.append(box);}section.append(rawDetails('评价点、证据缺口与模型来源',{criterion_matches:ev.criterion_matches,gaps:ev.gaps,provider:semantic.provider}));host.append(section);}
    for(const baseline of result.word_frequency||[]){const s=el('div',null,'research-observation');s.append(el('h3','词频对照 · '+baseline.question_id),el('p','按原词表匹配，不转换为能力分。分母：'+baseline.denominator.value+' 个中文字符／字母数字码点。'));s.append(table(['词条','原始次数','上下文规则保留','每100码点'],baseline.terms.filter(t=>t.raw_count).map(t=>[t.phrase,t.raw_count,t.context_retained_count,t.per_100_alphanumeric_codepoints?.toFixed(2)])));s.append(rawDetails('全部词条、上下文与来源',baseline));host.append(s);}
    for(const mm of result.multimodal||[]){if(request!==runRequest)return;const section=el('div',null,'research-observation');section.append(el('h3','回答 '+short(mm.answer_id)+' · 转写 v'+mm.transcript_revision),el('p','面部和声音为独立观察；样本标签与算法数值不等同于内心状态或稳定能力。','small muted'));host.append(section);const answer=run.input.answers.find(a=>a.answer_id===mm.answer_id)||{answer_id:mm.answer_id};const timeline=view(answer,mm.transcript_revision,section);try{await timeline.load();}catch(error){if(request!==runRequest)return;section.append(el('p','时间线暂不可用：'+error.message,'notice warning'));}if(request!==runRequest)return;
      const observed=window.SuitMultimodalView.mount({data:mm,timeline,run,sourceLabel:'独立研究运行'});multimodalViews.add(observed);section.append(observed.element);host.append(section);}
    if(request!==runRequest)return;if(result.errors?.length)host.append(rawDetails('本次失败模块',result.errors));host.append(rawDetails('完整可追溯运行数据',run));host.scrollIntoView({behavior:'smooth'});
  }
  function annotationDraft() {
    return {rater:$('rater').value,exposure:$('exposure').value,note:$('annotationNote').value,
      ratings:[...$('annotationRatings').children].filter(c=>c.dataset.question).map(card=>{
        const item={question_id:card.dataset.question};for(const field of ['score','answer','quote','reason'])item[field]=card.querySelector('[data-field='+field+']').value;return item;
      })};
  }
  function rememberAnnotation(dirty=false) {
    if(!annotationInput||!annotationExperimentId||annotationLoading)return;
    if(seenExperiments.has(annotationExperimentId))$('exposure').value='ai_seen';
    annotationDrafts.set(annotationExperimentId,annotationDraft());
    if(dirty)dirtyAnnotations.add(annotationExperimentId);
  }
  function quoteAmbiguity(card,answers) {
    const quote=card.querySelector('[data-field=quote]').value.trim(),answer=answers.find(a=>a.answer_id===card.querySelector('[data-field=answer]').value);
    let count=0,offset=0;while(quote&&answer&&(offset=answer.transcript.indexOf(quote,offset))!==-1){count++;offset++;}
    const hint=card.querySelector('[data-field=quoteHint]');
    hint.textContent=count>1?'该原话在所选版本出现 '+count+' 次；此研究标注只保存原话，尚未绑定具体位置。请补充前后文以区分。':quote&&!count?'在所选回答版本中未找到这段原话，请核对后再保存。':'研究标注保存原话级引用；不会自动生成句段编号、字符位置或媒体时间。';
    hint.className='small '+(quote&&count!==1?'warning':'muted');
  }
  function applyAnnotationDraft(experiment) {
    const draft=annotationDrafts.get(experiment);
    $('rater').value=draft?.rater||'';$('exposure').value=seenExperiments.has(experiment)?'ai_seen':draft?.exposure||'blind_self_declared';$('annotationNote').value=draft?.note||'';
    for(const card of $('annotationRatings').children){const item=draft?.ratings.find(r=>r.question_id===card.dataset.question);if(item)for(const field of ['score','answer','quote','reason'])card.querySelector('[data-field='+field+']').value=item[field];quoteAmbiguity(card,annotationInput.answers);}
  }
  async function loadAnnotation() {
    rememberAnnotation();const request=++annotationRequest,experiment=$('annotationExperiment').value,r=listed.runs.find(x=>x.experiment_id===experiment);
    annotationInput=null;annotationExperimentId='';annotationLoading=true;
    const host=$('annotationRatings');host.replaceChildren(el('p',r?'正在读取该实验冻结的回答版本…':'请先创建并选择研究实验。'));
    const buttons=[...$('annotationForm').querySelectorAll('button[type=submit]')];buttons.forEach(b=>b.disabled=true);
    for(const id of ['rater','exposure','annotationNote'])$(id).disabled=true;
    try {
      if(!r)return;const run=await api('/research/runs/'+r.run_id);if(request!==annotationRequest||$('annotationExperiment').value!==experiment)return;
      annotationInput=run.input;annotationExperimentId=experiment;host.replaceChildren();
      for(const q of annotationInput.questions){
        const answers=annotationInput.answers.filter(a=>(a.parent_question_id||a.question_id)===q.question_id);if(!answers.length)continue;
        const card=el('div',null,'research-annotation');card.dataset.question=q.question_id;card.append(el('h3',q.text));
        const scoreLabel=el('label','参考分'),score=el('select');score.dataset.field='score';for(const v of ['',1,2,3,4,5]){const o=el('option',v===''?'证据不足 / N/A':v);o.value=v;score.append(o);}scoreLabel.append(score);
        const source=el('select');source.dataset.field='answer';source.setAttribute('aria-label','标注引用来源回答');for(const a of answers){const o=el('option',short(a.answer_id)+' · v'+a.transcript_revision);o.value=a.answer_id;source.append(o);card.append(el('p',a.transcript,'small'));}
        const qlabel=el('label','原文引用'),quote=el('textarea');quote.dataset.field='quote';qlabel.append(quote);
        const hint=el('p');hint.dataset.field='quoteHint';hint.setAttribute('aria-live','polite');
        const reasonLabel=el('label','参考评分理由'),reason=el('textarea');reason.required=true;reason.maxLength=2000;reason.dataset.field='reason';reasonLabel.append(reason);
        quote.addEventListener('input',()=>quoteAmbiguity(card,answers));source.addEventListener('change',()=>quoteAmbiguity(card,answers));card.append(scoreLabel,source,qlabel,hint,reasonLabel);host.append(card);
      }
      applyAnnotationDraft(experiment);
    } finally {if(request===annotationRequest){annotationLoading=false;buttons.forEach(b=>b.disabled=!annotationInput);for(const id of ['rater','exposure','annotationNote'])$(id).disabled=!annotationInput;}}
  }
  async function annotationHistory(){const data=await api('/research/annotations');$('annotationHistory').replaceChildren(...data.annotations.map(a=>el('p',stamp(a.created_at)+' · '+a.rater+' · '+(a.exposure==='ai_seen'?'已看 AI':'自报独立盲评')+' · '+a.ratings.length+' 题','small')));}
  submit('runForm',async()=>{const selections=[...$('answerSelections').querySelectorAll('input:checked')].map(c=>({answer_id:c.dataset.answer,transcript_revision:Number($('answerSelections').querySelector('select[data-revision="'+c.dataset.answer+'"]').value)}));const modules=[...$('moduleSelections').querySelectorAll('input:checked')].map(c=>c.value);const created=await post('/research/runs',{version:report.version,selections,modules,repeats:Number($('repeats').value),purpose:$('purpose').value.trim(),condition:$('condition').value.trim(),participant_group:$('participantGroup').value.trim(),cloud_consent:$('cloudConsent').checked},'create');notice('已创建 '+created.runs.length+' 次独立运行。可先完成老师参考标注。');await refreshRuns();$('annotationExperiment').value=created.experiment.experiment_id;await loadAnnotation();});
  submit('annotationForm',async()=>{
    if(annotationLoading||!annotationInput||annotationExperimentId!==$('annotationExperiment').value)throw new Error('请等待所选实验的冻结版本加载完成。');
    rememberAnnotation();const experiment=annotationExperimentId,draft=annotationDrafts.get(experiment),signature=JSON.stringify(draft);
    const ratings=draft.ratings.map(item=>{const a=annotationInput.answers.find(a=>a.answer_id===item.answer),quote=item.quote.trim();if(!a)throw new Error('标注回答与当前实验不一致，请重新选择。');return{question_id:item.question_id,score:item.score===''?null:Number(item.score),reason:item.reason.trim(),evidence:quote?[{answer_id:a.answer_id,transcript_revision:a.transcript_revision,quote}]:[]};});
    await post('/research/annotations',{experiment_id:experiment,rater:draft.rater.trim(),exposure:draft.exposure,ratings,note:draft.note.trim()},'annotation:'+experiment);
    if(annotationExperimentId===experiment)rememberAnnotation();if(JSON.stringify(annotationDrafts.get(experiment))===signature)dirtyAnnotations.delete(experiment);
    notice('独立参考已保存，正式内容评分未改变。');await annotationHistory();
  });
  submit('referenceForm',async()=>{const targets={};$('referenceTargets').querySelectorAll('select').forEach(s=>{if(s.value)targets[s.dataset.factor]=Number(s.value);});await post('/reference-profiles',{version:report.version,targets,title:$('referenceTitle').value.trim(),source:$('referenceSource').value.trim(),status:$('referenceStatus').value},'reference');await loadReport();notice('参考要求已保存到新报告版本；旧版保持原有口径。');});
  submit('publishForm',async()=>{await post('/research/publish',{version:report.version,run_ids:[...selected],note:$('publicationNote').value.trim()},'publish');await loadReport();notice('选定研究已收入报告 v'+report.version+'，可从上方“本场报告”查看。');});
  $('refreshRuns').addEventListener('click',()=>refreshRuns().catch(e=>notice(e.message,true)));
  $('quickAnalyze').addEventListener('click',()=>quickAnalyze().catch(e=>notice(e.message,true)));
  $('quickView').addEventListener('click',()=>{const run=currentLocalRun();if(run)showRun(run.run_id).catch(e=>notice(e.message,true));});
  $('quickPublish').addEventListener('click',async()=>{if(quickBusy)return;quickBusy=true;renderQuick();try{await loadReport();const run=currentLocalRun();if(!run||!['succeeded','partial'].includes(run.status))throw new Error('本场当前素材的分析尚未完成。');if(report.research_appendix?.selected_runs?.some(r=>r.run_id===run.run_id)){notice('这次分析已经加入本场报告，无需重复发布。');return;}await post('/research/publish',{version:report.version,run_ids:[run.run_id],note:'本场当前音视频的本地分析，状态：'+states[run.status]+'。模型观察待本人及指导老师核对，不参与内容分数或追问。'},'quick-publish:'+run.run_id);await loadReport();notice('分析已保存到本场报告 v'+report.version+'，点上方“本场报告”即可查看。');}catch(e){notice(e.message,true);}finally{quickBusy=false;renderQuick();}});
  $('annotationExperiment').addEventListener('change',()=>loadAnnotation().catch(e=>notice(e.message,true)));
  for(const name of ['input','change'])$('annotationForm').addEventListener(name,event=>{if(event.target!==$('annotationExperiment'))rememberAnnotation(true);});
  $('compareRuns').addEventListener('click',async()=>{try{const comparison=await post('/research/compare',{run_ids:[...selected]});$('comparison').replaceChildren(el('h3','运行对照'),el('p',comparison.same_inputs?'相同回答版本与参与者分组':'输入不同，不能把差值直接视为方法差异。'),table(['运行','条件','状态','耗时（秒）'],comparison.runs.map(r=>[short(r.run_id),r.input.condition,states[r.status]||r.status,r.result?.duration_seconds])),el('p','独立参考 MAE：'+(comparison.blind_reference_mae??'N/A')+' · 可比较评分对 '+comparison.blind_scored_pairs),table(['运行','题目','模型','参考','绝对差','暴露'],comparison.differences.map(r=>[short(r.run_id),r.question_id,r.model_score,r.reference_score,r.absolute_difference,r.exposure])),el('p',comparison.limitations,'small muted'));}catch(e){notice(e.message,true);}});
  $('downloadResearch').addEventListener('click',async()=>{try{saveJSON(await api('/research/export'),'research-'+short(sid)+'.json');}catch(e){notice(e.message,true);}});
  window.addEventListener('pagehide',disposeViews);
  window.addEventListener('beforeunload',event=>{if(dirtyAnnotations.size){event.preventDefault();event.returnValue='';}});
  window.addEventListener('hashchange',()=>{const next=new URLSearchParams(location.hash.slice(1));if((next.has('session')||next.has('token'))&&(next.get('session')!==sid||next.get('token')!==token))location.reload();});
  async function init(){if(!sid||!token)throw new Error('请从本场工作台或老师复核页进入研究工作台。');for(const[id,path]of [['reviewLink','/hr-review'],['reportLink','/interview-report']])$(id).href=path+'#'+new URLSearchParams({session:sid,token});$('workbenchLink').href='/workbench#session='+encodeURIComponent(sid);$('quickWorkbench').href=$('workbenchLink').href;await loadReport();renderInputs();await refreshRuns();await annotationHistory();$('researchContent').hidden=false;notice(currentAnswers().length?'本场回答已加载。上方显示分析进度；可一键分析音视频或直接查看已有结果。':'请先确认场次：当前没有已提交回答。');setInterval(()=>{if(!document.hidden&&listed.runs.some(r=>['queued','running'].includes(r.status)))refreshRuns().catch(e=>notice(e.message,true));},5000);}
  init().catch(e=>notice(e.message,true));
})();
