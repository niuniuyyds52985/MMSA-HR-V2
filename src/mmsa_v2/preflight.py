"""Bounded, local-only recording checks before a scored interview.

An energy meter cannot distinguish speech from hum. This check combines signal
quality with local ASR and still asks the participant to listen back. Trials are
temporary inputs owned by the API caller; this module never stores them.
"""
from pathlib import Path
import math

from .media import _audio_samples, _SLOTS, media_info


METHOD = "local-recording-preflight.v1"
MAX_BYTES = 5 * 1024 * 1024
MAX_SECONDS = 15
SAMPLE_RATE = 16000


def _transcribe(signal):
    from interview_asr import FAST_ENGINE, transcribe
    return transcribe(signal, "zh", FAST_ENGINE)


def _metrics(samples):
    import numpy as np
    import webrtcvad

    signal = samples.astype("float32") / 32768
    rms = float(np.sqrt(np.mean(signal ** 2)))
    window = np.hanning(8192)
    spectra = []
    for start in range(0, max(1, len(signal) - 8192 + 1), 4096):
        block = signal[start:start + 8192]
        if len(block) < 8192:
            block = np.pad(block, (0, 8192 - len(block)))
        spectra.append(np.abs(np.fft.rfft(block * window)) ** 2)
    power = np.mean(spectra, axis=0)
    frequency = np.fft.rfftfreq(8192, 1 / SAMPLE_RATE)
    audible = (frequency >= 20) & (frequency <= 7600)
    total = float(power[audible].sum())
    peak = int(np.argmax(np.where(audible, power, 0)))
    ratio = lambda mask: float(power[mask].sum() / total) if total else 0.0
    pcm = samples.astype("<i2", copy=False).tobytes()
    frames = [pcm[i:i + 640] for i in range(0, len(pcm) - 639, 640)]
    vad = webrtcvad.Vad(2)
    speech = sum(vad.is_speech(frame, SAMPLE_RATE) for frame in frames)
    return signal, {"rms_dbfs": round(20 * math.log10(max(rms, 1e-8)), 2),
                    "low_frequency_ratio": round(ratio((frequency >= 20) & (frequency < 160)), 4),
                    "speech_band_ratio": round(ratio((frequency >= 160) & (frequency <= 5000)), 4),
                    "spectral_peak_fraction": round(float(power[max(0, peak - 2):peak + 3].sum() / total) if total else 0.0, 4),
                    "dominant_frequency_hz": round(float(frequency[peak]), 1),
                    "clipping_fraction": round(float((np.abs(signal) >= .98).mean()), 4),
                    "speech_fraction": round(speech / len(frames), 4) if frames else 0.0}


def inspect_trial(path):
    path = Path(path)
    if not path.is_file() or not 0 < path.stat().st_size <= MAX_BYTES:
        raise ValueError("设备试录须为非空且不超过 5 MB")
    info = media_info(path)
    if not info["duration_seconds"] <= MAX_SECONDS:
        raise ValueError("设备试录最长 15 秒，请使用 5 秒试录重新检查")
    result = {"method": METHOD, "status": "fail", "messages": [], "codes": [], "transcript": "",
              "duration_seconds": info["duration_seconds"], "metrics": {}, "local_only": True,
              "network_used": False, "scored": False}
    if not info["has_audio"]:
        result.update(messages=["试录没有音轨。请选择麦克风后重新试录。"], codes=["no_audio"])
        return result
    if info["duration_seconds"] < 2:
        result.update(messages=["试录过短。请完整说一句话，至少录制 2 秒。"], codes=["too_short"])
        return result
    if not _SLOTS.acquire(blocking=False):
        raise ValueError("本机正在处理另一段录制，请稍后重新检查设备")
    try:
        signal, metrics = _metrics(_audio_samples(path, info["duration_seconds"]))
        result["metrics"] = metrics
        failures, warnings = [], []
        if metrics["rms_dbfs"] < -60:
            failures.append(("near_silence", "音轨接近静音。请检查麦克风选择、静音开关和输入音量。"))
        if metrics["clipping_fraction"] > .05:
            failures.append(("clipping", "录音存在明显削波失真。请降低输入增益并重新试录。"))
        elif metrics["clipping_fraction"] > .01:
            warnings.append(("clipping_warning", "部分录音接近削波，请回放确认声音没有破音。"))
        if metrics["low_frequency_ratio"] >= .80:
            failures.append(("low_frequency_noise", "音轨以低频嗡声为主，音量变化不能证明人声清楚。请更换麦克风或检查连接后重试。"))
        elif metrics["low_frequency_ratio"] >= .65:
            warnings.append(("low_frequency_warning", "低频干扰较多，请回放确认人声清楚，必要时更换麦克风。"))
        if metrics["spectral_peak_fraction"] >= .65:
            failures.append(("narrowband_tone", "音轨主要是持续单音或啸声，尚未验证到可辨人声。请说完整一句话后重新试录。"))
        if metrics["speech_band_ratio"] < .20 and metrics["rms_dbfs"] >= -60:
            failures.append(("speech_band_missing", "音轨缺少足够的语音频段，可能存在嗡声或高频干扰。请检查输入设备后重新试录。"))
        if failures:
            result.update(messages=[x[1] for x in failures], codes=[x[0] for x in failures])
            return result
        try:
            output = _transcribe(signal)
            transcript = (output.get("text") or "".join(s.get("text", "") for s in output.get("segments", []))).strip()
            result["transcript"] = transcript[:1000]
            result["asr_engine"] = (output.get("provenance") or {}).get("engine", "local-whisper")
        except (RuntimeError, OSError, ValueError, ImportError):
            warnings.append(("local_asr_unavailable", "本机语音识别暂不可用，尚未验证人声内容。请回放并亲自确认听得清楚，或改用文字/已有文件作答。"))
            result.update(status="warning", messages=[x[1] for x in warnings], codes=[x[0] for x in warnings])
            return result
        if sum(c.isalnum() for c in transcript) < 2:
            result.update(messages=["本机未识别出有效原话。请先回放检查人声、确认麦克风并重新试录；也可改用文字或已有文件作答。"], codes=["empty_transcription"])
            return result
        result.update(status="warning" if warnings else "pass",
                      messages=[x[1] for x in warnings] + ["本机已识别出试录文字。请回放，确认人声清楚且文字与实际所说内容相符。"],
                      codes=[x[0] for x in warnings])
        return result
    finally:
        _SLOTS.release()
