/* Manual playback alignment for one explicit answer and transcript revision. */
(() => {
  'use strict';
  const displayText = text => window.SuitTextDisplay?.simplify(text) ?? String(text ?? '');
  const el = (tag, text, cls) => { const node = document.createElement(tag); if (text != null) node.textContent = tag === 'pre' ? String(text) : displayText(text); if (cls) node.className = cls; return node; };
  const integer = Number.isSafeInteger;
  const seconds = ms => (ms / 1000).toFixed(3);
  function milliseconds(value) {
    const text = String(value).trim();
    if (!/^(?:\d+(?:\.\d{0,3})?|\.\d{1,3})$/.test(text)) throw new Error('时间请填写非负秒数，最多保留三位小数。');
    const ms = Math.round(Number(text) * 1000);
    if (!integer(ms)) throw new Error('时间超出可用范围。');
    return ms;
  }
  function mount({answerId, revision, fetchTimeline, save, onSaved} = {}) {
    const root = el('details', null, 'research-observation alignment-editor');
    root.append(el('summary', '人工校准 · 转写 v' + revision));
    const status = el('p', '展开后载入本版本句段。', 'research-caption'); status.setAttribute('role', 'status'); status.setAttribute('aria-live', 'polite');
    const content = el('div'), retry = el('button', '重新载入句段'); retry.type = 'button'; retry.hidden = true;
    root.append(status, retry, content);
    let timeline = null, rows = [], busy = false, loading = null, saveButton, fields = [], draftKey = null;
    const tell = (message, error = false) => { status.textContent = message; status.className = error ? 'notice warning' : 'research-caption'; };
    const configured = typeof answerId === 'string' && answerId.length > 0 && integer(revision) && revision >= 1 && typeof fetchTimeline === 'function' && typeof save === 'function';
    if (!configured) { tell('人工校准配置不完整，无法提交。', true); return {element: root, load: async () => null}; }

    function validate(data) {
      if (!data || data.schema_version !== 'interview-timeline.v1' || data.answer_id !== answerId || data.transcript_revision !== revision) throw new Error('返回的时间线不属于指定回答、转写版本或支持的格式。');
      const media = data.media || {}, duration = media.duration_ms;
      if (media.available !== true || !integer(duration) || duration <= 0 || duration > 180000 || typeof media.media_sha256 !== 'string' || !media.media_sha256) throw new Error('此回答没有可校准的原始媒体或有效时长。');
      if (!Array.isArray(data.segments) || !data.segments.length || typeof data.text !== 'string') throw new Error('此版本没有可校准的句段。');
      const chars = Array.from(data.text), ids = new Set(); let end = 0;
      for (const segment of data.segments) {
        if (!segment || typeof segment.segment_id !== 'string' || !segment.segment_id || segment.segment_id.length > 100 || ids.has(segment.segment_id)
          || typeof segment.text !== 'string' || !integer(segment.char_start) || !integer(segment.char_end) || segment.char_start < end
          || segment.char_end <= segment.char_start || segment.char_end > chars.length || chars.slice(segment.char_start, segment.char_end).join('') !== segment.text) throw new Error('句段标识、原文或字符位置不一致，请重新载入。');
        ids.add(segment.segment_id); end = segment.char_end;
      }
      if (!data.segments.some(segment => segment.text.trim())) throw new Error('此版本没有可校准的有效原文。');
      return data;
    }
    function input(labelText, type, cls) {
      const label = el('label', labelText), field = el(type === 'textarea' ? 'textarea' : 'input');
      if (type !== 'textarea') field.type = type;
      if (cls) field.className = cls;
      label.append(field); fields.push(field); return {label, field};
    }
    function render(data) {
      content.replaceChildren(); rows = []; fields = [];
      content.append(el('p', '时间相对原片起点，单位为秒。保存的是本版本的完整时间映射：未勾选句段将变为“未对齐”，不会保留旧时间。只校准回放时间，不修改原文、评分或报告版本。', 'research-caption'));
      content.append(el('p', '原片时长 ' + seconds(data.media.duration_ms) + ' 秒；请对照原片核准每段起止时间。', 'research-caption'));
      for (const segment of data.segments.filter(item => item.text.trim())) {
        const row = el('div', null, 'research-evidence-unit'), check = input('纳入本次时间映射', 'checkbox', 'alignment-include'); check.label.className = 'checkline';
        const timed = integer(segment.start_ms) && integer(segment.end_ms) && 0 <= segment.start_ms && segment.start_ms < segment.end_ms && segment.end_ms <= data.media.duration_ms;
        check.field.checked = timed;
        const start = input('开始（秒）', 'number', 'alignment-start'), end = input('结束（秒）', 'number', 'alignment-end');
        for (const field of [start.field, end.field]) { field.min = '0'; field.max = seconds(data.media.duration_ms); field.step = '0.001'; field.disabled = !timed; }
        if (timed) { start.field.value = seconds(segment.start_ms); end.field.value = seconds(segment.end_ms); }
        check.field.addEventListener('change', () => { start.field.disabled = end.field.disabled = !check.field.checked || busy; });
        const grid = el('div', null, 'research-grid'); grid.append(start.label, end.label);
        row.append(el('p', segment.text), check.label, grid);
        rows.push({segment, include: check.field, start: start.field, end: end.field}); content.append(row);
      }
      const reviewer = input('复核人（必填）', 'text', 'alignment-reviewer'), reason = input('校准依据 / 原因（必填）', 'textarea', 'alignment-reason');
      reviewer.field.maxLength = 100; reason.field.maxLength = 2000; reviewer.field.required = reason.field.required = true; reason.field.rows = 3;
      const count = el('p', null, 'research-caption');
      function recount() { const selected = rows.filter(row => row.include.checked).length; count.textContent = `本次纳入 ${selected} / ${rows.length} 段；${rows.length - selected} 段将未对齐。`; }
      rows.forEach(row => row.include.addEventListener('change', recount)); recount();
      saveButton = el('button', '保存人工时间映射'); saveButton.type = 'button'; saveButton.className = 'alignment-save';
      saveButton.addEventListener('click', () => commit(reviewer.field, reason.field));
      content.append(reviewer.label, reason.label, count, saveButton);
    }
    async function load() {
      if (loading) return loading;
      if (timeline || busy) return timeline;
      retry.disabled = true; retry.hidden = true; tell('正在载入转写 v' + revision + ' 的句段…');
      loading = (async () => {
        try { const data = validate(await Promise.resolve().then(() => fetchTimeline(revision))); timeline = data; render(data); tell('已载入转写 v' + revision + '。请按原片核准时间后保存。'); return data; }
        catch (error) { tell(error?.message || '时间线载入失败，请重试。', true); retry.hidden = false; return null; }
        finally { retry.disabled = false; loading = null; }
      })();
      return loading;
    }
    async function commit(reviewer, reason) {
      if (busy || !timeline) return;
      let body;
      try {
        const who = reviewer.value.trim(), why = reason.value.trim();
        if (!who || who.length > 100 || !why || why.length > 2000) throw new Error('请填写复核人（最多 100 字）和校准原因（最多 2000 字）。');
        let previousEnd = 0;
        const segments = rows.filter(row => row.include.checked).map(row => {
          const start = milliseconds(row.start.value), end = milliseconds(row.end.value);
          if (start >= end || end > timeline.media.duration_ms) throw new Error('每段结束时间须晚于开始，且不得超过原片时长。');
          if (start < previousEnd) throw new Error('按原文顺序，句段时间不能倒退或重叠。');
          previousEnd = end; return {segment_id: row.segment.segment_id, start_ms: start, end_ms: end};
        });
        if (!segments.length || segments.length > 1000) throw new Error('每次请纳入 1 至 1000 个句段。');
        body = {revision, segments, reviewer: who, reason: why};
        const signature = JSON.stringify(body);
        if (!draftKey || draftKey.signature !== signature) draftKey = {signature, key: crypto.randomUUID()};
        body.idempotency_key = draftKey.key;
      } catch (error) { tell(error?.message || '请检查时间映射输入。', true); return; }
      busy = true; saveButton.disabled = true; fields.forEach(field => { field.disabled = true; }); tell('正在保存人工时间映射…');
      try {
        const result = await save(body);
        tell('人工时间映射已保存' + (integer(result?.version) ? '（版本 ' + result.version + '）' : '') + '。请刷新时间线查看；输入已保留。');
        if (typeof onSaved === 'function') {
          try { await onSaved(result); }
          catch (_) { tell('人工时间映射已保存，但时间线刷新失败。请重新打开时间线；无需重复提交。', true); }
        }
      } catch (error) { tell(error?.message || '保存失败，输入已保留，可重试。', true); }
      finally {
        busy = false; saveButton.disabled = false; fields.forEach(field => { field.disabled = false; });
        rows.forEach(row => { row.start.disabled = row.end.disabled = !row.include.checked; });
      }
    }
    retry.addEventListener('click', () => load());
    root.addEventListener('toggle', () => { if (root.open && !timeline) load(); });
    root.addEventListener('keydown', event => { if (event.key === 'Enter' && event.target?.tagName === 'INPUT') event.preventDefault(); });
    return {element: root, load};
  }
  window.SuitAlignmentEditor = {mount};
})();
