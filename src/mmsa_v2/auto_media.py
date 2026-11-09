"""Versioned, offline measurements and an explainable competition demonstration rubric."""

import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile

from .media import LEGACY, analyze_recording, media_info
from .state_rules import METHOD, LEGACY_METHOD, SMILE_THRESHOLDS
from interview_media import ALLOWED_SUFFIXES, MediaError, _run
from interview_multimodal import run_multimodal


CHUNK_MS = 120_000
FULL_VISUAL_CACHE_MS = 120_000
MODULES = ("au", "gaze", "expression", "audio")
PRODUCTION_MODULES = ("au", "gaze", "audio")
RUBRIC = ("比赛演示规则；按有效采样段汇总外显表达，并非经过效度验证的情绪测量。"
          "愉悦用 FER happy 和嘴角上提 AU12 交叉印证，较明显时结合颊部 AU6；"
          "低落方向用 FER sad 与嘴角下拉 AU15 交叉印证。FER 前两名输出差低于 0.25 时留空。"
          "声音用于核对发声与录制质量。")


def _classify(fer, au, audio):
    flags = set(fer.get("quality_flags", [])) | set(au.get("quality_flags", []))
    if fer.get("quality") != "valid" or au.get("quality") != "valid" or flags & {"repeated_identical_frames","low_light","overexposed"}:
        return None, "该时点没有质量合格的同一张脸的表情与动作观测。", {}
    if fer.get("track_segment_id") != au.get("track_segment_id") or not fer.get("track_segment_id"):
        return None, "表情与动作的跟踪片段未对齐。", {}
    voice = audio.get("values", {})
    if (audio.get("quality") != "valid" or voice.get("rms_dbfs") is None or
            not math.isfinite(voice["rms_dbfs"]) or voice["rms_dbfs"] < -65 or
            set(audio.get("quality_flags", [])) & {"audio_clipping","near_silence"}):
        return None, "该时点的声音缺失、近静音或存在削波。", {}
    if not voice.get("speech_fraction") or voice["speech_fraction"] <= 0:
        return None, "该声学窗口未测得发声，未纳入有效作答时长。", {}
    label = fer["values"].get("label")
    scores = fer["values"].get("raw_scores", {})
    ranked = sorted(scores.values(), reverse=True)
    if (len(ranked) < 2 or not all(math.isfinite(value) for value in ranked) or
            scores.get(label) != ranked[0] or ranked[0] - ranked[1] < 0.25):
        return None, "表情模型的前两名原始输出差小于演示阈值 0.25，或输出不完整。", {}
    intensity = au["values"].get("intensity", {})
    if any(not isinstance(intensity.get(k), (int, float)) for k in ("AU06","AU12","AU15")):
        return None, "缺少用于复核的面部动作强度。", {}
    cheek, up, down = (intensity[k] for k in ("AU06","AU12","AU15"))
    values = {"expression": label, "fer_logit_margin":round(ranked[0]-ranked[1],3),
              "AU06": cheek, "AU12": up, "AU15": down,
              "rms_dbfs": voice["rms_dbfs"], "f0_hz": voice.get("f0_hz"),
              "speech_fraction": voice.get("speech_fraction")}
    # AU intensities are native OpenFace 0–5 measurements, not probabilities.
    smile = SMILE_THRESHOLDS[LEGACY_METHOD]
    if label == "happy" and up >= smile.upbeat_lip and up > down:
        if up >= smile.exuberant_lip and cheek >= smile.exuberant_cheek:
            return "洋溢", f"愉悦表情，嘴角上提至少 {smile.exuberant_lip:g} 且强于下拉，颊部提升至少 {smile.exuberant_cheek:g}。", values
        return "饱满", f"愉悦表情，嘴角上提至少 {smile.upbeat_lip:g} 且强于下拉。", values
    if label == "sad" and down >= 1 and down > up:
        if down >= 2.5:
            return "非常不好", "低落方向表情，嘴角下拉至少 2.5。", values
        return "略微 down", "低落方向表情，嘴角下拉至少 1 且强于上提。", values
    if label == "neutral" and max(up, down) < 2.5:
        return "平和", "中性表情，上提与下拉均未达明显强度。", values
    return None, "表情类别与面部动作未满足本演示的分档条件。", values


def estimate_observations(modules, duration_ms):
    """Score only the intersection of simultaneous FER, AU and audio observation intervals."""
    facial = {r["start_ms"]: r for r in modules.get("au", {}).get("observations", [])}
    acoustic = {r["start_ms"] // 1000: r for r in modules.get("audio", {}).get("observations", [])}
    segments, evidence = [], []
    for row in modules.get("expression", {}).get("observations", []):
        start = row["start_ms"]
        au = facial.get(start, {})
        voice = acoustic.get(start // 1000, {})
        end = min(row["end_ms"], au.get("end_ms", row["end_ms"]), duration_ms)
        if not (voice.get("start_ms", duration_ms) <= start < voice.get("end_ms", 0)):
            voice = {}
        else:
            end = min(end, voice["end_ms"])
        if end <= start:
            continue
        state, reason, values = _classify(row, au, voice)
        evidence.append(dict(start=start/1000, end=end/1000, state=state, reason=reason, values=values))
        if state:
            if segments and segments[-1]["end"] == start/1000 and segments[-1]["state"] == state:
                segments[-1]["end"] = end/1000
            else:
                segments.append(dict(start=start/1000, end=end/1000, state=state))
    return segments, evidence


def _shift(row, offset, limit):
    return {**row, "start_ms": row["start_ms"] + offset,
            "end_ms": min(limit, row["end_ms"] + offset),
            **({"last_input_ms": min(limit, row["last_input_ms"] + offset)} if "last_input_ms" in row else {})}


def _part_file(source, folder, start, length, has_video):
    # Re-encode on accurate output seeks so inter-frame codecs do not shift the displayed timestamps.
    target = folder / ("clip.mp4" if has_video else "clip.wav")
    args = ["ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe", "-ss", str(start/1000),
            "-i", str(source), "-t", str(length/1000)]
    if has_video:
        args += ["-map","0:v:0","-map","0:a:0?","-vf","scale=w='min(640,iw)':h=-2",
                 "-c:v","libx264","-preset","veryfast","-crf","16","-c:a","aac"]
    else:
        args += ["-vn","-ac","1","-ar","16000","-c:a","pcm_s16le"]
    _run([*args, str(target)], timeout=240)
    return target


def _combine(parts, duration, transcript, *, full_sampling=False):
    """Keep full audio, time-aligned audit samples and bounded face overlays in the session/HTML."""
    from interview_multimodal import speech_units
    modules = {}
    for name in MODULES:
        values = []
        geometry = []
        source = {}
        statuses = []
        summary = {}
        for offset, limit, run in parts:
            mod = run.get("modules", {}).get(name, {})
            if not mod:
                continue
            source = mod
            statuses.append(mod.get("status"))
            # Long recordings retain representative visual rows; scoring uses all native samples before thinning.
            stride = 1 if full_sampling or name == "audio" else max(1, math.ceil(duration/FULL_VISUAL_CACHE_MS))
            values.extend(_shift(r, offset, limit) for r in mod.get("observations", [])[::stride]
                          if r["start_ms"] + offset < limit)
            geom = mod.get("geometry") or {}
            geometry.extend(_shift(r, offset, limit) for r in geom.get("observations", [])
                            if r["start_ms"] + offset < limit)
            if name == "audio":
                sm = mod.get("summary") or {}
                for key in ("speech_ms","pause_ms","pause_count"):
                    summary[key] = summary.get(key, 0) + sm.get(key, 0)
                for key in ("speech_intervals","pause_intervals"):
                    summary.setdefault(key, []).extend(_shift(r, offset, limit) for r in sm.get(key, [])
                                                       if r["start_ms"] + offset < limit)
        if not source:
            continue
        if name == "audio":
            units = speech_units(transcript)
            summary.update(duration_ms=duration,transcript_units=units,
                           speech_rate_units_per_min=round(units*60000/duration,2) if units and duration and summary.get("speech_ms") else None)
        elif len(parts) == 1:
            summary = source.get("summary", {})
        else:
            summary = {"displayed_samples": len(values)}
        status = statuses[0] if len(set(statuses))==1 and len(statuses)==len(parts) else "partial"
        modules[name] = {k: source.get(k) for k in ("model_id","provenance","reason","version")}
        modules[name].update(status=status, observations=values, summary=summary)
        if geometry:
            geom = next(r["modules"][name]["geometry"] for _,_,r in parts if r.get("modules",{}).get(name,{}).get("geometry"))
            stride = max(1, math.ceil(len(geometry)/360))
            modules[name]["geometry"] = {k:geom[k] for k in ("schema_version","analysis_fps","notice") if k in geom}
            modules[name]["geometry"].update(observations=geometry[::stride], sampled_frame_count=len(geometry[::stride]),
                                              display_stride=stride)
    return modules


class AffectAnalysisError(ValueError):
    """A retryable model failure, optionally retaining completed local measurements."""

    def __init__(self, message, *, processing=None, transcript="", provenance=None):
        super().__init__(message)
        self.processing = processing
        self.transcript = transcript
        self.provenance = provenance or {"method": METHOD, "status": "failed"}


def affect_method_fingerprint():
    """Version the fusion policy, native AU window and pinned speech-model assets."""
    from .affect_fusion import POLICY
    from .visual_emotion import AU_METHOD, AU_SOURCE
    from .speech_emotion import METHOD as SPEECH_METHOD, MODEL_ID, FILES
    value = {"method": METHOD, "policy": POLICY, "au_method": AU_METHOD, "au_source": AU_SOURCE,
             "au_window_seconds": .2, "speech_method": SPEECH_METHOD, "speech_model": MODEL_ID,
             "speech_files": FILES, "speech_window_seconds": 5}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _validate_rows(rows, duration):
    previous = 0
    for row in rows:
        left, right = row.get("start_ms"), row.get("end_ms")
        if (type(left) not in (int, float) or type(right) not in (int, float)
                or not math.isfinite(left) or not math.isfinite(right)
                or not 0 <= previous <= left < right <= duration):
            raise ValueError("已保存观测的时间轴不完整或发生重叠。")
        previous = right


def _cached_native_modules(processing, fingerprint, duration, info):
    """Only <=120s old V1 caches are unthinned; failed models are never reusable."""
    audit = processing.get("v1") or {}
    if (duration > FULL_VISUAL_CACHE_MS or audit.get("duration_ms") != duration
            or audit.get("media_sha256") != fingerprint):
        raise ValueError("原片指纹/时长不符，或保存的展示采样已抽稀。")
    modules = audit.get("modules") or {}
    for name, needed in (("au", info["has_video"]), ("audio", info["has_audio"])):
        mod = modules.get(name) or {}
        if mod.get("status") in {"unavailable", "failed", "error"}:
            raise ValueError("上次模型未完成，不能复用为成功观测。")
        rows = mod.get("observations") or []
        if needed and not rows:
            raise ValueError("缺少原始观测，需要重新采集。")
        _validate_rows(rows, duration)
        if name == "audio":
            _validate_rows((mod.get("summary") or {}).get("speech_intervals") or [], duration)
    return copy.deepcopy(modules)


def _validate_speech(module, fingerprint, duration):
    from .speech_emotion import METHOD as SPEECH_METHOD, MODEL_ID, FILES
    provenance = module.get("provenance") or {}
    if (module.get("method") != SPEECH_METHOD or module.get("model_id") != MODEL_ID
            or module.get("duration_ms") != duration or provenance.get("media_sha256") != fingerprint
            or provenance.get("weights_sha256") != FILES["model.pt"]):
        raise AffectAnalysisError("声音情绪结果的模型、原片指纹或时长未通过核对。")
    if module.get("status") not in {"complete", "no_valid_observations", "no_audio"}:
        raise AffectAnalysisError("声音情绪模型尚未完成有效运行，请检查模型安装后重试。")
    _validate_rows(module.get("observations") or [], duration)


def analyze_answer(path, answer):
    """AU+SER production inference on the original timeline, without legacy FER fallback."""
    from .affect_fusion import estimate
    from .visual_emotion import au_expression_evidence
    from .speech_emotion import analyze_speech_emotion, SpeechEmotionError
    path = Path(path).resolve()
    expected = (answer.get("media") or {}).get("sha256")
    with path.open("rb") as f:
        fingerprint = hashlib.file_digest(f, "sha256").hexdigest()
    if expected and expected != fingerprint:
        raise ValueError("原片指纹与本题不一致")
    info = media_info(path)
    duration = round(info["duration_seconds"] * 1000)
    saved = answer.get("processing") or {}
    if (saved.get("media_sha256") == fingerprint and saved.get("duration_seconds") == duration / 1000
            and answer.get("processing_transcribe")):
        processing = copy.deepcopy(saved)
    else:
        processing = analyze_recording(path, transcribe=not bool(answer.get("text") or answer.get("transcript")))
    transcript = answer.get("transcript") or processing.get("transcript") or ""
    processing.pop("affect", None)
    try:
        try:
            native = _cached_native_modules(processing, fingerprint, duration, info)
        except ValueError:
            native = None
        if native is None:
            parts = []
            scratch = LEGACY / "tmp"
            scratch.mkdir(exist_ok=True)
            split = duration > 180000
            for start in range(0, duration, CHUNK_MS if split else duration):
                limit = min(duration, start + CHUNK_MS) if split else duration
                with tempfile.TemporaryDirectory(prefix="v2-mm-", dir=scratch) as tmp:
                    folder = Path(tmp)
                    clip = (_part_file(path, folder, start, limit - start, info["has_video"])
                            if split or path.suffix.lower() not in ALLOWED_SUFFIXES else path)
                    snapshot = {"transcript": "" if split else transcript,
                                "media_sha256": fingerprint if clip == path else None}
                    run = run_multimodal(clip, snapshot, folder, modules=PRODUCTION_MODULES)
                    for name, needed in (("au", info["has_video"]), ("audio", info["has_audio"])):
                        mod = run.get("modules", {}).get(name) or {}
                        if needed and (not mod or mod.get("status") in {"unavailable", "failed", "error"}):
                            label = "OpenFace 面部动作" if name == "au" else "声学采集"
                            raise AffectAnalysisError(label + "未完成，未生成替代评分；请检查本机模型后重试。")
                    parts.append((start, limit, run))
            native = _combine(parts, duration, transcript, full_sampling=True)
            processing["v1"] = {"modules": _combine(parts, duration, transcript),
                "media_sha256": fingerprint, "duration_ms": duration,
                "chunks": [{"start_ms": a, "end_ms": b, "status": r["status"], "run_id": r.get("run_id"),
                            "module_status": {k: v["status"] for k, v in r["modules"].items()}}
                           for a, b, r in parts], "diagnostics": []}
        au = au_expression_evidence(native, duration, window_seconds=.2)
        speech = analyze_speech_emotion(path)
        _validate_speech(speech, fingerprint, duration)
        audio = native.get("audio") or {}
        result = estimate(au, speech, audio, duration, fingerprint)
        processing["affect"] = {"schema_version": 1, "method": METHOD,
            "method_fingerprint": affect_method_fingerprint(), "media_sha256": fingerprint,
            "duration_ms": duration, "modules": {"au": au, "speech": speech, "audio": audio}}
        return {"processing": processing, "state_estimate": result, "transcript": transcript}
    except (AffectAnalysisError, SpeechEmotionError, MediaError, OSError, ImportError, ValueError, RuntimeError) as exc:
        message = str(exc) if isinstance(exc, (AffectAnalysisError, SpeechEmotionError)) else "本机声画分析未完成，请检查模型与采集依赖后重试；未生成替代评分。"
        raise AffectAnalysisError(message, processing=processing, transcript=transcript,
            provenance={"method": METHOD, "status": "failed", "media_sha256": fingerprint,
                        "duration_ms": duration, "error_type": type(exc).__name__}) from exc


def _pack_estimate(fingerprint, duration, segments, evidence):
    # Each classified interval is retained; store one illustrative measurement per contiguous result/reason.
    compact = []
    for row in evidence:
        if compact and (compact[-1]["state"],compact[-1]["reason"],compact[-1]["end"]) == (row["state"],row["reason"],row["start"]):
            compact[-1]["end"] = row["end"]
        else:
            compact.append({**row, "example_start":row["start"], "example_end":row["end"]})
    valid = round(sum(s["end"]-s["start"] for s in segments),3)
    estimate = {"method":LEGACY_METHOD, "source":"local_estimate", "media_sha256":fingerprint, "duration_seconds":duration/1000,
                "segments":segments,"evidence":compact,"reason":RUBRIC,"review_required":True,
                "quality":{"sample_count":len(evidence),"valid_seconds":valid,"coverage":valid/(duration/1000),
                           "unclassified_seconds":round(duration/1000-valid,3)}}
    if not segments:
        estimate["reason"] += " 本题缺可交叉核对的有效时段，请查看质量与采集记录。"
    return estimate


def estimate_legacy_from_cached_answer(answer):
    """Reclassify complete native sampling rows of a previously estimated clip without inference."""
    audit = (answer.get("processing") or {}).get("v1") or {}
    previous = answer.get("state_estimate") or {}
    fingerprint = (answer.get("media") or {}).get("sha256")
    if answer.get("observations"):
        raise ValueError("本题有人工标注，保留教师结果。")
    if not fingerprint or any(record.get("media_sha256") != fingerprint for record in (audit, previous)):
        raise ValueError("原片与已保存的观测、估计指纹不一致。")
    duration = audit.get("duration_ms")
    if not isinstance(duration, int) or duration <= 0 or duration / 1000 != previous.get("duration_seconds"):
        raise ValueError("原片与估计时长不一致。")
    # _combine thins every visual stream for clips longer than 120 s.
    if duration > FULL_VISUAL_CACHE_MS:
        raise ValueError("本题保存的是抽稀后的展示采样，需从原片重新分析。")
    modules = audit.get("modules") or {}
    if any(not (modules.get(name) or {}).get("observations") for name in ("expression", "au", "audio")):
        raise ValueError("缺少重算所需的表情、动作或声音原始采样。")
    segments, evidence = estimate_observations(modules, duration)
    if len(evidence) != (previous.get("quality") or {}).get("sample_count"):
        raise ValueError("当前可对齐的采样数量与原分析记录不符。")
    return _pack_estimate(fingerprint, duration, segments, evidence)


def estimate_from_cached_answer(answer):
    """Recompute only a verified AU+SER cache; old FER caches require real SER inference."""
    from .affect_fusion import estimate
    from .visual_emotion import AU_METHOD
    if answer.get("observations"):
        raise ValueError("本题有人工标注，保留教师结果。")
    cached = (answer.get("processing") or {}).get("affect") or {}
    fingerprint = (answer.get("media") or {}).get("sha256")
    if (not fingerprint or cached.get("media_sha256") != fingerprint or cached.get("schema_version") != 1
            or cached.get("method") != METHOD or cached.get("method_fingerprint") != affect_method_fingerprint()):
        raise ValueError("缺少同版本 AU+声音情绪模型缓存，需运行新算法。")
    duration = cached.get("duration_ms")
    actual_seconds = (answer.get("media") or {}).get("duration_seconds")
    if (type(duration) is not int or duration <= 0
            or (actual_seconds is not None and round(actual_seconds * 1000) != duration)):
        raise ValueError("原片与模型缓存时长不一致。")
    modules = cached.get("modules") or {}
    au, speech, audio = (modules.get(key) or {} for key in ("au", "speech", "audio"))
    if au.get("method") != AU_METHOD:
        raise ValueError("缺少同版本 OpenFace AU 表情证据。")
    _validate_speech(speech, fingerprint, duration)
    _validate_rows(au.get("observations") or [], duration)
    _validate_rows(audio.get("observations") or [], duration)
    _validate_rows((audio.get("summary") or {}).get("speech_intervals") or [], duration)
    return estimate(au, speech, audio, duration, fingerprint)
