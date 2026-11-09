"""Time-aligned AU expression evidence and learned speech-emotion inference.

EMFACS describes AU combinations. The five delivery bands below are an explicit
project mapping, not an EMFACS standard or a calibrated measure of inner emotion.
Neither FER argmax nor a class probability is used as an intensity measurement.
"""
from __future__ import annotations

import copy
import math

METHOD = "v2.multimodal-affect.au-ser.3"
BANDS = {-2: "非常不好", -1: "略微 down", 0: "平和", 1: "饱满", 2: "洋溢"}
POLICY = {
    "version": METHOD,
    "description": "OpenFace AU 直接形成面部动作证据；emotion2vec+ 独立识别声音情绪；按同一时间轴融合后再统计五档时长。",
    "face_rules": "AU6+12、AU1+4+15、AU4+5+7+23、AU1+2+5+26 分别作为开心、悲伤、愤怒、惊讶的表情组合证据。AU12 单独只表示嘴角上提，不证明真实开心。",
    "voice_mapping": "happy 对应正向表达，sad/angry/disgusted/fearful 对应负向表达，neutral 对应中性；surprised/other/unknown 不强行指定正负。声音类别的模型分值不作为情绪强度。",
    "classification_rules": "面部正负组合决定方向；中性模态不否决另一模态的明确方向。声画正负相反时保留冲突缺口。强正向须 AU6+12 组合且 AU12≥2.25、AU6≥0.75；强负向须悲伤组合且 AU15≥2.5。单独声音方向只进入饱满或略微 down。没有情绪方向证据时留空，不补平和。",
    "source_notice": "AU 组合参考 Interspeech 2022 第3.3节及表1；动作强弱到五档的映射为本项目演示口径，尚非经过本场景验证的真实情绪量表。",
    "pattern_source": "https://www.isca-archive.org/interspeech_2022/goncalves22_interspeech.pdf",
    "time_rule": "仅统计同一时段有有效 AU、可用声音情绪推理且 VAD 检出发声的交集。五档先按有效秒数形成占比，再计算百分制和25%贡献。",
    "au_scale": "OpenFace intensity 0–5; occurrence 0/1",
    "smile_motion_min": .75,
    "strong_smile_lip_min": 2.25,
    "strong_smile_cheek_min": .75,
    "strong_sad_lip_min": 2.5,
}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _face_directions(values):
    patterns = values.get("patterns") or {}
    active = {name for name, item in patterns.items() if (item.get("matched_fraction") or 0) > 0}
    up = (values.get("mean_intensity") or {}).get("AU12")
    occurrence = (values.get("smile_motion") or {}).get("occurrence_fraction")
    positive = "happy" in active or (_finite(occurrence) and occurrence > 0
                                        and _finite(up) and up >= POLICY["smile_motion_min"])
    return positive, bool(active & {"sad", "anger"}), active


def face_band(row):
    """Return a descriptive AU band; other movements are not default neutral."""
    if row.get("quality") != "valid" or row.get("coverage", 0) < 1:
        return None, "本时段缺少完整、有效的 OpenFace AU 观测。"
    values = row.get("values") or {}
    au = values.get("mean_intensity") or {}
    positive, negative, active = _face_directions(values)
    up, cheek, down = (au.get(k) for k in ("AU12", "AU06", "AU15"))
    if positive and negative:
        return None, "同期出现正负方向的 AU 组合，面部方向存在冲突。"
    if positive:
        if ("happy" in active and _finite(up) and _finite(cheek)
                and up >= POLICY["strong_smile_lip_min"] and cheek >= POLICY["strong_smile_cheek_min"]):
            return 2, "AU6+AU12 组合，嘴角和颊部达到本项目较强表达刻度。"
        return 1, "检出 AU6+AU12 组合或持续嘴角上提动作；采用正向表达档。"
    if negative:
        if "sad" in active and _finite(down) and down >= POLICY["strong_sad_lip_min"]:
            return -2, "检出 AU1+AU4+AU15 组合，嘴角下拉达到本项目较强表达刻度。"
        return -1, "检出悲伤或愤怒方向的 AU 组合；采用负向表达档。"
    if "surprise" in active:
        return None, "检出惊讶组合，单凭该组合不确定正负方向。"
    relevant = values.get("low_activation_aus") or []
    if (relevant and values.get("low_activation_fraction") == 1
            and all(_finite(au.get(k)) and au[k] < POLICY["smile_motion_min"] for k in relevant)):
        return 0, "目标表情 AU 未检出，且对应动作强度低，作为平和的面部证据。"
    return None, "有面部动作，但未匹配明确方向或低激活证据；不默认平和。"


def speech_band(row):
    if row.get("quality") != "valid":
        return None, "本时段声音情绪推理不可用。"
    values = row.get("values") or {}
    label = values.get("label") or row.get("label")
    if label == "happy":
        return 1, "声音模型识别为 happy，提供正向证据；分类分值不用于判断强弱。"
    if label in {"sad", "angry", "disgusted", "fearful", "anger", "disgust", "fear"}:
        return -1, "声音模型识别为负向类别，提供负向证据；分类分值不用于判断强弱。"
    if label == "neutral":
        return 0, "声音模型识别为 neutral，提供中性证据。"
    return None, "声音类别不确定正负方向，保留原始类别与模型分值。"


def fuse_bands(face, voice):
    """Neutral is complementary, not a veto. Opposite directions stay explicit."""
    if face is None and voice is None:
        return None, "声画均没有明确的目标状态证据。"
    if face is not None and voice is not None and face * voice < 0:
        return None, "声画正负方向冲突，保留缺口供回看，不以平均抵消成平和。"
    if face is None:
        return voice, "面部未提供明确方向，采用声音情绪证据。"
    if voice is None:
        return face, "声音类别未提供明确方向，采用 AU 表情证据。"
    if face == 0 and voice != 0:
        return voice, "面部低激活，声音提供明确方向；中性面部不否决声音。"
    if voice == 0 and face != 0:
        return face, "声音中性，AU 提供明确方向；中性声音不否决面部。"
    return face, "声画方向一致；强弱来自可回看的 AU 动作，非模型置信度。"


def _intervals(rows, duration_ms):
    previous = 0
    for row in rows:
        start, end = row.get("start_ms"), row.get("end_ms")
        if not (_finite(start) and _finite(end) and 0 <= previous <= start < end <= duration_ms):
            raise ValueError("Invalid or overlapping affect observation intervals")
        previous = end
    return rows


def estimate(au_module, speech_module, audio_module, duration_ms, media_sha256, visual_comparison=None):
    """Intersect AU, actual SER windows, and exact VAD speech intervals.

    No extrapolation of an entire second from a small speech_fraction. A missing
    model remains a recorded gap. Every evidence row retains its own values.
    """
    if type(duration_ms) is not int or duration_ms <= 0:
        raise ValueError("duration_ms must be a positive integer")
    au_rows = _intervals(au_module.get("observations") or [], duration_ms)
    voice_rows = _intervals(speech_module.get("observations") or [], duration_ms)
    speech_intervals = _intervals((audio_module.get("summary") or {}).get("speech_intervals") or [], duration_ms)
    quality_rows = _intervals(audio_module.get("observations") or [], duration_ms)
    # The speech model supplies its own exact VAD intervals. Older independent
    # adapters without that field still use the acoustic extractor's VAD.
    voice_vad = []
    for row in voice_rows:
        intervals = row.get("speech_intervals", [{"start_ms": row["start_ms"], "end_ms": row["end_ms"]}])
        for interval in _intervals(intervals, duration_ms):
            if not row["start_ms"] <= interval["start_ms"] < interval["end_ms"] <= row["end_ms"]:
                raise ValueError("Speech VAD outside its inference window")
        voice_vad.extend(intervals)
    _intervals(voice_vad, duration_ms)
    streams = [au_rows, voice_rows, quality_rows, speech_intervals, voice_vad]
    boundaries = {0, duration_ms}
    for rows in streams:
        for row in rows:
            boundaries.update((row["start_ms"], row["end_ms"]))
    boundaries = sorted(boundaries)
    cursors = [0] * len(streams)
    segments, evidence = [], []
    for a, b in zip(boundaries, boundaries[1:]):
        aligned = []
        for index, rows in enumerate(streams):
            while cursors[index] < len(rows) and rows[cursors[index]]["end_ms"] <= a:
                cursors[index] += 1
            row = rows[cursors[index]] if cursors[index] < len(rows) else {}
            aligned.append(row if row and row["start_ms"] <= a < b <= row["end_ms"] else {})
        au, voice, acoustic, vad, model_vad = aligned
        af, face_reason = face_band(au)
        av, voice_reason = speech_band(voice)
        band, fusion_reason = fuse_bands(af, av)
        values, speech_values = au.get("values") or {}, voice.get("values") or {}
        positive, negative, _ = _face_directions(values)
        if positive and negative:
            band, fusion_reason = None, "同期面部正负组合冲突，保留缺口，不由声音覆盖该冲突。"
        if au.get("quality") != "valid" or au.get("coverage", 0) < 1 or voice.get("quality") != "valid":
            band, fusion_reason = None, "采集或模型推理缺失，本段不计入五档。"
        volume = (acoustic.get("values") or {}).get("rms_dbfs")
        acoustic_ok = (acoustic.get("quality") == "valid" and _finite(volume) and volume >= -65
            and not set(acoustic.get("quality_flags") or []) & {"near_silence", "audio_clipping"})
        if not acoustic_ok:
            band, fusion_reason = None, "本段声音近静音、削波或采集不可用。"
        elif not vad or not model_vad:
            band, fusion_reason = None, "本时段未同时检出有效发声，不将附近声音窗口外推至停顿。"
        state = BANDS.get(band)
        record = {"start": round(a / 1000, 6), "end": round(b / 1000, 6), "state": state,
            "reason": fusion_reason,
            "values": {"au_intensity": copy.deepcopy(values.get("mean_intensity", {})),
                "au_presence": copy.deepcopy(values.get("presence_fraction", {})),
                "au_patterns": copy.deepcopy(values.get("patterns", {})),
                "face_state": BANDS.get(af), "face_reason": face_reason,
                "speech_state": BANDS.get(av), "speech_reason": voice_reason,
                "speech_label": speech_values.get("label", voice.get("label")),
                "speech_scores": copy.deepcopy(speech_values.get("scores", voice.get("scores", {}))),
                "speech_start": voice["start_ms"] / 1000 if voice else None,
                "speech_end": voice["end_ms"] / 1000 if voice else None,
                "rms_dbfs": volume, "fusion_rule": fusion_reason}}
        evidence.append(record)
        if state:
            if segments and segments[-1]["state"] == state and segments[-1]["end"] == record["start"]:
                segments[-1]["end"] = record["end"]
            else:
                segments.append({k: record[k] for k in ("start", "end", "state")})
    valid = round(sum(s["end"] - s["start"] for s in segments), 6)
    models = {"face": {k: copy.deepcopy(au_module.get(k)) for k in ("model_id", "method", "provenance", "status")},
              "speech": {k: copy.deepcopy(speech_module.get(k)) for k in ("model_id", "method", "provenance", "status")}}
    if visual_comparison:
        models["visual_comparison"] = {k: copy.deepcopy(visual_comparison.get(k)) for k in ("model_id", "method", "provenance", "status")}
    return {"method": METHOD, "source": "local_estimate", "media_sha256": media_sha256,
        "duration_seconds": duration_ms / 1000, "segments": segments, "evidence": evidence,
        "reason": POLICY["description"], "models": models, "fusion_policy": copy.deepcopy(POLICY), "review_required": True,
        "quality": {"sample_count": len(evidence), "valid_seconds": valid, "coverage": valid / (duration_ms / 1000),
                    "unclassified_seconds": round(duration_ms / 1000 - valid, 6)}}
