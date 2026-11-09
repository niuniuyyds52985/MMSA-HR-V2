"""Evidence-linked draft for the local 5+3 simulation, with no external inference."""

import copy
import hashlib
import json
import re

from .assessment import experience_score


# Each tuple describes one behaviour, corroborating evidence, and the H-column
# conditions for the high anchor. The expressions never award points by hit count.
RULES_BANK = "v2.70fae5c88f50b0cd"
# Audited material for RULES_BANK. Only I-column display labels/source annotations
# are excluded; G prompts, H anchors, indicators, raw workbook cells, choice IDs
# and answer keys, all scoring rules and source-workbook hashes remain covered.
RULES_MATERIAL_SHA256 = "98cec1eb7c0230563a786afa74169c40cb19e0265b56bb3022a28f0b02a0c6f8"
METHOD = "本地模拟规则估分"
MANY = r"(?:[3-9]\d*|[1-9]\d+|三|四|五|六|七|八|九|十多?)"
RESULT = r"按时(?:交付|完成)|如期|准时|提前.{0,6}(?:完成|交付)|最终完成|下降了?|减少了?|缩短了?|提升了?|提高了?|从.{1,12}(?:降|升|提高|减少|缩短|达到|提升)(?:到|至)?|达到了?|独立完成|完成了|恢复了|通过了|验证了"
RULES = {
    "F1": (r"牵头|主动组织|主动承担|带领|协调|重新分工|分配任务|组织.{0,6}(?:同学|小组|成员|团队)",
           r"分工|跟进.{0,6}进度|协调.{1,7}(?:资源|进度|时间)|负责.{0,6}兜底",
           (rf"牵头.{{0,5}}{MANY}次", r"(?:没有|无)(?:正式)?头衔|没有奖励", r"(?:加班|多花|花了).{1,16}(?:冲突|背责|担责|责任)|(?:冲突|背责|担责).{1,16}(?:加班|多花)"),
           r"(?:我|本人).{0,5}(?:从未|没有过|没主动|从来没).{0,3}牵头|我只在有.{0,4}(?:头衔|奖励)时才"),
    "F2": (r"带教|教会|教.{0,6}(?:方法|操作|使用)|指导|带着.{1,8}(?:练习|操作)|帮助.{1,8}(?:学会|上手)|拆解.{0,6}步骤",
           r"(?:他|她|对方|新人|同学).{0,10}(?:独立|自己)(?:完成|交付|操作)|每周.{0,6}(?:带|教|复盘)",
           (rf"(?:带教|帮助|辅导).{{0,4}}{MANY}(?:名|人|位|个)", r"(?:竞争|对手).{0,12}分享|分享.{0,12}(?:竞争|对手)", r"独立(?:完成|交付|操作)"),
           r"(?:我|本人).{0,3}(?:全部|全都|全是|总是)(?:替|帮).{0,5}(?:做完|代劳)|我从未(?:持续)?帮助"),
    "F3": (r"分别.{0,6}(?:听|聊|问|沟通|谈)|单独.{0,8}(?:聊|谈|沟通)|先听|听取.{0,8}(?:意见|顾虑)|了解.{0,8}(?:顾虑|诉求|想法|意见)|对齐.{0,4}目标|协调.{0,6}(?:冲突|分歧)",
           r"达成一致|共同目标|重新分工|折中|形成.{0,4}共识|都(?:接受|同意)|商量.{0,5}(?:分工|方案)",
           (rf"(?:沟通|协调|调解).{{0,4}}{MANY}次", r"一致|共识|和解|都同意", r"按时|如期|没耽误|未延误"),
           r"我(?:从来没有|从未|没有主动)(?:主动)?沟通|我一律交给(?:上级|领导)"),
    "F4": (r"定.{0,5}目标|设.{0,4}目标|倒排|拆解.{0,5}目标|改用|改走|换了.{0,5}(?:方案|办法|路径)|尝试.{0,5}(?:替代|路径|方案)",
           r"目标.{0,12}\d|\d+\s*%|达成率|完成率|量化差距",
           (r"目标.{0,16}\d", r"(?:两|三|2|3)(?:套|条|种)(?:路径|方案|办法)", r"(?:达成率|完成率|完成了?).{0,4}(?:[89]\d|100)\s*[%％]|量化.{0,8}差距.{0,12}复盘"),
           r"我.{0,8}(?:遇阻|受阻|遇到困难)(?:时|后)?就(?:直接)?放弃"),
    "F5": (r"梳理|画.{0,4}(?:价值链|流程图)|对接|盘点|协调|分析.{0,8}(?:环节|影响)|放弃.{0,8}(?:局部|部门|个人|排期)|推迟|延后|让出",
           r"上游|下游|供应链|交付环节|生产部门|采购部门|销售团队|客服团队",
           (r"上游|下游|其他部门", r"放弃|牺牲|让出|推迟|延后", r"整体|整体交付|全组|全团队|全公司"),
           r"我(?:决定|就|只管)?直接开工.{0,16}(?:不知道|不清楚|没考虑)(?:上下游|上游|下游)"),
    "F6": (r"重排|调整.{0,8}(?:方案|流程|优先级|计划|方法|策略)|改用|改为|改成|换成|重新安排|重新设计|重新制定|推出.{0,4}新方案|改了.{0,4}(?:计划|方案)|换了.{0,5}(?:思路|办法|流程|方法)",
           r"验证|对照|测试|试点|试用|对比|小范围|回访|复测|抽查",
           (r"当天|当晚|次日|第二天|两天内|24小时|48小时", r"验证|对照|测试|试点|试用|对比|回访|复测|抽查"),
           r"我(?:一直|当时|选择)?等(?:信息|指令|消息).{0,5}(?:齐|全|明朗|明确).{0,3}再(?:行动|动手|动)|我从未经历过变化"),
    "F7": (r"追问|请教|询问.{0,8}(?:反馈|意见|细节)|主动.{0,5}(?:问|征求)|约.{0,6}复盘|请.{0,5}指出|请.{1,6}提意见",
           r"反馈|批评|意见|问题|指出|细节",
           (rf"(?:反馈|意见|请教).{{0,6}}{MANY}次|{MANY}次.{{0,10}}(?:反馈|意见)", r"改为|改成|调整|修改", r"复测|回访|对照|复查|验证"),
           r"我(?:从未|从来没)(?:主动)?(?:征求|寻求|问过).{0,4}(?:反馈|意见)|我只(?:顾|是)解释.{0,5}(?:不改|没改)"),
    "F8": (r"自学|学习了?|学会|迁移|应用到|运用到|用在|复用|提炼.{0,6}方法",
           r"独立.{0,6}(?:交付|完成)|迁移.{0,6}(?:项目|任务)|应用到.{1,10}|复用.{0,6}(?:方法|流程)",
           (r"(?:两|二|2|三|3)(?:个|项|门)(?:新领域|技能|工具)", r"(?:[1-4一二三四两]周内|不到一个月)", r"迁移|用在|应用到|复用"),
           r"我(?:只是|只有|只)(?:报班|听课|看教程).{0,12}(?:没有|毫无)(?:任何)?(?:产出|实际交付|实践)"),
}

CONTEXT = r"实习|社团|校园|课题|项目|团队|小组|比赛|竞赛|部门|公司|志愿|课程设计|上学期|学期末|去年|上个月|当时|那次"
COST = r"(?:我|为此).{0,8}(?:多花|花了|额外投入|加班|牺牲|放弃|推迟|延后).{1,14}"
REFLECTION = r"我(?:学会|学到|认识到|意识到|发现)|我的收获|这让我|复盘(?:后|时)"
ROLE = r"管培|管理培训|岗位|这份工作|任职后"


def _rules_compatible(bank):
    """Allow reviewed presentation revisions without trusting a base-version claim alone."""
    if bank.get("version") != RULES_BANK and bank.get("base_version") != RULES_BANK:
        return False
    try:
        material = {k: copy.deepcopy(bank[k]) for k in
                    ("factors", "rules", "required_fields", "original_weights_sheet")}
        material["sources"] = {k: v for k, v in bank["sources"].items()
                               if k != "config/v2/choice_wording.json"}
        for factor in material["factors"]:
            for question in factor["questions"]:
                if question["id"].endswith("-I"):
                    question.pop("source", None)
                    question.pop("wording_provenance", None)
                    question["options"] = [{"id": option["id"]} for option in question["options"]]
        serialized = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode()).hexdigest() == RULES_MATERIAL_SHA256
    except (KeyError, TypeError, AttributeError):
        return False


def _normalized(text):
    return re.sub(r"[^\w]", "", text.lower())


def _statements(text, prompt):
    """Exclude copies, questions, plans and quoted suggestions from action evidence."""
    prompt_text = _normalized(prompt)
    result = []
    for match in re.finditer(r"[^。！？!?\n；;]+[。！？!?]?", text):
        sentence = match.group().strip()
        plain = _normalized(sentence)
        if len(plain) < 4 or plain in prompt_text or sentence.endswith(("？", "?", "吗", "吗。")):
            continue
        if re.search(r"如果|假如|假设|我会|我打算|我计划|我要是|我应该|我认为应该|我可能会|他们说|他建议我", sentence):
            continue
        if sentence not in result:
            result.append(sentence)
    return result


def _affirmed(pattern, sentence):
    # Reject negated events and reported speech instead of treating the underlying
    # action word as positive evidence. Allow intrinsically affirmative contrasts
    # encoded by a complete rule, e.g. “没有头衔” in F1.
    for match in re.finditer(pattern, sentence):
        before = re.split(r"[，,：:]", sentence[:match.start()])[-1]
        if not re.search(r"没有|并未|从未|没能|未能|不是|并非|不会|未曾|没|不想|不打算|别人(?:说|做)|他(?:说|建议)|她(?:说|建议)|[“「]", before):
            return True
    return False


def _quote(sentences, pattern, *, affirmative=True):
    for sentence in sentences:
        if (_affirmed(pattern, sentence) if affirmative else re.search(pattern, sentence)):
            return sentence
    return ""


def _personal_action(sentences, pattern):
    for sentence in sentences:
        for match in re.finditer(pattern, sentence):
            prefix = sentence[:match.start()]
            subjects = re.findall(r"我(?:们)?|同事|别人|导师|对方|老师|她们?|他们?", prefix)
            if subjects and subjects[-1].startswith("我") and _affirmed(pattern, sentence):
                return sentence
            # “我先找同事分别沟通” still describes the speaker's action.
            if re.search(r"我(?:们)?(?:先|主动)?(?:找|约|带领|向|跟|和|请|带着)[^，,。]{1,8}$", prefix) and _affirmed(pattern, sentence):
                return sentence
    return ""


def _rating(question, factor, sentences):
    fid = factor["id"]
    action, corroboration, high, opposite = RULES[fid]
    context = _quote(sentences, CONTEXT)
    act = _personal_action(sentences, action)
    corroborated = _quote(sentences, corroboration)
    outcome = _quote(sentences, RESULT)
    reverse = _quote(sentences, opposite)
    high_evidence = [_quote(sentences, pattern) for pattern in high]
    cost = _quote(sentences, COST)
    source = next((k for k in factor["source_cells"] if k.startswith("H")), "H")
    verification = {}
    if reverse and act:
        rating, reason, quotes = None, "同时出现相反和积极的行为陈述，需核对发生次序与语境。", [act, reverse]
    elif reverse:
        rating, reason, quotes = 1, "作答明确陈述了低档锚点中的反向行为。", [reverse]
    elif act and corroborated and outcome and all(high_evidence) and context:
        rating, reason, quotes = 4, "本题较高档的行为、条件和结果均有原话支持，行动代价仍需核实。", [act, corroborated, outcome, *high_evidence, context]
        if cost:
            rating, reason = 5, "本题高档行为条件齐备，可定位本人行动、付出代价、实际经历与结果。"
            verification = {k: {"question_id": question["id"], "quote": v} for k, v in
                            (("action", act), ("cost", cost), ("experience", context))}
            quotes.append(cost)
    elif act and corroborated and outcome and (context or cost) and all(high_evidence[1:]):
        rating, reason, quotes = 4, "实际行动有本题相关的旁证及结果，支持较高档；最高档条件尚未全部验证。", [act, corroborated, outcome, context or cost]
    elif act and corroborated and (context or outcome):
        rating, reason, quotes = 3, "讲述了与本题相关的行动及其具体情境，符合通用中档的积极行为描述。", [act, corroborated, context or outcome]
    elif act and (context or outcome):
        rating, reason, quotes = 2, "描述了相关行动，但本题锚点所需的进一步证据不齐。", [act, context or outcome]
    else:
        rating, reason, quotes = None, "尚未辨认出能对应本题锚点的本人行为。需核对原片或补充实际例子。", []
    entry = {"factor_id": fid, "rating": rating, "reason": f"{METHOD} · 八因子提问清单!{source}。{reason}",
             "evidence": [{"question_id": question["id"], "quote": q} for q in dict.fromkeys(quotes) if q]}
    if verification:
        entry["verification"] = verification
    return entry


def _experience(question, sentences, rating):
    """C6 draft. Count each evidenced episode once, up to five raw points."""
    context = _quote(sentences, CONTEXT)
    action = _personal_action(sentences, r"整理|设计|做了|分析|组织|牵头|复盘|修改|梳理|制定|调整|协调|负责|改|联系|听|核对|沟通|拆解|教|交付|提出|访谈|追问|学习|测试|调查|查阅")
    if not action and rating["rating"] and rating["rating"] >= 2:
        action = rating["evidence"][0]["quote"]
    if not (context and action):
        return None
    facets = {"情境": context, "本人工作": action, "结果": _quote(sentences, RESULT),
              "收获": _quote(sentences, REFLECTION), "岗位关联": _quote(sentences, ROLE)}
    # Only explicit, distinct event names justify adding a second episode.
    named = re.search(r"[《「“]([^《》“”「」]{2,35})[》」”](?:项目|比赛|课题|实习)|(?:在|参加了|负责了)([\w]{2,16}?(?:公司实习|银行实习|社团活动|竞赛|比赛|项目|课题))(?:时|中|期间|里|上|，|,)", context)
    key = next((g for g in named.groups() if g), "") if named else ""
    present = {k:v for k,v in facets.items() if v}
    return key, len(present), {"question_id": question["id"], "quote": action}, present


def content_draft(state, bank):
    """Produce a reviewable offline draft; candidates never receive the scoring map."""
    factors = {f["id"]: f for f in bank["factors"]}
    compatible = _rules_compatible(bank)
    entries, episodes = [], {}
    for question in state["plan"]:
        fid = question.get("factor_id")
        if question.get("kind") != "text" or fid not in factors or not question["id"].endswith("-G"):
            continue
        answer = state["answers"].get(question["id"], {})
        raw = (answer.get("text") or answer.get("transcript") or "").strip()
        sentences = _statements(raw, question["prompt"])
        if not compatible or fid not in RULES:
            entries.append({"factor_id": fid, "rating": None, "evidence": [], "reason": "本地估分规则尚未核对本场题库版本。"})
            continue
        entry = _rating(question, factors[fid], sentences)
        entries.append(entry)
        episode = _experience(question, sentences, entry)
        if episode and (episode[0] not in episodes or episodes[episode[0]][1] < episode[1]):
            episodes[episode[0]] = episode

    # An unnamed example could describe either named example. Never add it twice.
    named = sorted((x for k,x in episodes.items() if k), key=lambda x: -x[1])[:2]
    selected = named or list(episodes.values())[:1]
    if "" in episodes and episodes[""][1] > sum(x[1] for x in selected):
        selected = [episodes[""]]
    rated = sum(f["rating"] is not None for f in entries)
    result = {"source":"local_auto", "author":METHOD, "factors":entries,
              "experience_score":experience_score([x[1] for x in selected]),
              "experience_reason":"本次问答缺少可定位的经历与本人工作。",
              "summary":f"{METHOD}已取得 {rated}/{len(entries)} 项问答的可评级原话。",
              "next_steps":[f'请核对{factors[f["factor_id"]]["name"]}题的原片或作答原话。'
                            for f in entries if f["rating"] is None]}
    if selected:
        result["experience_evidence"] = selected[0][2]
        parts = [f'{ref["question_id"]}（{key or "一段经历"}）' + "、".join(facets) + f'，暂计 {points}/5。'
                 for key, points, ref, facets in selected]
        result["experience_reason"] = (f"{METHOD} · 评分!B6:C6。演示暂按情境、本人工作、结果、收获、岗位关联"
                                       "各 1 分，每段最高 5 分，合计最高 10 分。" + "".join(parts))
    return result
