"""Independent, local research measurements; never scores or selects questions.

OpenFace 2.2 provides explicitly named AU and geometric gaze estimates. The
OpenCV Zoo expression model classifies apparent expressions. Audio uses WebRTC
VAD and pYIN. No output is an honesty, personality or mental-state measure.
"""
from __future__ import annotations
import csv
from datetime import datetime, timezone
import hashlib
import importlib.util
from importlib.metadata import version, PackageNotFoundError
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from interview_media import inspect_media, _run

ROOT = Path(__file__).resolve().parent
MODELS = ROOT / 'models'
VERSION = 'independent-multimodal.v6'
DEFAULT_MODULES = ('au', 'gaze', 'expression', 'audio')
MODULES = (*DEFAULT_MODULES, 'au_net', 'gaze360', 'emotic')
FPS = 5
MIN_TRACK_CONFIDENCE = 0.8
MAX_GEOMETRY_OBSERVATIONS = 180
GEOMETRY_VERSION = 'face-geometry.v1'
GAZE_RAY_MM = 50.0
QUALITY_VERSION = 'capture-quality.v2'
EXPRESSION_NAME = 'facial_expression_recognition_mobilefacenet_2022july.onnx'
EXPRESSION_SHA256 = '4f61307602fc089ce20488a31d4e4614e3c9753a7d6c41578c854858b183e1a9'
OPENFACE_SHA256 = '4201e96f3bc561a0e3a7fa50d2e12ce782b8a685746d63062572cd5296478dec'
EXPRESSION_LABELS = ['angry', 'disgust', 'fearful', 'happy', 'neutral', 'sad', 'surprised']
AU_INTENSITY = [1, 2, 4, 5, 6, 7, 9, 10, 12, 14, 15, 17, 20, 23, 25, 26, 45]
AU_PRESENCE = [*AU_INTENSITY[:-1], 28, 45]


def _now():
    return datetime.now(timezone.utc).isoformat()


def _hash(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _module(name, model_id, status='not_run', reason=None):
    return {'module': name, 'status': status, 'model_id': model_id, 'version': VERSION,
            'usage': 'observed_only', 'observations': [], 'summary': {}, 'reason': reason,
            'provenance': {}, 'elapsed_seconds': 0, 'notices': []}


def _environment():
    result = {'python': os.sys.version.split()[0], 'platform': os.name, 'device': 'cpu'}
    for package in ['numpy', 'opencv-python', 'librosa', 'webrtcvad-wheels', 'torch']:
        try:
            result[package] = version(package)
        except PackageNotFoundError:
            result[package] = None
    return result


def _verified_expression():
    manifest = json.loads((MODELS / (EXPRESSION_NAME + '.source.json')).read_text(encoding='utf-8'))
    path = MODELS / EXPRESSION_NAME
    if manifest['sha256'] != EXPRESSION_SHA256 or _hash(path) != EXPRESSION_SHA256:
        raise ValueError('expression_asset_mismatch')
    return path, manifest


def _verified_openface():
    manifest = json.loads((MODELS / 'openface-2.2.0.source.json').read_text(encoding='utf-8'))
    if manifest['archive_sha256'] != OPENFACE_SHA256:
        raise ValueError('openface_release_mismatch')
    directory = MODELS / 'openface-2.2.0'
    # Verify executable and every loaded model / runtime. No arbitrary executables.
    for relative, digest in manifest['files'].items():
        path = directory / relative
        if not path.resolve().is_relative_to(directory.resolve()) or _hash(path) != digest:
            raise ValueError('openface_asset_mismatch')
    executable = directory / 'FeatureExtraction.exe'
    if not executable.is_file():
        raise ValueError('openface_executable_missing')
    return directory, executable, manifest


def _observation(start, end, values, quality='valid', reason=None, **extra):
    return {'start_ms': int(start), 'end_ms': int(end), 'last_input_ms': int(end),
            'values': values, 'quality': quality, 'reason': reason,
            'usage': 'observed_only', **extra}


def _align_face(image, points):
    """OpenCV Zoo nonreflective least-squares similarity transform, 112 px."""
    import cv2
    import numpy as np
    standard = np.array([[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366],
                         [41.5493, 92.3655], [70.7299, 92.2041]])
    x, y = standard[:, 0:1], standard[:, 1:2]
    a = np.vstack([np.hstack([x, y, np.ones((5, 1)), np.zeros((5, 1))]),
                   np.hstack([y, -x, np.zeros((5, 1)), np.ones((5, 1))])])
    target = np.vstack([points[:, 0:1], points[:, 1:2]])
    sc, ss, tx, ty = np.linalg.lstsq(a, target, rcond=-1)[0].ravel()
    transform = np.linalg.inv(np.array([[sc, -ss, 0], [ss, sc, 0], [tx, ty, 1]]))
    return cv2.warpAffine(image, transform[:, :2].T, (112, 112))


def _expression_and_quality(prepared, duration, requested):
    import cv2
    import numpy as np
    from interview_visual import _load_detector, MODEL_SHA256
    module = _module('expression', 'opencv-zoo/MobileFaceNet-ProgressiveTeacher/2022july')
    started = time.perf_counter()
    detector, why = _load_detector(cv2)
    quality = {}
    net = None
    if requested:
        try:
            path, manifest = _verified_expression()
            net = cv2.dnn.readNetFromONNX(np.frombuffer(path.read_bytes(), dtype=np.uint8))
            net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            module['provenance'] = {**manifest, 'labels': EXPRESSION_LABELS,
                'preprocessing': 'YuNet five points; official nonreflective similarity; 112x112 RGB; (pixel/255-0.5)/0.5',
                'face_detector_sha256': MODEL_SHA256, 'output_scale': 'raw model logits, argmax class; not calibrated probability'}
        except Exception:
            module.update(status='unavailable', reason='基础表情权重缺失、校验失败或 OpenCV 无法加载。')
    if detector is None:
        module.update(status='unavailable', reason=why)
        return module, quality
    capture = cv2.VideoCapture(str(prepared))
    try:
        index, same_frame_count, track_segment = 0, 0, 0
        previous_hash, previous_box = None, None
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            start, end = index * 1000 // FPS, min(duration, (index + 1) * 1000 // FPS)
            if start >= duration:
                break
            detector.setInputSize((frame.shape[1], frame.shape[0]))
            _, detected = detector.detect(frame)
            faces = [] if detected is None else [f for f in detected if len(f) == 15 and np.isfinite(f).all() and float(f[14]) >= 0.9]
            reason = '未检出可靠人脸。' if not faces else ('画面含多个人脸，未选择或识别其中任何人。' if len(faces) > 1 else None)
            if len(faces) == 1 and min(float(faces[0][2]), float(faces[0][3])) < 40:
                reason = '人脸像素不足，未运行表情分类。'
            gray_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            frame_hash = hashlib.sha256(gray_frame.tobytes()).digest()
            same_frame_count = same_frame_count + 1 if frame_hash == previous_hash else 1
            previous_hash = frame_hash
            brightness = float(gray_frame.mean())
            flags = []
            if brightness < 20:
                flags.append('low_light')
            elif brightness > 245:
                flags.append('overexposed')
            if same_frame_count >= 2 * FPS:
                flags.append('repeated_identical_frames')
                reason = reason or '连续至少 2 秒抽样画面完全相同，疑似采集冻结；该窗口不作为有效观察。'
                if same_frame_count == 2 * FPS:
                    # Offline review: invalidate the whole observed repeated run,
                    # including its initial threshold-estimation interval. Keep
                    # when that quality decision became knowable explicitly.
                    for earlier in range(index - same_frame_count + 1, index):
                        prior = quality.get(earlier)
                        if prior is None:
                            continue
                        prior['reason'] = prior.get('reason') or reason
                        prior['quality_flags'] = list(dict.fromkeys([*prior.get('quality_flags', []), 'repeated_identical_frames']))
                        prior['quality_last_input_ms'] = end
                        prior['track_segment_id'] = None
                        if net is not None and earlier < len(module['observations']):
                            item = module['observations'][earlier]
                            item.update(quality='missing', reason=prior['reason'], values={},
                                quality_flags=prior['quality_flags'], track_segment_id=None,
                                last_input_ms=max(item['last_input_ms'], end))
            box = [float(v) for v in faces[0][:4]] if len(faces) == 1 else None
            segment_id = None
            if not reason:
                if previous_box is None or _box_iou(box, previous_box) < 0.1:
                    track_segment += 1
                segment_id = f'visible-face-run-{track_segment:03d}'
                previous_box = box
            else:
                previous_box = None
            quality[index] = {'face_count': len(faces), 'reason': reason,
                'frame_width': int(frame.shape[1]), 'frame_height': int(frame.shape[0]),
                'brightness_mean': round(brightness, 2), 'quality_flags': flags,
                'track_segment_id': segment_id, 'quality_version': QUALITY_VERSION,
                'quality_last_input_ms': end}
            if net is not None:
                if reason:
                    module['observations'].append(_observation(start, end, {}, 'missing', reason))
                else:
                    aligned = _align_face(frame, faces[0][4:14].reshape(5, 2).astype(float))
                    blob = cv2.dnn.blobFromImage(cv2.cvtColor(aligned, cv2.COLOR_BGR2RGB).astype(np.float32) / 127.5 - 1)
                    net.setInput(blob, 'data')
                    scores = np.asarray(net.forward('label')).reshape(-1)
                    if len(scores) != 7 or not np.isfinite(scores).all():
                        module['observations'].append(_observation(start, end, {}, 'missing', '表情模型输出不合法。'))
                    else:
                        gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
                        module['observations'].append(_observation(start, end,
                            {'label': EXPRESSION_LABELS[int(np.argmax(scores))],
                             'raw_scores': {k: round(float(v), 6) for k, v in zip(EXPRESSION_LABELS, scores)}},
                            face_count=1, brightness_mean=round(float(gray.mean()), 2),
                            quality_flags=flags, track_segment_id=segment_id,
                            laplacian_variance=round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 2)))
            index += 1
    finally:
        capture.release()
    if net is not None:
        valid = [item for item in module['observations'] if item['quality'] == 'valid']
        module.update(status='complete' if valid else 'no_valid_observations',
                      reason=None if valid else '抽样画面没有满足条件的单个人脸。')
        module['summary'] = {'sampled_frames': len(module['observations']), 'valid_frames': len(valid),
            'label_counts': {label: sum(item['values']['label'] == label for item in valid) for label in EXPRESSION_LABELS}}
    module['capture_quality'] = _quality_summary(quality)
    module['notices'] = ['类别仅表示模型对外显表情的分类，不代表真实内心状态，也不用于潜力评分或追问。',
                         '5 fps 抽样不是微表情检测；中文面试域尚未做人类标注效度评测。']
    module['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    return module, quality


def _box_iou(one, two):
    """Spatial continuity only; never face identity or re-identification."""
    if not one or not two:
        return 0.0
    x = max(0.0, min(one[0] + one[2], two[0] + two[2]) - max(one[0], two[0]))
    y = max(0.0, min(one[1] + one[3], two[1] + two[3]) - max(one[1], two[1]))
    intersection = x * y
    union = one[2] * one[3] + two[2] * two[3] - intersection
    return intersection / union if union > 0 else 0.0


def _quality_summary(frames):
    values = list(frames.values())
    return {'schema_version': QUALITY_VERSION, 'sampled_frames': len(values),
        'single_face_eligible_frames': sum(not q.get('reason') for q in values),
        'missing_face_frames': sum(q.get('face_count') == 0 for q in values),
        'multiple_face_frames': sum(q.get('face_count', 0) > 1 for q in values),
        'flag_counts': {flag: sum(flag in q.get('quality_flags', []) for q in values)
                        for flag in ('low_light', 'overexposed', 'repeated_identical_frames')},
        'thresholds': {'brightness_mean_low': 20, 'brightness_mean_high': 245,
                       'identical_sampled_frames_min': 2 * FPS, 'face_min_pixels': 40},
        'track_scope': 'contiguous single-face spatial segments; not identity or cross-segment matching',
        'not_measured': ['occlusion_type', 'person_identity', 'camera_hardware_failure'],
        'freeze_policy': 'offline backmark complete identical-sample run after 2 second threshold; last_input_ms preserves decision time',
        'notice': '质量阈值为工程提示，未做采集设备校准；相同抽样帧只提示疑似冻结。'}


def _camera_for_frame(width, height):
    """Explicit uncalibrated intrinsics, passed to OpenFace and used for display."""
    focal = (500.0 * width / 640.0 + 500.0 * height / 480.0) / 2.0
    return {'fx': focal, 'fy': focal, 'cx': width / 2.0, 'cy': height / 2.0,
            'calibration': 'estimated_from_image_size; not camera/screen calibrated'}


def _geometry_frame(row, width, height, camera, start, end, sample_index,
                    reason=None, frame_quality=None):
    """2D points and the official 50 mm gaze ray projection, never a screen target.

    OpenFace eye_lmk_X/Y/Z 0..7 and 28..35 are the two pupil rings.
    Their mean and camera-space unit gaze vector define the displayed ray, as in
    the upstream Visualizer::SetObservationGaze implementation.
    """
    item = {'start_ms': int(start), 'end_ms': int(end),
        'last_input_ms': max(int(end), (frame_quality or {}).get('quality_last_input_ms', int(end))),
        'sample_index': int(sample_index), 'frame_width': width, 'frame_height': height,
        'landmarks': [], 'gaze_arrows': [], 'quality': 'missing' if reason else 'valid',
        'reason': reason, 'usage': 'observed_only',
        'quality_flags': (frame_quality or {}).get('quality_flags', []),
        'track_segment_id': (frame_quality or {}).get('track_segment_id')}
    if reason:
        return item
    try:
        coordinates = [[float(row[f'x_{i}']) / width, float(row[f'y_{i}']) / height] for i in range(68)]
        # Do not clamp out-of-frame model estimates onto the face or invent points.
        if any(not math.isfinite(v) or not -0.25 <= v <= 1.25 for point in coordinates for v in point):
            raise ValueError('invalid_landmarks')
        item['landmarks'] = [[round(v, 6) for v in point] for point in coordinates]
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        item.update(quality='missing', reason='68 点缺失或越界，未生成替代点位。')
        return item
    if camera is None:
        item['gaze_reason'] = '缺少本次运行的相机参数，未投影视线方向。'
        return item

    def project(point):
        if point[2] <= 1 or any(not math.isfinite(v) for v in point):
            raise ValueError('invalid_eye_depth')
        return [(camera['fx'] * point[0] / point[2] + camera['cx']) / width,
                (camera['fy'] * point[1] / point[2] + camera['cy']) / height]

    for eye, offset in ((0, 0), (1, 28)):
        try:
            centre = [sum(float(row[f'eye_lmk_{axis}_{i}']) for i in range(offset, offset + 8)) / 8
                      for axis in 'XYZ']
            direction = [float(row[f'gaze_{eye}_{axis}']) for axis in 'xyz']
            length = math.sqrt(sum(v * v for v in direction))
            if not 0.95 <= length <= 1.05:
                raise ValueError('invalid_direction')
            origin = project(centre)
            tip = project([p + d * GAZE_RAY_MM for p, d in zip(centre, direction)])
            if any(not math.isfinite(v) or abs(v) > 4 for v in [*origin, *tip]):
                raise ValueError('invalid_projection')
            item['gaze_arrows'].append({'eye': eye, 'origin': [round(v, 6) for v in origin],
                'tip': [round(v, 6) for v in tip], 'direction_camera': [round(v, 6) for v in direction]})
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            item['gaze_reason'] = '部分眼部三维点或方向无效；只展示可用点位/方向。'
    return item


def parse_openface_csv(csv_path, duration_ms, face_quality=None, frame_size=None, camera_intrinsics=None):
    """Parse only explicit documented columns; reject failed and invalid frames."""
    results = {'au': [], 'gaze': []}
    geometry = []
    previous_start = -1
    with Path(csv_path).open(encoding='utf-8-sig', newline='') as handle:
        for row_index, raw in enumerate(csv.DictReader(handle)):
            row = {key.strip(): value.strip() for key, value in raw.items()}
            if row_index >= 900:
                break
            start = round(float(row['timestamp']) * 1000)
            end = min(duration_ms, start + 1000 // FPS)
            if start < 0 or start <= previous_start or end <= start:
                raise ValueError('invalid_frame_time')
            previous_start = start
            confidence = float(row['confidence'])
            frame_index = int(row['frame']) - 1
            q = (face_quality or {}).get(frame_index)
            reason = None
            if not math.isfinite(confidence) or row['success'] != '1' or confidence < MIN_TRACK_CONFIDENCE:
                reason = '人脸跟踪失败或置信度不足；数值不作为有效观测。'
            elif face_quality is not None and (q is None or q.get('reason')):
                reason = q['reason'] if q else '该帧缺少单人脸质量核验。'
            quality = 'missing' if reason else 'valid'
            if frame_size:
                geometry.append(_geometry_frame(row, *frame_size, camera_intrinsics, start, end,
                                                 frame_index, reason, q))
            for name in ('au', 'gaze'):
                values = {}
                local_reason = reason
                if not reason:
                    try:
                        if name == 'au':
                            intensity = {f'AU{i:02d}': float(row[f'AU{i:02d}_r']) for i in AU_INTENSITY}
                            presence = {f'AU{i:02d}': float(row[f'AU{i:02d}_c']) for i in AU_PRESENCE}
                            if any(not math.isfinite(v) or not 0 <= v <= 5 for v in intensity.values()) or any(v not in (0, 1) for v in presence.values()):
                                raise ValueError('invalid_au')
                            values = {'intensity': intensity, 'presence': {k: int(v) for k, v in presence.items()}}
                        else:
                            values = {key: float(row[key]) for key in ['gaze_angle_x', 'gaze_angle_y', 'gaze_0_x', 'gaze_0_y', 'gaze_0_z', 'gaze_1_x', 'gaze_1_y', 'gaze_1_z']}
                            if any(not math.isfinite(v) for v in values.values()) or any(abs(values[k]) > math.pi for k in ['gaze_angle_x', 'gaze_angle_y']):
                                raise ValueError('invalid_gaze')
                            for eye in (0, 1):
                                length = math.sqrt(sum(values[f'gaze_{eye}_{axis}'] ** 2 for axis in 'xyz'))
                                if not 0.95 <= length <= 1.05:
                                    raise ValueError('invalid_gaze_vector')
                    except (KeyError, ValueError, TypeError):
                        values, local_reason = {}, '模型返回缺失列或越界数值。'
                results[name].append(_observation(start, end, values,
                    'missing' if local_reason else quality, local_reason,
                    tracking_confidence=confidence if math.isfinite(confidence) else None,
                    quality_flags=(q or {}).get('quality_flags', []),
                    track_segment_id=(q or {}).get('track_segment_id'),
                    last_input_ms=duration_ms if name == 'au' else max(end, (q or {}).get('quality_last_input_ms', end))))
    if frame_size:
        stride = max(1, math.ceil(len(geometry) / MAX_GEOMETRY_OBSERVATIONS))
        sampled = geometry[::stride]
        results['geometry'] = {'schema_version': GEOMETRY_VERSION,
            'coordinate_space': 'normalized_sample_frame', 'landmark_order': 'OpenFace 68-point (0-based)',
            'camera_intrinsics': camera_intrinsics, 'ray_length_mm': GAZE_RAY_MM,
            'analysis_fps': FPS, 'sample_stride': stride,
            'frame_width': frame_size[0], 'frame_height': frame_size[1],
            'source_frame_count': len(geometry), 'sampled_frame_count': len(sampled),
            'observations': sampled,
            'notice': '真实模型点位；箭头是估计相机坐标下的 50 mm 视线方向投影，不是屏幕注视点。未采样区间不插值。'}
    return results


def _openface(prepared, directory, duration, requested, face_quality):
    output = {name: _module(name, 'OpenFace/2.2.0-CLNF') for name in requested}
    started = time.perf_counter()
    phase, outcome, returncode = 'asset_verification', 'not_started', None
    attempt = {'timeout_seconds': 240, 'automatic_retry': False}
    try:
        model_directory, executable, manifest = _verified_openface()
        attempt.update(source=manifest['source'], archive_sha256=manifest['archive_sha256'], assets_verified=True)
        # Relative ASCII paths avoid legacy OpenCV Windows Unicode path decoding.
        frame_size = next(((q['frame_width'], q['frame_height']) for q in face_quality.values()
                           if q.get('frame_width') and q.get('frame_height')), None)
        camera = _camera_for_frame(*frame_size) if frame_size else None
        args = [str(executable), '-f', os.path.relpath(prepared, model_directory),
                '-out_dir', os.path.relpath(directory, model_directory), '-of', 'observations.csv',
                '-mloc', 'model/main_clnf_general.txt', '-aus', '-gaze', '-2Dfp', '-pose', '-q']
        if camera:
            for key in ('fx', 'fy', 'cx', 'cy'):
                args.extend(['-' + key, str(camera[key])])
        phase, outcome = 'process_execution', 'running'
        process = subprocess.run(args, cwd=model_directory, capture_output=True, timeout=240,
                                 creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        returncode = process.returncode
        phase = 'output_validation'
        csv_path = directory / 'observations.csv'
        if process.returncode:
            outcome = 'nonzero_exit'
            raise ValueError('openface_inference_failed')
        if not csv_path.is_file():
            outcome = 'missing_output'
            raise ValueError('openface_inference_failed')
        phase, outcome = 'output_parse', 'parse_failed'
        data = parse_openface_csv(csv_path, duration, face_quality, frame_size, camera)
        for name, module in output.items():
            valid = [item for item in data[name] if item['quality'] == 'valid']
            module.update(status='complete' if valid else 'no_valid_observations', observations=data[name],
                          reason=None if valid else '当前片段没有通过跟踪和单人脸校验的观测。')
            module['summary'] = {'sampled_frames': len(data[name]), 'valid_frames': len(valid),
                                 'valid_fraction': round(len(valid) / len(data[name]), 4) if data[name] else None}
            module['provenance'] = {'source': manifest['source'], 'archive_sha256': manifest['archive_sha256'],
                'weights_manifest_sha256': _hash(MODELS / 'openface-2.2.0.source.json'),
                'landmark_model': 'CLNF general (official bundled model)', 'fps': FPS, 'confidence_threshold': MIN_TRACK_CONFIDENCE,
                'preprocessing': 'FFmpeg 5 fps, max-width 640, MJPEG quality 3; original aspect ratio; single-face quality gate',
                'camera_calibration': camera or 'not_available; no screen coordinates',
                'license': 'noncommercial internal research; bundled Copyright.txt / OpenFace-license.txt',
                'temporal_context': 'whole-clip person normalization' if name == 'au' else 'tracked frame',
                'units': {'intensity': 'model scale 0–5', 'presence': 'binary 0/1'} if name == 'au' else {'gaze_angle_x': 'radian', 'gaze_angle_y': 'radian', 'vectors': 'unit direction'},
                'output_documentation': 'https://github.com/TadasBaltrusaitis/OpenFace/wiki/Output-Format'}
            module['notices'] = (['面部动作强度是模型估计，不是心理或潜力评分。', 'AU 采用全片个体归一化，不能回填为面试当时已知的证据。'] if name == 'au' else
                ['视线是相机坐标几何估计；未做相机/屏幕校准，不输出精确屏幕注视点。', '不解释注意力、诚信、自信、人格或心理状态。'])
            module['capture_quality'] = _quality_summary(face_quality)
        if 'geometry' in data:
            output['gaze' if 'gaze' in output else 'au']['geometry'] = data['geometry']
        phase, outcome = 'complete', 'completed'
    except Exception as error:
        if isinstance(error, subprocess.TimeoutExpired):
            outcome, reason = 'timeout', 'OpenFace 运行超过 240 秒；本次 AU / 视线没有可验收结果，其他模块单独保留。'
        elif phase == 'asset_verification':
            outcome, reason = 'asset_verification_failed', 'OpenFace 模型或运行文件校验失败；未启动推理。'
        elif phase == 'process_execution':
            outcome, reason = 'execution_failed', 'OpenFace 子进程启动或执行失败；未生成替代值。'
        else:
            reason = 'OpenFace 返回状态或输出校验失败；未生成替代值。'
        attempt['error_type'] = type(error).__name__
        for module in output.values():
            module.update(status='unavailable', reason=reason, observations=[], summary={})
    elapsed = round(time.perf_counter() - started, 3)
    for module in output.values():
        module['elapsed_seconds'] = elapsed
        module['provenance']['execution'] = {**attempt, 'phase': phase, 'outcome': outcome,
                                            'returncode': returncode, 'elapsed_seconds': elapsed}
    return output


def speech_units(text):
    """Chinese characters + whitespace-delimited alphabetic/numeric tokens."""
    return len(re.findall(r'[\u3400-\u4dbf\u4e00-\u9fff]|[A-Za-z0-9]+(?:[\x27’-][A-Za-z0-9]+)*', text))


def intervals_from_flags(flags, frame_ms, duration_ms, selected=True):
    intervals, begin = [], None
    for i, flag in enumerate([*flags, not selected]):
        if flag == selected and begin is None:
            begin = i * frame_ms
        elif flag != selected and begin is not None:
            end = min(i * frame_ms, duration_ms)
            if end > begin:
                intervals.append({'start_ms': begin, 'end_ms': end})
            begin = None
    return intervals


def analyze_audio(pcm, snapshot, duration_ms):
    import numpy as np
    import webrtcvad
    module = _module('audio', 'WebRTC-VAD2+pYIN+RMS')
    started = time.perf_counter()
    raw = np.frombuffer(pcm, dtype='<i2')
    audio = raw.astype(np.float32) / 32768.0
    duration = min(duration_ms, round(len(audio) / 16))
    vad = webrtcvad.Vad(2)
    frame_samples = 320
    flags = [vad.is_speech(raw[i:i + frame_samples].tobytes(), 16000)
             for i in range(0, len(raw) - frame_samples + 1, frame_samples)]
    speech = intervals_from_flags(flags, 20, duration)
    # Internal >=300 ms non-speech gaps only. Leading/trailing silence is separate.
    non_speech = intervals_from_flags(flags, 20, duration, False)
    pauses = [v for v in non_speech if v['end_ms'] - v['start_ms'] >= 300 and
              speech and v['start_ms'] >= speech[0]['end_ms'] and v['end_ms'] <= speech[-1]['start_ms']]
    pitch, pitch_reason = None, None
    if len(audio) >= 1024 and float(np.sqrt(np.mean(audio * audio))) >= 0.0001:
        try:
            import librosa
            pitch, voiced, voiced_prob = librosa.pyin(audio, fmin=60, fmax=500, sr=16000,
                frame_length=1024, hop_length=320, center=False, fill_na=np.nan)
            pitch[~voiced] = np.nan
        except Exception:
            pitch_reason = 'pYIN 未能计算有效 F0，其他声学观测仍保留。'
    else:
        pitch_reason = '静音或不足 64 ms，没有有效 F0。'
    for start in range(0, duration, 1000):
        end = min(start + 1000, duration)
        samples = audio[start * 16:end * 16]
        if not len(samples):
            continue
        frame_values = flags[start // 20:min(len(flags), (end + 19) // 20)]
        # pYIN outputs are centred 32 ms into each 64 ms input frame.
        f0 = [] if pitch is None else [float(value) for i, value in enumerate(pitch)
                                       if start <= 32 + i * 20 < end and math.isfinite(float(value))]
        rms = float(np.sqrt(np.mean(samples * samples)))
        clipping = float(np.mean(np.abs(samples) >= 0.999))
        quality_flags = []
        if clipping >= 0.01:
            quality_flags.append('audio_clipping')
        if rms < 0.0001:
            quality_flags.append('near_silence')
        usable_pitch = f0 if clipping < 0.05 else []
        module['observations'].append(_observation(start, end,
            {'rms_dbfs': round(20 * math.log10(max(rms, 1e-8)), 3),
             'speech_fraction': round(sum(frame_values) / len(frame_values), 4) if frame_values else None,
             'f0_hz': round(float(np.median(usable_pitch)), 3) if usable_pitch else None,
             'f0_voiced_frames': len(usable_pitch), 'clipped_sample_fraction': round(clipping, 5)},
             quality_flags=quality_flags,
             f0_reason=(None if usable_pitch else '此窗口削波比例至少 5%，不展示 F0。' if clipping >= 0.05
                        else pitch_reason or '此窗口没有通过 pYIN 的有声 F0。'),
             last_input_ms=duration))
    spoken_ms = sum(item['end_ms'] - item['start_ms'] for item in speech)
    transcript = snapshot.get('transcript', snapshot.get('text', '')) or ''
    units = speech_units(transcript)
    has_usable_pitch = any(item['values']['f0_hz'] is not None for item in module['observations'])
    summary_pitch_reason = None if has_usable_pitch else pitch_reason or (
        'F0 经过逐窗质量门控后均不可用；请查看削波标记和各窗口原因。'
        if any('audio_clipping' in item.get('quality_flags', []) for item in module['observations'])
        else '本片段没有通过 pYIN 的有效有声 F0。')
    module.update(status='complete', reason=None)
    module['summary'] = {'duration_ms': duration, 'speech_ms': spoken_ms,
        'vad_analyzed_ms': len(flags) * 20, 'vad_unprocessed_tail_ms': max(0, duration - len(flags) * 20),
        'speech_fraction': round(spoken_ms / duration, 4) if duration else None,
        'pause_count': len(pauses), 'pause_ms': sum(p['end_ms'] - p['start_ms'] for p in pauses),
        'transcript_units': units, 'speech_rate_units_per_min': round(units * 60000 / duration, 2) if duration and units and spoken_ms else None,
        'articulation_rate_units_per_speech_min': round(units * 60000 / spoken_ms, 2) if spoken_ms and units else None,
        'f0_status': 'available' if has_usable_pitch else 'no_valid_pitch',
        'f0_reason': summary_pitch_reason, 'clipped_sample_fraction': round(float(np.mean(np.abs(audio) >= 0.999)), 5) if len(audio) else None,
        'speech_intervals': speech, 'pause_intervals': pauses, 'non_speech_intervals': non_speech}
    module['provenance'] = {'vad': 'webrtcvad-wheels', 'vad_mode': 2, 'vad_frame_ms': 20,
        'f0_method': 'librosa.pyin', 'f0_range_hz': [60, 500], 'f0_frame_samples': 1024, 'f0_hop_samples': 320,
        'pcm': 'signed little-endian int16, 16 kHz mono', 'rms_window_ms': 1000, 'pause_min_ms': 300,
        'quality_version': QUALITY_VERSION, 'clipping_sample_threshold': 0.999,
        'clipping_warning_fraction': 0.01, 'f0_suppressed_clipping_fraction': 0.05,
        'quality_threshold_status': 'engineering flags; not perceptual quality validation',
        'rate_units': 'Chinese characters + alphabetic/numeric tokens; count from specified transcript revision, not syllables',
        'rate_alignment': 'whole-answer transcript only; no word-time attribution',
        'temporal_context': 'offline full-clip pYIN Viterbi decoding; not causal realtime',
        'code_sha256': _hash(__file__), 'sources': ['https://github.com/wiseman/py-webrtcvad',
            'https://librosa.org/doc/main/generated/librosa.pyin.html']}
    module['notices'] = ['发声/停顿是 VAD 算法估计；噪声、音乐和设备会影响检测。',
        'F0 仅在 pYIN 判为有声的帧显示，范围外和无声窗口为空；不从声线判断心理或能力。',
        '语速依赖所选转写文本的正确性，单位为中文字符/英文数字词元，不是音节。']
    module['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    return module



_SAFE_MODEL_ERRORS = frozenset({
    'gaze360_asset_missing', 'gaze360_asset_mismatch', 'gaze360_checkpoint_mismatch',
    'gaze360_checkpoint_state_invalid', 'gaze360_checkpoint_keys_mismatch',
    'gaze360_checkpoint_tensor_mismatch', 'gaze360_face_detector_unavailable',
    'gaze360_native_fps_below_8_or_unknown', 'invalid_model_artifact',
    'emotic_asset_missing_or_size_mismatch', 'emotic_asset_hash_mismatch',
    'emotic_face_count_guard_asset_unavailable', 'emotic_checkpoint_not_a_state_dictionary',
    'emotic_thresholds_invalid', 'emotic_face_count_guard_unavailable', 'emotic_native_fps_unknown',
    'native_video_track_missing', 'native_video_decode_failed', 'native_video_frame_limit',
    'native_video_timestamp_missing', 'native_video_timestamp_out_of_bounds',
    'native_video_timestamps_not_increasing', 'native_video_request_order_invalid',
    'native_video_frame_missing', 'native_video_sampling_gap',
    'native_video_temporal_density_insufficient',
})


def _model_failure(error, elapsed):
    """Persist only fixed diagnostics; never paths or raw decoder/model errors."""
    code = str(error) if str(error) in _SAFE_MODEL_ERRORS else 'model_execution_failed'
    if code.startswith('native_video_') or 'native_fps' in code:
        category = 'media_timeline'
    elif 'asset' in code or 'checkpoint' in code:
        category = 'asset_validation'
    elif isinstance(error, (ImportError, ModuleNotFoundError)):
        category = 'dependency'
    elif isinstance(error, PermissionError):
        category = 'filesystem_permission'
    else:
        category = 'model_execution'
    error_type = next((name for cls, name in (
        (ModuleNotFoundError, 'ModuleNotFoundError'), (ImportError, 'ImportError'),
        (PermissionError, 'PermissionError'), (ValueError, 'ValueError'),
        (RuntimeError, 'RuntimeError')) if isinstance(error, cls)), 'Exception')
    return {'code': code, 'category': category, 'error_type': error_type,
            'elapsed_seconds': round(elapsed, 3), 'automatic_retry': False}


def run_multimodal(media_path, transcript_snapshot, output_dir, modules=None):
    """Fresh local run. Caller owns authorization, snapshot selection and queue."""
    selected = list(DEFAULT_MODULES) if modules is None else list(dict.fromkeys(modules))
    if not selected or any(name not in MODULES for name in selected):
        raise ValueError('Unsupported research module')
    started, began = time.perf_counter(), _now()
    result = {'schema_version': VERSION, 'run_id': uuid.uuid4().hex, 'started_at': began,
        'answer_id': transcript_snapshot.get('answer_id'),
        'transcript_revision': transcript_snapshot.get('transcript_revision'),
        'modules': {}, 'usage': 'observed_only', 'result_cache_reused': False,
        'environment': _environment(), 'code_sha256': _hash(__file__)}
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    if media_path is None:
        for name in selected:
            result['modules'][name] = _module(name, None, 'no_media', '纯文字回答没有原始媒体。')
        result['media_sha256'] = None
    else:
        source = Path(media_path).resolve()
        metadata = inspect_media(source)
        digest = _hash(source)
        expected = transcript_snapshot.get('media_sha256')
        if expected and expected != digest:
            raise ValueError('Research input media hash mismatch')
        result.update(media_sha256=digest, duration_ms=metadata['duration_ms'],
            sampling={'visual_fps': FPS, 'visual_max_duration_ms': 180000,
                'timestamp_source': 'FFmpeg resampled video presentation timeline; answer-relative milliseconds',
                'sampling_method': 'fps=5; sampled frame windows are not original camera frame times',
                'geometry_max_observations': MAX_GEOMETRY_OBSERVATIONS,
                'audio_sample_rate_hz': 16000, 'audio_window_ms': 1000})
        if 'audio' in selected:
            if not metadata['has_audio']:
                result['modules']['audio'] = _module('audio', 'WebRTC-VAD2+pYIN+RMS', 'no_audio', '媒体没有音轨。')
            else:
                try:
                    pcm = _run(['ffmpeg', '-v', 'error', '-nostdin', '-protocol_whitelist', 'file,pipe',
                                '-i', str(source), '-t', '180', '-vn', '-ac', '1', '-ar', '16000', '-f', 's16le', 'pipe:1'], timeout=120)
                    result['modules']['audio'] = analyze_audio(pcm, transcript_snapshot, metadata['duration_ms'])
                except Exception:
                    result['modules']['audio'] = _module('audio', 'WebRTC-VAD2+pYIN+RMS', 'unavailable', '音轨解码或声学依赖不可用。')
        if 'au_net' in selected:
            if not metadata['has_video']:
                result['modules']['au_net'] = _module('au_net', 'jingyang2017/AU-Net/cross_model-15AU', 'no_video', '媒体没有视频轨道。')
            else:
                try:
                    from interview_au_net import run_au_net
                    original = run_au_net(source, destination / 'original-au-net', max_windows=180)
                    result['modules']['au_net'] = {**_module('au_net', original['model_id']), **original,
                        'version': original['schema_version']}
                    result['sampling']['au_net'] = {'timestamp_source': 'native decoded media frames',
                        'centre_interval_ms': 1000, 'temporal_offset_native_frames': 10, 'max_windows': 180,
                        'resampled_5fps_input': False}
                except Exception:
                    result['modules']['au_net'] = _module('au_net', 'jingyang2017/AU-Net/cross_model-15AU',
                        'unavailable', '原 AU-Net 权重、源码校验或独立 CPU 推理失败；请检查官方资产安装，未使用 OpenFace 代填。')
        # These models use the native source and their own input contracts.
        # An unavailable model never falls back to a differently named model.
        for name, model_id in [('gaze360', 'erkil1452/gaze360'), ('emotic', 'Tandon-A/emotic')]:
            if name not in selected:
                continue
            if not metadata['has_video']:
                result['modules'][name] = _module(name, model_id, 'no_video', '媒体没有视频轨道。')
                continue
            module_started = time.perf_counter()
            try:
                if name == 'gaze360':
                    from interview_gaze360 import run_gaze360
                    original = run_gaze360(source, destination / name, max_windows=180)
                else:
                    from interview_emotic import run_emotic
                    original = run_emotic(source, destination / name, max_windows=180)
                if (not re.fullmatch(r'[a-f0-9]{32}', str(original.get('run_id', '')))
                        or original.get('artifact') != f"{name}-{original['run_id']}.json"):
                    raise ValueError('invalid_model_artifact')
                result['modules'][name] = {**_module(name, original['model_id']), **original,
                    'version': original['schema_version']}
                result['sampling'][name] = {'timestamp_source': 'native decoded media frames',
                    'centre_interval_ms': 1000, 'max_windows': 180, 'resampled_5fps_input': False,
                    'input_contract': 'seven_frames_at_8hz_native_decode' if name == 'gaze360' else 'person_crop_and_full_context'}
            except Exception as error:
                label = 'Gaze360' if name == 'gaze360' else 'EMOTIC'
                diagnostic = _model_failure(error, time.perf_counter() - module_started)
                reason = (label + ' 无法取得可靠的原始帧时间戳或所需帧；未生成替代结果。'
                          if diagnostic['category'] == 'media_timeline' else
                          label + ' 的模型资产校验或独立推理失败；请检查诊断类别，未使用其他模型代填。')
                result['modules'][name] = _module(name, model_id, 'unavailable', reason)
                result['modules'][name].update(elapsed_seconds=diagnostic['elapsed_seconds'],
                    provenance={'execution': diagnostic})
        visual = [name for name in selected if name in ('au', 'gaze', 'expression')]
        if visual:
            if not metadata['has_video']:
                for name in visual:
                    result['modules'][name] = _module(name, None, 'no_video', '媒体没有视频轨道。')
            else:
                try:
                    with tempfile.TemporaryDirectory(prefix='mm-', dir=destination) as scratch:
                        directory = Path(scratch)
                        prepared = directory / 'sample.avi'
                        _run(['ffmpeg', '-v', 'error', '-nostdin', '-protocol_whitelist', 'file,pipe',
                              '-i', str(source), '-t', '180', '-vf', "fps=5,scale=w='min(640,iw)':h=-2", '-an',
                              '-c:v', 'mjpeg', '-q:v', '3', str(prepared)], timeout=120)
                        expression, quality = _expression_and_quality(prepared, metadata['duration_ms'], 'expression' in visual)
                        if 'expression' in visual:
                            result['modules']['expression'] = expression
                        subset = [name for name in visual if name != 'expression']
                        if subset:
                            result['modules'].update(_openface(prepared, directory, metadata['duration_ms'], subset, quality))
                except Exception:
                    for name in visual:
                        result['modules'].setdefault(name, _module(name, None, 'unavailable', '视频抽样或运行依赖失败；没有生成替代结果。'))
    result['completed_at'] = _now()
    result['elapsed_seconds'] = round(time.perf_counter() - started, 3)
    for name, module in result['modules'].items():
        module['completed_at'] = result['completed_at']
        for observation in module['observations']:
            observation['completed_at'] = result['completed_at']
        if name in ('gaze360', 'emotic') and module.get('artifact'):
            # The nested artifact and frozen report must contain the same final
            # provenance, including pipeline completion/version fields.
            folder = destination / name
            folder.mkdir(parents=True, exist_ok=True)
            (folder / module['artifact']).write_text(
                json.dumps(module, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    result['status'] = 'complete' if all(m['status'] == 'complete' for m in result['modules'].values()) else 'partial'
    result['artifact'] = 'multimodal-' + result['run_id'] + '.json'
    (destination / result['artifact']).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    return result
