/* Synthetic DOM data only: no models, camera, media files, or provider requests. */
'use strict';
const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
class Node {
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.attributes={};this.listeners={};this.dataset={};this.style={};this._text='';}
  append(...nodes){this.children.push(...nodes);}
  replaceChildren(...nodes){this.children=nodes;this._text='';}
  set textContent(v){this.children=[];this._text=String(v);}
  get textContent(){return this._text+this.children.map(n=>n.textContent).join('');}
  setAttribute(k,v){this.attributes[k]=String(v);}
  addEventListener(k,fn){(this.listeners[k]||=[]).push(fn);}
  fire(k){for(const fn of this.listeners[k]||[])fn({});}
  all(predicate){return this.children.flatMap(n=>[...(predicate(n)?[n]:[]),...n.all(predicate)]);}
}
const doc={createElement:tag=>new Node(tag),createElementNS:(_,tag)=>new Node(tag),createTextNode:t=>{const n=new Node('text');n.textContent=t;return n;}};
const context={document:doc};context.window=context;vm.createContext(context);
vm.runInContext(fs.readFileSync(path.join(__dirname,'../site/js/multimodal-view.js'),'utf8'),context);
const mm=context.SuitMultimodalView;
const observation=(start,end,value,quality='valid')=>({start_ms:start,end_ms:end,quality,values:{f0_hz:value}});
const data={transcript_revision:2,modules:{audio:{status:'complete',observations:[observation(0,1000,200),observation(1000,2000,null),observation(2000,3000,220),observation(4000,5000,230)]},gaze:{status:'complete',observations:[],geometry:{frame_width:640,frame_height:360,analysis_fps:5,sample_stride:1,observations:[{start_ms:200,end_ms:400,quality:'valid',landmarks:[[.5,.5],[.6,.6]],gaze_arrows:[{origin:[.5,.5],tip:[.6,.7]}]},{start_ms:600,end_ms:800,quality:'invalid',landmarks:[[.5,.5]],gaze_arrows:[]}]}}}};
let listener,overlay,detached=0,unsubscribed=0;const seeks=[];
const timeline={attachOverlay(n){overlay=n;return()=>detached++;},subscribeTime(fn){listener=fn;fn(0,{tagName:'VIDEO'});return()=>unsubscribed++;},seek:async ms=>seeks.push(ms),refreshTime(){}};
const view=mm.mount({data,timeline});
assert.equal(mm.windowAt(data.modules.gaze.geometry.observations,400),null);
assert.equal(mm.windowAt(data.modules.gaze.geometry.observations,599),null);
assert.equal(mm.windowAt(data.modules.gaze.geometry.observations,200).start_ms,200);
listener(250,{tagName:'VIDEO'});
assert.equal(overlay.children.filter(n=>n.tagName==='CIRCLE').length,2);
assert.equal(overlay.children.find(n=>n.tagName==='CIRCLE').attributes.cx,'320');
assert.equal(overlay.children.filter(n=>n.tagName==='LINE').length,1);
listener(500,{tagName:'VIDEO'});assert.equal(overlay.children.length,0,'Missing time must not retain previous geometry');
listener(650,{tagName:'VIDEO'});assert.equal(overlay.children.length,0,'Invalid detection must not draw points');
listener(250,{tagName:'AUDIO'});assert.equal(overlay.style.display,'none','Audio media never displays face overlay');
console.log('PASS exact observation windows, real coordinate projection, missing/invalid/audio overlays');
const curve=view.element.all(n=>n.attributes.class==='mm-line')[0].attributes.d;
assert.equal((curve.match(/ M/g)||[]).length,3,'Null and unrecorded gaps both break curve');
const select=view.element.all(n=>n.tagName==='SELECT')[0];
assert.equal(select.value,'f0_hz');
assert.equal(view.element.all(n=>n.attributes.class==='mm-point').length,3);
console.log('PASS missing values are not plotted as zero or connected across gaps');
view.setChannel('geometry');
assert.match(view.element.textContent,/真实面部检测点/);
view.setChannel('audio');
const raw=view.element.all(n=>n.className==='mm-raw')[0];
assert.equal(raw.children[1].children.length,0,'Raw data must be lazy');
raw.open=true;raw.fire('toggle');assert.equal(raw.children[1].children.length,4);
view.dispose();assert.equal(detached,1);assert.equal(unsubscribed,1);
console.log('PASS tab selection, lazy raw records, overlay/time cleanup');

// Optional model inputs use their actual source pixels and frame duration.
// The surrounding sample/result window must never hold a crop over other frames.
const capture={frame_width:640,frame_height:360,at_ms:1000,duration_ms:40,boxes:[{kind:'head',x:180,y:50,width:160,height:180}]};
const gaze360={start_ms:1000,end_ms:2000,centre_ms:1000,last_input_ms:1360,input_frame_times_ms:[640,760,880,1000,1120,1240,1360],requested_frame_times_ms:[625,750,875,1000,1125,1250,1375],ready_at:'2026-10-26T00:00:02Z',quality:'valid',capture,values:{direction_xyz:[0,.5,-.866],yaw_pitch_radians:[0,.524],angular_interval_radians:[.1,.12]}};
const optional=mm.mount({data:{modules:{gaze360:{status:'complete',observations:[gaze360]},emotic:{status:'complete',observations:[{...gaze360,capture:{...capture,boxes:[{kind:'context',x:0,y:0,width:640,height:360},{kind:'person',x:150,y:0,width:240,height:360}]},values:{category_scores:{Anger:.1,Happiness:.4,Sadness:.2,Surprise:.3,Confidence:.99},category_thresholds:{Anger:.2,Happiness:.3,Confidence:.8},above_threshold:['Happiness','Confidence'],vad:{dominance:.9}}}]}}},timeline});
listener(1000,{tagName:'VIDEO'});
assert.equal(overlay.children.filter(n=>n.tagName==='RECT').length,1);
assert.equal(overlay.children.find(n=>n.tagName==='RECT').attributes.x,'180');
assert.equal(overlay.children.filter(n=>n.tagName==='CIRCLE'||n.tagName==='LINE').length,0,'Gaze360 does not borrow OpenFace dots or arrows');
assert.match(optional.element.textContent,/最后输入帧 00:01.360/);
assert.match(optional.element.textContent,/实际输入帧/);
assert.match(optional.element.textContent,/8 Hz/);
assert.match(optional.element.textContent,/请求 00:00.625 · 实际 00:00.640 · 时间匹配误差 \+15 ms/);
assert.equal(mm.frameTimings(gaze360)[6].error_ms,-15);
assert.equal(mm.frameTimings({input_frame_times_ms:[0,null,250],requested_frame_times_ms:[0,125,250]})[1].actual_ms,null,'Missing frame retains its index');
assert.equal(mm.frameTimings({input_frame_times_ms:[0,250],requested_frame_times_ms:[0,125,250]})[1].error_ms,null,'Unmatched timing arrays cannot invent frame pairing');
assert.equal(optional.element.all(n=>n.tagName==='SELECT')[0].children.length,7,'Gaze360 arrays have named numeric axes');
assert.equal(mm.metricName('angular_interval_radians.yaw')[1],'rad · 模型分位区间，非本场校准置信率');
assert.equal(mm.metricName('yaw_pitch_radians.pitch')[0],'Gaze360 俯仰角');
assert.equal(mm.metricName('direction_xyz.z')[0],'Gaze360 方向 z');
listener(1040,{tagName:'VIDEO'});assert.equal(overlay.children.length,0,'Capture overlay ends at the exact native-frame boundary');
listener(1500,{tagName:'VIDEO'});assert.equal(overlay.children.length,0,'Wide result window cannot retain capture geometry');
assert.equal(mm.captureAt([{...gaze360,capture:{...capture,duration_ms:null}}],1000),null);
assert.equal(mm.captureAt([{...gaze360,quality:'missing'}],1000),null);
assert.equal(mm.captureBoxes({...capture,boxes:[{kind:'head',x:-1,y:0,width:20,height:20}]}).length,0);
optional.setChannel('emotic');listener(1000,{tagName:'VIDEO'});
assert.equal(overlay.children.filter(n=>n.tagName==='RECT').length,2,'EMOTIC exposes actual scene plus person input');
assert.match(optional.element.textContent,/完整场景/);
assert.match(optional.element.textContent,/场景辅助感知情绪/);
const optionalRaw=optional.element.all(n=>n.className==='mm-raw')[0];optionalRaw.open=true;optionalRaw.fire('toggle');
assert.ok(!optional.element.textContent.includes('Confidence'),'Unapproved category never enters metrics or raw output');
assert.ok(!optional.element.textContent.includes('dominance'),'VAD/internal attribute payload is not displayed');
assert.match(optional.element.textContent,/Happiness/);
listener(1000,{tagName:'AUDIO'});assert.equal(overlay.style.display,'none');
optional.dispose();
console.log('PASS Gaze360/EMOTIC actual frame capture, separate source geometry, allowed output and units');

const metricView=mm.mount({data:{modules:{expression:{status:'complete',observations:[{start_ms:0,end_ms:1000,quality:'valid',values:{label:'happy',raw_scores:{angry:-1,happy:2}}}]},audio:{status:'complete',observations:[{start_ms:0,end_ms:1000,quality:'valid',values:{f0_hz:160,rms_dbfs:-20}}]}}},timeline});
metricView.setChannel('expression','raw_scores.happy');assert.equal(metricView.element.all(n=>n.tagName==='SELECT')[0].value,'raw_scores.happy');
metricView.setChannel('audio','rms_dbfs');assert.equal(metricView.element.all(n=>n.tagName==='SELECT')[0].value,'rms_dbfs');
metricView.setChannel('audio','f0_hz');assert.equal(metricView.element.all(n=>n.tagName==='SELECT')[0].value,'f0_hz');
metricView.setChannel('expression','raw_scores.unknown');assert.equal(metricView.element.all(n=>n.tagName==='SELECT')[0].value,'raw_scores.angry');metricView.dispose();
console.log('PASS event channels select the exact expression, pitch or energy metric without inventing absent metrics');
