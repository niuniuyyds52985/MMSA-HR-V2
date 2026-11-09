"""Independent official AU-Net CPU adapter; not part of the default pipeline.

Uses pinned author code and weights. The face detector is our existing YuNet,
with its five points replacing the upstream demo's face_alignment landmarks;
that deliberate preprocessing difference is recorded on every run. No scores,
questions, participant sessions or cloud services are accessed here.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import math
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT / 'models' / 'original-au-net'
VERSION = 'original-au-net-cpu.v2'
COMMIT = 'd447837d1eb4631ff473ccb61f13aefeb23844cd'
LABELS = (1, 2, 4, 6, 7, 9, 10, 12, 14, 15, 17, 23, 24, 25, 26)
PINNED = {
    'models/FAN.py': ('3b3ae37f3d510ce6d8553db280df4b2fcce353a7', 5605),
    'models/aunet.py': ('5deeb6ace6f9b2ee416a33bf77ca789ca7c64fd0', 3221),
    'models/__init__.py': ('e69de29bb2d1d6434b8b29ae775ad8c2e48c5391', 0),
    'models/2dfan2.pth': ('02738a66b9420dd15364d7d79f908f0627fb88b4', 48630574),
    'checkpoints/cross_model.pth': ('19745a2ade2f5b8efa9e5dbc26ec44cad1792d96', 10684878),
}


def _verified_assets():
    hashes = {}
    for relative, (blob, size) in PINNED.items():
        path = ASSETS / relative
        if not path.is_file() or path.stat().st_size != size:
            raise ValueError('original_au_net_asset_missing')
        data = path.read_bytes()
        if hashlib.sha1(f'blob {len(data)}\0'.encode() + data).hexdigest() != blob:
            raise ValueError('original_au_net_asset_mismatch')
        hashes[relative] = hashlib.sha256(data).hexdigest()
    return hashes


def _load_network():
    import torch
    hashes = _verified_assets()
    package = '_zhenjian_official_aunet_d447837'
    if package not in sys.modules:
        spec = importlib.util.spec_from_file_location(package, ASSETS / 'models' / '__init__.py',
                                                     submodule_search_locations=[str(ASSETS / 'models')])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        spec.loader.exec_module(module)
    from importlib import import_module
    official = import_module(package + '.aunet')
    # Construct the same modules without the author's cwd-dependent torch.load.
    feature = official.FAN_()
    feature.fan.load_state_dict(torch.load(ASSETS / 'models' / '2dfan2.pth',
                                        map_location='cpu', weights_only=True), strict=True)
    predictor = official.AUFAN_TDN_(alpha=0.9, beta=0.1, n_classes=len(LABELS))
    state = torch.load(ASSETS / 'checkpoints' / 'cross_model.pth', map_location='cpu', weights_only=True)
    state = _map_predictor_keys(state, predictor.state_dict())
    predictor.load_state_dict(state, strict=True)
    return feature.eval(), predictor.eval(), hashes


def _map_predictor_keys(state, expected):
    """Explicit official checkpoint legacy names, never order-based loading."""
    prefixes = {'emo_net_1': 'ConvNet_1', 'emo_net_diff': 'ConvNet_diff',
                'emo_net_2': 'ConvNet_2', 'emo_fc': 'predictor'}
    normalized = {}
    for name, value in state.items():
        name = name.removeprefix('module.')
        prefix, dot, suffix = name.partition('.')
        key = prefixes.get(prefix, prefix) + dot + suffix
        if key in normalized or key not in expected or value.shape != expected[key].shape:
            raise ValueError('original_au_net_predictor_keys_mismatch')
        normalized[key] = value
    if set(normalized) != set(expected):
        raise ValueError('original_au_net_predictor_keys_mismatch')
    return normalized


def _similarity(source, target):
    """Deterministic least-squares nonreflective similarity, source -> target."""
    import numpy as np
    source, target = np.asarray(source, float), np.asarray(target, float)
    if source.shape != (5, 2) or target.shape != (5, 2) or not np.isfinite([source, target]).all():
        raise ValueError('invalid_alignment_points')
    matrix = np.zeros((10, 4), dtype=float)
    matrix[0::2] = np.column_stack((source[:, 0], -source[:, 1], np.ones(5), np.zeros(5)))
    matrix[1::2] = np.column_stack((source[:, 1], source[:, 0], np.zeros(5), np.ones(5)))
    if np.linalg.matrix_rank(matrix) < 4:
        raise ValueError('degenerate_alignment_points')
    a, b, tx, ty = np.linalg.lstsq(matrix, target.reshape(-1), rcond=None)[0]
    if not 0.01 <= math.hypot(a, b) <= 100:
        raise ValueError('invalid_alignment_scale')
    return np.array([[a, -b, tx], [b, a, ty]])


def _crop_triplet(frames, detector):
    import cv2
    import numpy as np
    detections = []
    for frame in frames:
        detector.setInputSize((frame.shape[1], frame.shape[0]))
        _, found = detector.detect(frame)
        faces = [] if found is None else [face for face in found if len(face) == 15 and
            np.isfinite(face).all() and face[14] >= .9]
        if len(faces) != 1 or min(faces[0][2:4]) < 40:
            raise ValueError('triplet_requires_one_reliable_face')
        detections.append(faces[0])
    if np.array_equal(frames[0], frames[1]) and np.array_equal(frames[1], frames[2]):
        raise ValueError('triplet_temporal_input_identical')
    centre = detections[1]
    x, y, width, height = centre[:4]
    crop_width = 200 * (width + height) / 260
    scale = 256 / crop_width
    affine = np.array([[scale, 0, 128 - scale * (x + width / 2)],
                       [0, scale, 128 - scale * (y + height / 2)]], dtype=float)
    # Author demo's target five points, after the central-frame crop.
    target = np.column_stack((centre[4:14].reshape(5, 2), np.ones(5))) @ affine.T
    crops = []
    for index, (frame, face) in enumerate(zip(frames, detections)):
        transform = affine if index == 1 else _similarity(face[4:14].reshape(5, 2), target)
        image = cv2.warpAffine(frame, transform, (256, 256), borderValue=0)
        crops.append(cv2.cvtColor(image, cv2.COLOR_BGR2RGB).transpose(2, 0, 1).astype('float32') / 255.0)
    return np.stack(crops)


def run_au_net(media_path, output_dir, *, sample_interval_ms=1000, max_windows=24):
    """Fresh bounded CPU run; uses real frames ±10 native frames around centre.

    Run in an isolated worker process. This function does not register tasks or
    replace the existing default OpenFace module, and does not pad edge frames.
    """
    import cv2
    import numpy as np
    import torch
    from interview_media import inspect_media
    from interview_visual import _load_detector, MODEL_SHA256
    if not 200 <= sample_interval_ms <= 10000 or not 1 <= max_windows <= 180:
        raise ValueError('invalid_original_au_net_sampling')
    source = Path(media_path).resolve()
    metadata = inspect_media(source)
    if not metadata['has_video']:
        raise ValueError('original_au_net_requires_video')
    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    run_id = uuid.uuid4().hex
    result = {'schema_version': VERSION, 'run_id': run_id, 'module': 'au_net',
        'model_id': 'jingyang2017/AU-Net/cross_model-15AU', 'usage': 'observed_only',
        'started_at': datetime.now(timezone.utc).isoformat(), 'observations': [],
        'media_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'duration_ms': metadata['duration_ms'], 'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    feature, predictor, hashes = _load_network()
    detector, reason = _load_detector(cv2)
    if detector is None:
        raise ValueError('original_au_net_face_detector_unavailable')
    capture = cv2.VideoCapture(str(source))
    threads = torch.get_num_threads()
    try:
        fps = float(capture.get(cv2.CAP_PROP_FPS))
        if not math.isfinite(fps) or not 1 <= fps <= 240:
            raise ValueError('original_au_net_native_fps_unknown')
        offset = 10000 / fps
        eligible_centres = list(np.arange(math.ceil(offset), metadata['duration_ms'] - offset, sample_interval_ms))
        centres = eligible_centres[:max_windows]
        torch.set_num_threads(min(4, os.cpu_count() or 1))
        for centre in centres:
            frames, actual_times = [], []
            observation = {'start_ms': max(0, round(centre - offset)),
                'end_ms': min(metadata['duration_ms'], round(centre + offset + 1000 / fps)),
                'centre_ms': round(centre), 'last_input_ms': round(centre + offset),
                'values': {}, 'quality': 'missing', 'reason': None, 'usage': 'observed_only'}
            try:
                for at in (centre - offset, centre, centre + offset):
                    capture.set(cv2.CAP_PROP_POS_MSEC, float(at))
                    ok, frame = capture.read()
                    if not ok:
                        raise ValueError('triplet_decode_failed')
                    frames.append(frame)
                    actual_times.append(round(capture.get(cv2.CAP_PROP_POS_MSEC)))
                if len(set(actual_times)) != 3:
                    raise ValueError('triplet_timestamps_not_distinct')
                observation.update(input_frame_times_ms=actual_times,
                    requested_centre_ms=round(centre), centre_ms=actual_times[1],
                    start_ms=min(actual_times), end_ms=min(metadata['duration_ms'], round(max(actual_times) + 1000 / fps)),
                    last_input_ms=max(actual_times))
                tensor = torch.from_numpy(_crop_triplet(frames, detector))
                with torch.inference_mode():
                    _, features = feature(tensor)
                    # Official order: centre, previous, next.
                    logits = predictor(features[1:2], features[0:1], features[2:3])[0]
                    scores = torch.sigmoid(logits)
                if len(scores) != 15 or not torch.isfinite(scores).all():
                    raise ValueError('original_au_net_output_invalid')
                observation.update(quality='valid', values={
                        'activation_scores': {f'AU{au:02d}': round(float(v), 6) for au, v in zip(LABELS, scores)},
                        'raw_logits': {f'AU{au:02d}': round(float(v), 6) for au, v in zip(LABELS, logits)}})
            except (ValueError, cv2.error) as error:
                observation['reason'] = str(error) if isinstance(error, ValueError) else 'triplet_preprocessing_failed'
                if observation['reason'] == 'triplet_temporal_input_identical':
                    observation['quality_flags'] = ['identical_temporal_input']
                    observation['reason'] = '三个时序输入帧完全相同，缺少可核对的时间变化；未生成 AU-Net 输出。'
            result['observations'].append(observation)
        result['provenance'] = {'source_commit': COMMIT, 'source': 'https://github.com/jingyang2017/AU-Net',
            'assets_sha256': hashes, 'labels': list(LABELS), 'device': 'cpu', 'torch': torch.__version__,
            'alpha': .9, 'beta': .1, 'native_fps': fps, 'temporal_offset_native_frames': 10,
            'sample_interval_ms': sample_interval_ms, 'max_windows': max_windows,
            'edge_policy': 'skip centres without both temporal neighbours; never duplicate/pad',
            'preprocessing_version': 'aunet-yunet-five-point.v1',
            'temporal_quality_gate': 'reject three exactly identical decoded frames; not a diagnosis of camera failure',
            'preprocessing': 'official demo crop scale=260; neighbouring frames aligned to central five points; RGB 256x256 /255',
            'preprocessing_difference': 'YuNet bbox/five points instead of upstream face_alignment SFD/68-point model',
            'face_detector_sha256': MODEL_SHA256,
            'output_scale': 'sigmoid activation score (not calibrated probability); raw logits; not AU intensity 0–5',
            'class_thresholds': 'not calibrated or applied',
            'weights_loading': 'weights_only=True; CPU; explicit legacy emo_net/emo_fc prefixes mapped to official layer names; strict keys/shapes'}
    finally:
        capture.release()
        torch.set_num_threads(threads)
    valid = sum(item['quality'] == 'valid' for item in result['observations'])
    result.update(status='complete' if valid and valid == len(result['observations']) else 'partial' if valid else 'no_valid_observations',
        completed_at=datetime.now(timezone.utc).isoformat(), elapsed_seconds=round(time.perf_counter() - started, 3),
        summary={'sampled_windows': len(result['observations']), 'valid_windows': valid,
            'eligible_windows': len(eligible_centres), 'truncated_by_window_limit': len(eligible_centres) > len(centres)},
        notices=['正式 AU-Net 权重的独立 CPU 运行；不是现有 OpenFace 输出改名。',
            '输入使用未来帧，结果不得倒填为当时追问依据。',
            '检测/对齐前端与作者演示不同，尚无本项目效度或等价性结论。'])
    artifact = 'au-net-' + run_id + '.json'
    (destination / artifact).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    return result


if __name__ == '__main__':
    result = run_au_net(ROOT / 'data' / 'small-question-video.mp4',
        ROOT / 'local-data' / 'interview-validation' / 'original-au-net', max_windows=3)
    print(json.dumps({key: result[key] for key in ('run_id', 'model_id', 'status', 'summary', 'elapsed_seconds')}, ensure_ascii=False))
