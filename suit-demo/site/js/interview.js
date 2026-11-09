(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const displayText = text => window.SuitTextDisplay?.simplify(String(text ?? '')) ?? String(text ?? '');
  const params = new URLSearchParams(location.hash.slice(1));
  let sessionId = params.get('session') || '', token = params.get('token') || '';
  let state = null, draft = null, draftRound = '', stream = null, recorder = null;
  let recording = false, busy = false, previewUrl = '', timerId = null, pollId = null, autosaveId = null;
  let startedAt = 0, chunks = [], dbPromise, correctionKey = null;
  let recordingPurpose = '', submitAfterStop = false, recordingWrites = Promise.resolve(), deviceOpening = false;
  let audioContext = null, meterFrame = null, practiceUrl = '', speaking = false, spokenRound = '', nextWaiting = false, speechGeneration = 0, recordingFault = false;
  let deviceRequestSerial = 0, pageClosed = false, practiceCompleted = false, audioObserved = false, nextWaitText = '';
  let sessionStarted = false, advancePromise = null, advanceTimer = null, advanceError = '', advanceErrorRound = '';
  try { sessionStarted = sessionStorage.getItem('suit-started-' + sessionId) === 'yes'; } catch (_) {}
  const uuid = () => { if (crypto.randomUUID) return crypto.randomUUID(); const bytes = crypto.getRandomValues(new Uint8Array(16)); bytes[6] = (bytes[6] & 15) | 64; bytes[8] = (bytes[8] & 63) | 128; const hex = [...bytes].map(value => value.toString(16).padStart(2,'0')).join(''); return hex.slice(0,8) + '-' + hex.slice(8,12) + '-' + hex.slice(12,16) + '-' + hex.slice(16,20) + '-' + hex.slice(20); };
  const endpoint = suffix => '/v1/interviews/' + encodeURIComponent(sessionId) + (suffix || '');
  let rememberedIdentity = '', rememberedCompletion = false, rememberedAnswers = false;
  function rememberSession(hasAnswers = false, completed = false) {
    if (!sessionId) return;
    const value = JSON.stringify({session_id:sessionId,updated_at:Date.now()});
    for (const key of ['suit-recent-interview-v1', ...(hasAnswers ? ['suit-recent-answered-interview-v1'] : [])]) {
      try { localStorage.setItem(key,value); } catch (_) { try { sessionStorage.setItem(key,value); } catch (_) {} }
    }
    rememberedIdentity = sessionId; rememberedAnswers = hasAnswers; rememberedCompletion = completed;
  }
  function interviewPlan(){
    const mode=state?.form_mode||$('createFormMode')?.value||'quick';
    const positive=value=>Number.isInteger(value)&&value>0,nonnegative=value=>Number.isInteger(value)&&value>=0;
    const main=positive(state?.main_question_count)?state.main_question_count:positive(state?.budget?.main_maximum)?state.budget.main_maximum:mode==='full'?8:3;
    const follow=nonnegative(state?.max_followups)?state.max_followups:nonnegative(state?.budget?.followup_maximum)?state.budget.followup_maximum:nonnegative(state?.budget?.maximum)&&state.budget.maximum>=main?state.budget.maximum-main:mode==='full'?4:1;
    return {mode,main,follow};
  }
  function updatePlanLabel(){const plan=interviewPlan();$('planBadge').textContent=plan.main+' 道主问 · 最多 '+plan.follow+' 道追问';if($('createModeHint'))$('createModeHint').textContent=plan.mode==='full'?'为八个因子分别安排主问；题目覆盖不等于已充分体现全部要求。':'快速体验安排三个因子，报告只形成局部结论。';}
  function completionLinks() {
    const holder = $('completionRoleLinks'); holder.replaceChildren();
    $('completionIdentity').textContent = '本场档案 ' + sessionId.slice(0,12) + ' · ' + (state?.created_at ? new Date(state.created_at).toLocaleString('zh-CN',{hour12:false}) : '创建时间未记录');
    let credential = '';
    try { const saved = JSON.parse(sessionStorage.getItem('suit-workbench-hr-links-v1') || '{}'); const item = saved && typeof saved === 'object' && !Array.isArray(saved) ? saved[sessionId] : null; if (typeof item?.token === 'string') credential = item.token; } catch (_) {}
    holder.hidden = false;
    if (credential) for (const [path,label] of [['/interview-report','查看本场报告']]) {
      const link = document.createElement('a'); link.className = 'button primary'; link.textContent = label;
      link.href = path + '#' + new URLSearchParams({session:sessionId,token:credential}).toString(); holder.append(link);
    }
    const back = document.createElement('a'); back.className = 'button ' + (credential ? 'subtle' : 'primary'); back.textContent = '返回工作台'; back.href = '/workbench#session=' + encodeURIComponent(sessionId); holder.append(back);
    const latest = new Map();
    for(const job of state?.jobs || []) { if(job.status === 'superseded')continue; const k=job.job_type==='content'?'content:'+job.question_id:'media:'+job.answer_id; const old=latest.get(k); if(!old || String(job.created_at || '')>=String(old.created_at || ''))latest.set(k,job); }
    const jobs=[...latest.values()], pending=jobs.some(job=>['queued','running'].includes(job.status)), failed=jobs.some(job=>['failed','blocked'].includes(job.status));
    $('completionReportState').textContent=pending?'报告正在后台生成，已有内容可先查看。':failed?'报告已有部分内容，尚有分析待老师处理。':'可在老师端查看本场报告及分析进度。';
    $('completionAccessNote').textContent=credential?'报告会随分析结果更新；查看报告不影响已保存的回答。':'本场报告由指导老师查看，无需重复作答。';
  }
  function updateWorkbenchLinks() { document.querySelectorAll('[data-workbench-link]').forEach(link => { link.href = '/workbench' + (sessionId ? '#session=' + encodeURIComponent(sessionId) : ''); }); }
  const statusNames = {created:'待开始',answering:'进行中',interview_finished:'已完成',active:'进行中',in_progress:'进行中',completed:'已完成',complete:'已完成',queued:'等待转写',pending:'等待转写',running:'正在转写',processing:'正在转写',completed_with_errors:'转写需复核',failed:'转写失败',done:'已完成',succeeded:'已完成',ready:'已完成',not_required:'文字已保存',text_only:'文字已保存',not_requested:'文字已保存'};
  function notice(message, kind = '') { $('notice').textContent = message; $('notice').className = 'notice' + (kind ? ' ' + kind : ''); $('notice').hidden = !message; }
  function errorMessage(error) { return error.message || '请求失败，请检查本地服务和网络连接后重试。'; }
  async function api(url, options = {}) {
    const headers = new Headers(options.headers || {});
    if (token) headers.set('Authorization', 'Bearer ' + token);
    if (options.body && !(options.body instanceof FormData)) headers.set('Content-Type', 'application/json');
    let response;
    try { response = await fetch(url, {cache:'no-store', ...options, headers}); }
    catch (_) { throw new Error('无法连接本地服务。已保留草稿，请检查服务是否运行后重试。'); }
    let payload;
    try { payload = await response.json(); } catch (_) { payload = {}; }
    if (!response.ok) {
      const detail = payload.detail;
      const message = typeof detail === 'string' ? detail : detail?.message;
      const error = new Error(message || (response.status === 401 || response.status === 403 ? '访问凭证无效或权限不足，请使用候选人专用链接。' : '请求未完成（' + response.status + '），请重试。'));
      error.status = response.status; error.code = detail?.code; throw error;
    }
    return payload;
  }
  function openDB() {
    if (!dbPromise) dbPromise = new Promise((resolve, reject) => {
      let rejected=false;
      const request = indexedDB.open('suit-interview-drafts-v1', 2);
      request.onupgradeneeded = () => {
        if (!request.result.objectStoreNames.contains('drafts')) request.result.createObjectStore('drafts', {keyPath:'key'});
        if (!request.result.objectStoreNames.contains('recordings')) request.result.createObjectStore('recordings', {keyPath:'key'});
      };
      request.onsuccess = () => { if(rejected){request.result.close();return;} request.result.onversionchange = () => request.result.close(); resolve(request.result); };
      request.onerror = () => { rejected=true; dbPromise=null; reject(request.error); };
      request.onblocked = () => { rejected=true; dbPromise=null; reject(new Error('其他旧作答页仍在使用草稿库')); };
    });
    return dbPromise;
  }
  async function draftStore(action, value) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const transaction = db.transaction('drafts', action === 'get' ? 'readonly' : 'readwrite');
      const store = transaction.objectStore('drafts');
      const request = store[action](value);
      transaction.oncomplete = () => resolve(request.result);
      transaction.onerror = () => reject(transaction.error);
      transaction.onabort = () => reject(transaction.error);
    });
  }
  async function recordingPart(target, blob, index) {
    const db = await openDB(), key = target.key + ':' + target.attempt_id + ':' + index;
    return new Promise((resolve, reject) => {
      const transaction = db.transaction(['drafts','recordings'], 'readwrite');
      transaction.objectStore('recordings').put({key,blob});
      target.recording_parts = [...(target.recording_parts || []), key];
      target.recording_incomplete = true; target.updated_at = Date.now();
      transaction.objectStore('drafts').put(target);
      transaction.oncomplete = () => resolve();
      transaction.onerror = () => reject(transaction.error); transaction.onabort = () => reject(transaction.error);
    });
  }
  async function recoverRecording(target) {
    if (target.blob || !target.recording_parts?.length) return;
    const db = await openDB();
    const parts = await Promise.all(target.recording_parts.map(key => new Promise((resolve,reject) => {
      const transaction = db.transaction('recordings','readonly'), request = transaction.objectStore('recordings').get(key);
      transaction.oncomplete = () => resolve(request.result?.blob || null); transaction.onerror = () => reject(transaction.error);
    })));
    const found=[]; for(const part of parts) { if(!part)break;found.push(part); }
    if (found.length) { target.blob = new Blob(found,{type:target.recording_mime || 'video/webm'}); target.name = '中断前保留的回答'+(target.recording_mime?.includes('mp4')?'.mp4':'.webm'); target.recording_incomplete = true; }
  }
  async function removeRecordingParts(target) {
    if (!target?.recording_parts?.length) return;
    const db = await openDB();
    await new Promise((resolve,reject) => {
      const transaction = db.transaction('recordings','readwrite'), store = transaction.objectStore('recordings');
      for (const key of target.recording_parts) store.delete(key);
      transaction.oncomplete = () => resolve(); transaction.onerror = () => reject(transaction.error);
    });
    target.recording_parts = [];
  }
  function newDraft(roundId) { return {key:sessionId + ':' + roundId,round_id:roundId,attempt_id:uuid(),idempotency_key:uuid(),text:'',blob:null,name:'',mode:selectedMode(),attempted:false,updated_at:Date.now()}; }
  async function saveDraft() {
    if (!draft) return;
    draft.updated_at = Date.now();
    try { await draftStore('put', draft); $('draftStatus').textContent = draft.attempted ? '待确认的提交已保存在浏览器' : '草稿已保存在当前浏览器'; return true; }
    catch (_) { $('draftStatus').textContent = '草稿保存失败，请勿刷新'; notice('当前浏览器无法保存草稿（可能空间不足或禁用存储）。请勿刷新页面，提交成功前请保留原始文件。', 'warning'); return false; }
  }
  function selectedMode() { return document.querySelector('input[name="mode"]:checked')?.value || 'media'; }
  function setPreview(blob, name) {
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = '';
    $('preview').srcObject = null;
    $('preview').removeAttribute('src');
    $('preview').muted = false;
    $('preview').controls = true;
    if (blob) { previewUrl = URL.createObjectURL(blob); $('preview').src = previewUrl; }
    $('videoPlaceholder').hidden = Boolean(blob);
    $('clearMedia').hidden = !blob;
    $('redoRecording').hidden = !blob;
    $('recoveredMediaLabel').hidden = !draft?.recording_incomplete;
    $('mediaInfo').textContent = blob ? (name || '本轮录制.webm') + ' · ' + (blob.size / 1024 / 1024).toFixed(1) + ' MB · 已保留在本机浏览器草稿' : 'MP4、WebM 或常见音频格式 · 最长 180 秒 · 最大 100 MB';
  }
  function updateControls() {
    const liveDevices = deviceStreamLive();
    const locked = busy || recording || deviceOpening || Boolean(draft?.attempted);
    $('recordButton').disabled = locked;
    $('recordButton').textContent = deviceOpening ? '正在开启设备…' : liveDevices ? '返回设备预览' : '开启设备预览';
    $('trialButton').disabled = locked || !liveDevices || Boolean(draft?.blob);
    $('trialButton').textContent = practiceCompleted ? '再试录 5 秒（可选）' : '试录 5 秒（可选）';
    $('closeDevice').disabled = busy || recording || deviceOpening || !stream;
    $('redoRecording').disabled = locked;
    $('mediaFile').disabled = locked;
    $('clearMedia').disabled = locked;
    $('transcript').disabled = locked;
    document.querySelectorAll('input[name="mode"]').forEach(input => input.disabled = locked);
    const textMode = selectedMode() === 'text', hasMedia = Boolean(draft?.blob);
    $('submitButton').disabled = busy || deviceOpening || (recording && recordingPurpose === 'practice') || (!textMode && !hasMedia && !recording && !liveDevices);
    $('submitButton').textContent = busy ? '正在保存…' : recording ? (recordingPurpose === 'practice' ? '正在试录…' : '结束回答') : draft?.attempted ? '重试保存回答' : !sessionStarted ? '开始面试' : textMode || hasMedia ? '提交回答' : '开始回答';
    $('actionHint').textContent = busy ? '确认保存成功前请保留本页' : recording ? (recordingPurpose === 'practice' ? '这是设备试录，不计入正式回答' : '完整回答后，点击结束回答') : draft?.attempted ? '重试只补传缺失分片，不会重复答卷' : !sessionStarted ? (textMode ? '点击“开始面试”阅读第一题，再输入回答。' : hasMedia ? '先回放核对已载入的文件，再继续。' : liveDevices ? '确认画面与声音后，点击“开始面试”阅读第一题。' : '先开启设备预览，连接摄像头与麦克风。') : textMode ? '提交后根据内容判断是否需要追问' : hasMedia ? '可以先回放检查，再提交' : liveDevices ? '本场设备保持连接，无需重复检查；点击开始回答才录制' : '设备当前未连接，请重新开启设备预览。';
    $('nextButton').disabled = busy;
    $('nextButton').hidden = !advanceError;
    $('nextButton').textContent = '重试连接';
    $('nextHint').textContent = advanceError || '回答已保存，正在准备下一题…';
    $('sessionDeviceStatus').textContent = liveDevices ? '设备保持开启，当前未录制；下一题无需重检，整场结束后自动关闭。' : '设备当前已关闭；下一题可重新开启，已保存回答不受影响。';
    $('submittedCloseDevice').disabled = recording || deviceOpening || !stream;
    $('saveCorrection').disabled = busy;
    $('restartSubmission').disabled = busy || recording;
    $('speakQuestion').disabled = recording || busy || !window.speechSynthesis;
    $('autoRead').disabled = recording || !window.speechSynthesis;
    $('setupTransition').hidden = sessionStarted || Boolean(draft?.blob);
    $('setupTransition').classList.toggle('is-connected',liveDevices&&!recording);
    $('setupTransition').classList.toggle('is-checking',deviceOpening||recording);
    $('setupPhase').textContent = textMode ? '准备开始' : deviceOpening ? '第 1 步 · 正在连接' : liveDevices ? '第 2 步 · 确认后开始' : '第 1 步 · 连接设备';
    $('setupReadyTitle').textContent = textMode ? '已选择文字作答' : deviceOpening ? '正在连接摄像头与麦克风…' : recording ? '正在进行可选试录' : liveDevices ? '摄像头与麦克风已连接' : '请先开启设备预览';
    $('setupReadyDescription').textContent = textMode ? '点击“开始面试”查看第一题，再输入回答。文字作答不会开启摄像头或麦克风。' : deviceOpening ? '请在浏览器提示中允许使用设备；连接后仍需你确认画面与声音。' : recording ? '这段试录只用于本机回放检查，不会提交为正式回答。' : liveDevices ? (practiceCompleted ? '试录已结束，请回放确认画面与声音。' : '请确认预览画面能看清、说话时音量条有变化；也可试录 5 秒后回放。') + '确认后点击下方“开始面试”，阅读第一题。此时尚未正式录制。' : '开启后检查预览画面与音量变化，确认后再开始面试。本场检查一次，后续题目复用设备连接。';
    $('setupIntro').hidden = sessionStarted;
    $('consentLabel').hidden = sessionStarted && $('consent').checked;
    $('answerHeading').textContent = sessionStarted ? '你的回答' : textMode ? '准备开始文字面试' : liveDevices ? '设备已连接，确认后开始' : '检查设备，准备开始';
    $('setupEyebrow').textContent='开始前 · 了解本场流程';
    const plan=interviewPlan();$('setupDescription').textContent='本场共 '+plan.main+' 道主问，最多 '+plan.follow+' 道追问。每题保存后自动进入下一步。';updatePlanLabel();
    $('setupStepPrepare').textContent=textMode?'选择文字方式':'检查一次设备';
    $('setupRecordingNote').textContent=textMode?'无需开启摄像头和麦克风，不录音录像。开始面试后看到题目再输入回答，提交后后台继续分析。':'预览不录制；可选的 5 秒试录仅供本机回放，不上传。正式作答点击“开始回答”录音录像，“结束回答”即停止并保存，后台分析继续。';
    $('consentText').textContent=textMode?'我已了解本场流程，同意按所选方式保存文字回答，供指导老师查看原话及本场报告，并发送至已配置的百炼服务分析。文字方式不采集音视频，结果用于方法验证。':'我已了解本场流程，同意按所选方式保存回答，供指导老师查看原话、录像片段及本场报告；音视频用于本地面部动作、基础表情、视线和声音观测，回答文字发送至已配置的百炼服务分析。结果用于方法验证。';
    $('recordButton').classList.toggle('primary',!sessionStarted&&!liveDevices);
    $('questionCard').hidden = !sessionStarted || !state?.current_round;
    $('transcriptLabel').hidden = !textMode || !sessionStarted;
    $('transcript').hidden = !textMode || !sessionStarted;
    $('transcriptHint').hidden = !textMode || !sessionStarted;
    $('trialButton').hidden = sessionStarted;
  }
  function updateMode() {
    const textOnly = selectedMode() === 'text';
    $('mediaPanel').hidden = textOnly;
    $('transcriptLabel').textContent = textOnly ? '文字回答' : '补充文字（可选）';
    $('transcriptHint').textContent = '提交后会自动显示下一题。';
    $('transcript').placeholder=textOnly?'在这里输入你的回答':'补充文字（可选）';
  }
  function currentAnswer() { return state?.answers?.find(answer => answer.round_id === state.current_round?.round_id && answer.current !== false); }
  function answerText(answer) { return answer?.transcript ?? answer?.transcript_current ?? ''; }
  function isPending(answer) { return ['queued','pending','running','processing'].includes(answer?.job_status); }
  function contentPhase(answer) {return answer?.content_status || 'not_requested';}
  function decisionAnswer(result) {const snapshot=result.state||result,roundId=snapshot.current_round?.round_id||state?.current_round?.round_id;return snapshot.answers?.find(answer=>answer.round_id===roundId&&answer.current!==false)||currentAnswer();}
  function nextStepHint() {
    const answer=currentAnswer();
    if(!answer)return '本题保存后可以继续。';
    if(isPending(answer))return '转写仍在后台进行。可以点击继续；只有需要决定追问时才等待对应文字分析。';
    if(['failed','completed_with_errors'].includes(answer.job_status))return '转写未完整就绪，可校正文字后继续；已保存的原始回答保持不变。';
    if(['queued','running','pending','processing'].includes(contentPhase(answer)))return '内容分析正在后台进行。可以点击继续，页面会显示是否仍需等待追问判断。';
    return '本题已保存，点击继续确认下一步。其他研究分析在后台继续。';
  }
  function pendingDecisionText(result) {
    const answer=decisionAnswer(result), media=answer?.job_status, content=contentPhase(answer);
    const phase=['queued','pending'].includes(media)?'正在等待语音转写': ['running','processing'].includes(media)?'正在转写本题音视频': ['queued','pending'].includes(content)?'文字已就绪，正在等待内容分析': ['running','processing'].includes(content)?'正在分析本题文字，判断是否追问':'正在确认本题的追问决定';
    return phase+(Number.isFinite(result.decision_wait_remaining)?' · 本次决定等待上限剩余约 '+Math.ceil(Math.max(0,result.decision_wait_remaining))+' 秒':'')+'。回答已保存，无需重复提交。';
  }
  function renderProcessingSteps(answer) {
    $('jobLabel').textContent=statusNames[answer.job_status]||answer.job_status||'已保存';
    const steps=$('processingSteps');steps.replaceChildren();
    const content=contentPhase(answer),contentLabel={queued:'内容分析排队中',pending:'内容分析排队中',running:'内容分析中',processing:'内容分析中',succeeded:'内容分析已完成',complete:'内容分析已完成',completed:'内容分析已完成',failed:'内容分析未完成',blocked:'内容分析暂不可用'}[content]||'继续时确认下一步';
    for(const [label,css]of [['回答已保存','is-done'],[isPending(answer)?(['queued','pending'].includes(answer.job_status)?'语音转写排队中':'语音转写中'):['failed','completed_with_errors'].includes(answer.job_status)?'转写需核对':'文字已就绪',isPending(answer)?'is-waiting':['failed','completed_with_errors'].includes(answer.job_status)?'is-error':'is-done'],[contentLabel,['queued','pending','running','processing'].includes(content)?'is-waiting':['succeeded','complete','completed'].includes(content)?'is-done':['failed','blocked'].includes(content)?'is-error':'']]){const step=document.createElement('span');step.className='processing-step '+css;step.textContent=label;steps.append(step);}
  }
  function renderJourney() {
    const list = $('roundList'); list.replaceChildren();
    const rounds = state.rounds?.length ? state.rounds : (state.current_round ? [state.current_round] : []);
    let mainNumber = 0;
    for (const [index, round] of rounds.entries()) {
      if (round.kind !== 'followup') mainNumber++;
      const li = document.createElement('li'), dot = document.createElement('span'), title = document.createElement('span');
      const done = state.answers?.some(answer => answer.round_id === round.round_id && answer.current !== false);
      li.className = state.current_round?.round_id === round.round_id ? 'active' : done ? 'done' : '';
      dot.className = 'round-dot'; dot.textContent = done ? '✓' : String(index + 1);
      title.className = 'round-title'; title.textContent = round.kind === 'followup' ? '补充追问' : '主问题 ' + mainNumber;
      li.append(dot, title); list.append(li);
    }
  }
  async function loadDraft(roundId) {
    clearTimeout(autosaveId);
    draftRound = roundId;
    try { draft = await draftStore('get', sessionId + ':' + roundId); }
    catch (_) { draft = null; notice('浏览器草稿存储不可用。提交成功前请勿刷新，并保留原始音视频。', 'warning'); }
    const recovered = Boolean(draft);
    if(recovered && (draft.blob || draft.text || draft.recording_parts?.length))sessionStarted=true;
    if (!draft) draft = newDraft(roundId);
    try { await recoverRecording(draft); } catch (_) { notice('未能读取完整录制草稿，请检查保留的文件或重新录制。','warning'); }
    $('recoveredMediaConfirm').checked = false;
    $('transcript').value = draft.text || '';
    document.querySelectorAll('input[name="mode"]').forEach(input=>input.checked=input.value===(draft.mode==='text'?'text':'media'));
    $('pendingNotice').hidden = !recovered;
    $('pendingNotice').textContent = draft.recording_incomplete ? '上次录制中断。已恢复采集到的片段，请回放确认或重新录制；未完成的片段不会自动提交。' : draft.attempted ? '上次提交尚未确认。网络恢复后点击继续上传，系统只补传缺片。' : '已恢复本轮未提交草稿，请检查内容后继续。';
    $('draftStatus').textContent = recovered ? '草稿已从当前浏览器恢复' : '草稿保存在当前浏览器';
    setPreview(draft.blob, draft.name); updateMode();
    if(!draft.blob && draft.mode!=='text' && deviceStreamLive())await showLivePreview(true);
    updateControls();
    $('uploadStatus').textContent = draft.attempted ? '上次提交结果未确认，请用同一草稿重试。' : '';
    $('restartSubmission').hidden=!draft.attempted;
  }
  function focusQuestion() { $('questionText').setAttribute('tabindex','-1');$('questionText').focus({preventScroll:true});$('questionCard').scrollIntoView({behavior:'smooth',block:'start'}); }
  async function render(nextState) {
    if (!nextState) return;
    const previousRound=state?.current_round?.round_id;
    state = nextState;
    const hasAnswers = Boolean(state.answers?.some(answer => answer.current !== false)), completed = ['completed','complete','interview_finished'].includes(state.status);
    if (hasAnswers) sessionStarted = true;
    if (advanceErrorRound && state.current_round?.round_id !== advanceErrorRound) { advanceError=''; advanceErrorRound=''; }
    if(completed){stopSpeech();stopTracks();}
    if (rememberedIdentity !== sessionId || (hasAnswers && !rememberedAnswers) || (completed && !rememberedCompletion)) rememberSession(hasAnswers,completed);
    updateWorkbenchLinks();
    $('welcome').hidden = true; $('sessionView').hidden = false;
    $('sessionLabel').textContent = displayText(state.candidate_ref || '面试 ' + sessionId.slice(0, 12));
    $('stateLabel').textContent = statusNames[state.status] || state.status || '进行中';
    const mainRoundIds = new Set((state.rounds || []).filter(round => round.kind === 'main').map(round => round.round_id));
    const mainAnswered = new Set((state.answers || []).filter(answer => answer.current !== false && mainRoundIds.has(answer.round_id)).map(answer => answer.question_id)).size;
    const plan=interviewPlan();$('progressLabel').textContent = Math.min(mainAnswered, plan.main) + ' / '+plan.main+' 道主问题';updatePlanLabel();
    renderJourney();
    const round = state.current_round;
    if (!round) {
      $('questionCard').hidden = true; $('answerCard').hidden = true; $('submittedCard').hidden = true;
      $('completeCard').hidden = !['completed','complete','interview_finished'].includes(state.status);
      if (!$('completeCard').hidden) { completionLinks();if(previousRound){$('completionTitle').focus({preventScroll:true});$('completeCard').scrollIntoView({behavior:'smooth',block:'start'});} }
      if ($('completeCard').hidden) notice('当前尚无可作答轮次，请刷新页面重试。', 'warning');
      return;
    }
    $('completeCard').hidden = true; $('questionCard').hidden = false;
    const mainNumber = (state.rounds || []).filter(item => item.kind === 'main').findIndex(item => item.round_id === round.round_id) + 1;
    $('roundKind').textContent = round.kind === 'followup' ? 'FOLLOW-UP / 补充追问' : 'MAIN QUESTION / 主问题 · ' + (mainNumber || 1);
    $('questionText').textContent = displayText(round.text || round.prompt || '');
    $('questionHint').textContent = round.kind === 'followup' ? '请针对上一段回答补充具体事实，本轮证据会并入对应主问题。' : round.question_id === 'results_K6' ? '请基于题目给定的目标、进度与预算约束，说明你的分析、行动安排与检查点。' : '请以一段真实经历回答，可以说明情境、行动、结果和反思。';
    const answer = currentAnswer();
    $('answerCard').hidden = Boolean(answer); $('submittedCard').hidden = !answer;
    if (answer) {
      if (draft?.round_id === round.round_id) {
        try { await removeRecordingParts(draft); } catch (_) { /* Confirmed media is already on the server. */ }
        try { await draftStore('delete', draft.key); } catch (_) { /* A confirmed server copy exists. */ }
        draft = null;
      }
      $('jobLabel').textContent = statusNames[answer.job_status] || answer.job_status || '已保存';
      $('submittedTranscript').textContent = displayText(answerText(answer)) || (isPending(answer) ? '音视频已保存，正在等待转写。此时可以安全刷新页面。' : '当前没有可用转写。可通过“校正转写文字”补充，再继续下一轮。');
      if (!$('correctionDetails').open) $('correctedText').value = answerText(answer);
      $('correctionDetails').hidden = true;
      renderProcessingSteps(answer);
    } else if (draftRound !== round.round_id || !draft) {
      await loadDraft(round.round_id);
    }
    updateControls();
    if (spokenRound !== round.round_id) {
      spokenRound = round.round_id; stopSpeech();
      if (sessionStarted && $('autoRead').checked && !answer) speakQuestion();
    }
    if(sessionStarted&&!answer&&previousRound!==round.round_id)focusQuestion();
    queueAdvance();
  }
  async function refresh(silent = false) {
    if (!sessionId || !token) return;
    try { await render(await api(endpoint())); }
    catch (error) { if (!silent || error.status === 401 || error.status === 403) notice(errorMessage(error), 'error'); }
  }
  async function createInterview(event) {
    event.preventDefault();
    $('createButton').disabled = true; notice('正在准备面试…');
    const form_mode=$('createFormMode').value==='full'?'full':'quick',candidate_ref=$('candidateRef').value.trim(),signature=JSON.stringify({candidate_ref,form_mode});let key;
    try { const previous=JSON.parse(sessionStorage.getItem('suit-create-request-v2')||'null');key=previous?.signature===signature?previous.idempotency_key:uuid();sessionStorage.setItem('suit-create-request-v2',JSON.stringify({signature,idempotency_key:key})); } catch (_) { key=uuid(); }
    try {
      const result = await api('/v1/interviews', {method:'POST',body:JSON.stringify({candidate_ref,form_mode,idempotency_key:key})});
      sessionId = result.session_id; token = result.candidate_token;
      rememberSession();
      const candidateURL = new URL(result.candidate_url || '/interview#session=' + encodeURIComponent(sessionId) + '&token=' + encodeURIComponent(token), location.origin);
      const hrURL = new URL(result.hr_url || '/hr-review#session=' + encodeURIComponent(sessionId) + '&token=' + encodeURIComponent(result.hr_token), location.origin);
      $('candidateLink').value = candidateURL.href; $('hrLink').value = hrURL.href; $('openHr').href = hrURL.href;
      $('creatorLinks').hidden = false;
      if (result.hr_token) { try { const saved = JSON.parse(sessionStorage.getItem('suit-workbench-hr-links-v1') || '{}'); saved[sessionId] = {token:result.hr_token,candidate_token:result.candidate_token}; sessionStorage.setItem('suit-workbench-hr-links-v1',JSON.stringify(saved)); } catch (_) { /* The separately issued HR link remains visible above. */ } }
      updateWorkbenchLinks();
      history.replaceState(null, '', candidateURL.pathname + candidateURL.search + candidateURL.hash);
      try { sessionStorage.removeItem('suit-create-request-v2'); } catch (_) { /* No durable secret is stored here. */ }
      notice('');
      await refresh(); startPolling();
    } catch (error) { notice(errorMessage(error), 'error'); }
    finally { $('createButton').disabled = false; }
  }
  function requireConsent() { if ($('consent').checked) { try { sessionStorage.setItem('suit-consent-' + sessionId,'yes'); } catch (_) {} return true; } notice('请先阅读并勾选数据使用说明，再开始录制或提交。', 'warning'); $('consent').focus(); return false; }
  function stopSpeech() {
    speechGeneration++;
    if (window.speechSynthesis) window.speechSynthesis.cancel();
    speaking = false; $('speakQuestion').textContent = '朗读题目'; $('speechStatus').textContent = '';
  }
  function speakQuestion() {
    if (!window.speechSynthesis || !window.SpeechSynthesisUtterance || recording || busy) return;
    if (speaking) { stopSpeech(); return; }
    const generation=++speechGeneration;
    window.speechSynthesis.cancel();
    const speech = new SpeechSynthesisUtterance($('questionText').textContent);
    speech.lang = 'zh-CN'; speech.rate = 1;
    const voices = window.speechSynthesis.getVoices();
    speech.voice = voices.find(voice => /^zh/i.test(voice.lang) && voice.localService) || voices.find(voice => /^zh/i.test(voice.lang)) || null;
    speaking = true; $('speakQuestion').textContent = '停止朗读'; $('speechStatus').textContent = '正在朗读题目；开始录制时会自动停止。';
    const done = () => { if(generation!==speechGeneration)return; speaking=false; $('speakQuestion').textContent='朗读题目'; $('speechStatus').textContent=''; };
    speech.onend=done; speech.onerror=done; window.speechSynthesis.speak(speech);
  }
  function stopMeter() {
    if (meterFrame !== null && window.cancelAnimationFrame) cancelAnimationFrame(meterFrame);
    meterFrame=null;
    if (audioContext) { try { const closing=audioContext.close(); if(closing?.catch)closing.catch(()=>{}); } catch (_) {} }
    audioContext=null; $('micLevel').style.width='0%'; $('micMeter').setAttribute('aria-valuenow','0');
  }
  function startMeter() {
    stopMeter();
    const AudioContextClass=window.AudioContext || window.webkitAudioContext;
    if (!AudioContextClass || !window.requestAnimationFrame || !stream) { $('micStatus').textContent=practiceCompleted?'本场试录已完成，设备仍保持连接':'音量条不可用，可选试录回放确认声音'; return; }
    try {
      audioContext=new AudioContextClass();
      const source=audioContext.createMediaStreamSource(stream), analyser=audioContext.createAnalyser();
      analyser.fftSize=512; source.connect(analyser);
      const values=new Uint8Array(analyser.fftSize);
      const tick=()=> {
        if (!stream || !audioContext) return;
        analyser.getByteTimeDomainData(values);
        let squared=0; for (const value of values) squared+=((value-128)/128)**2;
        const level=Math.min(100,Math.round(Math.sqrt(squared/values.length)*350));
        $('micLevel').style.width=level+'%'; $('micMeter').setAttribute('aria-valuenow',String(level));
        if(level>4)audioObserved=true;
        $('micStatus').textContent=level>4 ? '已收到声音 · 本场设备连接有效' : audioObserved ? '设备仍已连接 · 当前输入较安静' : '请说一句话，确认音量条有变化';
        meterFrame=requestAnimationFrame(tick);
      };
      tick();
    } catch (_) { stopMeter(); $('micStatus').textContent='音量预览不可用，请试录并回放检查声音'; }
  }
  function stopTracks() {
    deviceRequestSerial++;
    stopMeter();
    const previous=stream; stream=null;
    if(previous) previous.getTracks().forEach(track=>track.stop());
    practiceCompleted=false;audioObserved=false;
    $('deviceStatus').textContent='设备已关闭'; $('micStatus').textContent='重新开启预览后可再次录制';
  }
  function deviceStreamLive(target=stream) {
    if(!target || target.active===false)return false;
    const tracks=target.getTracks();
    return tracks.some(track=>track.kind==='audio') && tracks.some(track=>track.kind==='video') && tracks.every(track=>track.readyState==='live'&&track.enabled!==false);
  }
  function bindDeviceStream(target) {
    for(const track of target.getTracks())if(track.addEventListener)track.addEventListener('ended',()=>{
      if(stream!==target)return;
      if(recording){recordingFault=true;submitAfterStop=false;stopRecording();}
      stopTracks();notice('设备连接已中断，需要重新开启检查。已录片段仍会保留，请回放核对后再提交。','warning');updateControls();
    },{once:true});
  }
  async function showLivePreview(reused=false) {
    if(!deviceStreamLive())return;
    const target=stream;
    $('preview').pause();$('preview').removeAttribute('src');$('preview').srcObject=target;$('preview').muted=true;$('preview').controls=false;$('videoPlaceholder').hidden=true;
    try {await $('preview').play();}catch(_){if(stream===target)$('deviceStatus').textContent='设备保持连接 · 请点击“返回设备预览”重试画面';return;}
    if(stream!==target || pageClosed)return;
    $('deviceStatus').textContent=reused?'本场设备保持连接 · 尚未开始本题录制':'设备预览中 · 尚未正式录制';
    if(!audioContext)startMeter();
  }
  async function previewDevices() {
    if (!requireConsent() || recording || busy || deviceOpening) return;
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) { notice('浏览器不支持此处录制，请通过本机 localhost 或 HTTPS 使用 Chrome / Edge，也可上传已有音视频。','warning'); return; }
    deviceOpening=true; updateControls();
    try {
      const reused=deviceStreamLive();
      if(!reused){
        stopTracks();const request=++deviceRequestSerial;
        const opened=await navigator.mediaDevices.getUserMedia({video:{width:{ideal:1280},height:{ideal:720}},audio:true});
        if(pageClosed||request!==deviceRequestSerial){opened.getTracks().forEach(track=>track.stop());return;}
        if(!deviceStreamLive(opened)){opened.getTracks().forEach(track=>track.stop());throw new Error('摄像头或麦克风轨道不可用，请重新连接设备。');}
        stream=opened;bindDeviceStream(opened);
      }
      await showLivePreview(reused);
      notice(sessionStarted ? '本场设备保持连接，当前未录制。点击“开始回答”录制本题。' : '摄像头与麦克风已连接。请确认画面与声音，再点击“开始面试”查看第一题；不会自动录制。');
    } catch(error) {
      stopTracks();
      notice(error.name==='NotAllowedError' ? '摄像头或麦克风权限未开放。请在浏览器地址栏授权后重试。' : error.name==='NotFoundError' ? '没有找到摄像头或麦克风，可连接设备后重试，或上传已有音视频。' : '无法开启设备：'+errorMessage(error),'error');
    } finally { deviceOpening=false; updateControls(); }
  }
  function stopRecording() {
    if(recorder?.state==='recording') recorder.stop();
    clearInterval(timerId);
  }
  async function resetMedia() {
    if(!draft || draft.attempted || busy || recording) return;
    try { await removeRecordingParts(draft); } catch (_) {}
    draft.blob=null; draft.name=''; draft.attempt_id=uuid(); draft.idempotency_key=uuid();
    delete draft.upload_id; delete draft.upload_sha256; delete draft.upload_init_key;
    delete draft.recording_incomplete; delete draft.recording_mime; draft.recording_parts=[];
    $('recoveredMediaConfirm').checked=false; await saveDraft(); setPreview(null);if(deviceStreamLive()&&selectedMode()==='media')await showLivePreview(true);updateControls();
  }
  async function restartSubmission() {
    if(!draft||busy||recording)return;
    const original=draft;busy=true;updateControls();
    try {
      await render(await api(endpoint()));
      if(state?.answers?.some(answer=>answer.round_id===original.round_id&&answer.current!==false)) { notice('服务端已经保存本轮回答，已恢复保存结果。');return; }
      if(draft!==original||state?.current_round?.round_id!==original.round_id) { notice('本场进度已经更新，请按当前题目继续。');return; }
      draft.attempted=false;draft.attempt_id=uuid();draft.idempotency_key=uuid();
      delete draft.upload_id;delete draft.upload_sha256;delete draft.upload_init_key;
      await saveDraft();$('restartSubmission').hidden=true;$('pendingNotice').hidden=true;$('uploadStatus').textContent='';
      notice('已确认本轮尚未正式保存。现在可以修改文字、替换文件或重新录制；再次提交将使用新的提交编号。');
    } catch(error) { notice('尚不能确认服务器状态，暂时保留原提交。'+errorMessage(error),'error'); }
    finally { busy=false;updateControls(); }
  }
  async function beginRecording(purpose='answer') {
    if(!draft || busy || recording || !requireConsent()) return;
    if(!deviceStreamLive()) { await previewDevices(); if(!deviceStreamLive()) return; }
    stopSpeech();
    if(practiceUrl) { URL.revokeObjectURL(practiceUrl); practiceUrl=''; }
    if(purpose==='answer' && draft.blob) await resetMedia();
    const target=draft, mimeType=['video/webm;codecs=vp9,opus','video/webm;codecs=vp8,opus','video/webm','video/mp4'].find(type=>MediaRecorder.isTypeSupported(type));
    try {
      $('preview').pause(); $('preview').removeAttribute('src'); $('preview').srcObject=stream; $('preview').muted=true; $('preview').controls=false;
      await $('preview').play();
      const recorderOptions={videoBitsPerSecond:1600000,audioBitsPerSecond:128000}; if(mimeType)recorderOptions.mimeType=mimeType;
      recorder=new MediaRecorder(stream,recorderOptions);
      const activeRecorder=recorder; chunks=[]; recordingWrites=Promise.resolve(); submitAfterStop=false; recordingPurpose=purpose; recordingFault=false;
      if(purpose==='answer') { target.recording_parts=[]; target.recording_mime=activeRecorder.mimeType||mimeType||'video/webm'; target.recording_incomplete=true; }
      recorder.ondataavailable=event=> {
        if(!event.data.size) return;
        const index=chunks.length; chunks.push(event.data);
        if(purpose==='answer') recordingWrites=recordingWrites.then(()=>recordingPart(target,event.data,index)).catch(()=> {
          $('draftStatus').textContent='浏览器分片保存失败，请勿关闭页面';
        });
      };
      recorder.onerror=()=> { recordingFault=true; submitAfterStop=false; notice('录制出现错误。将保留已录内容，请回放检查或重新录制。','error'); stopRecording(); };
      recorder.onstop=async()=> {
        const autoSubmit=submitAfterStop, blob=new Blob(chunks,{type:activeRecorder.mimeType||'video/webm'});
        clearInterval(timerId); recording=false; recordingPurpose=''; submitAfterStop=false;
        $('recordingBadge').hidden=true; $('stopButton').hidden=true;
        if(purpose==='practice') {
          if(practiceUrl) URL.revokeObjectURL(practiceUrl);
          $('preview').srcObject=null; $('preview').muted=false; $('preview').controls=true;
          if(blob.size) { practiceUrl=URL.createObjectURL(blob); $('preview').src=practiceUrl; }
          practiceCompleted=Boolean(blob.size)&&!recordingFault;
          $('practiceStatus').hidden=false; $('practiceStatus').textContent=blob.size ? '试录完成。请播放检查画面与声音；试录不上传、不计入正式回答。准备好后点击“开始回答”。' : '试录没有有效内容，请重新检查设备。';
          $('deviceStatus').textContent=deviceStreamLive()?'正在回看试录 · 本场设备继续连接':'正在回看已保留试录 · 设备需重新开启';
        } else {
          busy=true; updateControls();
          await recordingWrites;
          if(blob.size) {
            target.blob=blob; target.name='回答-'+target.round_id+(blob.type.includes('mp4')?'.mp4':'.webm'); target.recording_incomplete=recordingFault;
            const saved=await saveDraft();
            if(saved) { try { await removeRecordingParts(target); await saveDraft(); } catch(_){} }
            setPreview(blob,target.name);
          } else { target.recording_incomplete=false; setPreview(null); notice('没有录到有效内容，请重新录制或上传文件。','warning'); }
          $('deviceStatus').textContent=deviceStreamLive()?'本题录制已结束 · 本场设备继续连接':'本题录制已结束 · 设备需重新开启';busy=false;
        }
        updateControls();
        if(purpose==='answer'&&autoSubmit&&blob.size) await submitAnswer();
      };
      recorder.start(1000); recording=true; startedAt=Date.now();
      $('timer').textContent='00:00'; $('recordingKind').textContent=purpose==='practice'?'设备试录':'正式回答';
      $('recordingBadge').hidden=false; $('stopButton').hidden=false; $('stopButton').textContent=purpose==='practice'?'结束试录':'结束并回看';
      $('practiceStatus').hidden=true; updateControls();
      notice(purpose==='practice'?'试录 5 秒。请说一句话，随后回放检查。':'正在正式录制。回答完毕后点击“完成并提交”，或先结束回看。');
      timerId=setInterval(()=> {
        const seconds=Math.floor((Date.now()-startedAt)/1000);
        $('timer').textContent=String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');
        if(seconds>=(purpose==='practice'?5:180)) { stopRecording(); if(purpose==='answer') notice('已达到 3 分钟上限。请回放检查后提交。'); }
      },250);
    } catch(error) {
      recording=false; recordingPurpose=''; stopTracks(); notice('无法开始录制：'+errorMessage(error),'error'); updateControls();
    }
  }
  async function chooseFile(event) {
    const file = event.target.files[0]; if (!file || !draft) return;
    if (!requireConsent()) { event.target.value = ''; return; }
    if (file.size > 100 * 1024 * 1024) { notice('文件超过 100 MB，请压缩或截取后上传。','error'); event.target.value = ''; return; }
    await resetMedia(); stopTracks(); stopSpeech(); draft.blob = file; draft.name = file.name;
    await saveDraft(); setPreview(file,file.name); updateControls(); notice('文件已加入本轮草稿。请先回放检查，再提交。'); event.target.value = '';
  }
  async function mainAction() {
    if(busy || deviceOpening) return;
    if(!sessionStarted) { if(!requireConsent())return; sessionStarted=true; try { sessionStorage.setItem('suit-started-'+sessionId,'yes'); } catch (_) {} updateControls();focusQuestion(); if($('autoRead').checked)speakQuestion(); return; }
    if(recording) { if(recordingPurpose==='answer') { submitAfterStop=true; stopRecording(); } return; }
    if(selectedMode()==='media' && !draft?.blob && !draft?.attempted) return beginRecording('answer');
    return submitAnswer();
  }
  async function submitAnswer() {
    if (!draft || !requireConsent() || busy || recording) return;
    clearTimeout(autosaveId);
    if (!draft.attempted) { draft.text = $('transcript').value.trim(); draft.mode = selectedMode(); }
    const useMedia = draft.mode === 'media' && draft.blob;
    if (!draft.text && !useMedia) { notice('请先录制或上传音视频，或输入文字回答。', 'warning'); return; }
    if(useMedia && draft.recording_incomplete && !$('recoveredMediaConfirm').checked) { notice('这段录制曾中断，请先回放并确认使用保留片段，或重新录制。','warning'); $('recoveredMediaConfirm').focus(); return; }
    stopSpeech();
    const submittingDraft=draft;
    busy = true; draft.attempted = true; await saveDraft(); updateControls();
    $('uploadStatus').textContent = '正在保存回答；请保持页面开启。';
    try {
      let response;
      if(useMedia) {
        if(!window.InterviewUploads) throw new Error('上传组件尚未载入，请保留草稿并刷新后重试。');
        response=await window.InterviewUploads.upload({endpoint:endpoint(),token,draft,persist:saveDraft,onProgress:progress=> {
          $('uploadProgress').hidden=false; $('uploadProgress').value=progress.total ? Math.round(progress.sent/progress.total*100) : 0;
          $('uploadStatus').textContent=progress.phase==='checking' ? '正在检查文件…' : progress.phase==='finalizing' ? '上传完成，正在校验并保存回答…' : (progress.resumed?'已恢复上传，':'正在上传，')+Math.round(progress.sent/progress.total*100)+'% · 网络中断后可继续';
        }});
      } else {
        const form=new FormData(); form.append('round_id',draft.round_id); form.append('attempt_id',draft.attempt_id); form.append('idempotency_key',draft.idempotency_key); form.append('language','zh');
        if(draft.text) form.append('transcript',draft.text);
        response=await api(endpoint('/answers'),{method:'POST',body:form});
      }
      notice('回答已保存，正在准备下一题…');
      $('uploadStatus').textContent = '';
      if (response.state) await render(response.state); else await refresh();
    } catch (error) {
      await refresh(true);
      if (state?.answers?.some(answer=>answer.round_id===submittingDraft.round_id&&answer.current!==false)) notice('服务端已保存本轮回答，已恢复最新状态。');
      else {
        if(draft===submittingDraft) $('restartSubmission').hidden=false;
        $('uploadStatus').textContent = '尚未确认提交成功；草稿与文件仍保留。';
        notice(errorMessage(error) + (draft?.attempted ? '请点击重试，系统会使用同一提交编号。' : ''),'error');
      }
    } finally { busy = false; $('uploadProgress').hidden=true; updateControls(); queueAdvance(); }
  }
  function queueAdvance(delay=0) {
    if(pageClosed || busy || advancePromise || advanceError || !currentAnswer())return;
    if(advanceTimer!=null)return;
    advanceTimer=setTimeout(()=>{advanceTimer=null;nextRound();},delay);
  }
  function nextRound() {
    if(advancePromise)return advancePromise;
    if(pageClosed || busy || !state?.current_round || !currentAnswer())return Promise.resolve();
    clearTimeout(advanceTimer);advanceTimer=null;advanceError='';advanceErrorRound='';
    const roundId=state.current_round.round_id;
    advancePromise=advanceRound(roundId).finally(()=>{advancePromise=null;queueAdvance(1500);});
    return advancePromise;
  }
  async function advanceRound(roundId) {
    busy = true; nextWaiting=true; nextWaitText=''; stopSpeech(); updateControls();
    const keyName = 'suit-next-' + sessionId + '-' + roundId;
    let key;
    try { key = sessionStorage.getItem(keyName) || uuid(); sessionStorage.setItem(keyName,key); } catch (_) { key = uuid(); }
    try {
      const requestBody = JSON.stringify({round_id:roundId,idempotency_key:key});
      let result;
      for (let attempt = 0; attempt < 75 && !pageClosed; attempt++) {
        result = await api(endpoint('/next'), {method:'POST',body:requestBody});
        if(pageClosed)return;
        if (!result.decision_pending) break;
        updateControls();notice('回答已保存，正在准备下一题…');
        await new Promise(resolve => setTimeout(resolve,1000));
      }
      if(pageClosed || !result)return;
      await render(result.state || result);
      notice(result.decision_pending ? '回答已保存，正在准备下一题…' : '');
    }
    catch (error) { if(!pageClosed){advanceError='连接暂时中断。回答已保存，请重试连接。';advanceErrorRound=roundId;notice(errorMessage(error),'error');await refresh(true);} }
    finally { busy = false; nextWaiting=false; nextWaitText=''; updateControls(); }
  }
  async function correctTranscript() {
    const answer = currentAnswer(); if (!answer || busy) return;
    const text = $('correctedText').value.trim(), reason = $('correctionReason').value.trim();
    if (!text || !reason) { notice('请填写校正后的文字与校正原因。','warning'); return; }
    busy = true; updateControls();
    const data = {version:answer.transcript_revision,text,reason};
    const signature = JSON.stringify(data);
    if (correctionKey?.signature !== signature) correctionKey = {signature,id:uuid()};
    try {
      const result = await api(endpoint('/answers/' + encodeURIComponent(answer.answer_id) + '/transcript'), {method:'POST',body:JSON.stringify({...data,idempotency_key:correctionKey.id})});
      $('correctionDetails').open = false; $('correctionReason').value = ''; correctionKey = null;
      if (result.state) await render(result.state); else await refresh(); notice('校正已保存，原始文字及修订记录已保留。');
    } catch (error) { notice(error.status === 409 ? '转写版本已更新。请先复制当前校正内容，刷新页面后基于最新版本重新校正。' : errorMessage(error),'error'); }
    finally { busy = false; updateControls(); }
  }
  function startPolling() { clearInterval(pollId); pollId = setInterval(() => { if (!busy && !recording && document.visibilityState === 'visible') refresh(true); }, 4500); }
  $('createForm').addEventListener('submit',createInterview);
  $('createFormMode').addEventListener('change',updatePlanLabel);updatePlanLabel();
  $('recordButton').addEventListener('click',previewDevices); $('trialButton').addEventListener('click',()=>beginRecording('practice'));
  $('stopButton').addEventListener('click',()=>{submitAfterStop=false;stopRecording();});
  $('closeDevice').addEventListener('click',()=>{stopTracks();setPreview(draft?.blob,draft?.name);updateControls();});
  $('submittedCloseDevice').addEventListener('click',()=>{if(recording||deviceOpening)return;stopTracks();setPreview(draft?.blob,draft?.name);updateControls();});
  $('speakQuestion').addEventListener('click',speakQuestion);
  $('autoRead').addEventListener('change',()=>{if($('autoRead').checked)speakQuestion();else stopSpeech();});
  $('mediaFile').addEventListener('change',chooseFile); $('submitButton').addEventListener('click',mainAction); $('nextButton').addEventListener('click',nextRound); $('saveCorrection').addEventListener('click',correctTranscript);
  $('clearMedia').addEventListener('click',resetMedia); $('redoRecording').addEventListener('click',async()=>{await resetMedia();await previewDevices();});
  $('restartSubmission').addEventListener('click',restartSubmission);
  $('transcript').addEventListener('input', () => { if (!draft || draft.attempted) return; draft.text = $('transcript').value; $('draftStatus').textContent = '正在保存草稿…'; clearTimeout(autosaveId); autosaveId = setTimeout(saveDraft,350); });
  document.querySelectorAll('input[name="mode"]').forEach(input => input.addEventListener('change', async () => { if(selectedMode()==='text')stopTracks();try { sessionStorage.setItem('suit-mode-'+sessionId,selectedMode()); } catch (_) {} updateMode(); if (draft) { draft.mode = selectedMode(); await saveDraft(); } updateControls(); }));
  document.querySelectorAll('[data-copy]').forEach(button => button.addEventListener('click', async () => { const field = $(button.dataset.copy); try { await navigator.clipboard.writeText(field.value); button.textContent = '已复制'; setTimeout(() => button.textContent = '复制',1800); } catch (_) { field.focus(); field.select(); notice('请按 Ctrl+C 复制已选中的链接。'); } }));
  window.addEventListener('beforeunload', event => { if (recording || busy) { event.preventDefault(); event.returnValue = ''; } });
  window.addEventListener('pagehide', () => { pageClosed=true;clearTimeout(advanceTimer);advanceTimer=null;stopSpeech();if(recording){recordingFault=true;submitAfterStop=false;stopRecording();}stopTracks(); clearInterval(timerId); clearInterval(pollId); if (previewUrl) URL.revokeObjectURL(previewUrl); if(practiceUrl)URL.revokeObjectURL(practiceUrl); });
  window.addEventListener('pageshow',()=>{pageClosed=false;updateControls();if(sessionId&&token){startPolling();queueAdvance();}});
  window.navigator?.mediaDevices?.addEventListener?.('devicechange',()=>{if(stream&&!deviceStreamLive()){if(recording){recordingFault=true;submitAfterStop=false;stopRecording();}stopTracks();notice('设备状态已改变，请重新开启检查。已录片段仍会保留。','warning');updateControls();}});
  window.addEventListener('hashchange', () => {
    const next = new URLSearchParams(location.hash.slice(1));
    if ((next.get('session') || '') === sessionId && (next.get('token') || '') === token) return;
    if (recording || busy || deviceOpening) {
      history.replaceState(null,'','/interview#'+new URLSearchParams({session:sessionId,token}).toString());
      notice('录制或提交仍在进行，已保留本场。请先结束录制并确认草稿保存后，再切换面试链接。','warning'); return;
    }
    clearTimeout(autosaveId);
    Promise.resolve(draft ? saveDraft() : true).then(saved => {
      if (saved) location.reload();
      else history.replaceState(null,'','/interview#'+new URLSearchParams({session:sessionId,token}).toString());
    });
  });
  updateWorkbenchLinks();
  try { $('consent').checked=sessionStorage.getItem('suit-consent-'+sessionId)==='yes'; } catch (_) {}
  try { const mode=sessionStorage.getItem('suit-mode-'+sessionId);if(mode==='text'||mode==='media')document.querySelectorAll('input[name="mode"]').forEach(input=>input.checked=input.value===mode); } catch (_) {}
  if (sessionId && token) { $('welcome').hidden = true; notice('正在恢复面试进度…'); refresh().then(() => { if (state) notice(''); }); startPolling(); }
  else if (sessionId || token) { notice('面试链接不完整，请使用包含完整访问凭证的候选人链接。','error'); $('welcome').hidden = true; }
})();
