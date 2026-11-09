"""Visible per-answer previews from recorded OpenFace coordinates and acoustic samples."""
from html import escape
from math import isfinite

from .reporting import _number
from .scoring import STATE_POINTS


def _finite(value):
    return type(value) in (int, float) and isfinite(value)


def whole_state_proportions(observation):
    """Display shares with the complete recording as denominator, never impute gaps."""
    duration = observation.get("answer_seconds")
    valid = observation.get("valid_seconds")
    dist = observation.get("proportions")
    if not (_finite(duration) and duration > 0 and _finite(valid) and 0 <= valid <= duration):
        return None
    if valid == 0:
        return {**dict.fromkeys(STATE_POINTS, 0.0), "未分类": 1.0}
    if not isinstance(dist, dict) or any(not _finite(dist.get(label)) for label in STATE_POINTS):
        return None
    return {**{label: dist[label] * valid / duration for label in STATE_POINTS},
            "未分类": (duration - valid) / duration}


def render_state_denominators(observation):
    """Keep whole-recording and classified-only distributions side by side."""
    whole = whole_state_proportions(observation)
    dist = observation.get("proportions")
    if whole is None:
        return '<p class="micro">原片总时长未确认，暂无法计算占整段作答的比例。</p>'
    rows = []
    for label in STATE_POINTS:
        within = _number(dist.get(label), "%", 100) if dist else "—"
        rows.append(f'<tr><td>{escape(label)}</td><td>{_number(whole[label],"%",100)}</td><td>{within}</td></tr>')
    rows.append(f'<tr><td>未分类</td><td>{_number(whole["未分类"],"%",100)}</td><td>不进入五态分母</td></tr>')
    return ('<table class="state-denominators"><thead><tr><th>状态</th><th>占整段作答</th>'
            '<th>占已分类时段</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>'
            '<p class="micro">两列使用不同分母；未分类时段保留为空缺，不等同于平和或低分。</p>')


def _jump(qid, channel, at_ms, label):
    return (f'<a href="#q-{escape(qid, quote=True)}" data-media-channel="{channel}" '
            f'data-at-ms="{int(at_ms)}">{escape(label)}</a>')


def _face(modules, qid):
    geometry = (modules.get("gaze", {}).get("geometry") or modules.get("au", {}).get("geometry") or {})
    for frame in geometry.get("observations", []):
        points = frame.get("landmarks") or []
        if (frame.get("quality") != "valid" or len(points) != 68 or
                not all(len(p) == 2 and all(_finite(v) for v in p) for p in points)):
            continue
        w, h = frame.get("frame_width"), frame.get("frame_height")
        if not (_finite(w) and _finite(h) and w > 0 and h > 0):
            continue
        xy = [(x*w, y*h) for x,y in points]
        x1, x2 = min(x for x,_ in xy), max(x for x,_ in xy)
        y1, y2 = min(y for _,y in xy), max(y for _,y in xy)
        extent = max(x2-x1, y2-y1)
        if extent <= 0:
            continue
        pad = extent * .085
        bounds = f'{x1-pad:.3f} {y1-pad:.3f} {x2-x1+2*pad:.3f} {y2-y1+2*pad:.3f}'
        circles = ''.join(f'<circle cx="{x:.3f}" cy="{y:.3f}" r="{extent*.0095:.3f}"/>' for x,y in xy)
        t = frame["start_ms"]
        svg = f'<svg class="landmark-snapshot" viewBox="{bounds}" role="img" aria-label="本题 OpenFace 实测 68 点">{circles}</svg>'
        return (svg + _jump(qid, "geometry", t, f'{_number(t/1000)} 秒 · 原片叠加 68 点'))
    return '<p>本题无有效的 OpenFace 68 点采样。</p>'


def _audio(modules, qid, seconds):
    rows = modules.get("audio", {}).get("observations") or []
    traces = []
    for key, title, unit in (("rms_dbfs","能量","dBFS"), ("f0_hz","音高","Hz")):
        valid = [r for r in rows if r.get("quality")=="valid" and _finite(r.get("values",{}).get(key))]
        if not valid or not seconds:
            traces.append(f'<p>{title} · 无有效采样</p>')
            continue
        low = min(r["values"][key] for r in valid)
        high = max(r["values"][key] for r in valid)
        commands = []
        prev_end = None
        for r in valid:
            y = 15 if high == low else 27 - 24*(r["values"][key]-low)/(high-low)
            x = min(220, max(0, 220*r["start_ms"]/(1000*seconds)))
            end = min(220, max(x, 220*r["end_ms"]/(1000*seconds)))
            op = "L" if prev_end == r["start_ms"] else "M"
            commands.append(f'{op}{x:.2f} {y:.2f}H{end:.2f}')
            prev_end = r["end_ms"]
        line = " ".join(commands)
        traces.append(f'<div class="acoustic-trace"><span>{title} · {_number(low)} 至 {_number(high)} {unit}</span>'
                      f'<svg viewBox="0 0 220 30" preserveAspectRatio="none" role="img" aria-label="逐窗{title}">'
                      f'<path d="{line}"/></svg></div>')
    if rows:
        traces.append(_jump(qid, "audio", rows[0]["start_ms"], "回听原声与查看逐窗实测值"))
    return "".join(traces)


def _emotion_sources(answer, qid):
    from .state_audit import render_state_error
    error = render_state_error(answer)
    estimate = answer.get("state_estimate") or {}
    if not estimate.get("fusion_policy"):
        return error
    models = estimate.get("models") or {}
    names = []
    for key, label in (("face","面部"),("speech","声音")):
        model = models.get(key) or {}
        if model.get("model_id"):
            names.append(label+"："+str(model["model_id"]))
    return (error+'<p class="micro">'+escape(" · ".join(names))+'</p>'
            f'<p><a href="#q-{escape(qid,quote=True)}">查看本题逐时段 AU、声音情绪与融合依据</a></p>')


def render_media_evidence(report):
    """Keep previews and denominators inspectable without opening each question or requesting video."""
    from .state_audit import render_state_audit
    plan = {q["id"]: (i,q) for i,q in enumerate(report.get("plan",[]), start=1)}
    items = []
    weighted_terms = []
    for obs in report.get("questions",[]):
        if obs.get("capture") == "not_applicable" or obs["id"] not in plan:
            continue
        number, q = plan[obs["id"]]
        qid = obs["id"]
        a = report.get("answers",{}).get(qid, {})
        modules = ((a.get("processing") or {}).get("v1") or {}).get("modules",{})
        duration, valid = obs.get("answer_seconds"), obs.get("valid_seconds") or 0
        remaining = max(0, duration-valid) if duration is not None else None
        score = f'{_number(obs["score"])} / 100' if obs.get("score") is not None else "待评"
        if obs.get("score") is not None and valid:
            weighted_terms.append(f'{_number(obs["score"])} × {_number(valid)}')
        method = "教师标注" if a.get("observations") else "分析未完成" if a.get("state_estimate_error") else "本地估计" if a.get("state_estimate") else "待采集"
        items.append(f'<article class="mm-proof-card"><h4>第 {number} 题 · {escape(q.get("factor_name") or qid)}'
                     f' <span>{score}</span></h4><p class="mm-prompt">{escape(q.get("prompt") or "")}</p>'
                     f'<p>{method} · 有效 {_number(valid)} / {_number(duration)} 秒 · 覆盖 {_number(obs.get("coverage"),"%",100)}'
                     f' · 未分类 {_number(remaining)} 秒</p>'
                     + render_state_denominators(obs) +
                     f'<div class="mm-proof-signals"><div>{_face(modules,qid)}</div>'
                     f'<div>{_audio(modules,qid,duration)}</div></div>'
                     + _emotion_sources(a,qid) + '</article>')
    if not items:
        return ""
    session = report["session"]
    average = ""
    if session.get("score") is not None:
        average = (f'<p>每题按有效时长将五态换算为百分制。全场按各题有效秒数加权，'
                   f'有效 {_number(session["valid_seconds"])} 秒的平均分为 <b>{_number(session["score"])} / 100</b>。</p>')
        if 1 <= len(weighted_terms) <= 4:
            average += (f'<p class="micro">（{" ＋ ".join(weighted_terms)}）÷ {_number(session["valid_seconds"])}'
                        f' ≈ {_number(session["score"])} 分</p>')
    return ('<section id="multimodal-evidence"><h3>多模态评分依据 · 逐题面部点位与声音</h3>'
            + average + "".join(items) + render_state_audit(report) + '</section>')
