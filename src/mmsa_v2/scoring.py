"""Duration-weighted five-state scoring for consented competition simulations."""

from fractions import Fraction
from math import isfinite


STATE_POINTS = {"洋溢": 100, "饱满": 75, "平和": 50, "略微 down": 25, "非常不好": 0}
BAND_WIDTH = 20
_BANDS = ("非常不好", "略微 down", "平和", "饱满", "洋溢")
_WEIGHT_PERCENT = {
    "basic_info": 5,
    "experience": 8,
    "potential": 60,
    "operation": 2,
    "multimodal": 25,
}
_OTHER_COMPONENTS = tuple(key for key in _WEIGHT_PERCENT if key != "multimodal")
COVERAGE_POLICY = {"version":"v2.coverage.70.1", "min_question_coverage":0.7, "min_session_coverage":0.7}


SCORE_LABELS = {"total": "综合得分", "estimate": "综合估分", "partial": "已评分项累计贡献", "pending": "待评分"}


def score_summary(report):
    """Use the same value and completeness label in personal and cohort reports."""
    if report.get("total") is not None:
        return {"display_score": report["total"], "score_kind": "total"}
    if report.get("estimated_total") is not None:
        return {"display_score": report["estimated_total"], "score_kind": "estimate"}
    available = []
    for item in report.get("components", {}).values():
        for key in ("contribution", "partial_contribution", "estimated_contribution"):
            if item.get(key) is not None:
                available.append(item[key])
                break
    return {"display_score": sum(available) if available else None,
            "score_kind": "partial" if available else "pending"}


def _number(value, field):
    if type(value) not in (int, float) or (type(value) is float and not isfinite(value)):
        raise ValueError(f"{field} must be a finite number")
    # Treat a JSON decimal such as 0.3 as three tenths throughout aggregation.
    return Fraction(str(value))


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a nonempty string")
    return value.strip()


def _summary(durations, answer_seconds, status):
    valid = sum(durations.values(), Fraction())
    points = None
    band = None
    band_level = None
    proportions = None
    if valid:
        points = sum(seconds * STATE_POINTS[state] for state, seconds in durations.items()) / valid
        proportions = {state: float(seconds / valid) for state, seconds in durations.items()}
        band_level = min(int(points // BAND_WIDTH) + 1, len(_BANDS))
        band = _BANDS[band_level - 1]
    result = {
        "status": status,
        "answer_seconds": float(answer_seconds) if answer_seconds is not None else None,
        "valid_seconds": float(valid),
        "coverage": float(valid / answer_seconds) if answer_seconds else None,
        "proportions": proportions,
        "score": float(points) if points is not None else None,
        "band": band,
        "band_level": band_level,
    }
    return result, points


def _read_question(question):
    if not isinstance(question, dict):
        raise ValueError("each question must be an object")
    question_id = _text(question.get("id"), "question.id")
    title = _text(question.get("title"), f"{question_id}.title")
    capture = question.get("capture", "recorded")
    if capture not in ("recorded", "missing", "not_applicable"):
        raise ValueError(f"{question_id}.capture must be recorded, missing or not_applicable")
    segments = question.get("segments")
    if not isinstance(segments, list):
        raise ValueError(f"{question_id}.segments must be a list")
    seconds = question.get("answer_seconds")
    if seconds is not None:
        seconds = _number(seconds, f"{question_id}.answer_seconds")
        if seconds < 0:
            raise ValueError(f"{question_id}.answer_seconds must be nonnegative")
    if capture == "recorded" and seconds is None:
        raise ValueError(f"{question_id}: a recording needs a known answer_seconds duration")
    if capture == "not_applicable" and seconds is not None:
        raise ValueError(f"{question_id}: use null duration for a non-media question")
    if capture != "recorded" and segments:
        raise ValueError(f"{question_id}: only recorded answers may have classified segments")

    spans = []
    for segment in segments:
        if not isinstance(segment, dict):
            raise ValueError(f"{question_id}: each classified segment must be an object")
        state = segment.get("state")
        if not isinstance(state, str) or state not in STATE_POINTS:
            raise ValueError(f"{question_id}: unrecognized expression state")
        start = _number(segment.get("start"), f"{question_id}.segment.start")
        end = _number(segment.get("end"), f"{question_id}.segment.end")
        if not 0 <= start < end <= seconds:
            raise ValueError(f"{question_id}: segment must lie within the answer and have positive duration")
        spans.append((start, end, state))

    durations = dict.fromkeys(STATE_POINTS, Fraction())
    previous_end = Fraction()
    for start, end, state in sorted(spans):
        if start < previous_end:
            raise ValueError(f"{question_id}: classified segments overlap")
        durations[state] += end - start
        previous_end = end
    valid = sum(durations.values())
    if capture == "not_applicable":
        status = "not_applicable"
    elif not valid:
        status = "unavailable"
    elif valid == seconds:
        status = "complete"
    else:
        status = "partial"
    summary, _ = _summary(durations, seconds, status)
    summary.update(id=question_id, title=title, capture=capture)
    return summary, durations, seconds


def calculate_report(data):
    """Calculate question/session observations and the five-component simulation total.

    Segment times use the answer's own timeline. Capture failures exempt no time
    from coverage; if their duration is unknown the session denominator is unknown.
    Only fully classified required recordings can publish a complete demo total.
    """
    if not isinstance(data, dict) or data.get("purpose") != "simulation":
        raise ValueError("purpose must be simulation")
    candidate = _text(data.get("candidate"), "candidate")
    inputs = data.get("components")
    if not isinstance(inputs, dict) or set(inputs) != set(_OTHER_COMPONENTS):
        raise ValueError("components must contain basic_info, experience, potential and operation")
    scores = {}
    for key in _OTHER_COMPONENTS:
        score = None if inputs[key] is None else _number(inputs[key], key)
        if score is not None and not 0 <= score <= 100:
            raise ValueError(f"{key} must lie between 0 and 100")
        scores[key] = score

    if not isinstance(data.get("questions"), list):
        raise ValueError("questions must be a list")
    questions = []
    seen = set()
    session_durations = dict.fromkeys(STATE_POINTS, Fraction())
    total_seconds = Fraction()
    required_statuses = []
    for question in data["questions"]:
        summary, durations, seconds = _read_question(question)
        question_id = summary["id"]
        if question_id in seen:
            raise ValueError(f"duplicate question id: {question_id}")
        seen.add(question_id)
        questions.append(summary)
        if summary["capture"] == "not_applicable":
            continue
        required_statuses.append(summary["status"])
        for state in STATE_POINTS:
            session_durations[state] += durations[state]
        if seconds is None:
            total_seconds = None
        elif total_seconds is not None:
            total_seconds += seconds
    if not required_statuses:
        status = "not_applicable" if questions else "unavailable"
        total_seconds = None
    elif all(s == "complete" for s in required_statuses):
        status = "complete"
    elif any(session_durations.values()):
        status = "partial"
    else:
        status = "unavailable"
    session, scores["multimodal"] = _summary(session_durations, total_seconds, status)
    policy = dict(data.get("coverage_policy") or COVERAGE_POLICY)
    question_min = _number(policy.get("min_question_coverage"), "min_question_coverage")
    session_min = _number(policy.get("min_session_coverage"), "min_session_coverage")
    if not (0 <= question_min <= 1 and 0 <= session_min <= 1):
        raise ValueError("coverage thresholds must be within 0–1")
    insufficient = []
    for question in questions:
        if question["capture"] == "not_applicable":
            continue
        good = bool(question["valid_seconds"] and question["answer_seconds"] and
                    _number(question["valid_seconds"], "valid_seconds") >= question_min * _number(question["answer_seconds"], "answer_seconds"))
        question["meets_coverage_threshold"] = good
        if not good:
            insufficient.append(question["id"])
    eligible = bool(required_statuses and not insufficient and total_seconds and
                    sum(session_durations.values(), Fraction()) >= session_min * total_seconds)
    session.update(coverage_policy=policy, meets_coverage_threshold=eligible,
                   insufficient_question_ids=insufficient)

    components = {}
    total = Fraction()
    for key, percent in _WEIGHT_PERCENT.items():
        score = scores[key]
        contribution = None
        if score is not None and (key != "multimodal" or status == "complete"):
            contribution = score * percent / 100
        components[key] = {
            "score": float(score) if score is not None else None,
            "weight": percent / 100,
            "contribution": float(contribution) if contribution is not None else None,
        }
        if contribution is not None:
            total += contribution
    complete = status == "complete" and all(s is not None for s in scores.values())
    # The estimate explicitly describes observed time. A wholly unobserved
    # required answer cannot be represented by another answer's recording.
    estimated_total = None
    if status == "partial" and eligible and all(s in ("partial", "complete") for s in required_statuses):
        estimate = scores["multimodal"] * _WEIGHT_PERCENT["multimodal"] / 100
        components["multimodal"]["estimated_contribution"] = float(estimate)
        if all(v is not None for v in scores.values()):
            estimated_total = float(total + estimate)
    return {
        "candidate": candidate,
        "purpose": "simulation",
        "questions": questions,
        "session": session,
        "components": components,
        "total": float(total) if complete else None,
        "estimated_total": estimated_total,
        "coverage_policy": policy,
    }
