(() => {
  'use strict';
  const ns='http://www.w3.org/2000/svg', finite=v=>typeof v==='number'&&Number.isFinite(v);
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=String(text);return n;};
  const svg=(tag,attrs={})=>{const n=document.createElementNS(ns,tag);Object.entries(attrs).forEach(([k,v])=>n.setAttribute(k,String(v)));return n;};
  const names={audio:'声音',expression:'表情类别',au:'面部动作',au_net:'AU-Net 动作',gaze:'OpenFace 视线',gaze360:'Gaze360 视线',emotic:'EMOTIC 感知情绪',geometry:'OpenFace 采集点'};
  const labels={angry:'愤怒样表情',disgust:'厌恶样表情',fearful:'恐惧样表情',happy:'开心样表情',neutral:'中性表情',sad:'悲伤样表情',surprised:'惊讶样表情'};
  const emoticLabels={Anger:'愤怒',Happiness:'开心',Sadness:'悲伤',Surprise:'惊讶'};
  const states={complete:'完成',succeeded:'完成',partial:'部分完成',unavailable:'不可用',no_media:'没有原片',no_video:'没有视频',no_audio:'没有音轨',no_valid_observations:'没有有效观测',queued:'等待分析',running:'分析中'};
  const qualityNames={audio_clipping:'录音削波，请降低麦克风增益',near_silence:'接近静音，请检查麦克风距离',suspected_freeze:'检测到连续重复帧',low_light:'画面偏暗',overexposure:'画面过亮'};
  const stamp=(ms,precision=1)=>finite(ms)?`${Math.floor(ms/60000).toString().padStart(2,'0')}:${(ms%60000/1000).toFixed(precision).padStart(precision+3,'0')}`:'—';
  function flatten(value,prefix='',result={}) {for(const[k,v]of Object.entries(value||{})){if(finite(v))result[prefix+k]=v;else if(Array.isArray(v)){const axes={direction_xyz:['x','y','z'],yaw_pitch_radians:['yaw','pitch'],angular_interval_radians:['yaw','pitch']}[k];if(axes&&v.length===axes.length)v.forEach((n,i)=>{if(finite(n))result[prefix+k+'.'+axes[i]]=n;});}else if(v&&typeof v==='object')flatten(v,prefix+k+'.',result);}return result;}
  function emoticValues(value={}) {const out={};for(const key of ['category_scores','category_thresholds']){out[key]={};for(const label of Object.keys(emoticLabels))if(finite(value[key]?.[label]))out[key][label]=value[key][label];}out.above_threshold=(Array.isArray(value.above_threshold)?value.above_threshold:[]).filter(label=>Object.hasOwn(emoticLabels,label));return out;}
  function windowAt(observations,time) {return (observations||[]).find(o=>finite(o.start_ms)&&finite(o.end_ms)&&o.start_ms<=time&&time<o.end_ms)||null;}
  function captureAt(observations,time) {return (observations||[]).find(o=>o.quality==='valid'&&finite(o.capture?.at_ms)&&finite(o.capture?.duration_ms)&&o.capture.duration_ms>0&&o.capture.at_ms<=time&&time<o.capture.at_ms+o.capture.duration_ms)?.capture||null;}
  function captureBoxes(capture) {const w=capture?.frame_width,h=capture?.frame_height;if(!finite(w)||!finite(h)||w<=0||h<=0)return [];return (capture.boxes||[]).filter(b=>['head','person','context'].includes(b.kind)&&[b.x,b.y,b.width,b.height].every(finite)&&b.x>=0&&b.y>=0&&b.width>0&&b.height>0&&b.x+b.width<=w+.01&&b.y+b.height<=h+.01);}
  function frameTimings(observation) {const actual=Array.isArray(observation.input_frame_times_ms)?observation.input_frame_times_ms:[],requested=Array.isArray(observation.requested_frame_times_ms)?observation.requested_frame_times_ms:[],matched=actual.length===requested.length;return actual.map((time,index)=>({frame:index+1,actual_ms:finite(time)?time:null,requested_ms:matched&&finite(requested[index])?requested[index]:null,error_ms:matched&&finite(time)&&finite(requested[index])?Math.round((time-requested[index])*1000)/1000:null}));}
  function metricName(key) {
    const known={f0_hz:['音高','Hz'],rms_dbfs:['录音能量','dBFS'],speech_fraction:['发声窗口占比','0–1'],voiced_fraction:['发声窗口占比','0–1'],clipped_sample_fraction:['削波样本占比','0–1'],gaze_angle_x:['水平视线角','rad'],gaze_angle_y:['垂直视线角','rad'],f0_voiced_frames:['有效音高帧','帧']};
    if(known[key])return known[key];
    if(key.startsWith('intensity.'))return [key.slice(10)+' 动作强度','模型标度 0–5'];
    if(key.startsWith('presence.'))return [key.slice(9)+' 动作检出','0 / 1'];
    if(key.startsWith('raw_scores.'))return [(labels[key.slice(11)]||key.slice(11))+' 原始分数','模型原始标度'];
    if(key.startsWith('activation_scores.'))return [key.slice(18)+' 激活分值','AU-Net sigmoid 0–1，非校准概率'];
    if(key.startsWith('raw_logits.'))return [key.slice(11)+' 原始输出','AU-Net logit'];
    if(key.startsWith('category_scores.'))return [(emoticLabels[key.slice(16)]||key.slice(16))+' 场景感知分值','EMOTIC 原始线性分值，非概率'];
    if(key.startsWith('category_thresholds.'))return [(emoticLabels[key.slice(20)]||key.slice(20))+' 类别阈值','对应权重的阈值'];
    if(key.startsWith('direction_xyz.'))return ['Gaze360 方向 '+key.slice(14),'三维单位向量'];
    if(key.startsWith('yaw_pitch_radians.'))return ['Gaze360 '+({yaw:'水平角',pitch:'俯仰角'}[key.slice(18)]||key.slice(18)),'rad'];
    if(key.startsWith('angular_interval_radians.'))return ['Gaze360 '+({yaw:'水平',pitch:'俯仰'}[key.slice(25)]||key.slice(25))+'区间半宽','rad · 模型分位区间，非本场校准置信率'];
    return [key,'见模型来源'];
  }
  let serial=0;
  function mount({data,timeline,run={},sourceLabel='研究观察'}) {
    const root=el('section','multimodal-view'),header=el('div','mm-heading');
    header.append(el('h3','','多模态分析'),el('span','mm-source',sourceLabel));
    const modules={...(data?.modules||{})};if(modules.emotic)modules.emotic={...modules.emotic,observations:(modules.emotic.observations||[]).map(o=>({...o,values:emoticValues(o.values)}))};const geometry=modules.gaze?.geometry||modules.au?.geometry;
    const keys=['audio','expression','au','au_net','gaze','gaze360','emotic'].filter(k=>modules[k]);if(geometry||modules.au||modules.gaze)keys.push('geometry');
    const tabs=el('div','mm-tabs'),body=el('div','mm-body'),status=el('p','mm-status');status.setAttribute('role','status');
    const context=el('p','mm-context',`对应转写 v${data?.transcript_revision??'—'} · ${run.completed_at?'已完成于 '+new Date(run.completed_at).toLocaleString('zh-CN',{hour12:false}):'按本次运行快照显示'}`);
    root.append(header,context,tabs,body,status);
    let active=keys[0]||'audio',currentTime=0,lastPlayer=null,currentLabel=null,currentMetrics=null,currentCapture=null,cursor=null,duration=1,chartLeft=54,chartWidth=670,selectedMetric='',disposed=false,page=0,showPoints=true,showGaze=true;
    const metricSelections={};
    const overlay=svg('svg',{'aria-hidden':'true',preserveAspectRatio:'xMidYMid meet'}),markerId='mm-gaze-arrow-'+(++serial);
    const detachOverlay=timeline?.attachOverlay?.(overlay)||(()=>{});
    const button=(text,fn,cls='')=>{const b=el('button',cls,text);b.type='button';b.addEventListener('click',()=>Promise.resolve().then(fn).catch(error=>{if(!disposed)status.textContent=error.message||'无法读取对应片段';}));return b;};
    function drawOverlay(time,player) {
      overlay.replaceChildren();
      if(['gaze360','emotic'].includes(active)){
        overlay.style.display=player?.tagName==='VIDEO'?'':'none';
        const capture=captureAt(modules[active]?.observations,time),boxes=captureBoxes(capture);if(!boxes.length)return;
        overlay.setAttribute('viewBox',`0 0 ${capture.frame_width} ${capture.frame_height}`);
        for(const box of boxes){const color=box.kind==='context'?'#ffc660':'#61f6d4';overlay.append(svg('rect',{x:box.x,y:box.y,width:box.width,height:box.height,fill:'none',stroke:color,'stroke-width':Math.max(2,capture.frame_width/320),'stroke-dasharray':box.kind==='context'?'8 5':'none'}));const text=svg('text',{x:box.x+5,y:Math.min(capture.frame_height-5,box.y+20),fill:color,'font-size':Math.max(13,capture.frame_width/42),'paint-order':'stroke',stroke:'#17364b','stroke-width':2});text.textContent={head:'头部输入',person:'人物输入',context:'场景输入'}[box.kind];overlay.append(text);}return;
      }
      const frame=windowAt(geometry?.observations,time);overlay.style.display=player?.tagName==='VIDEO'&&(showPoints||showGaze)?'':'none';
      if(!frame||frame.quality!=='valid')return;
      const w=frame.frame_width||geometry.frame_width,h=frame.frame_height||geometry.frame_height;if(!finite(w)||!finite(h)||w<=0||h<=0)return;
      overlay.setAttribute('viewBox',`0 0 ${w} ${h}`);
      if(showPoints)for(const point of frame.landmarks||[]){if(point.length===2&&point.every(finite))overlay.append(svg('circle',{cx:point[0]*w,cy:point[1]*h,r:Math.max(1.4,w/420),fill:'#61f6d4',stroke:'#17364b','stroke-width':0.6}));}
      if(showGaze){const defs=svg('defs'),marker=svg('marker',{id:markerId,markerWidth:7,markerHeight:7,refX:6,refY:3.5,orient:'auto',markerUnits:'strokeWidth'});marker.append(svg('path',{d:'M0,0 L7,3.5 L0,7 Z',fill:'#ffc660'}));defs.append(marker);overlay.append(defs);
        for(const ray of frame.gaze_arrows||[]){if(ray.origin?.length!==2||ray.tip?.length!==2||![...ray.origin,...ray.tip].every(finite))continue;overlay.append(svg('line',{x1:ray.origin[0]*w,y1:ray.origin[1]*h,x2:ray.tip[0]*w,y2:ray.tip[1]*h,stroke:'#ffc660','stroke-width':2,'marker-end':`url(#${markerId})`}));}}
    }
    function update(time,player) {
      if(disposed)return;if(player!==undefined)lastPlayer=player;currentTime=time;drawOverlay(time,lastPlayer);
      const selected=active==='geometry'?windowAt(geometry?.observations,time):windowAt(modules[active]?.observations,time);
      if(currentLabel)currentLabel.textContent=`${stamp(time)} · `+(selected?`${stamp(selected.start_ms)}–${stamp(selected.end_ms)} · ${selected.quality==='valid'?'有效采样':selected.reason||'无效/缺失'}`:'当前时刻未采样或没有对应观测');
      if(cursor){const x=chartLeft+Math.min(duration,Math.max(0,time))/duration*chartWidth;cursor.setAttribute('x1',x);cursor.setAttribute('x2',x);}
      if(currentMetrics){currentMetrics.replaceChildren();if(!selected||selected.quality!=='valid')currentMetrics.append(el('p','mm-missing',selected?.reason||'此时没有有效结果；缺失不补值。'));
        else if(active==='geometry')currentMetrics.append(el('p','',`${selected.landmarks?.length||0} 个真实面部点位 · ${selected.gaze_arrows?.length||0} 条有效视线方向`));
        else if(active==='expression'){currentMetrics.append(el('strong','mm-expression',labels[selected.values?.label]||selected.values?.label||'未分类'),el('p','mm-help','外显表情类别预测；原始分数不是情绪概率。'));const scores=el('div','mm-score-list');for(const[label,value]of Object.entries(selected.values?.raw_scores||{}))scores.append(el('span','',(labels[label]||label)+' '+(finite(value)?value.toFixed(3):'缺失')));currentMetrics.append(scores);}
        else{const value=flatten(selected.values)[selectedMetric],name=metricName(selectedMetric);currentMetrics.append(el('strong','mm-current-value',finite(value)?value.toFixed(active==='au'?2:3):'无有效值'),el('span','mm-help',name[0]+' · '+name[1]));if(selected.f0_reason&&selectedMetric==='f0_hz')currentMetrics.append(el('p','mm-help',selected.f0_reason));}}
      if(currentMetrics&&selected?.quality_flags?.length)currentMetrics.append(el('p','mm-missing',selected.quality_flags.map(flag=>qualityNames[flag]||flag).join('；')));
      if(currentMetrics&&active==='emotic'&&selected?.quality==='valid'){const above=selected.values?.above_threshold||[];currentMetrics.append(el('p','mm-help','本帧超过类别阈值：'+(above.length?above.map(k=>emoticLabels[k]).join('、'):'无')+'。原始分值与类别阈值可分别核对。'));}
      if(currentCapture){currentCapture.replaceChildren();
        if(!selected){currentCapture.append(el('p','mm-help','当前时刻没有对应模型采样，未延用其他时刻的采集框。'));return;}
        const timings=el('div','mm-capture-times');for(const [label,value]of [['输入中心',selected.centre_ms],['最后输入帧',selected.last_input_ms]])if(finite(value))timings.append(el('span','',label+' '+stamp(value,3)));if(selected.ready_at)timings.append(el('span','','本次就绪 '+String(selected.ready_at)));currentCapture.append(timings);
        const capture=selected.capture,boxes=captureBoxes(capture);if(boxes.length){currentCapture.append(el('p','mm-help',`原片 ${capture.frame_width} × ${capture.frame_height} 像素 · `+boxes.map(b=>`${{head:'头部',person:'人物',context:'完整场景'}[b.kind]}框 x=${b.x.toFixed(1)}, y=${b.y.toFixed(1)}, 宽=${b.width.toFixed(1)}, 高=${b.height.toFixed(1)}`).join('；')));if(finite(capture.at_ms)&&finite(capture.duration_ms)&&capture.duration_ms>0)currentCapture.append(button('定位实际采集帧 '+stamp(capture.at_ms,3),()=>timeline.seek(capture.at_ms,{play:false})),el('p','mm-help',`点击后暂停在原帧。采集框仅在 ${stamp(capture.at_ms,3)} 起的 ${capture.duration_ms.toFixed(1)} 毫秒原始帧区间叠加。`));else currentCapture.append(el('p','mm-help','缺少原始帧时长，采集范围仅列出，不在播放画面持续叠加。'));}
        else currentCapture.append(el('p','mm-help','本窗口没有可核对的输入框；没有绘制示意框。'));
        if(selected.input_frame_times_ms?.length){const frames=el('div','mm-input-frames');frames.append(el('span','','实际输入帧（点击暂停核对）：'));for(const input of frameTimings(selected)){const label=`${input.frame} · ${stamp(input.actual_ms,3)}`;frames.append(finite(input.actual_ms)?button(label,()=>timeline.seek(input.actual_ms,{play:false})):el('span','',label+' 缺失'));}currentCapture.append(frames);if(selected.requested_frame_times_ms?.length){const comparison=el('details','mm-frame-timing');comparison.append(el('summary','','请求采样与实际原帧时间'));for(const input of frameTimings(selected))comparison.append(el('p','mm-help',`第 ${input.frame} 帧 · 请求 ${stamp(input.requested_ms,3)} · 实际 ${stamp(input.actual_ms,3)} · 时间匹配误差 ${finite(input.error_ms)?(input.error_ms>0?'+':'')+input.error_ms+' ms':'未记录或无法配对'}`));comparison.append(el('p','mm-help','时间匹配误差 = 实际原帧时间 − 请求时间，不是视线估计误差。'));currentCapture.append(comparison);}}
      }
    }
    function chart(observations,key) {
      const valid=(observations||[]).filter(o=>finite(o.start_ms)&&finite(o.end_ms));duration=Math.max(1,...valid.map(o=>o.end_ms));
      const values=valid.map(o=>({o,value:o.quality==='valid'?flatten(o.values)[key]:null})),numbers=values.map(x=>x.value).filter(finite);
      const wrap=el('div','mm-chart');if(!numbers.length){wrap.append(el('p','mm-missing','这个指标没有有效数值。请检查声音/画面质量或切换指标。'));return wrap;}
      const lo=Math.min(...numbers),hi=Math.max(...numbers),span=hi-lo||1,low=lo===hi?lo-0.5:lo,high=lo===hi?hi+0.5:hi;
      const graph=svg('svg',{viewBox:'0 0 750 175',role:'group','aria-label':metricName(key).join('，')+'，点击采样点定位原片'}),x=t=>chartLeft+t/duration*chartWidth,y=v=>132-(v-low)/(high-low)*105;
      for(const n of [low,(low+high)/2,high]){graph.append(svg('line',{x1:chartLeft,x2:chartLeft+chartWidth,y1:y(n),y2:y(n),class:'mm-grid'}));const label=svg('text',{x:chartLeft-8,y:y(n)+4,'text-anchor':'end'});label.textContent=n.toFixed(Math.abs(n)<2?2:1);graph.append(label);}
      let path='',connected=false,previousEnd=null;for(const item of values){if(!finite(item.value)){connected=false;previousEnd=item.o.end_ms;continue;}if(previousEnd!=null&&item.o.start_ms>previousEnd)connected=false;path+=(connected?' L':' M')+x(item.o.start_ms).toFixed(2)+','+y(item.value).toFixed(2);connected=true;previousEnd=item.o.end_ms;}graph.append(svg('path',{d:path,class:'mm-line',fill:'none'}));
      const stride=Math.max(1,Math.ceil(values.length/180));values.forEach((item,index)=>{if(index%stride||!finite(item.value))return;const point=svg('circle',{cx:x(item.o.start_ms),cy:y(item.value),r:3.5,tabindex:0,role:'button','aria-label':`${stamp(item.o.start_ms)} ${metricName(key)[0]} ${item.value}`,class:'mm-point'}),title=svg('title');title.textContent=`${stamp(item.o.start_ms)} · ${item.value}`;point.append(title);const seek=()=>timeline.seek(item.o.start_ms).catch(e=>status.textContent=e.message);point.addEventListener('click',seek);point.addEventListener('keydown',e=>{if(e.key==='Enter'||e.key===' '){e.preventDefault();seek();}});graph.append(point);});
      cursor=svg('line',{x1:chartLeft,x2:chartLeft,y1:20,y2:138,class:'mm-cursor'});graph.append(cursor);
      for(const t of [0,duration/2,duration]){const label=svg('text',{x:x(t),y:159,'text-anchor':t===0?'start':t===duration?'end':'middle'});label.textContent=stamp(t);graph.append(label);}wrap.append(graph,el('p','mm-help',`横轴：原片时间 · 纵轴：${metricName(key)[1]} · ${values.length} 个分析窗口${stride>1?'，可点击点每 '+stride+' 窗显示一次':''}。缺失段断开。`));return wrap;
    }
    function renderRaw(observations,host) {
      const details=el('details','mm-raw'),summary=el('summary','','逐窗原始记录（'+observations.length+'）'),list=el('div'),controls=el('div','mm-pages');details.append(summary,list,controls);let ready=false;
      function draw(){list.replaceChildren();controls.replaceChildren();const start=page*12;for(const o of observations.slice(start,start+12)){const row=el('div','mm-raw-row');row.append(button(stamp(o.start_ms)+'–'+stamp(o.end_ms),()=>timeline.seek(o.start_ms)),el('span','',o.reason||o.quality),el('code','',JSON.stringify({values:o.values,sample_index:o.sample_index,landmark_count:o.landmarks?.length,gaze_arrows:o.gaze_arrows?.length,quality_flags:o.quality_flags,f0_reason:o.f0_reason,track_segment_id:o.track_segment_id,centre_ms:o.centre_ms,last_input_ms:o.last_input_ms,ready_at:o.ready_at,input_frame_times_ms:o.input_frame_times_ms,requested_frame_times_ms:o.requested_frame_times_ms,input_frame_time_errors_ms:o.input_frame_time_errors_ms,max_input_frame_error_ms:o.max_input_frame_error_ms,input_timing_comparison:o.requested_frame_times_ms?frameTimings(o):undefined,capture:o.capture,input_captures:o.input_captures})));list.append(row);}const back=button('上一页',()=>{page--;draw();}),next=button('下一页',()=>{page++;draw();});back.disabled=page===0;next.disabled=start+12>=observations.length;controls.append(back,el('span','',observations.length?`${start+1}–${Math.min(start+12,observations.length)} / ${observations.length}`:'没有记录'),next);}
      details.addEventListener('toggle',()=>{if(details.open&&!ready){ready=true;draw();}});host.append(details);
    }
    function render() {
      body.replaceChildren();cursor=null;currentCapture=null;currentLabel=el('p','mm-window');currentMetrics=el('div','mm-current');selectedMetric='';page=0;
      for(const b of tabs.children){const on=b.dataset.module===active;b.setAttribute('aria-selected',String(on));b.tabIndex=on?0:-1;}
      const module=modules[active];
      const origin=data?.module_origins?.[active==='geometry'?(modules.gaze?.geometry?'gaze':'au'):active];
      if(origin)context.textContent=`当前原话 v${data.transcript_revision} · 采集输入 v${origin.analysis_input_transcript_revision} · ${origin.phase==='extended'?'扩展观察':origin.phase==='core'?'基础观察':'冻结观察'} · 运行 ${origin.run_id||'未记录'}`+(origin.projection?' · 按同一原片关联当前原话':'');
      if(active==='geometry'){
        body.append(el('p','mm-help','青色：真实面部检测点；黄色：预测视线方向。只显示有效采样区间，未采样时不延用旧点。'));
        if(!geometry){body.append(el('p','mm-missing','这次运行没有保存真实点位。旧数值结果仍可查看；使用新模型版本重新运行后才会生成点位。'));}
        else{const controls=el('div','mm-controls');for(const [name,label]of [['points','显示面部点位'],['gaze','显示视线方向']]){const l=el('label'),c=el('input');c.type='checkbox';c.checked=name==='points'?showPoints:showGaze;c.addEventListener('change',()=>{if(name==='points')showPoints=c.checked;else showGaze=c.checked;timeline.refreshTime?.();});l.append(c,document.createTextNode(label));controls.append(l);}const first=geometry.observations?.find(o=>o.quality==='valid');if(first)controls.append(button('查看首个有效采样点',()=>timeline.seek(first.start_ms)));body.append(controls,currentLabel,currentMetrics);const origin=el('details','mm-provenance');origin.append(el('summary','',`采样来源 · ${geometry.analysis_fps??'—'} fps · ${geometry.sampled_frame_count??geometry.observations?.length} 个窗口`),el('p','mm-help',geometry.notice||'方向为相机坐标估计，未经屏幕校准。'),el('p','mm-help',`源帧 ${geometry.source_frame_count??'—'} · 点位间隔 ${geometry.sample_stride??1} 帧`));body.append(origin);renderRaw(geometry.observations||[],body);}
      }else if(!module){body.append(el('p','mm-missing','本次未选择该分析模块。'));}
      else{
        const observations=module.observations||[],valid=observations.filter(o=>o.quality==='valid');body.append(el('p','mm-help',(states[module.status]||module.status||'状态未知')+' · 有效窗口 '+valid.length+'/'+observations.length+(module.reason?' · '+module.reason:'')));
        if(active==='au_net')body.append(el('p','mm-help','原 AU-Net 权重的离线时序观察：每个窗口读取前、中、后三帧。激活分值不是 OpenFace 强度或校准概率。人脸预处理采用 YuNet 五点，差异见模型来源。'));
        if(active==='gaze360')body.append(el('p','mm-help','Gaze360 独立视线估计：以 8 Hz 请求七个时刻（间隔 125 毫秒，中心前后各 375 毫秒），取最接近的原始帧。请求和实际时间可逐帧核对；这不是任意七个相邻视频帧。结果包含中心之后的输入，方向及模型角区间需结合坐标定义读取；未经屏幕校准，不代表屏幕注视位置。'));
        if(active==='emotic')body.append(el('p','mm-help','EMOTIC 使用人物裁剪与完整场景两路输入，显示愤怒、开心、悲伤、惊讶四类场景辅助感知情绪分值。它与面部动作编码不同，不作为真实内心状态、人格或岗位评分。采集框表示实际输入范围，不是模型结论的因果解释。'));
        if(active==='audio'){const s=module.summary||{},stats=el('div','mm-stats');for(const [name,value,unit]of [['发声',s.speech_ms==null?null:s.speech_ms/1000,'秒'],['停顿',s.pause_count,'次'],['语速',s.speech_rate_units_per_min,'转写单位/分']])stats.append(el('span','',name+' '+(finite(value)?value.toFixed(name==='停顿'?0:1):'—')+' '+unit));body.append(stats);}
        const metrics=[...new Set(observations.flatMap(o=>Object.keys(flatten(o.values))))].filter(k=>!k.endsWith('_frames'));
        if(metrics.length){const select=el('select'),label=el('label','mm-metric-label','观察指标');for(const key of metrics){const o=el('option','',metricName(key).join(' · '));o.value=key;select.append(o);}selectedMetric=metrics.includes(metricSelections[active])?metricSelections[active]:active==='audio'&&metrics.includes('f0_hz')?'f0_hz':active==='gaze'&&metrics.includes('gaze_angle_x')?'gaze_angle_x':metrics[0];select.value=selectedMetric;label.append(select);const chartHost=el('div');const draw=()=>{selectedMetric=select.value;metricSelections[active]=selectedMetric;chartHost.replaceChildren(chart(observations,selectedMetric));update(currentTime);};select.addEventListener('change',draw);body.append(label,currentLabel,currentMetrics,chartHost);draw();}
        else body.append(currentLabel,currentMetrics);
        if(['gaze360','emotic'].includes(active)){const capturePanel=el('section','mm-capture');capturePanel.append(el('h4','','本窗口采集来源'));currentCapture=el('div');capturePanel.append(currentCapture);body.append(capturePanel);}
        renderRaw(observations,body);
        const provenance=el('details','mm-provenance');provenance.append(el('summary','','模型、采样与单位'),el('p','mm-help',module.model_id||module.model||'来源未记录'));const pre=el('pre','',JSON.stringify({source:origin,provenance:module.provenance,summary:active==='emotic'?undefined:module.summary,notices:module.notices},null,2));provenance.append(pre);body.append(provenance);
      }
      update(currentTime);
    }
    tabs.setAttribute('role','tablist');tabs.setAttribute('aria-label','多模态观察通道');
    for(const key of keys){const b=button(names[key],()=>{active=key;render();});b.dataset.module=key;b.setAttribute('role','tab');tabs.append(b);}
    tabs.addEventListener('keydown',event=>{if(!['ArrowLeft','ArrowRight'].includes(event.key))return;event.preventDefault();const index=keys.indexOf(active);active=keys[(index+(event.key==='ArrowRight'?1:-1)+keys.length)%keys.length];render();[...tabs.children].find(b=>b.dataset.module===active)?.focus();});
    render();const unsubscribe=timeline?.subscribeTime?.(update)||(()=>{});
    return {element:root,dispose(){disposed=true;unsubscribe();detachOverlay();},setChannel(key,metric){if(keys.includes(key)){active=key;if(typeof metric==='string')metricSelections[key]=metric;render();}}};
  }
  window.SuitMultimodalView={mount,windowAt,captureAt,captureBoxes,frameTimings,emoticValues,flatten,metricName};
})();
