(() => {
  'use strict';
  const displayText = text => window.SuitTextDisplay?.simplify(text) ?? String(text ?? '');
  const el = (tag, text) => { const n = document.createElement(tag); if (text != null) n.textContent = tag === 'pre' ? String(text) : displayText(text); return n; };
  const names = {au:'OpenFace 面部动作',au_net:'AU-Net 原模型（离线时序）',gaze:'OpenFace 视线几何',gaze360:'Gaze360 原模型（七帧时序）',expression:'基础外显表情',emotic:'EMOTIC 场景辅助感知情绪（四类研究）',audio:'声音指标',semantic:'千问语义证据',word_frequency:'原词表词频对照'};
  const states = {queued:'排队中',running:'运行中',succeeded:'完成',partial:'部分完成',failed:'失败',complete:'完成',no_media:'没有原片',no_video:'没有视频',no_audio:'没有音轨',unavailable:'不可用'};
  const categories = {background:'背景',goal_constraint:'目标与约束',own_action:'本人行动',team_action:'团队行动',result:'结果',reflection_transfer:'反思与迁移'};
  const state = value => states[value] || value || '未记录状态';
  function json(value) { const n=el('pre',JSON.stringify(value,null,2)); n.style.whiteSpace='pre-wrap'; n.style.overflowWrap='anywhere'; return n; }
  function action(text, fn) {
    const b=el('button',text); b.type='button'; b.className='evidence-reference-link no-print';
    const status=el('span'); status.setAttribute('role','status');
    b.addEventListener('click',()=>Promise.resolve().then(fn).catch(e=>{status.textContent=e.message;}));
    const wrap=el('div'); wrap.append(b,status); return wrap;
  }
  function evidence(ref, onEvidence) {
    const block=el('div'); block.append(el('blockquote',ref.quote),el('small','回答 '+String(ref.answer_id||'').slice(0,10)+' · 转写 v'+ref.transcript_revision));
    if(typeof onEvidence==='function') block.append(action('定位转写 v'+ref.transcript_revision,()=>onEvidence(ref)));
    return block;
  }
  // Also used for the formal content evaluation; renders only its supplied snapshot.
  function semantic(evaluation, onEvidence) {
    const root=el('details'); root.append(el('summary','查看内容证据结构与评价点'));
    for(const unit of evaluation?.evidence_units||[]) {
      const block=el('div'); block.className='research-evidence-unit';
      block.append(el('strong',(categories[unit.category]||unit.category)+' · '+unit.interpretation));
      for(const ref of unit.evidence||[]) block.append(evidence(ref,onEvidence));
      root.append(block);
    }
    for(const match of evaluation?.criterion_matches||[]) root.append(el('p','评价点 '+match.criterion_id+'：'+match.reason));
    for(const gap of evaluation?.gaps||[]) root.append(el('p','待补证据：'+gap.description));
    if(!(evaluation?.evidence_units?.length||evaluation?.criterion_matches?.length||evaluation?.gaps?.length)) root.append(el('p','此结果未记录细粒度证据结构。'));
    return root;
  }
  function semanticRun(item, onEvidence) {
    const ev=item.evaluation||{}, block=el('section'); block.className='research-evidence-unit';
    block.append(el('h3','独立语义研究 · '+(ev.question_id||item.question_id||'未记录题目')),
      el('p','研究评分草稿 '+(ev.rating?.score??'N/A')+' / 5 · '+(ev.rating?.reason||'未记录理由')),
      el('p','追问建议：'+(ev.followup?.reason||'未记录')+(ev.followup?.branch_id?' · 分支 '+ev.followup.branch_id:'')),
      el('p','本次独立运行用于方法对照，未替换正式评分或面试当时的追问决定。'));
    for(const ref of ev.rating?.evidence||[]) block.append(evidence(ref,onEvidence));
    for(const ref of ev.followup?.evidence||[]) block.append(evidence(ref,onEvidence));
    block.append(semantic(ev,onEvidence));
    const source=el('details'); source.append(el('summary','模型、提示与校验来源'),json(item.provider||{})); block.append(source);
    return block;
  }
  function mount({appendix,onSeek,onEvidence,title='研究观察附录'}) {
    const root=el('section'); root.className='card report-section'; root.append(el('h2',title));
    if(!appendix) { root.append(el('p','尚未选择研究运行发布至此报告版本。可在研究工作台创建、核对后显式发布。')); return {element:root}; }
    root.append(el('p',appendix.note),el('p',appendix.input_stale?'引用素材已有更新；本附录保留历史观察，不能作为当前回答结果。':'已冻结所选研究运行；观察未用于能力评分或追问。'));
    for(const run of appendix.selected_runs||[]) {
      const block=el('details'),result=run.result;
      block.append(el('summary','运行 '+String(run.run_id||'').slice(0,10)+' · '+(run.condition||'未记录条件')+' · '+state(run.status||result?.status)),
        el('p','完成时间：'+(run.completed_at||'尚未记录')+' · 耗时：'+(result?.duration_seconds==null?'尚未记录':result.duration_seconds+' 秒')));
      if(run.error) block.append(el('p','运行错误：'+run.error));
      if(!result) block.append(el('p','此冻结运行尚无结果。'));
      for(const item of result?.semantic||[]) block.append(semanticRun(item,onEvidence));
      for(const mm of result?.multimodal||[]) {
        block.append(el('h3','回答 '+String(mm.answer_id||'').slice(0,10)+' · 转写 v'+mm.transcript_revision));
        for(const [name,module] of Object.entries(mm.modules||{})) {
          const d=el('details'); d.append(el('summary',(names[name]||name)+' · '+state(module.status)),el('p',module.reason||'独立算法观察，需结合原片与质量说明核对。'));
          d.append(json({model_id:module.model_id,summary:module.summary,provenance:module.provenance,notices:module.notices}));
          const list=el('div'); list.style.maxHeight='320px'; list.style.overflow='auto';
          let shown=0,loaded=false;const observations=module.observations||[],more=el('button','继续显示 30 个窗口');more.type='button';
          const draw=()=>{for(const item of observations.slice(shown,shown+30)){const row=el('div');row.append(el('span',(item.start_ms/1000).toFixed(2)+'–'+(item.end_ms/1000).toFixed(2)+'s · '+item.quality+' · '+JSON.stringify(item.values)+(item.reason?' · '+item.reason:'')));if(typeof onSeek==='function')row.append(action('回放此窗口',()=>onSeek(mm.answer_id,mm.transcript_revision,item.start_ms)));list.append(row);}shown+=30;more.hidden=shown>=observations.length;};
          more.addEventListener('click',draw);d.addEventListener('toggle',()=>{if(d.open&&!loaded){loaded=true;draw();}});
          if(!observations.length)list.append(el('p','没有可展示的观测窗口。'));
          d.append(list,more); block.append(d);
        }
      }
      for(const baseline of result?.word_frequency||[]) block.append(el('p','词频对照 '+baseline.question_id+'：原始次数 '+JSON.stringify(baseline.raw_counts_by_source_label)+'，上下文规则保留 '+baseline.context_retained_total+'。不换算能力分。'));
      if(result?.errors?.length) {
        const errors=el('details'); errors.open=true; errors.append(el('summary','本次失败模块 / 未完成项目'));
        for(const error of result.errors) errors.append(typeof error==='string'?el('p',error):json(error));
        block.append(errors);
      }
      root.append(block);
    }
    if(!appendix.selected_runs?.length) root.append(el('p','此版本没有已选研究运行。'));
    return {element:root};
  }
  window.SuitResearchSummary={mount,semantic};
})();
