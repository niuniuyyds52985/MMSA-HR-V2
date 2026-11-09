"""Evaluate anonymous reference JSONL against frozen predictions by subject.

This utility does not label people, train models, read media or call a service.
References are user-supplied, and their independence is reported, not assumed.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re

TASK_LABELS = {
    'visible_expression': ['angry', 'disgust', 'fearful', 'happy', 'neutral', 'sad', 'surprised'],
    'relative_pitch': ['lower', 'similar', 'higher'],
    'relative_recording_energy': ['lower', 'similar', 'higher'],
    'relative_pitch_variation': ['smaller', 'similar', 'larger'],
}
IDENTIFIER = re.compile(r'[A-Za-z0-9_-]{1,80}\Z')


def _validate(records, prediction=False):
    indexed = {}
    allowed = {'sample_id', 'subject_id', 'task', 'label', 'start_ms', 'end_ms',
               'protocol_version', 'source_run_id'}
    for row in records:
        if not isinstance(row, dict) or set(row) - allowed:
            raise ValueError('Rows must use the documented anonymous fields only')
        if any(not isinstance(row.get(key), str) or not IDENTIFIER.fullmatch(row[key])
               for key in ('sample_id', 'subject_id')):
            raise ValueError('sample_id and subject_id must be anonymous ASCII identifiers')
        if row['sample_id'] in indexed:
            raise ValueError('Duplicate sample_id')
        if row.get('task') not in TASK_LABELS:
            raise ValueError('Unsupported task')
        if row.get('label') not in TASK_LABELS[row['task']] and not (prediction and row.get('label') is None):
            raise ValueError('Unsupported or missing label')
        if ('start_ms' in row) != ('end_ms' in row):
            raise ValueError('Both temporal bounds are required together')
        if 'start_ms' in row:
            a, b = row['start_ms'], row['end_ms']
            if (isinstance(a, bool) or isinstance(b, bool) or not isinstance(a, (int, float))
                    or not isinstance(b, (int, float)) or not math.isfinite(a) or not math.isfinite(b)
                    or a < 0 or b <= a):
                raise ValueError('Invalid temporal bounds')
        indexed[row['sample_id']] = dict(row)
    return indexed


def _metrics(rows, predictions, labels):
    matrix = {label: {predicted: 0 for predicted in [*labels, 'unavailable']} for label in labels}
    correct = covered = 0
    for row in rows:
        predicted = predictions.get(row['sample_id'], {}).get('label')
        matrix[row['label']][predicted if predicted is not None else 'unavailable'] += 1
        covered += predicted is not None
        correct += predicted == row['label']
    classes = {}
    for label in labels:
        tp = matrix[label][label]
        predicted_count = sum(matrix[truth][label] for truth in labels)
        support = sum(matrix[label].values())
        precision = tp / predicted_count if predicted_count else None
        recall = tp / support if support else None
        f1 = 2 * tp / (predicted_count + support) if predicted_count + support else None
        classes[label] = {'support': support, 'predicted': predicted_count, 'true_positive': tp,
                          'precision': precision, 'recall': recall, 'f1': f1}
    supported_f1 = [value['f1'] for value in classes.values() if value['support']]
    total = len(rows)
    return {'reference_samples': total, 'reference_subjects': len({v['subject_id'] for v in rows}),
        'covered_samples': covered, 'abstained_or_missing_samples': total - covered,
        'coverage': covered / total if total else None,
        'accuracy_all_reference_samples': correct / total if total else None,
        'accuracy_covered_only': correct / covered if covered else None,
        'macro_f1_supported_classes': sum(supported_f1) / len(supported_f1) if supported_f1 else None,
        'matrix_rows': 'reference labels', 'matrix_columns': 'prediction labels plus unavailable',
        'confusion_matrix': matrix, 'per_class': classes,
        'status': 'computed_sample_estimate' if total else 'unvalidated_no_reference_samples'}


def evaluate_records(annotations, predictions, *, seed='observation-v1', test_fraction=.3,
                     reference_kind='unspecified'):
    """Deterministic subject-disjoint split; no threshold fitting or random search."""
    if not 0 < test_fraction < 1:
        raise ValueError('test_fraction must be between zero and one')
    if reference_kind not in ('unspecified', 'synthetic_fixture', 'independent_human'):
        raise ValueError('Unsupported reference_kind')
    truth, predicted = _validate(annotations), _validate(predictions, prediction=True)
    if set(predicted) - set(truth):
        raise ValueError('Prediction sample without a matching reference')
    for key, row in predicted.items():
        for field in ('subject_id', 'task', 'start_ms', 'end_ms'):
            if row.get(field) != truth[key].get(field):
                raise ValueError('Prediction and reference identity, task or interval mismatch')
    subjects = sorted({row['subject_id'] for row in truth.values()},
                      key=lambda v: hashlib.sha256((seed + '\0' + v).encode()).hexdigest())
    if len(subjects) == 1:
        raise ValueError('At least two subjects are required for a subject-disjoint split')
    test_size = min(len(subjects) - 1, max(1, math.ceil(len(subjects) * test_fraction))) if subjects else 0
    test_subjects = set(subjects[:test_size])
    calibration_subjects = set(subjects[test_size:])
    output = {'schema_version': 'observation-accuracy.v1',
        'status': 'unvalidated_no_reference_data' if not truth else
                  'synthetic_engineering_check_only' if reference_kind == 'synthetic_fixture' else
                  'sample_estimates_reference_independence_unverified',
        'reference_kind_declared': reference_kind, 'project_accuracy_validated': False,
        'split': {'method': 'sha256 seed-plus-anonymous-subject ranking', 'seed': seed,
            'test_fraction_requested': test_fraction, 'calibration_subjects': sorted(calibration_subjects),
            'test_subjects': sorted(test_subjects), 'subject_overlap': 0,
            'policy': 'freeze this split before calibration; changing seed after seeing test labels is invalid'},
        'calibration': {}, 'test': {},
        'limitations': ['工具不核实标注独立性、抽样代表性或标注者质量；不能把输入标注自动当成真实内心状态。',
            '校准集只供调节显示规则；调整后必须冻结规则并仅在未参与调整的测试被试上报告结果。',
            '按人物分组，不能将同一人的不同帧混入两组。样本少或类别缺失时保留缺失，不伪造准确率。',
            '拒判计入 coverage；按全体参考样本计算的召回包含拒判，不能只选有输出片段。']}
    for split, group in [('calibration', calibration_subjects), ('test', test_subjects)]:
        for task in sorted({row['task'] for row in truth.values()}):
            rows = [row for row in truth.values() if row['subject_id'] in group and row['task'] == task]
            output[split][task] = _metrics(rows, predicted, TASK_LABELS[task])
    output['input_counts'] = {'annotations': len(truth), 'predictions': len(predicted),
                              'subjects': len(subjects), 'tasks': dict(Counter(v['task'] for v in truth.values()))}
    return output


def _read_jsonl(path):
    rows = []
    with Path(path).open(encoding='utf-8-sig') as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ValueError(f'Invalid JSON on line {line_number}') from error
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--annotations', required=True, help='Anonymous reference JSONL')
    parser.add_argument('--predictions', required=True, help='Frozen prediction JSONL; null label means abstention')
    parser.add_argument('--out', required=True, help='New result JSON path; existing output is never overwritten')
    parser.add_argument('--seed', default='observation-v1')
    parser.add_argument('--test-fraction', type=float, default=.3)
    parser.add_argument('--reference-kind', choices=['unspecified', 'synthetic_fixture', 'independent_human'], default='unspecified')
    args = parser.parse_args(argv)
    result = evaluate_records(_read_jsonl(args.annotations), _read_jsonl(args.predictions),
        seed=args.seed, test_fraction=args.test_fraction, reference_kind=args.reference_kind)
    target = Path(args.out)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
    print(json.dumps({'status': result['status'], 'project_accuracy_validated': False,
                      'input_counts': result['input_counts']}, ensure_ascii=False))
    return result


if __name__ == '__main__':
    main()
