/* Synthetic DOM/event tests only: no browser, server, participant data, or cloud calls. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=file=>fs.readFileSync(path.resolve(__dirname,'../site',file),'utf8');
const tick=async()=>{for(let i=0;i<40;i++)await Promise.resolve();await new Promise(resolve=>setImmediate(resolve));};
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve};};
class Node {
  constructor(tag,doc){this.tagName=tag.toUpperCase();this.ownerDocument=doc;this.children=[];this.parentElement=null;this.listeners=new Map();this.attributes={};this.dataset={};this.style={};this.className='';this._text='';this.disabled=false;}
  get isConnected(){return this===this.ownerDocument?.body||Boolean(this.parentElement?.isConnected);}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
  set textContent(v){this.replaceChildren();this._text=String(v);}
  set innerHTML(_){throw new Error('Unsafe HTML write');}
  get value(){return this._value??(this.tagName==='SELECT'?this.children[0]?.value||'':'');}
  set value(v){this._value=String(v);}
  get classList(){return{add:v=>this.className+=' '+v};}
  append(...nodes){for(let n of nodes){if(typeof n==='string')n=this.ownerDocument.createTextNode(n);n.remove();n.parentElement=this;this.children.push(n);}}
  remove(){if(this.parentElement)this.parentElement.children=this.parentElement.children.filter(n=>n!==this);this.parentElement=null;}
  replaceChildren(...nodes){for(const n of this.children)n.parentElement=null;this.children=[];this._text='';this.append(...nodes);}
  setAttribute(k,v){this.attributes[k]=String(v);if(k==='id')this.id=String(v);}
  addEventListener(k,fn){if(!this.listeners.has(k))this.listeners.set(k,[]);this.listeners.get(k).push(fn);}
  fire(type,extra={}){const e={type,target:this,currentTarget:this,bubbles:true,defaultPrevented:false,preventDefault(){this.defaultPrevented=true;},...extra};this.dispatch(e);return e;}
  dispatch(e){e.currentTarget=this;for(const fn of this.listeners.get(e.type)||[])fn(e);if(e.bubbles)this.parentElement?.dispatch(e);}
  click(){if(!this.disabled)this.fire('click');}
  matches(s){const checked=s.endsWith(':checked');if(checked&&!this.checked)return false;s=s.replace(':checked','');const attr=s.match(/\[([^=\]]+)(?:=["']?([^\]"']*)["']?)?\]/);if(attr){const key=attr[1],value=key.startsWith('data-')?this.dataset[key.slice(5).replace(/-([a-z])/g,(_,c)=>c.toUpperCase())]:this[key]??this.attributes[key];if(attr[2]==null?value==null:String(value)!==attr[2])return false;s=s.replace(attr[0],'');}const id=s.match(/#([\w-]+)/);if(id&&this.id!==id[1])return false;const cls=s.match(/\.([\w-]+)/);if(cls&&!this.className.split(/\s+/).includes(cls[1]))return false;const tag=s.match(/^[\w-]+/);return!tag||this.tagName===tag[0].toUpperCase();}
  querySelectorAll(s){const out=[];for(const n of this.children){if(n.matches(s))out.push(n);out.push(...n.querySelectorAll(s));}return out;}
  querySelector(s){return this.querySelectorAll(s)[0]||null;}
  scrollIntoView(){}
}
function environment(){
  const doc={createElement(tag){return new Node(tag,this);},createElementNS(_,tag){return this.createElement(tag);},createTextNode(text){const n=this.createElement('');n.textContent=text;return n;},getElementById(id){return this.body.querySelector('#'+id);}};doc.body=doc.createElement('body');
  for(const match of source('research.html').matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)){const n=doc.createElement(match[1]);n.id=match[2];doc.body.append(n);}
  const get=id=>doc.getElementById(id),form=get('annotationForm');
  for(const id of ['annotationExperiment','rater','exposure','annotationRatings','annotationNote'])form.append(get(id));
  const submit=doc.createElement('button');submit.type='submit';form.append(submit);
  for(const value of ['blind_self_declared','ai_seen']){const n=doc.createElement('option');n.value=value;get('exposure').append(n);}
  const hub=new Node('window'),views=[],requests=[];
  const sandbox={Node,document:doc,console,URLSearchParams,Headers,Blob,Response,Date,Set,Map,Number,Array,Math,Promise,setTimeout,clearTimeout,setInterval:()=>0,
    location:{hash:'#session=fixture&token=fixture-token'},crypto:{randomUUID:()=>Math.random().toString()},addEventListener:(...args)=>hub.addEventListener(...args)};
  sandbox.window=sandbox;sandbox.SuitPotentialProfile={mount:()=>({element:doc.createElement('section')})};
  sandbox.SuitEvidenceTimeline={mount:options=>{const element=doc.createElement('section');const view={element,options,loads:0,disposed:false,load:async()=>{assert.ok(element.isConnected,'timeline must be attached before loading');view.loads++;if(env.timelineGate)await env.timelineGate.promise;},dispose:()=>view.disposed=true,seek:async()=>{},openQuote:async()=>{}};views.push(view);return view;}};
  const context=vm.createContext(sandbox),env={doc,get,sandbox,context,views,requests,submit,fire:type=>hub.fire(type),run:file=>vm.runInContext(source(file),context)};env.run('js/multimodal-view.js');return env;
}
const answer=id=>({answer_id:id,question_id:'q',transcript_revision:2,transcript:'我负责。我负责。后来完成。',current:true});
function run(id,experiment,mm=false){return{run_id:id,experiment_id:experiment,status:'succeeded',condition:experiment,modules:['word_frequency'],repeat_index:1,created_at:'2026-10-24',input:{condition:experiment,input_versions:{[id+'-answer']:2},answers:[answer(id+'-answer')],questions:[{question_id:'q',text:'合成问题 '+experiment}]},result:{duration_seconds:1,semantic:[],word_frequency:[],multimodal:mm?[{answer_id:id+'-answer',transcript_revision:2,modules:{audio:{status:'complete',observations:[{start_ms:0,end_ms:1000,quality:'valid',values:{rms:-20}}]}}}]:[],errors:[]}};}
function wire(env){
  env.runs=[run('r1','e1',true),run('r2','e2')];env.gates=new Map();env.posts=[];
  env.report={version:1,answers:[answer('r1-answer')],potential_profile:{factors:[]}};
  env.sandbox.fetch=async(url,options={})=>{env.requests.push({url,options});let data;
    if(options.method==='POST'){const body=JSON.parse(options.body);env.posts.push(body);data={};if(url.endsWith('/research/runs')){const queued={...run('new','new-experiment'),status:'queued',modules:body.modules,input_versions:Object.fromEntries(body.selections.map(a=>[a.answer_id,a.transcript_revision]))};env.runs.unshift(queued);data={runs:[queued],experiment:{experiment_id:'new-experiment'}};}if(url.endsWith('/research/publish')){env.report.version++;env.report.research_appendix={selected_runs:body.run_ids.map(run_id=>({run_id}))};data=env.report;}}
    else if(url.endsWith('/report'))data=env.report;
    else if(url.endsWith('/research/runs'))data={runs:env.runs,experiments:[{experiment_id:'e1',purpose:'one'},{experiment_id:'e2',purpose:'two'}]};
    else if(url.endsWith('/research/annotations'))data={annotations:[]};
    else if(url.includes('/research/runs/')){const id=url.split('/').pop();if(env.gates.has(id))await env.gates.get(id).promise;data=env.runs.find(r=>r.run_id===id);}
    else throw new Error('Unexpected request '+url);
    return new Response(JSON.stringify(data),{status:200});
  };
}
const buttons=(env,label)=>env.doc.body.querySelectorAll('button').filter(b=>b.textContent===label);
const field=(env,name)=>env.get('annotationRatings').querySelector('[data-field='+name+']');
const change=(env,id,value)=>{env.get(id).value=value;env.get(id).fire('change');};
let count=0;async function check(name,fn){await fn();count++;console.log('PASS '+name);}
(async()=>{
  await check('Gaze360 and EMOTIC remain explicit optional modules while OpenFace is identified',async()=>{
    const env=environment();wire(env);env.run('js/research.js');await tick();const choices=env.get('moduleSelections').querySelectorAll('input'),byName=Object.fromEntries(choices.map(c=>[c.value,c]));assert.equal(byName.gaze360.checked,false);assert.equal(byName.emotic.checked,false);assert.equal(byName.gaze.checked,true);assert.match(env.get('moduleSelections').textContent,/OpenFace 视线几何/);assert.match(env.get('moduleSelections').textContent,/EMOTIC 感知情绪/);assert.equal(env.posts.length,0);
  });
  await check('Run timeline is connected before load; late older result cannot replace newer selection',async()=>{
    const env=environment();wire(env);env.run('js/research.js');await tick();buttons(env,'查看结果')[0].click();await tick();assert.equal(env.views[0].loads,1);assert.ok(env.get('runDetail').textContent.includes('多模态分析'));
    const gate=deferred();env.gates.set('r1',gate);buttons(env,'查看结果')[0].click();await tick();buttons(env,'查看结果')[1].click();await tick();assert.match(env.get('runDetail').textContent,/e2/);gate.resolve();await tick();assert.match(env.get('runDetail').textContent,/e2/);assert.ok(!env.get('runDetail').textContent.includes('e1 ·'));
  });
  await check('Late timeline completion cannot append old run details after a switch',async()=>{
    const env=environment();wire(env);env.run('js/research.js');await tick();env.timelineGate=deferred();buttons(env,'查看结果')[0].click();await tick();buttons(env,'查看结果')[1].click();await tick();env.timelineGate.resolve();await tick();assert.match(env.get('runDetail').textContent,/e2/);assert.ok(!env.get('runDetail').textContent.includes('多模态分析'));assert.ok(env.views[0].disposed);
  });
  await check('Annotation drafts remain per experiment, repeated quote is disclosed, and save binds frozen experiment',async()=>{
    const env=environment();wire(env);env.run('js/research.js');await tick();field(env,'reason').value='e1 未保存理由';field(env,'reason').fire('input');field(env,'quote').value='我负责。';field(env,'quote').fire('input');assert.match(field(env,'quoteHint').textContent,/出现 2 次/);change(env,'rater','teacher-e1');change(env,'annotationNote','e1 note');assert.ok(env.fire('beforeunload').defaultPrevented);
    change(env,'annotationExperiment','e2');await tick();assert.equal(field(env,'reason').value,'');field(env,'reason').value='e2 理由';field(env,'reason').fire('input');change(env,'annotationNote','e2 note');change(env,'annotationExperiment','e1');await tick();assert.equal(field(env,'reason').value,'e1 未保存理由');assert.equal(env.get('rater').value,'teacher-e1');assert.equal(env.get('annotationNote').value,'e1 note');assert.equal(field(env,'quote').value,'我负责。');
    env.get('annotationForm').fire('submit');await tick();assert.equal(env.posts[0].experiment_id,'e1');assert.equal(env.posts[0].ratings[0].evidence[0].answer_id,'r1-answer');assert.ok(env.fire('beforeunload').defaultPrevented,'other experiment draft remains dirty');
  });
  await check('Out-of-order experiment loads cannot show or submit a mismatched frozen input',async()=>{
    const env=environment();wire(env);env.run('js/research.js');await tick();const gate=deferred();env.gates.set('r2',gate);change(env,'annotationExperiment','e2');await tick();assert.ok(env.submit.disabled);env.get('annotationForm').fire('submit');await tick();assert.equal(env.posts.length,0);change(env,'annotationExperiment','e1');await tick();gate.resolve();await tick();assert.equal(field(env,'answer').value,'r1-answer');assert.equal(env.get('annotationExperiment').value,'e1');
  });
  await check('Exposure follows viewed experiment only and cannot be self-reset to blind after viewing',async()=>{
    const env=environment();wire(env);env.run('js/research.js');await tick();buttons(env,'查看结果')[1].click();await tick();assert.equal(env.get('exposure').value,'blind_self_declared');change(env,'annotationExperiment','e2');await tick();assert.equal(env.get('exposure').value,'ai_seen');change(env,'exposure','blind_self_declared');assert.equal(env.get('exposure').value,'ai_seen');
  });
  await check('Alignment editor mounts once per selected version and preserves its idempotency key',async()=>{
    const env=environment();wire(env);const editors=[];env.sandbox.SuitAlignmentEditor={mount:options=>{const editor={element:env.doc.createElement('details'),load:async()=>{},options};editors.push(editor);return editor;}};env.run('js/research.js');await tick();buttons(env,'打开此版本时间线')[0].click();await tick();const timeline=env.views[0];buttons(env,'校准此版本句段时间')[0].click();await tick();buttons(env,'校准此版本句段时间')[0].click();await tick();assert.equal(editors.length,1);assert.equal(editors[0].options.revision,2);assert.ok(editors[0].element.isConnected);
    await editors[0].options.save({revision:2,idempotency_key:'editor-owned-key'});assert.equal(env.posts[0].idempotency_key,'editor-owned-key');editors[0].options.onSaved();assert.ok(timeline.disposed);assert.ok(!timeline.element.isConnected);buttons(env,'打开此版本时间线')[0].click();await tick();assert.equal(env.views.length,2);
  });
  await check('Published summary shows semantic evidence, partial status and errors; optional callback is safe',async()=>{
    const env=environment();env.run('js/research-summary.js');const ref={answer_id:'a',transcript_revision:2,quote:'<b>原话</b>'};const item={evaluation:{question_id:'q',rating:{score:3,reason:'研究理由',evidence:[ref]},followup:{reason:'缺口理由',evidence:[ref]},evidence_units:[{category:'own_action',interpretation:'具体行动',evidence:[ref]}],criterion_matches:[{criterion_id:'c',reason:'符合证据'}],gaps:[{description:'缺少结果'}]},provider:{model:'fixture'}};const appendix={note:'独立实验',selected_runs:[{run_id:'run',status:'partial',condition:'fixture',result:{semantic:[item],errors:[{module:'au',reason:'模型超时'}]}}]};
    let actual;const mounted=env.sandbox.SuitResearchSummary.mount({appendix,onEvidence:r=>actual=r}).element;env.doc.body.append(mounted);assert.match(mounted.textContent,/部分完成/);assert.match(mounted.textContent,/研究评分草稿 3/);assert.match(mounted.textContent,/模型超时/);assert.match(mounted.textContent,/缺少结果/);assert.equal(mounted.querySelector('b'),null);mounted.querySelector('button').click();await tick();assert.equal(actual,ref);
    const plain=env.sandbox.SuitResearchSummary.mount({appendix}).element;assert.equal(plain.querySelectorAll('button').length,0);assert.match(plain.textContent,/原话/);
  });
  await check('Empty session has an explicit identity and no enabled analysis or publication path',async()=>{
    const env=environment();wire(env);env.report.answers=[];env.runs=[];env.run('js/research.js');await tick();assert.match(env.get('quickStatus').textContent,/空场次/);assert.match(env.get('quickSessionTitle').textContent,/fixture/);assert.equal(env.get('researchDetails').hidden,true);assert.equal(env.get('quickAnalyze').disabled,true);assert.equal(env.get('quickPublish').disabled,true);env.get('quickAnalyze').click();await tick();assert.equal(env.posts.length,0);
  });
  await check('One-click analysis freezes only current media, sends no cloud module, and blocks double submission',async()=>{
    const env=environment();wire(env);env.runs=[];env.report.version=8;env.report.answers=[{...answer('current'),has_media:true},{...answer('text-only'),has_media:false},{...answer('replaced'),has_media:true,current:false}];env.run('js/research.js');await tick();env.get('quickAnalyze').click();env.get('quickAnalyze').click();await tick();assert.equal(env.posts.length,1);assert.deepEqual(env.posts[0].selections,[{answer_id:'current',transcript_revision:2}]);assert.deepEqual(env.posts[0].modules,['au','gaze','expression','audio']);assert.equal(env.posts[0].cloud_consent,false);assert.equal(env.posts[0].version,8);assert.equal(env.get('quickAnalyze').disabled,true);assert.match(env.get('quickStatus').textContent,/无需重复提交/);
  });
  await check('Quick publication requires a finished run matching all current media versions and preserves report identity',async()=>{
    const env=environment();wire(env);env.report.answers=[{...answer('a'),has_media:true}];env.runs=[{...run('ready','e1'),modules:['au','gaze','expression','audio'],input_versions:{a:1}}];env.run('js/research.js');await tick();assert.equal(env.get('quickPublish').disabled,true);env.runs[0].input_versions.a=2;env.get('refreshRuns').click();await tick();assert.equal(env.get('quickPublish').disabled,false);env.get('quickPublish').click();await tick();assert.equal(env.posts.length,1);assert.deepEqual(env.posts[0].run_ids,['ready']);assert.equal(env.report.version,2);assert.equal(env.get('quickPublish').disabled,true);assert.match(env.get('quickPublish').textContent,/已加入/);
    assert.equal(env.get('quickStatus').textContent,'本场音视频分析已完成，已加入报告 v2。可查看本场分析结果，或从上方“本场报告”查看汇总。');
    const other=environment();wire(other);other.report.answers=[{...answer('a'),has_media:true}];other.runs=[{...run('ready','e1'),modules:['au','gaze','expression','audio'],input_versions:{a:2}}];other.run('js/research.js');await tick();other.report.research_appendix={selected_runs:[{run_id:'ready'}]};other.report.version++;other.get('quickPublish').click();await tick();assert.equal(other.posts.length,0,'server already published after another tab or lost response; do not create another report');assert.match(other.get('notice').textContent,/已经加入/);assert.match(other.get('quickStatus').textContent,/已加入报告 v2/);assert.ok(!other.get('quickStatus').textContent.includes('再决定'));
    other.runs[0].status='partial';other.get('refreshRuns').click();await tick();assert.match(other.get('quickStatus').textContent,/部分完成，已加入报告 v2/);
  });
  await check('Role hash changes reload the captured session; plain section anchors do not',async()=>{
    const env=environment();wire(env);let reloads=0;env.sandbox.location.reload=()=>reloads++;env.run('js/research.js');await tick();env.sandbox.location.hash='#section';env.fire('hashchange');assert.equal(reloads,0);env.sandbox.location.hash='#session=other&token=other-token';env.fire('hashchange');assert.equal(reloads,1);
  });
  await check('Automatic results remain visible without requiring or publishing another research run',async()=>{
    const env=environment();wire(env);env.report.answers=[{...answer('a'),has_media:true}];const auto={...run('auto','e1'),answer_id:'a',transcript_revision:2,modules:['au','gaze','expression','audio'],input_versions:{a:2}};env.runs=[auto];env.report.standard_analysis={enabled:true,runs:[auto]};env.run('js/research.js');await tick();assert.match(env.get('automaticRuns').textContent,/查看这段自动分析/);assert.equal(env.get('quickPublish').disabled,true);assert.match(env.get('quickStatus').textContent,/上方自动分析照常可用/);assert.equal(env.posts.length,0);
  });
  console.log(count+' research UI regressions passed.');
})().catch(error=>{console.error(error);process.exitCode=1;});
