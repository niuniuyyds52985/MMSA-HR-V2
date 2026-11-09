/* Synthetic VM + Fake DOM event regression. No browser, server, real media, or cloud calls. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const site = path.resolve(__dirname,'../site');
const source = file => fs.readFileSync(path.join(site,file),'utf8');
const checks = [];
const tick = async () => { for (let i = 0; i < 30; i++) await Promise.resolve(); await new Promise(resolve => setImmediate(resolve)); };
const deferred = () => { let resolve, reject; const promise = new Promise((yes,no) => { resolve = yes; reject = no; }); return {promise,resolve,reject}; };
async function check(name, fn) { await fn(); checks.push(name); console.log('PASS ' + name); }
class FakeNode {
  constructor(tag = '', doc = null) { this.tagName = tag.toUpperCase(); this.ownerDocument = doc; this.children = []; this.parentElement = null; this.listeners = new Map(); this.attributes = {}; this.className = ''; this.hidden = false; this.disabled = false; this.checked = false; this.style = {}; this.dataset = {}; this.paused = true; this.currentTime = 0; this.readyState = 0; this._value = undefined; this._text = ''; }
  get isConnected() { return this === this.ownerDocument?.body || Boolean(this.parentElement?.isConnected); }
  get classList() { const self = this; return {contains:value => self.className.split(/\s+/).includes(value),toggle(value,on) { const names = new Set(self.className.split(/\s+/).filter(Boolean)); const enabled = on == null ? !names.has(value) : on; enabled ? names.add(value) : names.delete(value); self.className = [...names].join(' '); return enabled; },add(value) { this.toggle(value,true); },remove(value) { this.toggle(value,false); }}; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  set textContent(value) { this.replaceChildren(); this._text = String(value); }
  set innerHTML(_) { throw new Error('Unsafe innerHTML write'); }
  get value() { return this._value ?? (this.tagName === 'SELECT' ? this.children[0]?.value || '' : ''); }
  set value(value) { this._value = String(value); }
  get src() { return this.attributes.src || ''; }
  set src(value) { this.attributes.src = value; if (['AUDIO','VIDEO'].includes(this.tagName)) queueMicrotask(() => { this.readyState = 1; this.fire('loadedmetadata'); }); }
  append(...nodes) { this._text = this._text || ''; for (let node of nodes) { if (typeof node === 'string') node = this.ownerDocument.createTextNode(node); node.remove(); node.parentElement = this; this.children.push(node); } }
  replaceChildren(...nodes) { for (const node of this.children) node.parentElement = null; this.children = []; this._text = ''; this.append(...nodes); }
  remove() { if (this.parentElement) { const parent = this.parentElement; parent.children = parent.children.filter(node => node !== this); this.parentElement = null; } }
  setAttribute(name,value) { this.attributes[name] = String(value); if (name === 'class') this.className = String(value); if (name === 'id') this.id = String(value); }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(type,fn,options) { if (!this.listeners.has(type)) this.listeners.set(type,[]); this.listeners.get(type).push({fn,once:options?.once}); }
  removeEventListener(type,fn) { this.listeners.set(type,(this.listeners.get(type) || []).filter(listener => listener.fn !== fn)); }
  fire(type,properties = {}) { const event = {type,target:this,currentTarget:this,bubbles:true,stopped:false,preventDefault() {},stopPropagation() { this.stopped = true; },...properties}; this.dispatchEvent(event); return event; }
  dispatchEvent(event) { event.currentTarget = this; for (const listener of [...(this.listeners.get(event.type) || [])]) { if (listener.once) this.removeEventListener(event.type,listener.fn); listener.fn(event); } if (event.bubbles && !event.stopped) this.parentElement?.dispatchEvent(event); }
  click() { if (!this.disabled) this.fire('click'); }
  contains(node) { return node === this || this.children.some(child => child.contains(node)); }
  matches(selector) {
    if (selector.includes(',')) return selector.split(',').some(value => this.matches(value.trim()));
    const checked = selector.endsWith(':checked'); if (checked && !this.checked) return false;
    selector = selector.replace(':checked','');
    const attr = selector.match(/\[([^=\]]+)(?:="([^"]*)")?\]/); if (attr) { const actual = this[attr[1]] ?? this.attributes[attr[1]]; if (attr[2] == null ? actual == null : String(actual) !== attr[2]) return false; selector = selector.replace(attr[0],''); }
    const id = selector.match(/#([\w-]+)/); if (id && this.id !== id[1]) return false;
    const classes = [...selector.matchAll(/\.([\w-]+)/g)]; if (classes.some(item => !this.classList.contains(item[1]))) return false;
    const tag = selector.match(/^[\w-]+/); return !tag || this.tagName === tag[0].toUpperCase();
  }
  querySelectorAll(selector) { const result = []; for (const child of this.children) { if (child.matches(selector)) result.push(child); result.push(...child.querySelectorAll(selector)); } return result; }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  scrollIntoView() { this.scrolled = true; }
  focus() { this.focused = true; }
  async play() { this.paused = false; this.fire('play'); }
  pause() { this.paused = true; }
  load() {}
}
function environment(htmlFile) {
  const document = {createElement(tag) { return new FakeNode(tag,this); },createElementNS(_,tag) { return this.createElement(tag); },createTextNode(text) { const node = new FakeNode('',this); node.textContent = text; return node; },getElementById(id) { return this.body.querySelector('#' + id); },querySelectorAll(selector) { return this.body.querySelectorAll(selector); },querySelector(selector) { return this.body.querySelector(selector); }};
  document.body = document.createElement('body');
  if (htmlFile) for (const match of source(htmlFile).matchAll(/<([a-z][a-z0-9]*)\b[^>]*\bid="([^"]+)"[^>]*>/g)) { const node = document.createElement(match[1]); node.id = match[2]; document.body.append(node); }
  const urls = {created:[],revoked:[]}, eventHub = new FakeNode();
  const sandbox = {document,console,URLSearchParams,Headers,Blob,Response,Date,Set,Map,Number,Array,Math,Promise,setTimeout,clearTimeout,queueMicrotask,
    crypto:{randomUUID:() => 'synthetic-uuid'},location:{hash:'#session=synthetic-session&token=synthetic-token',href:'http://fixture/#synthetic',pathname:'/'},history:{replaceState() {}},sessionStorage:{getItem() { return null; },setItem() {}},
    URL:{createObjectURL() { const value = 'blob:synthetic-' + urls.created.length; urls.created.push(value); return value; },revokeObjectURL(value) { urls.revoked.push(value); }},
    addEventListener: (...args) => eventHub.addEventListener(...args),getSelection:() => null,print() {}};
  sandbox.window = sandbox; const context = vm.createContext(sandbox); vm.runInContext(source('js/text-display.js'),context); vm.runInContext(source('js/evidence-timeline.js'),context);
  vm.runInContext(source('js/research-summary.js'),context);
  vm.runInContext(source('js/report-reader.js'),context);
  sandbox.SuitPotentialProfile={mount:()=>({element:document.createElement('section')})};
  return {document,sandbox,context,urls,run:file => vm.runInContext(source(file),context),fire:type => eventHub.fire(type)};
}
function timeline(overrides = {}) {
  return {schema_version:'interview-timeline.v1',session_id:'synthetic-session',answer_id:'answer-1',transcript_revision:2,current_transcript_revision:2,is_current_revision:true,answer_current:true,text:'🙂我做了。然后我做了。',media:{available:true},alignment:{status:'aligned',available_asr_revisions:[2]},segments:[
    {segment_id:'one',index:0,text:'🙂我做了。',char_start:0,char_end:5,start_ms:0,end_ms:2000,alignment_status:'aligned'},
    {segment_id:'two',index:1,text:'然后我做了。',char_start:5,char_end:11,start_ms:3000,end_ms:5000,alignment_status:'aligned'}
  ],observations:{audio:{status:'available',unit:'dBFS',windows:[{start_ms:0,end_ms:2000,rms_dbfs:-30},{start_ms:3000,end_ms:5000,rms_dbfs:-20}]}},references:[],...overrides};
}
function mount(env,data = timeline(),extra = {}) {
  const calls = {timeline:[],media:0};
  const view = env.sandbox.SuitEvidenceTimeline.mount({answer:{answer_id:data.answer_id,transcript_revision:data.transcript_revision,has_media:data.media.available,media_type:'audio/wav'},sessionId:'synthetic-session',fetchJSON:async revision => { calls.timeline.push(revision); return data; },fetchMedia:async () => { calls.media++; return new Blob(['synthetic media fixture'],{type:'audio/wav'}); },...extra});
  env.document.body.append(view.element); return {view,calls};
}
function report() {
  const answer = {answer_id:'answer-1',question_id:'leadership_F3_Q1',current:true,transcript_revision:2,transcript:'🙂我做了。然后我做了。',has_media:true,media_type:'audio/wav',revisions:[]};
  const evidence = {answer_id:answer.answer_id,transcript_revision:2,quote:'🙂我做了。'};
  const question = {question_id:answer.question_id,factor_id:'leadership',text:'合成问题',answers:[answer],score:3,ai_score:3,score_source:'model_draft',reason:'合成评分理由',evidence:[evidence],anchors:{1:'较少具体证据',3:'存在具体行动',5:'行动与结果充分'},model_evaluation:{rating:{reason:'合成模型理由',evidence:[evidence]},followup:{reason:'合成追问缺口',evidence:[evidence]}}};
  return {session_id:'synthetic-session',candidate_ref:'合成测试夹具',version:4,status:'answering',answers:[answer],questions:[question],coverage:{planned_factor_ids:['leadership'],uncovered_factor_ids:['learning']},current_round:{round_id:'round-1',question_id:answer.question_id,kind:'main'},reviews:[],assessments:[],rounds:[],decision_events:[],jobs:[]};
}
function wireFetch(env,requests,readReport = report) {
  env.sandbox.fetch = async (url,options = {}) => {
    requests.push({url,method:options.method || 'GET'});
    if (url.includes('/timeline?')) { const revision = Number(url.split('transcript_revision=')[1]); return new Response(JSON.stringify(timeline({transcript_revision:revision,current_transcript_revision:3,is_current_revision:false,references:[{source:'model_rating',quote:'LATER RECORD',reason:'later result',transcript_revision:revision}]})),{status:200}); }
    if (url.includes('/media/')) return new Response(new Blob(['synthetic media fixture'],{type:'audio/wav'}),{status:200});
    if (url.endsWith('/report')) return new Response(JSON.stringify(readReport()),{status:200});
    if (url.includes('/factor-profile?')) return new Response(JSON.stringify({factors:[]}),{status:200});
    throw new Error('Unexpected fixture request: ' + url);
  };
}
(async () => {
  await check('Unicode offsets, repeated quotes, whitespace across segments, stale alignment',async () => {
    const env = environment(), find = env.sandbox.SuitEvidenceTimeline.locateQuote;
    const segments = [{segment_id:'a',text:'🙂Hello',char_start:0,char_end:6,start_ms:0,end_ms:1000,alignment_status:'aligned'},{segment_id:'space',text:' ',char_start:6,char_end:7,start_ms:null,end_ms:null,alignment_status:'unaligned'},{segment_id:'b',text:'world',char_start:7,char_end:12,start_ms:1200,end_ms:2000,alignment_status:'aligned'}];
    const span = find('🙂Hello world','Hello world',segments)[0]; assert.equal(span.char_start,1); assert.equal(span.end_ms,2000);
    assert.equal(find('🙂Hello world','🙂Hello',segments)[0].char_end,6);
    assert.deepEqual(Array.from(find('🙂同句同句','同句',[]),item => item.char_start),[1,3]);
    assert.equal(find('🙂Hello world','Hello world',segments.map(item => ({...item,start_ms:null,end_ms:null,alignment_status:'unaligned'})))[0].start_ms,null);
  });

  await check('Simplified timeline keeps canonical Unicode offsets, original data and media time',async () => {
    const env = environment(), first = '🙂𨋢我負責協調。', second = '後來完成任務。', boundary = Array.from(first).length;
    const data = timeline({text:first + second,segments:[
      {segment_id:'traditional-one',text:first,char_start:0,char_end:boundary,start_ms:1500,end_ms:3200,alignment_status:'aligned'},
      {segment_id:'traditional-two',text:second,char_start:boundary,char_end:boundary + Array.from(second).length,start_ms:4000,end_ms:6000,alignment_status:'aligned'}
    ]});
    const original = JSON.stringify(data), freeze = value => { if (value && typeof value === 'object') { Object.values(value).forEach(freeze); Object.freeze(value); } }; freeze(data);
    const {view,calls} = mount(env,data); await view.load();
    const texts = view.element.querySelectorAll('.evidence-segment-text');
    assert.equal(texts[0].textContent,'🙂䢂我负责协调。'); assert.equal(texts[1].textContent,'后来完成任务。'); assert.equal(calls.media,0);
    const resolved = env.sandbox.SuitTextDisplay.resolveOriginalQuote(data.text,'负责协调');
    assert.equal(resolved.quote,'負責協調'); assert.equal(resolved.char_start,3); assert.equal(resolved.char_end,7);
    assert.equal(env.sandbox.SuitEvidenceTimeline.locateQuote(data.text,'负责协调',data.segments).length,0,'display spelling must not replace canonical quote matching');
    await view.openQuote({answer_id:'answer-1',transcript_revision:2,...resolved,segment_id:'traditional-one'});
    assert.equal(view.element.querySelector('mark').textContent,'负责协调'); assert.equal(texts[0].textContent,'🙂䢂我负责协调。');
    assert.equal(env.document.querySelector('audio').currentTime,1.5); assert.equal(JSON.stringify(data),original); assert.equal(resolved.quote,'負責協調');
    view.dispose();
  });
  await check('Highlighting a partial 乾隆 phrase preserves its whole-segment conversion context',async () => {
    const env = environment(), text = '🙂我讀過乾隆的書，參觀乾清宮。', characters = Array.from(text), start = characters.indexOf('乾');
    const data = timeline({text,media:{available:false},segments:[{segment_id:'protected-name',text,char_start:0,char_end:characters.length,start_ms:null,end_ms:null,alignment_status:'unaligned'}]});
    const {view} = mount(env,data); await view.load();
    const row = view.element.querySelector('.evidence-segment-text'), expected = '🙂我读过乾隆的书，参观乾清宫。'; assert.equal(row.textContent,expected);
    await view.openQuote({answer_id:'answer-1',transcript_revision:2,quote:'乾',char_start:start,char_end:start + 1,segment_id:'protected-name'});
    assert.equal(view.element.querySelector('mark').textContent,'乾'); assert.equal(row.textContent,expected,'must convert the complete source before splitting highlight fragments');
    await view.openQuote({answer_id:'answer-1',transcript_revision:2,quote:'缺失原文'});
    assert.equal(view.element.querySelector('mark'),null); assert.equal(row.textContent,expected); assert.equal(data.text,text); view.dispose();
  });
  await check('Script-equivalent display occurrences retain distinct canonical evidence locations',async () => {
    const env = environment(), first = '我負責。', second = '我负责。', text = first + second;
    const data = timeline({text,media:{available:false},segments:[
      {segment_id:'traditional',text:first,char_start:0,char_end:4,start_ms:null,end_ms:null,alignment_status:'unaligned'},
      {segment_id:'simplified',text:second,char_start:4,char_end:8,start_ms:null,end_ms:null,alignment_status:'unaligned'}
    ]});
    const {view} = mount(env,data); await view.load();
    assert.equal(env.sandbox.SuitTextDisplay.resolveOriginalQuote(text,'负责'),null,'ambiguous displayed selection must not silently choose the first original');
    await view.openQuote({answer_id:'answer-1',transcript_revision:2,quote:'負責'});
    assert.equal(view.element.querySelector('.is-selected').querySelector('.evidence-segment-text').textContent,'我负责。');
    assert.equal(view.element.querySelectorAll('.evidence-segment')[0].classList.contains('is-selected'),true);
    await view.openQuote({answer_id:'answer-1',transcript_revision:2,quote:'负责'});
    assert.equal(view.element.querySelectorAll('.evidence-segment')[1].classList.contains('is-selected'),true);
    assert.equal(view.element.querySelectorAll('.evidence-segment')[0].classList.contains('is-selected'),false); assert.equal(data.text,text); view.dispose();
  });
  await check('HR simplified selection fills original evidence without rewriting the transcript or draft',async () => {
    const env = environment('hr-review.html'), requests = [], data = report(), canonical = '🙂我負責協調並完成任務。';
    data.answers[0].transcript = canonical; data.questions[0].evidence = []; data.questions[0].model_evaluation.rating.evidence = []; data.questions[0].model_evaluation.followup.evidence = [];
    wireFetch(env,requests,() => data); env.run('js/hr-review.js'); await tick();
    const transcript = env.document.querySelector('.answer-text'), reason = env.document.querySelector('.rating-reason');
    assert.equal(transcript.textContent,'🙂我负责协调并完成任务。'); reason.value = '老師尚未保存的理由';
    env.sandbox.getSelection = () => ({toString:() => '负责协调',anchorNode:transcript,focusNode:transcript});
    env.document.querySelectorAll('button').find(button => button.textContent === '引用选中文字').click();
    assert.equal(env.document.querySelector('.quote-field').value,'負責協調'); assert.equal(reason.value,'老師尚未保存的理由'); assert.equal(data.answers[0].transcript,canonical);
    assert.ok(requests.every(item => item.method === 'GET')); env.fire('pagehide');
  });
  await check('Lazy read and repeated quote disambiguation do not load media',async () => {
    const env = environment(), {view,calls} = mount(env);
    assert.equal(calls.timeline.length,0); await view.load(); assert.equal(calls.timeline.length,1); assert.equal(calls.media,0);
    await view.openQuote({answer_id:'answer-1',transcript_revision:2,quote:'我做了。'});
    assert.equal(view.element.querySelector('.evidence-match-choices').children.length,2); assert.equal(calls.media,0);
    view.element.querySelector('.evidence-match-choices').children[1].click(); await tick();
    assert.equal(env.document.querySelector('audio').currentTime,3);
    assert.match(view.element.querySelector('.is-active').textContent,/然后/); assert.equal(view.element.querySelector('mark').textContent,'我做了。'); view.dispose();
  });
  await check('Player seek, RMS click, explicit loop and loop off share one player',async () => {
    const env = environment(), {view} = mount(env); await view.seek(0);
    const player = env.document.querySelector('audio'); player.currentTime = 3.5; player.fire('seeked');
    assert.match(view.element.querySelector('.evidence-segment.is-active').textContent,/然后/);
    assert.equal(view.element.querySelectorAll('.evidence-energy-point.is-active').length,1);
    view.element.querySelectorAll('.evidence-energy-point')[0].click(); await tick(); assert.equal(player.currentTime,0);
    const loop = view.element.querySelector('input'); loop.checked = true; loop.fire('change'); await tick();
    player.currentTime = 2; player.fire('timeupdate'); assert.equal(player.currentTime,0);
    loop.checked = false; loop.fire('change'); player.currentTime = 2; player.fire('timeupdate'); assert.equal(player.currentTime,2); view.dispose();
  });
  await check('Playback follows the active sentence inside its transcript viewport without scrolling the page',async()=>{
    const env=environment(),{view}=mount(env);await view.seek(0,{play:false});
    const holder=view.element.querySelector('.evidence-segments'),rows=holder.querySelectorAll('.evidence-segment'),player=env.document.querySelector('audio');
    holder.clientHeight=120;holder.scrollHeight=400;holder.scrollTop=0;holder.getBoundingClientRect=()=>({top:100,bottom:220});
    rows[0].getBoundingClientRect=()=>({top:100-holder.scrollTop,bottom:140-holder.scrollTop});rows[1].getBoundingClientRect=()=>({top:260-holder.scrollTop,bottom:300-holder.scrollTop});
    const nodes=[env.document.body,...env.document.querySelectorAll('*')];for(const node of nodes)node.scrolled=false;
    player.currentTime=3.2;player.fire('timeupdate');assert.equal(holder.scrollTop,88);assert.equal(rows[1].getAttribute('aria-current'),'true');assert.ok(nodes.every(node=>!node.scrolled));
    holder.scrollTop=0;player.currentTime=3.4;player.fire('timeupdate');assert.equal(holder.scrollTop,0,'Manual browsing is not snapped back on every timeupdate');view.dispose();
  });
  await check('Wrong revision, cross-answer, unknown quote, and mismatched payload are rejected',async () => {
    const env = environment(), {view} = mount(env);
    await assert.rejects(view.openQuote({answer_id:'answer-1',transcript_revision:1,quote:'我做了。'}),/版本/);
    await assert.rejects(view.openQuote({answer_id:'other',transcript_revision:2,quote:'我做了。'}),/回答/);
    await view.openQuote({answer_id:'answer-1',transcript_revision:2,quote:'不存在的原话'}); assert.match(view.element.querySelector('.evidence-timeline-status').textContent,/未在/); view.dispose();
    const wrong = mount(env,timeline(),{fetchJSON:async () => timeline({transcript_revision:3})}).view; await assert.rejects(wrong.load(),/版本不一致/); wrong.dispose();
  });
  await check('Latest seek wins one delayed media read; disposal revokes owned URL',async () => {
    const env = environment(), gate = deferred(); let reads = 0;
    const {view} = mount(env,timeline(),{fetchMedia:() => { reads++; return gate.promise; }});
    const first = view.seek(0); await tick(); const second = view.seek(3000); await tick();
    gate.resolve(new Blob(['synthetic'],{type:'audio/wav'})); await Promise.all([first,second]);
    assert.equal(reads,1); assert.equal(env.document.querySelector('audio').currentTime,3);
    view.dispose(); assert.equal(env.urls.created.length,1); assert.equal(env.urls.revoked.length,1);
  });
  await check('Frame seek pauses exactly, refreshes actual capture overlay, and preserves ordinary playback',async () => {
    const env=environment(),{view}=mount(env,timeline(),{answer:{answer_id:'answer-1',transcript_revision:2,has_media:true,media_type:'video/mp4'}});
    env.run('js/multimodal-view.js');const observed=env.sandbox.SuitMultimodalView.mount({timeline:view,data:{modules:{gaze360:{status:'complete',observations:[{start_ms:500,end_ms:1500,centre_ms:500,last_input_ms:875,quality:'valid',values:{direction_xyz:[0,0,-1]},capture:{at_ms:500,duration_ms:33.333,frame_width:640,frame_height:360,boxes:[{kind:'head',x:160,y:40,width:180,height:200}]}}]}}}});env.document.body.append(observed.element);
    await view.loadMedia();const media=view.element.querySelector('video');let plays=0;media.addEventListener('play',()=>plays++);const times=[];view.subscribeTime((ms,p)=>times.push({ms,paused:p?.paused}));
    await view.seek(500,{play:false});assert.equal(media.currentTime,.5);assert.equal(media.paused,true);assert.equal(plays,0);assert.equal(times.at(-1).ms,500);assert.equal(times.at(-1).paused,true);assert.equal(view.element.querySelector('.evidence-player-overlay').querySelectorAll('rect').length,1);assert.match(view.element.querySelector('.evidence-timeline-status').textContent,/已暂停定位/);
    media.fire('seeked');assert.equal(media.paused,true);assert.equal(media.currentTime,.5);assert.equal(view.element.querySelector('.evidence-player-overlay').querySelectorAll('rect').length,1);
    await view.seek(3000);assert.equal(media.paused,false);assert.equal(plays,1);await view.seek(500,{play:false});assert.equal(media.paused,true);assert.equal(plays,1);assert.equal(view.element.querySelector('.evidence-player-overlay').querySelectorAll('rect').length,1);
    await view.openQuote({answer_id:'answer-1',transcript_revision:2,quote:'🙂我做了。'});assert.equal(media.paused,false);assert.equal(plays,2,'Original quote seeks still play by default');observed.dispose();view.dispose();
  });
  await check('Latest paused-frame intent wins a delayed play seek and latest play intent can supersede it',async () => {
    const env=environment(),gate=deferred(),{view}=mount(env,timeline(),{fetchMedia:()=>gate.promise});const old=view.seek(0);await tick();const frame=view.seek(500,{play:false});await tick();gate.resolve(new Blob(['synthetic'],{type:'audio/wav'}));await Promise.all([old,frame]);const media=view.element.querySelector('audio');assert.equal(media.currentTime,.5);assert.equal(media.paused,true);view.dispose();
    const secondEnv=environment(),secondGate=deferred(),next=mount(secondEnv,timeline(),{fetchMedia:()=>secondGate.promise}).view;const oldFrame=next.seek(500,{play:false});await tick();const latestPlay=next.seek(3000);await tick();secondGate.resolve(new Blob(['synthetic'],{type:'audio/wav'}));await Promise.all([oldFrame,latestPlay]);assert.equal(next.element.querySelector('audio').currentTime,3);assert.equal(next.element.querySelector('audio').paused,false);next.dispose();
  });
  await check('Late paused-frame request cannot pause or replace another answer playback',async () => {
    const env=environment(),gate=deferred();let oldReads=0;const old=mount(env,timeline({answer_id:'old'}),{fetchJSON:()=>gate.promise,fetchMedia:async()=>{oldReads++;return new Blob(['old']);}}).view,newer=mount(env,timeline({answer_id:'new'})).view;
    const pending=old.seek(500,{play:false});await tick();await newer.seek(3000);gate.resolve(timeline({answer_id:'old'}));await pending;assert.equal(oldReads,0);assert.equal(newer.element.querySelector('audio').currentTime,3);assert.equal(newer.element.querySelector('audio').paused,false);old.dispose();newer.dispose();
  });
  await check('Cross-answer late timeline cannot steal focus or start old media; new player pauses previous',async () => {
    const env = environment(), gate = deferred(); let oldReads = 0;
    const old = mount(env,timeline({answer_id:'old'}),{fetchJSON:() => gate.promise,fetchMedia:async () => { oldReads++; return new Blob(['old']); }}).view;
    const newer = mount(env,timeline({answer_id:'new'})).view;
    const oldSeek = old.seek(0); await tick(); await newer.seek(3000); gate.resolve(timeline({answer_id:'old'})); await oldSeek;
    assert.equal(oldReads,0); assert.equal(old.element.scrolled,undefined);
    const newPlayer = newer.element.querySelector('audio'); assert.equal(newPlayer.paused,false);
    await old.seek(0); assert.equal(newPlayer.paused,true); old.dispose(); newer.dispose();
  });
  await check('Disposed view ignores late blob and releases no uncreated URL',async () => {
    const env = environment(), gate = deferred(), {view} = mount(env,timeline(),{fetchMedia:() => gate.promise});
    const seek = view.seek(0); await tick(); view.dispose(); view.element.remove(); gate.resolve(new Blob(['synthetic'])); await seek;
    assert.equal(env.urls.created.length,0); assert.equal(env.document.querySelector('audio'),null);
  });
  await check('Pure text and stale correction can locate characters without inventing time; safe text rendering',async () => {
    const text = '<img src=x onerror=alert(1)>🙂修订', base = timeline({text,media:{available:false},alignment:{status:'no_media'},segments:[{segment_id:'t',text,char_start:0,char_end:Array.from(text).length,start_ms:null,end_ms:null,alignment_status:'unaligned'}],observations:{audio:{status:'not_available',reason:'没有录音'}}});
    const env = environment(), {view,calls} = mount(env,base); await view.openQuote({answer_id:'answer-1',transcript_revision:2,quote:'🙂修订'});
    assert.equal(view.element.querySelector('img'),null); assert.equal(view.element.querySelector('mark').textContent,'🙂修订'); assert.equal(calls.media,0);
    assert.match(view.element.querySelector('.evidence-timeline-status').textContent,/没有可用媒体/); view.dispose();
    const stale = mount(env,{...base,media:{available:true},alignment:{status:'stale'}}); await stale.view.openQuote({answer_id:'answer-1',transcript_revision:2,quote:'🙂修订'});
    assert.equal(stale.calls.media,0); assert.match(stale.view.element.querySelector('.evidence-timeline-status').textContent,/没有可靠时间对齐/); stale.view.dispose();
  });
  await check('Media-only answer and invalid RMS keep available playback and honest missing states',async () => {
    const env = environment(), {view} = mount(env,timeline({text:'',segments:[],alignment:{status:'unaligned',reason:'无转写'},observations:{audio:{status:'invalid',reason:'窗口无效'}}}));
    await view.seek(0); assert.ok(env.document.querySelector('audio')); assert.match(view.element.querySelector('.evidence-empty').textContent,/窗口无效/); assert.match(view.element.querySelector('.evidence-timeline-status').textContent,/没有可靠对齐/); view.dispose();
  });
  await check('Original ASR revision buttons use explicit API versions only',async () => {
    const env = environment(), versions = [], {view} = mount(env,timeline({transcript_revision:3,alignment:{status:'stale',asr_transcript_revision:null,available_asr_revisions:[2]}}),{onOpenRevision:version => versions.push(version)});
    await view.load(); const source = view.element.querySelectorAll('button').find(node => node.textContent.includes('原始 ASR')); assert.ok(source); source.click(); assert.deepEqual(versions,[2]); view.dispose();
  });
  await check('Loop control does not bubble into an unsaved review form',async () => {
    const env = environment(), form = env.document.createElement('form'); env.document.body.append(form); let changed = 0; form.addEventListener('input',() => changed++);
    const {view} = mount(env); form.append(view.element); await view.seek(0); view.element.querySelector('input').fire('input'); assert.equal(changed,0); view.dispose();
  });
  await check('HR page wiring preserves rating, quote, review and followup draft values',async () => {
    const env = environment('hr-review.html'), requests = []; wireFetch(env,requests); env.run('js/hr-review.js'); await tick();
    const get = id => env.document.getElementById(id), reason = env.document.querySelector('.rating-reason'), quote = env.document.querySelector('.quote-field'); assert.ok(reason);
    reason.value = '未保存老师理由'; quote.value = '🙂我做了。'; get('reviewReason').value = '未保存复核说明'; get('assessmentQuote').value = '🙂我做了。'; get('assessmentReason').value = '未保存追问说明';
    env.document.querySelector('.quote-item').querySelector('.evidence-reference-link').click(); await tick();
    assert.ok(env.document.querySelector('audio'),JSON.stringify({requests,status:env.document.querySelector('.evidence-reference-error')?.textContent,timeline:env.document.querySelector('.evidence-timeline-status')?.textContent})); assert.equal(reason.value,'未保存老师理由'); assert.equal(quote.value,'🙂我做了。'); assert.equal(get('reviewReason').value,'未保存复核说明'); assert.equal(get('assessmentReason').value,'未保存追问说明');
    get('locateAssessmentQuote').click(); await tick(); assert.equal(get('assessmentQuote').value,'🙂我做了。');
    assert.ok(requests.filter(item => item.url.includes('/timeline?')).every(item => item.url.endsWith('transcript_revision=2'))); assert.ok(requests.every(item => item.method === 'GET')); env.fire('pagehide');
  });
  await check('Report page uses frozen revision and excludes subsequent same-version references',async () => {
    const env = environment('interview-report.html'), requests = []; wireFetch(env,requests); env.run('js/interview-report.js'); await tick();
    env.document.querySelector('.report-quote').querySelector('.evidence-reference-link').click(); await tick();
    assert.ok(env.document.querySelector('audio')); assert.ok(!env.document.body.textContent.includes('LATER RECORD'));
    assert.match(env.document.querySelector('.evidence-timeline-heading').textContent,/v2/); assert.ok(requests.filter(item => item.url.includes('/timeline?')).every(item => item.url.endsWith('transcript_revision=2'))); env.fire('beforeunload');
  });
  await check('Refresh keeps draft reference revision until explicit adoption of the current version',async () => {
    const env = environment('hr-review.html'), requests = [], data = report(); wireFetch(env,requests,() => data); env.run('js/hr-review.js'); await tick();
    const get = id => env.document.getElementById(id);
    env.document.querySelector('.quote-field').value = '🙂我做了。'; env.document.querySelector('.rating-reason').value = '保留的理由'; get('reviewReason').value = '保留的复核说明'; get('reviewForm').fire('input');
    get('assessmentQuote').value = '🙂我做了。'; get('assessmentQuote').fire('input');
    data.version = 5; data.answers[0].transcript_revision = 3;
    get('refreshReport').click(); await tick();
    assert.equal(env.document.querySelector('.rating-reason').value,'保留的理由'); assert.match(env.document.querySelector('.evidence-quote-version').textContent,/v2.*已变化/);
    get('reviewForm').fire('submit'); await tick(); assert.match(get('notice').textContent,/旧转写版本/); assert.ok(requests.every(item => item.method === 'GET'));
    const link = env.document.querySelector('.quote-item').querySelector('.evidence-reference-link'); link.click(); await tick();
    assert.ok(requests.some(item => item.url.endsWith('transcript_revision=2')));
    env.document.querySelector('.confirm-evidence-version').click(); link.click(); await tick();
    assert.ok(requests.some(item => item.url.endsWith('transcript_revision=3'))); assert.match(env.document.querySelector('.evidence-quote-version').textContent,/v3.*当前版本/);
    assert.match(get('assessmentVersion').textContent,/v2.*已更新/); get('confirmAssessmentVersion').click(); assert.match(get('assessmentVersion').textContent,/v3.*当前版本/); env.fire('pagehide');
  });
  console.log(JSON.stringify({fixture:'synthetic-only; VM and Fake DOM; no browser or decoder validation',passed:checks.length,checks},null,2));
})().catch(error => { console.error(error); process.exitCode = 1; });
