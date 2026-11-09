"""Versioned interpretation of the supplied term tables, never keyword scoring."""
import re


POLICY_VERSION = "v2.contextual-terms.1"
HIGH_RATING_MIN = 5  # Four-point verification remains a separate business choice.
RISK_FILE = "【风险扣分词-L列】_修订对照表_v3.xlsx"
RISK_FILE_SHA256 = "1888e487e6c91006d4b7406394b8d7c24d420cb42837c4efb73e082bebff108c"
RISK_CAP_TEXT = "真·高雷区词（命中→触发复核/封顶低档（该项最高2分），不单独定论）"


def _terms(text):
    value = re.sub(r"【[^】]+】", "、", text or "")
    result = []
    for phrase in re.split(r"[、\n]+", value):
        phrase = phrase.strip(" ，,")
        if phrase and phrase not in result:
            result.append(phrase)
    return result


def lexicon(factor):
    positive, risk = factor.get("positive_terms", {}), factor.get("risk_terms", {})
    # Positive D/E/F are complementary. Revised risk E already includes old D;
    # retain D as source context without treating it as a second penalty.
    positives = _terms("、".join(positive.get(k, "") for k in ("base", "revised", "auxiliary")))
    risks = _terms(risk.get("revised") or risk.get("base", ""))
    fid = factor["id"]
    return {"positive_terms": dict(positive), "risk_terms": dict(risk),
            "positive_cues": [{"cue_id":f"{fid}:positive:{i}", "term":term, "source":positive.get("source", "")}
                              for i,term in enumerate(positives,1)],
            "risk_cues": [{"cue_id":f"{fid}:risk:{i}", "term":term, "source":risk.get("source", "")}
                          for i,term in enumerate(risks,1)]}


def policy(bank):
    # A future workbook with a changed hash must explicitly re-approve its cap.
    supported = bank.get("sources", {}).get(RISK_FILE) == RISK_FILE_SHA256
    return {"version":POLICY_VERSION, "high_rating_min":HIGH_RATING_MIN,
            "risk_cap": {"enabled":supported, "maximum":2 if supported else None,
                         "scope":"current_factor_only", "source":RISK_FILE+"!E1",
                         "source_sha256":RISK_FILE_SHA256, "source_text":RISK_CAP_TEXT,
                         "conditions":["本人实际反向行为", "原话证据", "对应指标反证", "排除中性、否定、引述和已纠正语境"]}}


def literal_risk_candidates(spec, questions):
    result=[]
    for cue in spec["risk_cues"]:
        term=re.sub(r"[（(][^）)]*[）)]", "", cue["term"])
        if term and any(term in text for q in questions if q["factor_id"]==spec["factor_id"]
                        for text in q["answers"].values()):
            result.append(cue["cue_id"])
    return result


def cap_result(factor_result, spec, scoring_policy):
    """Apply a sourced cap only to confirmed semantic reverse-behavior evidence."""
    original=factor_result["rating"]
    cap=scoring_policy["risk_cap"]
    confirmed=[r for r in factor_result["term_review"]["risk"] if r["status"]=="confirmed_reverse"]
    adjustments=[]
    if confirmed and not cap["enabled"] and original is not None:
        adjustments.append({"rule_id":POLICY_VERSION+":unverified-risk-policy", "source":cap["source"],
                            "scope":"current_factor_only", "from":original, "to":None,
                            "reason":"风险表来源版本尚未核实，保留反证并等待老师核查，未擅用旧封顶。",
                            "evidence":[ref for r in confirmed for ref in r["evidence"]],
                            "cue_ids":[r["cue_id"] for r in confirmed]})
    if confirmed and cap["enabled"] and original is not None and original>cap["maximum"]:
        adjustments.append({"rule_id":POLICY_VERSION+":risk-cap", "source":cap["source"],
                            "source_text":cap["source_text"], "scope":"current_factor_only",
                            "from":original, "to":cap["maximum"], "reason":"已核对本人实际反向行为及对应指标，排除中性例外后按风险表限制本因子最高2分。",
                            "evidence":[ref for r in confirmed for ref in r["evidence"]],
                            "cue_ids":[r["cue_id"] for r in confirmed]})
    return {"proposed_rating":original, "accepted_rating":adjustments[0]["to"] if adjustments else original,
            "adjustments":adjustments, "policy_version":POLICY_VERSION}
