/* Read-only links between frozen measurements, transcript segments and class samples. */
(() => {
 'use strict';
 const names={happy:'开心样式',neutral:'中性样式',disgust:'厌恶样式',sad:'悲伤样式',angry:'生气样式',fearful:'害怕样式',surprised:'惊讶样式'};
 const flags=new Set(['low_light','overexposed','repeated_identical_frames','multiple_faces','multiple_persons','frozen','blurred','face_too_small']);
 function expressionSamples(module){
  const all=Array.isArray(module?.observations)?module.observations:[],valid=[];
  for(const [index,o]of all.entries()){
   const values=o.values||{},scores=values.raw_scores||{},label=values.label;
   if(o.quality!=='valid'||(o.quality_flags||[]).some(f=>flags.has(f))||!Object.hasOwn(names,label)||!Number.isFinite(o.start_ms)||!Number.isFinite(o.end_ms)||o.start_ms<0||o.end_ms<=o.start_ms)continue;
   if(Object.keys(scores).length!==7||!Object.keys(names).every(k=>Number.isFinite(scores[k]))||scores[label]!==Math.max(...Object.values(scores)))continue;
   const ranked=Object.entries(scores).sort((a,b)=>b[1]-a[1]);valid.push({observation:o,index,label,ranked,gap:ranked[0][1]-ranked[1][1]});
  }
  return {total:all.length,valid,excluded:all.length-valid.length,rows:Object.entries(names).map(([label,name])=>{const samples=valid.filter(x=>x.label===label);return {label,name,count:samples.length,fraction:valid.length?samples.length/valid.length:null,example:samples[0]||null};})};
 }
 function segmentQuotes(data,event){
  if(!data||!['aligned','partial'].includes(data.alignment?.status)||typeof data.text!=='string'||!Number.isFinite(event?.start_ms)||!Number.isFinite(event?.end_ms)||event.start_ms<0||event.end_ms<=event.start_ms||(event.answer_id!=null&&event.answer_id!==data.answer_id)||(event.transcript_revision!=null&&event.transcript_revision!==data.transcript_revision))return [];
  const chars=Array.from(data.text);
  return (data.segments||[]).filter(s=>s.alignment_status==='aligned'&&Number.isInteger(s.char_start)&&Number.isInteger(s.char_end)&&s.char_start>=0&&s.char_end>s.char_start&&s.char_end<=chars.length&&typeof s.text==='string'&&chars.slice(s.char_start,s.char_end).join('')===s.text&&s.text.trim()&&Number.isFinite(s.start_ms)&&Number.isFinite(s.end_ms)&&s.start_ms>=0&&s.start_ms<s.end_ms&&s.start_ms<event.end_ms&&s.end_ms>event.start_ms).map(s=>({answer_id:data.answer_id,transcript_revision:data.transcript_revision,segment_id:s.segment_id,char_start:s.char_start,char_end:s.char_end,quote:s.text,start_ms:s.start_ms,end_ms:s.end_ms}));
 }
 function readingHint(event){
  if(event?.kind==='audio_pitch')return '这描述声音频率的变化，可能与重读、句调或音高估计误差有关；请结合原话回听。';
  if(event?.kind==='audio_energy')return '这描述录入声音的强弱；说话力度、麦克风距离和设备增益都可能影响它。';
  if(event?.kind==='apparent_expression')return '这是外显表情的模型预测，讲话时的嘴部和鼻部动作也可能影响分类；需结合原帧核对。';
  return '';
 }
 window.SuitObservationContext={expressionSamples,segmentQuotes,readingHint,names};
})();
