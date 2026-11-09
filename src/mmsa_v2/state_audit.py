"""Explain a saved automatic five-state estimate using its own observation timeline."""

from bisect import bisect_right
from collections import Counter
from html import escape
import json
from math import isfinite
import re
from .state_rules import METHOD_V1, SMILE_THRESHOLDS

REFERENCE_LINKS = (
    ("OpenFace 2.0 · 面部点位与动作单元", "https://ieeexplore.ieee.org/document/8373812/"),
    ("Progressive Teacher · 七类外显表情识别", "https://arxiv.org/abs/2205.14361"),
    ("pYIN · 声音基频估计", "https://webspace.eecs.qmul.ac.uk/s.e.dixon/pub/2014/MauchDixon-PYIN-ICASSP2014.pdf"),
    ("Plutchik · 情绪类别与强度", "https://www.jstor.org/stable/27857503"),
)


def _render_legacy_state_audit(report):
    """Count sampled FER votes before filtering and account for rejected milliseconds."""
    labels = Counter()
    accepted = Counter()
    rejected_ms = Counter()
    known_estimates = []
    methods = {}
    for qid, answer in report.get("answers", {}).items():
        estimate = answer.get("state_estimate") or {}
        # The report gives manual annotations precedence, including a deliberately empty set.
        method = estimate.get("method")
        if answer.get("observations") or method not in SMILE_THRESHOLDS:
            continue
        methods.setdefault(method, []).append(qid)
        known_estimates.append(estimate)
        modules = (answer.get("processing") or {}).get("v1", {}).get("modules", {})
        segments = sorted(estimate.get("segments", []), key=lambda segment: segment["start"])
        starts = [round(seg["start"] * 1000) for seg in segments]
        for sample in modules.get("expression", {}).get("observations", []):
            label = (sample.get("values") or {}).get("label") or "missing"
            labels[label] += 1
            at = sample.get("start_ms")
            if at is not None:
                index = bisect_right(starts, at) - 1
                if index >= 0 and at < round(segments[index]["end"] * 1000):
                    accepted[label] += 1
        for interval in estimate.get("evidence", []):
            if interval.get("state") is None:
                ms = round(interval["end"] * 1000) - round(interval["start"] * 1000)
                rejected_ms[interval.get("reason") or "缺少有效观测"] += ms
    if not known_estimates:
        return ""

    unknown_ms = sum(round(e["duration_seconds"] * 1000)
                     - sum(round(s["end"] * 1000) - round(s["start"] * 1000) for s in e.get("segments", []))
                     for e in known_estimates)
    uncovered = unknown_ms - sum(rejected_ms.values())
    if uncovered > 0:
        rejected_ms["没有对应采样窗口的时长（含片尾）"] += uncovered

    audit = []
    if labels:
        audit.append(f'<p>已保存 {sum(labels.values())} 个视频采样窗。表情模型首选中性 {labels["neutral"]} 个，'
                     f'其中 {accepted["neutral"]} 个通过五档规则；首选愉悦 {labels["happy"]} 个，'
                     f'其中 {accepted["happy"]} 个通过；首选悲伤 {labels["sad"]} 个，其中 {accepted["sad"]} 个通过。'
                     '这里统计采样次数，上方五态占比按通过核对的秒数计算。</p>')
    if rejected_ms:
        reasons = "".join(f'<tr><td>{escape(reason)}</td><td>{ms / 1000:g} 秒</td></tr>'
                          for reason, ms in rejected_ms.most_common())
        audit.append('<table><thead><tr><th>自动估计未纳入的原因</th><th>时长</th></tr></thead>'
                     f'<tbody>{reasons}</tbody></table>')
    links = " · ".join(f'<a href="{url}" target="_blank" rel="noopener noreferrer">{title}</a>'
                       for title, url in REFERENCE_LINKS)
    positive_rows = []
    versions = []
    for method, qids in methods.items():
        smile = SMILE_THRESHOLDS[method]
        version = "v" + method.rsplit(".", 1)[-1]
        suffix = f"（{version}）" if len(methods) > 1 else ""
        versions.append(f'{version}（{escape("、".join(qids))}）')
        positive_rows.append(
            f'<tr><td>洋溢{suffix}</td><td>愉悦、嘴角上提强于下拉，上提 AU12 ≥ {smile.exuberant_lip:g} 且颊部提升 AU06 ≥ {smile.exuberant_cheek:g}</td><td>100</td></tr>'
            f'<tr><td>饱满{suffix}</td><td>愉悦、AU12 ≥ {smile.upbeat_lip:g} 且强于下拉，低于洋溢条件</td><td>75</td></tr>')
    return ('<details class="state-method"><summary>五态算法、未分类原因与参考文献</summary>'
            f'<p>本场自动判档口径：{"、".join(versions)}。</p>'
            '<p>系统先对齐同一时段的面部动作、表情标签和声音，再按下表判定。'
            '单人跟踪和同期音轨须可用，声音窗口须检出发声，排除近静音和削波。'
            '七类表情中前两名的原始模型输出差须达到 0.25。</p>'
            '<table><thead><tr><th>比赛表达档位</th><th>演示判定条件（AU 强度 0–5）</th><th>代表分</th></tr></thead>'
            '<tbody>'+''.join(positive_rows)+
            '<tr><td>平和</td><td>中性，且 AU12 和嘴角下拉 AU15 均 &lt; 2.5</td><td>50</td></tr>'
            '<tr><td>略微 down</td><td>悲伤、1 ≤ AU15 &lt; 2.5，且下拉强于上提</td><td>25</td></tr>'
            '<tr><td>非常不好</td><td>悲伤、AU15 ≥ 2.5，且下拉强于上提</td><td>0</td></tr></tbody></table>'
            '<p>音高与能量曲线供回听核查；声音在这一版参与发声和质量筛选。'
            '只有中性、愉悦、悲伤三类与相应动作相符时才进入五档，其余类别或冲突时段留为未分类。'
            '这样的筛选会改变有效时段内各档的占比。</p>'
            + "".join(audit)
            + '<p>以下研究分别用于采集、识别和概念解释。五档映射、各项阈值、代表分和 25% 权重均为本项目演示口径，'
            '在本项目中文面试片段上的各档准确率仍待独立标注检验。</p><p>'
            + links + '</p></details>')


EMOTION_LABELS = {"angry":"愤怒", "disgust":"厌恶", "disgusted":"厌恶", "fear":"恐惧",
                  "fearful":"恐惧", "happy":"愉悦", "neutral":"中性", "other":"其他",
                  "sad":"悲伤", "surprised":"惊讶", "surprise":"惊讶", "anger":"愤怒", "unknown":"未知"}


def _finite(value):
    return type(value) in (int, float) and isfinite(value)


def _display(value):
    if _finite(value):
        return f"{value:.3f}".rstrip("0").rstrip(".")
    return str(value)


def _saved_structure(value):
    """Display the saved policy without running it or treating text as markup."""
    if isinstance(value, (dict, list)):
        return '<pre class="state-saved-policy">'+escape(json.dumps(value,ensure_ascii=False,indent=2,default=str))+'</pre>'
    return '<p>'+escape(str(value))+'</p>'


def render_state_error(answer):
    """Explain a saved inference failure without implying the old score is current."""
    error = answer.get("state_estimate_error")
    if not isinstance(error, dict) or not error or answer.get("observations"):
        return ""
    message = error.get("message") or "本次多模态分析未生成有效结果。"
    method = error.get("method")
    return ('<div class="state-analysis-error"><p><b>多模态分析未完成</b> · '
            + escape(str(message)) + '</p>'
            + ('<p class="micro">方法 '+escape(str(method))+'</p>' if method else '')
            + '<p class="micro">本次缺口不按平和或零分处理；处理上述原因后可重新分析本题。</p></div>')


def _render_au_patterns(patterns):
    if not isinstance(patterns, dict):
        return _saved_structure(patterns)
    rows=[]
    for label, pattern in patterns.items():
        if not isinstance(pattern, dict):
            continue
        units=pattern.get("aus") or []
        units=" + ".join(str(value) for value in units) if isinstance(units,list) else str(units)
        fraction=pattern.get("matched_fraction")
        matched=_display(fraction*100)+"%" if _finite(fraction) else "—"
        active,valid=pattern.get("active_seconds"),pattern.get("valid_seconds")
        if _finite(active) and _finite(valid):
            matched += f'（{_display(active)} / {_display(valid)} 秒）'
        strength=pattern.get("strength")
        strength=_display(strength) if _finite(strength) else "—"
        rows.append('<tr>'+''.join('<td>'+escape(value)+'</td>' for value in
                    (EMOTION_LABELS.get(label,label),units or "—",matched,strength))+'</tr>')
    if not rows:
        return _saved_structure(patterns)
    return ('<table><thead><tr><th>组合方向</th><th>面部动作</th><th>本窗匹配占比</th>'
            '<th>组合动作强度</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>'
            '<p class="micro">匹配占比以本窗有效采样时长为分母；组合动作强度为 0–1 的动作激活描述，不是情绪强度。</p>')


def render_state_window_values(segment):
    """Render model measurements from this exact saved window, not a fresh inference."""
    values = segment.get("values") or {}
    if not isinstance(values, dict) or not any(k in values for k in ("au_intensity", "speech_scores", "fusion_rule", "au_patterns")):
        return ""
    parts = []
    intensities = values.get("au_intensity") or {}
    presence = values.get("au_presence") or {}
    au = []
    for name, strength in sorted(intensities.items()):
        if not re.fullmatch(r"AU\d{2}", name) or not _finite(strength):
            continue
        detected = presence.get(name)
        suffix = ("（检出）" if detected == 1 else "（未检出）" if detected == 0 else
                  f'（检出占比 {_display(detected*100)}%）' if _finite(detected) and 0 <= detected <= 1 else "")
        au.append(f'{name} {_display(strength)}{suffix}')
    if au:
        parts.append('<p><b>面部动作平均强度（0–5）</b>　'+escape(" · ".join(au))+'</p>')
    patterns = values.get("au_patterns")
    if patterns:
        parts.append('<details class="state-patterns"><summary>本窗 AU 组合匹配</summary>'+_render_au_patterns(patterns)+'</details>')
    if "face_state" in values:
        parts.append('<p><b>面部侧状态</b>　'+escape(values.get("face_state") or "未形成五档判断")+'</p>')
    scores = values.get("speech_scores") or {}
    voice_range = ""
    voice_start = segment.get("speech_start", values.get("speech_start"))
    voice_end = segment.get("speech_end", values.get("speech_end"))
    if _finite(voice_start) and _finite(voice_end):
        voice_range = f' · 语音模型窗口 {_display(voice_start)}–{_display(voice_end)} 秒'
    if values.get("speech_label"):
        label = values["speech_label"]
        parts.append('<p><b>声音情绪模型</b>　'+escape(EMOTION_LABELS.get(label,label)+voice_range)+'</p>')
    if "speech_state" in values:
        parts.append('<p><b>声音侧状态</b>　'+escape(values.get("speech_state") or "未形成五档判断")+'</p>')
    numeric = [(label,value) for label,value in scores.items() if _finite(value)]
    if numeric:
        text = " · ".join(f'{EMOTION_LABELS.get(label,label)} {_display(value)}' for label,value in numeric)
        parts.append('<details class="state-speech-scores"><summary>声音模型类别分值</summary><p>'
                     +escape(text)+'</p><p class="micro">这些是模型类别输出，不代表情绪强度或已验证准确率；同一语音窗的结果可用于多个面部采样窗。</p></details>')
    if values.get("fusion_rule"):
        parts.append('<p><b>声画融合依据</b>　'+escape(str(values["fusion_rule"]))+'</p>')
    comparison = []
    for key,title in (("face_valence","愉悦度"),("face_arousal","活跃度")):
        if _finite(values.get(key)):
            comparison.append(f'{title} {_display(values[key])}')
    if comparison:
        parts.append('<p class="micro">独立视觉模型对照（非 OpenFace 输出）：'+escape(" · ".join(comparison))+'</p>')
    return '<div class="state-window-values">'+''.join(parts)+'</div>'


def _render_joint_state_audit(report):
    estimates = [(qid,a.get("state_estimate") or {}) for qid,a in report.get("answers",{}).items()
                 if not a.get("observations") and (a.get("state_estimate") or {}).get("fusion_policy")]
    if not estimates:
        return ""
    parts = []
    for qid, estimate in estimates:
        models = estimate.get("models") or {}
        rows = []
        for branch, label in (("face","面部动作"),("speech","声音情绪"),("visual_comparison","独立视觉对照")):
            model = models.get(branch) or {}
            if not model:
                continue
            identifier = model.get("model_id") or model.get("name") or "未记录模型名称"
            provenance = model.get("provenance") or {}
            trace = model.get("sha256") or model.get("weights_sha256") or provenance.get("weights_sha256") or provenance.get("sha256") or provenance.get("model_sha256")
            if not trace and provenance.get("weights_manifest_sha256"):
                trace = "权重清单 SHA256：" + str(provenance["weights_manifest_sha256"])
            if not trace and provenance.get("archive_sha256"):
                trace = "模型包 SHA256：" + str(provenance["archive_sha256"])
            trace = trace or "未记录模型指纹"
            rows.append(f'<tr><td>{label}</td><td>{escape(str(identifier))}</td><td><code>{escape(str(trace))}</code></td></tr>')
        policy = estimate["fusion_policy"]
        policy_parts = []
        if isinstance(policy, dict):
            for key,label in (("version","映射版本"),("description","判定流程"),("source_notice","依据与适用范围"),
                              ("pattern_source","AU 组合文献"),("time_rule","时段口径"),
                              ("face_rules","面部 AU 组合与五档规则"),("voice_mapping","声音结果映射"),("classification_rules","声画融合与五档规则")):
                if key in policy:
                    policy_parts.append('<div><b>'+label+'</b>'+_saved_structure(policy[key])+'</div>')
        else:
            policy_parts.append(_saved_structure(policy))
        parts.append('<details class="state-policy-question"><summary>'+escape(str(qid))+' · 保存的模型与融合口径</summary>'
                     '<p>方法 '+escape(str(estimate.get("method") or "未记录"))+'</p>'
                     + ('<table><thead><tr><th>分支</th><th>模型</th><th>模型指纹</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>' if rows else '')
                     + ''.join(policy_parts)+'</details>')
    return ('<details class="state-method state-method-joint"><summary>面部、声音如何共同形成五态</summary>'
            '<p>先分别提取面部动作与声音情绪结果，再按本场保存的融合规则给出五态，最后按已分类秒数汇总。'
            '面部 AU 组合有文献依据，五档映射和代表分属于本项目演示规则；结果描述可观测表达，不等同于真实内心状态。'
            'OpenFace 点位和动作采样继续保留，逐时段的声音窗口与融合依据可在题目细则中回看。</p>'
            + ''.join(parts)+'</details>')


def render_state_audit(report):
    """Preserve historic rules and show only the policy saved with each estimate."""
    return _render_legacy_state_audit(report) + _render_joint_state_audit(report)
