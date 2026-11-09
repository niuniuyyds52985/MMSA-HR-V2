"""Deterministic readable observations from one frozen multimodal answer item.

No media access, inference, cloud request, score or decision is performed here.
Comparisons are within the same recording. AU/geometry are independent concurrent
measurements, never explanations of the expression classifier's hidden reasoning.
"""
from __future__ import annotations

import copy
import math
import statistics

VERSION = 'multimodal-readable.v3'
EXPRESSION = {'happy': '开心样式', 'sad': '悲伤样式', 'angry': '生气样式',
              'surprised': '惊讶样式', 'neutral': '中性样式',
              'fearful': '害怕样式', 'disgust': '厌恶样式'}
CONTEXT = {'Happiness': '开心', 'Sadness': '悲伤', 'Anger': '生气', 'Surprise': '惊讶'}
AU_NAMES = {'AU01': '内眉上提', 'AU02': '外眉上提', 'AU04': '眉部下压',
            'AU05': '上眼睑上提', 'AU06': '脸颊上提', 'AU07': '眼睑收紧',
            'AU09': '鼻部皱起', 'AU10': '上唇上提', 'AU12': '嘴角上提',
            'AU14': '嘴角收紧', 'AU15': '嘴角下拉', 'AU17': '下巴上提',
            'AU20': '嘴唇横向拉伸', 'AU23': '嘴唇收紧', 'AU25': '双唇分开',
            'AU26': '下颌下降', 'AU45': '眨眼'}
AU_RELATED = {'happy': ['AU12', 'AU06', 'AU25'], 'sad': ['AU04', 'AU15', 'AU01'],
              'angry': ['AU04', 'AU07', 'AU23'], 'surprised': ['AU01', 'AU02', 'AU25', 'AU26'],
              'fearful': ['AU01', 'AU04', 'AU20', 'AU25'],
              'disgust': ['AU09', 'AU10', 'AU17'], 'neutral': []}
VISUAL_FLAGS = {'low_light', 'overexposed', 'repeated_identical_frames', 'multiple_faces',
                'multiple_persons', 'frozen', 'blurred', 'face_too_small'}
AUDIO_FLAGS = {'near_silence', 'audio_clipping', 'clipped', 'invalid_audio'}
PARAMETERS = {'pitch_change_semitones': 2.0, 'energy_change_db': 3.0,
              'minimum_audio_event_windows': 2, 'minimum_reference_windows': 5,
              'minimum_reference_duration_ms': 5000, 'minimum_expression_samples': 3,
              'maximum_events': 24,
              'status': 'engineering display thresholds, not validated perceptual cutoffs'}
REFERENCES = [
    {'id': 'pyin', 'url': 'https://www.eecs.qmul.ac.uk/~simond/pub/2014/MauchDixon-PYIN-ICASSP2014.pdf',
     'use': 'F0 estimation; does not supply emotion labels or this project display thresholds'},
    {'id': 'gemaps', 'url': 'https://doi.org/10.1109/TAFFC.2015.2457417',
     'use': 'Separate acoustic measurement domains; this module is not an eGeMAPS implementation'},
    {'id': 'expression', 'url': 'https://github.com/opencv/opencv_zoo/tree/main/models/facial_expression_recognition',
     'use': 'Seven apparent expression classes; project-domain accuracy not established'},
    {'id': 'openface', 'url': 'https://github.com/TadasBaltrusaitis/OpenFace/wiki/Action-Units',
     'use': 'AU names, distinct presence/intensity predictions and video normalization'},
    {'id': 'facs_cooccurrence', 'url': 'https://pmc.ncbi.nlm.nih.gov/articles/PMC3992629/',
     'use': 'Posed expression/AU co-occurrence; not deterministic rules or classifier explanations'}]


def _number(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if low is not None and value < low or high is not None and value > high:
        return None
    return float(value)


def _times(item):
    start, end = _number(item.get('start_ms'), 0), _number(item.get('end_ms'), 0)
    return (start, end) if start is not None and end is not None and end > start else None


def _valid(item, visual=False, audio=False):
    if not isinstance(item, dict) or item.get('quality') != 'valid' or not _times(item):
        return False
    flags = item.get('quality_flags') or []
    if not isinstance(flags, (list, tuple)) or any(not isinstance(flag, str) for flag in flags):
        return False
    if item.get('reason') or set(flags) & (VISUAL_FLAGS if visual else AUDIO_FLAGS if audio else set()):
        return False
    if visual and item.get('face_count') not in (None, 1):
        return False
    return True


def _observations(module):
    if module.get('status') in ('unavailable', 'no_media', 'no_video', 'no_audio', 'failed', 'no_valid_observations'):
        return []
    values = module.get('observations') or []
    return values if isinstance(values, list) else []


def _coverage(items, eligible):
    return {'sampled_observations': len(items), 'valid_observations': len(eligible),
            'excluded_observations': len(items) - len(eligible),
            'valid_fraction': round(len(eligible) / len(items), 4) if items else None,
            'meaning': 'valid sampled observations, not continuous emotional duration'}


def _ref(module_name, module, rows, path='observations'):
    return {'module': module_name, 'model_id': module.get('model_id'),
            'path': f'modules.{module_name}.{path}', 'observation_indices': [i for i, _ in rows],
            'start_ms': int(min(v['start_ms'] for _, v in rows)),
            'end_ms': int(max(v['end_ms'] for _, v in rows)),
            'last_input_ms': max(_number(v.get('last_input_ms'), 0) or v['end_ms'] for _, v in rows)}


def _clock(ms):
    return f'{ms / 1000:g}'


def _groups(rows, key):
    groups = []
    for row in rows:
        index, value = row
        if (groups and index == groups[-1][-1][0] + 1
                and value['start_ms'] == groups[-1][-1][1]['end_ms']
                and key(value) == key(groups[-1][-1][1])):
            groups[-1].append(row)
        else:
            groups.append([row])
    return groups


def _percentile(values, fraction):
    values = sorted(values)
    index = (len(values) - 1) * fraction
    lower = int(index)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower)


def _audio(module, result):
    observations = _observations(module)
    valid = [(i, v) for i, v in enumerate(observations) if _valid(v, audio=True)]
    result['coverage']['audio'] = _coverage(observations, valid)
    speech = [(i, v) for i, v in valid
              if (_number((v.get('values') or {}).get('speech_fraction'), 0, 1) or 0) >= .5]
    result['coverage']['audio']['speech_reference_candidates'] = len(speech)
    for metric, field, threshold in [('pitch', 'f0_hz', 2.0), ('energy', 'rms_dbfs', 3.0)]:
        rows = [(i, v) for i, v in speech
                if _number((v.get('values') or {}).get(field), 60 if metric == 'pitch' else -160,
                           500 if metric == 'pitch' else 0) is not None
                and (metric != 'pitch' or (_number(v['values'].get('f0_voiced_frames'), 0) or 0) >= 5)]
        result['coverage']['audio'][f'{metric}_eligible_windows'] = len(rows)
        if len(rows) < PARAMETERS['minimum_reference_windows'] + PARAMETERS['minimum_audio_event_windows']:
            continue
        whole_median = statistics.median(v['values'][field] for _, v in rows)
        def difference(value, baseline):
            return 12 * math.log2(value / baseline) if metric == 'pitch' else value - baseline
        labelled = []
        for i, value in rows:
            delta = difference(value['values'][field], whole_median)
            if abs(delta) >= threshold:
                labelled.append((i, {**value, '_direction': 'higher' if delta > 0 else 'lower'}))
        candidates = []
        for group in _groups(labelled, lambda v: v['_direction']):
            if len(group) < PARAMETERS['minimum_audio_event_windows']:
                continue
            indices = {i for i, _ in group}
            baseline_rows = [(i, v) for i, v in rows if i not in indices]
            if (len(baseline_rows) < PARAMETERS['minimum_reference_windows']
                    or sum(v['end_ms'] - v['start_ms'] for _, v in baseline_rows)
                    < PARAMETERS['minimum_reference_duration_ms']):
                continue
            measured = statistics.median(v['values'][field] for _, v in group)
            baseline = statistics.median(v['values'][field] for _, v in baseline_rows)
            delta = difference(measured, baseline)
            if abs(delta) < threshold:
                continue
            start, end = group[0][1]['start_ms'], group[-1][1]['end_ms']
            higher = delta > 0
            title = ('音调偏高' if higher else '音调偏低') if metric == 'pitch' else ('这段录音更响' if higher else '这段录音更轻')
            noun, unit, raw_unit = ('音调', '半音', 'Hz') if metric == 'pitch' else ('录音能量', 'dB', 'dBFS')
            description = (f'{_clock(start)}–{_clock(end)} 秒，{noun}较本题其他有效有声窗'
                           f'{"高" if higher else "低"} {abs(delta):.1f} {unit}。')
            candidates.append((abs(delta), {'kind': f'audio_{metric}', 'start_ms': start,
                'end_ms': end, 'anchor_ms': start, 'title': title, 'description': description,
                'relation': 'measured_change', 'quality': 'valid', 'geometry': None,
                'measurements': {'metric': field, 'median': round(measured, 3), 'unit': raw_unit,
                                 'difference': round(delta, 3), 'difference_unit': unit,
                                 'sampled_windows': len(group)},
                'reference': {'scope': 'same_answer_other_valid_speech_windows',
                    'method': 'median of per-window values, event windows excluded',
                    'median': round(baseline, 3), 'unit': raw_unit,
                    'sampled_windows': len(baseline_rows),
                    'observation_indices': [i for i, _ in baseline_rows],
                    'not_a_neutral_or_personality_baseline': True},
                'evidence_refs': [_ref('audio', module, group), _ref('audio', module, baseline_rows)],
                'limitations': ['同一录音内比较；音调不等于音色，录音能量受设备增益和距离影响。',
                                '显示阈值尚未经过本项目独立听感标注验证。']}))
        result['events'].extend(v for _, v in sorted(candidates, key=lambda item: -item[0])[:4])
    pitch_values = [v['values']['f0_hz'] for _, v in speech
                    if _number(v['values'].get('f0_hz'), 60, 500) is not None
                    and (_number(v['values'].get('f0_voiced_frames'), 0) or 0) >= 5]
    if len(pitch_values) >= 5:
        low, high = _percentile(pitch_values, .1), _percentile(pitch_values, .9)
        result['audio_summary']['pitch'] = {'median_hz': round(statistics.median(pitch_values), 3),
            'p10_hz': round(low, 3), 'p90_hz': round(high, 3),
            'p10_p90_span_semitones': round(12 * math.log2(high / low), 3),
            'unit_of_analysis': 'per-second F0 medians, not within-frame pitch variation',
            'sampled_windows': len(pitch_values)}
        result['overview'].append(f'本题有效逐秒音高的主要范围为 {low:.0f}–{high:.0f} Hz（10%–90%分位），跨度 {12 * math.log2(high / low):.1f} 个半音。')
    summary = module.get('summary') or {}
    for key in ('duration_ms', 'speech_ms', 'pause_count', 'pause_ms', 'transcript_units',
                'speech_rate_units_per_min', 'articulation_rate_units_per_speech_min'):
        value = _number(summary.get(key), 0)
        result['audio_summary'][key] = value
    result['audio_summary']['rate_unit'] = '中文字符及英文／数字词元，不是音节；仅整题，依赖转写版本'
    rate = result['audio_summary']['speech_rate_units_per_min']
    if rate is not None:
        result['overview'].append(f'本题语速约每分钟 {rate:.0f} 个中文字符／英文数字词元；只对应本题整体。')
    if not speech:
        result['overview'].append('本题缺少可用于比较的有效有声窗口。')


def _same_time(item, anchor, track):
    return (item['start_ms'] <= anchor < item['end_ms']
            and not (track and item.get('track_segment_id') and track != item['track_segment_id']))


def _geometry(modules, anchor, track):
    for name in ('gaze', 'au'):
        geometry = (modules.get(name) or {}).get('geometry') or {}
        if geometry.get('coordinate_space') != 'normalized_sample_frame':
            continue
        for index, item in enumerate(_observations(geometry)):
            if not _valid(item, visual=True) or not _same_time(item, anchor, track):
                continue
            points = item.get('landmarks') or []
            width, height = _number(item.get('frame_width'), 1), _number(item.get('frame_height'), 1)
            if not width or not height or len(points) != 68:
                continue
            if any(not isinstance(p, (list, tuple)) or len(p) != 2
                   or any(_number(v, 0, 1) is None for v in p) for p in points):
                continue
            pixels = [(p[0] * width, p[1] * height) for p in points]
            def centroid(indices):
                return tuple(statistics.mean(pixels[i][axis] for i in indices) for axis in (0, 1))
            eye_distance = math.dist(centroid(range(36, 42)), centroid(range(42, 48)))
            if eye_distance < 5:
                continue
            metrics = {'mouth_width_over_eye_distance': math.dist(pixels[48], pixels[54]) / eye_distance,
                       'inner_lip_gap_over_eye_distance': math.dist(pixels[62], pixels[66]) / eye_distance}
            return {'source_module': name, 'observation_index': index,
                'start_ms': item['start_ms'], 'end_ms': item['end_ms'], 'anchor_ms': anchor,
                'coordinate_space': 'normalized_sample_frame', 'landmark_order': 'OpenFace 68-point (0-based)',
                'frame_width': width, 'frame_height': height,
                'landmarks': copy.deepcopy(points), 'highlight_indices': [36, 39, 42, 45, 48, 54, 62, 66],
                'metrics': {k: round(v, 4) for k, v in metrics.items()},
                'metric_definition': '2D pixel distances divided by distance between eye-centre means; pose-dependent, not muscle strength',
                'evidence_ref': _ref(name, modules[name], [(index, item)], 'geometry.observations')}
    return None


def _au_support(modules, label, anchor, track):
    module = modules.get('au') or {}
    for index, item in enumerate(_observations(module)):
        if not _valid(item, visual=True) or not _same_time(item, anchor, track):
            continue
        values = item.get('values') or {}
        intensity, presence = values.get('intensity') or {}, values.get('presence') or {}
        codes = AU_RELATED[label] or list(AU_NAMES)
        evidence = []
        for code in codes:
            score = _number(intensity.get(code), 0, 5)
            present = presence.get(code)
            if score is None and (isinstance(present, bool) or present not in (0, 1)):
                continue
            evidence.append({'code': code, 'name': AU_NAMES[code], 'intensity': score,
                'intensity_unit': 'OpenFace 0–5',
                'presence': present if not isinstance(present, bool) and present in (0, 1) else None})
        if label == 'neutral':
            evidence.sort(key=lambda v: -(v['intensity'] or 0))
            evidence = evidence[:3]
        return evidence, _ref('au', module, [(index, item)]) if evidence else None
    return [], None


def _expressions(modules, result):
    module = modules.get('expression') or {}
    observations = _observations(module)
    rows = []
    for index, item in enumerate(observations):
        if not _valid(item, visual=True):
            continue
        values = item.get('values') or {}
        scores = values.get('raw_scores') or {}
        label = values.get('label')
        if (label not in EXPRESSION or set(scores) != set(EXPRESSION)
                or any(_number(v) is None for v in scores.values())
                or scores[label] != max(scores.values())):
            continue
        rows.append((index, item))
    result['coverage']['expression'] = _coverage(observations, rows)
    counts = {label: sum(item['values']['label'] == label for _, item in rows) for label in EXPRESSION}
    result['expression_summary'] = {'label_counts': counts,
        'meaning': '有效采样帧的模型类别计数，不是情绪持续时间或真实感受比例'}
    groups = _groups(rows, lambda v: (v['values']['label'], v.get('track_segment_id')))
    groups = [v for v in groups if len(v) >= PARAMETERS['minimum_expression_samples']]
    for group in sorted(groups, key=lambda v: (-len(v), v[0][0]))[:12]:
        label = group[0][1]['values']['label']
        representative = group[0]
        geometry = None
        for row in group:
            candidate = _geometry(modules, row[1]['start_ms'], row[1].get('track_segment_id'))
            if candidate:
                representative, geometry = row, candidate
                break
        _, item = representative
        anchor = item['start_ms']
        au, au_ref = _au_support(modules, label, anchor, item.get('track_segment_id'))
        start, end = group[0][1]['start_ms'], group[-1][1]['end_ms']
        description = f'{_clock(start)}–{_clock(end)} 秒，连续 {len(group)} 个有效采样帧预测为{EXPRESSION[label]}。'
        present = [v for v in au if v['presence'] == 1 and v['intensity'] is not None]
        if present:
            v = present[0]
            description += f' 同期 {v["code"]}（{v["name"]}）强度估计 {v["intensity"]:.2f}/5。'
        refs = [_ref('expression', module, group)]
        if au_ref:
            refs.append(au_ref)
        if geometry:
            refs.append(geometry['evidence_ref'])
        result['events'].append({'kind': 'apparent_expression', 'label': label,
            'start_ms': start, 'end_ms': end, 'anchor_ms': anchor,
            'title': EXPRESSION[label] + '（模型预测）', 'description': description,
            'relation': 'co_occurring', 'quality': 'valid',
            'measurements': {'sampled_frames': len(group), 'representative_observation_index': representative[0],
                             'raw_scores': copy.deepcopy(item['values']['raw_scores']), 'au': au},
            'reference': None, 'geometry': geometry, 'evidence_refs': refs,
            'limitations': ['表情分类、AU 和点位来自不同算法；同期线索不是分类器作出判断的因果解释。',
                'AU 存在与强度分别预测，可能不一致；未出现某个 AU 不反证某类表情。',
                '采样窗口不是连续表情真值；点位距离是二维几何，不能等同肌肉强度。']})
    if rows:
        top = max(counts, key=counts.get)
        result['overview'].append(f'有效表情采样中，{EXPRESSION[top]}预测最多（{counts[top]}/{len(rows)} 帧）。')
    elif observations:
        result['overview'].append('表情采样未达到可读描述的质量要求；保留原始缺失记录。')


def _context_expressions(module, result):
    """Optional context head remains separate from face expression/AU support."""
    items = _observations(module)
    rows = []
    def labels(item):
        values = item['values']
        return tuple(v for v in CONTEXT if values['category_scores'][v] > values['category_thresholds'][v])

    for index, item in enumerate(items):
        if not _valid(item, visual=True):
            continue
        values = item.get('values') or {}
        scores, thresholds = values.get('category_scores') or {}, values.get('category_thresholds') or {}
        if set(scores) != set(CONTEXT) or set(thresholds) != set(CONTEXT):
            continue
        if any(_number(v) is None for v in [*scores.values(), *thresholds.values()]):
            continue
        capture = item.get('capture') or {}
        if not isinstance(capture, dict):
            continue
        at = _number(capture.get('at_ms'), 0)
        if at is None or not item['start_ms'] <= at < item['end_ms']:
            continue
        rows.append((index, item))
    result['coverage']['emotic'] = _coverage(items, rows)
    counts = {label: sum(label in labels(item) for _, item in rows) for label in CONTEXT}
    groups = [group for group in _groups(rows, lambda item: (labels(item), item.get('track_segment_id')))
              if labels(group[0][1])]
    selected = sorted(groups, key=lambda group: (-len(group), group[0][0]))[:4]
    result['context_expression_summary'] = {'label_frame_counts': counts,
        'valid_capture_frames': len(rows), 'group_count': len(groups), 'displayed_groups': len(selected),
        'meaning': '多标签有效采集帧计数；相邻采样归组不是连续情绪持续时间'}
    for group in selected:
        index, item = group[0]
        values, current_labels = item['values'], labels(item)
        at, last_at = item['capture']['at_ms'], group[-1][1]['capture']['at_ms']
        capture_range = f'{_clock(at)} 秒' if len(group) == 1 else f'{_clock(at)}–{_clock(last_at)} 秒'
        samples = [{'observation_index': sample_index, 'at_ms': sample['capture']['at_ms'],
                    'duration_ms': _number(sample['capture'].get('duration_ms'), 0),
                    'category_scores': copy.deepcopy(sample['values']['category_scores']),
                    'category_thresholds': copy.deepcopy(sample['values']['category_thresholds'])}
                   for sample_index, sample in group]
        result['events'].append({'kind': 'context_expression', 'labels': list(current_labels),
            'start_ms': item['start_ms'], 'end_ms': group[-1][1]['end_ms'], 'anchor_ms': at,
            'title': '场景辅助标签（研究）',
            'description': f'{capture_range}的 {len(group)} 个' + ('相邻有效采集帧中，' if len(group) > 1 else '有效采集帧中，')
                           + '场景辅助模型的' + '、'.join(CONTEXT[v] for v in current_labels) + '标签超过所用阈值。',
            'relation': 'model_estimate', 'quality': 'valid', 'geometry': None, 'reference': None,
            'measurements': {'sampled_frames': len(group), 'representative_observation_index': index,
                             'category_scores': copy.deepcopy(values['category_scores']),
                             'category_thresholds': copy.deepcopy(values['category_thresholds']),
                             'samples': samples},
            'evidence_refs': [_ref('emotic', module, group)],
            'limitations': ['人物与完整场景双路输入，不是纯面部分类；线性分值不是概率、强度或心理量表。',
                '只合并相邻、同类且质量有效的采集帧；未采样间隔不能解释为连续情绪持续。',
                '顶层分值对应首个代表帧；samples 保留每个采集帧的实际分值与阈值。']})
    if rows:
        positive = [f'{CONTEXT[label]} {count} 帧' for label, count in counts.items() if count]
        result['overview'].append(f'场景辅助模型获得 {len(rows)} 个有效采集帧；'
            + ('超过所用阈值的标签为：' + '、'.join(positive) + '（多标签计数）。' if positive
               else '四类标签均未超过所用阈值。'))
    elif items:
        result['overview'].append('场景辅助采样未达到可读描述的质量要求；保留原始缺失记录。')



def _gaze(module, modules, result):
    items = _observations(module)
    rows = [(i, v) for i, v in enumerate(items) if _valid(v, visual=True)
            and all(_number((v.get('values') or {}).get(k), -math.pi, math.pi) is not None
                    for k in ('gaze_angle_x', 'gaze_angle_y'))]
    result['coverage']['gaze'] = _coverage(items, rows)
    result['gaze_summary'] = {'camera_facing_fraction': None, 'calibration_status': 'not_calibrated',
        'meaning': '与同一跟踪段常见方向相比的视线估计变化；未测定摄像头注视比例。'}
    tracks = {}
    for row in rows:
        tracks.setdefault(row[1].get('track_segment_id'), []).append(row)
    events = []
    for track, samples in tracks.items():
        if len(samples) < 25 or sum(v['end_ms'] - v['start_ms'] for _, v in samples) < 5000:
            continue
        baseline = {k: statistics.median(v['values'][k] for _, v in samples)
                    for k in ('gaze_angle_x', 'gaze_angle_y')}
        def direction(item):
            delta = {k: math.degrees(item['values'][k] - baseline[k]) for k in baseline}
            axis = max(delta, key=lambda k: abs(delta[k]))
            return (axis, 1 if delta[axis] > 0 else -1) if abs(delta[axis]) >= 12 else None
        grouped = [g for g in _groups(samples, direction) if direction(g[0][1])
                   and len(g) >= 3 and g[-1][1]['end_ms'] - g[0][1]['start_ms'] >= 600]
        for group in grouped:
            axis, sign = direction(group[0][1]); axis_name = '水平' if axis.endswith('_x') else '垂直'
            start, end = group[0][1]['start_ms'], group[-1][1]['end_ms']
            anchor = group[0][1]['start_ms']
            delta = statistics.median(math.degrees(v['values'][axis] - baseline[axis]) for _, v in group)
            geometry = _geometry(modules, anchor, track)
            reference = _ref('gaze', module, samples)
            refs = [_ref('gaze', module, group), reference]
            if geometry: refs.append(geometry['evidence_ref'])
            events.append({'kind': 'gaze_direction', 'start_ms': start, 'end_ms': end, 'anchor_ms': anchor,
                'title': '视线方向变化（' + axis_name + '）', 'quality': 'valid', 'relation': 'within_recording_reference',
                'description': f'{_clock(start)}–{_clock(end)} 秒，视线估计在{axis_name}方向上偏离本跟踪段常见方向约 {abs(delta):.1f}°，可回看同期原话与画面。',
                'measurements': {'axis': axis, 'median_relative_degrees': round(delta, 2),
                    'sampled_frames': len(group), 'threshold_degrees': 12},
                'reference': {'kind': 'same_track_median', 'baseline_radians': baseline,
                              'start_ms': reference['start_ms'], 'end_ms': reference['end_ms']},
                'geometry': geometry, 'evidence_refs': refs,
                'limitations': ['12°和600毫秒是展示用工程阈值，尚未按本场景校准。',
                    '常见方向不是摄像头或屏幕中心；未做注视点校准，不能得出盯摄像头比例。',
                    '采样变化不代表分心、心理状态或诚信，也不用于潜力评价。']})
    result['events'].extend(sorted(events, key=lambda e: (-(e['end_ms']-e['start_ms']),e['start_ms']))[:4])
    if rows:
        result['overview'].append('视线已有有效估计；' + (f'发现 {len(events)} 段相对常见方向的持续偏移，可结合原片回看。' if events else '未发现达到展示阈值的持续方向偏移，不能解释为始终注视摄像头。'))


def summarize_multimodal(result, source_run_id=None):
    """Accept ONE result['multimodal'][i], not the containing research run.

    Source observations are immutable. Returned dictionaries own their contents.
    Empty/malformed inputs produce explicit missing summaries, never fake values.
    """
    if not isinstance(result, dict) or not isinstance(result.get('modules'), dict):
        raise ValueError('Expected one multimodal answer item with modules')
    modules = {name: value for name, value in result['modules'].items() if isinstance(value, dict)}
    summary = {'schema_version': VERSION, 'source_run_id': source_run_id or result.get('run_id'),
        'multimodal_run_id': result.get('run_id'), 'answer_id': result.get('answer_id'),
        'transcript_revision': result.get('transcript_revision'), 'media_sha256': result.get('media_sha256'),
        'usage': 'observed_only', 'overview': [], 'coverage': {}, 'events': [],
        'audio_summary': {}, 'expression_summary': {}, 'context_expression_summary': {},
        'parameters': copy.deepcopy(PARAMETERS),
        'sampling_notice': '表情和68点对应既有重采样窗口；原生帧采集模型另保留各自时间。未采样间隔不插值。',
        'references': copy.deepcopy(REFERENCES),
        'validation': {'status': 'unvalidated_for_current_domain', 'independent_reference_samples': 0,
                       'notice': '当前没有独立标注数据支持本项目准确率；描述阈值为待校准工程规则。'}}
    if isinstance(modules.get('audio'), dict):
        _audio(modules['audio'], summary)
    _expressions(modules, summary)
    if isinstance(modules.get('gaze'), dict):
        _gaze(modules['gaze'], modules, summary)
    if isinstance(modules.get('emotic'), dict):
        _context_expressions(modules['emotic'], summary)
    summary['events'].sort(key=lambda v: (v['start_ms'], v['kind']))
    total = len(summary['events'])
    summary['events'] = summary['events'][:PARAMETERS['maximum_events']]
    summary['events_truncated'] = total > len(summary['events'])
    for index, event in enumerate(summary['events']):
        event['id'] = f'observation-{index + 1:03d}'
        event['source_run_id'] = summary['source_run_id']
        event['last_input_ms'] = max(reference['last_input_ms'] for reference in event['evidence_refs'])
        for reference in event['evidence_refs']:
            reference['source_run_id'] = summary['source_run_id']
            reference['multimodal_run_id'] = summary['multimodal_run_id']
            reference['answer_id'] = summary['answer_id']
    audio_events = [v['description'] for v in summary['events'] if v['kind'].startswith('audio_')]
    summary['overview'] = audio_events[:2] + summary['overview']
    if not summary['overview']:
        summary['overview'] = ([event['description'] for event in summary['events'][:2]]
            or ['尚无足够有效观测生成可读摘要；请查看各模块状态与缺失原因。'])
    summary['status'] = 'available' if summary['events'] or any(
        v.get('valid_observations', 0) for v in summary['coverage'].values()) else 'insufficient_data'
    return summary
