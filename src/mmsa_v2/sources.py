"""Read the versioned V2 bank and extract, cite and route resume facts."""

import hashlib
import io
import json
from pathlib import Path
import re
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile


BANK_FILE = Path(__file__).resolve().parents[2] / "config/v2/bank.json"
CHOICE_WORDING_FILE = BANK_FILE.with_name("choice_wording.json")
CHOICE_WORDING_SOURCE = "config/v2/choice_wording.json"
SHORT_MODE = "short_5_3"
LEGACY_MODE = "legacy_full"
SCREENING_COUNT = 5
INTERVIEW_COUNT = 3
# G-column questions originally followed other questions in their row; restore that context separately.
EXPERIENCE_CONTEXT = {
    "F1": "请回想一次你牵头组织或协调他人的经历。",
    "F2": "请回想一次你持续帮助同学或同事成长的经历。",
    "F3": "请回想一次你与团队成员处理分歧的经历。",
    "F4": "请回想一次目标明确、推进中遇到阻碍的经历。",
    "F5": "请回想一次在本组与整体利益之间需要取舍的经历。",
    "F6": "请回想一次原有计划或规则中途发生变化的经历。",
    "F7": "请回想一次收到反馈后调整做法的经历。",
    "F8": "请回想一次把一种做事方法用到另一种情境的经历。",
}
WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
DATE = re.compile(r"^(?:20\d\d[.年/-]\d{1,2}).{0,12}(?:20\d\d[.年/-]\d{1,2}|至今|现在)")
HEADINGS = {
    "教育背景": "education", "教育经历": "education", "英语学习成绩": "english", "英语水平": "english",
    "实习经历": "internships", "实习经验": "internships", "校园实践经历": "campus", "校园经历": "campus",
    "项目经历": "campus", "荣誉奖励": "honors", "专业技能": "skills", "自我评价": "about",
}
EXAMS = {
    "CET4": re.compile(r"CET[ -]?4|英语四级", re.I),
    "CET6": re.compile(r"CET[ -]?6|英语六级", re.I),
    "雅思": re.compile(r"雅思|IELTS", re.I),
    "托福": re.compile(r"托福|TOEFL", re.I),
}
MATCH_TERMS = {
    "F1": ("牵头", "统筹", "协调", "分工", "组建", "负责人", "组织", "队长"),
    "F2": ("指导", "带教", "培训", "辅导", "培养", "授课", "帮助", "志愿"),
    "F3": ("分歧", "协商", "谈判", "协调", "沟通", "对接", "协作"),
    "F4": ("交付", "达成", "提升", "降低", "目标", "降本", "完成"),
    "F5": ("供应链", "上下游", "战略", "产业链", "业务链", "价值链", "整体"),
    "F6": ("调整", "变化", "迭代", "应急", "预警", "优化"),
    "F7": ("反馈", "复盘", "访谈", "建议", "改进", "回顾"),
    "F8": ("学习", "建模", "迁移", "从零", "从 0", "搭建", "研究"),
}


def load_bank():
    """Apply reviewed wording only when constructing a new frozen session bank."""
    bank = json.loads(BANK_FILE.read_text(encoding="utf-8"))
    wording_bytes = CHOICE_WORDING_FILE.read_bytes()
    wording = json.loads(wording_bytes.decode("utf-8"))
    if wording.get("base_bank_version") != bank["version"] or not wording.get("version"):
        raise ValueError("选项改写与原题库版本不匹配，请先核对改写来源")
    questions = {q["id"]: q for f in bank["factors"] for q in f["questions"] if q["id"].endswith("-I")}
    revisions = wording.get("questions", [])
    if len(revisions) != len(questions) or {r["id"] for r in revisions} != set(questions):
        raise ValueError("选项改写必须完整且不重复地覆盖八因子情境题")
    for revision in revisions:
        question = questions[revision["id"]]
        if (revision.get("original_source") != question["source"] or
                revision.get("score_source") != question["score_source"]):
            raise ValueError("选项改写的原题或计分来源不匹配")
        options = {o["id"]: o for o in question["options"]}
        rewrites = revision.get("options", [])
        if len(rewrites) != len(options) or {o["id"] for o in rewrites} != set(options):
            raise ValueError("选项改写不能增加、删除或替换选项编号")
        for rewrite in rewrites:
            original = options[rewrite["id"]]
            if (rewrite.get("original") != original["text"] or
                    rewrite.get("score") != question["answer_key"][rewrite["id"]]):
                raise ValueError("选项改写的原文或原分值不匹配")
            revised = rewrite.get("revised")
            if not isinstance(revised, str) or not revised.strip() or not rewrite.get("rationale"):
                raise ValueError("选项改写须提供有效文字和行为含义说明")
            original["text"] = revised
        question["wording_provenance"] = {
            "version": wording["version"], "source": CHOICE_WORDING_SOURCE + "#" + question["id"],
            "original_source": question["source"], "score_source": question["score_source"],
            "rationale": revision["rationale"], "options": rewrites}
        question["source"] += "；选项经题库等义改写（" + wording["version"] + "）"
    bank["base_version"] = bank["version"]
    bank["sources"][CHOICE_WORDING_SOURCE] = hashlib.sha256(wording_bytes).hexdigest()
    bank["wording_revision"] = {"version": wording["version"], "source": CHOICE_WORDING_SOURCE,
                                "description": wording["description"]}
    # Include the complete applied content as well as its source file fingerprint.
    canonical = json.dumps({k: v for k, v in bank.items() if k != "version"}, ensure_ascii=False, sort_keys=True)
    bank["version"] = "v2." + hashlib.sha256(canonical.encode()).hexdigest()[:16]
    return bank


def _paragraphs(content):
    try:
        with ZipFile(io.BytesIO(content)) as archive:
            info = archive.getinfo("word/document.xml")
            if info.file_size > 8 * 1024 * 1024:
                raise ValueError("简历正文超过解析上限")
            xml = archive.read(info)
        if b"<!DOCTYPE" in xml.upper():
            raise ValueError("简历正文不支持 DTD")
        root = ElementTree.fromstring(xml)
    except (BadZipFile, ElementTree.ParseError, KeyError, RuntimeError) as exc:
        raise ValueError("无法读取 DOCX 简历正文") from exc
    paragraphs = []
    for index, p in enumerate(root.iter(WORD + "p"), 1):
        runs = ["".join(t.text or "" for t in r.iter(WORD + "t")) for r in p.iter(WORD + "r")]
        text = "".join(runs).strip()
        if text:
            paragraphs.append({"source": f"word/document.xml#p{index}", "text": text, "runs": runs})
    if not paragraphs:
        raise ValueError("简历中没有可解析的文字")
    return paragraphs


def _experiences(section, kind):
    dates = [(idx, item) for idx, item in enumerate(section) if idx and DATE.search(item["text"])]
    result = []
    for n, (idx, date) in enumerate(dates):
        end = dates[n+1][0] - 1 if n+1 < len(dates) else len(section)
        title = section[idx-1]
        lines = section[idx+1:end]
        result.append({"kind": kind, "title": title["text"], "source": title["source"], "dates": date["text"],
                       "text": "\n".join(v["text"] for v in lines),
                       "evidence": [{"source": v["source"], "text": v["text"]} for v in lines]})
    if not result and section and not any(re.fullmatch(r"(?:暂无|尚无|没有|无)(?:相关)?(?:实习|经历|实习经历)?[。！]?", p["text"]) for p in section):
        # Free-form sections can be used for questions. Their episode counts need review.
        result.append({"kind": kind, "title": section[0]["text"], "source": section[0]["source"],
                       "dates": None, "text": "\n".join(v["text"] for v in section),
                       "evidence": [{"text": v["text"], "source": v["source"]} for v in section]})
    return result


def parse_resume(path_or_bytes, filename=None):
    if isinstance(path_or_bytes, (bytes, bytearray)):
        content = bytes(path_or_bytes)
    else:
        path = Path(path_or_bytes)
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("简历文件超过 16 MB")
        content = path.read_bytes()
        filename = filename or path.name
    if len(content) > 16 * 1024 * 1024:
        raise ValueError("简历文件超过 16 MB")
    paragraphs = _paragraphs(content)
    sections = {}
    current = "header"
    for p in paragraphs:
        heading = HEADINGS.get(p["text"].rstrip("：: "))
        if heading:
            current = heading
            sections.setdefault(heading, [])
        else:
            sections.setdefault(current, []).append(p)
    facts = []

    def fact(key, value, p, quote):
        if p:
            facts.append({"field": key, "value": value, "source": p["source"], "quote": quote})
        return value

    first = paragraphs[0]
    name_match = re.fullmatch(r"(?:姓名[：:]\s*)?([\u4e00-\u9fff·]{2,12})", first["text"])
    name = name_match[1] if name_match and name_match[1] not in ("简历", "个人简历", "教育背景") else None
    if not name:
        for p in paragraphs:
            match = re.search(r"姓名[：:]\s*([\u4e00-\u9fff·]{2,12})", p["text"])
            if match:
                name = match[1]
                break
    if not name and filename:
        name = re.sub(r"^\d+[_-]|\(\d+\)$", "", Path(filename).stem)
    edu = sections.get("education", [])
    ranks = {"博士": 4, "硕士": 3, "本科": 2, "学士": 2, "专科": 1}
    degree_rows = []
    for idx, row in enumerate(edu):
        match = re.search("博士|硕士|本科|学士|专科", row["text"])
        if match:
            degree_rows.append((idx, match[0]))
    degree_line = None
    degree = None
    degree_section = []
    if degree_rows:
        idx, degree = max(degree_rows, key=lambda item: ranks[item[1]])
        end = next((i for i, _ in degree_rows if i > idx), len(edu))
        degree_section = edu[idx:end]
        degree_line = edu[idx]
        fact("education", degree, degree_line, degree_line["text"])
    status = "待核实"
    for p in degree_section:
        if "预计" in p["text"] or "在读" in p["text"]:
            status = fact("education_status", "在读", p, p["text"])
            break
        if "已取得" in p["text"] or "已毕业" in p["text"]:
            status = fact("education_status", "已取得", p, p["text"])
    school = None
    tags = []
    gender = None
    for p in sections.get("header", []) + degree_section:
        for run in p["runs"]:
            explicit = re.search(r"院校[：:]\s*([^（）()\s｜|]+)(?:[（(]([^（）()]+)[）)])?", run)
            if explicit:
                school = fact("school", explicit[1], p, explicit[0])
                label = explicit[2] or ""
                tags = [t for t in ("985", "211", "其他") if t in label]
                if tags:
                    fact("school_tier", " / ".join(tags), p, explicit[0])
            sex = re.search(r"性别[：:]\s*[♀♂]?\s*([男女])", run)
            if sex:
                gender = fact("gender", sex[1], p, sex[0])
    if degree_line:
        match = re.match(r"([\u4e00-\u9fff·]+?(?:大学|学院))(?=[\s（）(]|$)", degree_line["text"])
        if match:
            if school != match[1]:
                tags = []
            school = fact("school", match[1], degree_line, match[1])
            label = re.search(r"[（(]((?:985|211|其他)(?:[/、\s]+(?:985|211|其他))*)[）)]", degree_line["text"])
            if label:
                tags = [t for t in ("985", "211", "其他") if t in label[1]]
                fact("school_tier", " / ".join(tags), degree_line, label[0])
    english = []
    for p in sections.get("english", []):
        for exam, pattern in EXAMS.items():
            found = pattern.search(p["text"])
            if found:
                # Search after the last exam token, since CET labels appear twice in the template.
                tail = p["text"][list(pattern.finditer(p["text"]))[-1].end():]
                match = re.search(r"^\s*[)）：:\s]*\s*(\d+(?:\.\d+)?)", tail)
                score = float(match[1]) if match else None
                english.append({"exam": exam, "score": score, "source": p["source"], "text": p["text"]})
                fact("english", exam, p, p["text"])
    internships = _experiences(sections.get("internships", []), "实习")
    campus = _experiences(sections.get("campus", []), "校园")
    internship = "有" if internships else "未提供"
    if internships:
        fact("internship", "有", next(p for p in paragraphs if p["source"] == internships[0]["source"]), internships[0]["title"])
    else:
        for p in sections.get("internships", []):
            if re.search(r"无|没有", p["text"]):
                internship = fact("internship", "无", p, p["text"])
                break
    return {"name": name or "未提供姓名", "education": degree or "未提供", "education_status": status,
            "school": school or "未提供", "school_tier": " / ".join(tags) or "未提供", "school_tags": tags,
            "internship": internship, "internships": internships, "experiences": internships + campus,
            "english": english, "english_none": any(p["text"] in ("无", "暂无") for p in sections.get("english", [])),
            "gender": gender, "honors": [p["text"] for p in sections.get("honors", [])],
            "skills": [s.strip() for p in sections.get("skills", []) for s in p["runs"] if s.strip()],
            "facts": facts, "source_hash": hashlib.sha256(content).hexdigest()}


def _match_factor(fid, experiences):
    best = (0, None, [])
    for entry in experiences:
        words = [word for word in MATCH_TERMS[fid] if word in entry["title"] + entry["text"]]
        if len(words) > best[0]:
            best = (len(words), entry, words)
    return best


def _bank_item(source, factor, version, match):
    q = {k: source[k] for k in ("id", "source", "kind", "prompt", "options", "fields", "factor_id")}
    q.update(factor_name=factor["name"], bank_version=version, derived_from=[],
             reason="通用题库，覆盖本因子共同考察点。")
    if source.get("wording_provenance"):
        # Display provenance without exposing the original score mapping in a plan.
        q["wording_provenance"] = {k: v for k, v in source["wording_provenance"].items() if k != "options"}
    _, entry, words = match
    if entry:
        supporting = [e["source"] for e in entry.get("evidence", [])
                      if any(word in e["text"] for word in words)]
        q.update(reason=f"简历经历「{entry['title']}」涉及{'、'.join(words)}，用于选择本组问题。",
                 derived_from=list(dict.fromkeys([entry["source"], *supporting])))
    return q


def screening_plan(resume, bank):
    """Five versioned bank choices, covering the workbook's three dimensions."""
    pool = []
    for factor in bank["factors"]:
        source = next((q for q in factor["questions"] if q["kind"] == "choice" and q.get("answer_key")
                       and q["id"].endswith("-I")), None)
        if source:
            match = _match_factor(factor["id"], resume.get("experiences", []))
            pool.append((factor, source, match))
    pool.sort(key=lambda item: -item[2][0])  # bank order breaks ties, including an empty resume
    if len(pool) < SCREENING_COUNT:
        raise ValueError("本场题库缺少五个不同因子的单选题")
    dimensions = {}
    for item in pool:
        dimensions.setdefault(item[0]["dimension"], item[0]["id"])
    representatives = set(dimensions.values())
    selected = sorted(pool, key=lambda item: item[0]["id"] not in representatives)[:SCREENING_COUNT]
    plan = []
    for factor, source, match in selected:
        q = _bank_item(source, factor, bank["version"], match)
        q["stage"] = "screening"
        q["score_source"] = source["score_source"]
        if not match[1]:
            wording = "情境单选题（选项经题库等义改写，沿用原计分）" if source.get("wording_provenance") else "原始单选题"
            q["reason"] = f"覆盖「{factor['dimension']}」；按提问清单顺序选择「{factor['name']}」的{wording}。"
        plan.append(q)
    return plan


def interview_questions(screening, answers, resume, bank):
    """Fill the other three factors. SJT choices influence order, never the G-factor's rating."""
    by_factor = {q["factor_id"]: q for q in screening}
    if not all(answers.get(q["id"], {}).get("value") is not None for q in screening):
        return []
    dimensions = {}
    for factor in bank["factors"]:
        choice = by_factor.get(factor["id"])
        if choice:
            original = next(q for q in factor["questions"] if q["id"] == choice["id"])
            value = answers[choice["id"]]["value"]
            label = next(o["text"] for o in original["options"] if o["id"] == value)
            dimensions.setdefault(factor["dimension"], []).append(
                (original["answer_key"][value], choice["id"], label, original["score_source"]))

    pool = []
    for factor in bank["factors"]:
        if factor["id"] in by_factor:
            continue
        dimension = factor["dimension"]
        signals = dimensions[dimension]  # screening_plan represents each of the three dimensions
        lowest = min(item[0] for item in signals)
        mean = sum(item[0] for item in signals) / len(signals)
        match = _match_factor(factor["id"], resume.get("experiences", []))
        strength, entry, words = match
        narrative = next(q for q in factor["questions"] if q["kind"] == "text" and q["id"].endswith("-G"))
        q = _bank_item(narrative, factor, bank["version"], match)
        citations = "；".join(f"{qid} 选「{label}」对应 {score} 分（{cell}）" for score,qid,label,cell in signals)
        reason = f"单选未覆盖「{factor['name']}」，选本因子 G 列补充实际经历。排序参考同维度「{dimension}」的已答题：{citations}。"
        if entry:
            reason += f"简历「{entry['title']}」出现{'、'.join(words)}，用于安排题序和提示举例。"
        else:
            reason += "简历中未识别到直接匹配的经历线索。"
        q.update(stage="interview", reason=reason, answer_hint=EXPERIENCE_CONTEXT[factor["id"]],
                 resume_hint=f"可结合简历中的「{entry['title']}」或其他真实经历作答。" if entry else "",
                 ordering_question_ids=[item[1] for item in signals])
        pool.append(((lowest, -strength, mean), q))
    if len(pool) != INTERVIEW_COUNT:
        raise ValueError("本场题库的八个因子必须由五道单选与三道问答分别覆盖")
    pool.sort(key=lambda item: item[0])  # equal signals and resume matches retain bank order
    return [q for _, q in pool]


def make_plan(resume, bank=None):
    bank = bank or load_bank()
    experiences = resume.get("experiences", [])
    intro = {"id": "EXPERIENCE", "kind": "text", "factor_id": None, "factor_name": "经历介绍",
             "prompt": "请介绍一至两段实习或校园经历，说明你本人的工作、收获以及与管培岗位的关联。",
             "fields": [], "options": [], "reason": "评分表要求记录经历介绍。", "source": "评分!B6:C6",
             "derived_from": [], "bank_version": bank["version"]}
    if experiences:
        intro["prompt"] += f" 你可以从简历中的「{experiences[0]['title']}」讲起。"
        intro["derived_from"] = [experiences[0]["source"]]
    plan = [intro]
    matched = [(factor, _match_factor(factor["id"], experiences)) for factor in bank["factors"]]
    for factor, match in sorted(matched, key=lambda item: -item[1][0]):
        entry = match[1]
        for source in factor["questions"]:
            q = _bank_item(source, factor, bank["version"], match)
            if entry and q["id"].endswith("-G"):
                q["prompt"] += f" 可结合简历中的「{entry['title']}」作答，也可以提供更合适的经历。"
            plan.append(q)
    return plan


def conditional_questions(question, value, bank=None):
    bank = bank or load_bank()
    original = next((q for f in bank["factors"] for q in f["questions"] if q["id"] == question["id"]), None)
    probe = original.get("probe") if original else None
    if not probe or (probe["when_any"] and value not in probe["when_any"]):
        return []
    return [{**question, "id": question["id"] + "-followup", "parent_id": question["id"],
             "prompt": probe["prompt"], "kind": "text", "options": [], "fields": [],
             "reason": "题库中的条件核实题。"}]


def verification_questions(factor_id, bank=None):
    bank = bank or load_bank()
    factor = next((f for f in bank["factors"] if f["id"] == factor_id), None)
    if not factor:
        raise ValueError("未知因子")
    return [{"id": f"{factor_id}-verify-{key}", "parent_id": f"{factor_id}-G", "factor_id": factor_id,
             "factor_name": factor["name"], "kind": "text", "options": [], "fields": [],
             "prompt": bank["rules"][f"B{row}"].strip("“”"), "source": f"评分与记录规则!B{row}",
             "reason": bank["rules"][f"C{row}"], "bank_version": bank["version"], "derived_from": []}
            for row, key in [(14, "action"), (15, "cost"), (16, "experience")]]
