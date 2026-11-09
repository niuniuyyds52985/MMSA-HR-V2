"""Local emotion2vec+ speech categories, independent of facial classifications.

The normalized class scores are model support, NOT calibrated emotion intensity.
No network calls or implicit downloads occur during inference.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys
from threading import RLock

PROJECT = Path(__file__).resolve().parents[2]
MODEL_DIR = PROJECT / "suit-demo/models/emotion2vec-plus-base"
DEPENDENCIES = PROJECT / ".runtime/emotion2vec"
MODEL_ID = "iic/emotion2vec_plus_base"
METHOD = "v2.emotion2vec-plus-base.local.1"
LABELS = ("angry", "disgusted", "fearful", "happy", "neutral", "other", "sad", "surprised", "unknown")
FILES = {
    "model.pt": "60710b5aae1dbe69bdac8920028fb05882d4314fd09031922b4b61ee9e7aadbd",
    "config.yaml": "50674ec187406b4f1208925594ceaf1f28f16a22333b5f2ac5b3d0cbffdd670e",
    "tokens.txt": "654625c18ef0f0583441a3b811c20904f2cf30febb716f32e14810b8e6bb715c",
}
_LOCK = RLock()
_MODEL = None


class SpeechEmotionError(ValueError):
    pass


def _load_model():
    global _MODEL
    with _LOCK:
        if _MODEL is not None:
            return _MODEL
        for name, expected in FILES.items():
            path = MODEL_DIR / name
            if not path.is_file():
                raise SpeechEmotionError("本机声音情绪模型未安装；请运行 scripts/prepare_speech_emotion.py。")
            with path.open("rb") as handle:
                actual = hashlib.file_digest(handle, "sha256").hexdigest()
            if actual != expected:
                raise SpeechEmotionError("声音情绪模型文件校验不符，未进行推理。")
        if DEPENDENCIES.is_dir() and str(DEPENDENCIES) not in sys.path:
            sys.path.insert(0, str(DEPENDENCIES))
        try:
            import torch
            from omegaconf import OmegaConf
            from .vendor.emotion2vec.model import Emotion2vec
        except (ImportError, OSError) as exc:
            raise SpeechEmotionError("本机声音情绪模型依赖不可用，请运行安装脚本后重试。") from exc
        torch.set_num_threads(2)
        conf = OmegaConf.load(MODEL_DIR / "config.yaml")
        model = Emotion2vec(model_conf=conf["model_conf"], vocab_size=len(LABELS))
        # The upstream fine-tuned checkpoint omits the pretraining reconstruction
        # decoder; extract_features never calls it. Remove it instead of retaining
        # the upstream loader's randomly initialized, unused decoder parameters.
        model.modality_encoders["AUDIO"].decoder = None
        state = torch.load(MODEL_DIR / "model.pt", map_location="cpu", weights_only=True)
        for key in ("state_dict", "model_state_dict", "model"):
            if key in state:
                state = state[key]
        expected = model.state_dict()
        mapped = {}
        for key, value in expected.items():
            source = next((prefix + key for prefix in ("d2v_model.", "module.", "") if prefix + key in state), None)
            if source is None or state[source].shape != value.shape:
                raise SpeechEmotionError("声音情绪模型参数不完整或形状不匹配，未使用随机参数。")
            mapped[key] = state[source]
        model.load_state_dict(mapped, strict=True)
        model.eval()
        _MODEL = model
        return model


def predict_samples(samples):
    """Return nine class scores for nonempty mono 16kHz float32 audio."""
    import numpy as np
    samples = np.asarray(samples, dtype=np.float32)
    if samples.ndim != 1 or len(samples) < 1600 or not np.isfinite(samples).all():
        raise SpeechEmotionError("声音情绪输入须为有效的16kHz单声道片段。")
    with _LOCK:
        model = _load_model()
        import torch
        import torch.nn.functional as F
        with torch.inference_mode():
            source = torch.from_numpy(samples.copy())
            if model.cfg.normalize:
                source = F.layer_norm(source, source.shape)
            features = model.extract_features(source.view(1, -1), padding_mask=None)["x"]
            logits = model.proj(features.mean(dim=1))
            scores = torch.softmax(logits, dim=-1)[0].tolist()
    if len(scores) != len(LABELS) or not all(math.isfinite(x) and 0 <= x <= 1 for x in scores):
        raise SpeechEmotionError("声音情绪模型返回了无效分类分数。")
    return dict(zip(LABELS, (float(x) for x in scores)))


def _voice_quality(samples, start_ms=0):
    import numpy as np
    import webrtcvad
    samples = np.asarray(samples, dtype=np.float32)
    rms = float(np.sqrt(np.mean(samples ** 2))) if len(samples) else 0.0
    rms_dbfs = 20 * math.log10(max(rms, 1e-8))
    clipping_fraction = float(np.mean(np.abs(samples) >= 0.999)) if len(samples) else 0.0
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    vad = webrtcvad.Vad(2)
    intervals = []
    for at in range(0, len(pcm) - 479, 480):
        if vad.is_speech(pcm[at:at + 480].tobytes(), 16000):
            left, right = start_ms + at * 1000 // 16000, start_ms + (at + 480) * 1000 // 16000
            if intervals and intervals[-1]["end_ms"] == left:
                intervals[-1]["end_ms"] = right
            else:
                intervals.append({"start_ms": left, "end_ms": right})
    speech_ms = sum(i["end_ms"] - i["start_ms"] for i in intervals)
    fraction = speech_ms / (len(samples) / 16) if len(samples) else 0
    flags = []
    if rms_dbfs < -60:
        flags.append("near_silence")
    if clipping_fraction > .01:
        flags.append("audio_clipping")
    if speech_ms < 240 or fraction < .1:
        flags.append("insufficient_speech")
    return {"rms_dbfs": round(rms_dbfs, 3), "speech_fraction": round(fraction, 6),
            "speech_ms": speech_ms, "clipping_fraction": round(clipping_fraction, 6)}, intervals, flags


def analyze_speech_emotion(path, *, window_seconds=5):
    """Separate 5-second windows; explicit VAD intervals permit exact fusion intersections."""
    from .media import media_info, _audio_samples
    if type(window_seconds) is not int or not 1 <= window_seconds <= 15:
        raise ValueError("声音情绪时间窗须为1至15秒的整数。")
    path = Path(path)
    info = media_info(path)
    with path.open("rb") as handle:
        media_hash = hashlib.file_digest(handle, "sha256").hexdigest()
    provenance = {"source": "https://www.modelscope.cn/models/iic/emotion2vec_plus_base",
        "weights_sha256": FILES["model.pt"], "architecture": "FunASR 1.4.24 pinned MIT subset",
        "input": "16kHz mono; official layer_norm; feature mean; classifier softmax",
        "output_scale": "normalized class support, not calibrated probability or emotion intensity",
        "window_seconds": window_seconds, "network_used": False, "media_sha256": media_hash}
    if not info["has_audio"]:
        return {"model_id": MODEL_ID, "version": METHOD, "method": METHOD, "status": "no_audio",
                "duration_ms": round(info["duration_seconds"] * 1000), "provenance": provenance,
                "reason": "录制没有音轨。", "observations": []}
    samples = _audio_samples(path, info["duration_seconds"]).astype("float32") / 32768
    rows = []
    for start in range(0, len(samples), window_seconds * 16000):
        end = min(len(samples), start + window_seconds * 16000)
        sample = samples[start:end]
        left, right = start * 1000 // 16000, end * 1000 // 16000
        values, intervals, flags = _voice_quality(sample, left)
        row = {"start_ms": left, "end_ms": right, "last_input_ms": right,
               "quality": "missing" if flags else "valid", "quality_flags": flags,
               "speech_intervals": intervals, "values": values}
        if not flags:
            scores = predict_samples(sample)
            row["values"].update(label=max(scores, key=scores.get), scores=scores)
        else:
            row["reason"] = "静音、削波或有效发声不足，本窗口没有情绪推断。"
        rows.append(row)
    return {"model_id": MODEL_ID, "version": METHOD, "method": METHOD,
            "duration_ms": round(info["duration_seconds"] * 1000),
            "status": "complete" if any(r["quality"] == "valid" for r in rows) else "no_valid_observations",
            "provenance": provenance,
            "observations": rows,
            "summary": {"windows": len(rows), "valid_windows": sum(r["quality"] == "valid" for r in rows),
                        "speech_ms": sum(r["values"]["speech_ms"] for r in rows)}}
