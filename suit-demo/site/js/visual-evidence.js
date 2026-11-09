(() => {
  'use strict';
  const element = (tag,className,text) => { const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node; };
  const numeric = value => typeof value === 'number' && Number.isFinite(value);
  const valueText = (value,digits = 1) => numeric(value) ? value.toFixed(digits) : 'N/A';
  function timeText(milliseconds) { const seconds = Math.max(0,milliseconds) / 1000; return String(Math.floor(seconds / 60)).padStart(2,'0') + ':' + (seconds % 60).toFixed(1).padStart(4,'0'); }
  const approximateTime = frame => Boolean(frame.timestamp_source && frame.timestamp_source !== 'decoder');
  const frameTime = frame => (approximateTime(frame) ? '≈ ' : '') + timeText(frame.timestamp_ms);
  function mount({answer = {},fetchJSON,fetchFrame,onSeek}) {
    const root = element('section','visual-evidence'), header = element('div','visual-evidence-heading');
    header.append(element('h3','','面部画面与采集质量'),element('span','visual-evidence-tag','本地原片'));
    const intro = element('p','visual-evidence-intro','查看按时间抽取的原始画面、检出人脸的位置，以及整张缩略帧的亮度和清晰度原始指标。点击画面可定位本条回答的原始视频。');
    const loadButton = element('button','visual-evidence-load','查看面部画面与采集质量'); loadButton.type = 'button';
    const status = element('p','visual-evidence-status'); status.setAttribute('role','status'); status.setAttribute('aria-live','polite');
    const content = element('div','visual-evidence-content'); content.hidden = true;
    root.append(header,intro,loadButton,status,content);
    const urls = new Map();
    let disposed = false, busy = false, loaded = false, payload = null, frames = [], shown = 0, grid = null, more = null, count = null;
    const audioOnly = String(answer.media_type || '').startsWith('audio/');
    function message(text,error = false) { if (disposed) return; status.textContent = text; status.className = 'visual-evidence-status' + (error ? ' error' : ''); }
    function releaseURLs() { for (const url of urls.values()) URL.revokeObjectURL(url); urls.clear(); }
    function metric(holder,label,value,detail) { const item = element('div'), term = element('dt','',label), description = element('dd','',value); if (detail) description.append(element('small','',detail)); item.append(term,description); holder.append(item); }
    function frameFacts(frame) {
      const facts = element('p','visual-frame-facts');
      facts.append(element('span','',Number.isInteger(frame.face_count) && frame.face_count >= 0 ? frame.face_count === 0 ? '人脸检测：本帧未检出' : '人脸检测：' + frame.face_count + ' 个框' : '人脸检测：不可用'));
      facts.append(element('span','','整帧亮度：' + valueText(frame.brightness_mean) + ' 灰度值（0–255）'));
      facts.append(element('span','','清晰度：' + valueText(frame.laplacian_variance) + ' 灰度²（拉普拉斯方差）'));
      return facts;
    }
    function appendFaceBoxes(holder,frame) {
      if (!numeric(frame.width) || !numeric(frame.height) || frame.width <= 0 || frame.height <= 0 || frame.face_count == null) return;
      for (const box of frame.face_boxes || []) {
        if (![box.x,box.y,box.width,box.height].every(numeric) || box.width <= 0 || box.height <= 0) continue;
        const left = Math.max(0,Math.min(frame.width,box.x)), top = Math.max(0,Math.min(frame.height,box.y));
        const width = Math.max(0,Math.min(frame.width - left,box.width)), height = Math.max(0,Math.min(frame.height - top,box.height));
        if (!width || !height) continue;
        const rectangle = element('span','visual-face-box'); rectangle.setAttribute('aria-hidden','true');
        rectangle.style.left = left / frame.width * 100 + '%'; rectangle.style.top = top / frame.height * 100 + '%'; rectangle.style.width = width / frame.width * 100 + '%'; rectangle.style.height = height / frame.height * 100 + '%'; holder.append(rectangle);
      }
    }
    async function fillFrame(frame,holder,placeholder,retry) {
      if (disposed || retry.disabled) return; retry.disabled = true; retry.hidden = true; placeholder.textContent = '正在读取本地画面…';
      try {
        const blob = await fetchFrame(frame.index);
        if (disposed) return;
        if (!blob || typeof blob.size !== 'number' || !blob.size || !/^image\/(jpeg|png|webp|gif)$/i.test(blob.type || '')) throw new Error('未取得有效的本地缩略图。');
        const previous = urls.get(frame.index); if (previous) URL.revokeObjectURL(previous);
        const url = URL.createObjectURL(blob); urls.set(frame.index,url);
        const image = element('img'); image.alt = '原始视频 ' + frameTime(frame) + ' 抽样画面'; image.decoding = 'async';
        image.addEventListener('error',() => { if (disposed) return; image.remove(); holder.querySelectorAll('.visual-face-box').forEach(box => box.remove()); placeholder.hidden = false; placeholder.textContent = '本帧图片无法显示'; retry.disabled = false; retry.hidden = false; });
        image.src = url; holder.append(image); appendFaceBoxes(holder,frame); placeholder.hidden = true;
      } catch (error) { if (!disposed) { placeholder.hidden = false; placeholder.textContent = error.message || '本帧加载失败'; retry.hidden = false; } }
      finally { if (!disposed) retry.disabled = false; }
    }
    function makeFrame(frame) {
      const card = element('article','visual-frame'), button = element('button','visual-frame-button'); button.type = 'button'; button.setAttribute('aria-label','定位原始视频 ' + frameTime(frame));
      const imageHolder = element('span','visual-frame-image'); if (numeric(frame.width) && numeric(frame.height) && frame.width > 0 && frame.height > 0) imageHolder.style.aspectRatio = String(frame.width) + ' / ' + String(frame.height);
      const placeholder = element('span','visual-frame-placeholder','正在读取本地画面…'); imageHolder.append(placeholder);
      const caption = element('span','visual-frame-caption'); caption.append(element('strong','','▶ ' + frameTime(frame)),element('span','',approximateTime(frame) ? '约定位原片' : '定位原片'));
      if (approximateTime(frame)) caption.title = frame.timestamp_source === 'frame_rate_approximate' ? '时间按视频帧率估算，为近似定位时间。' : frame.timestamp_source === 'requested_seek_approximate' ? '时间来自抽帧请求位置，为近似定位时间。' : '时间来自估算值，为近似定位时间。';
      button.append(imageHolder,caption); card.append(button,frameFacts(frame));
      const retry = element('button','visual-frame-retry','重试此帧'); retry.type = 'button'; retry.hidden = true; card.append(retry);
      retry.addEventListener('click',() => fillFrame(frame,imageHolder,placeholder,retry));
      button.addEventListener('click',async () => {
        if (disposed || typeof onSeek !== 'function') return;
        grid.querySelectorAll('.visual-frame').forEach(item => item.classList.remove('selected')); card.classList.add('selected');
        message('已选择 ' + frameTime(frame) + ' 的原片位置，请在本条回答的播放器中核对。');
        try { await onSeek(frame.timestamp_ms); } catch (error) { message(error.message || '无法定位原片，请重试。',true); }
      });
      if (typeof onSeek !== 'function') { button.disabled = true; caption.lastChild.textContent = '原片抽样'; }
      return {card,ready:() => fillFrame(frame,imageHolder,placeholder,retry)};
    }
    async function showMore() {
      if (disposed || !more || more.disabled || shown >= frames.length) return;
      more.disabled = true; const batch = frames.slice(shown,shown + 12); shown += batch.length;
      const entries = batch.map(makeFrame); entries.forEach(item => grid.append(item.card));
      count.textContent = '已展示 ' + shown + ' / ' + frames.length + ' 个抽样画面；统计值使用全部有效抽样帧。';
      more.hidden = shown >= frames.length; more.textContent = '显示更多画面（剩余 ' + (frames.length - shown) + ' 帧）';
      // Limit thumbnail reads to three at a time; only explicitly expanded frames are fetched.
      for (let start = 0; start < entries.length && !disposed; start += 3) await Promise.allSettled(entries.slice(start,start + 3).map(item => item.ready()));
      if (!disposed) more.disabled = false;
    }
    async function render(data) {
      if (disposed) return; releaseURLs(); content.replaceChildren(); content.hidden = false; frames = []; shown = 0;
      if (data.status === 'no_video' || data.status === 'failed') { content.append(element('div','visual-evidence-empty',data.reason || (data.status === 'no_video' ? '该回答没有可解码的视频画面。' : '本地画面提取失败，未生成采集质量指标。'))); return; }
      frames = (Array.isArray(data.frames) ? data.frames : []).filter(frame => Number.isInteger(frame.index) && frame.index >= 0 && numeric(frame.timestamp_ms)).sort((a,b) => a.timestamp_ms - b.timestamp_ms);
      if (!frames.length) { content.append(element('div','visual-evidence-empty',data.reason || '本次没有可显示的抽样画面，未以 0 代替缺失指标。')); return; }
      const sampling = data.sampling || {}, summary = data.summary || {}, detector = data.detector || {};
      const detectable = frames.filter(frame => Number.isInteger(frame.face_count) && frame.face_count >= 0).length;
      const detected = frames.filter(frame => Number.isInteger(frame.face_count) && frame.face_count > 0).length;
      const metrics = element('dl','visual-evidence-summary');
      metric(metrics,'成功解码 / 计划抽样',valueText(sampling.decoded_frame_count,0) + ' / ' + valueText(sampling.requested_frame_count,0) + ' 帧','采样间隔 ' + (numeric(sampling.interval_ms) ? valueText(sampling.interval_ms / 1000,2) + ' 秒' : 'N/A') + '；不是逐帧分析');
      metric(metrics,'含检出人脸的帧 / 可检测帧',detector.status === 'available' && detectable ? detected + ' / ' + detectable + ' 帧' : 'N/A','可检测 ' + detectable + ' / ' + frames.length + ' 个抽样帧；检测不可用时不记为零');
      metric(metrics,'整帧平均亮度',valueText(summary.mean_brightness) + ' 灰度值','范围 0–255；对有效抽样帧的均值');
      metric(metrics,'整帧清晰度原始值',valueText(summary.mean_laplacian_variance) + ' 灰度²','拉普拉斯方差；受纹理、缩放和压缩影响');
      content.append(metrics);
      const detectorText = detector.status === 'available' ? '本地人脸检测可用。绿色框仅标记本帧检出的位置；未检出不代表原片中没有人脸。' : '本地人脸检测不可用：' + (detector.reason || '未提供原因') + '。原始画面与整帧指标仍可查看。';
      content.append(element('p','visual-evidence-detector',detectorText + (detector.name ? ' 检测器：' + detector.name + '。' : '')));
      if (data.reason) content.append(element('p','visual-evidence-status',data.reason));
      count = element('p','visual-evidence-detector'); grid = element('div','visual-evidence-grid'); more = element('button','visual-evidence-load','显示更多画面'); more.type = 'button'; more.addEventListener('click',showMore); content.append(count,grid,more);
      content.append(element('p','visual-evidence-footnote','亮度与拉普拉斯方差在整张缩略帧上计算（最长边不超过 640 像素），并非原分辨率数值。画面在本机抽取，人脸框用于采集可用性核对。上述指标不进入内容模型、评分或追问；不表示诚信、人格、情绪或心理状态。' + (data.algorithm_version ? ' 提取版本：' + data.algorithm_version + '。' : '')));
      content.append(element('p','visual-evidence-print-note','本报告仅打印已展开的抽样画面。完整原片请在本机服务中回放核对。'));
      await showMore();
    }
    async function load() {
      if (disposed || busy || audioOnly) return;
      if (loaded) { content.hidden = false; loadButton.textContent = '收起画面与采集质量'; return; }
      busy = true; loadButton.disabled = true; loadButton.textContent = '正在本机提取画面…'; message('正在读取本地原片并提取抽样画面。提取过程不会更改回答与评分。');
      try {
        payload = await fetchJSON(); if (disposed) return;
        if (!payload || !['succeeded','partial','no_video','failed'].includes(payload.status)) throw new Error('本地画面接口没有返回可识别的结果。');
        await render(payload); if (disposed) return; loaded = payload.status !== 'failed';
        loadButton.textContent = loaded ? '收起画面与采集质量' : '重试提取画面';
        message(payload.status === 'partial' ? '部分画面提取成功，请结合下方成功帧数与原因核对。' : payload.status === 'succeeded' ? '本地抽样画面已载入。选择时间点可回到原片核对。' : payload.reason || '本次没有可用视频画面。',payload.status === 'failed');
      } catch (error) { message(error.message || '无法读取本地画面，请确认服务后重试。',true); loadButton.textContent = '重试提取画面'; }
      finally { if (!disposed) { busy = false; loadButton.disabled = false; } }
    }
    loadButton.addEventListener('click',() => { if (disposed) return; if (loaded) { content.hidden = !content.hidden; loadButton.textContent = content.hidden ? '查看面部画面与采集质量' : '收起画面与采集质量'; } else load(); });
    if (audioOnly) { loadButton.hidden = true; intro.textContent = '该回答仅包含音频，没有视频画面。可通过原始音频和录音能量时间线核对采集质量。'; message('视频画面、人脸检测和整帧图像指标：不适用（音频回答）。'); }
    function dispose() { if (disposed) return; disposed = true; releaseURLs(); content.querySelectorAll('img').forEach(image => image.removeAttribute('src')); }
    return {element:root,load,dispose};
  }
  window.SuitVisualEvidence = Object.freeze({mount});
})();
