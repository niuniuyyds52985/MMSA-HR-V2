/* Synthetic VM/Fake DOM checks only. These do not claim browser or alignment accuracy. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.resolve(__dirname, '../site/js/alignment-editor.js'), 'utf8');
const tick = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };
class Node {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.listeners = {}; this.value = ''; this.attributes = {}; this.disabled = false; this.hidden = false; this.checked = false; this._text = ''; }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; this._text = ''; }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  set innerHTML(_) { throw new Error('No HTML injection'); }
  setAttribute(name,value) { this.attributes[name] = String(value); }
  addEventListener(name,fn) { (this.listeners[name] ||= []).push(fn); }
  fire(name,event = {}) { for (const fn of this.listeners[name] || []) fn({target:this,...event}); }
  click() { if (!this.disabled) this.fire('click'); }
  all(predicate) { return this.children.flatMap(child => [...(predicate(child) ? [child] : []), ...child.all(predicate)]); }
}
const data = () => ({schema_version:'interview-timeline.v1',answer_id:'answer-A',transcript_revision:2,text:'🙂第一句。第二句。',media:{available:true,duration_ms:5000,media_sha256:'fixture-sha'},segments:[
  {segment_id:'r2-one',text:'🙂第一句。',char_start:0,char_end:5,start_ms:0,end_ms:2000},
  {segment_id:'r2-two',text:'第二句。',char_start:5,char_end:9,start_ms:3000,end_ms:5000}
]});
function mount(extra = {}) {
  const calls = {load:[],save:[]}; let sequence = 0;
  const context = {document:{createElement:tag => new Node(tag)},crypto:{randomUUID:() => 'fixture-key-' + (++sequence)}}; context.window = context;
  vm.runInNewContext(source,context);
  const view = context.SuitAlignmentEditor.mount({answerId:'answer-A',revision:2,fetchTimeline:async revision => { calls.load.push(revision); return data(); },save:async body => { calls.save.push(body); return {version:1}; },...extra});
  const byClass = cls => view.element.all(node => (node.className || '').split(' ').includes(cls));
  const fillAudit = () => { byClass('alignment-reviewer')[0].value = '合成测试复核人'; byClass('alignment-reason')[0].value = '合成测试时间校准'; };
  return {view,calls,byClass,fillAudit};
}
let passed = 0;
async function check(name,fn) { await fn(); passed++; console.log('PASS ' + name); }
(async () => {
  await check('lazy load, exact revision, no nested form or submit buttons', async () => {
    const e = mount(); assert.equal(e.calls.load.length,0);
    e.view.element.open = true; e.view.element.fire('toggle'); await tick();
    assert.deepEqual(e.calls.load,[2]); assert.equal(e.byClass('alignment-include').length,2);
    assert.ok(e.byClass('alignment-include').every(node => node.checked));
    assert.equal(e.view.element.all(node => node.tagName === 'FORM').length,0);
    assert.ok(e.view.element.all(node => node.tagName === 'BUTTON').every(node => node.type === 'button'));
    let prevented = false; e.view.element.fire('keydown',{key:'Enter',target:{tagName:'INPUT'},preventDefault:()=>{ prevented = true; }}); assert.ok(prevented);
    await e.view.load(); assert.equal(e.calls.load.length,1);
  });
  await check('reject wrong schema, answer, revision, media and duplicate stable IDs', async () => {
    const cases = [d=>{d.schema_version='v99';},d=>{d.answer_id='answer-B';},d=>{d.transcript_revision=3;},d=>{d.media.available=false;},d=>{d.media.duration_ms=0;},d=>{d.segments[1].segment_id=d.segments[0].segment_id;}];
    for (const change of cases) { const d = data(); change(d); const e = mount({fetchTimeline:async()=>d}); await e.view.load(); assert.equal(e.byClass('alignment-save').length,0); assert.equal(e.calls.save.length,0); }
  });
  await check('audit required; partial snapshot sends exact IDs and integer milliseconds', async () => {
    const e = mount(); await e.view.load(); e.byClass('alignment-save')[0].click(); await tick(); assert.equal(e.calls.save.length,0);
    e.fillAudit(); e.byClass('alignment-include')[1].checked = false; e.byClass('alignment-include')[1].fire('change');
    e.byClass('alignment-start')[0].value = '0.123'; e.byClass('alignment-end')[0].value = '1.987';
    assert.match(e.view.element.textContent,/1 段将未对齐/);
    e.byClass('alignment-save')[0].click(); await tick();
    assert.deepEqual(JSON.parse(JSON.stringify(e.calls.save[0])),{revision:2,segments:[{segment_id:'r2-one',start_ms:123,end_ms:1987}],reviewer:'合成测试复核人',reason:'合成测试时间校准',idempotency_key:'fixture-key-1'});
  });
  await check('reject empty, excessive precision, out of range and overlapping times', async () => {
    for (const [firstEnd, secondStart] of [['','3'],['2.0001','3'],['6','3'],['4','3'],['NaN','3']]) {
      const e = mount(); await e.view.load(); e.fillAudit(); e.byClass('alignment-end')[0].value = firstEnd; e.byClass('alignment-start')[1].value = secondStart;
      e.byClass('alignment-save')[0].click(); await tick(); assert.equal(e.calls.save.length,0);
    }
  });
  await check('save guard, failure retains input and stable retry key; changed payload new key', async () => {
    const attempts = []; let reject;
    const e = mount({save:body=>{attempts.push(body);return new Promise((_,no)=>{reject=no;});}}); await e.view.load(); e.fillAudit();
    const button = e.byClass('alignment-save')[0]; button.click(); button.click(); assert.equal(attempts.length,1); assert.ok(button.disabled);
    reject(new Error('合成网络失败')); await tick(); assert.equal(e.byClass('alignment-reviewer')[0].value,'合成测试复核人'); assert.ok(!button.disabled);
    button.click(); assert.equal(attempts[1].idempotency_key,attempts[0].idempotency_key); reject(new Error('再次失败')); await tick();
    e.byClass('alignment-reason')[0].value='修改合成依据'; button.click(); assert.notEqual(attempts[2].idempotency_key,attempts[1].idempotency_key); reject(new Error('结束合成请求')); await tick();
  });
  await check('saved callback rejection reports saved status without unhandled rejection', async () => {
    const e = mount({onSaved:async()=>{throw new Error('Synthetic refresh failure');}}); await e.view.load(); e.fillAudit(); e.byClass('alignment-save')[0].click(); await tick();
    assert.equal(e.calls.save.length,1); assert.match(e.view.element.textContent,/已保存，但时间线刷新失败/); assert.ok(!e.byClass('alignment-save')[0].disabled);
  });
  console.log(`Completed ${passed} synthetic component checks; browser QA is separate.`);
})().catch(error => { console.error(error); process.exitCode = 1; });
