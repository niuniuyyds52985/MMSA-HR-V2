(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const ACCESS_KEY = 'suit-workbench-access-v1', HR_KEY = 'suit-workbench-hr-links-v1';
  const params = new URLSearchParams(location.hash.slice(1));
  const readStorage = key => { try { return sessionStorage.getItem(key) || ''; } catch (_) { return ''; } };
  const saveStorage = (key,value) => { try { sessionStorage.setItem(key,value); } catch (_) { /* Access remains usable until this page closes. */ } };
  let access = params.get('access') || readStorage(ACCESS_KEY), selectedId = params.get('session') || '';
  let sessions = [], imported = {}, filter = 'all', loading = false, polling = null;
  const technicalPrefixes = ['合成验收-', 'SIM-MCP-QA-STDIO-', '开发验证｜产品升级', '开发测试：Suit仓库样例媒体', '接口联调：合成文字，不是真实候选人', '交付验收｜合成完整流程（非真人评定）'];
  const isTechnical = session => technicalPrefixes.some(prefix => String(session.candidate_ref || '').startsWith(prefix));
  const hasAnswers = session => Number(session.answer_count) > 0;
  function recentId(key) { let raw = ''; try { raw = localStorage.getItem(key) || ''; } catch (_) { /* Session storage remains available in restricted browsers. */ } try { return JSON.parse(raw || readStorage(key) || '{}').session_id || ''; } catch (_) { return ''; } }
  function rememberSelection(session) {
    if (!session || isTechnical(session)) return;
    const value = JSON.stringify({session_id:session.session_id,updated_at:Date.now()});
    for (const key of ['suit-recent-interview-v1', ...(hasAnswers(session) ? ['suit-recent-answered-interview-v1'] : [])]) {
      try { localStorage.setItem(key,value); } catch (_) { saveStorage(key,value); }
    }
  }
  const pendingAnalysis = new Set(), analysisKeys = new Map();
  try { const saved = JSON.parse(readStorage(HR_KEY) || '{}'); if (saved && typeof saved === 'object' && !Array.isArray(saved)) imported = saved; } catch (_) { /* Ignore invalid local storage. */ }
  if (access) saveStorage(ACCESS_KEY,access);
  if (params.has('access')) history.replaceState(null,'','/workbench' + (selectedId ? '#session=' + encodeURIComponent(selectedId) : ''));
  const element = (tag,className,text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
  const uuid = () => crypto.randomUUID ? crypto.randomUUID() : Date.now().toString(36) + '-' + Array.from(crypto.getRandomValues(new Uint8Array(16)),n => n.toString(16).padStart(2,'0')).join('');
  const finished = session => ['interview_finished','completed','complete'].includes(session.status);
  const isProcessing = session => pendingAnalysis.has(session.session_id) || ['queued','running','processing'].includes(session.content_status);
  const category = session => session.review_status === 'reviewed' ? 'reviewed' : session.review_status === 'stale' || session.score_status === 'complete' || session.content_status === 'succeeded' || Number(session.content_jobs?.succeeded) > 0 || session.has_model_draft ? 'draft' : 'pending';
  const canAnalyze = session => Boolean(access) && finished(session) && !isProcessing(session) && Number(session.answer_count) > 0 && (['blocked','failed','not_requested'].includes(session.content_status) || Number(session.content_jobs?.not_requested) > 0);
  const categoryName = session => ({pending:'待处理',draft:'复核草稿',reviewed:'已人工复核'})[category(session)];
  function notice(message,kind = '') { $('notice').textContent = message; $('notice').className = 'notice' + (kind ? ' ' + kind : ''); $('notice').hidden = !message; }
  function time(value) { const date = new Date(value); return !value || Number.isNaN(date.getTime()) ? '时间未记录' : date.toLocaleString('zh-CN',{hour12:false,year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}); }
  function sessionName(session) { return session.candidate_ref || '面试 ' + time(session.created_at) + ' · ' + session.session_id.slice(0,12); }
  function statusText(session) {
    if (!hasAnswers(session)) return '本场尚无已提交回答，请先继续候选人作答';
    if (session.review_status === 'stale') return '回答已更新，原复核需重新确认';
    if (isProcessing(session)) return '报告正在逐步更新，可先查看已完成内容';
    if (session.review_status === 'reviewed') return '人工评分和证据已保存';
    if (category(session) === 'draft') return ['blocked','failed'].includes(session.content_status) || Number(session.content_jobs?.not_requested) > 0 ? '部分内容草稿可用，仍有回答待分析和人工核验' : '内容证据已就绪，可以查看整场报告';
    if (!finished(session)) return '候选人面试尚未完成';
    return ({blocked:'模型分析受阻，可检查配置后生成草稿',failed:'内容分析未完成，可重试或直接人工复核',not_requested:'回答已保存，尚未生成评分草稿'})[session.content_status] || '回答已保存，等待内容分析或人工复核';
  }
  async function api(path,options = {},credential = access) {
    const headers = new Headers(options.headers || {}); if (credential) headers.set('Authorization','Bearer ' + credential); if (options.body) headers.set('Content-Type','application/json');
    let response; try { response = await fetch(path,{cache:'no-store',...options,headers}); } catch (_) { throw new Error('无法连接本机服务，请确认面试服务正在运行后重试。'); }
    let data; try { data = await response.json(); } catch (_) { data = {}; }
    if (!response.ok) { const error = new Error(response.status === 401 || response.status === 403 ? '工作台授权不可用。请从本机启动器重新打开，或导入已有 HR 专用链接。' : typeof data.detail === 'string' ? data.detail : data.detail?.message || '请求未完成，请刷新后重试。'); error.status = response.status; throw error; }
    return data;
  }
  function roleURL(path,id,credential) { return path + '#session=' + encodeURIComponent(id) + '&token=' + encodeURIComponent(credential); }
  function allowedURL(value,path) { const url = new URL(value,location.origin); if (url.origin !== location.origin || url.pathname !== path) throw new Error('角色入口无效，请刷新工作台后重试。'); return url.pathname + url.hash; }
  async function openRole(session,role,button) {
    if (role !== 'candidate' && !hasAnswers(session)) { notice('本场尚未提交回答，请先继续候选人作答。','warning'); return; }
    button.disabled = true;
    try {
      let destination;
      if (access) { const links = await api('/v1/workbench/sessions/' + encodeURIComponent(session.session_id) + '/open',{method:'POST'}); destination = allowedURL(links[role + '_url'],{candidate:'/interview',hr:'/hr-review',report:'/interview-report',research:'/research'}[role]); }
      else if (role === 'candidate' && imported[session.session_id]?.candidate_token) destination = roleURL('/interview',session.session_id,imported[session.session_id].candidate_token);
      else if (imported[session.session_id]?.token && role !== 'candidate') destination = roleURL({hr:'/hr-review',report:'/interview-report',research:'/research'}[role],session.session_id,imported[session.session_id].token);
      else throw new Error('此操作需要独立工作台授权，请使用本机启动器打开工作台。');
      location.assign(destination);
    } catch (error) { notice(error.message,'error'); button.disabled = false; }
  }
  function action(label,callback,className = '') { const button = element('button',className,label); button.type = 'button'; button.addEventListener('click',() => callback(button)); return button; }
  function selectSession(id,scroll = true) { selectedId = id; rememberSelection(sessions.find(session=>session.session_id===id)); history.replaceState(null,'','/workbench#session=' + encodeURIComponent(id)); render(); if (scroll) $('selectedSession').scrollIntoView({behavior:'smooth',block:'start'}); }
  async function analyze(session,button) {
    if (!access || isProcessing(session)) return;
    if (!analysisKeys.has(session.session_id)) analysisKeys.set(session.session_id,readStorage('suit-analysis-key-' + session.session_id) || uuid());
    const key = analysisKeys.get(session.session_id); saveStorage('suit-analysis-key-' + session.session_id,key);
    pendingAnalysis.add(session.session_id); button.disabled = true; render();
    notice('已请求生成本场评分草稿。内容分析完成后，仍需 HR 核对原文与评分理由。');
    try {
      await api('/v1/workbench/sessions/' + encodeURIComponent(session.session_id) + '/analyze',{method:'POST',body:JSON.stringify({idempotency_key:key,reason:'操作员在面试工作台明确点击生成本场内容评分草稿，供 HR 核验。'})});
      analysisKeys.delete(session.session_id); try { sessionStorage.removeItem('suit-analysis-key-' + session.session_id); } catch (_) {}
      pendingAnalysis.delete(session.session_id); await refresh(true);
    } catch (error) { pendingAnalysis.delete(session.session_id); notice(error.message + ' 如未确认结果，请刷新状态；再次操作会复用本次请求编号。','error'); render(); }
  }
  function appendMeta(node,session) { const meta = element('div','session-meta'); meta.append(element('span','','档案 ' + session.session_id.slice(0,12)),element('span','','创建于 ' + time(session.created_at)),element('span','',(session.main_answer_count ?? session.answer_count ?? 0) + ' 道主问已答'),element('span','',finished(session) ? '面试已完成' : '面试进行中'),element('span','',session.form_mode==='full'?'完整覆盖 · 8 道主问':session.form_mode==='quick'?'快速体验 · 3 道主问':'历史场次 · 按原题组')); if(isTechnical(session))meta.append(element('span','technical-badge','开发验证档案')); node.append(meta); }
  function makeActions(session,compact = false) {
    const actions = element('div',compact ? 'session-actions' : 'actions');
    if(compact&&hasAnswers(session)){actions.append(action('查看整场报告 →',button=>openRole(session,'report',button),'primary'));return actions;}
    if ((!compact || !hasAnswers(session)) && (access || imported[session.session_id]?.candidate_token) && !finished(session)) actions.append(action('继续候选人面试 →',button => openRole(session,'candidate',button),'primary'));
    if (!compact && canAnalyze(session)) actions.append(action('生成评分草稿',button => analyze(session,button),category(session) === 'pending' ? 'primary' : ''));
    if (!compact && isProcessing(session)) { const wait = action('内容分析处理中…',() => {}); wait.disabled = true; actions.append(wait); }
    for(const [role,label,style] of [['report','查看整场报告','primary'],['hr','逐题复核','subtle'],['research','高级研究','subtle']]) {
      const button=action(label,button=>openRole(session,role,button),style); button.disabled=!hasAnswers(session);
      if(button.disabled)button.title='本场尚无已提交回答，请先完成至少一道题。';actions.append(button);
    }
    if(!hasAnswers(session))actions.append(element('span','session-empty-note',access?'提交回答后可复核和研究。':'请使用本场候选人链接先提交回答。'));
    if (compact) actions.append(action('查看下一步 ↑',() => selectSession(session.session_id),'view-session'));
    return actions;
  }
  function renderSelected() {
    const card = $('selectedSession'), session = sessions.find(item => item.session_id === selectedId); card.replaceChildren(); card.hidden = !session; if (!session) return;
    const header = element('div','section-heading'), title = element('div'); title.append(element('div','next-kicker','当前面试 · 下一步')); const name = element('h2','',sessionName(session)); name.id = 'selectedTitle'; title.append(name); header.append(title,element('span','pill ' + category(session),categoryName(session))); card.append(header); appendMeta(card,session);
    card.append(element('p','next-description',statusText(session) + '。' + (session.review_status === 'reviewed' ? '查看报告可核对保存的评分、引用证据和复核记录。' : isProcessing(session) ? '本页会自动刷新处理状态，也可先打开原始回答进行人工核对。' : !finished(session) ? '继续完成本场面试，已提交的回答会保留。' : '直接打开整场报告，查看内容依据、多模态观察和对应原片。需要时再逐题复核。')));
    card.append(makeActions(session));
    if (canAnalyze(session)) card.append(element('p','cloud-note','点击“生成评分草稿”将本场题目和回答文字发送至已配置的百炼服务，仅处理本场面试。生成结果是模型草稿，完成后仍需 HR 复核。'));
    if (!access) card.append(element('p','cloud-note','当前通过已导入的 HR 专用链接访问本场。生成云端评分草稿及其他场次管理，需要从本机启动器打开独立授权的工作台。'));
  }
  function render() {
    $('accessCard').hidden = Boolean(access) || sessions.length > 0; $('accessLabel').textContent = access ? '本机工作台' : sessions.length ? '已导入 HR 权限' : '待授权'; $('dashboard').hidden = !access && !sessions.length;
    const includeTechnical = $('showTechnical').checked, regular = sessions.filter(item => !isTechnical(item)), counted = includeTechnical ? sessions : regular;
    $('technicalCount').textContent = String(sessions.length-regular.length);
    $('totalCount').textContent = counted.length; for (const [key,id] of [['pending','pendingCount'],['draft','draftCount'],['reviewed','reviewedCount']]) $(id).textContent = counted.filter(item => category(item) === key).length;
    document.querySelectorAll('[data-workbench-home]').forEach(link=>{link.href='/workbench'+(selectedId?'#session='+encodeURIComponent(selectedId):'');});
    renderSelected(); const list = $('sessionList'); list.replaceChildren();
    const search = $('sessionSearch').value.trim().toLocaleLowerCase(), shown = sessions.filter(item => (includeTechnical || Boolean(search) || item.session_id===selectedId || !isTechnical(item)) && (filter === 'all' || category(item) === filter) && ((item.candidate_ref || '') + ' ' + item.session_id).toLocaleLowerCase().includes(search));
    $('listContext').textContent = (access ? '本机已保存的面试' : '已验证的 HR 链接') + ' · 普通档案 ' + regular.length + ' 场，开发验证 ' + (sessions.length-regular.length) + ' 场；当前显示 ' + shown.length + ' 场。' + (!includeTechnical ? '开发验证默认收起，可勾选或搜索查看。' : '');
    if (!shown.length) list.append(element('div','empty-state',sessions.length ? '没有匹配的面试，请调整搜索或筛选条件。' : '还没有面试档案。点击顶部“新建面试”开始，或导入已有 HR 专用链接。'));
    for (const session of shown) { const row = element('article','session-row' + (session.session_id === selectedId ? ' current' : '')), info = element('div'); info.append(action(sessionName(session),() => selectSession(session.session_id),'session-name'),element('p','session-id','档案 ' + session.session_id)); appendMeta(info,session); const state = element('div','session-state'); state.append(element('span','pill ' + (isProcessing(session) ? 'processing' : category(session)),isProcessing(session) ? '内容分析中' : categoryName(session)),element('p','',statusText(session))); row.append(info,state,makeActions(session,true)); list.append(row); }
  }
  function importedSession(report,id) { const session = report.session || report; const answers = (report.answers || []).filter(item => item.current !== false), questions = report.questions || []; return {session_id:id,candidate_ref:session.candidate_ref,created_at:session.created_at,status:session.status,form_mode:report.form_mode||session.form_mode,answer_count:answers.length,main_answer_count:answers.filter(item => !item.parent_question_id).length,content_status:answers.some(item => ['queued','running'].includes(item.content_status)) ? 'running' : questions.some(item => item.model_evaluation) ? 'succeeded' : 'not_requested',has_model_draft:questions.some(item => item.model_evaluation),score_status:report.score_status || 'incomplete',review_status:questions.some(item => item.review_stale) ? 'stale' : questions.length && questions.every(item => item.score_source === 'hr') ? 'reviewed' : 'not_reviewed'}; }
  async function refresh(silent = false) {
    if (loading) return; loading = true; $('refreshSessions').disabled = true;
    try {
      if (access) { const data = await api('/v1/workbench/sessions'); sessions = Array.isArray(data.sessions) ? data.sessions : []; }
      else { const results = await Promise.allSettled(Object.entries(imported).map(async ([id,value]) => importedSession(await api('/v1/interviews/' + encodeURIComponent(id) + '/report',{},value.token),id))); sessions = results.filter(result => result.status === 'fulfilled').map(result => result.value); if (results.some(result => result.status === 'rejected') && !silent) notice('部分已导入链接不可用，请重新导入对应场次的 HR 专用链接。','warning'); }
      if (!selectedId && sessions.length) {
        const regular=sessions.filter(item=>!isTechnical(item)), recent=[recentId('suit-recent-answered-interview-v1'),recentId('suit-recent-interview-v1')];
        const chosen=recent.map(id=>regular.find(item=>item.session_id===id&&hasAnswers(item))).find(Boolean)||regular.find(hasAnswers)||regular[0];
        if(chosen){selectedId=chosen.session_id;rememberSelection(chosen);history.replaceState(null,'','/workbench#session='+encodeURIComponent(selectedId));}
      }
      render();
      if (selectedId && !sessions.some(item => item.session_id === selectedId) && !silent) notice(access ? '未找到当前场次。请从下方档案选择，或确认打开的是保存该面试的本机服务。' : '要打开选中的面试，请导入其 HR 专用链接，或从本机启动器进入工作台。','warning');
    } catch (error) { if (error.status === 401 || error.status === 403) { access = ''; try { sessionStorage.removeItem(ACCESS_KEY); } catch (_) {} sessions = []; render(); } if (!silent || error.status === 401 || error.status === 403) notice(error.message,'error'); }
    finally { loading = false; $('refreshSessions').disabled = false; }
  }
  let creating=false;
  function formMode(){return $('formMode').value==='full'?'full':'quick';}
  function updateCreationMode(){ $('createModeDescription').textContent=formMode()==='full'?'从题库为八因子各安排一道主问，最多追加四道追问。报告逐项区分已体现、部分体现、有相反证据、证据不足与未测项。':'从题库安排 3 个因子、最多追加 1 道追问；未测项目保持未测，只形成局部结论。'; }
  $('formMode').addEventListener('change',updateCreationMode);
  $('newInterviewForm').addEventListener('submit',async event=>{
    event.preventDefault();if(creating)return;creating=true;$('createInterviewButton').disabled=true;notice('正在创建本场面试…');
    const payload={candidate_ref:$('newCandidateRef').value.trim(),form_mode:formMode()},signature=JSON.stringify(payload),key='suit-workbench-create-request-v1';let request;
    try{const saved=JSON.parse(readStorage(key)||'null');request=saved?.signature===signature?saved:{signature,idempotency_key:uuid()};}catch(_){request={signature,idempotency_key:uuid()};}saveStorage(key,JSON.stringify(request));
    try{const result=await api('/v1/interviews',{method:'POST',body:JSON.stringify({...payload,idempotency_key:request.idempotency_key})});
      if(!result.session_id||!result.candidate_token)throw new Error('创建响应缺少场次入口，请重试核对同一次请求。');
      const destination=allowedURL(result.candidate_url||roleURL('/interview',result.session_id,result.candidate_token),'/interview');
      if(result.hr_token){imported[result.session_id]={token:result.hr_token,candidate_token:result.candidate_token};saveStorage(HR_KEY,JSON.stringify(imported));}
      rememberSelection({session_id:result.session_id,candidate_ref:payload.candidate_ref,answer_count:0});try{sessionStorage.removeItem(key);}catch(_){}location.assign(destination);
    }catch(error){notice(error.message+' 可重试；相同编号与模式会复用创建请求。','error');}
    finally{creating=false;$('createInterviewButton').disabled=false;}
  });
  updateCreationMode();
  $('importForm').addEventListener('submit',async event => {
    event.preventDefault(); $('importButton').disabled = true;
    try { const url = new URL($('importLink').value.trim(),location.origin); if (url.origin !== location.origin || !['/hr-review','/interview-report'].includes(url.pathname)) throw new Error('请粘贴本机服务的 HR 复核或报告专用链接，候选人链接不能用于此处。'); const fragment = new URLSearchParams(url.hash.slice(1)), id = fragment.get('session'), credential = fragment.get('token'); if (!id || !credential) throw new Error('链接不完整，请粘贴带会话和访问凭证的完整 HR 专用链接。'); const report = await api('/v1/interviews/' + encodeURIComponent(id) + '/report',{},credential); imported[id] = {token:credential}; saveStorage(HR_KEY,JSON.stringify(imported)); selectedId = id; $('importLink').value = ''; $('importSection').open = false; history.replaceState(null,'','/workbench#session=' + encodeURIComponent(id)); if (access) await refresh(true); else { sessions = sessions.filter(item => item.session_id !== id); sessions.unshift(importedSession(report,id)); render(); } notice('HR 权限已验证，可继续复核或查看本场报告。'); }
    catch (error) { notice(error.status === 401 || error.status === 403 ? '该链接未通过 HR 权限验证，请使用有效的 HR 专用链接。' : error.message,'error'); }
    finally { $('importButton').disabled = false; }
  });
  $('sessionSearch').addEventListener('input',render); $('showTechnical').addEventListener('change',render); $('refreshSessions').addEventListener('click',() => refresh());
  $('filters').addEventListener('click',event => { const button = event.target.closest('[data-filter]'); if (!button) return; filter = button.dataset.filter; $('filters').querySelectorAll('button').forEach(item => { const active = item === button; item.classList.toggle('active',active); item.setAttribute('aria-pressed',String(active)); }); render(); });
  window.addEventListener('hashchange',() => { selectedId = new URLSearchParams(location.hash.slice(1)).get('session') || selectedId; render(); });
  window.addEventListener('pagehide',() => clearInterval(polling));
  render(); if (access || Object.keys(imported).length) refresh(); else { $('importSection').open = true; if (selectedId) notice('本场面试已定位。请从本机启动器打开工作台，或导入其 HR 专用链接继续。'); }
  polling = setInterval(() => { if (!document.hidden && (access || Object.keys(imported).length)) refresh(true); },5000);
})();
