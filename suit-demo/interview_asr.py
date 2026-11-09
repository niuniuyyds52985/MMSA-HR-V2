"""Versioned local ASR engines with verified assets and explicit fallback.

Only model objects are retained between requests. No transcript/audio cache,
remote inference, model download, or cross-answer decoder context is used.
"""
from __future__ import annotations
import importlib.metadata
import threading
import time

from fetch_asr import ASSETS, COMMIT, REPO, verify_assets

LEGACY_ENGINE = 'openai-whisper-base.v1'
FAST_ENGINE = 'faster-whisper-base-int8.v1'
ENGINES = {LEGACY_ENGINE, FAST_ENGINE}
_FAST_MODEL = None
_FAST_HASHES = None
_FAST_LOCK = threading.Lock()
_DECODE = {'beam_size': 1, 'best_of': 5, 'temperature': [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    'compression_ratio_threshold': 2.4, 'log_prob_threshold': -1.0, 'no_speech_threshold': 0.6,
    'condition_on_previous_text': False, 'vad_filter': False, 'word_timestamps': False}


def _load_fast():
    global _FAST_MODEL, _FAST_HASHES
    with _FAST_LOCK:
        cold = _FAST_MODEL is None
        if cold:
            hashes = verify_assets()
            from faster_whisper import WhisperModel
            # A local path plus local_files_only prevents implicit HF downloads.
            _FAST_MODEL = WhisperModel(str(ASSETS), device='cpu', compute_type='int8',
                                      cpu_threads=4, num_workers=1, local_files_only=True)
            _FAST_HASHES = hashes
        return _FAST_MODEL, _FAST_HASHES, cold


def warmup(engine=FAST_ENGINE):
    """Load once without executing participant audio or retaining a transcript."""
    if engine not in ENGINES:
        raise ValueError('unsupported_asr_engine')
    started = time.perf_counter()
    if engine == FAST_ENGINE:
        try:
            _load_fast()
            return {'engine': FAST_ENGINE, 'status': 'ready', 'elapsed_seconds': round(time.perf_counter() - started, 3)}
        except Exception:
            pass
    from interview_media import _load_model
    _load_model()
    return {'engine': LEGACY_ENGINE, 'status': 'ready', 'elapsed_seconds': round(time.perf_counter() - started, 3),
            'fallback': engine == FAST_ENGINE}


def transcribe(audio, language, engine=LEGACY_ENGINE):
    if engine not in ENGINES:
        raise ValueError('unsupported_asr_engine')
    fallback = None
    if engine == FAST_ENGINE:
        try:
            started = time.perf_counter()
            model, hashes, cold = _load_fast()
            loaded = time.perf_counter()
            segments, info = model.transcribe(audio, language=None if language == 'auto' else language, **_DECODE)
            # Fully consume the lazy iterator before reporting success/timing.
            selected = [{'start': segment.start, 'end': segment.end, 'text': segment.text}
                        for segment in segments]
            return {'text': ''.join(item['text'] for item in selected).strip(), 'segments': selected,
                'language': info.language, 'provenance': {'engine': FAST_ENGINE, 'requested_engine': engine,
                    'model': REPO, 'model_sha256': hashes['model.bin'], 'repository_commit': COMMIT,
                    'asset_sha256': hashes, 'backend': 'CTranslate2', 'compute_type': 'int8', 'device': 'cpu',
                    'cpu_threads': 4, 'faster_whisper_version': importlib.metadata.version('faster-whisper'),
                    'ctranslate2_version': importlib.metadata.version('ctranslate2'), 'decoder': dict(_DECODE),
                    'cold_model_load': cold, 'load_seconds': round(loaded - started, 3),
                    'inference_seconds': round(time.perf_counter() - loaded, 3), 'network_used': False,
                    'timestamp_source': 'Whisper segment timestamps; no fabricated word alignment'}}
        except Exception as error:
            # Record category only; third-party exceptions may contain file paths.
            fallback = {'from_engine': FAST_ENGINE, 'to_engine': LEGACY_ENGINE,
                'reason': '快速本地转写不可用；本次显式回退现有 Whisper base。',
                'error_type': type(error).__name__}
    from interview_media import _load_model
    started = time.perf_counter()
    model, digest = _load_model()
    loaded = time.perf_counter()
    result = model.transcribe(audio, language=None if language == 'auto' else language,
                              fp16=False, condition_on_previous_text=False)
    result['provenance'] = {'engine': LEGACY_ENGINE, 'requested_engine': engine, 'model': 'openai-whisper/base',
        'model_sha256': digest, 'backend': 'PyTorch', 'compute_type': 'float32', 'device': 'cpu', 'cpu_threads': 4,
        'decoder': {'fp16': False, 'condition_on_previous_text': False, 'other_options': 'existing openai-whisper defaults'},
        'load_seconds': round(loaded - started, 3), 'inference_seconds': round(time.perf_counter() - loaded, 3),
        'network_used': False, 'timestamp_source': 'Whisper segment timestamps; no fabricated word alignment',
        'fallback': fallback}
    return result
