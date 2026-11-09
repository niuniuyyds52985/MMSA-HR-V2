"""Local, frozen-version Chinese PDF reports. No URLs, browser or network renderer."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import hashlib
import re
import threading
from xml.sax.saxutils import escape

from interview_text_normalization import normalize_for_match

FONT_PATH = Path(__file__).parent / 'vendor/noto-sans-sc/NotoSansSC-Regular.ttf'
FONT_SHA256 = '432a5a6e9f533c2b15d15cc79d2912b66b19d4ce634efdbe15704a7484c56225'
FONT_NAME = 'ZhenjianNotoSC'
_RENDER_LOCK = threading.RLock()
_FONT_READY = False
FACTOR_NAMES = {'leadership': '领导潜力', 'results': '结果导向', 'learning': '学习敏锐度',
                'develop_others': '发展他人', 'balance_people_task': '平衡人际与任务',
                'systems_thinking': '系统思考', 'adaptability': '适应力', 'feedback': '反馈运用'}
MODULE_NAMES = {'audio': '声音', 'expression': '表情类别', 'au': '面部动作', 'au_net': 'AU-Net 面部动作',
                'gaze': '视线观察', 'gaze360': 'Gaze360 视线', 'emotic': '场景辅助标签'}
STATES = {'complete': '已完成', 'succeeded': '已完成', 'partial': '部分完成', 'failed': '未完成',
          'unavailable': '不可用', 'queued': '排队中', 'running': '处理中', 'no_video': '没有视频',
          'no_audio': '没有音轨', 'no_valid_observations': '没有有效采样', 'not_applicable': '无适用素材'}
REQUIREMENT_STATES = {'demonstrated': '已有证据支持', 'partial': '部分体现',
                      'not_met': '有相反证据，待复核', 'insufficient_evidence': '证据不足',
                      'not_measured': '本轮未测'}


class PdfExportError(Exception):
    pass


def _safe(value):
    text = str(value) if value is not None else ''
    # Only explicit report fields are rendered. Additionally redact capabilities
    # accidentally pasted into otherwise ordinary free text; never embed links.
    text = re.sub(r'\bsk-(?:ws-)?[A-Za-z0-9._-]{16,}', '[已隐藏密钥]', text, flags=re.I)
    text = re.sub(r'(?i)\b(?:token|access_token|api_key|apikey|authorization)\s*[:=]\s*[^\s，。；<>]+', '[已隐藏凭证]', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9._~+/-]+', '[已隐藏凭证]', text)
    text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', text)
    return normalize_for_match(text).replace('\u2011', '-').replace('\u2013', '-').replace('\u2014', '-')


def _stamp(ms):
    if not isinstance(ms, (int, float)) or ms < 0:
        return '时间未记录'
    return f'{int(ms // 60000):02d}:{ms % 60000 / 1000:04.1f}'


def _score(question):
    value = question.get('score')
    return f'{value} / 5' if type(value) is int and 1 <= value <= 5 and not question.get('review_stale') else '暂未形成'


def _font():
    global _FONT_READY
    if _FONT_READY:
        return
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        if not FONT_PATH.is_file() or hashlib.sha256(FONT_PATH.read_bytes()).hexdigest() != FONT_SHA256:
            raise PdfExportError('PDF 中文字体缺失或校验失败，请重新安装完整交付包。')
        pdfmetrics.registerFont(TTFont(FONT_NAME, str(FONT_PATH)))
        _FONT_READY = True
    except ImportError:
        raise PdfExportError('PDF 导出依赖未安装，请运行交付环境安装脚本。') from None


def _observations(report, answer):
    """Select the newest source for each module at this frozen transcript version."""
    runs = list((report.get('standard_analysis') or {}).get('runs') or [])
    runs += list((report.get('research_appendix') or {}).get('selected_runs') or [])
    picked, sources = set(), []
    ordered = sorted(enumerate(runs), key=lambda pair: (str(pair[1].get('completed_at') or pair[1].get('created_at') or ''), pair[0]), reverse=True)
    for _, run in ordered:
        if run.get('status') == 'superseded':
            continue
        for item in (run.get('result') or {}).get('multimodal') or []:
            if item.get('answer_id') != answer.get('answer_id') or item.get('transcript_revision') != answer.get('transcript_revision'):
                continue
            modules = {key: value for key, value in (item.get('modules') or {}).items() if key not in picked}
            if modules:
                picked.update(modules)
                sources.append((run, item, modules))
    return sources


def _measurement(event):
    values = event.get('measurements') or {}
    pieces = []
    for key, label in [('median', '片段中位值'), ('difference', '相对变化'), ('rms_dbfs', '声音电平'),
                       ('f0_hz', '基频'), ('sampled_frames', '采样帧数'), ('valid_frames', '有效帧数')]:
        value = values.get(key)
        if type(value) in (float, int):
            unit = values.get('difference_unit' if key == 'difference' else 'unit', '')
            pieces.append(f'{label} {value:.3g} {unit}'.strip())
    for key, label in [('activation_scores', 'AU 激活'), ('intensity', 'AU 强度'), ('category_scores', '模型类别原始分值（非概率）')]:
        numeric = [(name, value) for name, value in (values.get(key) or {}).items() if type(value) in (float, int)] if isinstance(values.get(key), dict) else []
        if numeric:
            pieces.append(label + '：' + '，'.join(f'{name} {value:.3g}' for name, value in sorted(numeric, key=lambda pair: pair[1], reverse=True)[:3]))
    return '；'.join(pieces)


def build_report_pdf(report):
    """Return a PDF from one already-frozen report, without modifying its payload."""
    if not isinstance(report, dict) or type(report.get('version')) is not int or report['version'] < 1:
        raise PdfExportError('报告缺少有效的冻结版本，无法导出。')
    with _RENDER_LOCK:
        _font()
        return _render(report)


def _render(report):
    from functools import partial
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen.canvas import Canvas
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, KeepTogether
    from interview_profile import FACTORS
    factor_names = {factor["factor_id"]: factor["name"] for factor in FACTORS}

    ink, muted, teal = colors.HexColor('#18333d'), colors.HexColor('#5c727c'), colors.HexColor('#087c87')
    styles = {
        'title': ParagraphStyle('title', fontName=FONT_NAME, fontSize=23, leading=31, textColor=ink, spaceAfter=12),
        'heading': ParagraphStyle('heading', fontName=FONT_NAME, fontSize=15, leading=23, textColor=teal, spaceBefore=14, spaceAfter=8, keepWithNext=True),
        'sub': ParagraphStyle('sub', fontName=FONT_NAME, fontSize=11, leading=18, textColor=ink, spaceBefore=9, spaceAfter=5, keepWithNext=True),
        'requirement': ParagraphStyle('requirement', fontName=FONT_NAME, fontSize=10.5, leading=17, textColor=ink, spaceBefore=6, spaceAfter=3, keepWithNext=True, wordWrap='CJK'),
        'body': ParagraphStyle('body', fontName=FONT_NAME, fontSize=9.5, leading=16, textColor=ink, spaceAfter=7, wordWrap='CJK', splitLongWords=True),
        'small': ParagraphStyle('small', fontName=FONT_NAME, fontSize=8, leading=13, textColor=muted, spaceAfter=5, wordWrap='CJK', splitLongWords=True),
        'quote': ParagraphStyle('quote', fontName=FONT_NAME, fontSize=9, leading=15, textColor=ink, leftIndent=10, rightIndent=8, borderColor=colors.HexColor('#d2e5e6'), borderWidth=.7, borderPadding=8, backColor=colors.HexColor('#f4f9f9'), spaceBefore=5, spaceAfter=9, wordWrap='CJK', splitLongWords=True),
        'cell': ParagraphStyle('cell', fontName=FONT_NAME, fontSize=8.5, leading=14, textColor=ink, wordWrap='CJK', splitLongWords=True),
    }
    glyphs = pdfmetrics.getFont(FONT_NAME).face.charWidths
    def p(text, style='body'):
        plain = _safe(text)
        plain = ''.join(c if c in '\n\t' or ord(c) in glyphs else '[未支持字符]' for c in plain)
        return Paragraph(escape(plain).replace('\n', '<br/>'), styles[style])
    def add(text, style='body'):
        story.append(p(text, style))
    def quote(ref, label=None):
        if not isinstance(ref, dict) or not ref.get('quote'):
            return
        add((str(label) + ' · ' if label else '') + '“' + str(ref['quote']) + '”', 'quote')
        add('引用：回答 ' + str(ref.get('answer_id', '未记录'))[:12] + ' · 转写 v' + str(ref.get('transcript_revision', '未记录')), 'small')
    def table(rows, widths):
        result = Table([[p(cell, 'cell') for cell in row] for row in rows], colWidths=widths, repeatRows=1, hAlign='LEFT')
        result.setStyle(TableStyle([('BACKGROUND', (0,0),(-1,0), colors.HexColor('#edf5f6')), ('VALIGN',(0,0),(-1,-1),'TOP'),
            ('LEFTPADDING',(0,0),(-1,-1),9),('RIGHTPADDING',(0,0),(-1,-1),9),('TOPPADDING',(0,0),(-1,-1),5),('BOTTOMPADDING',(0,0),(-1,-1),5),
            ('LINEBELOW',(0,0),(-1,0),.6,colors.HexColor('#c6dcdf')),('LINEBELOW',(0,1),(-1,-1),.35,colors.HexColor('#e0e9ec'))]))
        story.append(result)

    stream, story = BytesIO(), []
    doc = SimpleDocTemplate(stream, pagesize=A4, rightMargin=19*mm, leftMargin=19*mm, topMargin=24*mm, bottomMargin=22*mm,
        title='真才实鉴面试报告 v' + str(report['version']), author='真才实鉴', pageCompression=1, invariant=1)
    def page(canvas, document):
        canvas.saveState()
        canvas.setFont(FONT_NAME, 8)
        canvas.setFillColor(muted)
        canvas.drawString(19*mm, A4[1]-14*mm, '真才实鉴  /  管培生模拟面试')
        canvas.drawRightString(A4[0]-19*mm, A4[1]-14*mm, '冻结报告 v' + str(report['version']))
        canvas.setStrokeColor(colors.HexColor('#dce7ea'))
        canvas.line(19*mm, 18*mm, A4[0]-19*mm, 18*mm)
        canvas.drawString(19*mm, 12*mm, '依据回答内容形成初评；声音与表情观察供原片核对。')
        canvas.drawRightString(A4[0]-19*mm, 12*mm, '第 ' + str(document.page) + ' 页')
        canvas.restoreState()

    questions = report.get('questions') or []
    conclusion = report.get('potential_conclusion') or {}
    # An export must not silently relabel an old conclusion with a new version.
    conclusion_current = isinstance(conclusion, dict) and conclusion.get('source_version') == report['version']
    conclusion_factors = {f['factor_id']: f for f in conclusion.get('factors', [])
                          if isinstance(f, dict) and f.get('factor_id')} if conclusion_current else {}
    add('管培生面试报告', 'title')
    add(report.get('candidate_ref') or '未命名模拟面试', 'sub')
    add('报告版本 v' + str(report['version']) + ' · 创建时间 ' + str(report.get('created_at') or '未记录') + '\n报告生成时间 ' + str(report.get('generated_at') or '未记录'), 'small')
    add('本场结论与覆盖范围', 'heading')
    valid = [q for q in questions if _score(q) != '暂未形成']
    reviewed = [q for q in questions if q.get('score_source') == 'hr' and not q.get('review_stale')]
    total = report.get('total_score')
    add('内容证据总分：' + (f'{total:.1f} / 100' if type(total) in (int, float) else '暂未形成') + f'。当前有效评价 {len(valid)} / {len(questions)} 题，老师已复核 {len(reviewed)} 题。')
    planned_factors = len({q.get('factor_id') for q in questions if q.get('factor_id')})
    add(f'本场安排 {len(questions)} 道主问，覆盖 {planned_factors} / 8 个因子。主问与追答共同构成一道评价单元。', 'small')
    add('未测或技术缺失不记为 0 分。每因子抽到一题仅表示初步覆盖，不能替代对其各项要求逐一核对；不同题卷尚未完成等值验证。', 'small')
    if conclusion_current:
        add(conclusion.get('summary') or '本版本尚未形成可读的要求结论。')
        add(conclusion.get('teacher_review_note') or '逐要求判断仍为模型草稿，待指导老师核对。', 'small')
    elif conclusion:
        add('要求结论版本与本报告不一致，未沿用旧结论；当前原话与逐题评价仍保留。', 'small')
    else:
        add('此版本未保存逐要求结论。下方保留逐题内容评价和原话，不据分数反推整项要求是否满足。', 'small')
    add('八因子覆盖', 'heading')
    factor_rows = [['潜力维度 / 因子', '要求证据状态', '题目初评']]
    for factor in FACTORS:
        matching = [q for q in questions if q.get('factor_id') == factor['factor_id']]
        q = matching[0] if len(matching) == 1 else None
        state = '本轮未测' if not matching else '多个问题，未配置合并' if q is None else '依据更新，待复核' if q.get('review_stale') else '老师已复核' if q.get('score_source') == 'hr' else '模型初评，待复核' if _score(q) != '暂未形成' else '材料待核对'
        factor_conclusion = conclusion_factors.get(factor['factor_id'])
        if factor_conclusion and not any(item.get('review_stale') for item in matching):
            state = REQUIREMENT_STATES.get(factor_conclusion.get('status'), '证据不足')
        factor_rows.append([factor['dimension_name'] + ' / ' + factor['name'], state, _score(q) if q else '未测'])
    table(factor_rows, [80*mm, 58*mm, 34*mm])
    add('PDF 为该版本的阅读副本，简体显示不改写原始转写。原片与逐窗数据请在同场工作台核对；本文件不包含访问凭证或可访问原片的链接。', 'small')

    events = report.get('decision_events') or report.get('decisions') or []
    for index, q in enumerate(questions, 1):
        story.append(PageBreak())
        add(f'第 {index} 题 · {factor_names.get(q.get("factor_id"), q.get("factor_id", ""))}', 'title')
        add(q.get('text') or '题目文字未记录', 'sub')
        source = '老师复核' if q.get('score_source') == 'hr' else '模型初评，待老师核对' if q.get('model_evaluation') or q.get('score_source') == 'model_draft' else '尚无当前初评'
        add('当前评价：' + _score(q) + ' · ' + source)
        if q.get('review_stale'):
            add('依据已更新，旧复核不计入当前总分。')
        add(q.get('reason') or '本题尚未形成可用评价。')
        printed_refs = {}
        for ref in (q.get('evidence') or [])[:6]:
            if isinstance(ref, dict) and ref.get('quote'):
                ref_key = (ref.get('answer_id'), ref.get('transcript_revision'), ref.get('quote'))
                if ref_key not in printed_refs:
                    printed_refs[ref_key] = len(printed_refs) + 1
                    quote(ref, '原话证据 ' + str(printed_refs[ref_key]))
        factor_conclusion = conclusion_factors.get(q.get('factor_id'))
        if factor_conclusion:
            add('潜力模型要求与本场证据', 'heading')
            add(factor_conclusion.get('notice') or '以下为回答内容的逐要求草稿，题目总分复核不等于逐项确认。', 'small')
            requirements = factor_conclusion.get('requirements') or factor_conclusion.get('criteria') or []
            for requirement in requirements:
                state = REQUIREMENT_STATES.get(requirement.get('status'), '证据不足')
                if q.get('review_stale'):
                    state = '依据更新，待重新核对'
                add(state + ' · ' + str(requirement.get('text') or '原表要求未记录'), 'requirement')
                add('原有判断依据已变化，本版本不将旧要求结论视为当前结论。' if q.get('review_stale')
                    else requirement.get('reason') or '尚无可核对的判断理由。')
                if not q.get('review_stale'):
                    for ref in (requirement.get('evidence') or [])[:2]:
                        if not isinstance(ref, dict) or not ref.get('quote'):
                            continue
                        ref_key = (ref.get('answer_id'), ref.get('transcript_revision'), ref.get('quote'))
                        if ref_key in printed_refs:
                            add('对应原话：证据 ' + str(printed_refs[ref_key]) + '，回答 ' + str(ref.get('answer_id', '未记录'))[:12]
                                + ' · 转写 v' + str(ref.get('transcript_revision', '未记录')), 'small')
                        else:
                            printed_refs[ref_key] = len(printed_refs) + 1
                            quote(ref, '原话证据 ' + str(printed_refs[ref_key]))
                gaps = requirement.get('gaps') or []
                if gaps:
                    add('待补材料：' + '；'.join(str(gap) for gap in gaps[:3]), 'small')
                cells = list(dict.fromkeys(str(ref['source_cell']) for ref in requirement.get('source_refs') or []
                                           if isinstance(ref, dict) and ref.get('source_cell')))
                if cells:
                    add('原表考察内容：' + '、'.join(cells), 'small')
            if not requirements:
                add('该因子的原表要求尚未进入本版本结论。', 'small')
        gaps = (q.get('model_evaluation') or {}).get('gaps') or []
        if gaps and q.get('score_source') != 'hr' and not q.get('review_stale'):
            add('待核实事项', 'sub')
            for gap in gaps[:5]:
                add('• ' + str(gap.get('description') or '模型未提供具体说明'))
        add('为何追问 / 如何推进', 'heading')
        decisions = [e for e in events if e.get('parent_question_id') == q.get('question_id')]
        if not decisions:
            add('本版本未记录已封存的推进决定。')
        for decision in decisions:
            summary = decision.get('decision_summary') or {}
            add('追加追问' if decision.get('action') == 'ask_followup' else '继续推进', 'sub')
            add(summary.get('brief_reason') or decision.get('reason_for_hr') or '未记录具体原因。')
            selected = summary.get('selected_branch') or {}
            branch = selected if isinstance(selected, str) else selected.get('text')
            if branch:
                add('实际追问：' + branch)
            for ref in (summary.get('used_evidence') or [])[:2]:
                quote(ref)
            dictionary = summary.get('dictionary_snapshot') or {}
            matches = dictionary.get('matches') or []
            if matches:
                add('词典线索：' + '；'.join(str(m.get('term') or '') + ('（保留）' if m.get('retained') else '（排除）') for m in matches[:8]), 'small')
                add('词典线索按当时记录呈现，关键词出现本身不代表获得能力分。', 'small')
        for answer in q.get('answers') or []:
            if answer.get('current') is False:
                continue
            answer_start = len(story)
            label = '追答' if answer.get('parent_question_id') else '主问回答'
            add(label + '与多模态观察', 'heading')
            add('回答 ' + str(answer.get('answer_id') or '')[:12] + ' · 转写 v' + str(answer.get('transcript_revision') or '未记录')
                + (' · 采用文字作答，未采集音视频。' if not answer.get('has_media') else ''), 'small')
            text = answer.get('transcript') or ''
            add(text[:1400] + ('\n（此处为前 1400 字节选，完整原文保留在工作台。）' if len(text)>1400 else '') if text else '本回答尚无可读转写；可在工作台回放已保存原片。', 'quote')
            if len(text) < 500:
                story[answer_start:] = [KeepTogether(story[answer_start:])]
            if not answer.get('has_media'):
                continue
            sources = _observations(report, answer)
            if not sources:
                add('本版本没有与此回答版本相符的可读多模态结果，已有原片仍可核对。', 'small')
                continue
            event_pool = []
            for run, item, modules in sources:
                summary = item.get('reader_summary') or {}
                coverage = summary.get('coverage') or {}
                for key, module in modules.items():
                    c = coverage.get(key) or {}
                    info = MODULE_NAMES.get(key, key) + '：' + STATES.get(module.get('status'), module.get('status') or '未记录状态')
                    if c.get('sampled_observations') is not None:
                        info += f'；有效采集 {c.get("valid_observations", 0)} / {c["sampled_observations"]}'
                    add(info, 'small')
                for event in summary.get('events') or []:
                    refs = event.get('evidence_refs') or []
                    if refs and not any(ref.get('module') in modules or ref.get('module_id') in modules for ref in refs):
                        continue
                    event_pool.append((event, summary, run))
            selected = []
            # Extended context must not crowd out actual voice and face highlights.
            for category in ('audio_', 'apparent_expression', 'context_expression'):
                match = next((item for item in event_pool if str(item[0].get('kind') or '').startswith(category)), None)
                if match and match not in selected:
                    selected.append(match)
            for item in event_pool:
                if len(selected) >= 5:
                    break
                if item not in selected:
                    selected.append(item)
            for event, summary, run in selected:
                add(str(event.get('title') or '可回看观察') + ' · ' + _stamp(event.get('start_ms')) + ' - ' + _stamp(event.get('end_ms')), 'sub')
                add(event.get('description') or '本片段未提供可读解释。')
                measurement = _measurement(event)
                if measurement:
                    add('测量依据：' + measurement, 'small')
                add('核对采集点 ' + _stamp(event.get('anchor_ms', event.get('start_ms'))) + ' · 来源运行 ' + str(summary.get('source_run_id') or run.get('run_id') or '未记录')[:16], 'small')
            if not selected:
                add('当前有模块状态，但尚无可定位的观察摘要。', 'small')
            add('以上为实际采样及模型类别描述，不等于当事人的真实感受、诚信、人格或心理状态，也不计入能力分。其余观察及原始测量保留在工作台。', 'small')
    doc.build(story, onFirstPage=page, onLaterPages=page, canvasmaker=partial(Canvas, invariant=1))
    return stream.getvalue()
