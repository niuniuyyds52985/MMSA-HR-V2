"""Local AU expression evidence and an independent pretrained VA comparator.

AU patterns are descriptive expression prototypes, not claims about inner state.
No FER label is consulted to accept or reject an AU pattern. Neither interface
assigns the project's five business bands; that mapping belongs to fusion.
"""
from __future__ import annotations

from functools import lru_cache
import copy
import hashlib
import json
import math
from pathlib import Path
import tempfile

from .media import LEGACY, media_info

AU_METHOD = "v2.openface-expression-prototypes.1"
AU_SOURCE = "https://www.isca-archive.org/interspeech_2022/goncalves22_interspeech.pdf"
PATTERNS = {
    "happy": ("AU06", "AU12"),
    "sad": ("AU01", "AU04", "AU15"),
    "surprise": ("AU01", "AU02", "AU05", "AU26"),
    "anger": ("AU04", "AU05", "AU07", "AU23"),
}
LOW_ACTIVATION_AUS = ("AU04", "AU06", "AU09", "AU10", "AU12", "AU15", "AU23")
BAD_FLAGS = {"repeated_identical_frames", "low_light", "overexposed"}
VISUAL_METHOD = "v2.emotieffnet-va.1"
MODEL_ID = "EmotiEffLib/enet_b0_8_va_mtl"
MODEL_HASH = "c43e056ad388d4a8dc911832b8291435b2af537f967e5870ebd731574ec7e812"
MODEL_REVISION = "520a051c64cd191521e5934655314e769a319684"
MODEL_DIR = LEGACY / "models" / "emotieffnet"
LABELS = ("anger", "contempt", "disgust", "fear", "happy", "neutral", "sad", "surprise")


def _finite(value, low, high):
    return type(value) in (int, float) and math.isfinite(value) and low <= value <= high


def au_expression_evidence(modules, duration_ms, window_seconds=5):
    """Aggregate native OpenFace *_c and *_r samples in disjoint time windows.

    `matched_fraction` is matched observed seconds / valid observed seconds,
    never a calibrated probability. `strength` is the mean weakest required AU
    intensity / 5 among matched frames; it is muscle activation, not emotion
    intensity. Missing columns are unavailable, rather than absent activation.
    """
    if type(duration_ms) is not int or duration_ms <= 0:
        raise ValueError("duration_ms must be positive")
    if not _finite(window_seconds, .2, 60):
        raise ValueError("invalid window duration")
    step = round(window_seconds * 1000)
    rows = (modules.get("au") or {}).get("observations") or []
    previous_end = 0
    for row in rows:
        start, end = row.get("start_ms"), row.get("end_ms")
        if not (_finite(start, 0, duration_ms) and _finite(end, 0, duration_ms)
                and previous_end <= start < end):
            raise ValueError("invalid or overlapping OpenFace observation intervals")
        previous_end = end
    output = []
    for start in range(0, duration_ms, step):
        end = min(start + step, duration_ms)
        measured = 0.0
        intensity_sums, intensity_times, presence_sums, presence_times = {}, {}, {}, {}
        patterns = {name: {"aus": list(aus), "valid_ms": 0, "matched_ms": 0,
                           "strength_sum": 0.0} for name, aus in PATTERNS.items()}
        quiet_ms = quiet_valid_ms = smile_ms = smile_valid_ms = 0
        for row in rows:
            ms = min(end, row["end_ms"]) - max(start, row["start_ms"])
            if ms <= 0 or row.get("quality") != "valid" or BAD_FLAGS.intersection(row.get("quality_flags", [])):
                continue
            values = row.get("values") or {}
            intensities, presence = values.get("intensity") or {}, values.get("presence") or {}
            if not intensities or not presence:
                continue
            measured += ms
            for key, value in intensities.items():
                if _finite(value, 0, 5):
                    intensity_sums[key] = intensity_sums.get(key, 0) + ms * value
                    intensity_times[key] = intensity_times.get(key, 0) + ms
            for key, value in presence.items():
                if _finite(value, 0, 1) and value in (0, 1):
                    presence_sums[key] = presence_sums.get(key, 0) + ms * value
                    presence_times[key] = presence_times.get(key, 0) + ms
            for name, aus in PATTERNS.items():
                if not all(_finite(presence.get(k), 0, 1) and presence[k] in (0, 1)
                           and _finite(intensities.get(k), 0, 5) for k in aus):
                    continue
                item = patterns[name]
                item["valid_ms"] += ms
                if all(presence[k] == 1 for k in aus):
                    item["matched_ms"] += ms
                    item["strength_sum"] += ms * min(intensities[k] for k in aus) / 5
            if all(_finite(presence.get(k), 0, 1) and presence[k] in (0, 1) for k in LOW_ACTIVATION_AUS):
                quiet_valid_ms += ms
                quiet_ms += ms * int(all(presence[k] == 0 for k in LOW_ACTIVATION_AUS))
            if _finite(presence.get("AU12"), 0, 1) and presence["AU12"] in (0, 1):
                smile_valid_ms += ms
                smile_ms += ms * presence["AU12"]
        for item in patterns.values():
            valid, matched = item.pop("valid_ms"), item.pop("matched_ms")
            strength_sum = item.pop("strength_sum")
            item.update(valid_seconds=round(valid / 1000, 6), active_seconds=round(matched / 1000, 6),
                        matched_fraction=matched / valid if valid else None,
                        strength=strength_sum / matched if matched else (0.0 if valid else None),
                        source=AU_SOURCE, source_section="3.3, Table 1")
        mean = {k: intensity_sums[k] / intensity_times[k] for k in intensity_sums}
        output.append({"start_ms": start, "end_ms": end, "quality": "valid" if measured else "missing",
                       "coverage": measured / (end - start), "valid_seconds": measured / 1000,
                       "values": {"mean_intensity": mean,
                                  "presence_fraction": {k: presence_sums[k] / presence_times[k] for k in presence_sums},
                                  "patterns": patterns,
                                  "smile_motion": {"au": "AU12", "occurrence_fraction": smile_ms / smile_valid_ms if smile_valid_ms else None,
                                                   "mean_intensity": mean.get("AU12"), "interpretation": "嘴角上提动作；单独不等同真实开心"},
                                  "low_activation_fraction": quiet_ms / quiet_valid_ms if quiet_valid_ms else None,
                                  "low_activation_aus": list(LOW_ACTIVATION_AUS)},
                       "reason": None if measured else "本时窗没有质量合格的 OpenFace AU 原始观测。"})
    upstream = modules.get("au") or {}
    upstream_provenance = copy.deepcopy(upstream.get("provenance") or {})
    return {"module": "au_expression", "method": AU_METHOD,
            "model_id": (upstream.get("model_id") or "OpenFace") + " + AU prototypes",
            "status": "complete" if rows and all(r["coverage"] == 1 for r in output) else "partial" if any(r["valid_seconds"] for r in output) else "unavailable",
            "observations": output,
            "provenance": {**upstream_provenance, "upstream_model_id": upstream.get("model_id"),
                           "upstream": upstream_provenance, "source": AU_SOURCE, "source_section": "3.3, Table 1", "input": "OpenFace AU occurrence (_c) and intensity (_r)",
                           "window_seconds": window_seconds, "no_fer_gate": True,
                           "strength_definition": "matched-frame mean of min(required AU intensities)/5; descriptive muscle activation, not emotion intensity",
                           "low_activation_definition": "all listed AU occurrence outputs absent; project descriptive measure, not an emotion label"}}


@lru_cache(maxsize=1)
def _model():
    import onnxruntime as ort
    model = MODEL_DIR / "model.onnx"
    manifest = json.loads((MODEL_DIR / "source.json").read_text(encoding="utf-8"))
    if manifest.get("revision") != MODEL_REVISION or hashlib.sha256(model.read_bytes()).hexdigest() != MODEL_HASH:
        raise ValueError("EmotiEffNet model fingerprint mismatch")
    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 1
    return ort.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])


def _predict_face(rgb):
    """Official ONNX preprocessing: RGB crop, resize 224, ImageNet normalization."""
    import cv2
    import numpy as np
    x = cv2.resize(rgb, (224, 224)).astype("float32") / 255
    x = (x - np.asarray([.485, .456, .406], dtype="float32")) / np.asarray([.229, .224, .225], dtype="float32")
    model = _model()
    result = model.run(None, {model.get_inputs()[0].name: x.transpose(2, 0, 1)[None]})[0]
    result = np.asarray(result).reshape(-1)
    if result.shape != (10,) or not np.isfinite(result).all():
        raise ValueError("unexpected EmotiEffNet output")
    scores = np.exp(result[:8] - result[:8].max())
    scores /= scores.sum()
    return {"valence": float(result[-2]), "arousal": float(result[-1]),
            "class_scores": {name: float(value) for name, value in zip(LABELS, scores)},
            "label": LABELS[int(result[:8].argmax())]}


def analyze_visual_emotion(path, duration_ms=None, sample_fps=1):
    """Independent frame-crop VA comparator; no raw media leaves this computer.

    This does not replace OpenFace or reinterpret landmarks as a VA model.
    Frame predictions describe sampled windows, not continuous camera truth.
    """
    import cv2
    from interview_visual import _load_detector
    from interview_media import _run
    path = Path(path)
    info = media_info(path)
    duration = duration_ms or round(info["duration_seconds"] * 1000)
    result = {"module": "visual_emotion", "method": VISUAL_METHOD, "model_id": MODEL_ID,
              "status": "unavailable", "observations": [],
              "provenance": {"model_sha256": MODEL_HASH, "revision": MODEL_REVISION,
                             "repository": "https://github.com/sb-ai-lab/EmotiEffLib",
                             "execution": "local CPU ONNX", "sample_fps": sample_fps,
                             "outputs": "8 class softmax scores (not calibrated intensity); separate learned valence/arousal regression",
                             "preprocessing": "YuNet single face bounding box; RGB crop; 224x224; ImageNet normalization"}}
    if not info["has_video"]:
        result["reason"] = "没有视频轨道。"
        return result
    if sample_fps not in (1, 2, 5):
        raise ValueError("unsupported visual sampling rate")
    _model()
    detector, why = _load_detector(cv2)
    if detector is None:
        result["reason"] = why
        return result
    scratch = LEGACY / "tmp"
    scratch.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="visual-emotion-", dir=scratch) as tmp:
        prepared = Path(tmp) / "sample.avi"
        _run(["ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe", "-i", str(path),
              "-t", str(duration / 1000), "-vf", f"fps={sample_fps},scale=w='min(640,iw)':h=-2", "-an", "-c:v", "mjpeg", "-q:v", "3", str(prepared)], timeout=120)
        capture = cv2.VideoCapture(str(prepared))
        try:
            i = 0
            while True:
                ok, frame = capture.read()
                if not ok:
                    break
                start, end = i * 1000 // sample_fps, min(duration, (i + 1) * 1000 // sample_fps)
                i += 1
                if start >= end:
                    break
                detector.setInputSize((frame.shape[1], frame.shape[0]))
                _, faces = detector.detect(frame)
                faces = [] if faces is None else [f for f in faces if float(f[14]) >= .9]
                values, reason = {}, None
                if len(faces) != 1:
                    reason = "未检出单个可靠人脸。"
                else:
                    x, y, w, h = (float(v) for v in faces[0][:4])
                    if min(w, h) < 40:
                        reason = "人脸像素不足。"
                    else:
                        crop = frame[max(0, int(y)):min(frame.shape[0], int(y+h)), max(0, int(x)):min(frame.shape[1], int(x+w))]
                        brightness = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).mean() if crop.size else 0
                        if brightness < 20 or brightness > 245:
                            reason = "面部亮度不足或过曝。"
                        else:
                            values = _predict_face(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB))
                result["observations"].append({"start_ms": start, "end_ms": end, "values": values,
                                               "quality": "missing" if reason else "valid", "reason": reason})
        finally:
            capture.release()
    valid = sum(r["end_ms"]-r["start_ms"] for r in result["observations"] if r["quality"] == "valid")
    result.update(status="complete" if valid == duration else "partial" if valid else "unavailable",
                  summary={"valid_seconds": valid / 1000, "coverage": valid / duration})
    return result
