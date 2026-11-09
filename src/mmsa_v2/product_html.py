"""Self-contained V2 interview and cohort reports. All markup is rendered from one snapshot."""

from html import escape
import json
from math import cos, pi, sin
from pathlib import Path
import re
from urllib.parse import quote

from .reporting import COMPONENT_NAMES, _number, _observation_score
from .scoring import STATE_POINTS, SCORE_LABELS, score_summary
from .media_evidence import render_media_evidence, whole_state_proportions
from .state_audit import render_state_error, render_state_window_values


WEB = Path(__file__).resolve().parents[2] / "web" / "v2"
V1_SITE = WEB.parents[1] / "suit-demo" / "site"
COLORS = ("#15a47d", "#238ec6", "#6f89ad", "#ce903e", "#b76375")
SOURCES = {"local_review":"本地人工录入","local_auto":"本地规则计算","bailian_auto":"百炼 Qwen 内容评分","harness":"千问办公接口","mixed":"多个来源",
           "scripted_demo":"脚本演示","unrecorded":"历史来源未登记"}
STATUS = {"in_progress": "作答中", "created": "已建立", "interviewing": "作答中",
          "interview_open": "面试进行中", "unstarted": "待建面试", "awaiting_verification":"等待补答", "review_changes":"退回补充",
          "interview_finished": "已完成面试", "finished": "已完成面试",
          "awaiting_harness": "待千问分析", "draft": "分析草稿", "reviewed": "老师已复核",
          "request_changes": "退回补充", "approve": "已通过复核", "approved": "已通过复核",
          "not_started": "待面试", "not_applicable": "不适用", "complete": "完整覆盖",
          "partial": "部分覆盖", "unavailable": "未取得有效观测", "pending_review": "待老师复核"}


def safe(value, missing="未提供"):
    return escape(str(value)) if value is not None and value != "" else escape(missing)


def _script_data(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False).replace("<",r"\u003c").replace("\u2028",r"\u2028").replace("\u2029",r"\u2029")


def status_text(value):
    return safe(STATUS.get(value, value))


def _report_status(item):
    scripted = (item.get("draft") or {}).get("source") == "scripted_demo" or item.get("analysis_source") == "scripted_demo"
    reviewer = str((item.get("review") or {}).get("reviewer") or item.get("reviewer") or "")
    if item.get("status") == "reviewed" and (scripted or "脚本" in reviewer):
        return "演练脚本已完成模拟复核"
    return status_text(item.get("status"))


def _doc(title, body):
    styles = "\n".join((WEB / name).read_text(encoding="utf-8") for name in ("style.css", "report.css"))
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
    <meta name="viewport" content="width=device-width,initial-scale=1"><meta name="referrer" content="no-referrer">
    <title>{safe(title)} · 真才实鉴</title><style>{styles}</style></head><body>
    <main class="shell"><div class="mast"><span class="brand"><i></i>真才实鉴</span>
    <span class="meta">V2 · 通用管培生模拟面试</span></div>{body}</main></body></html>'''


def _section(title, content):
    return f'<section class="section"><h2 class="section-title">{safe(title)}</h2>{content}</section>'


def _metric(label, value, subtitle):
    return f'<div class="metric"><span>{safe(label)}</span><strong>{value}</strong><small>{subtitle}</small></div>'


def _fact(label, value):
    return f'<div class="fact"><dt>{safe(label)}</dt><dd>{safe(value)}</dd></div>'


def _bars(items):
    rows = []
    for i, (label, fraction, display) in enumerate(items):
        width = max(0, min(100, float(fraction or 0) * 100))
        color = COLORS[i % len(COLORS)]
        rows.append(f'<div class="bar"><div class="barline"><span>{safe(label)}</span><b>{display}</b></div>'
                    f'<div class="bartrack"><span style="width:{width:.5f}%;--bar-color:{color}"></span></div></div>')
    return '<div class="legend">' + ''.join(rows) + '</div>'


def _states(obs):
    proportions = obs.get("proportions")
    if not proportions:
        return f'<p class="note">{status_text(obs.get("status", "unavailable"))}</p>'
    level, band = obs.get("band_level"), obs.get("band")
    return ('<p class="micro"><b>已分类时段内分布</b> · 以下五项合计 100%，不包含未分类时段。</p>'
            + _state_bars(proportions) + f'<p class="note">第{safe(level)}档 · {safe(band)} · {_observation_score(obs)} 分<br>'
            f'有效 {_number(obs.get("valid_seconds"))} 秒 / 应分析 {_number(obs.get("answer_seconds"))} 秒'
            f' · 覆盖 {_number(obs.get("coverage"), "%", 100)}</p>')


def _radar(factors, label="八因子雷达图，每个因子满分5分"):
    if not factors:
        return '<p class="empty">等待八因子评价。</p>'
    n = len(factors)

    def point(index, value):
        angle = -pi / 2 + 2 * pi * index / n
        return 152 + cos(angle) * value, 114 + sin(angle) * value

    def points(coords):
        return ' '.join(f'{x:.2f},{y:.2f}' for x, y in coords)

    svg = [f'<svg class="radar" viewBox="0 0 304 228" role="img" aria-label="{safe(label)}">']
    for ring in range(1, 6):
        pts = points(point(i, 15 * ring) for i in range(n))
        svg.append(f'<polygon points="{pts}" fill="none" stroke="#dbe5ee"/>')
    rated_points = []
    for i, factor in enumerate(factors):
        rating = factor.get("rating")
        x, y = point(i, 75)
        lx, ly = point(i, 93)
        svg.append(f'<path d="M152 114L{x:.2f} {y:.2f}" stroke="#dde7ef"/>')
        svg.append(f'<text x="{lx:.2f}" y="{ly:.2f}" text-anchor="middle" dominant-baseline="middle" '
                   f'font-size="10" fill="#41566b">{safe(factor["name"])}</text>')
        if isinstance(rating, (int, float)) and 0 <= rating <= 5:
            rated_points.append(point(i, rating * 15))
    if len(rated_points) == n:
        svg.append(f'<polygon points="{points(rated_points)}" fill="#56b5aa44" stroke="#158f87" stroke-width="2"/>')
    for x, y in rated_points:
        svg.append(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.5" fill="#147c7b"/>')
    svg.append('</svg>')
    return ''.join(svg)


def _resume_facts(resume):
    tests = "、".join(f'{e.get("exam", "")} {e.get("score") if e.get("score") is not None else ""}'.strip() for e in resume.get("english", []))
    if not tests and resume.get("english_none"):
        tests = "无"
    edu = " · ".join(v for v in [resume.get("education"),resume.get("education_status")] if v)
    return ''.join(_fact(k, v) for k, v in [
        ("最高学历", edu), ("院校", resume.get("school")),
        ("院校情况", resume.get("school_tier")), ("有无实习", resume.get("internship")), ("英语水平", tests)])




def _answer_text(question, answer):
    value = answer.get("value")
    options = {o["id"]: o["text"] for o in question.get("options", [])}
    if isinstance(value, list):
        decoded = " → ".join(options.get(v, str(v)) for v in value)
    elif isinstance(value, dict):
        parts = []
        for field in question.get("fields", []):
            selected = value.get(field["id"])
            labels = {o["id"]: o["text"] for o in field.get("options", [])}
            parts.append(f'{field["prompt"]} {labels.get(selected, selected) or "未答"}')
        decoded = "\n".join(parts)
    else:
        decoded = options.get(value, value) if isinstance(value, str) else ""
    texts = [s for s in [decoded, answer.get("text"), answer.get("transcript")] if isinstance(s, str) and s]
    return "\n".join(dict.fromkeys(texts))


def _question_cards(report, workbench=False):
    rows = []
    observations = {q["id"]: q for q in report.get("questions", [])}
    factors = {f["id"]: f for f in report.get("factors", [])}
    for i, q in enumerate(report.get("plan", []), start=1):
        qid = q["id"]
        answer = report.get("answers", {}).get(qid) or {}
        obs = observations.get(qid, {})
        media = answer.get("media")
        player = ""
        if media:
            media_id = quote(str(report["id"]), safe="")
            url = f'/v2/api/sessions/{media_id}/answers/{quote(qid, safe="")}/media'
            if media.get("sha256"):
                url += "?sha256=" + quote(media["sha256"], safe="")
            offline_path = media.get("export_path")
            if offline_path:
                url = offline_path
            element = 'audio' if str(media.get("mime", "")).startswith("audio/") else 'video'
            caption = "随报告导出的原始片段" if offline_path else "原片可在本机工作台回放。"
            player = (f'<div class="evidence-timeline-player"><{element} controls preload="none" src="{safe(url)}"></{element}>'
                      f'<div class="evidence-player-overlay"></div></div><p class="micro">{caption}</p>')
            audit = (answer.get("processing") or {}).get("v1") or {}
            if audit.get("modules"):
                payload = {"modules":audit["modules"], "duration_ms":audit.get("duration_ms"), "transcript_revision":audit.get("transcript_revision")}
                player += ('<div class="v1-evidence"></div><script type="application/json" class="v1-data">'
                           + _script_data(payload) + '</script>')
            manual = answer.get("observations") or {}
            estimate = answer.get("state_estimate") or {}
            player += render_state_error(answer)
            spans = manual.get("segments") or []
            if spans:
                links = []
                for span in spans:
                    t1,t2 = span["start"],span["end"]
                    links.append(f'<li><button type="button" data-seek-seconds="{float(t1):g}">'
                                 f'{_number(t1)}–{_number(t2)} 秒</button> · {safe(span["state"])} · {_number(STATE_POINTS.get(span["state"]))} 分</li>')
                player += '<details><summary>已复核时段与原片定位</summary><ul class="list">'+''.join(links)+'</ul></details>'
            if estimate:
                body = []
                for segment in estimate.get("evidence") or []:
                    label = segment.get("state")
                    rating = f'{safe(label)} · {_number(STATE_POINTS[label])} 分' if label in STATE_POINTS else "未分类"
                    measurements = segment.get("values") or {}
                    measured = [f'{key} {_number(measurements[key])}' for key in ("AU06","AU12","AU15")
                                if measurements.get(key) is not None]
                    for key,title,unit in (("f0_hz","音高"," Hz"),("rms_dbfs","能量"," dBFS")):
                        if measurements.get(key) is not None:
                            measured.append(f'{title} {_number(measurements[key])}{unit}')
                    if measurements.get("speech_fraction") is not None:
                        measured.append(f'发声 {_number(measurements["speech_fraction"], "%", 100)}')
                    if measured and segment.get("example_end") is not None:
                        measured.insert(0, f'采样示例 {_number(segment["example_start"])}–{_number(segment["example_end"])} 秒')
                    details = safe(" · ".join(measured), "")
                    body.append(f'<tr><td><button type="button" data-seek-seconds="{float(segment["start"]):g}">'
                                f'{_number(segment["start"])}–{_number(segment["end"])} 秒</button></td>'
                                f'<td>{rating}</td><td>{safe(segment.get("reason"))}<div class="micro">{details}</div>'
                                + render_state_window_values(segment) + '</td></tr>')
                method = estimate.get("method")
                source = "本题按人工标注计分" if manual else "面部与声音联合估计" if estimate.get("fusion_policy") else "按有效时段的本地规则估计"
                player += f'<details><summary>五态估计依据 · {source}</summary><p>{safe(estimate.get("reason"),"")}</p>'
                if method:
                    player += f'<p class="micro">方法 {safe(method)}</p>'
                if body:
                    player += '<table><thead><tr><th>原片时段</th><th>状态及代表分</th><th>依据与质量</th></tr></thead><tbody>'+''.join(body)+'</tbody></table>'
                player += '</details>'
        reason = q.get("verification_reason") or q.get("reason")
        why = "追问依据" if q.get("parent_question_id") or q.get("condition_parent") else "选题依据"
        pick = f'<p><b>{why}</b>　{safe(reason)}</p>' if reason else ""
        factor = factors.get(q.get("factor_id"), {})
        judgment = ""
        if q.get("kind")=="text" and factor:
            if factor.get("anchors"):
                judgment += f'<details><summary>本题评分点 · {safe(factor.get("anchor_source"),"题库 H 列")}</summary><p class="quote">{safe(factor["anchors"])}</p></details>'
            if factor.get("reason"):
                citations = [ref["quote"] for ref in factor.get("evidence",[]) if ref.get("question_id")==qid]
                judgment += (f'<p><b>因子评级 {_number(factor.get("rating"))} / 5</b>　{safe(factor["reason"])}</p>'
                             + ''.join(f'<blockquote class="quote">{safe(t)}</blockquote>' for t in citations))
        hints = ''.join(f'<p>{safe(q[key])}</p>' for key in ("answer_hint","resume_hint") if q.get(key))
        action = (f'<p class="note noprint"><a href="#review-tools" data-review-question="{safe(qid)}">查看评分细则</a></p>'
                  if workbench else "")
        signal = (f'<p class="fact">选项原分 <b>{_number(answer["sjt_score"])} / 5</b> · {safe(q.get("score_source"))}</p>'
                  if answer.get("sjt_score") is not None else "")
        missing = "尚无作答原文"
        if media:
            missing = "已收到原片，尚无可用转写，请回放原片。" if answer.get("processing_transcribe") else "已收到原片，待转写"
        transcript_source = answer.get("transcription_provenance") or {}
        transcript_note = ""
        if transcript_source:
            transcript_note = (f'<p class="meta">语音转写：{safe(transcript_source.get("model"))} · '
                               f'{"未识别出有效原话，请回听录音" if transcript_source.get("status")=="empty" else "百炼音轨识别，原文可对照录音复核"}。'
                               '逐字时间戳未提供。</p>')
        rows.append(f'''<details id="q-{safe(qid)}"><summary>{i}. {safe(q["prompt"])}
          <small>{safe(q.get("factor_name"), "")}</small></summary>
          {pick}{hints}<h4>学生作答</h4><blockquote class="quote">{safe(_answer_text(q,answer),missing)}</blockquote>
          {transcript_note}{signal}{judgment}{player}{_states(obs)}{action}</details>''')
    return ''.join(rows) or '<p class="empty">尚无题目。</p>'


def _factors(factors, plan, short):
    open_questions_ready = any(q["kind"]=="text" for q in plan)
    parts = []
    for f in factors:
        coverage = ""
        empty = "等待有证据的分析"
        signal_html = ""
        matching = [q for q in plan if q.get("factor_id")==f["factor_id"]]
        if short:
            if not matching:
                coverage = "本场未抽到" if open_questions_ready else "待选问答"
                empty = "本场没有对应题目。" if open_questions_ready else "完成五道单选后确定问答题。"
            elif any(q["kind"]=="text" for q in matching):
                coverage = "问答评级"
            else:
                coverage = "情境单选"
            signal = f.get("choice_signal")
            if signal:
                signal_html = (f'<p><a href="#q-{safe(signal["question_id"])}">单选作答</a> · {safe(signal["answer_label"])}</p>'
                               f'<p class="meta">选项原分 {_number(signal["score"])} / 5 · {safe(signal["source"])}</p>')
                if f.get("rating_source")=="reviewed_adjustment":
                    signal_html += f'<p>老师复核后采用 {_number(f["rating"])} / 5。</p>'
                elif f.get("assessment_rating") is not None and f["assessment_rating"]!=signal["score"]:
                    signal_html += f'<p>草稿建议 {_number(f["assessment_rating"])} / 5，等待老师复核。</p>'
                empty = "该因子按所选情境选项对应的分值计入。"
        snippets = []
        for item in f.get("evidence") or []:
            if isinstance(item,dict):
                snippets.append(f'<li><a href="#q-{safe(item.get("question_id"))}">对应作答</a> · {safe(item.get("quote"))}</li>')
        gap_names = {"action":"本人行动","cost":"行为代价","experience":"相似经历"}
        gaps = [safe(gap_names.get(g,g)) for g in f.get("verification_gaps", [])]
        gap_note = '<p class="note">高分待核实 · '+ '、'.join(gaps)+'</p>' if gaps else ''
        attribution = f'<p class="meta">{safe(SOURCES.get(f.get("source"),"来源未登记"))} · {safe(f.get("author"))}</p>' if f.get("reason") else ""
        if not snippets:
            snippets = [f'<li><a href="#q-{safe(q["id"])}">{safe(q["id"])} · 查看题目与作答记录</a></li>'
                        for q in matching if q.get("kind") == "text"]
        shown_score = "待评" if f.get("rating") is None else _number(f["rating"]) + " / 5"
        parts.append(f'<details id="factor-{safe(f["factor_id"])}"><summary>{safe(f["name"])} <small>{coverage+" · " if coverage else ""}{shown_score}'
                     f'{" · 高分待核实" if gaps else ""}</small></summary>'
                     f'{signal_html}<p>{safe(f.get("reason"),empty)}</p>{attribution}{gap_note}<ul class="list">{"".join(snippets)}</ul></details>')
    return ''.join(parts)


def _source_facts(resume):
    rows = []
    for f in resume.get("facts") or []:
        rows.append(f'<tr><td>{safe(f.get("value"))}</td><td>{safe(f.get("source"))}</td><td>{safe(f.get("quote"))}</td></tr>')
    changes = ''.join(f'<li>{safe(a.get("reviewer"))} · {safe(a.get("reason"))}</li>' for a in resume.get("corrections",[]))
    if not rows and not changes:
        return ""
    return ('<details><summary>简历事实与来源</summary><div class="table-wrap"><table><thead><tr><th>识别结果</th>'
            '<th>原文位置</th><th>原文摘录</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'
            + ('<h4>档案更正</h4><ul class="list">'+changes+'</ul>' if changes else '') + '</details>')


def _teacher_summary(report):
    teacher = report.get("teacher_assessment") or {}
    if not teacher.get("factors"):
        return ""
    ratings = {f["factor_id"]:f for f in report.get("factors", [])}
    rows = []
    for tf in teacher["factors"]:
        f = ratings.get(tf["factor_id"], {})
        rows.append(f'<tr><td>{safe(f.get("name") or tf["factor_id"])}</td><td>{_number(tf.get("rating"))}</td>'
                    f'<td>{_number(f.get("rating"))}</td><td>{safe(tf.get("reason"))}</td></tr>')
    return ('<details><summary>老师独立评分与分析草稿</summary><p class="meta">'+safe(teacher.get("reviewer"))+
            '</p><div class="table-wrap"><table><thead><tr><th>因子</th><th>老师独立评价</th><th>报告评级</th><th>老师判断依据</th></tr></thead><tbody>'
            +''.join(rows)+'</tbody></table></div></details>')


def _model_rounds(report, model):
    nodes = (report.get("auto_scoring") or {}).get("nodes") or {}
    evidence = model.get("nodes") or {}
    rows = []
    for name in dict.fromkeys([*nodes, *evidence]):
        if not (name.startswith("question:") or name == "summary"):
            continue
        progress = nodes.get(name) or {}
        meta = progress.get("provenance") or evidence.get(name) or {}
        status = {"succeeded":"已完成", "failed":"未完成，成功评分已保留"}.get(progress.get("status"), "已保存")
        if meta.get("status") == "skipped_no_text":
            status = "缺少原话，未调用模型"
        if progress.get("cached"):
            status += " · 复用相同证据的已完成结果"
        label = "经历与综合意见汇总" if name == "summary" else "问答 " + name.removeprefix("question:")
        attempts = meta.get("attempt_count")
        requests = [a.get("request_id") for a in meta.get("attempts", []) if a.get("request_id")]
        if not requests and meta.get("request_id"):
            requests = [meta["request_id"]]
        rows.append(f'<tr><td>{safe(label)}</td><td>{safe(status)}<br>{safe(progress.get("error"), "")}</td>'
                    f'<td>{safe(meta.get("returned_model") or meta.get("model"))}'
                    f'{(" · " + str(attempts) + " 次请求") if attempts else ""}'
                    f'<br>{safe("；".join(requests))}</td></tr>')
    if not rows:
        return ""
    return ('<div class="fact"><dt>逐题调用与失败重试记录</dt><dd>每题独立保存，全部题评完成后再汇总。'
            '重试复用未改变证据的成功结果；汇总不改写因子分。<div class="table-wrap"><table>'
            '<thead><tr><th>步骤</th><th>状态</th><th>模型与请求编号</th></tr></thead><tbody>' + ''.join(rows) + '</tbody></table></div></dd></div>')


def _term_basis(factor):
    labels = {"supported":"有原话支持", "negated":"否定表达", "hypothetical":"计划或假设", "other_person":"他人行为",
              "slogan":"仅口号", "unclear":"待核实", "confirmed_reverse":"本人实际反向行为", "neutral":"中性例外", "corrected":"已改变的旧做法"}
    rows = []
    for kind, items in (factor.get("term_review") or {}).items():
        for item in items:
            quotes = ''.join(f'<blockquote class="quote"><a href="#q-{safe(r.get("question_id"))}">对应原话</a> · {safe(r.get("quote"))}</blockquote>' for r in item.get("evidence", []))
            rows.append(f'<tr><td>{"正向" if kind == "positive" else "风险"}线索：{safe(item.get("term"))}<br>{safe(item.get("source"))}</td>'
                        f'<td>{safe(labels.get(item.get("status"),item.get("status")))}</td><td>{safe(item.get("reason"))}{quotes}</td></tr>')
    decision = factor.get("policy_result") or {}
    changes = ''.join(f'<p>{safe(a.get("source"))}：{safe(a.get("reason"))}</p>' for a in decision.get("adjustments", []))
    if not rows and not decision:
        return ""
    return (f'<div class="fact"><dt>{safe(factor.get("factor_id"))} · 词义核对与规则执行</dt><dd>'
            f'模型建议 {_number(decision.get("proposed_rating"))}/5；规则执行后 {_number(decision.get("accepted_rating"))}/5。'
            '词语命中本身不扣分；实际反向行为须有原话和对应指标。'+changes+
            ('<div class="table-wrap"><table><thead><tr><th>线索与来源</th><th>语境判断</th><th>依据</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>' if rows else '')+'</dd></div>')


def _score_basis(report, missing_oral_text=""):
    draft = report.get("draft") or {}
    raw = report.get("basic_info_breakdown") or {}
    provenance = draft.get("provenance") or {}
    parts = []
    for key,title,explanation in [
        (None,"基本信息",raw.get("policy")),
        (None,"英语得分依据",raw.get("english_reason")),
        ("experience_score","基本情况",missing_oral_text or draft.get("experience_reason")),
        ("operation_score","面试整体",draft.get("operation_reason")),
        ("summary","综合意见",None), ("next_steps","下一轮建议",None)]:
        actor = provenance.get(key) or {}
        if not explanation and not actor:
            continue
        author = SOURCES.get(actor.get("source"),"") + " " + (actor.get("author") or "")
        parts.append(f'<div class="fact"><dt>{safe(title)} · {safe(author.strip(),"工作簿规则")}</dt>'
                     f'<dd>{safe(explanation,"见报告综合意见")}</dd></div>')
    ref = draft.get("experience_evidence")
    if ref:
        parts.append(f'<div class="fact"><dt>经历评分引文</dt><dd><a href="#q-{safe(ref["question_id"])}">对应问答</a>'
                     f' · {safe(ref["quote"])}</dd></div>')
    model = draft.get("model_provenance") or {}
    rounds = _model_rounds(report, model)
    if rounds:
        parts.append(rounds)
    if model:
        parts.append(f'<div class="fact"><dt>百炼模型调用记录</dt><dd>模型 {safe(model.get("returned_model") or model.get("model"))}'
                     f' · 状态 {safe(model.get("status"))} · 提示规则 {safe(model.get("prompt_version"))}'
                     f'<br>请求编号 {safe(model.get("request_id"))} · 引文校验 {safe(model.get("validation"))}'
                     f'<br>模型分析正式问答原话；单选、操作分和加权总分由程序计算。</dd></div>')
        for factor in model.get("factor_details", []):
            judgments = []
            relevance = factor.get("answer_relevance") or {}
            if relevance:
                response_labels = {"substantive":"回应了题目", "off_topic":"回答完全离题", "explicit_non_answer":"明确未作答", "unclear":"原话语义待核实"}
                quotes = ''.join(f'<blockquote class="quote"><a href="#q-{safe(r.get("question_id"))}">对应原话</a> · {safe(r.get("quote"))}</blockquote>' for r in relevance.get("evidence", []))
                note = "本次题目按已确认的演示规则计最低档 1 分；能力指标缺少证据的部分保持如实记录。" if relevance.get("status") in ("off_topic", "explicit_non_answer") else ""
                parts.append(f'<div class="fact"><dt>{safe(factor.get("factor_id"))} · 作答回应情况：{safe(response_labels.get(relevance.get("status"), relevance.get("status")))}</dt>'
                             f'<dd>{safe(relevance.get("reason"))} {safe(note, "")}{quotes}'
                             f'<small>{safe(relevance.get("source"), "")} · {safe(relevance.get("policy_version"), "")}</small></dd></div>')
            labels = {"demonstrated":"有证据支持", "partial":"部分支持", "not_met":"存在相反证据", "insufficient_evidence":"证据不足"}
            for item in factor.get("judgments", []):
                refs = ''.join(f'<blockquote class="quote"><a href="#q-{safe(r.get("question_id"))}">{safe(r.get("question_id"))}</a> · {safe(r.get("quote"))}</blockquote>' for r in item.get("evidence", []))
                judgments.append(f'<tr><td>{safe(item.get("criterion"))}</td><td>{safe(labels.get(item.get("status"),item.get("status")))}</td><td>{safe(item.get("reason"))}{refs}</td></tr>')
            parts.append(f'<div class="fact"><dt>{safe(factor.get("factor_id"))} · Qwen 逐项评分点核对</dt><dd><div class="table-wrap"><table>'
                         '<thead><tr><th>题库指标</th><th>判断</th><th>理由与原话</th></tr></thead><tbody>'+''.join(judgments)+'</tbody></table></div></dd></div>')
            parts.append(_term_basis(factor))
    if any(q.get("capture") != "not_applicable" for q in report.get("questions",[])):
        parts.append('<div class="fact"><dt>多模态表现</dt><dd><a href="#multimodal-evidence">'
                     '查看每题的点位、声音、状态占比及整场评分依据</a></dd></div>')
    return '<details><summary>评分依据与撰写来源</summary><dl>'+''.join(parts)+'</dl></details>' if parts else ""




def _media_summary(answers):
    media = [a for a in answers.values() if a.get("media")]
    if not media:
        return ""
    durations = [a["media"].get("duration_seconds") for a in media]
    timing = f"（共约 {round(sum(durations))} 秒）" if all(isinstance(v, (float, int)) for v in durations) else ""
    transcripts = sum(bool((a.get("transcript") or "").strip()) for a in media)
    windows = sum(len((a.get("processing") or {}).get("observations", [])) for a in media)
    segments = sum(len((a.get("observations") or {}).get("segments", [])) for a in media)
    estimated = sum(len((a.get("state_estimate") or {}).get("segments",[])) for a in media if not a.get("observations"))
    audited = sum(bool((a.get("processing") or {}).get("v1",{}).get("modules")) for a in media)
    extraction = (f"有 {audited} 段原片已返回 V1 声画分析记录。" if audited else
                  (f"已从 {windows} 个短片段提取声画特征。" if windows else "原片尚无声画特征提取结果。"))
    return (f"已保存 {len(media)} 段原片{timing}，其中 {transcripts} 段有转写。"
            f"{extraction}人工标注 {segments} 个时段，本地自动估计 {estimated} 个时段。")


def _resume_view(resume):
    edu = " · ".join(x for x in (resume.get("education"), resume.get("education_status")) if x)
    education = " · ".join(x for x in (resume.get("school"), edu, resume.get("school_tier")) if x)
    english = [e["exam"] + (" " + _number(e["score"]) if e.get("score") is not None else "")
               for e in resume.get("english", []) if e.get("exam")]
    if not english and resume.get("english_none"):
        english = ["无"]
    internships = [e["title"] for e in resume.get("internships", []) if e.get("title")]
    campus = [e["title"] for e in resume.get("experiences", []) if e.get("kind")=="校园" and e.get("title")]
    honors, skills = resume.get("honors", []), resume.get("skills", [])
    attrs = []
    for label, values in [("教育", [education]), ("实习", internships or [resume.get("internship")]),
                          ("校园经历", campus), ("荣誉奖励", honors), ("语言", english), ("专业技能", skills)]:
        available = [v for v in values if v and v!="未提供"]
        if available:
            body = "<br>".join(safe(v) for v in available[:3])
            attrs.append(f'<div class="attr"><div class="k">{label}</div><div class="v">{body}</div></div>')
    tags = [resume.get("school"), edu, resume.get("school_tier")]
    if internships:
        tags.append(f"实习 {len(internships)} 段")
    tags.extend(english)
    if campus:
        tags.append(campus[0].split()[0])
    tags_html = "".join(f'<span class="mini-tag">{safe(v)}</span>' for v in dict.fromkeys(tags) if v and v!="未提供")
    badge_values = []
    if campus:
        badge_values.append(("🎓",campus[0].split()[0]))
    if honors:
        badge_values.append(("🏅",honors[0].split()[-1]))
    badge_values.extend(("◆", value) for value in skills if value and len(value)<=12 and "英语" not in value)
    badge_values.extend(("◇", v) for v in english)
    badges = "".join(f'<div class="badge-tag p{i}">{icon} {safe(value)}</div>'
                     for i,(icon,value) in enumerate(list(dict.fromkeys(badge_values))[:5], start=1))
    return tags_html, "".join(attrs) or '<div class="attr">待补充简历资料</div>', badges


def _display_contribution(item):
    for key in ("contribution", "partial_contribution", "estimated_contribution"):
        if item.get(key) is not None:
            return item[key]
    return None


def _headline(report):
    summary = score_summary(report)
    kind, value = summary["score_kind"], summary["display_score"]
    if kind == "total":
        return SCORE_LABELS[kind], _number(value) + " / 100", "五项加权"
    if kind == "estimate":
        coverage = _number(report["session"]["coverage"], "%", 100)
        return SCORE_LABELS[kind], _number(value) + " / 100", f'多模态有效时长覆盖 {coverage}'
    if value is None:
        return "综合得分", "待评 / 100", "尚无可计入的分项"
    note = "总分待补齐分项"
    if report["components"]["multimodal"].get("estimated_contribution") is not None:
        note += " · 含多模态估算"
    return SCORE_LABELS[kind], _number(value) + " / 100", note


def _score_cards(report, assessed, headline):
    components = report["components"]
    info = components["basic_info"]
    potential = components["potential"]
    points = _display_contribution(potential)
    factor_score = potential["score"]
    factor_label = "潜力八因子"
    if factor_score is None and assessed:
        rated = [f["rating"] for f in report["factors"] if f.get("rating") is not None and not f.get("verification_gaps")]
        factor_score = sum(rated) / (5 * len(report["factors"])) * 100
        factor_label = "八因子当前累计"
    value = ("待评" if factor_score is None else _number(factor_score)) + " / 100"
    factor_note = f"已有 {assessed}/8 项得分"
    if assessed < 8:
        factor_note += f" · 还缺 {8-assessed} 项，非最终成绩"
    if points is not None:
        factor_note += f' · 加权贡献 {_number(points)}/{_number(potential["weight"], scale=100)} 分'
    breakdown = report.get("basic_info_breakdown", {})
    raw = breakdown.get("raw", {})
    if info["score"] is not None:
        basic_note = f'权重 {_number(info["weight"], "%", 100)} · 计入总分 {_number(info["contribution"])} 分'
        if raw and all(v is not None for v in raw.values()):
            basic_note = f'原分 {_number(sum(raw.values()))}/{_number(breakdown["max"])} · ' + basic_note
    else:
        basic_note = " · ".join(f'{label} {_number(raw[k])}/5' if raw.get(k) is not None else f'{label}待核实'
                               for k,label in (("education","学历院校"),("internship","实习"),("english","英语")))
    def card(label, number, note, style):
        return (f'<div class="sc"><div class="lbl">{safe(label)}</div><div class="num {style}">{safe(number)}</div>'
                f'<div class="w">{safe(note)}</div></div>')

    basic = card("简历基本信息分", (_number(info["score"]) if info["score"] is not None else "待核实") + " / 100", basic_note, "a")
    factor = (f'<a class="sc factor-lead{" incomplete" if assessed < 8 else ""}" href="#factor-overview"><div class="lbl">{factor_label}</div>'
              f'<div class="num b">{safe(value)}</div><div class="w">{safe(factor_note)}<br>查看八因子逐项得分 ↓</div></a>')
    return basic + factor + card(*headline, "c")


def _resume_internship_summary(resume):
    internships = resume.get("internships") or []
    if resume.get("internship") == "无":
        count = "0 段"
    elif internships and all(item.get("dates") for item in internships):
        count = f"{len(internships)} 段"
    else:
        count = "段数待核实"
    records = ""
    if internships and resume.get("internship") != "无":
        records = '<details><summary>查看简历实习记录</summary><ul>' + "".join(
            f'<li><span>{safe(item.get("title"))}</span><small>{safe(item.get("dates"), "时间未明确")}</small></li>'
            for item in internships) + '</ul></details>'
    return ('<section class="resume-internship-summary" aria-labelledby="internship-summary-title">'
            '<div><h3 id="internship-summary-title">简历实习段数</h3><span>资料展示 · 不单独计分</span></div>'
            f'<strong>{count}</strong>{records}</section>')


def _introduction_view(report):
    introduction = report.get("introduction") or {}
    if introduction.get("status") != "saved":
        return ""
    contents = ""
    if introduction.get("text"):
        contents += f'<blockquote>{safe(introduction["text"])}</blockquote>'
    media = introduction.get("media") or {}
    if media:
        url = media.get("export_path") or (f'/v2/api/sessions/{quote(str(report["id"]), safe="")}/introduction/media'
                                           f'?sha256={quote(str(media.get("sha256", "")), safe="")}')
        element = "audio" if str(media.get("mime", "")).startswith("audio/") else "video"
        contents += f'<{element} controls preload="none" src="{safe(url)}"></{element}>'
    return ('<details class="introduction-summary" id="warmup-introduction"><summary>暖场自我介绍 · 不计分</summary>'
            + contents + '</details>')


def _factor_overview(report, assessed, signals):
    factors = report.get("factors", [])
    plan = report.get("plan", [])
    cards = []
    for f in factors:
        fid = f["factor_id"]
        rating = f.get("rating")
        valid = rating is not None and not f.get("verification_gaps")
        if valid:
            if f.get("rating_source") == "reviewed_adjustment":
                source = "教师调整 · 已复核"
            elif f.get("rating_source") == "choice_key":
                source = "情境单选 · 选项分"
            else:
                source = "问答 · " + SOURCES.get(f.get("source"), "原话评分")
            number = f'{_number(rating)}<small> / 5</small>'
        else:
            number = "待核实" if rating is not None else "待评"
            main_questions = [q for q in plan if q.get("factor_id") == fid and not
                              (q.get("parent_id") or q.get("parent_question_id") or q.get("condition_parent"))]
            question = next((q for q in main_questions if q.get("kind") == "text"), next(iter(main_questions), {}))
            answer = report.get("answers", {}).get(question.get("id"), {})
            if rating is not None:
                source = f'草稿 {_number(rating)} / 5 · 待核证据'
            elif question.get("kind") == "text":
                source = "问答 · 待评分" if answer else "问答 · 待作答"
                qid = question.get("id")
                job = report.get("auto_scoring") or {}
                node = (job.get("nodes") or {}).get("question:" + qid, {}) if qid else {}
                if not answer:
                    number = "待作答"
                elif node.get("status") == "failed":
                    number, source = "分析失败", "Qwen 文本评分未完成 · 细则可查看原因"
                elif job.get("status") in ("queued", "running"):
                    if answer.get("media") and not (answer.get("text") or answer.get("transcript")):
                        number, source = "处理中", "正在提取录音与转写 · 完成后自动评分"
                    else:
                        number, source = "分析中", "Qwen 正在核对作答原话"
                elif answer.get("media") and not (answer.get("text") or answer.get("transcript")):
                    number, source = "缺少原话", "未取得有效转写 · 请核对原片"
                elif f.get("reason"):
                    number, source = "证据不足", "Qwen 已分析 · 未形成有效评级"
            elif question:
                source = "情境单选 · 待作答" if not answer else "情境单选 · 待评分"
            else:
                source = "本场尚无对应题目"
        style = " low" if valid and rating <= 2 else "" if valid else " pending"
        cards.append(f'<a class="factor-item{style}" href="#factor-{safe(fid)}" title="{safe(f.get("reason"),"查看本题评分依据")}">'
                     f'<span class="factor-name">{safe(f["name"])}</span><b>{number}</b>'
                     f'<span class="factor-source">{safe(source)}</span></a>')
    subtotal = ""
    if signals:
        raw = sum(f["choice_signal"]["score"] for f in signals)
        subtotal = f'<span>情境单选原分 <b>{_number(raw)} / {5 * len(signals)}</b></span>'
    return (f'<section id="factor-overview" aria-labelledby="factor-title"><div class="factor-heading">'
            f'<h3 id="factor-title">潜力八因子逐项得分</h3><div class="factor-tally">'
            f'{subtotal}<span>已有 <b>{assessed} / {len(factors)}</b> 项得分</span></div></div>'
            '<p class="factor-method">单选按题库选项计分；问答由 Qwen 对照原话与评分点分析。多模态表现单独计分。</p>'
            f'<div class="factor-grid">{"".join(cards)}</div></section>')


def _state_bars(proportions, missing="未分类"):
    bars = []
    for label, color in zip(STATE_POINTS, ("var(--ok)", "var(--info)", "var(--wn)", "#E88B7A", "var(--er)")):
        ratio = proportions[label] if proportions else None
        percent = _number(ratio, "%", 100) if ratio is not None else missing
        width = 100*ratio if ratio is not None else 0
        bars.append(f'<div class="bar"><div class="l1"><span>{safe(label)}</span><strong>{percent}</strong></div>'
                    f'<div class="bar-track"><div class="bar-fill" style="width:{width:.6f}%;background:{color}"></div></div></div>')
    return "".join(bars)


def _state_view(report):
    obs = report["session"]
    proportions = obs.get("proportions")
    answers = report.get("answers", {})
    media_summary = _media_summary(answers)
    hint = media_summary
    bars = _state_bars(proportions, "未分类" if media_summary else "无原片")
    if proportions:
        component = report["components"]["multimodal"]
        weight = component["weight"]
        score = obs["score"]
        coverage = obs.get("coverage")
        contribution = component["contribution"] if component["contribution"] is not None else component.get("estimated_contribution")
        scores = (f'<div class="session-mm-score"><b>{_number(score)} <small>/ 100</small></b>'
                  f'<span>折合 {_number(contribution)} / {_number(weight,scale=100)} 分'
                  + ('（有效时段估算）' if contribution is not None and obs["status"] != "complete" else '')
                  + '</span></div>')
        whole_shares = whole_state_proportions(obs)
        whole = ''
        if whole_shares is not None:
            parts = [f'{safe(label)} {_number(p,"%",100)}' for label,p in whole_shares.items()]
            whole = ('<details class="session-mm-whole"><summary>整场分布（含未分类）</summary><p>'
                     + " · ".join(parts) + '</p></details>')
        bars = (scores + f'<p class="session-mm-denominator"><b>已分类时段内分布</b><br>'
                f'分母 {_number(obs["valid_seconds"])} 秒 / 整场 {_number(obs.get("answer_seconds"))} 秒'
                f' · 覆盖 {_number(coverage,"%",100)}<br>以下五项合计 100%，不含未分类时段。</p>'
                + whole
                + bars)
        hint = (f'有效覆盖 {_number(coverage,"%",100)} · {safe(obs["band"])}档。'
                '整场按各题已分类秒数加权。')
        policy = obs.get("coverage_policy") or {}
        if policy and not obs.get("meets_coverage_threshold"):
            hint += (f'<br>仅展示观测分：每题及整场有效覆盖须分别达到 '
                     f'{_number(policy.get("min_question_coverage"),"%",100)} / '
                     f'{_number(policy.get("min_session_coverage"),"%",100)}，当前未达标，暂不计入综合分与排名。')
    elif media_summary:
        hint += " 计算占比需要先判定各段作答的状态，再按秒数汇总。"
    else:
        hint = "本场尚无可用于五态统计的答题录音录像。"
    if any(q.get("capture") != "not_applicable" for q in report.get("questions",[])):
        hint += '<br><a href="#multimodal-evidence">查看逐题点位、声音与判档依据</a>'
    return bars, hint.strip()


def _verdict_items(report, factors):
    strengths, concerns = [], []
    for f in factors:
        signal = f.get("choice_signal")
        score = f.get("rating")
        if score is None or f.get("verification_gaps"):
            continue
        if score < 4 and score > 2:
            continue
        if signal and f.get("rating_source")=="choice_key":
            detail = f'情境题选择“{safe(signal["answer_label"])}”。'
        else:
            detail = safe(f.get("reason") or "按已保存的作答证据评级。")
        text = f'<li><b>{safe(f["factor_id"])} {safe(f["name"])} {_number(score)} / 5</b>　{detail}</li>'
        (strengths if score>=4 else concerns).append(text)
    unresolved = [f["name"] for f in factors if f.get("rating") is None or f.get("verification_gaps")]
    if unresolved:
        concerns.append(f'<li><b>待补证据</b>　{safe("、".join(unresolved))}的有效评级尚未齐备。</li>')
    return ("".join(strengths) or "<li>尚无可引用的高分表现。</li>",
            "".join(concerns) or "<li>当前已保存的评分中尚无低分项或待复核缺口。</li>")


def _report_appendix(report, workbench):
    plan, factors = report.get("plan",[]), report.get("factors",[])
    answers = report.get("answers", {})
    oral_answers = [answers.get(q["id"], {}) for q in plan if q.get("kind") == "text"]
    missing_oral_text = ""
    if (report["components"]["experience"]["score"] is None and oral_answers
            and all(a.get("media") for a in oral_answers)
            and not any((a.get("text") or "").strip() or (a.get("transcript") or "").strip() for a in oral_answers)):
        missing_oral_text = f"已保存 {len(oral_answers)} 道问答原片，尚无可引用的作答文字或转写。需听核原片后按经历评分点给分。"
    raw = report.get("basic_info_breakdown", {}).get("raw", {})
    missing_basics = [label for key, label in (("education", "学历院校"), ("english", "英语"), ("internship", "实习"))
                      if raw.get(key) is None]
    unrated = [f["name"] for f in factors if f.get("rating") is None or f.get("verification_gaps")]
    observation = report["session"]
    coverage = (f'已分类 {_number(observation["valid_seconds"])} 秒，覆盖 {_number(observation["coverage"],"%",100)}。'
                if observation.get("valid_seconds") else "尚无已判定状态的作答时段。")
    pending = {
        "basic_info": "待核实" + "、".join(missing_basics) + "的简历依据。",
        "experience": missing_oral_text or "需引用当场讲述经历的作答原话，并按经历描述标准评分。",
        "potential": "待取得有效评级：" + "、".join(unrated) + "。",
        "operation": "需依据面试操作记录核对遗漏和错误，并记录流程操作分。",
        "multimodal": coverage,
    }
    rows = []
    for key,label in COMPONENT_NAMES.items():
        item = report["components"][key]
        if item["score"] is None:
            score = "待评"
        else:
            score = (_observation_score(observation) if key=="multimodal" else _number(item["score"])) + " / 100"
        status = "可计入" if item["contribution"] is not None else pending[key]
        points = _display_contribution(item)
        contribution = ("待评" if points is None else _number(points)) + " / " + _number(item["weight"], scale=100)
        if item.get("partial_contribution") is not None:
            score = f'{_number(item["rated_raw"])} / {5*item["rated_count"]}（已评因子原分）'
            status = f'已评 {item["rated_count"]}/8 项，每因子最高贡献 7.5 分。' + pending[key]
        elif item.get("estimated_contribution") is not None:
            contribution += "（估算）"
            status = "每题及整场有效覆盖均达到本场门槛，按有效时长估算。" + coverage
        elif key=="multimodal" and item["score"] is not None and points is None:
            policy = observation.get("coverage_policy") or {}
            status = coverage + (f'未达到每题 {_number(policy.get("min_question_coverage"),"%",100)}、整场 '
                       f'{_number(policy.get("min_session_coverage"),"%",100)} 的有效覆盖门槛，不计入综合分与排名。'
                       if policy else "仍有应采集题缺有效分类，整场贡献待评。")
        if key == "basic_info" and raw:
            details = [f'{name} {_number(raw[part])}/5' for part, name in (("education","学历院校"),("english","英语"),("internship","实习"))
                       if raw.get(part) is not None]
            status = " · ".join([status, *details])
        rows.append(f'<tr><th scope="row">{safe(label)}</th><td>{score}</td>'
                    f'<td>{_number(item["weight"],"%",100)}</td><td>{contribution}</td><td>{safe(status)}</td></tr>')
    weights = ('<h3>五项加权明细</h3><table><thead><tr><th>项目</th><th>评分结果</th>'
               '<th>权重</th><th>当前贡献 / 分项满分</th><th>评分进度</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table>')
    return ('<details id="report-appendix"><summary>查看评分细则、逐题作答与原片</summary>'+weights+
            render_media_evidence(report)+_score_basis(report, missing_oral_text)+_source_facts(report.get("resume",{}))+
            '<h3>八因子评价依据</h3>'+_factors(factors,plan,report.get("interview_mode")=="short_5_3")+
            '<section id="report-questions"><h3>逐题作答与原片</h3>'+_question_cards(report,workbench)+'</section>'+
            _teacher_summary(report)+'</details>')


def personal_html(report, *, workbench=False):
    """Render one candidate's version-pinned V2 snapshot into the supplied offline layout."""
    template = (WEB / "personal-template.html").read_text(encoding="utf-8")
    resume, plan, answers = report.get("resume",{}), report.get("plan",[]), report.get("answers",{})
    name, factors, components = report["candidate"], report["factors"], report["components"]
    signals = [f for f in factors if f.get("choice_signal")]
    assessed = sum(f.get("rating") is not None and not f.get("verification_gaps") for f in factors)
    review = report.get("review") or {}
    expected = report.get("expected_questions", len(plan))
    done = sum(bool(a.get("media") or a.get("text") or a.get("value") is not None) for a in answers.values())
    tags, attrs, badges = _resume_view(resume)
    bars, hint = _state_view(report)
    strengths, concerns = _verdict_items(report, factors)
    gender = resume.get("gender")
    chart_axes = (("education","学历与院校"),("internship","实习经历"),("english","英语水平"))
    raw = (report.get("basic_info_breakdown") or {}).get("raw",{})
    charts = {"resume":{"labels":[label for _,label in chart_axes],"data":[raw.get(key) for key,_ in chart_axes]},
              "factors":{"labels":[f["name"] for f in factors],
                         "data":[None if f.get("verification_gaps") else f.get("rating") for f in factors]},
              "weights":{"labels":[f'{label} {_number(components[key]["weight"],"%",100)}' for key,label in COMPONENT_NAMES.items()],
                         "data":[round(components[key]["weight"]*100, 8) for key in COMPONENT_NAMES]}}
    # JSON inside a script is parsed by HTML before JavaScript; escape '<' as well as JS strings.
    chart_data = _script_data(charts)
    media_seconds = report["session"].get("answer_seconds")
    timing = f' · 原片时长：{_number(media_seconds)} 秒' if media_seconds else ""
    status = _report_status(report)
    next_steps = report.get("next_steps") or []
    verdict = f'<strong>综合意见：</strong>{safe(report.get("summary"),"本场综合意见待教师复核。")}'
    if next_steps:
        verdict += '<br><strong>下一轮重点：</strong>' + '；'.join(safe(v) for v in next_steps)
    elif signals:
        low_signals = [f["name"] for f in signals if f["choice_signal"]["score"] <= 2]
        if low_signals:
            verdict += f'<br><strong>建议核实：</strong>围绕{safe("、".join(low_signals))}，请候选人举例说明当时的做法和结果。'
    stamp = f'报告编号 {safe(report["id"][:12])} · 版本 {safe(report["version"])} · {status}'
    headline = _headline(report)
    mode = (report.get("auto_scoring") or {}).get("status")
    badge = {"queued":"已交卷 · 等待自动评分","running":"自动评分中","partial":"部分评分待核实","error":"自动评分未完成",
             "manual_review_required":"补正已保存 · 待老师复核","superseded":"资料已更新 · 待重新分析"}.get(mode,headline[2])
    if any(f.get("source") in ("local_auto", "bailian_auto") for f in (report.get("draft") or {}).get("factors", [])) and not review.get("reviewer"):
        badge = "自动草稿 · " + badge
    audited = any((a.get("processing") or {}).get("v1") for a in answers.values())
    audit_styles = (V1_SITE/"css/multimodal-view.css").read_text(encoding="utf-8") if audited else ""
    audit_script = (V1_SITE/"js/multimodal-view.js").read_text(encoding="utf-8") if audited else ""
    slots = {"TITLE":safe(name)+" · 面试评价报告", "STAMP":stamp, "AVATAR":safe(name[:1]), "NAME":safe(name),
             "DEMOGRAPHICS":f'· {safe(gender)}' if gender and gender!="未提供" else "",
             "META":f'应聘：通用管培生 · {safe(report.get("batch_name"))} · 已作答 {done} / {expected} 题{timing}',
             "TAGS":tags, "TOTAL":safe(headline[1]), "TOTAL_LABEL":safe(headline[0]), "BADGE":safe(badge),
             "AUDIT_STYLE":audit_styles, "AUDIT_SCRIPT":audit_script,
             "PLAYER_SCRIPT":(WEB/"report-player.js").read_text(encoding="utf-8") if any(a.get("media") for a in answers.values()) else "",
             "SCORE_CARDS":_score_cards(report,assessed,headline), "RESUME_CHART_TITLE":"简历基本信息画像",
             "WEIGHT_CHART_TITLE":"评分权重（本次评估口径）", "RESUME_ATTRS":attrs,
             "CHARACTER_TAGS":badges, "CHARACTER_COLOR":{"男":"male", "女":"female"}.get(gender,"unspecified"),
             "STATE_BARS":bars, "STATE_HINT":hint, "FACTOR_OVERVIEW":_factor_overview(report,assessed,signals),
             "STRENGTHS":strengths, "CONCERNS":concerns, "VERDICT":verdict,
             "FOOT_BASIS":f'评估依据：通用管培生题库 · {safe(report.get("bank_version"))}',
             "FOOT_REVIEW":f'生成：真才实鉴 V2 · 复核：{safe(review.get("reviewer"),"待复核")}',
             "DETAILS":_report_appendix(report,workbench), "CHART_DATA":chart_data,
             "INTRODUCTION":_introduction_view(report), "INTERNSHIP_SUMMARY":_resume_internship_summary(resume),
             "MODE":"workbench" if workbench else "standalone", "TIP":"📄 本机综合报告" if workbench else "📄 真才实鉴 · 个人报告"}
    return re.sub(r"@@([A-Z_]+)@@", lambda match: slots[match.group(1)], template)


def batch_html(data):
    stats, members = data["stats"], data.get("candidates", [])
    head = f'<header class="heading"><div><h1>{safe(data["name"])} · 批次总分排序报告</h1><p class="meta">通用管培生 · {len(members)} 位候选人</p></div></header>'
    counts = ''.join(_metric(label, safe(stats.get(key,0)), "人") for key,label in
                     [("count","批次人数"),("interview_count","已建面试"),("finished_count","已结束面试"),("reviewed_count","已复核"),("ranked_count","可排序人数")])
    charts = []
    for key,label in [("education","最高学历"),("school_tier","院校情况"),("english","英语水平"),("gender","性别构成")]:
        dist = stats.get(key) or {}
        denom = stats.get("count") or 0
        bars = [(k, v / denom if denom else 0, f'{v} 人') for k,v in dist.items()]
        charts.append(f'<article class="card"><h3>{label}</h3>{_bars(bars) if bars else "待收集"}</article>')
    ranked_rows, pending_rows = [], []
    wall = []
    drawers = []
    factor_values = {}
    for c in members:
        resume, cid = c.get("resume") or {}, quote(str(c["id"]),safe="")
        link = f'#member-{cid}'
        actions = ''
        if c.get("session_id"):
            sid = quote(c["session_id"],safe="")
            version = quote(str(c.get("version","")),safe="")
            actions = f'<a href="/v2/api/sessions/{sid}/report.html?version={version}">查看本机个人报告 · 版本 {safe(c.get("version"))}</a>'
            if c.get("export_path"):
                actions = f'<a href="{safe(c["export_path"])}">打开个人报告 · 版本 {safe(c.get("version"))}</a>'
        for factor in c.get("factors") or []:
            values = factor_values.setdefault(factor["name"],[])
            if factor.get("rating") is not None and not factor.get("verification_gaps"):
                values.append(factor["rating"])
        summary = score_summary(c)
        score = c.get("display_score", summary["display_score"])
        kind = c.get("score_kind", summary["score_kind"])
        score_text = _number(score) + " / 100" if score is not None else "待评分"
        progress = (f'{c.get("answered_questions",0)} / {c["expected_questions"]} 题'
                    if c.get("expected_questions") is not None else "待建面试")
        row = (f'<tr data-candidate="{cid}"><td>{_number(c.get("rank")) if c.get("rank") is not None else "待评"}</td>'
               f'<td><a href="{link}">{safe(c["name"])}</a></td><td><strong>{score_text}</strong></td>'
               f'<td>{SCORE_LABELS[kind]}</td><td>{safe(progress)} · 因子 {c.get("rated_factor_count",0)} / 8 项<br>{_report_status(c)}</td>'
               f'<td>{actions or "待建立面试"}</td></tr>')
        (ranked_rows if c.get("rank") is not None else pending_rows).append(row)
        wall.append(f'<a class="tile" href="{link}"><strong>{safe(c["name"])}</strong><span class="meta">{safe(resume.get("school"))} · {safe(resume.get("education"))}</span></a>')
        drawers.append(f'<details id="member-{cid}"><summary>{safe(c["name"])} · 简历与进度</summary>'
                       f'<dl>{_resume_facts(resume)}</dl><p>{_report_status(c)} · {SCORE_LABELS[kind]} {score_text}</p>{actions}</details>')
    table_head = '<div class="table-wrap"><table><thead><tr><th>名次</th><th>候选人</th><th>分数 / 满分</th><th>计分口径</th><th>完成进度</th><th>个人报告</th></tr></thead><tbody>'
    ranking = (table_head + ''.join(ranked_rows) + '</tbody></table></div>' if ranked_rows
               else '<p class="empty">本批次暂没有评分齐全的面试，完成后将在这里按总分排列。</p>')
    ranking += '<p class="note">与个人报告右上角的分数一致，按显示到两位小数的分数降序，同分并列。综合估分标明口径，复核状态逐人列示。</p>'
    pending = (table_head + ''.join(pending_rows) + '</tbody></table></div>' if pending_rows
               else '<p>本批次所有人的分项评分已齐全。</p>')
    means = [{"name":k,"rating":sum(v)/len(v) if v else None} for k,v in factor_values.items()]
    comparisons = ''.join(f'<li>{safe(k)} · {_number(sum(v)/len(v) if v else None)} / 5 · 有效 {len(v)} 人</li>' for k,v in factor_values.items())
    compare = (f'<div class="split"><div class="card">{_radar(means)}</div><div class="card"><h3>分项均值与有效人数</h3>'
               f'<ul class="list">{comparisons}</ul></div></div>') if means else '<p class="empty">等待有证据的因子评级。</p>'
    body = (head + _section("批次概况",'<div class="metrics">'+counts+'</div>')
            + _section("总分排序",ranking) + _section("待完成与待评分",pending)
            + _section("同期画像分布",'<div class="grid">'+''.join(charts)+'</div><p class="note">分母为全批次人数。英语考试可多选。</p>')
            + _section("能力雷达 · 同期水平",compare)
            + _section("人物画像墙",'<div class="wall">'+''.join(wall)+'</div>')
            + _section("人员明细",''.join(drawers) or '<p>待导入简历。</p>'))
    return _doc(data["name"]+" · 同期报告",body + '''<script>
    function showMember(){const e=document.getElementById(location.hash.slice(1));if(e&&e.tagName==='DETAILS'){e.open=true;e.scrollIntoView()}}
    window.addEventListener('hashchange',showMember);showMember()</script>''')
