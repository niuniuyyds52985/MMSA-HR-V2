"""Independent Gaze360 seven-frame CPU inference with inspectable captures.

Outputs are direction estimates and the model's angular interval, never person
scores, attention, honesty or mental-state labels. The source/weight download
is separate; no network request occurs during inference.
"""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import time
import uuid

from fetch_gaze360 import ASSETS, COMMIT, REPO, SOURCES, WEIGHT_FILE, WEIGHT_BLOB, WEIGHT_BYTES, git_blob

ROOT = Path(__file__).resolve().parent
VERSION = 'gaze360-cpu.v2'
CHECKPOINT_SHA256 = '415acb32d268d706d9e126429af1135cc6482e6a274e4a2df6c05395c1405257'
MODEL_SAMPLE_HZ = 8
MODEL_INTERVAL_MS = 125
PINNED = {**SOURCES, WEIGHT_FILE: (WEIGHT_BLOB, WEIGHT_BYTES)}


def _verified_assets():
    hashes = {}
    for relative, (blob, size) in PINNED.items():
        path = ASSETS / relative
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError('gaze360_asset_missing')
        data = path.read_bytes()
        if git_blob(data) != blob:
            raise ValueError('gaze360_asset_mismatch')
        hashes[relative] = hashlib.sha256(data).hexdigest()
    if hashes[WEIGHT_FILE] != CHECKPOINT_SHA256:
        raise ValueError('gaze360_checkpoint_mismatch')
    return hashes


def _network_class():
    """Load verified author architecture, removing unused torchvision imports.

The old constructor's pretrained ImageNet download is suppressed because all
parameters are replaced by the complete gaze checkpoint before any inference.
The original forward method and resnet implementation are kept unchanged.
"""
    spec = importlib.util.spec_from_file_location('_zhenjian_gaze360_resnet', ASSETS / 'code' / 'resnet.py')
    resnet = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(resnet)
    tree = ast.parse((ASSETS / 'code' / 'model.py').read_text(encoding='utf-8'))
    tree.body = [item for item in tree.body if not (
        isinstance(item, ast.Import) and any(alias.name.startswith('torchvision') for alias in item.names)
        or isinstance(item, ast.ImportFrom) and item.module in ('resnet', 'torch.nn.init'))]
    namespace = {'resnet18': lambda pretrained=True: resnet.resnet18(pretrained=False)}
    exec(compile(tree, str(ASSETS / 'code' / 'model.py'), 'exec'), namespace)
    return namespace['GazeLSTM']


def _normalize_state(state, expected):
    import torch
    if not isinstance(state, dict):
        raise ValueError('gaze360_checkpoint_state_invalid')
    normalized = {}
    for key, value in state.items():
        name = key.removeprefix('module.')
        if name in normalized or name not in expected or not isinstance(value, torch.Tensor):
            raise ValueError('gaze360_checkpoint_keys_mismatch')
        if value.shape != expected[name].shape or not torch.isfinite(value).all():
            raise ValueError('gaze360_checkpoint_tensor_mismatch')
        normalized[name] = value
    if set(normalized) != set(expected):
        raise ValueError('gaze360_checkpoint_keys_mismatch')
    return normalized


def _load_network():
    import torch
    hashes = _verified_assets()
    model = _network_class()()
    checkpoint = torch.load(ASSETS / WEIGHT_FILE, map_location='cpu', weights_only=True)
    state = _normalize_state(checkpoint.get('state_dict'), model.state_dict())
    model.load_state_dict(state, strict=True)
    return model.eval(), hashes, {'state_dict_keys': len(state), 'checkpoint_epoch': checkpoint.get('epoch')}


def direction_from_angles(yaw, pitch):
    if not all(math.isfinite(value) for value in (yaw, pitch)):
        raise ValueError('gaze360_angles_nonfinite')
    if abs(yaw) > math.pi + 1e-6 or abs(pitch) > math.pi / 2 + 1e-6:
        raise ValueError('gaze360_angles_out_of_range')
    return [math.cos(pitch) * math.sin(yaw), math.sin(pitch), -math.cos(pitch) * math.cos(yaw)]


def _display_windows(duration_ms, sample_interval_ms, max_windows):
    return [(start, min(duration_ms, start + sample_interval_ms),
             (start + min(duration_ms, start + sample_interval_ms)) / 2)
            for start in range(0, int(duration_ms), sample_interval_ms)][:max_windows]


def _seven_requests(centre_ms, duration_ms):
    times = [centre_ms + offset * MODEL_INTERVAL_MS for offset in range(-3, 4)]
    if times[0] < 0 or times[-1] >= duration_ms:
        raise ValueError('gaze360_temporal_boundary_missing')
    return times


def _head_box(face, width, height):
    """Project front-facing YuNet face box to a larger head-region crop.

    This is a documented approximation, not the original AlphaPose annotation.
    Actual clipped pixels are recorded; there is no hidden border padding.
    """
    x, y, w, h = [float(value) for value in face[:4]]
    if not all(math.isfinite(value) for value in (x, y, w, h)) or min(w, h) < 40:
        raise ValueError('gaze360_face_too_small_or_invalid')
    requested = [math.floor(x - .25 * w), math.floor(y - .45 * h),
                 math.ceil(x + 1.25 * w), math.ceil(y + 1.15 * h)]
    left, top, right, bottom = max(0, requested[0]), max(0, requested[1]), min(width, requested[2]), min(height, requested[3])
    requested_area = (requested[2] - requested[0]) * (requested[3] - requested[1])
    if min(right - left, bottom - top) < 40 or (right - left) * (bottom - top) < .65 * requested_area:
        raise ValueError('gaze360_head_crop_clipped')
    return [left, top, right - left, bottom - top], [requested[0], requested[1], requested[2] - requested[0], requested[3] - requested[1]]


def _prepare_sequence(frames, actual_times, fps, detector, frame_durations_ms=None):
    import cv2
    import numpy as np
    from PIL import Image
    if len(frames) != 7 or len(actual_times) != 7 or len(set(actual_times)) != 7:
        raise ValueError('gaze360_requires_seven_distinct_frames')
    if any(b <= a for a, b in zip(actual_times, actual_times[1:])):
        raise ValueError('gaze360_timestamps_not_increasing')
    if all(np.array_equal(frames[0], frame) for frame in frames[1:]):
        raise ValueError('gaze360_identical_temporal_input')
    tensors, captures = [], []
    previous_box = None
    mean = np.array([.485, .456, .406], dtype=np.float32)[:, None, None]
    std = np.array([.229, .224, .225], dtype=np.float32)[:, None, None]
    for frame, at in zip(frames, actual_times):
        height, width = frame.shape[:2]
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        if float(gray.mean()) < 12 or float(gray.std()) < 2:
            raise ValueError('gaze360_dark_or_flat_frame')
        detector.setInputSize((width, height))
        _, found = detector.detect(frame)
        faces = [] if found is None else [item for item in found
            if len(item) == 15 and np.isfinite(item).all() and item[14] >= .9]
        if len(faces) != 1:
            raise ValueError('gaze360_requires_one_reliable_face')
        box, requested = _head_box(faces[0], width, height)
        if previous_box:
            px, py, pw, ph = previous_box
            distance = math.hypot(box[0] + box[2] / 2 - px - pw / 2, box[1] + box[3] / 2 - py - ph / 2)
            if distance > max(pw, ph) or max(box[2] / pw, pw / box[2], box[3] / ph, ph / box[3]) > 2:
                raise ValueError('gaze360_head_track_discontinuous')
        previous_box = box
        x, y, w, h = box
        rgb = cv2.cvtColor(frame[y:y+h, x:x+w], cv2.COLOR_BGR2RGB)
        image = Image.fromarray(rgb).resize((224, 224), Image.Resampling.BILINEAR)
        tensor = np.asarray(image).astype(np.float32).transpose(2, 0, 1) / 255.
        tensors.append((tensor - mean) / std)
        captures.append({'frame_width': width, 'frame_height': height, 'at_ms': at,
            'duration_ms': (frame_durations_ms[len(captures)] if frame_durations_ms is not None
                            else max(1, round(1000 / fps))),
            'boxes': [{'kind': 'head', 'x': x, 'y': y, 'width': w, 'height': h}],
            'requested_box_xywh': requested, 'padding': False})
    return np.stack(tensors), captures


def run_gaze360(media_path, output_dir, *, sample_interval_ms=1000, max_windows=180):
    import cv2
    import numpy as np
    import torch
    from interview_media import inspect_media
    from interview_visual import _load_detector, MODEL_SHA256
    if not isinstance(sample_interval_ms, int) or not 200 <= sample_interval_ms <= 10000 or not 1 <= max_windows <= 180:
        raise ValueError('invalid_gaze360_sampling')
    source = Path(media_path).resolve()
    metadata = inspect_media(source)
    if not metadata['has_video']:
        raise ValueError('gaze360_requires_video')
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    run_id = uuid.uuid4().hex
    result = {'schema_version': VERSION, 'run_id': run_id, 'module': 'gaze360',
        'model_id': 'erkil1452/gaze360/GazeLSTM-community-checkpoint', 'usage': 'observed_only',
        'started_at': datetime.now(timezone.utc).isoformat(), 'observations': [],
        'media_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'duration_ms': metadata['duration_ms'], 'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    detector, _ = _load_detector(cv2)
    if detector is None:
        raise ValueError('gaze360_face_detector_unavailable')
    from interview_native_video import NativeVideoSampler
    capture = NativeVideoSampler(source)
    threads = torch.get_num_threads()
    try:
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        model, hashes, checkpoint_info = _load_network()
        # MediaRecorder may report 1000 fps despite ~30 fps native PTS.
        # Preserve actual timestamps and require real 8 Hz temporal coverage.
        windows = _display_windows(metadata['duration_ms'], sample_interval_ms, max_windows)
        for start, end, centre in windows:
            observation = {'start_ms': start, 'end_ms': end, 'requested_centre_ms': round(centre),
                'centre_ms': round(centre), 'values': {}, 'quality': 'missing', 'reason': None,
                'usage': 'observed_only', 'input_frame_times_ms': [], 'requested_frame_times_ms': []}
            try:
                requests = _seven_requests(centre, metadata['duration_ms'])
                observation['requested_frame_times_ms'] = requests
                samples = capture.sample(requests, max_error_ms=MODEL_INTERVAL_MS / 2,
                                         max_frame_gap_ms=MODEL_INTERVAL_MS)
                frames = [item['frame'] for item in samples]
                actual_times = [item['at_ms'] for item in samples]
                observation.update(input_frame_times_ms=actual_times, centre_ms=actual_times[3],
                    last_input_ms=max(actual_times), temporal_start_ms=min(actual_times),
                    input_frame_time_errors_ms=[round(actual - request, 3) for actual, request in zip(actual_times, requests)],
                    max_input_frame_error_ms=round(max(abs(actual - request) for actual, request in zip(actual_times, requests)), 3))
                sequence, captures = _prepare_sequence(frames, actual_times, None, detector,
                    frame_durations_ms=[item['duration_ms'] for item in samples])
                for saved, sampled in zip(captures, samples):
                    saved['duration_source'] = sampled['duration_source']
                observation.update(capture=captures[3], input_captures=captures)
                with torch.inference_mode():
                    angles, interval = model(torch.from_numpy(sequence)[None])
                yaw, pitch = [float(value) for value in angles[0]]
                halfwidths = [float(value) for value in interval[0]]
                if len(halfwidths) != 2 or not all(math.isfinite(value) and 0 <= value <= math.pi for value in halfwidths):
                    raise ValueError('gaze360_interval_invalid')
                direction = direction_from_angles(yaw, pitch)
                observation.update(quality='valid', values={
                    'direction_xyz': [round(value, 6) for value in direction],
                    'yaw_pitch_radians': [round(yaw, 6), round(pitch, 6)],
                    'angular_interval_radians': [round(value, 6) for value in halfwidths]})
            except (ValueError, cv2.error) as error:
                observation['reason'] = str(error) if isinstance(error, ValueError) else 'gaze360_preprocessing_failed'
            observation['ready_at'] = datetime.now(timezone.utc).isoformat()
            observation['ready_elapsed_ms'] = round((time.perf_counter() - started) * 1000)
            result['observations'].append(observation)
        result['provenance'] = {'source': f'https://github.com/{REPO}', 'source_commit': COMMIT,
            'assets_sha256': hashes, **checkpoint_info, 'checkpoint_origin': 'community_backup_with_documented_official_download_script',
            'origin_limitation': 'Original endpoint returned 403. Backup Git blob pinned; no author checksum available to independently establish identical origin.',
            'backup_repository': 'm01ali/Classroom-Attention-Monitoring-Sys',
            'backup_commit': '66d66415ad8c4da88ebd9ba9c8477ac5983b2ec2',
            'device': 'cpu', 'torch': torch.__version__, 'native_fps': None,
            'native_timestamp_sampling': capture.provenance,
            'max_nearest_frame_error_ms': MODEL_INTERVAL_MS / 2,
            'max_native_frame_gap_ms': MODEL_INTERVAL_MS,
            'model_sample_hz': MODEL_SAMPLE_HZ, 'temporal_input_count': 7,
            'temporal_offsets_ms': [-375, -250, -125, 0, 125, 250, 375],
            'sample_interval_ms': sample_interval_ms, 'max_windows': max_windows,
            'edge_policy': 'missing when any of seven neighbours absent; never pad, duplicate or interpolate',
            'temporal_sampling': 'Original 8 Hz sequence, nearest decoded native frame; requested and actual timestamps retained.',
            'preprocessing_version': 'gaze360-yunet-head.v1',
            'preprocessing': 'RGB head crop; PIL bilinear 224x224; /255; ImageNet mean/std from official validation transform.',
            'preprocessing_difference': 'YuNet frontal face-expanded head region instead of original AlphaPose head crops. Frontal/single-face subset only; not validated full 360-degree capability.',
            'face_detector_sha256': MODEL_SHA256,
            'coordinate_definition': 'Gaze360 eye-centric Cartesian direction: x=cos(pitch)*sin(yaw), y=sin(pitch), z=-cos(pitch)*cos(yaw); toward camera is [0,0,-1]. No calibrated screen intersection.',
            'interval_definition': 'Per-angle symmetric half-width learned by q0.1/q0.9 pinball loss; not this-participant calibrated coverage, confidence probability or measured angular error.',
            'weights_loading': 'weights_only=True CPU, module. prefix removal, strict complete key/shape/finite checks; no random-parameter inference; original forward unchanged.'}
    finally:
        capture.close()
        torch.set_num_threads(threads)
    valid = sum(item['quality'] == 'valid' for item in result['observations'])
    result.update(status='complete' if valid and valid == len(result['observations']) else 'partial' if valid else 'no_valid_observations',
        completed_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=round(time.perf_counter() - started, 3),
        summary={'sampled_windows': len(result['observations']), 'valid_windows': valid,
            'eligible_windows': math.ceil(metadata['duration_ms'] / sample_interval_ms),
            'truncated_by_window_limit': math.ceil(metadata['duration_ms'] / sample_interval_ms) > len(result['observations'])},
        notices=['Gaze360 官方网络结构；权重来自有官方来源下载脚本的社区备份，缺作者校验值，来源限制保留。',
            '七帧按原数据 8 Hz 间隔读取，使用中心帧前后画面；不能倒填为面试当时已知信息。',
            '头部裁剪前端不同于原论文，未做本项目方向误差或分位区间覆盖率验证。',
            '三维方向与模型角区间不等于屏幕注视点、注意力、诚信、心理或能力判断。'])
    artifact = f'gaze360-{run_id}.json'
    result['artifact'] = artifact
    (destination / artifact).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    return result


if __name__ == '__main__':
    result = run_gaze360(ROOT / 'data' / 'small-question-video.mp4',
        ROOT / 'local-data' / 'interview-validation' / 'gaze360', max_windows=3)
    print(json.dumps({key: result[key] for key in ('run_id', 'model_id', 'status', 'summary', 'elapsed_seconds')}, ensure_ascii=False))
