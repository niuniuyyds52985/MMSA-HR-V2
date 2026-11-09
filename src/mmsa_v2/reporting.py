"""Render a standalone HTML snapshot of a V2 simulation score report."""

import argparse
from decimal import Decimal, ROUND_HALF_UP
from html import escape
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .scoring import BAND_WIDTH, STATE_POINTS, calculate_report


COMPONENT_NAMES = {
    "basic_info": "基本信息",
    "experience": "基本情况",
    "potential": "潜力八因子",
    "operation": "面试整体",
    "multimodal": "多模态表现",
}

STYLES = """
:root {
  color-scheme: light;
  --ink: #203244; --muted: #506273; --line: #d8e1e8; --blue: #275f92;
  --back: #f4f7fa; --tint: #edf4fa;
  font: 16px/1.55 "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
}
* {box-sizing: border-box}
body {margin: 0; color: var(--ink); background: var(--back)}
main {width: min(1060px, 100%); margin: 32px auto; padding: clamp(18px, 4vw, 44px);
  border: 1px solid var(--line); border-radius: 18px; background: white}
h1, h2, h3, p, dl, dd {margin: 0}
h1 {font-size: clamp(1.55rem, 3vw, 2.15rem); line-height: 1.25; overflow-wrap: anywhere}
h2 {font-size: 1.22rem} h3 {font-size: .96rem}
.mast {display: flex; justify-content: space-between; align-items: baseline; gap: 14px;
  border-bottom: 2px solid var(--tint); padding-bottom: 12px; margin-bottom: 22px}
.brand {color: var(--blue); font-weight: 700}
.caption, .meta, dt, .unit {color: var(--muted); font-size: .86rem}
.hero {display: flex; justify-content: space-between; align-items: center; gap: 20px;
  background: #fff8ee; border: 1px solid #eadbc5; border-radius: 13px; padding: 20px}
.hero .caption {margin-bottom: 5px}
.total {flex: 0 0 auto; text-align: right}
.total strong {font-size: 2.05rem; line-height: 1.2; color: #7a4d18}
.total .unit, .summary-score .unit {margin-left: 4px}
section {margin-top: 28px}
.section-title {display: flex; align-items: baseline; gap: 14px; margin-bottom: 12px}
.section-title::after {content: ""; flex: 1; border-top: 1px solid var(--line)}
.components {display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: 10px}
.component {border: 1px solid var(--line); border-radius: 10px; padding: 14px; background: #fbfcfd}
.component.feature {border-color: #a4c8d5; background: #f0f7f8}
.component .value {display: block; font-size: 1.5rem; font-weight: 650; margin: 4px 0}
.process {border: 1px solid var(--line); border-radius: 12px; padding: 18px;
  display: grid; grid-template-columns: minmax(150px, .8fr) minmax(220px, 1.5fr); gap: 22px}
.summary-score {font-size: 1.65rem; font-weight: 650}
.facts {display: grid; grid-template-columns: max-content 1fr; gap: 4px 14px; align-items: baseline;
  margin-top: 10px; font-size: .92rem}
.bars {display: grid; align-content: center; gap: 8px; margin: 0; padding: 0; list-style: none}
.bars li {display: grid; grid-template-columns: 6.2em minmax(40px, 1fr) 4.8em; align-items: center; gap: 8px; font-size: .92rem}
.bars li span:last-child {text-align: right; font-variant-numeric: tabular-nums}
progress, .no-bar {width: 100%; height: 9px; overflow: hidden; border: 0; border-radius: 9px; background-color: #e9eef2}
progress {appearance: none; color: var(--color)}
progress::-webkit-progress-bar {border-radius: 9px; background-color: #e9eef2}
progress::-webkit-progress-value {border-radius: 9px; background-color: var(--color)}
progress::-moz-progress-bar {border-radius: 9px; background-color: var(--color)}
.bars li:nth-child(1) {--color: #27876c} .bars li:nth-child(2) {--color: #3e829c}
.bars li:nth-child(3) {--color: #6786ad} .bars li:nth-child(4) {--color: #af834c}
.bars li:nth-child(5) {--color: #a6636e}
.source {margin-top: 10px; color: var(--muted); font-size: .85rem}
.scale {display: grid; grid-template-columns: repeat(auto-fit, minmax(142px, 1fr));
  gap: 8px 16px; margin-top: 10px; font-size: .86rem}
.scale dt {font-weight: 650; color: var(--ink)}
.scale dd {color: var(--muted)}
details {border-top: 1px solid var(--line); padding: 11px 0} details:last-child {border-bottom: 1px solid var(--line)}
summary {cursor: pointer; overflow-wrap: anywhere} summary:focus-visible {outline: 2px solid var(--blue); outline-offset: 3px}
summary strong {margin-right: 12px} details .process {margin-top: 10px; background: #fbfcfd}
@media (max-width: 620px) {main {margin: 0 auto; border: 0; border-radius: 0}
  .process {grid-template-columns: minmax(0, 1fr); gap: 16px} .hero {flex-wrap: wrap}
  .total {text-align: left} .bars li {grid-template-columns: 5.5em 1fr 4.4em}}
@media print {body, main {background: white} main {max-width: none; border: 0; padding: 0; margin: 0}
  .process, .component {break-inside: avoid}}
"""


def _number(value, suffix="", scale=1):
    if value is None:
        return "—"
    rounded = (Decimal(str(value)) * scale).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
    return format(rounded, "f").rstrip("0").rstrip(".") + suffix


def _status(observation):
    if observation.get("capture") == "missing":
        return "未采集"
    return {
        "not_applicable": "不适用",
        "unavailable": "无有效片段",
        "partial": "部分覆盖",
        "complete": "完整覆盖",
    }[observation["status"]]


def _distribution(observation):
    proportions = observation["proportions"]
    items = []
    for label in STATE_POINTS:
        fraction = None if proportions is None else proportions[label]
        if fraction is None:
            meter = '<span class="no-bar" aria-hidden="true"></span>'
        else:
            meter = f'<progress value="{Decimal(str(fraction)):f}" max="1" aria-label="{label}"></progress>'
        items.append(f'<li><span>{label}</span>{meter}<span>{_number(fraction, "%", 100)}</span></li>')
    return '<ul class="bars" aria-label="五种状态的有效时长占比">' + "".join(items) + "</ul>"


def _observation_score(observation):
    value = _number(observation["score"])
    level = observation["band_level"]
    if level is not None and level < len(STATE_POINTS):
        upper_bound = level * BAND_WIDTH
        if Decimal(value) >= upper_bound:
            return f"&lt;{upper_bound}"
    return value


def _scoring_scale():
    entries = []
    for level, (label, points) in enumerate(reversed(STATE_POINTS.items()), start=1):
        closing = "]" if level == len(STATE_POINTS) else ")"
        interval = f"[{(level - 1) * BAND_WIDTH}, {level * BAND_WIDTH}{closing}"
        entries.append(f'<div><dt>第{level}档 · {label}</dt><dd>代表分 {points} · 区间 {interval}</dd></div>')
    return '<dl class="scale" aria-label="模拟评分量表">' + "".join(entries) + '</dl>'


def _observation_panel(observation):
    score = observation["score"]
    score_display = "—" if score is None else f'{_observation_score(observation)}<span class="unit">/ 100</span>'
    band = "—"
    if observation["band_level"] is not None:
        band = f'第{observation["band_level"]}档 · {escape(observation["band"])}'
    return f"""
    <div class="process">
      <div>
        <div class="caption">多模态表现分</div><div class="summary-score">{score_display}</div>
        <dl class="facts">
          <dt>状态档次</dt><dd>{band}</dd>
          <dt>覆盖情况</dt><dd>{_status(observation)} · {_number(observation["coverage"], "%", 100)}</dd>
          <dt>有效分类时长</dt><dd>{_number(observation["valid_seconds"], " 秒")}</dd>
          <dt>作答时长</dt><dd>{_number(observation["answer_seconds"], " 秒")}</dd>
        </dl>
      </div>
      {_distribution(observation)}
    </div>"""


def render_report(report):
    """Render the result returned by calculate_report without rescoring it."""
    candidate = escape(str(report["candidate"]))
    total = report["total"]
    total_display = "待补齐" if total is None else f'{_number(total)}<span class="unit">/ 100</span>'
    cards = []
    for key, name in COMPONENT_NAMES.items():
        component = report["components"][key]
        card_type = "component feature" if key == "multimodal" else "component"
        score_value = _observation_score(report["session"]) if key == "multimodal" else _number(component["score"])
        cards.append(f"""
          <article class="{card_type}"><h3>{name}</h3>
            <span class="value">{score_value}</span>
            <p class="meta">权重 {_number(component["weight"], "%", 100)}</p>
            <p class="meta">折合 {_number(component["contribution"], " 分")}</p>
          </article>""")
    questions = []
    for question in report["questions"]:
        title = escape(str(question["title"] or question["id"]))
        question_id = escape(str(question["id"]))
        questions.append(f"""
          <details><summary><strong>{title}</strong><span class="meta">{_status(question)}</span></summary>
            <p class="source">题目编号 {question_id}</p>{_observation_panel(question)}
          </details>""")
    question_html = "".join(questions) or '<p class="meta">尚无作答记录。</p>'
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{candidate} · 面试评价报告</title><style>{STYLES}</style></head>
<body><main>
  <header><div class="mast"><span class="brand">真才实鉴</span><span class="caption">模拟面试评价报告</span></div>
    <div class="hero"><div><p class="caption">通用管培生 · 个人报告</p><h1>{candidate}</h1></div>
      <div class="total"><p class="caption">综合得分</p><strong>{total_display}</strong></div></div></header>
  <section><h2 class="section-title">核心评分</h2><div class="components">{"".join(cards)}</div></section>
  <section><h2 class="section-title">面试全程状态分布</h2>{_observation_panel(report["session"])}
    <p class="source">各状态代表分按有效时长占比加权求和，按未舍入的分数定档。整场表现分乘以多模态权重，即为本项综合分贡献。</p>
    {_scoring_scale()}
    <p class="source">演示使用人工提供的模拟状态时段和其余四项输入分。</p></section>
  <section><h2 class="section-title">逐题作答</h2>{question_html}</section>
</main></body></html>"""


def _write_html(destination, html):
    temporary = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(html)
        os.replace(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description="将 V2 模拟面试计分输入导出为离线 HTML。")
    parser.add_argument("input", type=Path, help="模拟面试 JSON")
    parser.add_argument("output", type=Path, help="个人报告 HTML")
    args = parser.parse_args()
    if args.output.suffix.lower() not in (".html", ".htm"):
        parser.error("输出报告须使用 .html 或 .htm 扩展名")
    if args.input.resolve() == args.output.resolve():
        parser.error("输出报告与输入文件须使用不同路径")
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8-sig"))
        html = render_report(calculate_report(payload))
        _write_html(args.output, html)
    except (OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
