/* Synthetic state semantics; no model, media, network or scoring computation. */
'use strict';
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const harness=fs.readFileSync(path.join(__dirname,'test_evidence_timeline.cjs'),'utf8').split('(async () => {')[0];
const shared={require,__dirname,console,URLSearchParams,Headers,Blob,Response,Date,Set,Map,Number,Array,Math,Promise,setTimeout,clearTimeout,queueMicrotask};
vm.runInNewContext(harness+'\nglobalThis.makeEnvironment=environment;',shared);
const e=shared.makeEnvironment(),create=e.document.createElement.bind(e.document);
e.document.createElement=tag=>{const n=create(tag);n.style.setProperty=(k,v)=>n.style[k]=v;return n;};
e.run('js/potential-profile.js');
const factors=Array.from({length:8},(_,i)=>({factor_id:'f'+i,name:'因子'+i,state:'not_measured',score:null,definition:'合成定义',reason:'本轮未安排此因子'}));
Object.assign(factors[0],{state:'processing',score:5,reason:'后台内容分析正在处理本题。'});
Object.assign(factors[1],{state:'analysis_unavailable',score:4,reason:'内容服务暂不可用，请稍后核对。'});
Object.assign(factors[2],{state:'insufficient_evidence',reason:'已有文字尚不足以对应评分锚点。'});
Object.assign(factors[3],{state:'scored',score:3,score_source:'model_draft',reason:'存在具体行动证据。'});
const root=e.sandbox.SuitPotentialProfile.mount({profile:{factors,report_version:3}}).element;
const cards=root.querySelectorAll('.potential-factor-card');
assert.match(cards[0].querySelector('.potential-card-state').textContent,/内容分析中/);
assert.doesNotMatch(cards[0].querySelector('.potential-card-state').textContent,/证据不足/);
assert.match(cards[1].querySelector('.potential-card-state').textContent,/自动分析暂不可用/);
assert.match(cards[2].querySelector('.potential-card-state').textContent,/证据不足/);
assert.match(cards[0].querySelector('.potential-card-reason').textContent,/正在处理本题/);
assert.match(cards[1].querySelector('.potential-card-reason').textContent,/内容服务暂不可用/);
console.log('PASS processing, unavailable, and insufficient evidence retain distinct labels and backend reasons');
assert.equal(cards[0].querySelector('.potential-card-score').textContent,'—');
assert.equal(cards[1].querySelector('.potential-card-score').textContent,'—');
assert.equal(root.querySelectorAll('.potential-score-point').length,1);
assert.equal(root.querySelectorAll('.potential-complete-outline').length,0);
assert.equal(root.querySelectorAll('.potential-factor-state').filter(n=>n.textContent==='内容分析中').length,1);
assert.equal(root.querySelectorAll('.potential-factor-state').filter(n=>n.textContent==='自动分析暂不可用').length,1);
console.log('PASS unfinished analysis never creates score points or closes the potential outline');

const detail=root.querySelector('.potential-factor-details');assert.ok(detail);assert.ok(!detail.open);root.querySelector('.potential-score-point').click();assert.equal(detail.open,true);assert.equal(cards[3].focused,true);
console.log('PASS factor details start collapsed and a scored graph point reveals its evidence card');

assert.equal(root.querySelectorAll('.potential-factor-sector').length,8);
assert.equal(root.querySelectorAll('.potential-scale-slot').length,40);
assert.equal(root.querySelectorAll('.potential-scale-slot.is-missing').length,35);
assert.equal(root.querySelectorAll('.potential-score-fill').length,3);
assert.ok(root.querySelectorAll('.potential-score-fill').every(n=>n.getAttribute('data-score')==='3'));
assert.match(root.querySelector('.potential-profile-caption').textContent,/空白虚线.*不是 0 分/);
assert.match(root.querySelector('.potential-factor-source').textContent,/模型草稿.*待复核/);
console.log('PASS every factor has five independent scale bands, only valid evidence is filled, and missing does not become zero');
const missing=root.querySelectorAll('.potential-factor-sector')[0];detail.open=false;missing.fire('keydown',{key:'Enter'});assert.equal(detail.open,true);assert.equal(cards[0].focused,true);
console.log('PASS keyboard access also opens an unfinished factor with its own preserved status and explanation');
const fractional=factors.map(f=>({...f}));fractional[3]={...fractional[3],score:2.5,score_source:'hr',target:4};fractional[4]={...fractional[4],state:'scored',score:0};fractional[5]={...fractional[5],target:3};
const referenced=e.sandbox.SuitPotentialProfile.mount({profile:{factors:fractional,reference_profile:{version:'teacher-reference-fixture'}}}).element;
assert.equal(referenced.querySelectorAll('.potential-score-fill').length,3);assert.ok(referenced.querySelectorAll('.potential-score-fill').every(n=>n.getAttribute('data-score')==='2.5'));
assert.equal(referenced.querySelectorAll('.potential-reference-point').length,2);assert.equal(referenced.querySelectorAll('.potential-score-point').length,1);
assert.equal(referenced.querySelectorAll('.potential-factor-card')[4].querySelector('.potential-card-score').textContent,'—');
assert.equal(referenced.querySelector('.potential-factor-source').textContent,'老师复核');assert.match(referenced.querySelectorAll('.potential-factor-card')[5].querySelector('.potential-card-reference').textContent,/当前无有效分，暂不比较/);
const noReference=e.sandbox.SuitPotentialProfile.mount({profile:{factors:fractional}}).element;assert.equal(noReference.querySelectorAll('.potential-reference-point').length,0);
console.log('PASS fractional HR scores and independent reference rings preserve their sources; zero, missing and absent reference profiles are not filled');
(async()=>{
  const original={answer_id:'synthetic-answer',transcript_revision:2,quote:'我負責協調',char_start:1,char_end:6},saved=JSON.stringify(original),calls=[];
  const linked=factors.map(f=>({...f}));linked[3]={...linked[3],evidence:[original]};
  const linkedRoot=e.sandbox.SuitPotentialProfile.mount({profile:{factors:linked},onEvidence:(ref,factor)=>calls.push({ref,factor})}).element;
  assert.equal(linkedRoot.querySelector('blockquote').textContent,'我负责协调');linkedRoot.querySelector('.potential-evidence-button').click();await Promise.resolve();await Promise.resolve();
  assert.equal(calls.length,1);assert.equal(calls[0].ref.quote,'我負責協調');assert.equal(calls[0].ref.transcript_revision,2);assert.equal(calls[0].ref.char_start,1);assert.equal(calls[0].factor.factor_id,'f3');assert.equal(JSON.stringify(original),saved);
  console.log('PASS graph evidence remains linked to its original text, revision and Unicode location while reading simplified Chinese');
})().catch(error=>{console.error(error);process.exitCode=1;});

const canonical={answer_id:'synthetic-main',transcript_revision:3,segment_id:'seg-1',char_start:1,char_end:6,quote:'我負責協調',answer_kind:'main'};
const conclusionFixture={schema_version:'potential-conclusion.v1',source_version:{report_version:7,criteria_version:'fixture-criteria-v1'},summary:'本次回答已提供协调行动；结果归属仍待补充。',coverage:{measured_factor_count:4,total_factor_count:8},factors:[
 {factor_id:'leadership',name:'领导意愿',dimension:'领导倾向',status:'demonstrated',criteria:[{id:'c1',text:'说明本人协调行动',status:'demonstrated',evidence:[canonical],gaps:[]}]},
 {factor_id:'results',name:'结果导向',dimension:'业务驱动',status:'partial',criteria:[{id:'c2',text:'检查实际结果',status:'partial',evidence:[{...canonical,answer_kind:'followup'}],gaps:['补充结果与目标的对应']}]},
 {factor_id:'learning',name:'学习敏锐度',dimension:'成长性',status:'not_met',criteria:[{id:'c3',text:'从经历中复盘',status:'not_met',evidence:[{...canonical,quote:'我沒有復盤'}],gaps:[]}]},
 {factor_id:'feedback',name:'接纳反馈',dimension:'成长性',status:'insufficient_evidence',criteria:[{id:'c4',text:'说明如何回应反馈',status:'insufficient_evidence',evidence:[],gaps:['缺少具体反馈事件']}],acoustic_score:5},
 ...['develop_others','balance_people_task','systems_thinking','adaptability'].map((factor_id,i)=>({factor_id,name:'未测因子'+i,dimension:'原始维度',status:'not_measured',criteria:[]}))]};
const beforeConclusion=JSON.stringify(conclusionFixture),conclusionRoot=e.sandbox.SuitPotentialProfile.mountConclusion({conclusion:conclusionFixture}).element;
assert.equal(conclusionRoot.querySelectorAll('.potential-conclusion-factor').length,8);assert.equal(conclusionRoot.querySelectorAll('.potential-conclusion-criterion').length,4);assert.equal(conclusionRoot.querySelectorAll('.potential-conclusion-count').length,5);assert.match(conclusionRoot.textContent,/已体现|部分体现|有相反证据，待复核|证据不足|本轮未测/);assert.match(conclusionRoot.textContent,/待补证据：补充结果与目标的对应/);assert.equal(conclusionRoot.querySelectorAll('.potential-conclusion-factor').filter(n=>n.open).length,0);assert.equal(JSON.stringify(conclusionFixture),beforeConclusion);assert.match(conclusionRoot.querySelectorAll('.potential-conclusion-factor')[3].textContent,/证据不足/);assert.equal(conclusionRoot.querySelector('blockquote').querySelector('p').textContent,'“我负责协调”');
console.log('PASS final potential conclusion preserves all five evidence states and eight factors without scoring missing or acoustic data');
const compactConclusion=e.sandbox.SuitPotentialProfile.mountConclusion({conclusion:conclusionFixture,compact:true}).element;assert.equal(compactConclusion.querySelectorAll('.potential-conclusion-criterion').length,0);assert.match(compactConclusion.textContent,/局部覆盖/);assert.match(compactConclusion.textContent,/4 \/ 8/);
const historical=e.sandbox.SuitPotentialProfile.mountConclusion().element;assert.match(historical.textContent,/尚未保存逐评价点/);assert.equal(historical.querySelectorAll('.potential-conclusion-status').length,0);
const malformed=e.sandbox.SuitPotentialProfile.mountConclusion({conclusion:{factors:[{factor_id:'x',name:'待核对',status:'not_met',criteria:[{id:'bad',text:'缺少材料',status:'not_met',evidence:[]}]}]}}).element;assert.equal(malformed.querySelectorAll('.status-not_met').filter(n=>n.className.includes('potential-conclusion-status')).length,0);assert.match(malformed.textContent,/未附可核对的相反原话/);
console.log('PASS compact conclusion labels partial coverage, history stays unjudged, and unsupported negative claims become insufficient evidence');
(async()=>{const captured=[],node=e.sandbox.SuitPotentialProfile.mountConclusion({conclusion:conclusionFixture,onEvidence:(ref,factor,criterion)=>captured.push({ref,factor,criterion})}).element;node.querySelector('.potential-evidence-button').click();await Promise.resolve();await Promise.resolve();assert.equal(captured[0].ref.quote,canonical.quote);assert.equal(captured[0].ref.transcript_revision,3);assert.equal(captured[0].ref.char_start,1);assert.equal(captured[0].ref.char_end,6);assert.equal(captured[0].criterion.id,'c1');assert.equal(JSON.stringify(conclusionFixture),beforeConclusion);console.log('PASS conclusion references preserve the exact original text, version and offsets for replay');})().catch(error=>{console.error(error);process.exitCode=1;});
