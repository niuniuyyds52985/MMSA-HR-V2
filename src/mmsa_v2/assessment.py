"""Deterministic V2 input validation and simulation score calculations."""

from math import isfinite
import re

from .sources import EXAMS, load_bank


def _choice(options, value):
    if not isinstance(value, str) or value not in {opt["id"] for opt in options}:
        raise ValueError("请选择当前题目提供的选项")
    return value


def validate_answer(question, value):
    kind = question["kind"]
    if kind == "text":
        if not isinstance(value, str) or not value.strip() or len(value) > 25000:
            raise ValueError("请填写 1 至 25000 字的回答。没有相关经历可以如实说明。")
        return value.strip()
    if kind == "choice":
        return _choice(question["options"], value)
    if kind == "ranking":
        options = question["options"]
        if not isinstance(value, list) or len(value) != len(options):
            raise ValueError("请为全部选项排序")
        checked = [_choice(options, v) for v in value]
        if len(set(checked)) != len(checked):
            raise ValueError("排序中不可重复选择")
        return checked
    if kind == "compound":
        fields = question["fields"]
        if not isinstance(value, dict) or set(value) != {f["id"] for f in fields}:
            raise ValueError("请分别完成本题的全部字段")
        return {f["id"]: _choice(f["options"], value[f["id"]]) for f in fields}
    raise ValueError("此题型尚未定义")


def sjt_score(question_id, value, bank=None):
    bank = bank or load_bank()
    question = next((q for f in bank["factors"] for q in f["questions"] if q["id"] == question_id), None)
    if not question or "answer_key" not in question:
        return None
    return question["answer_key"][validate_answer(question, value)]


def choice_signals(plan, answers, bank):
    """Read submitted options against the scoring column frozen with the session."""
    keyed = {q["id"]:q for factor in bank["factors"] for q in factor["questions"] if q.get("answer_key")}
    signals = {}
    for q in plan:
        source = keyed.get(q["id"])
        value = answers.get(q["id"],{}).get("value")
        if not source or not isinstance(value,str) or value not in source["answer_key"]:
            continue
        signals[q["factor_id"]] = {"question_id":q["id"], "answer_label":answer_label(q,value),
                                   "score":source["answer_key"][value], "source":source["score_source"]}
    return signals


def answer_label(question, value):
    """Resolve stable option IDs against the exact version of the question the candidate answered."""
    checked = validate_answer(question, value)
    if question["kind"] == "text":
        return checked
    if question["kind"] == "compound":
        parts = []
        for field in question["fields"]:
            labels = {o["id"]: o["text"] for o in field["options"]}
            parts.append(f"{field['prompt']}：{labels[checked[field['id']]]}")
        return "；".join(parts)
    labels = {o["id"]: o["text"] for o in question["options"]}
    if question["kind"] == "ranking":
        return " → ".join(labels[key] for key in checked)
    return labels[checked]


def evidence_signals(factor, text):
    """Locate literal evidence leads; return the original context for independent review."""
    if not text:
        return []
    positive, risk = factor["positive_terms"], factor["risk_terms"]
    groups = [("中性近似", risk["neutral"], risk["source"]),
              ("风险线索", re.sub(r"【[^】]+】", "", risk["revised"]), risk["source"])]
    for match in re.finditer(r"【(强|中)】(.*?)(?=【|$)", positive["revised"], re.S):
        groups.append(("正向" + match[1], match[2], positive["source"]))
    groups.extend([("原始正向", positive["base"], positive["source"]),
                   ("辅助线索", positive["auxiliary"], positive["source"])])
    seen = set()
    result = []
    for category, phrases, source in groups:
        for phrase in re.split(r"[、\n]", phrases):
            term = re.sub(r"[（(][^）)]*[）)]$", "", phrase.strip()).strip()
            if not term or (category, term) in seen:
                continue
            start = text.find(term)
            if start >= 0:
                end = start + len(term)
                seen.add((category, term))
                result.append({"category": category, "phrase": term, "start": start, "end": end,
                               "quote": text[max(0, start - 40):end + 40], "source": source})
    return sorted(result, key=lambda cue: (cue["start"], cue["end"]))


_CET6_QUALIFICATION = re.compile(
    r"(?:已?通过|已?获(?:得)?)?(?:大学)?(?:英语)?(?:六级|CET-?6)(?:考试)?(?:CET-?6)?(?:证书|已?通过|合格|达标)?",
    re.I,
)
_UNQUALIFIED = re.compile(r"未通过|没通过|未过|没过|不及格|未达|不合格|未取得|未考|备考|准备|计划|目标")


def _cet6_qualification(text):
    return _CET6_QUALIFICATION.fullmatch(re.sub(r"[()（）\s]", "", text))


def _english_breakdown(resume):
    """Apply 评分!C4 to exam results and explicit qualifications in the submitted resume."""
    corrected = any({"english", "english_none"} & event.get("changes", {}).keys()
                    for event in resume.get("corrections", []))
    origin = "人工更正" if corrected else "英语成绩栏"
    if resume.get("english_none"):
        return 0, f"{origin}明确登记为无，英语记 0/5。"

    exams = resume.get("english", [])
    thresholds = {"CET6": 425, "雅思": 7}
    evidence = []
    pending = []
    exam_has_cet6 = False
    cet6_result_below_threshold = False
    declaration = None
    for entry in exams:
        name = str(entry.get("exam") or "")
        exam = next((k for k, pattern in EXAMS.items() if pattern.search(name)), name)
        quote = str(entry.get("text") or name)
        score = entry.get("score")
        if exam == "CET6":
            exam_has_cet6 = True
        if exam not in thresholds:
            description = f"{exam} {score:g}" if type(score) in (int, float) else quote
            (evidence if exam == "CET4" else pending).append(f"{origin}「{description}」")
        elif _UNQUALIFIED.search(quote):
            evidence.append(f"{origin}「{quote}」")
            if exam == "CET6":
                cet6_result_below_threshold = True
        elif type(score) in (int, float):
            item = f"{origin}{exam} {score:g} 分"
            if score >= thresholds[exam]:
                return 5, f"{item}达标，英语记 5/5。"
            evidence.append(item)
            if exam == "CET6":
                cet6_result_below_threshold = True
        elif exam == "CET6" and _cet6_qualification(quote):
            declaration = f"{origin}「{quote}」"
        else:
            pending.append(f"{origin}「{quote}」")
    # Corrected records and CET6 entries in the exam section take precedence over skill tags.
    if not corrected and not exam_has_cet6:
        for skill in resume.get("skills", []):
            if not EXAMS["CET6"].search(skill):
                continue
            item = f"技能栏「{skill}」"
            if _cet6_qualification(skill):
                declaration = item
            elif _UNQUALIFIED.search(skill):
                evidence.append(item)
                cet6_result_below_threshold = True
            else:
                pending.append(item)
    if declaration and not cet6_result_below_threshold:
        details = "；".join(evidence + [declaration])
        return 5, f"{details}。按申报的六级资格记 5/5，未列六级分数。"
    if pending:
        return None, "、".join(pending) + "尚不足以判定是否达标。"
    if evidence:
        return 0, "、".join(evidence) + "。按本次申报材料，英语记 0/5。"
    return None, "本次材料缺英语等级或考试成绩。"


def basic_info_breakdown(resume):
    """The workbook's raw maxima are 5 each. Missing facts stay pending."""
    tags = resume.get("school_tags", [])
    degree = resume.get("education")
    status = resume.get("education_status")
    edu = None
    if tags and degree in ("硕士", "博士", "本科", "学士", "专科"):
        if degree not in ("硕士", "博士") or status in ("已取得", "在读"):
            master = degree in ("硕士", "博士") and status == "已取得"
            edu = 2 + int("985" in tags) + int("211" in tags) + int(master)
    english, english_reason = _english_breakdown(resume)
    internships = resume.get("internships", [])
    internship = None
    if resume.get("internship") == "无":
        internship = 0
    elif internships and all(e.get("dates") for e in internships):
        internship = min(5, len(internships))
    raw = {"education": edu, "english": english, "internship": internship}
    return {"raw": raw, "max": 15, "score": sum(raw.values()) * 100 / 15 if None not in raw.values() else None,
            "source": "评分!C3:C5", "english_reason": english_reason + "（评分!C4）",
            "policy": "按简历中的院校标签、已取得硕士及以上、英语资格及实习段数计分。英语六级明确自述可计资格分，有六级成绩时以 425 分为达标线，雅思达标线为 7.0。"}


def basic_info_score(resume):
    return basic_info_breakdown(resume)["score"]


def potential_score(ratings):
    if not isinstance(ratings, dict) or set(ratings) != {f"F{i}" for i in range(1, 9)}:
        return None
    if any(type(score) is not int or not 1 <= score <= 5 for score in ratings.values()):
        return None
    return sum(ratings.values()) * 100 / 40


def experience_score(episode_points):
    """A rater assigns up to 5 raw points per described experience, capped at 10 in total."""
    if not isinstance(episode_points, list) or not episode_points:
        return None
    if any(type(v) not in (int, float) or not isfinite(v) or not 0 <= v <= 5 for v in episode_points):
        raise ValueError("每段经历的评分范围为 0–5")
    return min(10, sum(episode_points)) * 10
