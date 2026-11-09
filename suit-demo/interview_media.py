"""Local media validation, existing Whisper ASR and measured audio energy only.

No import of Suit main: its module-level initialization loads emotion classifiers.
All offsets are relative to the submitted recording, never server arrival time.
"""
from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parent
MAX_DURATION_MS = 180000
ALLOWED_SUFFIXES = {'.mp4', '.webm', '.wav', '.m4a', '.mp3', '.ogg'}
_MODEL = None
_MODEL_LOCK = threading.Lock()
_MODEL_SHA = None


class MediaError(ValueError):
    pass


def _run(args, timeout=30):
    exe = shutil.which(args[0])
    if not exe:
        raise MediaError(f'{args[0]} 未找到，请使用项目启动器并检查 FFmpeg。')
    try:
        result = subprocess.run([exe, *args[1:]], capture_output=True,
                                timeout=timeout, check=False,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired as exc:
        raise MediaError('媒体处理超时；原回答保留，可重试。') from exc
    if result.returncode:
        # FFmpeg stderr may contain local paths or untrusted embedded metadata.
        raise MediaError('媒体不能解码，请使用有效的 MP4、WebM 或音频文件。')
    return result.stdout


def inspect_media(path):
    path = Path(path).resolve()
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise MediaError('不支持该媒体扩展名。')
    if not path.is_file() or not 0 < path.stat().st_size <= 100 * 1024 * 1024:
        raise MediaError('媒体须为非空文件且不超过 100 MB。')
    raw = _run(['ffprobe', '-v', 'error', '-protocol_whitelist', 'file,pipe',
                '-show_format', '-show_streams', '-of', 'json', str(path)])
    try:
        info = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MediaError('媒体信息无法读取。') from exc
    streams = info.get('streams', [])
    has_audio = any(s.get('codec_type') == 'audio' for s in streams)
    has_video = any(s.get('codec_type') == 'video' and
                    not s.get('disposition', {}).get('attached_pic') for s in streams)
    if not has_audio and not has_video:
        raise MediaError('媒体没有可用的音频或视频轨道。')
    durations = []
    for part in [info.get('format', {}), *streams]:
        try:
            value = float(part.get('duration', 0))
            if math.isfinite(value) and value > 0:
                durations.append(value)
        except (ValueError, TypeError):
            pass
    # Live MediaRecorder WebM frequently has no duration in its header.
    # Decode at most 181 seconds and read progress, keeping the original file intact.
    if not durations:
        progress = _run(['ffmpeg', '-v', 'error', '-nostdin',
                         '-protocol_whitelist', 'file,pipe', '-i', str(path),
                         '-t', '181', '-progress', 'pipe:1', '-nostats',
                         '-f', 'null', '-'], timeout=120).decode('utf-8', 'replace')
        for line in progress.splitlines():
            if line.startswith('out_time_us='):
                try:
                    durations.append(int(line.split('=', 1)[1]) / 1_000_000)
                except ValueError:
                    pass
    duration_ms = round(max(durations, default=0) * 1000)
    if not 0 < duration_ms <= MAX_DURATION_MS:
        raise MediaError('每段回答须大于 0 秒且不超过 180 秒。')
    return {'duration_ms': duration_ms, 'has_audio': has_audio, 'has_video': has_video,
            'mime_type': mimetypes.guess_type(path.name)[0] or 'application/octet-stream'}


def _load_model():
    global _MODEL, _MODEL_SHA
    with _MODEL_LOCK:
        if _MODEL is None:
            weights = ROOT / 'models' / 'base.pt'
            if not weights.is_file():
                raise MediaError('现有 Whisper base 权重缺失；未下载替代模型。')
            os.environ.setdefault('OMP_NUM_THREADS', '4')
            import torch
            import whisper
            torch.set_num_threads(4)
            with weights.open('rb') as handle:
                _MODEL_SHA = hashlib.file_digest(handle, 'sha256').hexdigest()
            _MODEL = whisper.load_model(str(weights), device='cpu')
    return _MODEL, _MODEL_SHA


def analyze_media(path, language='zh', asr_engine='openai-whisper-base.v1'):
    if language not in {'zh', 'en', 'auto'}:
        raise MediaError('仅支持 zh、en 或 auto 语言设置。')
    started = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat()
    metadata = inspect_media(path)
    result = {'status': 'complete', **metadata,
              'transcription': {'status': 'no_audio', 'text': '', 'segments': []},
              'audio': {'status': 'no_audio', 'windows': []},
              'model_runs': [], 'started_at': started_at}
    if metadata['has_audio']:
        import numpy as np
        pcm = _run(['ffmpeg', '-v', 'error', '-nostdin',
                    '-protocol_whitelist', 'file,pipe', '-i', str(path),
                    '-t', '181', '-vn', '-ac', '1', '-ar', '16000',
                    '-f', 's16le', 'pipe:1'], timeout=120)
        audio = np.frombuffer(pcm, dtype='<i2').astype(np.float32) / 32768.0
        if len(audio) > 16000 * 180 + 160:
            raise MediaError('解码音频超过 180 秒限制。')
        duration = min(metadata['duration_ms'], round(len(audio) / 16))
        windows = []
        for i in range(0, len(audio), 16000):
            sample = audio[i:i + 16000]
            rms = float(np.sqrt(np.mean(sample * sample))) if len(sample) else 0
            windows.append({'start_ms': round(i / 16),
                            'end_ms': min(duration, round((i + len(sample)) / 16)),
                            'rms_dbfs': round(20 * math.log10(max(rms, 1e-8)), 2)})
        result['audio'] = {'status': 'ok' if len(audio) else 'empty',
                           'method': 'PCM mono 16 kHz, non-overlapping 1000 ms RMS',
                           'unit': 'dBFS', 'windows': windows,
                           'meaning': '录音能量观测；不代表情绪、能力或评分。'}
        rms = float(np.sqrt(np.mean(audio * audio))) if len(audio) else 0
        if rms < 0.0001:
            result['transcription']['status'] = 'silent'
        else:
            asr_started = time.perf_counter()
            try:
                from interview_asr import transcribe
                output = transcribe(audio, language, engine=asr_engine)
                provenance = output['provenance']
                model_name, weights_hash = provenance['model'], provenance['model_sha256']
                segments = []
                for item in output.get('segments', []):
                    begin = max(0, min(duration, round(float(item['start']) * 1000)))
                    end = max(begin, min(duration, round(float(item['end']) * 1000)))
                    if end > begin and item.get('text', '').strip():
                        segments.append({'start_ms': begin, 'end_ms': end,
                                         'text': item['text'].strip()})
                text = output.get('text', '').strip()
                result['transcription'] = {'status': 'ok' if text else 'empty', 'text': text,
                    'segments': segments, 'language': output.get('language'),
                    'model': model_name, 'model_sha256': weights_hash,
                    'source': 'asr', 'review_required': True, 'provenance': provenance}
                result['model_runs'].append({'model': model_name, 'provenance': provenance,
                    'weights_sha256': weights_hash, 'status': 'ok' if text else 'empty',
                    'input_start_ms': 0, 'input_end_ms': duration,
                    'last_input_ms': duration,
                    'completed_at': datetime.now(timezone.utc).isoformat(),
                    'elapsed_seconds': round(time.perf_counter() - asr_started, 3)})
            except Exception as exc:
                # Keep useful recording-energy observations even when ASR fails.
                result['status'] = 'partial'
                result['transcription'] = {'status': 'unavailable', 'text': '', 'segments': [],
                    'reason': str(exc) if isinstance(exc, MediaError) else 'Whisper 转写失败；可人工核对或重试。'}
                result['model_runs'].append({'model': asr_engine, 'status': 'failed',
                    'completed_at': datetime.now(timezone.utc).isoformat()})
    result['completed_at'] = datetime.now(timezone.utc).isoformat()
    result['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    return result
