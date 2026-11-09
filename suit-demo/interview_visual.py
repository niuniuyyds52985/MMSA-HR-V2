"""On-demand original-video thumbnails and measured capture quality only.

No identity, expression, gaze, emotion, personality, integrity, or scoring model
is imported or called. This cache is deliberately separate from interview jobs,
reports, decisions, and session versions.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid

from interview_media import MediaError, inspect_media

ALGORITHM_VERSION = 'visual-quality.v2'
MODEL_PATH = Path(__file__).resolve().parent / 'models' / 'face_detection_yunet_2023mar.onnx'
MODEL_SHA256 = '8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4'
SCORE_THRESHOLD = 0.9
MAX_FRAMES = 60
MIN_INTERVAL_MS = 3000
THUMBNAIL_EDGE = 640
MAX_SOURCE_EDGE = 4096
MAX_SOURCE_PIXELS = 4096 * 2160
MAX_ANALYSIS_SECONDS = 25
_LOCKS = [threading.Lock() for _ in range(32)]
_DIGEST_LOCK = threading.Lock()
_SOURCE_DIGESTS = {}

NOTICES = [
    '仅展示授权原片的抽样画面和采集质量；不进行身份识别、表情、眼神、情绪、人格、心理或诚信推断。',
    '框为本地 YuNet 检测器找到的候选人脸区域；未检出不代表画面无人脸，检出也不证明身份。仅保留框，不保留人脸关键点。',
    '亮度为整张缩略帧灰度均值（0–255）；清晰度读数为整帧拉普拉斯方差（灰度²）。两者是原始读数，不是能力或心理指标。',
    '最多抽样60帧，未抽到的画面不在统计范围内；缩略图尺寸和压缩会影响质量读数。此结果不进入评分或追问决定。',
]


class VisualError(ValueError):
    pass


def _empty(status='failed', reason=None):
    return {'status': status, 'reason': reason, 'algorithm_version': ALGORITHM_VERSION,
            'analysis_scope': 'capture_quality_only', 'video': None,
            'detector': {'name': 'opencv_yunet_2023mar', 'status': 'not_run', 'reason': None,
                         'model_sha256': None},
            'sampling': {'max_frames': MAX_FRAMES, 'interval_ms': None,
                         'requested_frame_count': 0, 'decoded_frame_count': 0},
            'summary': {'sampled_frame_count': 0, 'frames_with_face': None,
                        'mean_brightness': None, 'mean_laplacian_variance': None},
            'metrics': {'brightness_mean': {'unit': 'gray_level_0_255', 'scope': 'whole_thumbnail'},
                        'laplacian_variance': {'unit': 'gray_squared', 'scope': 'whole_thumbnail',
                                               'method': 'cv2.Laplacian(gray, CV_64F, ksize=1).var()'}},
            'frames': [], 'notices': list(NOTICES)}


def sample_times(duration_ms):
    if type(duration_ms) is not int or not 0 < duration_ms <= 180000:
        raise VisualError('视频时长不在本地检查范围内。')
    interval = max(MIN_INTERVAL_MS, math.ceil(duration_ms / MAX_FRAMES))
    return interval, list(range(0, duration_ms, interval))[:MAX_FRAMES]


def _location(data_dir, media_hash):
    root = Path(data_dir).resolve()
    cache_root = root / 'visual-cache'
    directory = cache_root / f'{ALGORITHM_VERSION}-{media_hash}'
    if (not cache_root.resolve().is_relative_to(root)
            or not directory.resolve().is_relative_to(cache_root.resolve())):
        raise VisualError('画面缓存位置不合法。')
    return directory


def _frame_path(directory, index):
    if type(index) is not int or not 0 <= index < MAX_FRAMES:
        raise VisualError('缩略帧索引不合法。')
    root = Path(directory).resolve()
    path = root / f'{index:02d}.jpg'
    if not path.resolve().is_relative_to(root) or path.is_symlink():
        raise VisualError('缩略帧路径不合法。')
    return path


def _media_digest(path):
    source = Path(path).resolve()
    stat = source.stat()
    if not source.is_file() or not 0 < stat.st_size <= 100 * 1024 * 1024:
        raise VisualError('原片不存在、为空或超过本地大小限制。')
    identity = (str(source), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino)
    # Frame loads re-use this immutable-upload digest instead of rereading up to
    # 100 MB for every thumbnail. A changed source stat always gets a new hash.
    with _DIGEST_LOCK:
        if identity not in _SOURCE_DIGESTS:
            with source.open('rb') as stream:
                value = hashlib.file_digest(stream, 'sha256').hexdigest()
            if len(_SOURCE_DIGESTS) >= 128:
                _SOURCE_DIGESTS.pop(next(iter(_SOURCE_DIGESTS)))
            _SOURCE_DIGESTS[identity] = value
        return _SOURCE_DIGESTS[identity]


def _load_detector(cv2):
    try:
        if not MODEL_PATH.is_file():
            return None, '本地 YuNet 权重文件不可用；仍可查看普通画面与采集质量读数，未运行人脸框计数。'
        if not 0 < MODEL_PATH.stat().st_size <= 5 * 1024 * 1024:
            return None, '本地 YuNet 权重大小异常；拒绝运行未核验的检测器。'
        data = MODEL_PATH.read_bytes()
        if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
            return None, '本地 YuNet 权重校验不匹配；拒绝运行替换或损坏的模型。'
        import numpy as np
        # The buffer overload avoids OpenCV Windows filename encoding issues.
        # Fixed ONNX bytes run locally on CPU; no network or identity embeddings.
        detector = cv2.FaceDetectorYN.create('onnx', np.frombuffer(data, dtype=np.uint8),
                                           np.empty(0, dtype=np.uint8), (320, 320),
                                           SCORE_THRESHOLD, 0.3, 5000,
                                           cv2.dnn.DNN_BACKEND_OPENCV, cv2.dnn.DNN_TARGET_CPU)
        if detector is None:
            return None, '本地 YuNet 无法加载；未运行人脸框计数。'
        return detector, None
    except Exception:
        return None, '本地 YuNet 无法加载；未运行人脸框计数。'


def _detect_boxes(detector, thumbnail):
    """Retain only bounded rectangles. Never expose model landmarks or identity."""
    height, width = thumbnail.shape[:2]
    detector.setInputSize((width, height))
    _, faces = detector.detect(thumbnail)
    if faces is None:
        return []
    boxes = []
    for row in faces:
        if len(row) != 15:
            raise VisualError('检测器输出结构不合法。')
        x, y, w, h = [float(v) for v in row[:4]]
        score = float(row[14])
        if not all(math.isfinite(v) for v in (x, y, w, h, score)) or score < SCORE_THRESHOLD or w <= 0 or h <= 0:
            continue
        left, top = max(0, min(width, math.floor(x))), max(0, min(height, math.floor(y)))
        right, bottom = max(0, min(width, math.ceil(x + w))), max(0, min(height, math.ceil(y + h)))
        if right > left and bottom > top:
            boxes.append({'x': left, 'y': top, 'width': right - left, 'height': bottom - top})
    return boxes


def _build(path, directory):
    result = _empty()
    try:
        metadata = inspect_media(path)
    except MediaError as error:
        result['reason'] = str(error)
        return result
    except Exception:
        result['reason'] = '原片信息无法读取；未生成画面质量结果。'
        return result
    if not metadata['has_video']:
        result.update(status='no_video', reason='此回答的原始媒体没有视频轨道；未生成人脸框或画面质量数值。')
        result['detector']['reason'] = '无视频轨道，未运行检测器。'
        return result
    try:
        import cv2
    except ImportError:
        result['reason'] = '本地 OpenCV 不可用；未生成缩略帧或质量数值。'
        return result
    capture = None
    try:
        capture = cv2.VideoCapture(str(path))
        if not capture.isOpened():
            result['reason'] = '视频轨道存在，但本地解码器无法打开；可继续回放原片核对。'
            return result
        width, height = [capture.get(p) for p in (cv2.CAP_PROP_FRAME_WIDTH, cv2.CAP_PROP_FRAME_HEIGHT)]
        if not all(math.isfinite(v) and v > 0 for v in (width, height)):
            result['reason'] = '视频尺寸不可读取；未计算画面质量。'
            return result
        width, height = int(width), int(height)
        if max(width, height) > MAX_SOURCE_EDGE or width * height > MAX_SOURCE_PIXELS:
            result['reason'] = '视频分辨率超过本地画面检查上限（约4K）；原片未改变，未计算质量数值。'
            return result
        result['video'] = {'width': width, 'height': height, 'duration_ms': metadata['duration_ms']}
        interval, timestamps = sample_times(metadata['duration_ms'])
        result['sampling'].update(interval_ms=interval, requested_frame_count=len(timestamps))
        detector, detector_reason = _load_detector(cv2)
        result['detector'].update(status='available' if detector is not None else 'unavailable', reason=detector_reason,
                                  model_sha256=MODEL_SHA256 if detector is not None else None)
        result['opencv_version'] = cv2.__version__
        started = time.monotonic()
        stopped_early = False
        detector_failed = False
        frames = []
        fps = capture.get(cv2.CAP_PROP_FPS)
        fps = fps if math.isfinite(fps) and fps > 0 else None
        decoded_count = 0
        decoded_ms = -1.0
        result['sampling']['method'] = 'sequential_decode'
        for requested_ms in timestamps:
            if time.monotonic() - started >= MAX_ANALYSIS_SECONDS:
                stopped_early = True
                break
            # MediaRecorder VP9/WebM may have few seekable keyframes. Sequential
            # grabs preserve codec reference frames; only requested samples are
            # retrieved, resized, measured and retained.
            reached = False
            while decoded_count < 21600:
                if time.monotonic() - started >= MAX_ANALYSIS_SECONDS:
                    stopped_early = True
                    break
                if not capture.grab():
                    break
                position_ms = capture.get(cv2.CAP_PROP_POS_MSEC)
                actual_time = (math.isfinite(position_ms) and 0 <= position_ms < metadata['duration_ms']
                               and (decoded_count == 0 or position_ms > decoded_ms))
                if actual_time:
                    decoded_ms, timestamp_source = position_ms, 'decoder'
                elif fps is not None:
                    decoded_ms, timestamp_source = decoded_count * 1000 / fps, 'frame_rate_approximate'
                else:
                    result['reason'] = '解码器未提供可用时间戳或帧率；未将无法定位的画面标为精确时刻。'
                    stopped_early = True
                    break
                decoded_count += 1
                if decoded_ms >= metadata['duration_ms']:
                    break
                if decoded_ms >= requested_ms:
                    reached = True
                    break
            if not reached:
                break
            ok, frame = capture.retrieve()
            if not ok or frame is None or not getattr(frame, 'size', 0):
                continue
            actual_height, actual_width = frame.shape[:2]
            if max(actual_width, actual_height) > MAX_SOURCE_EDGE or actual_width * actual_height > MAX_SOURCE_PIXELS:
                result['reason'] = '实际解码尺寸超过本地检查上限；未继续处理原片。'
                stopped_early = True
                break
            scale = min(1.0, THUMBNAIL_EDGE / max(actual_width, actual_height))
            thumbnail = cv2.resize(frame, (max(1, round(actual_width * scale)), max(1, round(actual_height * scale))), interpolation=cv2.INTER_AREA)
            gray = cv2.cvtColor(thumbnail, cv2.COLOR_BGR2GRAY)
            brightness = float(gray.mean())
            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F, ksize=1).var())
            if not math.isfinite(brightness) or not math.isfinite(sharpness):
                continue
            box_items = None
            if detector is not None:
                try:
                    box_items = _detect_boxes(detector, thumbnail)
                except Exception:
                    detector, detector_failed = None, True
                    result['detector'].update(status='unavailable', reason='本地 YuNet 人脸框检测执行失败；已保留普通缩略帧与采集质量读数。')
            timestamp_ms = round(decoded_ms)
            ok, encoded = cv2.imencode('.jpg', thumbnail, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if not ok:
                continue
            index = len(frames)
            target = _frame_path(directory, index)
            target.write_bytes(encoded.tobytes())
            frames.append({'index': index, 'timestamp_ms': timestamp_ms, 'requested_timestamp_ms': requested_ms,
                           'timestamp_source': timestamp_source,
                           'width': thumbnail.shape[1], 'height': thumbnail.shape[0],
                           'face_count': len(box_items) if box_items is not None else None, 'face_boxes': box_items or [],
                           'brightness_mean': round(brightness, 3), 'laplacian_variance': round(sharpness, 3)})
        if detector_failed:
            # Do not mix counted and uncounted frames in a single denominator.
            for frame in frames:
                frame.update(face_count=None, face_boxes=[])
        result['frames'] = frames
        result['sampling']['decoded_frame_count'] = len(frames)
        result['summary']['sampled_frame_count'] = len(frames)
        if not frames:
            result['reason'] = result['reason'] or '未能解码任何抽样画面；未生成质量数值。'
            return result
        result['summary'].update(
            frames_with_face=sum(f['face_count'] > 0 for f in frames) if detector is not None else None,
            mean_brightness=round(sum(f['brightness_mean'] for f in frames) / len(frames), 3),
            mean_laplacian_variance=round(sum(f['laplacian_variance'] for f in frames) / len(frames), 3))
        incomplete = len(frames) < len(timestamps) or detector is None
        result['status'] = 'partial' if incomplete else 'succeeded'
        if not result['reason']:
            result['reason'] = ('达到本地检查时限；仅展示已成功解码的抽样帧。' if stopped_early else
                                '部分预定抽样帧未解码成功；统计仅覆盖已返回的帧。' if len(frames) < len(timestamps) else
                                result['detector']['reason'] if detector is None else None)
        return result
    except Exception:
        # Never expose source paths, codec stderr, or embedded media metadata.
        return _empty(reason='本地画面检查失败；原片和面试记录未改变，可稍后重试。')
    finally:
        if capture is not None:
            try:
                capture.release()
            except Exception:
                pass


def _cached(directory, media_hash):
    manifest = directory / 'quality.json'
    if manifest.is_symlink():
        return None
    try:
        if not manifest.is_file() or manifest.stat().st_size > 1024 * 1024:
            return None
        record = json.loads(manifest.read_text(encoding='utf-8'))
        if record['media_hash'] != media_hash or record['algorithm_version'] != ALGORITHM_VERSION:
            return None
        if record['status'] not in ('succeeded', 'partial', 'no_video') or len(record['frames']) > MAX_FRAMES:
            return None
        if any(f['index'] != i or not _frame_path(directory, i).is_file() for i, f in enumerate(record['frames'])):
            return None
        return record
    except (OSError, ValueError, TypeError, KeyError, VisualError):
        return None


def visual_quality(path, data_dir):
    """Authorized callers supply only a core-validated local media path."""
    try:
        media_hash = _media_digest(path)
        directory = _location(data_dir, media_hash)
        lock = _LOCKS[int(media_hash[:8], 16) % len(_LOCKS)]
        with lock:
            record = _cached(directory, media_hash)
            if record is None:
                directory.mkdir(parents=True, exist_ok=True)
                record = _build(path, directory)
                record['media_hash'] = media_hash
                if record['status'] in ('succeeded', 'partial', 'no_video'):
                    temporary = directory / (uuid.uuid4().hex + '.tmp')
                    try:
                        temporary.write_text(json.dumps(record, ensure_ascii=False, allow_nan=False), encoding='utf-8')
                        temporary.replace(directory / 'quality.json')
                    finally:
                        temporary.unlink(missing_ok=True)
            # Caller adds per-session URLs. Hash/cache locations never need to leave this module.
            public = {k: v for k, v in record.items() if k != 'media_hash'}
            public['detector'] = {k: v for k, v in record['detector'].items()
                                  if k in ('name', 'status', 'reason', 'model_sha256')}
            return public
    except (OSError, VisualError):
        return _empty(reason='原片或本地画面缓存不可读取；未生成质量数值。')


def visual_frame(path, data_dir, index):
    """Return an existing cached thumbnail; this path never starts analysis."""
    media_hash = _media_digest(path)
    directory = _location(data_dir, media_hash)
    lock = _LOCKS[int(media_hash[:8], 16) % len(_LOCKS)]
    with lock:
        record = _cached(directory, media_hash)
        if not record or type(index) is not int or not 0 <= index < len(record['frames']):
            raise VisualError('缩略帧不存在；请先打开此回答的画面质量记录。')
        return _frame_path(directory, index)
