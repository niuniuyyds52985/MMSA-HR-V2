"use strict";
const root = document.querySelector("#workspace");
const notices = document.querySelector("#notices");
const apiRoot = "/v2/api";
const params = new URLSearchParams(location.search);
const token = location.pathname.match(/^\/v2\/c\/([^/]+)\/?$/)?.[1] || params.get("candidate");
const batchId = params.get("batch");
const sessionId = params.get("session");
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const enc = encodeURIComponent;
const $ = (selector, parent = root) => parent.querySelector(selector);
const $$ = (selector, parent = root) => [...parent.querySelectorAll(selector)];
const field = (name, body) => '<label class="field">'+esc(name)+body+'</label>';
const section = (name, body) => '<section class="section"><h2 class="section-title">'+esc(name)+'</h2>'+body+'</section>';
const tag = (name) => '<span class="tag">'+esc(name)+'</span>';
const buttonLink = (label, url) => '<a class="button" href="'+esc(url)+'">'+esc(label)+'</a>';
const answered = (s,q) => {
  if(s.correction_request?.question_ids.includes(q.id) && !s.correction_request.completed_question_ids.includes(q.id))return false;
  const a = s.answers?.[q.id];
  return a?.value != null || (q.kind === "text" && Boolean(a?.text || a?.media));
};
const statusNames = {interview_open:"面试进行中",awaiting_verification:"等待补答",interview_finished:"面试已提交 · 待分析",draft:"分析草稿",reviewed:"已复核",review_changes:"已退回补充",unstarted:"待建面试"};
const stateLabels = ["洋溢","饱满","平和","略微 down","非常不好"];
const sourceNames={local_review:"本地人工录入",local_auto:"本地规则计算",bailian_auto:"百炼 Qwen 内容评分",harness:"千问办公接口",mixed:"多个来源",scripted_demo:"脚本演示",unrecorded:"历史来源未登记"};
const scoringLabels={queued:"等待自动评分",running:"正在转写和生成评分草稿",done:"自动评分草稿已生成",partial:"分析已结束，部分评分待核实",error:"自动评分遇到问题",failed:"自动评分遇到问题",manual_review_required:"补正已保存，等待老师复核"};
function automaticDraft(draft){
  if(!draft)return true;
  const origins=[...(draft.factors||[]).map(f=>f.source||draft.source),...Object.values(draft.provenance||{}).map(p=>p.source)];
  return origins.length>0&&origins.every(s=>["local_auto","bailian_auto"].includes(s));
}
const scoringBusy=state=>state==="queued"||state==="running";
function hrAnalysisStatus(state) {
  const job=state.auto_scoring||{};
  if (!scoringBusy(job.status)) return "";
  const oral=(state.plan||[]).filter(q=>q.kind==="text"&&q.factor_id);
  const ready=oral.filter(q=>String(state.answers?.[q.id]?.text||state.answers?.[q.id]?.transcript||"").trim()).length;
  const analyzed=oral.filter(q=>job.nodes?.["question:"+q.id]?.status==="succeeded").length;
  const rated=oral.filter(q=>state.draft?.factors?.some(f=>f.factor_id===q.factor_id&&f.rating!=null&&!f.verification_gaps?.length)).length;
  const title=job.status==="queued"?"面试已提交，等待后台分析":ready<oral.length?"正在提取录音、转写和声画观测":analyzed<oral.length?"Qwen 正在对照题库评分点分析问答":"正在汇总本场报告";
  return '<strong>'+esc(title)+'</strong><p>问答原话已就绪 '+ready+' / '+oral.length+' · 问答分析已完成 '+analyzed+' / '+oral.length+' · 已取得有效评分 '+rated+' / '+oral.length+'</p>'+
    '<p class="note">尚未完成的因子会随后台分析自动更新；当前显示的分数还不是最终结果。可先查看已完成部分，无需重新交卷。</p>';
}
const shownStatus = item=>item.status==="reviewed"&&(item.analysis_source==="scripted_demo"||item.draft?.source==="scripted_demo"||String(item.reviewer||item.review?.reviewer||"").includes("脚本"))
  ?"模拟复核已完成":item.status==="interview_open"&&item.answered_questions===0?"链接已准备 · 待学生作答":(statusNames[item.status]||item.status);
let candidateState, currentId, device;
let candidatePage = "setup", dirty = false, saving = false;
let sessionMonitor = 0;
function notice(text = "", error = false) {
  notices.hidden = !text;
  notices.className = "notice" + (error ? " alert" : "");
  notices.textContent = text;
}
async function request(path, data, method = "POST") {
  const options = {credentials:"same-origin",cache:"no-store"};
  if (data !== undefined) {
    options.method = method;
    if (data instanceof FormData) options.body = data;
    else { options.headers = {"Content-Type":"application/json"}; options.body = JSON.stringify(data); }
  }
  const response = await fetch(apiRoot + path, options);
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401) throw new Error("请用项目根目录的“启动面试工作台.cmd”打开本机工作台。");
    const problem = new Error(typeof result.detail === "string" ? result.detail : "操作未完成，请检查输入并重试。");
    problem.status = response.status;
    throw problem;
  }
  return result;
}
function onClick(selector, fn, parent = root) {
  const el = $(selector, parent);
  if (!el) return;
  el.addEventListener("click", async event => {
    event.preventDefault();
    if (el.disabled) return;
    el.disabled = true;
    try { notice(); await fn(event); }
    catch (err) { notice(err.message, true); }
    finally { el.disabled = false; }
  });
}
function role(text) { document.querySelector("#role-label").textContent = text; }
function sessionUrl(sid) { return "/v2/?session="+enc(sid); }
function batchUrl(bid) { return "/v2/?batch="+enc(bid); }
function reportUrl(sid, version) {
  return apiRoot+"/sessions/"+enc(sid)+"/report.html"+(version ? "?version="+enc(version):"");
}
function candidateUrl(value) { return "/v2/c/"+enc(value); }
function batchReportUrl(bid) { return apiRoot+"/batches/"+enc(bid)+"/report.html"; }
function head(title, sub, action = "") {
  return '<header class="heading"><div><h1>'+esc(title)+'</h1><p class="meta">'+esc(sub)+'</p></div><div class="row">'+action+'</div></header>';
}
function opts(options, current, empty = "请选择") {
  return '<option value="">'+esc(empty)+'</option>'+options.map(o=>'<option value="'+esc(o.id)+'"'+(o.id===current?" selected":"")+'>'+esc(o.text)+'</option>').join("");
}
async function dashboard() {
  const [result, availability] = await Promise.all([request("/batches"), request("/status")]);
  const demoCount = Number(availability.demo_resume_count) || 0;
  role("HR 面试总控");
  const tiles = result.batches.map(b=>'<a class="tile" href="'+batchUrl(b.id)+'"><strong>'+esc(b.name)+'</strong><span class="meta">'+esc(b.candidate_count ?? 0)+' 位候选人</span></a>').join("");
  const demoAction = demoCount > 0 ? '<button id="open-cohort" class="primary">创建 '+demoCount+' 人模拟批次</button>' : '';
  root.innerHTML = head("HR 面试总控","管理候选人、分发独立作答链接、查看个人报告与整批排名",demoAction) +
    section("面试批次",tiles?'<div class="wall">'+tiles+'</div>':'<p class="empty">创建一个批次后即可导入简历、安排面试。</p>')+
    section("新建批次",'<form id="batch-form" class="toolbar">'+field("批次名称",'<input id="batch-name" required maxlength="100" placeholder="例如：管培生模拟面试一组">')+'<button class="primary" type="submit">创建批次</button></form>'+
      (!demoCount?'<p class="note">创建批次后，可一次选择多份自己的 DOCX 简历导入。</p>':''));
  $("#batch-form").onsubmit = async e => {
    e.preventDefault(); const form=e.currentTarget; const submit=$("button",form); submit.disabled=true;
    try { const b=await request("/batches",{name:$("#batch-name").value.trim()}); location.href=batchUrl(b.id); }
    catch(err){ notice(err.message,true); submit.disabled=false; }
  };
  onClick("#open-cohort",async()=>{
    const current = await request("/status");
    const count = Number(current.demo_resume_count) || 0;
    if(count < 1){await dashboard();throw new Error("本机没有模拟简历，请创建批次并导入自己的 DOCX 简历。");}
    const batch=await request("/batches",{name:"通用管培生 · "+count+"人模拟面试"});
    await request("/batches/"+enc(batch.id)+"/import-demo",{});
    location.href=batchUrl(batch.id);
  });
}
async function showBatch() {
  const url="/batches/"+enc(batchId);
  const [b, availability] = await Promise.all([request(url), request("/status")]);
  const demoCount = Number(availability.demo_resume_count) || 0;
  role("HR 总控 · 批次管理");
  document.title=b.name+" · 面试批次 · 真才实鉴";
  const unstarted=b.candidates.filter(c=>!c.session_id);
  const stats=b.stats||{};
  root.innerHTML=head(b.name,"本批次总控 · "+b.candidates.length+" 位候选人",buttonLink("所有批次","/v2/")+buttonLink("汇总排名 HTML",batchReportUrl(batchId))+'<button id="batch-export">导出整批报告</button>')+
    '<ol class="control-flow" aria-label="面试管理流程"><li><b>1</b><span>管理候选人</span></li><li><b>2</b><span>复制各自作答链接</span></li><li><b>3</b><span>学生完成面试</span></li><li><b>4</b><span>HR 查看报告与排名</span></li></ol>'+
    '<div class="cohort-overview" aria-label="批次进度">'+
      '<span><strong>'+b.candidates.length+'</strong> 位候选人</span><span><strong>'+(stats.finished_count||0)+'</strong> 位已交卷</span>'+
      '<span><strong>'+(stats.ranked_count||0)+'</strong> 位可排名</span></div>'+
    '<details class="batch-import"'+(b.candidates.length?'':' open')+'><summary>导入或补充候选人简历</summary><div class="toolbar">'+field("本机 Word 简历",'<input type="file" id="resumes" multiple accept=".docx">')+'<button id="import-files" class="primary">导入所选简历</button>'+
      (demoCount > 0?'<button id="import-demo">导入 '+demoCount+' 份模拟简历</button>':'')+'</div></details>'+
    section("候选人",'<div class="between">'+field("筛选姓名或院校",'<input id="filter" type="search" placeholder="输入姓名或院校">')+'<div class="row">'+
      (unstarted.length?'<button id="prepare-cohort">为 '+unstarted.length+' 人生成独立链接</button>':'')+'<button id="refresh-batch">刷新进度</button></div></div>'+
      '<p class="note">将每人的作答链接交给对应面试者；HR 在这里查看评分报告和整批排名。当前为本机演示，链接仅能在本机使用。</p>'+
      '<div id="people" class="table-wrap"></div>')+'<section id="correction"></section>';
  async function ensureSession(person) {
    return person.session_id ? request("/sessions/"+enc(person.session_id)) : request(url+"/sessions",{candidate_id:person.id});
  }
  async function copyStudentLink(person) {
    const session=person.student_url ? null : await ensureSession(person);
    const link=location.origin+(person.student_url||candidateUrl(session.token));
    try {
      if(!navigator.clipboard?.writeText)throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(link);
      notice(person.name+"的独立作答链接已复制。");
    } catch { window.prompt("复制"+person.name+"的作答链接",link); }
    if(!person.session_id)await showBatch();
  }
  function scoreCell(person) {
    if(person.display_score==null)return '<span class="meta">待评分</span>';
    const labels={total:"综合分",estimate:"综合估分",partial:"已评分项累计",pending:"待评分"};
    return '<strong>'+esc(Number(person.display_score).toFixed(2))+' / 100</strong><small>'+esc(labels[person.score_kind]||"待评分")+'</small>';
  }
  function drawPeople() {
    const keyword=$("#filter").value.trim().toLowerCase();
    const items=b.candidates.filter(c=>(c.name+" "+(c.resume?.school||"")).toLowerCase().includes(keyword));
    $("#people").innerHTML=items.length?'<table class="cohort-table"><thead><tr><th>名次</th><th>候选人</th><th>状态与进度</th><th>当前分数</th><th>作答与报告</th></tr></thead><tbody>'+items.map(c=>'<tr data-candidate="'+esc(c.id)+'"><td>'+esc(c.rank??"待评")+'</td><th scope="row">'+esc(c.name)+'<small>'+esc([c.resume?.education,c.resume?.school].filter(Boolean).join(" · "))+'</small></th><td>'+esc(shownStatus(c))+
      (c.expected_questions?'<small>已答 '+esc(c.answered_questions??0)+' / '+esc(c.expected_questions)+' 题</small>':'')+'</td><td class="cohort-score">'+scoreCell(c)+'</td><td><div class="candidate-actions">'+
      '<div class="entry-group student-group"><span class="entry-label">学生入口</span><button class="copy-student" data-id="'+esc(c.id)+'">复制作答链接</button>'+
      (c.student_url?'<a class="button student-entry" href="'+esc(c.student_url)+'" target="_blank" rel="noopener">打开学生页</a>':'<button class="student-entry" data-id="'+esc(c.id)+'">'+(c.session_id?'打开学生页':'创建作答入口')+'</button>')+'</div>'+
      '<div class="entry-group hr-group"><span class="entry-label">HR 查看</span>'+(c.session_id?'<a class="button primary" href="'+esc(sessionUrl(c.session_id))+'">查看评分报告</a>':'')+'</div></div></td></tr>').join("")+'</tbody></table>':'<p class="empty">'+(b.candidates.length?'没有符合条件的候选人。':'导入简历后，可为每位候选人发起面试。')+'</p>';
    const rows=$$("#people tbody tr");
    items.forEach((person,i)=>{
      const cell=rows[i]?.querySelector(".candidate-actions");
      if(!cell)return;
      const btn=document.createElement("button");btn.textContent="核对简历";btn.type="button";btn.className="resume-check";
      btn.onclick=()=>resumeEditor(person,url);$(".hr-group",cell).append(btn);
      onClick("button.student-entry",async()=>{
        const tab=window.open("about:blank","_blank");
        if(!tab)throw new Error("浏览器未能打开学生页，请允许弹出窗口或使用“复制学生链接”。");
        tab.opener=null;
        try {
          const session=await ensureSession(person);
          tab.location.href=candidateUrl(session.token);
          if(!person.session_id)await showBatch();
        } catch(error){tab.close();throw error;}
      },cell);
      onClick(".copy-student",()=>copyStudentLink(person),cell);
    });
  }
  drawPeople(); $("#filter").oninput=drawPeople;
  onClick("#batch-export",async()=>{const out=await request(url+"/export",{});notice("已导出至 "+out.file+"。移动报告时，请一起保留同目录个人报告和 assets 文件夹。");});
  onClick("#refresh-batch",()=>showBatch());
  onClick("#prepare-cohort",async()=>{
    let done=0;
    try {
      for(const person of unstarted){await ensureSession(person);done++;notice("已建立 "+done+" / "+unstarted.length+" 个作答入口。");}
      await showBatch();notice("本批次的独立作答入口已准备好，可逐人打开或复制链接。");
    } catch(error){await showBatch();throw new Error("本次已建立 "+done+" 个入口。"+error.message);}
  });
  onClick("#import-demo",async()=>{await request(url+"/import-demo",{});await showBatch();notice("模拟简历已导入。");});
  onClick("#import-files",async()=>{
    const files=[...$("#resumes").files]; if(!files.length)throw new Error("请先选择 DOCX 简历。");
    let done=0;
    try {
      for(const file of files){ const form=new FormData();form.set("file",file);await request(url+"/resumes",form);done++; }
      await showBatch();notice("已处理 "+done+" 份简历。");
    } catch(error){await showBatch();throw new Error("已处理 "+done+" 份。"+error.message);}
  });
}
function resumeEditor(person,url) {
  const r=person.resume, region=$("#correction");
  const select=(key,values)=>field({education:"学历",education_status:"取得情况",internship:"实习经历"}[key],'<select name="'+key+'">'+opts(values.map(v=>({id:v,text:v})),r[key],"未核实")+'</select>');
  const tags=["985","211","双一流","其他"];
  const exams=(r.english||[]).map(e=>e.exam+(e.score!=null?" "+e.score:"")).join("\n");
  const sources=(r.facts||[]).map(f=>'<li>'+esc(f.source)+' · '+esc(f.quote)+'</li>').join("");
  const corrections=(r.corrections||[]).map(a=>'<li>'+esc(a.reviewer)+' · '+esc(a.reason)+'</li>').join("");
  region.innerHTML=section(person.name+" · 简历核对",buttonLink("下载简历原件",apiRoot+url+"/candidates/"+enc(person.id)+"/resume.docx")+'<details><summary>查看原文与更正历史</summary><ul class="list">'+sources+corrections+'</ul></details>'+
    '<form id="edit-resume" class="card stack"><div class="grid">'+select("education",["专科","本科","学士","硕士","博士","未提供"])+
    select("education_status",["在读","已取得","待核实"])+field("院校名称",'<input name="school" value="'+esc(r.school)+'">')+select("internship",["有","无","未提供"])+'</div>'+
    '<fieldset><legend>院校标签</legend><div class="row">'+tags.map(t=>'<label><input type="checkbox" name="school_tags" value="'+esc(t)+'" '+((r.school_tags||[]).includes(t)?"checked":"")+'>'+esc(t)+'</label>').join("")+'</div></fieldset>'+
    field("英语考试与分数（每行一项）",'<textarea name="english" placeholder="CET6 520">'+esc(exams)+'</textarea>')+'<label><input name="english_none" type="checkbox" '+(r.english_none?'checked':'')+'> 已确认无英语成绩</label>'+
    field("更正人",'<input name="reviewer" required>')+field("原文位置与更正原因",'<textarea name="reason" required></textarea>')+
    '<p class="note">面试开始作答后保留该场的简历快照。更正将保存在批次档案中。</p><button class="primary">保存更正</button></form>');
  region.scrollIntoView({block:"start"});
  $("#edit-resume").onsubmit=async e=>{
    e.preventDefault();const form=e.currentTarget,fields=new FormData(form),changes={};
    for(const key of ["education","education_status","school","internship"]){
      const value=String(fields.get(key)||"").trim();if(value&&value!==r[key])changes[key]=value;
    }
    const selected=fields.getAll("school_tags");if(JSON.stringify([...selected].sort())!==JSON.stringify([...(r.school_tags||[])].sort()))changes.school_tags=selected;
    const lines=String(fields.get("english")||"").trim();const no=!!fields.get("english_none");
    if(lines!==exams){
      changes.english=lines.split(/\r?\n/).map(v=>v.trim()).filter(Boolean).map(v=>{
        const m=v.match(/^(.*?)\s+(\d+(?:\.\d+)?)$/);
        return m?{exam:m[1],score:Number(m[2])}:{exam:v,score:null};
      });
    }
    if(no!==!!r.english_none)changes.english_none=no;
    if(!Object.keys(changes).length){notice("没有需要保存的更正。");return;}
    const submit=$("button",form);submit.disabled=true;
    try{await request(url+"/candidates/"+enc(person.id)+"/resume",{version:r.resume_version||1,reviewer:fields.get("reviewer"),reason:fields.get("reason"),changes},"PUT");await showBatch();notice("简历更正已存档。");}
    catch(err){notice(err.message,true);submit.disabled=false;}
  };
}
function answerEditor(q, old) {
  const value=old?.value;
  if(q.kind==="text") return field("作答原文",'<textarea id="written" maxlength="25000" placeholder="可在这里作答，也可以录制或上传音视频。">'+esc(old?.text||value||"")+'</textarea>');
  if(q.kind==="choice") return '<fieldset><legend>选择一项</legend>'+q.options.map(o=>'<label class="option"><input type="radio" name="choice" value="'+esc(o.id)+'" '+(value===o.id?'checked':'')+'>'+esc(o.text)+'</label>').join("")+'</fieldset>';
  if(q.kind==="ranking") return '<fieldset><legend>按优先顺序排列，每项选一次</legend>'+q.options.map((_,i)=>field("第 "+(i+1)+" 位",'<select class="ranking">'+opts(q.options,value?.[i])+'</select>')).join("")+'</fieldset>';
  if(q.kind==="compound") return '<fieldset><legend>分别完成以下项目</legend>'+q.fields.map(f=>field(f.prompt,'<select class="field-choice" data-key="'+esc(f.id)+'">'+opts(f.options,value?.[f.id])+'</select>')).join("")+'</fieldset>';
  throw new Error("当前题型不受支持。");
}
function readAnswer(q) {
  if(q.kind==="text") return $("#written").value.trim();
  if(q.kind==="choice") return $("input[name=choice]:checked")?.value;
  if(q.kind==="ranking") return $$(".ranking").map(n=>n.value);
  return Object.fromEntries($$(".field-choice").map(n=>[n.dataset.key,n.value]));
}
function activePlan(s) {
  if (s.status === "awaiting_verification" && Array.isArray(s.open_verifications)) {
    return s.plan.filter(q => s.open_verifications.includes(q.id));
  }
  return s.plan;
}
function questionLabel(q, showFactor = true) {
  if (!q) return "";
  const kinds = {text:"开放题",choice:"单选题",ranking:"排序题",compound:"组合题"};
  return [showFactor ? q.factor_name : "", q.parent_id ? "追问" : kinds[q.kind]].filter(Boolean).join(" · ");
}
const shortInterview = s => s.interview_mode === "short_5_3";
const totalQuestions = s => shortInterview(s) ? s.expected_questions : s.plan.length;
const isChoiceLocked = s => shortInterview(s) && s.plan.some(q=>q.stage==="interview" && answered(s,q));
function progressCount(done, total) {
  return '<p class="count">已作答 <strong>'+done+' / '+total+'</strong> 题</p>'+
    '<progress class="progress" aria-label="已作答题数" value="'+done+'" max="'+(total || 1)+'"></progress>';
}
function progressPanel(s, plan, q) {
  const done = plan.filter(item => answered(s,item)).length;
  const phased = shortInterview(s) && s.status!=="awaiting_verification";
  const warmup = s.status!=="awaiting_verification" && (s.introduction || !Object.keys(s.answers).length);
  const stages = ["设备测试", ...(warmup ? ["自我介绍"] : []), ...(phased ? ["5 道单选","3 道问答"] : ["逐题作答"]), "提交面试"];
  const total = phased ? totalQuestions(s) : plan.length;
  let step = stages.length-1;
  if (candidatePage==="setup") step=0;
  if (candidatePage==="introduction") step=1;
  if (candidatePage==="answer") step=(phased && q?.stage==="interview" ? 2 : 1)+(warmup?1:0);
  const steps = stages.map((label,i) => {
    const finished = i < step || candidatePage === "submitted";
    return '<li class="'+(finished ? 'done' : i===step ? 'current' : '')+'"'+(i===step?' aria-current="step"':'')+'><span aria-hidden="true">'+(finished?"✓":i+1)+'</span>'+label+'</li>';
  }).join("");
  let navigation = "";
  if (candidatePage === "answer" || candidatePage === "review") {
    const options = plan.map((item,i) => '<option value="'+esc(item.id)+'" '+(q?.id===item.id?'selected':'')+'>'+
      esc("第 "+(i+1)+" 题 · "+(answered(s,item)?"已答":"待答")+" · "+questionLabel(item,false))+'</option>').join("");
    navigation = field("前往题目",'<select id="question-picker">'+(!q?'<option value="">选择已答题目</option>':'')+options+'</select>');
  }
  return '<aside class="interview-sidebar" id="candidate-progress" aria-label="面试进展"><h2>面试进展</h2><ol class="interview-steps">'+steps+'</ol>'+
    progressCount(done,total)+'<p class="note">'+(s.status==="awaiting_verification"?"本轮补答 · ":"")+'剩余 '+(total-done)+' 题</p>'+navigation+'</aside>';
}
function releaseDevice() {
  device?.dispose();
  device = null;
}
function canSwitch() {
  if (saving || device?.busy) throw new Error("请先结束录制并等待保存完成。");
  if (dirty && !window.confirm("这道题还有未保存的作答，确定放弃修改吗？")) return false;
  dirty = false;
  return true;
}
function switchQuestion(id) {
  currentId = id;
  candidatePage = "answer";
  drawCandidate(true);
}
async function saveCandidateAnswer(base, q, submit) {
  const original = candidateState;
  try { return await submit(original.version); }
  catch (error) {
    if (error.status !== 409) throw error;
  }
  const latest = await request(base);
  const current = q ? activePlan(latest).find(item=>item.id===q.id) : null;
  if (!["interview_open","awaiting_verification"].includes(latest.status) || (!q && latest.status!=="interview_open") || JSON.stringify(current)!==JSON.stringify(q)) {
    throw new Error("这道题已变更或本场已提交。本页保留了你的作答，请先留存内容，再重新打开作答链接。");
  }
  const previousAnswer = q ? original.answers[q.id] : original.introduction;
  const currentAnswer = q ? latest.answers[q.id] : latest.introduction;
  if (JSON.stringify(currentAnswer)!==JSON.stringify(previousAnswer) &&
      !window.confirm("另一个窗口已保存这道题。要用本页作答更新它吗？取消可保留本页草稿。")) {
    throw new Error("已保留本页草稿，老师端仍显示之前保存的版本。");
  }
  return submit(latest.version);
}
function drawCandidate(focus = false) {
  releaseDevice();
  const s = candidateState, plan = activePlan(s);
  if (!["interview_open","awaiting_verification"].includes(s.status)) candidatePage = "submitted";
  const firstPending = plan.find(q => !answered(s,q));
  const q = plan.find(item => item.id === currentId) || firstPending || plan[0];
  const resumeAt = plan.find(item => item.id === currentId) || firstPending;
  const index = plan.indexOf(q);
  const base = "/c/"+enc(token);
  const old = s.answers?.[q?.id];
  const isSupplement = s.status === "awaiting_verification";
  const phased = shortInterview(s) && !isSupplement;
  const needsIntroduction = !isSupplement && !s.introduction && !Object.keys(s.answers).length;
  const locked = phased && q?.stage==="screening" && isChoiceLocked(s);
  const header = head(s.candidate+" · 模拟面试",s.correction_request ? "管培生面试 · 原题补正" : isSupplement ? "管培生面试 · 核实补答" : phased ? "通用管培生 · 5 道情境单选和 3 道面试问答" : "通用管培生 · 候选人作答");
  let content;
  if (candidatePage === "submitted") {
    content = '<h2 id="interview-heading" tabindex="-1">面试已提交</h2><p>本场作答已保存。老师可在工作台查看报告和逐题原片。</p>'+
      '<p id="submission-progress" role="status">'+esc(scoringLabels[s.auto_scoring]||"")+'</p><p class="note">如需校正原话或重录，请老师在该题细则中发起补正；开放后可通过同一链接完成。</p>';
  } else if (candidatePage === "setup") {
    content = '<h2 id="interview-heading" tabindex="-1">设备测试</h2><p class="note">试录将在本机检查人声与录音质量，检查后删除试录文件，不进入评分。请回放确认能清楚听到自己的话。</p>'+
      (s.correction_request?'<p class="notice">老师已开放 '+s.correction_request.question_ids.length+' 道原题补正：'+esc(s.correction_request.reason)+'。其他作答保留。</p>':'')+
      (s.content_provider!=="local_demo"?'<p class="note">正式问答使用百炼 Qwen 分析作答原话；本机转写不足时，音轨将发送至百炼语音服务。视频画面和面部分析保存在本机。</p>':'')+
      '<div id="device-test"></div><div class="interview-actions"><div><button class="primary" id="enter-interview">'+
      (resumeAt ? ((s.plan.some(item=>answered(s,item)) || currentId) ? "继续作答" : "开始面试") : "核对并提交")+
      '</button><button id="enter-without-recording">使用文字／上传文件继续</button>'+(s.introduction&&!isSupplement?'<button id="view-introduction">查看自我介绍</button>':'')+'<p class="note">'+(needsIntroduction?'先聊聊你自己，再开始正式面试。':resumeAt ? '进入第 '+(plan.indexOf(resumeAt)+1)+' 题 · '+esc(questionLabel(resumeAt,false)) : '本轮题目已全部保存。')+'</p></div></div>';
  } else if (candidatePage === "introduction") {
    content = '<h2 id="interview-heading" tabindex="-1">先认识一下你</h2><p class="question-prompt">请简单介绍一下自己，聊聊你的学习经历、兴趣，或一件你想分享的事。</p>'+
      '<div id="introduction-device"></div>'+
      field("自我介绍",'<textarea id="written" maxlength="25000" placeholder="从你想分享的事情开始吧。">'+esc(s.introduction?.text||"")+'</textarea>')+
      '<div class="interview-actions"><button id="save-introduction" class="primary">保存介绍，进入正式面试</button><button id="skip-introduction">'+(s.introduction?.status==="saved"?'直接进入正式面试':'跳过，进入正式面试')+'</button><button id="retest">返回设备测试</button></div>';
  } else if (candidatePage === "review") {
    content = '<h2 id="interview-heading" tabindex="-1">核对并提交</h2><p>本轮 '+plan.length+' 道题均已保存。你可以返回查看作答，也可以提交给老师。</p>'+
      '<div class="interview-actions"><button class="primary" id="finish">提交并结束面试</button><button id="back-to-answers">返回查看作答</button></div>';
  } else {
    if (!q) throw new Error("本场尚未配置题目。");
    currentId = q.id;
    content = '<div class="between"><p class="interview-position">第 '+(index+1)+' / '+(phased?totalQuestions(s):plan.length)+' 题</p>'+tag(questionLabel(q,false))+'</div>'+
      '<h2 id="interview-heading" class="question-prompt" tabindex="-1">'+esc(q.prompt)+'</h2>'+
      (q.answer_hint ? '<p class="note">'+esc(q.answer_hint)+'</p>' : '')+
      (q.resume_hint ? '<p class="notice">'+esc(q.resume_hint)+'</p>' : '')+
      (locked ? '<p class="note">你已保存面试问答，这道单选现在可以回看。</p>' : '')+
      (q.kind==="text" ? '<div id="answer-device"></div>' : '')+
      '<div id="answer-fields">'+answerEditor(q,old)+'</div>'+
      '<div class="interview-actions"><button id="save-answer" class="primary">'+(locked?'继续':'保存并继续')+'</button>'+
      (index>0?'<button id="previous">上一题</button>':'')+'<button class="quiet-action" id="retest">返回设备测试</button></div>'+
      '<p class="note">保存后可通过同一链接继续作答。</p>';
  }
  root.innerHTML = header+'<div class="interview-layout">'+progressPanel(s,plan,candidatePage==="answer"?q:null)+
    '<section class="interview-main" aria-labelledby="interview-heading">'+content+'</section></div>';
  if (focus && !saving) $("#interview-heading").focus();

  if (candidatePage === "setup") {
    device = window.InterviewDevice.mount($("#device-test"),{mode:"test",onPreflight:async(file)=>{
      const form=new FormData();form.set("file",file);return request(base+"/preflight",form);
    }});
    const enterInterview=()=>{
      if (device.busy && !window.confirm("设备测试还在进行，确定结束测试并继续吗？")) return;
      currentId = resumeAt?.id;
      candidatePage = needsIntroduction ? "introduction" : resumeAt ? "answer" : "review";
      drawCandidate(true);
    };
    onClick("#enter-interview",()=>{
      if(!device.recordingReady)throw new Error("请先完成试录检查并回放确认人声；也可以选择使用文字／上传文件继续。");
      enterInterview();
    });
    onClick("#enter-without-recording",enterInterview);
    onClick("#view-introduction",()=>{
      if (device.busy && !window.confirm("设备测试还在进行，确定结束测试并继续吗？")) return;
      candidatePage="introduction";drawCandidate(true);
    });
    return;
  }
  if (candidatePage === "submitted") {
    const message = $("#submission-progress");
    async function checkScore() {
      if (!message.isConnected) return;
      try {
        const updated = await request(base);
        if (!message.isConnected) return;
        message.textContent = scoringLabels[updated.auto_scoring] || "";
        if (!scoringBusy(updated.auto_scoring)) return;
      } catch { message.textContent = "评分进度暂未同步，正在重连。"; }
      window.setTimeout(checkScore,2500);
    }
    if (scoringBusy(s.auto_scoring)) window.setTimeout(checkScore,2000);
    return;
  }

  if (candidatePage === "introduction") {
    device=window.InterviewDevice.mount($("#introduction-device"),{
      mode:"answer",introduction:true,savedMedia:s.introduction?.media,onPendingChange:()=>{dirty=true;}
    });
    $("#written").addEventListener("input",()=>{dirty=true;});
    const enterQuestions=()=>{
      currentId=resumeAt?.id;
      candidatePage=resumeAt?"answer":"review";
      dirty=false;
      drawCandidate(true);
    };
    onClick("#save-introduction",async()=>{
      if(device.busy)throw new Error("请先结束录制，再保存自我介绍。");
      const text=$("#written").value.trim(), media=device.pending;
      if(!text&&!media&&!s.introduction?.media)throw new Error("请填写自我介绍、录制音视频，或选择跳过暖场。");
      await withSave(async()=>{
        candidateState=await saveCandidateAnswer(base,null,version=>{
          if(!media)return request(base+"/introduction",{text,version});
          const form=new FormData();form.set("file",media.file);form.set("text",text);form.set("version",String(version));
          return request(base+"/introduction/media",form);
        });
        enterQuestions();notice("自我介绍已保存，接下来开始正式面试。");
      });
    });
    onClick("#skip-introduction",async()=>{
      if(!canSwitch())return;
      await withSave(async()=>{
        candidateState=await saveCandidateAnswer(base,null,version=>request(base+"/introduction",{skip:true,version}));
        enterQuestions();
      });
    });
    onClick("#retest",()=>{if(canSwitch()){candidatePage="setup";drawCandidate(true);}});
    return;
  }

  $("#question-picker").onchange = event => {
    const picker = event.currentTarget;
    try {
      if (picker.value && canSwitch()) { notice(); switchQuestion(picker.value); return; }
    } catch (error) { notice(error.message,true); }
    picker.value = candidatePage==="answer" ? q.id : "";
  };
  if (candidatePage === "review") {
    onClick("#back-to-answers",()=>switchQuestion(plan[plan.length-1].id));
    onClick("#finish",async()=>{
      await withSave(async()=>{
        try { candidateState = await request(base+"/finish",{version:s.version}); }
        catch (error) {
          if (error.status!==409) throw error;
          candidateState = await request(base);
          const pending = activePlan(candidateState).find(item=>!answered(candidateState,item));
          currentId = pending?.id;
          candidatePage = pending ? "answer" : "review";
          notice("场次有更新，已同步最新作答进度。");
        }
        drawCandidate(true);
      });
    });
    return;
  }
  if (q.kind==="text") {
    device = window.InterviewDevice.mount($("#answer-device"),{
      mode:"answer",savedMedia:old?.media,onPendingChange:()=>{dirty=true;}
    });
  }
  if (locked) $("#answer-fields fieldset").disabled = true;
  $("#answer-fields").addEventListener("input",()=>{dirty=true;});
  onClick("#previous",()=>{if (canSwitch()) switchQuestion(plan[index-1].id);});
  onClick("#retest",()=>{if (canSwitch()) {candidatePage="setup"; drawCandidate(true);}});
  onClick("#save-answer",async()=>{
    if (locked) {
      const next = firstPending || plan[index+1];
      if (next) switchQuestion(next.id);
      else {candidatePage="review";drawCandidate(true);}
      return;
    }
    if (device?.busy) throw new Error("请先结束本题录制，再保存作答。");
    const value = readAnswer(q), media = device?.pending;
    if ((q.kind==="choice" && !value) || (q.kind==="text" && !value && !media && !old?.media)) {
      if (shortInterview(s)) await request(base+"/operation-event",{question_id:q.id,code:"empty_save"});
      throw new Error(q.kind==="choice" ? "请选择一项再保存。" : "请填写作答、录制或选择音视频文件。");
    }
    await withSave(async()=>{
      const endpoint = base+"/answers/"+enc(q.id);
      candidateState = await saveCandidateAnswer(base,q,version=>{
        if (!media) return request(endpoint,{value,version});
        const form = new FormData();
        form.set("file",media.file);
        form.set("version",String(version));
        form.set("text",value);
        return request(endpoint+"/media",form);
      });
      dirty = false;
      const updated = activePlan(candidateState), pos = updated.findIndex(item=>item.id===q.id);
      const next = updated.slice(pos+1).find(item=>!answered(candidateState,item)) || updated.find(item=>!answered(candidateState,item));
      currentId = next?.id;
      candidatePage = next ? "answer" : "review";
      drawCandidate(true);
      const opened = phased && plan.length===5 && updated.length===8;
      notice(opened ? "5 道单选已保存，接下来回答 3 道面试问题。" : "第 "+(index+1)+" 题已保存。");
    });
  });
}
async function withSave(action) {
  if (saving) return;
  saving = true;
  const activeDevice = device;
  const oldHeading = $("#interview-heading");
  root.inert = true;
  root.setAttribute("aria-busy","true");
  activeDevice?.setUploading(true);
  try { await action(); }
  finally {
    saving = false;
    root.inert = false;
    root.setAttribute("aria-busy","false");
    if (activeDevice === device) activeDevice?.setUploading(false);
    if (oldHeading!==$("#interview-heading")) $("#interview-heading")?.focus();
  }
}
async function candidate() {
  role("候选人 · 作答端");
  candidateState = await request("/c/"+enc(token));
  drawCandidate();
}
window.addEventListener("beforeunload",e=>{
  if (saving || dirty || (["answer","introduction"].includes(candidatePage) && device?.busy)) {e.preventDefault(); e.returnValue="";}
});
window.addEventListener("pagehide",releaseDevice);
window.addEventListener("pageshow",event=>{
  if (event.persisted && token) location.reload();
});

function hrQuestion(s, index, evidenceVersion) {
  const q=s.plan[index], a=s.answers[q.id]||{};
  const meta=a.media, proc=a.processing||{}, ann=a.observations;
  const endpoint="/sessions/"+enc(s.id)+"/answers/"+enc(q.id);
  let answer=a.text||"", value=a.value;
  if(q.kind==="choice")answer=q.options.find(o=>o.id===value)?.text||"";
  if(q.kind==="ranking"&&Array.isArray(value))answer=value.map(v=>q.options.find(o=>o.id===v)?.text).filter(Boolean).join(" → ");
  if(q.kind==="compound"&&value)answer=q.fields.map(f=>f.prompt+"　"+(f.options.find(o=>o.id===value[f.id])?.text||"待补齐")).join("\n");
  const source='<p class="meta">题库 · '+esc(q.source||s.bank_version)+'</p><p>'+esc(q.reason||"")+'</p>';
  const quote='<blockquote class="quote">'+esc(answer||a.transcript||"尚无作答原文")+'</blockquote>';
  let content='<h3>'+esc(q.prompt)+'</h3>'+source+quote;
  if (q.stage==="screening" && a.sjt_score!=null) content+='<p><strong>'+esc(q.factor_name)+' · '+esc(a.sjt_score)+' / 5 分</strong>　'+esc(q.score_source||"题库选项计分")+'</p>';
  if(meta){
    const mediaTag=String(meta.mime).startsWith("audio/")?"audio":"video";
    const url=apiRoot+endpoint+"/media?sha256="+enc(meta.sha256);
    content+='<'+mediaTag+' controls preload="metadata" src="'+esc(url)+'"></'+mediaTag+'>'+
      '<div class="toolbar"><button id="media-process">提取观测与转写</button><button id="media-signals">仅提取观测</button></div>';
    if(a.transcript)content+='<h4>音频转写</h4><p class="quote">'+esc(a.transcript)+'</p>';
    if(proc.observations?.length)content+='<p class="meta">已提取 '+proc.observations.length+' 个观察窗口。'+esc((proc.notices||[]).join("；"))+'</p>';
    const suggestions=proc.suggestions||proc.five_state_suggestions||proc.proposed_states||{};
    content+='<details id="annotate"><summary>复核并标注五态时段</summary><div class="stack">'+
      '<p class="note">回看原片后填写开始秒、结束秒与观察状态。未标注的区间计为未覆盖。</p>'+
      '<div class="toolbar">'+field("标注人",'<input id="annotator" value="'+esc(ann?.reviewer||"")+'">')+
      field("核实的总时长（秒）",'<input id="annotation-duration" type="number" step="any" min="0.001" value="'+esc(ann?.duration_seconds||meta.duration_seconds||"")+'">')+'</div>'+
      '<div id="intervals"></div><div class="toolbar"><button id="add-interval">增加时段</button>'+
      (suggestions.segments?.length?'<button id="use-proposals">载入待核实建议</button>':'')+
      '<button class="primary" id="commit-annotations">确认并保存标注</button></div></div></details>';
  }
  const canCorrect=shortInterview(s)&&q.stage==="interview"&&["interview_finished","draft","reviewed","review_changes"].includes(s.status);
  if(canCorrect){
    content+='<details id="answer-correction"><summary>校正原话／发起原题补录</summary><div class="stack">'+
      '<p class="note">仅补正这一道原题，其他题目与历史报告保留。已有人工评价不会被自动覆盖；受影响分项需要重新复核。</p>'+
      field("老师姓名",'<input id="correction-reviewer" maxlength="100">')+
      field("补正原因",'<textarea id="correction-reason" maxlength="1000"></textarea>')+
      (meta?field("回听核实后的原话",'<textarea id="corrected-transcript" maxlength="25000">'+esc(a.transcript||a.text||"")+'</textarea>'):'')+
      '<div class="toolbar">'+(meta?'<button id="save-transcript-correction" class="primary">保存校正并更新分析</button>':'')+
      '<button id="reopen-original-answer">开放此原题重新作答</button></div></div></details>';
  }
  $("#q-evidence").innerHTML=content;
  if(canCorrect){
    const correctionBody=()=>{
      const reviewer=$("#correction-reviewer").value.trim(),reason=$("#correction-reason").value.trim();
      if(!reviewer||!reason)throw new Error("请填写老师姓名和补正原因。");
      return {version:evidenceVersion(),input_version:s.input_version,media_sha256:meta?.sha256||null,reviewer,reason};
    };
    onClick("#save-transcript-correction",async()=>{
      const body=correctionBody();body.transcript=$("#corrected-transcript").value.trim();
      if(!body.transcript)throw new Error("请填写回听核实后的原话。");
      await request(endpoint+"/correct-transcript",body);await showSession(q.id);notice("原话校正已保存，历史原片与报告保留。");
    });
    onClick("#reopen-original-answer",async()=>{
      await request(endpoint+"/reopen-answer",correctionBody());await showSession(q.id);notice("已开放这一道原题，请学生通过原链接补正并提交。");
    });
  }
  if(!meta)return;
  const updateProcess=async(transcribe)=>{
    notice("正在本机处理媒体，请等候结果。");
    await request(endpoint+"/process",{transcribe,refresh:true});
    await showSession(q.id);notice("已完成本机媒体处理。");
  };
  onClick("#media-process",()=>updateProcess(true));onClick("#media-signals",()=>updateProcess(false));
  function addInterval(v={}){
    const div=document.createElement("div");div.className="toolbar interval";
    div.innerHTML=field("开始秒",'<input class="from" type="number" min="0" step="any" value="'+esc(v.start??"")+'">')+
      field("结束秒",'<input class="to" type="number" min="0" step="any" value="'+esc(v.end??"")+'">')+
      field("状态",'<select class="label-state">'+opts(stateLabels.map(s=>({id:s,text:s})),v.state)+'</select>')+
      '<button type="button" class="remove">移除</button>';
    $(".remove",div).onclick=()=>div.remove();$("#intervals").appendChild(div);
  }
  (ann?.segments||[]).forEach(addInterval);
  onClick("#add-interval",()=>addInterval());
  onClick("#use-proposals",()=>{
    const suggestions=proc.suggestions||proc.five_state_suggestions||proc.proposed_states||{};
    if(!$("#intervals").children.length||confirm("用建议时段替换当前未保存的标注？")){
      $("#intervals").replaceChildren();suggestions.segments.forEach(addInterval);
    }
  });
  onClick("#commit-annotations",async()=>{
    const segments=$$(".interval").map(row=>{
      if(!$(".from",row).value||!$(".to",row).value||!$(".label-state",row).value)throw new Error("请补全每段的起止秒和状态。");
      return {start:Number($(".from",row).value),end:Number($(".to",row).value),state:$(".label-state",row).value};
    });
    const duration=Number($("#annotation-duration").value),reviewer=$("#annotator").value.trim();
    if(!reviewer||!(duration>0))throw new Error("请填写标注人和已核实的时长。");
    await request(endpoint+"/observations",{segments,duration_seconds:duration,reviewer,confirmed:true,version:evidenceVersion(),media_sha256:meta.sha256});
    await showSession(q.id);notice("五态标注已保存，报告会使用新版本。");
  });
}

function independentForm(s) {
  const region=$("#independent");
  if(!region)return;
  const teacher=s.teacher_assessment||{};
  const factorList=Array.from(new Map(s.plan.filter(q=>q.factor_id).map(q=>[q.factor_id,q.factor_name]))).sort((a,b)=>a[0].localeCompare(b[0]));
  region.innerHTML='<summary>老师独立评分 · 已保存 '+(teacher.factors||[]).length+' / 8 项</summary><div class="stack">'+
    field("评价人",'<input id="teacher-name" value="'+esc(teacher.reviewer||"")+'">')+
    field("评价因子",'<select id="teacher-factor">'+factorList.map(([id,label])=>'<option value="'+esc(id)+'">'+esc(label)+'</option>').join("")+'</select>')+'<div id="teacher-factor-form"></div></div>';
  function update(){
    const id=$("#teacher-factor").value;
    const assessed=(teacher.factors||[]).find(f=>f.factor_id===id)||{};
    const choices=s.plan.filter(q=>q.factor_id===id&&((s.answers[q.id]?.text||s.answers[q.id]?.transcript)||typeof s.answers[q.id]?.value==="string"));
    const previous=assessed.evidence?.[0]||{};
    $("#teacher-factor-form").innerHTML='<div class="grid">'+field("独立评级",'<select id="teacher-rating"><option value="">证据不足</option>'+[1,2,3,4,5].map(v=>'<option value="'+v+'" '+(v===assessed.rating?"selected":"")+'>'+v+' 分</option>').join("")+'</select>')+
      field("引用的题目",'<select id="teacher-question">'+opts(choices.map(q=>({id:q.id,text:q.prompt})),previous.question_id)+'</select>')+'</div>'+
      '<blockquote class="quote" id="teacher-source"></blockquote>'+
      field("引用的逐字原话",'<textarea id="teacher-quote">'+esc(previous.quote||"")+'</textarea>')+
      field("评分理由或证据缺口",'<textarea id="teacher-reason">'+esc(assessed.reason||"")+'</textarea>')+
      '<button class="primary" id="teacher-save">保存本因子独立评价</button>';
    function showQuote(){const a=s.answers[$("#teacher-question").value]||{};
      $("#teacher-source").textContent=[...new Set([a.text,a.transcript,a.answer_label].filter(Boolean))].join("\n")||"请选择有作答的题目。";}
    $("#teacher-question").onchange=showQuote;showQuote();
    onClick("#teacher-save",async()=>{
      const text=$("#teacher-quote").value.trim(),qid=$("#teacher-question").value, rating=$("#teacher-rating").value;
      const reason=$("#teacher-reason").value.trim(), reviewer=$("#teacher-name").value.trim();
      if(!reviewer||!reason||(rating&&(!text||!qid)))throw new Error("请填写评价人和理由。给出具体分数时还需要一条原话及题号。");
      await request("/sessions/"+enc(s.id)+"/teacher-assessment",{input_version:s.input_version,version:s.version,reviewer,
        factors:[{factor_id:id,rating:rating?Number(rating):null,reason,evidence:qid&&text?[{question_id:qid,quote:text}]:[]}]});
      await showSession();$("#independent").open=true;notice("老师独立评分已保存。");
    });
  }
  $("#teacher-factor").onchange=update;update();
}

function localAnalysis(s, initialFactor) {
  const region=$("#local-analysis");if(!region)return;
  const draft=s.draft||{}, final=s.status==="reviewed";
  const factors=Array.from(new Map(s.plan.filter(q=>q.factor_id).map(q=>[q.factor_id,q.factor_name]))).sort((a,b)=>a[0].localeCompare(b[0]));
  const lastAuthor=draft.source==="local_review"?draft.author:"";
  const phased = shortInterview(s);
  const oral = s.plan.filter(q=>q.stage==="interview");
  const priorEvidence = draft.experience_evidence || {};
  const experienceInput = phased ? field("引用哪道问答的经历",'<select id="experience-question">'+opts(oral.map(q=>({id:q.id,text:q.factor_name+" · "+q.prompt})),priorEvidence.question_id)+'</select>')+
    '<blockquote class="quote" id="experience-source"></blockquote>'+
    field("引用回答原文或转写",'<textarea id="experience-quote">'+esc(priorEvidence.quote||"")+'</textarea>') :
    '<blockquote class="quote">'+esc(s.answers.EXPERIENCE?.text||s.answers.EXPERIENCE?.transcript||"等待经历介绍原话。")+'</blockquote>';
  region.innerHTML='<summary>本地人工分析草稿</summary><div class="stack">'+
    field("草稿录入人",'<input id="local-author" maxlength="100" value="'+esc(lastAuthor||"")+'">')+
    '<h3>逐项评价八因子</h3>'+
    field("待评价因子",'<select id="local-fid">'+factors.map(([id,name])=>'<option value="'+esc(id)+'">'+esc(id+" · "+name)+(draft.factors?.some(f=>f.factor_id===id)?" · 已评":"")+'</option>').join("")+'</select>')+
    '<div id="local-factor-editor" class="stack"></div><h3>经历、流程与综合意见</h3>'+
    experienceInput+
    '<div class="grid">'+field("经历介绍分（0–100）",'<input id="local-exp" type="number" step="any" min="0" max="100" value="'+esc(draft.experience_score??"")+'">')+
    field("流程操作分（0–100）",'<input id="local-op" type="number" step="any" min="0" max="100" value="'+esc(draft.operation_score??"")+'">')+'</div>'+
    field("经历评分依据",'<textarea id="local-exp-reason">'+esc(draft.experience_reason||"")+'</textarea>')+
    field("操作记录依据",'<textarea id="local-op-reason">'+esc(draft.operation_reason||"")+'</textarea>')+
    field("综合意见",'<textarea id="local-summary">'+esc(draft.summary||"")+'</textarea>')+
    field("下一轮建议（每行一条）",'<textarea id="local-next">'+esc((draft.next_steps||[]).join("\n"))+'</textarea>')+
    '<button id="local-sections" class="primary">保存分项与意见</button></div>';
  const picker=$("#local-fid");
  const nextFactor = phased ? oral.find(q=>!draft.factors?.some(f=>f.factor_id===q.factor_id))?.factor_id : null;
  picker.value=initialFactor||nextFactor||factors.find(([id])=>!draft.factors?.some(f=>f.factor_id===id))?.[0]||factors[0][0];
  function showExperienceSource() {
    const qid = $("#experience-question").value, a = s.answers[qid] || {};
    $("#experience-source").textContent = [a.text,a.transcript].filter(Boolean).join("\n") || (qid ? "此题暂无原文或转写。" : "选择含有经历证据的问答。");
  }
  if (phased) { $("#experience-question").onchange = showExperienceSource; showExperienceSource(); }
  const localBase="/sessions/"+enc(s.id);
  async function save(body, next) {
    if(final)throw new Error("此报告已复核。");
    const author=$("#local-author").value.trim();if(!author)throw new Error("请填写草稿录入人。");
    const sectionIds=["#local-exp","#local-exp-reason","#local-op","#local-op-reason","#local-summary","#local-next"];
    if (phased) sectionIds.push("#experience-question","#experience-quote");
    const unsubmitted=body.factors?sectionIds.map(key=>[key,$(key).value]):[];
    const saved=await request(localBase+"/local-draft",{version:s.version,input_version:s.input_version,source:"local_review",author,...body});
    await showSession(null,next||picker.value);
    if($("#local-analysis")){
      $("#local-analysis").open=true;$("#local-author").value=author;
      for(const [key,value] of unsubmitted)$(key).value=value;
      $("#experience-question")?.dispatchEvent(new Event("change"));
    }
    const unverified=(saved.draft?.factors||[]).find(f=>body.factors?.[0]?.factor_id===f.factor_id)?.verification_gaps||[];
    notice(unverified.length?"已保存，最高档尚需核实本人行动、代价或相似经历。":"本地草稿已保存。");
  }
  function update(){
    const id=picker.value;
    const cur=(draft.factors||[]).find(f=>f.factor_id===id)||{};
    const qs=s.plan.filter(q=>q.factor_id===id&&[s.answers[q.id]?.text,s.answers[q.id]?.transcript,s.answers[q.id]?.answer_label].some(Boolean));
    const choice = s.plan.find(q=>q.stage==="screening" && q.factor_id===id);
    const choiceScore = s.answers[choice?.id]?.sjt_score;
    $("#local-factor-editor").innerHTML='<details id="local-rules"><summary>题库考察要点与评分锚点</summary><p class="note">正在读取…</p></details>'+
      (choiceScore!=null ? '<p class="notice">此因子已按原表计 '+esc(choiceScore)+' / 5 分 · '+esc(choice.score_source)+'</p>' : '')+
      field("因子评级",'<select id="local-rating"><option value="">证据不足</option>'+[1,2,3,4,5].map(v=>'<option value="'+v+'" '+(v===cur.rating?"selected":"")+'>'+v+' 分</option>').join("")+'</select>')+
      field("判断理由或证据缺口",'<textarea id="local-reason">'+esc(cur.reason||"")+'</textarea>')+
      '<div id="local-refs"></div><button id="local-add-ref">增加原话引用</button>'+
      '<details id="local-verify"><summary>最高档核实</summary><p class="note">人工给出 5 分须分别引用本人行动、付出代价和相似经历。'+(phased?'缺失的证据可列入下一轮建议。':'也可向候选人发起题库核实。')+'</p><div id="verify-refs"></div></details>'+
      '<div class="toolbar"><button class="primary" id="local-save">保存本因子</button></div>';
    const old=$("#local-rules");
    old.addEventListener("toggle",async()=>{
      if(!old.open||old.dataset.loaded)return;
      try{const ev=await request(localBase+"/evidence?factor_id="+enc(id)),f=(ev.factors||[]).find(f=>f.id===id);
        $("p",old).textContent=[f?.indicators,f?.anchors].filter(Boolean).join("\n")||"当前题库未提供评分锚点。";$("p",old).className="summary-text";old.dataset.loaded="1";
      }catch(err){$("p",old).textContent=err.message;}
    });
    function reference(container, prev, label, removable, kind){
      const div=document.createElement("div");div.className="card local-ref";if(kind)div.dataset.kind=kind;
      const allowed = kind ? qs.filter(q=>q.kind==="text") : qs;
      div.innerHTML=field(label,'<select class="ref-q">'+opts(allowed.map(q=>({id:q.id,text:q.id+" · "+q.prompt})),prev?.question_id)+'</select>')+
        '<blockquote class="quote source-text"></blockquote>'+field("引用的逐字原话",'<textarea class="ref-text">'+esc(prev?.quote||"")+'</textarea>')+
        (removable?'<button class="remove-ref">移除引用</button>':'')+(kind&&!phased?'<button class="request-check">发起这项核实</button>':'');
      container.append(div);
      const sel=$(".ref-q",div);
      const updateText=()=>{const a=s.answers[sel.value]||{};$(".source-text",div).textContent=[...new Set([a.text,a.transcript,kind?"":a.answer_label].filter(Boolean))].join("\n")||"请选择一题查看作答。";};
      sel.onchange=updateText;updateText();
      if(removable)$(".remove-ref",div).onclick=()=>div.remove();
      if(kind)onClick(".request-check",async()=>{
        await request(localBase+"/clarifications",{source_question_id:id+"-G",question_id:id+"-verify-"+kind,input_version:s.input_version,reason:"最高档需补充核实："+label});
        await showSession();notice("核实题已加入候选人原作答链接。");
      },div);
    }
    const previous=cur.evidence?.length?cur.evidence:[{}];
    previous.forEach(ref=>reference($("#local-refs"),ref,"证据题目",true,null));
    onClick("#local-add-ref",()=>reference($("#local-refs"),{},"证据题目",true,null));
    for(const [kind,label] of [["action","本人行动"],["cost","付出代价"],["experience","相似经历"]])
      reference($("#verify-refs"),cur.verification?.[kind],label,false,kind);
    function readRef(div){const question_id=$(".ref-q",div).value,quote=$(".ref-text",div).value.trim();
      if(Boolean(question_id)!==Boolean(quote))throw new Error("引用须同时包含题目和逐字原话。");
      return quote?{question_id,quote}:null;}
    onClick("#local-save",async()=>{
      const rating=$("#local-rating").value,reason=$("#local-reason").value.trim();
      if(!reason)throw new Error("请填写判断理由或证据缺口。");
      const evidence=$$("#local-refs .local-ref").map(readRef).filter(Boolean);
      if(rating&&!evidence.length)throw new Error("具体评级至少需要引用一条该因子的实际作答原话。");
      const entry={factor_id:id,rating:rating?Number(rating):null,reason,evidence};
      if(rating==="5"){entry.verification={};for(const div of $$("#verify-refs .local-ref")){const r=readRef(div);if(r)entry.verification[div.dataset.kind]=r;}}
      const pos=factors.findIndex(([fid])=>fid===id);
      await save({factors:[entry]},factors[pos+1]?.[0]||id);
    });
  }
  picker.onchange=update;update();
  onClick("#local-sections",async()=>{
    const score=id=>$(id).value===""?null:Number($(id).value);
    const experience=score("#local-exp"),operation=score("#local-op"),experienceReason=$("#local-exp-reason").value.trim(),opReason=$("#local-op-reason").value.trim();
    if(experience!=null&&!experienceReason)throw new Error("请填写经历评分依据。");
    if(operation!=null&&!opReason)throw new Error("请填写操作记录依据。");
    const next=$("#local-next").value.split(/\r?\n/).map(s=>s.trim()).filter(Boolean);
    const changes={};
    if (phased) {
      const ref = experience==null ? null : {question_id:$("#experience-question").value,quote:$("#experience-quote").value.trim()};
      if (ref && (!ref.question_id || !ref.quote)) throw new Error("请为经历评分选择问答并引用原话。");
      if (JSON.stringify(ref)!==JSON.stringify(draft.experience_evidence||null)) Object.assign(changes,{experience_evidence:ref,experience_score:experience});
    }
    if(experience!==(draft.experience_score??null)||experienceReason!==(draft.experience_reason||""))Object.assign(changes,{experience_score:experience,experience_reason:experienceReason});
    if(operation!==(draft.operation_score??null)||opReason!==(draft.operation_reason||""))Object.assign(changes,{operation_score:operation,operation_reason:opReason});
    if($("#local-summary").value.trim()!==(draft.summary||""))changes.summary=$("#local-summary").value.trim();
    if(JSON.stringify(next)!==JSON.stringify(draft.next_steps||[]))changes.next_steps=next;
    if(!Object.keys(changes).length){notice("意见与上次相同。");return;}await save(changes);
  });
}

function hrProgress(s) {
  const done = s.plan.filter(q=>answered(s,q)).length;
  const next = s.plan.findIndex(q=>!answered(s,q));
  let detail = "本场作答已提交。";
  if (next >= 0) detail = "下一道待答是第 "+(next+1)+" 题 · "+questionLabel(s.plan[next])+"。";
  else if (["interview_open","awaiting_verification"].includes(s.status)) detail = "题目已全部保存，等待学生提交面试。";
  return '<div class="between"><h3>面试进度</h3>'+tag(shownStatus(s))+'</div>'+progressCount(done,totalQuestions(s))+
    (shortInterview(s)?'<p>5 道情境单选 · 3 道面试问答</p>':'')+
    '<p class="note">'+esc(detail)+'</p>';
}
function hrQuestionOptions(s, selected) {
  return s.plan.map((q,i)=>'<option value="'+i+'" '+(q.id===selected?'selected':'')+'>'+
    esc((i+1)+". "+(answered(s,q)?"已答":"待答")+" · "+q.prompt.slice(0,65))+'</option>').join("");
}
function introductionEvidence(s) {
  const intro=s.introduction;
  if (!intro || intro.status!=="saved") return "";
  let body=intro.text?'<blockquote class="quote">'+esc(intro.text)+'</blockquote>':"";
  if (intro.media) {
    const element=String(intro.media.mime).startsWith("audio/")?"audio":"video";
    const url=apiRoot+"/sessions/"+enc(s.id)+"/introduction/media?sha256="+enc(intro.media.sha256);
    body+='<'+element+' controls preload="none" src="'+esc(url)+'"></'+element+'>';
  }
  return '<details><summary>自我介绍 · 不计分</summary><div class="card stack">'+body+'</div></details>';
}
async function showSession(focusId, localFactor) {
  const monitor = ++sessionMonitor;
  const base="/sessions/"+enc(sessionId);
  const workbench=await request(base+"/workbench");
  if (monitor!==sessionMonitor) return;
  const s=workbench.session;
  let latest = s;
  role("HR 工作台 · 个人报告");
  document.title = s.candidate+" · 面试综合报告 · 真才实鉴";
  root.closest("main").classList.add("teacher-shell");
  const share=candidateUrl(s.token), draft=s.draft||{};
  const accepting=state=>["interview_open","awaiting_verification"].includes(state.status);
  const studentLink='<a class="button'+(accepting(s)?' teacher-student-shortcut':'')+'" href="'+esc(share)+'" target="_blank" rel="noopener">打开候选人界面</a>';
  const canConvert = state => !shortInterview(state) && state.status==="interview_open" && !Object.keys(state.answers).length && !state.draft && !state.teacher_assessment;
  const info='<div class="card"><div id="session-progress" aria-live="polite">'+hrProgress(s)+'</div><div class="toolbar">'+
    (!accepting(s)?studentLink:"")+'<button id="copy-url">复制作答链接</button>'+
    (canConvert(s)?'<button id="switch-short">改为 5 道单选 + 3 道问答</button>':'')+'</div>'+
    '<p class="micro" id="sync-status" role="status"></p></div>';
  const select='<select id="question-view">'+hrQuestionOptions(s,focusId)+'</select>';
  const review=s.review||{};
  const showReview=Object.keys(draft).length>0;
  const checked=Array.isArray(draft.factors)?draft.factors.length:0;
  function analysisProgress(state) {
    if (!shortInterview(state)) return "已收到 "+checked+" / 8 项因子分析。";
    const choices = state.plan.filter(q=>q.stage==="screening" && state.answers[q.id]?.sjt_score!=null).length;
    const oral = state.plan.filter(q=>q.stage==="interview" && state.draft?.factors?.some(f=>f.factor_id===q.factor_id && f.rating!=null && !f.verification_gaps?.length)).length;
    return (scoringLabels[state.auto_scoring?.status]?scoringLabels[state.auto_scoring.status]+"。":"")+
      "单选已计分 "+choices+" / 5 项，问答有效评分 "+oral+" / 3 项。";
  }
  const toolsOpen = $("#review-tools")?.open || Boolean(localFactor) || location.hash==="#review-tools";
  const nav = '<nav class="teacher-nav" aria-label="教师操作">'+buttonLink("返回批次",batchUrl(s.batch_id))+'<div class="row">'+
    (accepting(s)?studentLink:"")+'<a class="button" href="#review-tools">评分细则与复核</a>'+
    '<a class="button" id="standalone-report" href="'+esc(reportUrl(s.id,s.version))+'">个人 HTML 报告</a>'+
    '<button id="personal-export">导出离线报告</button></div></nav>';
  const cohortNav='<nav class="teacher-batch-nav" aria-label="批次导航">'+buttonLink("← 返回本批次总控",batchUrl(s.batch_id))+
    '<span class="meta"><strong>'+esc(s.candidate)+' · HR 个人报告</strong><br>'+esc(s.batch_name)+'</span>'+buttonLink("本批次汇总排名",batchReportUrl(s.batch_id))+'</nav>';
  root.innerHTML=cohortNav+'<div id="hr-analysis-status" class="notice" role="status" aria-live="polite"'+(scoringBusy(s.auto_scoring?.status)?'':' hidden')+'>'+hrAnalysisStatus(s)+'</div>'+
    '<iframe id="teacher-report" title="'+esc(s.candidate)+' · 个人面试评价报告" src="'+esc(workbench.report_url)+'"></iframe>'+
    '<div class="teacher-controls">'+nav+
    '<details id="review-tools"'+(toolsOpen?' open':'')+'><summary>评分细则与复核</summary>'+
    info+'<div id="hr-introduction">'+introductionEvidence(s)+'</div>'+section("题目、证据与原片",field("选择题目",select)+'<div class="card stack" id="q-evidence"></div>')+
    section("分析草稿与老师复核",'<div class="stack"><p><span id="analysis-progress">'+esc(analysisProgress(s))+'</span>'+(draft.input_version?" 证据版本 "+esc(draft.input_version)+" · 来源 "+esc(sourceNames[draft.source]||"历史来源未登记")+"。":"")+'</p>'+
      (["interview_finished","draft","review_changes"].includes(s.status)?'<details id="independent"></details>':'')+
      (showReview?'<details><summary>查看分析草稿与分项</summary><p class="summary-text">'+esc(draft.summary||"待补综合意见")+'</p><ul class="list">'+(draft.factors||[]).map(f=>'<li>'+esc(f.factor_id)+' · '+esc(f.rating??"证据不足")+' · '+esc(f.reason)+' · '+esc(sourceNames[f.source]||"来源未登记")+'</li>').join("")+'</ul></details>':'')+
      (["interview_finished","draft","review_changes"].includes(s.status)?'<details id="local-analysis"></details>':'')+
      (s.auto_scoring?.issues?.length?'<ul class="list">'+s.auto_scoring.issues.map(issue=>'<li>'+esc(issue)+'</li>').join("")+'</ul>':'')+
      '<div class="toolbar">'+(shortInterview(s) && ["interview_finished","draft"].includes(s.status) && !s.review && (!s.auto_scoring || ["partial","error","failed","superseded"].includes(s.auto_scoring.status) || (s.auto_scoring.status==="done" && (s.auto_scoring.issues?.length||!s.auto_scoring.engine))) && automaticDraft(s.draft)
        ?'<button id="auto-score">按本场记录重新分析</button>':'')+'<button id="copy-scope">复制千问分析指令</button><button id="refresh-session">刷新场次详情</button></div>'+
      (showReview?'<details id="review"><summary>查看或提交老师复核</summary><p class="note">'+(review.decision?esc(statusNames[review.decision]||({approve:"已通过",request_changes:"已退回"}[review.decision]))+" · "+esc(review.reviewer):"当前草稿待复核。")+'</p>'+
      field("复核人",'<input id="review-name" maxlength="100" value="'+esc(review.reviewer||"")+'">')+
      field("复核意见",'<textarea id="review-notes">'+esc(review.notes||"")+'</textarea>')+
      '<div class="toolbar"><button id="approve" class="primary">通过复核</button><button id="changes">退回补充</button></div></details>':'')+'</div>')+'</details></div>';
  async function copy(value, success){
    if(navigator.clipboard?.writeText){await navigator.clipboard.writeText(value);notice(success);}
    else{window.prompt("请复制以下内容",value);}
  }
  onClick("#copy-url",()=>copy(location.origin+share,"作答链接已复制。"));
  onClick("#switch-short",async()=>{
    await request(base+"/short-interview",{version:latest.version});
    await showSession();
    notice("已改为 5 道单选和 3 道问答。原作答链接可继续使用。");
  });
  independentForm(s);localAnalysis(s,localFactor);
  if(localFactor&&$("#local-analysis"))$("#local-analysis").open=true;
  onClick("#personal-export",async()=>{const out=await request(base+"/export",{version:latest.version});notice("已导出至 "+out.file+"。有原片时，请与同目录 assets 文件夹一起保留。");});
  onClick("#refresh-session",()=>showSession(latest.plan[Number($("#question-view").value)]?.id));
  onClick("#auto-score",async()=>{
    await request(base+"/auto-score",{version:latest.version});
    await showSession();
  });
  onClick("#copy-scope",()=>copy("使用真才实鉴 V2，对模拟面试场次 "+s.id+" 读取当前版本的题库、考察锚点、简历及实际回答。"+
    (shortInterview(s)?"五项情境单选沿用 J 列实得分。为其余三项逐一引用问答原话、按锚点评价，证据不足时记录缺口。":"逐项核查证据，保存八因子分析草稿。")+
    "保存经历评价、综合意见和下一轮核实建议，返回本机 HTML 报告入口。","千问分析指令已复制。"));
  const picker = $("#question-view");
  let shownId, shownAnswer;
  function displayEvidence() {
    const index = Number(picker.value), q = latest.plan[index];
    if (!q) return;
    const savedVersion = latest.version, savedAnswer = JSON.stringify(latest.answers[q.id]);
    shownId = q.id;
    shownAnswer = savedAnswer;
    hrQuestion(latest,index,()=>JSON.stringify(latest.answers[q.id])===savedAnswer ? latest.version : savedVersion);
  }
  displayEvidence();
  picker.onchange = displayEvidence;
  const frame = $("#teacher-report");
  let expandedReportDetails = [];
  frame.addEventListener("load",()=>{
    const doc=frame.contentDocument;
    if (!doc?.body) return;
    for (const id of expandedReportDetails) {
      const item=doc.getElementById(id);
      if (item?.tagName==="DETAILS") item.open=true;
    }
    expandedReportDetails=[];
    const fit=()=>{ frame.style.height=Math.ceil(doc.body.getBoundingClientRect().height)+"px"; };
    const observer=new frame.contentWindow.ResizeObserver(fit);
    observer.observe(doc.body);
    fit();
    doc.addEventListener("click",event=>{
      const target=event.target.closest("a[data-review-question]");
      if (!target) return;
      event.preventDefault();
      revealReportTarget("#review-tools",target.dataset.reviewQuestion);
      picker.focus({preventScroll:true});
      picker.scrollIntoView({block:"start"});
    });
  });
  function revealReportTarget(hash, qid) {
    if (hash==="#review-tools") {
      $("#review-tools").open=true;
      const index=latest.plan.findIndex(q=>q.id===qid);
      if (index>=0) { picker.value=String(index); displayEvidence(); }
    } else if (hash?.startsWith("#q-")) {
      const url=new URL(frame.src);
      if (url.hash!==hash) { url.hash=hash; frame.src=url.href; }
    }
  }
  root.onclick=event=>{
    const link=event.target.closest("a[href^='#']");
    if (link) revealReportTarget(link.hash,link.dataset.reviewQuestion);
  };
  revealReportTarget(location.hash);
  async function submitReview(decision) {
    const reviewer=$("#review-name").value.trim();if(!reviewer)throw new Error("请填写复核人。");
    await request(base+"/review",{version:s.version,reviewer,decision,notes:$("#review-notes").value.trim()});
    await showSession();notice("复核结果已保存。");
  }
  onClick("#approve",()=>submitReview("approve"));onClick("#changes",()=>submitReview("request_changes"));
  if (!accepting(s) && !scoringBusy(s.auto_scoring?.status)) return;
  let reportPending = null;
  let shownIntroduction=JSON.stringify(s.introduction);

  async function pollProgress() {
    if (monitor!==sessionMonitor) return;
    try {
      const progress = await request(base+"/progress");
      if (monitor!==sessionMonitor) return;
      if (progress.version!==latest.version) {
        const incoming = await request(base+"/workbench");
        if (monitor!==sessionMonitor) return;
        const selected = latest.plan[Number(picker.value)]?.id;
        latest = incoming.session;
        reportPending = incoming.report_url;
        $("#session-progress").innerHTML = hrProgress(latest);
        $("#analysis-progress").textContent = analysisProgress(latest);
        $("#hr-analysis-status").hidden = !scoringBusy(latest.auto_scoring?.status);
        $("#hr-analysis-status").innerHTML = hrAnalysisStatus(latest);
        if ($("#switch-short")) $("#switch-short").hidden=!canConvert(latest);
        $("#standalone-report").href = reportUrl(latest.id,latest.version);
        picker.innerHTML = hrQuestionOptions(latest,selected);
      }
      const reportDoc=frame.contentDocument;
      const reportPlaying=reportDoc && $$("video, audio",reportDoc).some(p=>!p.paused && !p.ended);
      if (reportPending && !reportPlaying) {
        expandedReportDetails=reportDoc ? $$("details[open][id]",reportDoc).map(d=>d.id) : [];
        frame.src=reportPending;
        reportPending=null;
      }
      const selected = latest.plan[Number(picker.value)]?.id;
      const introPlayer=$("#hr-introduction video, #hr-introduction audio");
      if (JSON.stringify(latest.introduction)!==shownIntroduction && (!introPlayer||introPlayer.paused)) {
        $("#hr-introduction").innerHTML=introductionEvidence(latest);
        shownIntroduction=JSON.stringify(latest.introduction);
      }
      const player = $("#q-evidence audio, #q-evidence video");
      const editing = $("#annotate[open]") || (player && !player.paused);
      if ((s.status!==latest.status || shortInterview(s)!==shortInterview(latest) || s.auto_scoring?.status!==latest.auto_scoring?.status)
          && !editing && !reportPlaying) { await showSession(selected); return; }
      const evidenceChanged = shownId!==selected || shownAnswer!==JSON.stringify(latest.answers[selected]);
      if (evidenceChanged && !editing) displayEvidence();
      $("#sync-status").textContent = editing && (evidenceChanged || s.status!==latest.status) ? "进度已更新，结束回看或收起标注后将同步详情。" : "";
    } catch (error) {
      if (monitor===sessionMonitor) $("#sync-status").textContent = "进度暂未同步，正在重连。";
    }
    if (monitor===sessionMonitor) window.setTimeout(pollProgress,2500);
  }
  window.setTimeout(pollProgress,1500);
}

async function init(){
  const hash=new URLSearchParams(location.hash.slice(1));
  const capability=hash.get("access");
  if(capability){
    history.replaceState(null,"",location.pathname+location.search);
    if(!token){
      const auth=await fetch("/v2/auth",{method:"POST",credentials:"same-origin",headers:{Authorization:"Bearer "+capability}});
      if(!auth.ok) throw new Error("请用项目根目录的“启动面试工作台.cmd”重新打开本机工作台。");
    }
  }
  if(token) await candidate();
  else if(sessionId)await showSession();
  else if(batchId)await showBatch();
  else await dashboard();
}
init().catch(error=>{root.replaceChildren();notice(error.message,true);}).finally(()=>root.setAttribute("aria-busy","false"));
