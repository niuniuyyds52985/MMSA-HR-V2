(() => {
  'use strict';
  const displayText = text => window.SuitTextDisplay?.simplify(String(text ?? '')) ?? String(text ?? '');
  const numeric = value => typeof value === 'number' && Number.isFinite(value);
  const validRange = item => numeric(item?.start_ms) && numeric(item?.end_ms) && item.start_ms >= 0 && item.end_ms > item.start_ms;
  const aligned = item => item?.alignment_status === 'aligned' && validRange(item);
  const element = (tag, cls, text) => { const node = document.createElement(tag); if (cls) node.className = cls; if (text != null) node.textContent = String(text); return node; };
  const button = (label, cls = '') => { const node = element('button', cls, label); node.type = 'button'; return node; };
  const timeText = ms => numeric(ms) ? String(Math.floor(ms / 60000)).padStart(2,'0') + ':' + ((ms % 60000) / 1000).toFixed(1).padStart(4,'0') : '无时间定位';
  const sourceNames = {model_rating:'模型评分草稿',model_followup:'模型追问建议',hr_rating:'老师评分引用',followup_assessment:'追问内容判定',decision:'封存追问决策'};
  const players = new Set();
  let playbackIntent = 0;

  // Offsets in the API are Unicode code points, never JavaScript UTF-16 indexes.
  function locateQuote(text, quote, segments) {
    const characters = Array.from(text || ''), needle = Array.from(quote || ''), occurrences = [];
    if (!needle.length) return occurrences;
    for (let start = 0; start <= characters.length - needle.length; start++) {
      if (!needle.every((value,index) => value === characters[start + index])) continue;
      const end = start + needle.length;
      const matching = segments.filter(segment => segment.char_start < end && segment.char_end > start);
      let covered = start;
      for (const segment of [...matching].sort((a,b) => a.char_start - b.char_start)) {
        if (segment.char_start > covered) break;
        covered = Math.max(covered,segment.char_end);
      }
      const meaningful = matching.filter(segment => String(segment.text || '').trim());
      const timed = meaningful.length > 0 && covered >= end && meaningful.every(aligned);
      occurrences.push({char_start:start,char_end:end,segment_ids:matching.map(segment => segment.segment_id),start_ms:timed ? Math.min(...meaningful.map(segment => segment.start_ms)) : null,end_ms:timed ? Math.max(...meaningful.map(segment => segment.end_ms)) : null,alignment_status:timed ? 'aligned' : 'unaligned'});
    }
    return occurrences;
  }

  function mount({answer = {}, sessionId, revision = answer.transcript_revision, fetchJSON, fetchMedia, onOpenRevision, showReferences = true}) {
    const root = element('section','evidence-timeline');
    const heading = element('div','evidence-timeline-heading');
    heading.append(element('h3','','原话与同步回放 · 转写 v' + revision),element('span','evidence-timeline-tag','模拟测试／方法验证'));
    const versionInfo = element('p','small muted','按指定转写版本读取；句段定位为近似范围，不提供词级时间。');
    const actions = element('div','evidence-timeline-actions no-print');
    const loadButton = button('载入原话时间线'), mediaButton = button('载入原始音视频');
    mediaButton.hidden = !answer.has_media;
    const loopLabel = element('label','evidence-loop'), loop = element('input'); loop.type = 'checkbox'; loop.disabled = true;
    loopLabel.append(loop,element('span','','循环所选片段')); actions.append(loadButton,mediaButton,loopLabel);
    const status = element('p','evidence-timeline-status'); status.setAttribute('role','status'); status.setAttribute('aria-live','polite');
    const playerHolder = element('div','evidence-timeline-player no-print');
    const playerStage = element('div','evidence-player-stage'), overlayHolder = element('div','evidence-player-overlay');
    playerHolder.append(playerStage,overlayHolder);
    const transcript = element('div','evidence-segments');
    const choices = element('div','evidence-match-choices');
    const observation = element('div','evidence-observations');
    const references = element('details','evidence-references'); references.hidden = true;
    root.append(heading,versionInfo,actions,status,playerHolder,choices,transcript,observation,references);
    let disposed = false, payload = null, timelineLoading = null, player = null, playerLoading = null, playerURL = null, cancelMetadata = null;
    let selectionSerial = 0, intentSerial = 0, selectedRange = null, segmentNodes = [], observationNodes = [], cursor = null, currentObservation = null, graphDuration = 0;
    let followedSegment = null;
    const timeListeners = new Set();
    function claimSelection() { intentSerial = ++playbackIntent; for (const other of players) if (other !== player) other.pause(); return ++selectionSerial; }
    const alive = () => !disposed && root.isConnected;
    function message(text, error = false) { if (disposed) return; status.textContent = text; status.className = 'evidence-timeline-status' + (error ? ' error' : ''); }
    function markSelection(occurrence) {
      for (const entry of segmentNodes) {
        entry.node.classList.toggle('is-selected', Boolean(occurrence?.segment_ids?.includes(entry.segment.segment_id)));
        entry.text.replaceChildren();
        const segment = entry.segment, chars = Array.from(displayText(segment.text || ''));
        if (occurrence && occurrence.char_start < segment.char_end && occurrence.char_end > segment.char_start) {
          const start = Math.max(0,occurrence.char_start - segment.char_start), end = Math.min(chars.length,occurrence.char_end - segment.char_start);
          entry.text.append(document.createTextNode(chars.slice(0,start).join('')),element('mark','',chars.slice(start,end).join('')),document.createTextNode(chars.slice(end).join('')));
        } else entry.text.textContent = displayText(segment.text || '');
      }
    }
    function syncTime(milliseconds) {
      if (disposed || !numeric(milliseconds)) return;
      let activeNode=null;
      for (const entry of segmentNodes) {
        const active = aligned(entry.segment) && milliseconds >= entry.segment.start_ms && milliseconds < entry.segment.end_ms;
        entry.node.classList.toggle('is-active',active); entry.node.setAttribute('aria-current',active ? 'true' : 'false');
        if(active&&!activeNode)activeNode=entry.node;
      }
      if(activeNode&&activeNode!==followedSegment)keepSegmentVisible(activeNode);
      followedSegment=activeNode;
      const windows = [];
      for (const entry of observationNodes) {
        const active = milliseconds >= entry.window.start_ms && milliseconds < entry.window.end_ms;
        entry.node.classList.toggle('is-active',active); if (active) windows.push(entry.window);
      }
      if (cursor) { const x = 52 + Math.min(graphDuration,Math.max(0,milliseconds)) / Math.max(1,graphDuration) * 530; cursor.setAttribute('x1',x); cursor.setAttribute('x2',x); }
      if (currentObservation) currentObservation.textContent = timeText(milliseconds) + ' · ' + (windows.length ? windows.map(item => 'RMS ' + item.rms_dbfs.toFixed(2) + ' dBFS').join('；') : '此时无 RMS 窗口观测');
      for (const listener of timeListeners) listener(milliseconds,player);
    }
    function keepSegmentVisible(node) {
      if(typeof transcript.getBoundingClientRect!=='function'||typeof node.getBoundingClientRect!=='function'||!(transcript.clientHeight>0))return;
      const viewport=transcript.getBoundingClientRect(),rect=node.getBoundingClientRect(),current=transcript.scrollTop||0;
      let next=current;
      if(rect.top<viewport.top+8)next=current+rect.top-viewport.top-8;
      else if(rect.bottom>viewport.top+transcript.clientHeight-8)next=current+rect.bottom-viewport.top-transcript.clientHeight+8;
      if(next!==current)transcript.scrollTop=Math.max(0,Math.min(Math.max(0,transcript.scrollHeight-transcript.clientHeight),next));
    }
    function onTime() {
      if (!player || disposed) return;
      const ms = player.currentTime * 1000;
      if (loop.checked && aligned(selectedRange) && !player.paused && ms >= selectedRange.end_ms) {
        player.currentTime = selectedRange.start_ms / 1000; syncTime(selectedRange.start_ms); return;
      }
      syncTime(ms);
    }
    function releasePlayer() {
      if (cancelMetadata) cancelMetadata();
      cancelMetadata = null;
      if (player) { players.delete(player); player.pause(); player.removeAttribute('src'); player.load(); player.remove(); player = null; }
      if (playerURL) URL.revokeObjectURL(playerURL);
      playerURL = null;
    }
    async function ensurePlayer() {
      if (playerLoading) return playerLoading;
      if (player) return player;
      playerLoading = (async () => {
        const blob = await fetchMedia();
        if (!alive()) return null;
        if (!blob || !blob.size) throw new Error('原始音视频为空，无法回放。');
        playerURL = URL.createObjectURL(blob);
        player = element(String(answer.media_type || blob.type || '').startsWith('audio/') ? 'audio' : 'video');
        player.controls = true; player.preload = 'metadata'; if (player.tagName === 'VIDEO') player.playsInline = true;
        player.setAttribute('aria-label','本条回答原始音视频 · 转写 v' + revision);
        players.add(player);
        player.addEventListener('pointerdown',claimSelection);
        player.addEventListener('keydown',claimSelection);
        player.addEventListener('play',() => { intentSerial = ++playbackIntent; for (const other of players) if (other !== player) other.pause(); });
        player.addEventListener('timeupdate',onTime); player.addEventListener('seeked',onTime);
        player.addEventListener('seeking',() => {
          const ms = player.currentTime * 1000;
          if (loop.checked && aligned(selectedRange) && (ms < selectedRange.start_ms || ms >= selectedRange.end_ms)) loop.checked = false;
          syncTime(ms);
        });
        player.addEventListener('ended',() => { if (loop.checked && aligned(selectedRange)) { player.currentTime = selectedRange.start_ms / 1000; player.play().catch(() => {}); } });
        playerStage.replaceChildren(player);
        await new Promise((resolve,reject) => {
          const target = player;
          let timer;
          const finish = error => {
            clearTimeout(timer); target.removeEventListener('loadedmetadata',loaded); target.removeEventListener('error',failed); cancelMetadata = null;
            error ? reject(error) : resolve();
          };
          const loaded = () => finish(), failed = () => finish(new Error('原始音视频无法解码，请重试或检查媒体格式。'));
          target.addEventListener('loadedmetadata',loaded,{once:true}); target.addEventListener('error',failed,{once:true});
          cancelMetadata = () => finish(); timer = setTimeout(() => finish(new Error('读取媒体信息超时，可重试。')),20000);
          target.src = playerURL;
        });
        if (!alive()) return null;
        mediaButton.textContent = '原始音视频已载入'; syncTime(player.currentTime * 1000); return player;
      })().catch(error => { releasePlayer(); throw error; }).finally(() => { playerLoading = null; });
      return playerLoading;
    }
    async function seekMedia(startMs, request, {play = true} = {}) {
      const target = await ensurePlayer();
      if (!alive() || !target || request !== selectionSerial || intentSerial !== playbackIntent) return;
      if (!play) target.pause();
      target.currentTime = Math.max(0,startMs / 1000); syncTime(target.currentTime * 1000);
      if (play) await target.play().catch(() => { message('已定位原片，请点击播放器播放。'); });
    }
    async function chooseOccurrence(occurrence, request = claimSelection()) {
      if (!alive() || request !== selectionSerial || intentSerial !== playbackIntent) return;
      selectedRange = occurrence; loop.disabled = !aligned(occurrence) || !payload?.media?.available;
      if (loop.disabled) loop.checked = false;
      markSelection(occurrence);
      const target = segmentNodes.find(entry => occurrence.segment_ids?.includes(entry.segment.segment_id));
      target?.node.scrollIntoView({behavior:'smooth',block:'nearest'});
      if (!payload?.media?.available || !aligned(occurrence)) {
        message('已定位原话；' + (!payload?.media?.available ? '此回答没有可用媒体。' : '该版本没有可靠时间对齐，不能定位到原片。')); return;
      }
      message('句段定位／近似：' + timeText(occurrence.start_ms) + '–' + timeText(occurrence.end_ms) + '。可开启片段循环。');
      await seekMedia(occurrence.start_ms,request);
    }
    function handle(promise) { promise.catch(error => { if (alive()) message(error.message || '读取失败，请重试。',true); }); }
    function renderSegments(data) {
      transcript.replaceChildren(); segmentNodes = [];followedSegment=null;
      for (const segment of data.segments || []) {
        if (!String(segment.text || '').trim()) continue;
        const row = button('','evidence-segment'), stamp = element('span','evidence-segment-time',aligned(segment) ? '≈ ' + timeText(segment.start_ms) + '–' + timeText(segment.end_ms) : '未对齐');
        const text = element('span','evidence-segment-text',displayText(segment.text || ''));
        row.append(stamp,text); row.setAttribute('aria-label',(aligned(segment) ? '定位句段：' : '查看未对齐原话：') + displayText(segment.text || ''));
        row.addEventListener('click',() => { choices.replaceChildren(); handle(chooseOccurrence({...segment,segment_ids:[segment.segment_id]})); });
        transcript.append(row); segmentNodes.push({node:row,text,segment});
      }
      if (!segmentNodes.length) transcript.append(element('p','muted',displayText(data.text || '此版本没有可用转写。原始媒体仍可单独回放。')));
    }
    function renderObservations(data) {
      observation.replaceChildren(); observationNodes = []; cursor = null; currentObservation = null;
      const audio = data.observations?.audio, windows = (audio?.windows || []).filter(item => validRange(item) && numeric(item.rms_dbfs));
      observation.append(element('h4','','声音观测 · RMS / dBFS'),element('p','small muted','仅观测录音能量，不代表情绪、能力或评分；点击窗口可回到原片与对应句段。'));
      if (audio?.status !== 'available' || !windows.length) { observation.append(element('p','evidence-empty',audio?.reason || '没有可用的 RMS 能量窗口；缺失不记为 0。')); return; }
      const svgNode = (tag,attributes = {}) => { const node = document.createElementNS('http://www.w3.org/2000/svg',tag); for (const [key,value] of Object.entries(attributes)) node.setAttribute(key,String(value)); return node; };
      const svg = svgNode('svg',{viewBox:'0 0 600 180',role:'group','aria-label':'RMS 录音能量观测，dBFS；数据点可定位回放'});
      const minimum = Math.floor(Math.min(...windows.map(item => item.rms_dbfs)) / 10) * 10, maximum = Math.max(minimum + 10,Math.ceil(Math.max(...windows.map(item => item.rms_dbfs)) / 10) * 10);
      graphDuration = Math.max(...windows.map(item => item.end_ms));
      const x = ms => 52 + ms / Math.max(1,graphDuration) * 530, y = db => 135 - (db - minimum) / (maximum - minimum) * 110;
      for (const value of [minimum,(minimum + maximum) / 2,maximum]) {
        svg.append(svgNode('line',{x1:52,x2:582,y1:y(value),y2:y(value),stroke:'#dce5d4'}));
        const label = svgNode('text',{x:44,y:y(value) + 4,'text-anchor':'end','font-size':11}); label.textContent = value.toFixed(0); svg.append(label);
      }
      svg.append(svgNode('polyline',{points:windows.map(item => x(item.start_ms) + ',' + y(item.rms_dbfs)).join(' '),fill:'none',stroke:'#3b8060','stroke-width':2}));
      cursor = svgNode('line',{x1:52,x2:52,y1:18,y2:138,stroke:'#a76328','stroke-dasharray':'4 3'}); svg.append(cursor);
      for (const item of windows) {
        const label = timeText(item.start_ms) + '–' + timeText(item.end_ms) + ' · ' + item.rms_dbfs.toFixed(2) + ' dBFS';
        const point = svgNode('circle',{cx:x(item.start_ms),cy:y(item.rms_dbfs),r:5,class:'evidence-energy-point',tabindex:0,role:'button','aria-label':label});
        const title = svgNode('title'); title.textContent = label; point.append(title);
        point.addEventListener('click',() => handle(seek(item.start_ms)));
        point.addEventListener('keydown',event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); handle(seek(item.start_ms)); } });
        observationNodes.push({node:point,window:item}); svg.append(point);
      }
      for (const [ms,anchor] of [[0,'start'],[graphDuration,'end']]) { const label = svgNode('text',{x:x(ms),y:162,'text-anchor':anchor,'font-size':11}); label.textContent = timeText(ms); svg.append(label); }
      currentObservation = element('p','small muted','播放或拖动时，当前句段与 RMS 窗口同步高亮。'); observation.append(svg,currentObservation);
    }
    function renderReferences(data) {
      references.replaceChildren(); references.hidden = !showReferences || !(data.references || []).length;
      if (!showReferences) return;
      references.append(element('summary','','本转写版本的引用记录（按来源区分）'),element('p','small muted','这些记录可能来自不同时刻；仅标为“封存追问决策”的引用代表当时用于决策的内容。'));
      for (const reference of data.references || []) {
        const item = element('div','evidence-reference');
        item.append(element('strong','small',sourceNames[reference.source] || '内容引用'),element('p','',displayText(reference.quote || '')),element('p','small muted',displayText(reference.reason || '')));
        const locate = button('定位引用原话与片段'); locate.addEventListener('click',() => handle(openQuote(reference)));
        item.append(locate); references.append(item);
      }
    }
    async function load() {
      if (payload) return payload;
      if (timelineLoading) return timelineLoading;
      loadButton.disabled = true; message('正在读取转写 v' + revision + ' 的原话时间线…');
      timelineLoading = (async () => {
        if (!Number.isInteger(revision) || revision < 1) throw new Error('该引用缺少明确转写版本，不能沿用当前版本定位。');
        const data = await fetchJSON(revision);
        if (!alive()) return null;
        if (data.schema_version !== 'interview-timeline.v1' || data.answer_id !== answer.answer_id || data.transcript_revision !== revision || (sessionId && data.session_id !== sessionId)) throw new Error('时间线回答或版本不一致，已拒绝关联。');
        payload = data;
        const alignment = data.alignment || {};
        versionInfo.textContent = '转写 v' + revision + (data.is_current_revision ? ' · 当前转写' : ' · 历史转写（当前 v' + data.current_transcript_revision + '）') + (data.answer_current === false ? ' · 此回答已被重录替换' : '') + ' · ' + (({aligned:'句段已对齐／近似',partial:'部分句段未对齐',unaligned:'文字尚未对齐',stale:'旧时间关系不可用于本版',no_media:'纯文字／无媒体'})[alignment.status] || '对齐状态未知') + (alignment.reason ? ' · ' + String(alignment.reason).replace(/[。.!！\s]+$/u,'') : '') + '。时间精度：句段，非词级。';
        mediaButton.hidden = !data.media?.available;
        for (const sourceRevision of new Set(alignment.available_asr_revisions || [alignment.asr_transcript_revision])) {
          if (!Number.isInteger(sourceRevision) || sourceRevision === revision || !onOpenRevision) continue;
          const original = button('查看原始 ASR v' + sourceRevision + '（历史）');
          original.addEventListener('click',() => onOpenRevision(sourceRevision)); actions.append(original);
        }
        renderSegments(data); renderObservations(data); renderReferences(data);
        loadButton.hidden = true; message('已载入指定版本。选择原话或引用以核对对应片段。');
        if (player) syncTime(player.currentTime * 1000);
        return data;
      })().catch(error => { if (!disposed) { loadButton.disabled = false; message(error.message,true); } throw error; }).finally(() => { timelineLoading = null; });
      return timelineLoading;
    }
    async function seek(milliseconds, {play = true} = {}) {
      const request = claimSelection();
      if (!numeric(milliseconds) || milliseconds < 0) throw new Error('该观测没有有效的媒体时间。');
      if (!play) player?.pause();
      await load(); if (!alive() || request !== selectionSerial || intentSerial !== playbackIntent) return;
      loop.checked = false; choices.replaceChildren();
      if (!payload.media?.available) { message('该回答没有可用媒体，仍可核对原话。'); return; }
      const segment = payload.segments.find(item => aligned(item) && milliseconds >= item.start_ms && milliseconds < item.end_ms);
      selectedRange = segment ? {...segment,segment_ids:[segment.segment_id]} : null; loop.disabled = !selectedRange;
      markSelection(selectedRange); if (play) syncTime(milliseconds);
      message((play ? '已选择原片 ' : '已暂停定位原片 ') + timeText(milliseconds) + (segment ? '，同步显示对应句段。' : '；此时没有可靠对齐的原话句段。'));
      await seekMedia(milliseconds,request,{play});
      if (alive() && request === selectionSerial && intentSerial === playbackIntent) playerHolder.scrollIntoView({behavior:'smooth',block:'nearest'});
    }
    async function openQuote(evidence) {
      const request = claimSelection();
      if (evidence.answer_id && evidence.answer_id !== answer.answer_id) throw new Error('引用不属于此回答，不能跨题定位。');
      if (evidence.transcript_revision !== revision) throw new Error('引用转写版本不一致，不能用新版替代历史原话。');
      const data = await load(); if (!alive() || !data || request !== selectionSerial || intentSerial !== playbackIntent) return;
      root.scrollIntoView({behavior:'smooth',block:'nearest'}); choices.replaceChildren();
      let occurrences = locateQuote(data.text,evidence.quote,data.segments || []);
      if (evidence.char_start != null || evidence.char_end != null || evidence.segment_id != null) {
        const segment = (data.segments || []).find(s => s.segment_id === evidence.segment_id);
        if (!Number.isInteger(evidence.char_start) || !Number.isInteger(evidence.char_end) ||
            (evidence.segment_id != null && (!segment || segment.char_start > evidence.char_start || segment.char_end < evidence.char_end))) {
          throw new Error('引用的句段与文字位置不一致，未退回第一处匹配。');
        }
        occurrences = occurrences.filter(o => o.char_start === evidence.char_start && o.char_end === evidence.char_end);
      }
      if (!occurrences.length) { loop.checked = false; loop.disabled = true; selectedRange = null; markSelection(null); message('此引用未在转写 v' + revision + ' 中精确匹配，无法定位。',true); return; }
      if (occurrences.length === 1) { await chooseOccurrence(occurrences[0],request); return; }
      loop.checked = false; loop.disabled = true; selectedRange = null; markSelection(null);
      message('这段原话出现 ' + occurrences.length + ' 次。请根据上下文选择位置，不自动选择第一处。');
      const chars = Array.from(displayText(data.text || ''));
      occurrences.forEach((occurrence,index) => {
        const context = chars.slice(Math.max(0,occurrence.char_start - 12),Math.min(chars.length,occurrence.char_end + 12)).join('');
        const choice = button('第 ' + (index + 1) + ' 处 · ' + (aligned(occurrence) ? '≈ ' + timeText(occurrence.start_ms) : '未对齐') + ' · …' + context + '…');
        choice.addEventListener('click',() => { choices.replaceChildren(); handle(chooseOccurrence(occurrence)); }); choices.append(choice);
      });
    }
    loadButton.addEventListener('click',() => handle(load()));
    mediaButton.addEventListener('click',() => {
      const request = claimSelection();
      handle((async () => { await load(); if (!alive() || request !== selectionSerial || intentSerial !== playbackIntent || !payload?.media?.available) return; await ensurePlayer(); })());
    });
    loop.addEventListener('change',() => { if (loop.checked && aligned(selectedRange)) handle(seekMedia(selectedRange.start_ms,claimSelection())); });
    loop.addEventListener('input',event => event.stopPropagation());
    function pause() { selectionSerial++; player?.pause(); }
    function subscribeTime(listener) { timeListeners.add(listener); listener(player ? player.currentTime * 1000 : 0,player); return () => timeListeners.delete(listener); }
    function attachOverlay(node) { overlayHolder.replaceChildren(node); return () => { if (overlayHolder.contains(node)) node.remove(); }; }
    async function loadMedia() { await load(); if (alive() && payload?.media?.available) await ensurePlayer(); }
    function dispose() { disposed = true; selectionSerial++; timeListeners.clear(); overlayHolder.replaceChildren(); releasePlayer(); payload = null; segmentNodes = []; observationNodes = []; }
    return {element:root,load,loadMedia,seek,openQuote,pause,isPlaying:()=>Boolean(player&&!player.paused),subscribeTime,attachOverlay,refreshTime:()=>syncTime(player?player.currentTime*1000:0),dispose};
  }
  window.SuitEvidenceTimeline = {mount,locateQuote};
})();
