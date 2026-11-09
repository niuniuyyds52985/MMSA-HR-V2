(() => {
  'use strict';
  // Keep internal evidence IDs in the source record, not the reading summary.
  const modelReason = text => String(text || '').replace(/[（(]\s*(?:eu|gap)_\d+(?:\s*[,，、]\s*(?:eu|gap)_\d+)*\s*[）)]/g,'');
  const names={audio:'声音',expression:'表情类别',au:'面部动作',gaze:'OpenFace 视线',au_net:'AU-Net 动作',gaze360:'Gaze360 视线',emotic:'场景辅助感知情绪'};
  const states={complete:'已完成',succeeded:'已完成',partial:'部分完成',failed:'未完成',unavailable:'不可用',queued:'排队中',running:'处理中',waiting_media:'素材准备中',waiting_worker:'等待分析',paused:'分析已暂停',not_applicable:'无适用素材',no_video:'没有视频',no_audio:'没有音轨',no_valid_observations:'没有有效采样',no_media:'未采集音视频'};
  const el=(tag,cls,text)=>{const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=window.SuitTextDisplay?.simplify(String(text))??String(text);return n;};
  const stamp=ms=>Number.isFinite(ms)?Math.floor(ms/60000).toString().padStart(2,'0')+':'+(ms%60000/1000).toFixed(1).padStart(4,'0'):'时间未记录';
  function action(label,fn){const b=el('button','reader-evidence',label);b.type='button';b.addEventListener('click',()=>Promise.resolve().then(fn).catch(error=>{let n=b.parentElement.querySelector('.reader-error');if(!n){n=el('p','reader-error');b.parentElement.append(n);}n.textContent=error.message||'无法定位此条证据';}));return b;}
  function selectAnalysis(report,answer){
    const automatic=report?.standard_analysis?.runs||report?.standard_analysis?.selected_runs||[],research=report?.research_appendix?.selected_runs||[];
    const ordered=list=>[...list].sort((a,b)=>(Number(b.attempt)||0)-(Number(a.attempt)||0)||String(b.completed_at||b.created_at||'').localeCompare(String(a.completed_at||a.created_at||'')));
    const modules={},origins={},sources=[];
    for(const run of [...ordered(automatic),...ordered(research)])for(const item of run.result?.multimodal||run.multimodal||[]){
      if(item.answer_id!==answer.answer_id||item.transcript_revision!==answer.transcript_revision)continue;
      const selected=[];
      for(const [key,module]of Object.entries(item.modules||{}))if(!Object.hasOwn(modules,key)){
        modules[key]=module;selected.push(key);origins[key]={run_id:run.run_id,phase:run.phase,automatic:automatic.includes(run),analysis_input_transcript_revision:item.analysis_input_transcript_revision??item.transcript_revision,projection:item.projection||null};
      }
      if(selected.length)sources.push({item,run,modules:selected,automatic:automatic.includes(run)});
    }
    if(!sources.length)return null;
    const rank=source=>source.run.phase==='extended'?1:source.automatic?0:2;
    sources.sort((a,b)=>rank(a)-rank(b));
    return {item:{answer_id:answer.answer_id,transcript_revision:answer.transcript_revision,modules,module_origins:origins},run:{},sources,automatic:sources.every(x=>x.automatic)};
  }
  function evidence(holder,refs,onEvidence,label='定位原话与录像'){
    for(const ref of refs||[]){if(!ref?.quote)continue;const row=el('blockquote','reader-quote');row.append(el('p','','“'+ref.quote+'”'));if(ref.answer_id&&Number.isInteger(ref.transcript_revision)&&onEvidence)row.append(action(label,()=>onEvidence(ref)));holder.append(row);}
  }
  const expressionNames={happy:'开心样式',sad:'悲伤样式',angry:'生气样式',surprised:'惊讶样式',neutral:'中性样式',fearful:'害怕样式',disgust:'厌恶样式'};
  const contextNames={Happiness:'开心',Sadness:'悲伤',Anger:'生气',Surprise:'惊讶'};
  const number=(value,digits=1)=>Number.isFinite(value)?value.toFixed(digits).replace(/\.0+$/,''):'未记录';
  function fold(title,cls=''){const d=el('details','reader-secondary '+cls);d.append(el('summary','',title));return d;}
  function note(parent,text,cls='reader-measurement-line'){parent.append(el('p',cls,text));}
  function decision(event,{onEvidence,factor,question}={}){
    const root=el('section','reader-decision'),s=event.decision_summary||{},branch=s.selected_branch,coverage=s.coverage_rules||s.dictionary_coverage||s.coverage||[];
    const semantic=s.model_involved_in_selection===true&&s.semantic_check,source=event.decision_source||s.decision_source;
    root.append(el('h4','',event.action==='ask_followup'?'这次追问的来由':'这一步为什么继续'));
    if(factor){const theory=el('section','reader-potential-reasoning');theory.append(el('h5','','潜力模型视角'),el('strong','',factor.dimension_name+' / '+factor.name));note(theory,factor.definition||'按本题对应因子的行为证据核对。');
      const targets=[s.target_evidence||event.target_evidence,...coverage.filter(r=>r.status==='needs_clarification').map(r=>r.target_evidence)].filter(Boolean);
      note(theory,targets.length?'当时待补的证据：'+[...new Set(targets)].join('；'):'当时未定位到新的可执行追问目标；需结合原话和流程限制核对。','reader-model-gap');
      if(question?.anchors?.['3'])note(theory,'本题基本锚点：'+question.anchors['3'],'small');
      const laterGaps=question?.model_evaluation?.gaps||[];if(laterGaps.length){const details=fold('后续内容模型核对 · 不改写当时决定');for(const gap of laterGaps.slice(0,3))if(gap.description)note(details,gap.description);theory.append(details);}
      note(theory,'待补的是回答证据，不能由此直接断言该因子能力不足。','small muted');root.append(theory);}
    root.append(el('h5','reader-routing-heading','实际选题与词典依据'));
    if(semantic)note(root,'当时由模型核对词典线索后选题 · '+(event.model_id||'已记录模型'),'reader-decision-source');
    else if(source==='dictionary-bank.v2-fallback')note(root,'当时采用本地词典规则 · 语义核对未通过或超时','reader-decision-source');
    else if(source==='workflow-budget')note(root,'按题组与追问次数限制继续','reader-decision-source');
    if(semantic){const view=fold('查看当时的语义核对');const labels={covered:'已有内容',gap:'待补证据',not_applicable:'当前不适用',uncertain:'尚不能确定'};for(const check of s.semantic_check.branches||[]){const cov=coverage.find(c=>c.branch_id===check.branch_id);note(view,(cov?.target_evidence||'本题追问目标')+'：'+(labels[check.status]||check.status));note(view,check.reason);evidence(view,check.evidence,onEvidence);}root.append(view);}
    const steps=el('div','reader-decision-steps');
    const step=(label)=>{const row=el('div','reader-decision-step');row.append(el('h5','',label));steps.append(row);return row;};
    const said=step('已说到'),refs=(s.used_evidence||event.used_evidence||[]).filter(ref=>ref?.quote);
    evidence(said,refs.slice(0,2),onEvidence);
    if(!refs.length)note(said,'当时的记录未附可定位原话。');
    if(refs.length>2){const more=fold('查看其余 '+(refs.length-2)+' 条原话');evidence(more,refs.slice(2),onEvidence);said.append(more);}
    const gaps=step('待补充'),targets=[s.target_evidence||event.target_evidence,...coverage.filter(rule=>rule.status==='needs_clarification').map(rule=>rule.target_evidence)].filter(Boolean);
    const unique=[...new Set(targets)];
    note(gaps,unique.length?unique.slice(0,2).join('；'):'此步未记录新的补充目标。');
    if(unique.length>2){const more=fold('其余待核对目标');for(const target of unique.slice(2))note(more,target);gaps.append(more);}
    const chosen=step(event.action==='ask_followup'?'所以问':'所以继续');
    const branchText=typeof branch==='string'?branch:branch?.text||branch?.question;
    if(branchText)note(chosen,branchText,'reader-branch');
    else if(event.action==='ask_followup'&&event.actual_next_round?.text)note(chosen,event.actual_next_round.text,'reader-branch');
    note(chosen,s.brief_reason||event.reason_for_hr||event.stop_reason||'此历史决定未记录简要说明。');
    const later=step('追答与后续核对'),comparisons=[...(event.model_comparison||[])];
    const laterEval=question?.model_evaluation;if(!comparisons.length&&laterEval?.created_at&&event.decision_time&&laterEval.created_at>event.decision_time)comparisons.push({...laterEval,same_input_as_decision:JSON.stringify(laterEval.input_versions)===JSON.stringify(event.input_versions)});
    if(comparisons.length){
      note(later,'已有 '+comparisons.length+' 份后续模型核对，没有改写当时的追问。');
      const details=fold('查看后续模型核对');
      for(const model of comparisons){note(details,model.followup?.reason||'未记录可读核对理由。');note(details,model.same_input_as_decision?'与决策使用相同文字版本':'输入版本与当时决定不同；请定位核对新增或修订的原话。','small muted');evidence(details,model.followup?.evidence,onEvidence);}
      later.append(details);
    }else note(later,event.checkpoint_kind==='followup'?'这次追答已保存；此记录暂未附后续模型核对。':'此记录暂未附后续模型核对；原话和当时决定可先核对。');
    root.append(steps);
    const audit=fold('选题规则、词表与版本','reader-decision-audit');
    if(coverage.length){const list=el('div','reader-coverage-rules');for(const rule of coverage){const row=el('div');row.append(el('strong','',rule.target_evidence||rule.criterion_id||rule.branch_id),el('span','reader-tag',rule.status==='lexical_cue_present'?'检测到内容线索':'待澄清'));for(const hit of (rule.hits||[]).filter(h=>h.retained_by_context_rule!==false).slice(0,3))if(hit.quote)row.append(el('p','small','原句：“'+hit.quote+'”'));list.append(row);}audit.append(list);}
    if(s.dictionary_snapshot){const dict=s.dictionary_snapshot,matches=dict.matches||[],d=fold('原词表命中对照 · '+matches.length+' 处');d.append(el('p','small muted',dict.involved_in_selection===true?'此记录明确列为选题输入。':'原词表用于对照，不参与本次选题；选题依据为题库目标覆盖规则。'));
      for(const hit of matches){const p=el('p','reader-dictionary-hit');p.append(el('strong','',hit.term||''),el('span','',hit.retained===false?' · 已由上下文规则排除':' · 保留为词语线索'));if(hit.quote)p.append(el('span','','：“'+hit.quote+'”'));if(hit.source_cell)p.append(el('small','',' · 来源 '+hit.source_cell));if(hit.flags?.length)p.append(el('small','',' · '+hit.flags.join('、')));if(hit.quote&&hit.answer_id&&Number.isInteger(hit.transcript_revision)&&onEvidence)p.append(action('查看命中原句',()=>onEvidence(hit)));d.append(p);}if(!matches.length)note(d,'未检出词表短语，不代表缺少相关能力。');audit.append(d);}
    note(audit,event.decision_summary?'题库覆盖规则 '+(s.coverage_policy_version||event.policy_version||'未记录')+' · 内容模型异步生成评分草稿':'历史政策 '+(event.policy_version||'未记录'),'small');
    note(audit,'决定时间：'+(event.decision_time||'未记录'),'small');
    if(branch?.branch_id||event.selected_branch_id)note(audit,'题库分支 '+(branch?.branch_id||event.selected_branch_id)+(branch?.source_cell?' · 来源 '+branch.source_cell:''),'small');
    root.append(audit);return root;
  }
  function measurementDetails(event,source,{onSeek,onChannel}={}){
    const details=fold('如何得到这条观察','reader-event-basis'),m=event.measurements||{},reference=event.reference||{},geometry=event.geometry;
    if(event.kind==='audio_pitch'||event.kind==='audio_energy'){
      const pitch=event.kind==='audio_pitch',unit=pitch?'Hz':'dBFS';
      if(Number.isFinite(m.median))note(details,'本片段'+(pitch?'音高中位数':'录音能量中位数')+'：'+number(m.median,2)+' '+unit+'。');
      if(Number.isFinite(reference.median)){
        const same=reference.scope==='same_answer_other_valid_speech_windows';
        note(details,(same?'参照为本题其他有效有声窗口':'原记录的参照中位数')+'：'+number(reference.median,2)+' '+(reference.unit||unit)+(Number.isFinite(reference.sampled_windows)?'，共 '+reference.sampled_windows+' 个窗口':'')+(same?'（不含当前片段）。':'；记录未说明参照范围。'));
      }else note(details,'该记录未附可量化的参照值。');
      if(Number.isFinite(m.difference))note(details,'相对差值：'+(m.difference>0?'高 ':m.difference<0?'低 ':'无变化 ')+number(Math.abs(m.difference),2)+' '+(pitch?'半音':'dB')+'。');
      if(Number.isFinite(m.sampled_windows))note(details,'片段由 '+m.sampled_windows+' 个有效采样窗形成。');
      note(details,pitch?'音高描述声音频率，不能据此断言音色或激昂等情绪。':'dBFS 是录音数字能量，不是环境声压；设备增益和麦克风距离也会影响数值。','small muted');
    }else if(event.kind==='apparent_expression'){
      const ranked=Object.entries(m.raw_scores||{}).filter(([,v])=>Number.isFinite(v)).sort((a,b)=>b[1]-a[1]);if(ranked.length>=2)note(details,'代表帧排名前两类：'+ranked.slice(0,2).map(([k,v])=>(expressionNames[k]||k)+' '+number(v,2)).join('；')+'（原始分值，不是概率）。','reader-class-comparison');
      if(Number.isFinite(m.sampled_frames))note(details,m.sampled_frames+' 个有效采样帧被预测为'+(expressionNames[event.label]||'该表情类别')+'；采样间隔不代表连续情绪持续。');
      const aus=(m.au||[]).filter(value=>value&&typeof value.code==='string'&&(Number.isFinite(value.intensity)||value.presence===0||value.presence===1));
      if(aus.length){note(details,'同期另一个算法测到的面部动作：');const list=el('ul','reader-measurement-list');for(const au of aus){let text=au.code+(au.name?'（'+au.name+'）':'');if(Number.isFinite(au.intensity))text+='：强度估计 '+number(au.intensity,2)+'/5';if(au.presence===0||au.presence===1)text+='；存在检测 '+(au.presence===1?'检出':'未检出');list.append(el('li','',text+'。'));}details.append(list);note(details,'动作存在与强度分别预测，可能不一致；同期线索不是表情分类器的因果解释。','small muted');}
      else note(details,'此片段没有附上可核对的同期面部动作值。');
      if(geometry){
        const metrics=geometry.metrics||{};
        if(Number.isFinite(metrics.mouth_width_over_eye_distance))note(details,'实际嘴角间距（点 48、54）为双眼中心距的 '+number(metrics.mouth_width_over_eye_distance,3)+' 倍。');
        if(Number.isFinite(metrics.inner_lip_gap_over_eye_distance))note(details,'实际内唇间距（点 62、66）为双眼中心距的 '+number(metrics.inner_lip_gap_over_eye_distance,3)+' 倍。');
        if(Array.isArray(geometry.landmarks))note(details,'同期记录 '+geometry.landmarks.length+' 个面部点位'+(Number.isFinite(geometry.frame_width)&&Number.isFinite(geometry.frame_height)?'，采样画面 '+geometry.frame_width+' × '+geometry.frame_height+' 像素':'')+'。','small');
        note(details,'以上是按实际采样帧宽高计算的二维距离比；会受姿态影响，不等同肌肉强度。','small muted');
        if(onSeek)details.append(action('暂停核对实际采集点',()=>{onChannel?.('geometry');return onSeek(geometry.anchor_ms??event.anchor_ms??event.start_ms,{play:false});}));
      }else note(details,'此片段没有同期点位记录，不补绘推测点位。','small muted');
    }else if(event.kind==='context_expression'){
      if(Number.isFinite(m.sampled_frames))note(details,'本条合并 '+m.sampled_frames+' 个相邻同标签有效采集帧；不代表连续情绪持续。');
      note(details,'下列分值与阈值对应首个代表帧，线性分值不是概率或情绪强度。','small muted');
      for(const [key,label]of Object.entries(contextNames))if(Number.isFinite(m.category_scores?.[key])&&Number.isFinite(m.category_thresholds?.[key]))note(details,label+'：原始分值 '+number(m.category_scores[key],3)+'，所用阈值 '+number(m.category_thresholds[key],3)+'。');
      note(details,'模型同时使用人物裁剪和完整场景，不是单凭面部表情得出的结果。','small muted');
    }else note(details,'此历史条目未提供可解释的测量格式；请结合原片与原始记录核对。');
    if(Number.isFinite(event.last_input_ms)&&Number.isFinite(event.end_ms)&&event.last_input_ms>event.end_ms)note(details,'此条描述还使用截至 '+stamp(event.last_input_ms)+' 的输入，属于事后汇总。','small muted');
    const technical=fold('原始技术记录','reader-technical');technical.append(el('p','small','来源运行 '+(source.item.reader_summary?.source_run_id||source.run.run_id||'未记录')),el('pre','reader-measurements',JSON.stringify({measurements:event.measurements,reference:event.reference,geometry:event.geometry,evidence_refs:event.evidence_refs,quality:event.quality},null,2)));details.append(technical);return details;
  }
  function attachSpeechContext(card,event,options){
    if(!options.timelinePromise)return;
    const box=el('div','reader-spoken-context');note(box,'正在对应这段原话…','small muted');card.append(box);
    options.timelinePromise.then(data=>{if(!box.isConnected)return;box.replaceChildren();const refs=window.SuitObservationContext?.segmentQuotes(data,event)||[];
      note(box,'说这句话时','reader-context-title');
      if(!refs.length){note(box,'这个采样时段没有可靠对齐的原话，不按文字长度猜测。','small muted');return;}
      for(const ref of refs.slice(0,3)){const q=el('blockquote','reader-quote');q.append(el('p','','“'+ref.quote+'”'),el('small','muted','原话句段 '+stamp(ref.start_ms)+'–'+stamp(ref.end_ms)+' · 观察窗口 '+stamp(event.start_ms)+'–'+stamp(event.end_ms)));box.append(q);}
      if(refs.length>3)note(box,'还覆盖 '+(refs.length-3)+' 个句段，可在播放器中查看。','small muted');
      note(box,'按句段时间重叠关联，不能判断变化发生在某个字上。','reader-context-note');
    }).catch(()=>{if(box.isConnected)box.replaceChildren(el('p','small muted','原话对应暂未加载，可通过下方原片按钮重试。'));});
  }
  function expressionDistribution(analysis,options){
    const module=analysis.item?.modules?.expression,summary=window.SuitObservationContext?.expressionSamples(module);if(!summary?.total)return null;
    const root=el('section','reader-expression-distribution');root.append(el('h6','','全部表情采样分布'),el('p','small muted','有效采样 '+summary.valid.length+' / '+summary.total+' 帧；占比按有效帧计算，不是情绪时长或真实感受。'));
    const sorted=[...summary.rows].sort((a,b)=>b.count-a.count);for(const row of sorted){const line=el('div','reader-expression-row');line.append(el('span','',row.name));const track=el('span','reader-expression-track'),fill=el('span');fill.style.width=(100*(row.fraction||0)).toFixed(1)+'%';track.append(fill);line.append(track,el('span','reader-expression-count',row.count+' 帧'+(row.fraction!==null?' · '+(row.fraction*100).toFixed(1)+'%':'')));
      if(row.example){const o=row.example.observation,preview=el('div','reader-event-replay');preview.hidden=true;const event={kind:'apparent_expression',label:row.label,start_ms:o.start_ms,end_ms:o.end_ms,anchor_ms:o.start_ms,measurements:{sampled_frames:1,raw_scores:o.values.raw_scores}};
        line.append(action('看'+row.name+'样本',async()=>{preview.hidden=false;if(options.onPreview)await options.onPreview(event,preview);else{options.onChannel?.('geometry');await options.onSeek?.(o.start_ms,{play:false});}}));root.append(line,preview);
      }else{line.append(el('small','muted','未检出此类样本'));root.append(line);}}
    note(root,'点位仅在该时刻有有效采样时叠加；缺失时不补画。模型输出仍需人工看原片校准；没有检出开心样本不等于没有开心表情。','small muted');return root;
  }
  function eventCard(entry,options){
    const {event,source}=entry,card=el('article','reader-event');card.append(el('div','reader-event-time',stamp(event.start_ms)+'–'+stamp(event.end_ms)),el('strong','',event.title||'采样观察'),el('p','',event.kind==='apparent_expression'?((event.measurements?.sampled_frames??'未记录')+' 个有效采样帧被预测为'+(expressionNames[event.label]||'该类别')+'；请查看原片核对。'):(event.description||'')));
    attachSpeechContext(card,event,options);const hint=window.SuitObservationContext?.readingHint(event);if(hint)note(card,hint,'reader-observation-meaning');
    const channel=event.evidence_refs?.find(ref=>source.modules.includes(ref.module||ref.module_id));
    const metric=event.kind==='gaze_direction'?'gaze_angle_x':event.kind==='audio_pitch'?'f0_hz':event.kind==='audio_energy'?'rms_dbfs':event.kind==='apparent_expression'&&Object.hasOwn(expressionNames,event.label)?'raw_scores.'+event.label:null;
    if(Number.isFinite(event.anchor_ms??event.start_ms)&&options.onSeek){const preview=el('div','reader-event-replay');preview.hidden=true;card.append(action(options.onPreview?(event.kind==='apparent_expression'?'原片与面部点位一起看':'听这句话，查看同步原片'):'定位对应片段',async()=>{if(options.onPreview){preview.hidden=false;return options.onPreview(event,preview);}options.onChannel?.(event.kind==='gaze_direction'?'gaze':channel?.module||channel?.module_id,metric);return options.onSeek(event.anchor_ms??event.start_ms,{play:false});}),preview);}
    card.append(measurementDetails(event,source,options));return card;
  }
  function highlights(entries,category){
    const ordered=[...entries].sort((a,b)=>{
      if(category==='audio')return String(a.event.kind).localeCompare(String(b.event.kind))||Math.abs(b.event.measurements?.difference||0)-Math.abs(a.event.measurements?.difference||0)||a.event.start_ms-b.event.start_ms;
      if(category!=='audio')return (b.event.measurements?.sampled_frames||0)-(a.event.measurements?.sampled_frames||0)||a.event.start_ms-b.event.start_ms;
      return a.event.start_ms-b.event.start_ms;
    });
    const picked=[],seen=new Set();
    for(const entry of ordered){const kind=category==='audio'?entry.event.kind:category==='expression'?entry.event.label:'window:'+entry.event.start_ms;if(!seen.has(kind)&&picked.length<(category==='gaze'?1:2)){picked.push(entry);seen.add(kind);}}
    for(const entry of ordered)if(category!=='expression'&&picked.length<(category==='gaze'?1:2)&&!picked.includes(entry))picked.push(entry);
    return picked.sort((a,b)=>a.event.start_ms-b.event.start_ms);
  }
  function observationGroup(title,category,entries,options,facts=[]){
    const group=el('section','reader-observation-group');group.dataset.category=category;group.append(el('h5','',title));
    for(const fact of [...new Set(facts)].slice(0,1))note(group,fact,'reader-group-overview');
    if(!entries.length){note(group,'尚未形成可定位的重点片段；可展开“采样与来源”核对状态。','small muted');return group;}
    const selected=highlights(entries,category),more=entries.filter(entry=>!selected.includes(entry));
    note(group,'先看 '+selected.length+' 个重点片段；这不是全场统计。','reader-highlight-notice');
    const primary=el('div','reader-highlight-events');for(const entry of selected)primary.append(eventCard(entry,options));group.append(primary);
    if(more.length){const extra=fold('查看其余 '+more.length+' 个片段（共 '+entries.length+' 个）','reader-more-events');for(const entry of more)extra.append(eventCard(entry,options));group.append(extra);}
    return group;
  }
  function provenance(sources,options){
    const details=fold('采样与来源','reader-provenance');note(details,'有效采样比例描述记录覆盖，不是情绪持续时间。未采样间隔不插值。','small muted');
    for(const source of sources){const summary=source.item.reader_summary,section=el('div','reader-source-record');
      for(const key of source.modules){const module=source.item.modules[key];note(section,(names[key]||key)+' · '+(states[module.status]||module.status||'状态未记录'),'reader-source-status');
        if(module.reason)note(section,module.reason,'small muted');
        const coverage=summary?.coverage?.[key];if(coverage)note(section,'有效采样 '+coverage.valid_observations+' / '+coverage.sampled_observations+'，排除 '+coverage.excluded_observations+' 个窗口。','reader-coverage');
        if(module.model_id)note(section,'模型：'+module.model_id,'small muted');
      }
      note(section,'来源运行：'+(summary?.source_run_id||source.run.run_id||'未记录')+' · 文字版本 '+(source.item.transcript_revision??'未记录'),'small muted');
      if(!summary)note(section,'此历史运行没有可读摘要；可核对实际模型输出与采集窗口。','small muted');
      const examples=fold('查看面部动作与视线采集示例','reader-sample-examples');let count=0;
      for(const key of source.modules.filter(k=>['gaze','gaze360','au','au_net'].includes(k))){
        const module=source.item.modules[key],o=(module.observations||[]).find(o=>o.quality==='valid'),v=o?.values||{};let text='';
        if(key==='gaze360'&&v.yaw_pitch_radians?.length===2&&v.yaw_pitch_radians.every(Number.isFinite))text='水平角 '+v.yaw_pitch_radians[0].toFixed(3)+' rad，俯仰角 '+v.yaw_pitch_radians[1].toFixed(3)+' rad；相机坐标下的方向估计。';
        if(key==='gaze'&&Number.isFinite(v.gaze_angle_x)&&Number.isFinite(v.gaze_angle_y))text='水平角 '+v.gaze_angle_x.toFixed(3)+' rad，垂直角 '+v.gaze_angle_y.toFixed(3)+' rad；可核对同期面部点位。';
        if(key==='au'||key==='au_net'){const values=v.intensity||v.activation_scores||{},numbers=Object.entries(values).filter(([,n])=>Number.isFinite(n)).sort((a,b)=>b[1]-a[1]).slice(0,3);if(numbers.length)text=numbers.map(([au,n])=>au+' '+n.toFixed(2)).join('，')+(key==='au'?'（OpenFace 强度 0–5）':'（AU-Net 激活值，非概率）');}
        if(text){const row=el('div','reader-sample-example');note(row,(names[key]||key)+' · '+stamp(o.capture?.at_ms??o.start_ms)+' · 首个有效采样','small');note(row,text);if(options.onSeek)row.append(action('核对这个采集窗口',()=>{options.onChannel?.(key);return options.onSeek(o.capture?.at_ms??o.start_ms,{play:false});}));examples.append(row);count++;}
        else if(module.observations_omitted||module.observation_count)note(section,'逐窗观测保留在对应模型详情中。','small muted');
      }
      if(count)section.append(examples);details.append(section);
    }return details;
  }
  function observations({analysis,answer,status,onSeek,onChannel,getTimeline,onPreview}={}){
    const root=el('section','reader-observations');root.append(el('h4','','同题表达与原话')); 
    if(!answer.has_media){root.append(el('p','muted','本题采用文字作答，未采集音视频。'));return root;}
    if(!analysis){root.append(el('p','reader-pending',states[status]||'本题观察尚未就绪。已保存的回答与原片仍可查看。'));return root;}
    const options={onSeek,onChannel,onPreview,timelinePromise:getTimeline?Promise.resolve().then(getTimeline).catch(()=>null):null},main=[],extended=[],entries=[];
    for(const source of analysis.sources){const isExtended=source.run.phase==='extended'||source.modules.every(key=>['gaze360','emotic','au_net'].includes(key));(isExtended?extended:main).push(source);
      for(const event of source.item.reader_summary?.events||[])if(!event.evidence_refs?.length||event.evidence_refs.some(ref=>source.modules.includes(ref.module||ref.module_id)))entries.push({event,source,extended:isExtended});
    }
    const ordinary=entries.filter(entry=>!entry.extended),audioFacts=[],faceFacts=[];
    for(const source of main){const summary=source.item.reader_summary;if(!summary)continue;const audio=summary.audio_summary||{},counts=summary.expression_summary?.label_counts||{};
      if(source.modules.includes('audio')&&Number.isFinite(audio.speech_rate_units_per_min))audioFacts.push('整题语速约每分钟 '+number(audio.speech_rate_units_per_min,0)+' 个中文字符／英文数字词元；不是逐句语速。');
      const ranked=Object.entries(counts).filter(([key,value])=>Object.hasOwn(expressionNames,key)&&Number.isFinite(value)&&value>0).sort((a,b)=>b[1]-a[1]);
      if(source.modules.includes('expression')&&ranked.length)faceFacts.push('有效采样中，'+expressionNames[ranked[0][0]]+'的预测帧最多；这是模型类别，不代表真实感受。');
    }
    if(main.some(source=>source.modules.includes('audio')))root.append(observationGroup('声音','audio',ordinary.filter(entry=>entry.event.kind?.startsWith('audio_')),options,audioFacts));
    if(main.some(source=>source.modules.includes('expression'))){const faceGroup=observationGroup('表情','expression',ordinary.filter(entry=>entry.event.kind==='apparent_expression'),options,faceFacts),distribution=expressionDistribution(analysis,options);if(distribution){const samples=fold('查看全部表情采样类别与样本','reader-expression-samples');samples.append(distribution);faceGroup.append(samples);}root.append(faceGroup);}
    const gaze=ordinary.filter(entry=>entry.event.kind==='gaze_direction');if(gaze.length)root.append(observationGroup('视线方向','gaze',gaze,options));
    const other=ordinary.filter(entry=>!entry.event.kind?.startsWith('audio_')&&entry.event.kind!=='apparent_expression'&&entry.event.kind!=='gaze_direction');
    if(other.length)root.append(observationGroup('其他实际观察','other',other,options));
    if(main.length)root.append(provenance(main,options));
    if(extended.length){const supplement=fold('扩展观察 · '+[...new Set(extended.flatMap(source=>source.modules))].map(key=>names[key]||key).join(' / '),'reader-extended');
      note(supplement,'扩展模型供方法核对；人物与场景标签、视线估计分别保留，不合并为能力分。','small muted');
      const context=entries.filter(entry=>entry.extended&&entry.event.kind==='context_expression');
      if(context.length)supplement.append(observationGroup('场景辅助标签','context',context,options));
      else note(supplement,'未形成可读场景标签；状态和具体采样见下方。','small muted');
      supplement.append(provenance(extended,options));root.append(supplement);
    }
    return root;
  }

  const brief=(value,limit=160)=>{const chars=Array.from(String(value||'').replace(/\s+/g,' ').trim());return chars.length>limit?chars.slice(0,limit).join('')+'…':chars.join('');};
  function contentContext(report,question){
    const answers=(question.answers||[]).filter(a=>a.current!==false),versions=Object.fromEntries(answers.map(a=>[a.answer_id,a.transcript_revision]));
    const sameInputs=inputs=>inputs&&Object.keys(inputs).length===answers.length&&Object.entries(inputs).every(([id,rev])=>versions[id]===rev);
    const jobs=(report.jobs||[]).filter(j=>j.job_type==='content'&&j.question_id===question.question_id&&j.status!=='superseded'&&(!j.input_versions||!Object.keys(j.input_versions).length||sameInputs(j.input_versions)));
    const job=[...jobs].sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||''))).at(0)||null;
    let prior=null;
    if(question.score==null&&!question.model_evaluation&&question.score_source!=='hr'&&!question.review_stale){
      const candidates=(report.model_evaluations||[]).filter(e=>{
        if(e.question_id!==question.question_id||!e.rating||!e.input_versions)return false;
        const covered=Object.keys(e.input_versions),missing=answers.filter(a=>!covered.includes(a.answer_id));
        return covered.length>0&&missing.length>0&&missing.every(a=>a.parent_question_id===question.question_id)&&covered.every(id=>versions[id]===e.input_versions[id])&&answers.some(a=>!a.parent_question_id&&covered.includes(a.answer_id));
      });
      prior=[...candidates].sort((a,b)=>String(b.created_at||'').localeCompare(String(a.created_at||''))).at(0)||null;
    }
    return {answers,job,prior,failed:!!job&&['failed','blocked'].includes(job.status),pending:!!job&&['queued','running'].includes(job.status),retryable:!!job&&['failed','blocked'].includes(job.status)&&Number(job.manual_retry_count||0)<3};
  }
  function contentError(job){
    const reasons={read_timeout:'等待模型返回超时',connect_timeout:'连接模型服务超时',connection_error:'连接模型服务失败',tls_error:'模型服务安全连接失败',proxy_error:'代理连接失败',invalid_response:'模型返回的内容未通过格式校验',incomplete_response:'模型没有完整返回内容',transport_error:'模型服务连接失败或超时'};
    if(job?.error_code==='api_http_error')return '模型服务返回异常'+(job.error_details?.http_status?'（HTTP '+job.error_details.http_status+'）':'');
    return String(reasons[job?.error_code]||job?.error||'本题内容分析尚未成功').replace(/[。.!！\s]+$/,'');
  }
  function contentRecovery(report,question,{onRetry}={}){
    const state=contentContext(report,question);if(!state.failed&&!state.pending)return null;
    const box=el('section','reader-content-recovery');box.setAttribute('aria-label','本题内容分析状态');
    box.append(el('strong','',state.pending?'本题内容分析正在更新':state.prior?'追答合并分析未完成':'本题内容分析未完成'));
    note(box,state.pending?'已有回答与观察可以先核对；本页会在分析完成后更新。':contentError(state.job)+'。回答与已有观察仍然保留。','small');
    if(state.failed){
      if(state.retryable&&onRetry){const b=action('重新分析本题',async()=>{if(b.disabled)return;b.disabled=true;b.textContent='正在提交…';try{await onRetry(question,state.job);b.textContent='已提交，等待后台分析';}catch(error){b.disabled=false;b.textContent='重新分析本题';throw error;}});box.append(b);note(box,'仅重新请求本题文字分析，无需重录。','small muted');}
      else if(!state.retryable)note(box,'本次输入已达到手动重试次数上限，可先由老师核对材料。','small muted');
      if(state.job.attempts)note(box,'后台已尝试 '+state.job.attempts+' 轮。','small muted');
    }
    return box;
  }
  function previousDraft(report,question,{onEvidence,compact=false}={}){
    const {prior}=contentContext(report,question);if(!prior)return null;
    const box=el('section','reader-previous-draft');
    box.append(el('strong','','主问旧初评 · '+(prior.rating.score==null?'未形成分数':prior.rating.score+' / 5')));
    note(box,'尚未纳入追答，仅供对照；不计入当前总分或能力轮廓。','reader-previous-label');
    if(!compact){note(box,modelReason(prior.rating.reason)||'保留上一次成功分析的依据。','small');evidence(box,(prior.rating.evidence||[]).slice(0,2),onEvidence);}
    return box;
  }
  function answerStory(answers,evaluation,onEvidence){
    const holder=el('div','reader-story-evidence'),ids=new Set(answers.map(a=>a.answer_id));
    const units=(evaluation?.evidence_units||[]).filter(unit=>(unit.evidence||[]).some(ref=>ids.has(ref.answer_id)));
    const selected=[...units.filter(u=>['own_action','result','reflection','transfer','goal_constraint'].includes(u.category)),...units].filter((u,i,all)=>all.indexOf(u)===i).slice(0,2);
    if(selected.length){for(const unit of selected){note(holder,'模型证据摘要：'+brief(unit.interpretation||unit.description||'回答中的行为证据'),'reader-story-interpretation');evidence(holder,(unit.evidence||[]).filter(ref=>ids.has(ref.answer_id)).slice(0,1),onEvidence);}}
    else {const answer=answers.find(a=>a.transcript?.trim());if(answer){note(holder,'转写节选 · 尚未形成该部分的证据摘要','small muted');const quote=Array.from(answer.transcript).slice(0,130).join('');evidence(holder,[{answer_id:answer.answer_id,transcript_revision:answer.transcript_revision,quote}],onEvidence);}else note(holder,answers.length?'尚无可读转写，请回听原始回答。':'没有提交这一部分回答。','small muted');}
    return holder;
  }
  function questionStory(report,question,{onEvidence,currentReason}={}){
    const state=contentContext(report,question),evaluation=question.model_evaluation||state.prior,root=el('section','reader-question-story');
    root.append(el('h4','','这道题如何形成当前依据'));
    const steps=el('ol','reader-story-steps'),add=(title)=>{const item=el('li','reader-story-step');item.append(el('h5','',title));steps.append(item);return item;};
    add('主问说了什么').append(answerStory(state.answers.filter(a=>!a.parent_question_id),evaluation,onEvidence));
    const events=report.decision_events?.length?report.decision_events:report.decisions||[],ask=events.find(d=>d.parent_question_id===question.question_id&&d.action==='ask_followup'),followups=state.answers.filter(a=>a.parent_question_id);
    const why=add(ask?'为什么追问':'提问如何推进');
    if(ask){const s=ask.decision_summary||{};note(why,s.brief_reason||ask.reason_for_hr||'具体依据见当时的追问记录。');const branch=s.selected_branch;const text=typeof branch==='string'?branch:branch?.text||ask.actual_next_round?.text||ask.question_text;if(text)note(why,'实际追问：'+text,'reader-story-question');}
    else {const event=events.find(d=>d.parent_question_id===question.question_id);note(why,event?.decision_summary?.brief_reason||event?.reason_for_hr||'尚无已封存的追问决定。');}
    if(ask||followups.length){const added=add('追答补充了什么');if(!question.model_evaluation&&followups.length)note(added,'追答已保存，合并评价尚未完成；以下只呈现回答材料。','small muted');added.append(answerStory(followups,question.model_evaluation,onEvidence));}
    const conclusion=add('当前初评与待核对');conclusion.className+=' reader-story-conclusion';note(conclusion,currentReason||(question.score_source==='hr'?question.reason:modelReason(question.reason))||'尚未形成当前评价。','report-question-reason');
    const gaps=!question.review_stale&&question.score_source!=='hr'?question.model_evaluation?.gaps||[]:[];
    for(const gap of gaps.slice(0,2))if(gap.description)note(conclusion,'待核对：'+brief(gap.description),'reader-story-gap');
    root.append(steps);return root;
  }

  window.SuitReportReader={modelReason,selectAnalysis,decision,observations,stamp,contentContext,contentError,contentRecovery,previousDraft,questionStory};
})();
