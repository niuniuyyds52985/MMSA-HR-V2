(() => {
  'use strict';
  const displayText = text => window.SuitTextDisplay?.simplify(text) ?? String(text ?? '');
  const NS = 'http://www.w3.org/2000/svg';
  const colors = {leadership_orientation:'#725399',business_drive:'#25738a',growth:'#44744e'};
  const states = {not_measured:'本轮未测',aggregation_not_configured:'多题汇总尚未配置',stale:'依据已更新，待复核',scored:'已有内容评分',awaiting_answer:'尚未作答',processing:'内容分析中',analysis_unavailable:'自动分析暂不可用',insufficient_evidence:'证据不足'};
  const sources = {hr:'指导老师复核',model_draft:'模型草稿，待老师复核',unavailable:'暂无有效评分'};
  let sequence = 0;
  const score = value => typeof value === 'number' && Number.isFinite(value) && value >= 1 && value <= 5 ? value : null;
  const el = (tag, cls, text) => { const item = document.createElement(tag); if (cls) item.className = cls; if (text != null) item.textContent = tag === 'pre' ? String(text) : displayText(text); return item; };
  const svgEl = (tag, attributes = {}, text) => { const item = document.createElementNS(NS, tag); Object.entries(attributes).forEach(([key,value])=>item.setAttribute(key,String(value))); if (text != null) item.textContent = tag === 'pre' ? String(text) : displayText(text); return item; };
  const point = (index, radius) => { const angle = index * Math.PI / 4 - Math.PI / 2; return [280 + Math.cos(angle) * radius, 220 + Math.sin(angle) * radius]; };

  function mount({profile = {}, onEvidence} = {}) {
    const root = el('section','potential-profile');
    const titleId = 'potential-profile-' + (++sequence);
    const heading = el('div','potential-profile-heading');
    const headingText = el('h3','','潜力模型 · 八因子证据图'); headingText.id = titleId;
    heading.append(headingText,el('span','potential-profile-version','报告 v' + (profile.report_version ?? '—')));
    root.append(heading);
    const factors = Array.isArray(profile.factors) ? profile.factors : [];
    if (factors.length !== 8 || new Set(factors.map(f=>f.factor_id)).size !== 8) {
      root.append(el('p','potential-profile-note','八因子配置不完整，暂不绘制能力轮廓。'));
      return {element:root};
    }
    const scored = factors.filter(f=>f.state === 'scored' && score(f.score) !== null);
    const hasReference = !!profile.reference_profile;
    const targets = factors.filter(f=>hasReference && score(f.target) !== null);
    root.append(el('p','potential-profile-intro',`当前 ${scored.length} / 8 个因子有有效内容分。量尺为 1–5 分，锚点仍属研究草案。`));
    const layout = el('div','potential-profile-layout');
    const figure = el('figure','potential-profile-figure');
    const svg = svgEl('svg',{viewBox:'0 0 560 440',role:'group','aria-labelledby':titleId,'aria-describedby':titleId+'-description'});
    svg.append(svgEl('desc',{id:titleId+'-description'},'八个因子各有独立扇区，按一至五分的径向高度填充，每圈代表一分。未测或待完成项保持空白虚线，不作为零分。实心点与填充是回答内容分，空心圈是老师录入的参考值；各项可点击查看来源和原话。'));
    const polar = (angle,radius) => [280 + Math.cos(angle)*radius,220 + Math.sin(angle)*radius];
    const sector = (index,inner,outer) => {
      const middle=index*Math.PI/4-Math.PI/2, half=Math.PI/10, a=middle-half,b=middle+half;
      const [x1,y1]=polar(a,outer),[x2,y2]=polar(b,outer);
      if(inner===0)return 'M280,220 L'+x1+','+y1+' A'+outer+','+outer+' 0 0 1 '+x2+','+y2+' Z';
      const [x3,y3]=polar(b,inner),[x4,y4]=polar(a,inner);
      return 'M'+x1+','+y1+' A'+outer+','+outer+' 0 0 1 '+x2+','+y2+' L'+x3+','+y3+' A'+inner+','+inner+' 0 0 0 '+x4+','+y4+' Z';
    };
    const focusCard = factorId => {
      const card = cards.find(item=>item.factorId === factorId)?.element;
      if (card) { factorDetails.open=true;card.scrollIntoView({block:'nearest',behavior:'smooth'}); card.focus({preventScroll:true}); }
    };
    factors.forEach((factor,index)=>{
      const color = colors[factor.dimension_id] || '#607269';
      const validScore = factor.state === 'scored' ? score(factor.score) : null;
      const stateLabel=states[factor.state]||'暂无有效评分';
      const group=svgEl('g',{class:'potential-factor-sector'+(validScore===null?' is-missing':' is-scored')+(factor.score_source==='hr'?' is-reviewed':' is-draft'),tabindex:0,role:'button','data-factor-id':factor.factor_id,'aria-label':factor.name+'，'+(validScore===null?stateLabel:validScore+' / 5 分，'+(sources[factor.score_source]||'评分来源待核对'))+'，查看证据与状态'});
      group.append(svgEl('title',{},factor.name+' · '+(validScore===null?stateLabel:validScore+' / 5 · '+(sources[factor.score_source]||'来源待核对'))));
      for(let level=1;level<=5;level++){
        group.append(svgEl('path',{d:sector(index,(level-1)*28,level*28),class:'potential-scale-slot'+(validScore===null?' is-missing':''),'data-level':level}));
        if(validScore!==null&&validScore>level-1)group.append(svgEl('path',{d:sector(index,(level-1)*28,Math.min(validScore,level)*28),fill:color,class:'potential-score-fill','data-level':level,'data-score':validScore}));
      }
      const [lx,ly] = point(index,178), anchor = index === 0 || index === 4 ? 'middle' : index < 4 ? 'start' : 'end';
      group.append(svgEl('text',{x:lx,y:ly-7,'text-anchor':anchor,fill:color,class:'potential-factor-label'},factor.name));
      group.append(svgEl('text',{x:lx,y:ly+10,'text-anchor':anchor,class:'potential-factor-state'},validScore===null?stateLabel:validScore+' / 5'));
      if(validScore!==null)group.append(svgEl('text',{x:lx,y:ly+24,'text-anchor':anchor,class:'potential-factor-source'},factor.score_source==='hr'?'老师复核':factor.score_source==='model_draft'?'模型草稿 · 待复核':'评分来源待核对'));
      if (hasReference && score(factor.target) !== null) {
        const [tx,ty] = point(index,factor.target*28);
        const marker = svgEl('circle',{cx:tx,cy:ty,r:7,stroke:color,class:'potential-reference-point'});
        marker.append(svgEl('title',{},factor.name + ' · 老师参考值 ' + factor.target)); group.append(marker);
      }
      if (validScore !== null) {
        const [sx,sy] = point(index,validScore*28);
        const marker=svgEl('circle',{cx:sx,cy:sy,r:4,fill:color,class:'potential-score-point'});
        marker.append(svgEl('title',{},factor.name + ' ' + validScore + '分 · ' + (sources[factor.score_source] || factor.score_source || '来源待核对')));group.append(marker);
      }
      group.addEventListener('click',()=>focusCard(factor.factor_id));
      group.addEventListener('keydown',event=>{if(event.key==='Enter'||event.key===' '){event.preventDefault();focusCard(factor.factor_id);}});
      svg.append(group);
    });
    for(let level=1;level<=5;level++){
      const [x,y]=polar(-Math.PI/2+Math.PI/8,level*28);
      svg.append(svgEl('text',{x,y:y+3,'text-anchor':'middle',class:'potential-ring-label'},level));
    }
    figure.append(svg,el('figcaption','potential-profile-caption','每圈 1 分，填充高度对应当前内容分；空白虚线是未测或待完成，不是 0 分。点击任一因子查看依据。'));
    const side = el('div','potential-profile-key');
    side.append(el('h4','','三大潜力维度'));
    for (const dimension of (Array.isArray(profile.dimensions) ? profile.dimensions : [])) {
      const row = el('p','potential-dimension');
      const dot = el('span','potential-dimension-dot'); dot.style.backgroundColor = colors[dimension.dimension_id] || '#607269';
      row.append(dot,document.createTextNode(dimension.name)); side.append(row);
    }
    side.append(el('p','potential-profile-note','色块与实心点：回答证据的内容分。图中逐项标明“模型草稿”或“老师复核”。'));
    side.append(el('p','potential-profile-note',targets.length ? `○ 老师参考值：${targets.length} 个因子已设置，仅比较该参考版本。` : '尚未录入老师参考值，不显示“达标”或“符合岗位”结论。'));
    side.append(el('p','potential-profile-note','未测、待作答、内容分析中、分析暂不可用、证据不足与过期依据分别保留。面部、视线和声学观测不进入此图。'));
    layout.append(figure,side); root.append(layout);
    const cardGrid = el('div','potential-factor-cards');
    const cards = [];
    factors.forEach(factor=>{
      const card = el('article','potential-factor-card'); card.tabIndex = -1;
      card.style.setProperty('--potential-color',colors[factor.dimension_id] || '#607269');
      cards.push({factorId:factor.factor_id,element:card});
      const title = el('div','potential-factor-card-heading');
      const value = factor.state === 'scored' ? score(factor.score) : null;
      title.append(el('h4','',factor.name),el('span','potential-card-score',value === null ? '—' : value + ' / 5'));
      card.append(title,el('p','potential-card-state',(factor.dimension_name || '') + ' · ' + (states[factor.state] || '暂无有效评分')),
        el('p','potential-card-definition',factor.definition || '定义待核对。'),
        el('p','potential-card-source','评分来源：' + (sources[factor.score_source] || factor.score_source || '暂无有效评分')),
        el('p','potential-card-reason',factor.reason || '暂无具体评分依据。'));
      if (hasReference && score(factor.target) !== null) {
        card.append(el('p','potential-card-reference','老师参考值 ' + factor.target + ' / 5；' + (value === null ? '当前无有效分，暂不比较。' : value >= factor.target ? '当前内容分达到该参考值。' : '当前内容分低于该参考值。')));
      }
      const evidence = Array.isArray(factor.evidence) ? factor.evidence : [];
      if (evidence.length) {
        const list = el('div','potential-card-evidence');
        evidence.forEach((reference,index)=>{
          if (!reference || typeof reference !== 'object') return;
          const block = el('div','potential-evidence-item');
          block.append(el('blockquote','',reference.quote || '引用原文未提供'));
          if (typeof onEvidence === 'function' && reference.answer_id && reference.quote && Number.isInteger(reference.transcript_revision)) {
            const button = el('button','potential-evidence-button',`定位原话 ${index+1} · v${reference.transcript_revision}`); button.type = 'button';
            const error = el('p','potential-profile-note'); error.setAttribute('role','status');
            button.addEventListener('click',()=>{
              error.textContent = '';
              Promise.resolve().then(()=>onEvidence({...reference},factor)).catch(()=>{
                error.textContent = '原话定位暂时失败，请重新加载时间线后再试。';
              });
            }); block.append(button,error);
          } else {
            block.append(el('p','potential-profile-note','此引用缺少可定位版本信息，保留文字核对。'));
          }
          list.append(block);
        });
        card.append(list);
      }
      if (factor.theoretical_sources) {
        const details = el('details','potential-theory'); details.append(el('summary','','理论来源'),el('p','',factor.theoretical_sources)); card.append(details);
      }
      cardGrid.append(card);
    });
    const factorDetails=el('details','potential-factor-details');factorDetails.append(el('summary','','逐因子证据与理论来源（8 项）'),cardGrid);
    root.append(factorDetails,el('p','potential-profile-usage',profile.usage || '模拟测试／方法验证；回答证据轮廓。'));
    return {element:root};
  }

  const conclusionStates={demonstrated:'已体现',partial:'部分体现',not_met:'有相反证据，待复核',insufficient_evidence:'证据不足',not_measured:'本轮未测'};
  function mountConclusion({conclusion,onEvidence,compact=false}={}){
    const root=el('section','potential-conclusion'+(compact?' is-compact':''));
    if(!conclusion||!Array.isArray(conclusion.factors)){root.append(el('p','potential-conclusion-empty','本报告尚未保存逐评价点的潜力结论。请先核对各题回答与评价依据；八因子图不代替文字结论。'));return {element:root};}
    const entries=conclusion.factors,validReference=ref=>ref&&typeof ref.quote==='string'&&ref.quote.trim()&&ref.answer_id&&Number.isInteger(ref.transcript_revision);
    const supportedStatus=(status,evidence)=>Object.hasOwn(conclusionStates,status)?status==='not_met'&&!(evidence||[]).some(validReference)?'insufficient_evidence':status:'insufficient_evidence';
    const allCriteria=entries.flatMap(f=>f.criteria||[]),counts=Object.fromEntries(Object.keys(conclusionStates).map(state=>[state,0]));
    for(const factor of entries){const evidence=(factor.criteria||[]).flatMap(c=>c.evidence||[]);counts[supportedStatus(factor.status,evidence)]++;}
    if(typeof conclusion.summary==='string'&&conclusion.summary.trim())root.append(el('p','potential-conclusion-summary',conclusion.summary));
    const coverage=conclusion.coverage||{},measured=coverage.measured_factor_count??entries.filter(f=>f.status!=='not_measured').length,total=coverage.total_factor_count??entries.length;
    root.append(el('p','potential-conclusion-scope','本场有回答证据的因子 '+measured+' / '+total+'；按本次回答和所用评价标准形成结论。'+(measured<total?'本轮为局部覆盖，未测因子不推分。':'')));
    const chips=el('div','potential-conclusion-counts');for(const [state,label]of Object.entries(conclusionStates)){const chip=el('span','potential-conclusion-count status-'+state,label+' '+counts[state]);chip.setAttribute('data-conclusion-status',state);chips.append(chip);}root.append(chips);
    root.append(el('p','potential-conclusion-boundary','结论来自回答文本；声音、表情和视线不加减分。没有老师岗位参考标准时，不生成岗位匹配率或录用结论。'));
    const reviewNote=conclusion.teacher_review_note||entries.map(f=>f.notice).find(Boolean);if(reviewNote)root.append(el('p','potential-conclusion-review-note',reviewNote));
    if(compact)return {element:root};
    root.append(el('p','potential-conclusion-scope','展开一个因子，查看原表要求、满足情况及对应原话。'));
    const list=el('div','potential-conclusion-factors');
    for(const factor of entries){
      const criteria=Array.isArray(factor.criteria)?factor.criteria:[],factorEvidence=criteria.flatMap(c=>c.evidence||[]),state=supportedStatus(factor.status,factorEvidence);
      const section=el('details','potential-conclusion-factor');section.open=false;section.setAttribute('data-factor-id',factor.factor_id||'');
      const heading=el('summary','potential-conclusion-factor-heading');heading.append(el('strong','',factor.name||factor.factor_id||'评价因子'),el('span','potential-conclusion-dimension',typeof factor.dimension==='object'?factor.dimension.name||'':factor.dimension||factor.dimension_name||''),el('span','potential-conclusion-status status-'+state,conclusionStates[state]));section.append(heading);
      if(factor.definition)section.append(el('p','potential-conclusion-factor-summary',factor.definition));
      if(factor.summary)section.append(el('p','potential-conclusion-factor-summary',factor.summary));
      if(factor.review_state==='stale')section.append(el('p','potential-conclusion-gap','回答版本已有更新，请按本报告引用重新核对结论。'));
      if(!criteria.length)section.append(el('p','potential-conclusion-empty',state==='not_measured'?'本轮未安排该因子，不作满足情况判断。':'本报告未提供该因子的逐评价点依据，请核对本题材料。'));
      for(const criterion of criteria){
        const evidence=Array.isArray(criterion.evidence)?criterion.evidence:[],criterionState=supportedStatus(criterion.status,evidence),row=el('article','potential-conclusion-criterion');row.setAttribute('data-criterion-id',criterion.id||'');
        const title=el('div','potential-conclusion-criterion-heading');title.append(el('h4','',criterion.text||criterion.id||'评价点'),el('span','potential-conclusion-status status-'+criterionState,conclusionStates[criterionState]));row.append(title);
        if(criterion.description&&criterion.description!==criterion.text)row.append(el('p','potential-conclusion-criterion-description',criterion.description));
        if(criterion.reason)row.append(el('p','potential-conclusion-reason',criterion.reason));
        if(criterion.status==='not_met'&&criterionState!=='not_met')row.append(el('p','potential-conclusion-empty','该判断未附可核对的相反原话，当前保留为证据不足。'));
        for(const ref of evidence){if(!ref?.quote)continue;const quote=el('blockquote','potential-conclusion-evidence');quote.append(el('p','','“'+ref.quote+'”'));const sourceLabel=ref.source_label||(ref.answer_kind==='followup'?'追答原话':ref.answer_kind==='main'?'主答原话':'本场原话');quote.append(el('small','',sourceLabel+' · 转写 v'+(ref.transcript_revision??'未记录')));
          if(validReference(ref)&&typeof onEvidence==='function'){const button=el('button','potential-evidence-button no-print','定位这句原话与原片');button.type='button';const message=el('p','potential-conclusion-link-status');message.setAttribute('role','status');button.addEventListener('click',()=>Promise.resolve().then(()=>onEvidence({...ref},factor,criterion)).catch(()=>{message.textContent='暂时无法定位此引用，请核对对应回答与转写版本。';}));quote.append(button,message);}else quote.append(el('small','potential-conclusion-empty','此条未提供完整引用版本，仅供文字核对。'));row.append(quote);}
        if(!evidence.some(ref=>ref?.quote)&&criterionState!=='not_measured')row.append(el('p','potential-conclusion-empty','尚无可定位文本引用，不能将缺少材料直接视为不满足。'));
        for(const gap of criterion.gaps||[]) {const text=typeof gap==='string'?gap:gap?.description||gap?.text;if(text)row.append(el('p','potential-conclusion-gap','待补证据：'+text));}
        if(criterion.source_refs?.length){const provenance=el('details','potential-conclusion-criterion-source');provenance.append(el('summary','','评价点原始出处'),el('pre','',JSON.stringify({criterion_id:criterion.id,source_refs:criterion.source_refs,criterion_ids:criterion.criterion_ids},null,2)));row.append(provenance);}section.append(row);
      }
      list.append(section);
    }
    root.append(list);
    const versions=el('details','potential-conclusion-provenance');versions.append(el('summary','','评价标准与结论来源版本'),el('pre','',JSON.stringify({schema_version:conclusion.schema_version,source_version:conclusion.source_version,coverage:conclusion.coverage},null,2)));root.append(versions);
    if(!allCriteria.length)root.append(el('p','potential-conclusion-empty','当前没有逐评价点记录，图表分数不会自动补写为结论。'));
    return {element:root};
  }

  window.SuitPotentialProfile = {mount,mountConclusion};
})();
