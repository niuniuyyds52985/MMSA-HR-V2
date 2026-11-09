"""Independent, four-category EMOTIC contextual perception research adapter.

This is the Tandon PyTorch architecture, with community-preserved checkpoints.
It is not the original Lua checkpoint, a face-only expression model, a measure
of someone's inner state, or an input to interview questions or scores.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import time
import uuid

from fetch_emotic import ASSETS, PINNED, TANDON_COMMIT, MIRROR_COMMIT, DETECTOR_COMMIT, verify

ROOT = Path(__file__).resolve().parent
VERSION = 'emotic-context-perception-four.v2'
MODEL_ID = 'Tandon-A/emotic/cdfmlr-preserved-four-categories'
ALLOWED_LABELS = ('Anger', 'Happiness', 'Sadness', 'Surprise')
ALLOWED_INDICES = (1, 16, 20, 23)
CONTEXT_MEAN = (.4690646, .4407227, .40508908)
CONTEXT_STD = (.2514227, .24312855, .24266963)
BODY_MEAN = (.43832874, .3964344, .3706214)
BODY_STD = (.24784276, .23621225, .2323653)


def _verified_assets():
    """No inference or deserialization; suitable for an installation doctor."""
    hashes = {name: verify(ASSETS / name, entry[3], entry[4]) for name, entry in PINNED.items()}
    from interview_visual import MODEL_PATH, MODEL_SHA256
    if not MODEL_PATH.is_file() or hashlib.sha256(MODEL_PATH.read_bytes()).hexdigest() != MODEL_SHA256:
        raise ValueError('emotic_face_count_guard_asset_unavailable')
    hashes['shared-yunet-face-count-guard'] = MODEL_SHA256
    return hashes


def _network_types():
    import torch
    from torch import nn

    class BasicBlock(nn.Module):
        expansion = 1

        def __init__(self, inplanes, planes, stride=1, downsample=None):
            super().__init__()
            self.conv1 = nn.Conv2d(inplanes, planes, 3, stride, 1, bias=False)
            self.bn1 = nn.BatchNorm2d(planes)
            self.relu = nn.ReLU(inplace=True)
            self.conv2 = nn.Conv2d(planes, planes, 3, 1, 1, bias=False)
            self.bn2 = nn.BatchNorm2d(planes)
            self.downsample = downsample
            self.stride = stride

        def forward(self, x):
            identity = x if self.downsample is None else self.downsample(x)
            return self.relu(self.bn2(self.conv2(self.relu(self.bn1(self.conv1(x))))) + identity)

    class Emotic(nn.Module):
        def __init__(self):
            super().__init__()
            self.num_context_features = 512
            self.num_body_features = 512
            self.fc1 = nn.Linear(1024, 256)
            self.bn1 = nn.BatchNorm1d(256)
            self.d1 = nn.Dropout(.5)
            self.fc_cat = nn.Linear(256, 26)
            self.fc_cont = nn.Linear(256, 3)
            self.relu = nn.ReLU()

        def forward(self, context, body):
            combined = torch.cat((context.reshape(-1, 512), body.reshape(-1, 512)), 1)
            # After loading, fc_cat contains only four selected rows and the
            # continuous head is removed. Other category values are not computed.
            return self.fc_cat(self.d1(self.relu(self.bn1(self.fc1(combined)))))

    def feature_network():
        layers = [nn.Conv2d(3, 64, 7, 2, 3, bias=False), nn.BatchNorm2d(64),
                  nn.ReLU(inplace=True), nn.MaxPool2d(3, 2, 1)]
        previous = 64
        for width, stride in ((64, 1), (128, 2), (256, 2), (512, 2)):
            down = nn.Sequential(nn.Conv2d(previous, width, 1, stride, bias=False),
                                 nn.BatchNorm2d(width)) if stride != 1 else None
            layers.append(nn.Sequential(BasicBlock(previous, width, stride, down), BasicBlock(width, width)))
            previous = width
        layers.append(nn.AdaptiveAvgPool2d((1, 1)))
        return nn.Sequential(*layers)

    return BasicBlock, Emotic, feature_network


def _load_network():
    import numpy as np
    import torch
    from torch import nn
    hashes = _verified_assets()
    BasicBlock, Emotic, feature_network = _network_types()
    # The pinned historical checkpoint contains full nn.Modules. Only reviewed
    # torch layer types and our own local classes are permitted by weights_only.
    allowed = [nn.Sequential, nn.Conv2d, nn.BatchNorm2d, nn.BatchNorm1d,
               nn.ReLU, nn.MaxPool2d, nn.AdaptiveAvgPool2d, nn.Linear, nn.Dropout,
               set, (set, '__builtin__.set'),
               (BasicBlock, 'torchvision.models.resnet.BasicBlock'),
               (Emotic, '__main__.Emotic'), (Emotic, 'emotic.Emotic')]
    networks = [feature_network(), feature_network(), Emotic()]
    with torch.serialization.safe_globals(allowed):
        for filename, network in zip(('model_context1.pth', 'model_body1.pth', 'model_emotic1.pth'), networks):
            loaded = torch.load(ASSETS / filename, map_location='cpu', weights_only=True)
            state = loaded.state_dict() if isinstance(loaded, nn.Module) else loaded
            if not isinstance(state, dict):
                raise ValueError('emotic_checkpoint_not_a_state_dictionary')
            network.load_state_dict(state, strict=True)
            del loaded, state
    head = networks[2]
    selected = nn.Linear(256, len(ALLOWED_LABELS))
    with torch.no_grad():
        selected.weight.copy_(head.fc_cat.weight[list(ALLOWED_INDICES)])
        selected.bias.copy_(head.fc_cat.bias[list(ALLOWED_INDICES)])
    head.fc_cat = selected
    del head.fc_cont
    thresholds = np.load(ASSETS / 'val_thresholds.npy', allow_pickle=False)
    if thresholds.shape != (26,) or not np.isfinite(thresholds).all():
        raise ValueError('emotic_thresholds_invalid')
    return [network.eval() for network in networks], thresholds[list(ALLOWED_INDICES)], hashes


def _person_detector():
    import cv2
    import numpy as np
    # Byte buffers support Windows Unicode paths without alternate filesystem aliases.
    net = cv2.dnn.readNetFromCaffe(np.frombuffer((ASSETS / 'person-deploy.prototxt').read_bytes(), np.uint8),
                                 np.frombuffer((ASSETS / 'person-mobilenet.caffemodel').read_bytes(), np.uint8))
    net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
    net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
    return net


def _detect_person(frame, detector):
    import cv2
    import numpy as np
    height, width = frame.shape[:2]
    if min(height, width) < 64:
        raise ValueError('emotic_frame_too_small')
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if float(gray.mean()) < 12 or float(gray.std()) < 4:
        raise ValueError('emotic_dark_or_flat_frame')
    blob = cv2.dnn.blobFromImage(frame, 1 / 127.5, (300, 300), (127.5, 127.5, 127.5), swapRB=False, crop=False)
    detector.setInput(blob)
    detections = detector.forward().reshape(-1, 7)
    boxes, scores = [], []
    for item in detections:
        if not np.isfinite(item).all() or int(item[1]) != 15 or item[2] < .6:
            continue
        x1, y1, x2, y2 = np.rint(item[3:7] * [width, height, width, height]).astype(int)
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(width, x2), min(height, y2)
        if x2 - x1 >= 40 and y2 - y1 >= 40:
            boxes.append([int(x1), int(y1), int(x2 - x1), int(y2 - y1)])
            scores.append(float(item[2]))
    indices = cv2.dnn.NMSBoxes(boxes, scores, .6, .35)
    survivors = [boxes[int(i)] for i in np.asarray(indices).reshape(-1)]
    if len(survivors) != 1:
        raise ValueError('emotic_requires_one_detected_person')
    return survivors[0]


def _prepare_inputs(frame, box):
    import cv2
    import numpy as np
    import torch
    if len(box) != 4 or any(not isinstance(value, (int, np.integer)) for value in box):
        raise ValueError('emotic_person_box_invalid')
    x, y, width, height = box
    if x < 0 or y < 0 or width < 40 or height < 40 or x + width > frame.shape[1] or y + height > frame.shape[0]:
        raise ValueError('emotic_person_box_invalid')
    rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    body = rgb[y:y + height, x:x + width]
    def normalize(image, size, mean, std):
        image = cv2.resize(image, size, interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255
        image = (image - np.asarray(mean, dtype=np.float32)) / np.asarray(std, dtype=np.float32)
        return torch.from_numpy(image.transpose(2, 0, 1).copy()).unsqueeze(0)
    return normalize(rgb, (224, 224), CONTEXT_MEAN, CONTEXT_STD), normalize(body, (128, 128), BODY_MEAN, BODY_STD)


def _project_outputs(scores, thresholds):
    """Only this explicit allowlist can enter any output/artifact."""
    import numpy as np
    scores, thresholds = np.asarray(scores, dtype=float).reshape(-1), np.asarray(thresholds, dtype=float).reshape(-1)
    if scores.shape != (4,) or thresholds.shape != (4,) or not np.isfinite([scores, thresholds]).all():
        raise ValueError('emotic_four_category_output_invalid')
    return {
        'category_scores': {name: round(float(score), 7) for name, score in zip(ALLOWED_LABELS, scores)},
        'category_thresholds': {name: round(float(value), 7) for name, value in zip(ALLOWED_LABELS, thresholds)},
        'above_threshold': [name for name, score, threshold in zip(ALLOWED_LABELS, scores, thresholds) if score > threshold],
    }


def validate_public_values(values):
    if set(values) != {'category_scores', 'category_thresholds', 'above_threshold'}:
        raise ValueError('emotic_public_output_keys_invalid')
    if set(values['category_scores']) != set(ALLOWED_LABELS) or set(values['category_thresholds']) != set(ALLOWED_LABELS):
        raise ValueError('emotic_public_categories_invalid')
    if not isinstance(values['above_threshold'], list) or len(set(values['above_threshold'])) != len(values['above_threshold']):
        raise ValueError('emotic_public_threshold_labels_invalid')
    if not set(values['above_threshold']) <= set(ALLOWED_LABELS):
        raise ValueError('emotic_public_categories_invalid')
    for field in ('category_scores', 'category_thresholds'):
        for value in values[field].values():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError('emotic_public_output_not_finite')
    return values


def run_emotic(media_path, output_dir, *, sample_interval_ms=1000, max_windows=24):
    import cv2
    import numpy as np
    import torch
    from interview_media import inspect_media
    from interview_visual import _load_detector, _detect_boxes
    if isinstance(sample_interval_ms, bool) or isinstance(max_windows, bool) or not isinstance(sample_interval_ms, int) or not isinstance(max_windows, int):
        raise ValueError('emotic_sampling_invalid')
    if not 200 <= sample_interval_ms <= 10000 or not 1 <= max_windows <= 180:
        raise ValueError('emotic_sampling_invalid')
    source, destination = Path(media_path).resolve(), Path(output_dir).resolve()
    metadata = inspect_media(source)
    if not metadata['has_video']:
        raise ValueError('emotic_requires_video')
    started, run_id = time.perf_counter(), uuid.uuid4().hex
    started_at = datetime.now(timezone.utc).isoformat()
    networks, thresholds, hashes = _load_network()
    detector = _person_detector()
    face_guard, _ = _load_detector(cv2)
    if face_guard is None:
        raise ValueError('emotic_face_count_guard_unavailable')
    from interview_native_video import NativeVideoSampler
    capture = NativeVideoSampler(source)
    old_threads = torch.get_num_threads()
    observations = []
    try:

        torch.set_num_threads(min(4, os.cpu_count() or 1))
        centres = list(range(0, metadata['duration_ms'], sample_interval_ms))
        previous_signature, frozen_group = None, []
        for requested_at in centres[:max_windows]:
            observation = {'start_ms': requested_at, 'end_ms': min(metadata['duration_ms'], requested_at + sample_interval_ms),
                           'centre_ms': requested_at, 'last_input_ms': requested_at, 'values': {},
                           'quality': 'missing', 'reason': None, 'usage': 'observed_only'}
            try:
                sample = capture.sample([requested_at], max_error_ms=125)[0]
                frame, at = sample['frame'], sample['at_ms']
                observation.update(centre_ms=at, last_input_ms=at, requested_centre_ms=requested_at)
                signature = hashlib.sha256(frame.tobytes()).digest()
                if signature == previous_signature:
                    frozen_group.append(observation)
                    # Include the first member: it was only known to be repeated
                    # after this later sample; preserve the actual last input time.
                    for earlier in frozen_group:
                        earlier.update(values={}, quality='missing', reason=earlier.get('reason') or 'emotic_repeated_decoded_frame',
                                       last_input_ms=at, ready_at=datetime.now(timezone.utc).isoformat(),
                                       quality_flags=['identical_sampled_frames'])
                    raise ValueError('emotic_repeated_decoded_frame')
                previous_signature, frozen_group = signature, [observation]
                # A body detector can merge two adjacent upper bodies into one
                # box. An independent face count rejects that known ambiguity.
                if len(_detect_boxes(face_guard, frame)) > 1:
                    raise ValueError('emotic_multiple_faces_detected')
                box = _detect_person(frame, detector)
                observation['capture'] = {'at_ms': at, 'duration_ms': sample['duration_ms'],
                    'duration_source': sample['duration_source'],
                    'frame_width': int(frame.shape[1]), 'frame_height': int(frame.shape[0]),
                    'boxes': [{'kind': 'person', 'x': box[0], 'y': box[1], 'width': box[2], 'height': box[3]},
                              {'kind': 'context', 'x': 0, 'y': 0, 'width': int(frame.shape[1]), 'height': int(frame.shape[0])}]}
                context, body = _prepare_inputs(frame, box)
                with torch.inference_mode():
                    scores = networks[2](networks[0](context), networks[1](body))[0].detach().cpu().numpy()
                observation.update(values=validate_public_values(_project_outputs(scores, thresholds)), quality='valid')
            except (ValueError, cv2.error) as error:
                observation['reason'] = str(error) if isinstance(error, ValueError) else 'emotic_preprocessing_failed'
            observation['ready_at'] = datetime.now(timezone.utc).isoformat()
            observations.append(observation)
    finally:
        capture.close()
        torch.set_num_threads(old_threads)
    for item in observations:
        if item['values']:
            validate_public_values(item['values'])
    valid = sum(item['quality'] == 'valid' for item in observations)
    result = {'schema_version': VERSION, 'module': 'emotic', 'model_id': MODEL_ID, 'run_id': run_id,
        'status': 'complete' if valid == len(observations) and valid else 'partial' if valid else 'no_valid_observations',
        'usage': 'observed_only', 'started_at': started_at, 'completed_at': datetime.now(timezone.utc).isoformat(),
        'media_sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'duration_ms': metadata['duration_ms'],
        'elapsed_seconds': round(time.perf_counter() - started, 3), 'observations': observations,
        'summary': {'sampled_windows': len(observations), 'valid_windows': valid, 'eligible_windows': len(centres),
                    'truncated_by_window_limit': len(centres) > len(observations)},
        'provenance': {'source': 'https://github.com/Tandon-A/emotic', 'source_commit': TANDON_COMMIT,
            'checkpoint_repository': 'https://github.com/cdfmlr/murecom-verse-1', 'checkpoint_commit': MIRROR_COMMIT,
            'checkpoint_origin': 'community-preserved Tandon-compatible weights; unavailable original release cannot be byte-compared',
            'assets_sha256': hashes, 'code_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'device': 'cpu', 'torch': torch.__version__, 'sample_interval_ms': sample_interval_ms, 'max_windows': max_windows,
            'native_fps': None, 'native_timestamp_sampling': capture.provenance,
            'max_nearest_frame_error_ms': 125, 'input': 'whole RGB scene 224x224 and detected person RGB crop 128x128, upstream channel normalization',
            'person_detector': 'chuanqi305/MobileNet-SSD/' + DETECTOR_COMMIT, 'person_threshold': .6, 'person_nms': .35,
            'preprocessing_difference': 'MobileNet-SSD person detection replaces upstream YOLOv3; require exactly one detected person; no full-body visibility guarantee',
            'identity_quality_gate': 'shared verified YuNet face count rejects more than one face; no face identity or embeddings',
            'public_labels': list(ALLOWED_LABELS), 'public_output_policy': 'four original category-head rows only; continuous head removed before inference',
            'output_scale': 'unbounded linear scores and checkpoint validation thresholds; not probabilities, intensity or calibrated psychological scales',
            'weights_loading': 'verified Git blobs, weights_only=True, fixed reviewed class allowlist, strict state shapes; local ResNet18 equivalent graph',
            'temporal_mode': 'offline individual sampled frames; repeated sampled images are backmarked missing with later last_input_ms'},
        'notices': ['场景辅助的感知情绪标签（研究），仅展示四类，不是纯面部表情测量。',
                    '输出不证明参与者真实内心状态，不进入能力评分或自动追问。',
                    '使用社区保存的 Tandon 兼容检查点；不能证明与已失联原发布文件逐字节一致。',
                    '人物框与整图框标明实际输入范围，不是因果解释；未完成中文面试场景效度验证。']}
    destination.mkdir(parents=True, exist_ok=True)
    artifact = 'emotic-' + run_id + '.json'
    result['artifact'] = artifact
    (destination / artifact).write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + '\n', encoding='utf-8')
    return result
