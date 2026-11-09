"""Audio-only Bailian fallback when a local transcript is unavailable.

The existing user-bound API credential remains bound to its exact base URL.
No video, resume, rubric, or interview instructions are sent to this endpoint.
Qwen-ASR returns text without word timestamps; chunk bounds are not alignments.
API: https://help.aliyun.com/zh/model-studio/qwen-asr-api-reference
"""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from hashlib import sha256
import io
from pathlib import Path
import re
import wave

import requests

from .media import _audio_samples, media_info
from .cloud_policy import ASR_MODEL, CloudPolicyError, validate_endpoint
from interview_credentials import CredentialError, get_api_key, public_settings


MODEL = ASR_MODEL
METHOD = "v2.bailian-asr.audio-only.1"
SAMPLE_RATE = 16000
CHUNK_SECONDS = 180
MAX_ENCODED_BYTES = 10 * 1024 * 1024
REQUEST_TIMEOUT = (8, 120)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class CloudASRError(ValueError):
    """Safe, user-facing error. Never include provider bodies or request headers."""
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def _base_url():
    try:
        return validate_endpoint(public_settings().get("base_url"))
    except CloudPolicyError as exc:
        raise CloudASRError(exc.code, str(exc)) from None
    except (CredentialError, OSError):
        raise CloudASRError("credentials_unavailable", "本机百炼凭据暂不可读取，原始录制已保留。") from None


def _wav_bytes(samples):
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(SAMPLE_RATE)
        output.writeframes(samples.astype("<i2", copy=False).tobytes())
    return target.getvalue()


def _safe_usage(value):
    """Keep numeric metering only, never arbitrary provider strings."""
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "seconds"):
        item = value.get(key)
        if type(item) in (int, float) and 0 <= item < 10**12:
            result[key] = item
    return result


def _identifier(value):
    return value if isinstance(value, str) and re.fullmatch(r"[\w.:-]{1,200}", value) else ""


def _request(wav, base_url, api_key):
    try:
        base_url = validate_endpoint(base_url)
    except CloudPolicyError as exc:
        raise CloudASRError(exc.code, str(exc)) from None
    audio = base64.b64encode(wav).decode("ascii")
    if len(audio) > MAX_ENCODED_BYTES:
        raise CloudASRError("audio_too_large", "单个语音片段超过百炼输入限制。")
    body = {"model": MODEL, "stream": False,
            "messages": [{"role": "user", "content": [{"type": "input_audio", "input_audio": {
                "data": "data:audio/wav;base64," + audio}}]}],
            "asr_options": {"enable_itn": False}}
    try:
        response = requests.post(base_url + "/chat/completions", json=body,
                                 headers={"Authorization": "Bearer " + api_key},
                                 timeout=REQUEST_TIMEOUT, allow_redirects=False)
        if response.status_code != 200:
            code = response.status_code
            if code in (401, 403):
                raise CloudASRError("authorization_failed", "百炼拒绝语音识别请求，请核对密钥权限与模型服务。")
            if code == 429:
                raise CloudASRError("rate_limited", "百炼语音识别暂时限流，原始录制已保留，可稍后重试。")
            raise CloudASRError("provider_http_error", f"百炼语音识别返回 HTTP {code}，原始录制已保留。")
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise CloudASRError("invalid_response", "百炼语音识别返回内容超过限制。")
        value = response.json()
    except requests.Timeout:
        raise CloudASRError("timeout", "百炼语音识别超时，原始录制已保留，可稍后重试。") from None
    except requests.RequestException:
        raise CloudASRError("network_error", "暂时无法连接百炼语音识别，原始录制已保留。") from None
    except (ValueError, TypeError) as exc:
        if isinstance(exc, CloudASRError):
            raise
        raise CloudASRError("invalid_response", "百炼语音识别返回了无法解析的结果。") from None
    try:
        choice = value["choices"][0]
        text = choice["message"]["content"]
        if choice.get("finish_reason") != "stop" or not isinstance(text, str) or len(text) > 25000:
            raise ValueError()
    except (KeyError, IndexError, TypeError, ValueError):
        raise CloudASRError("invalid_response", "百炼语音识别没有返回完整、有效的转写文本。") from None
    return {"text": text.strip(), "request_id": _identifier(value.get("id")),
            "model": _identifier(value.get("model")) or MODEL,
            "usage": _safe_usage(value.get("usage"))}


def transcribe_answer(path, answer):
    """Return transcript/provenance/chunks; no database mutation and no retries.

    ``status=empty`` is a genuine completed request with no recognized text. It
    must remain a transcription gap, never evidence of low competency. Chunk
    ``start/end`` values only describe submitted audio bounds, not speech timing.
    Errors abort the result rather than silently scoring a partial transcription.
    """
    path = Path(path)
    info = media_info(path)
    if not info["has_audio"]:
        raise CloudASRError("no_audio", "本题录制没有音轨，无法进行语音转写。")
    actual_sha = sha256(path.read_bytes()).hexdigest()
    expected_sha = (answer.get("media") or {}).get("sha256")
    if expected_sha and expected_sha != actual_sha:
        raise CloudASRError("media_changed", "本题原片与保存的校验值不一致，未发送云端转写。")
    base_url = _base_url()
    try:
        api_key = get_api_key(base_url)
    except (CredentialError, OSError):
        raise CloudASRError("credentials_unavailable", "本机百炼凭据暂不可读取，原始录制已保留。") from None
    seconds = info["duration_seconds"]
    samples = _audio_samples(path, seconds)
    chunks = []
    for start in range(0, len(samples), CHUNK_SECONDS * SAMPLE_RATE):
        end = min(len(samples), start + CHUNK_SECONDS * SAMPLE_RATE)
        wav = _wav_bytes(samples[start:end])
        item = _request(wav, base_url, api_key)
        chunks.append({"start": start / SAMPLE_RATE, "end": end / SAMPLE_RATE,
                       "alignment": "audio_chunk_only", "audio_sha256": sha256(wav).hexdigest(), **item})
    transcript = "\n".join(c["text"] for c in chunks if c["text"]).strip()
    if len(transcript) > 25000:
        raise CloudASRError("transcript_too_long", "本题转写超过文本评分长度限制，需复核原片。")
    status = "success" if transcript else "empty"
    return {"transcript": transcript, "status": status, "chunks": chunks,
            "provenance": {"provider": "aliyun-model-studio", "model": MODEL, "method": METHOD,
                           "media_sha256": actual_sha, "audio_only": True,
                           "duration_seconds": seconds, "created_at": datetime.now(timezone.utc).isoformat(),
                           "status": status, "word_timestamps_available": False,
                           "requests": [{k: c[k] for k in ("request_id", "model", "usage", "audio_sha256")} for c in chunks]}}
