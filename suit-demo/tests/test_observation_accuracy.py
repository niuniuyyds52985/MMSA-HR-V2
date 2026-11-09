import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from validate_observation_accuracy import evaluate_records, main


def rows():
    return [{'sample_id': f'sample-{i}', 'subject_id': f'subject-{i // 2}',
             'task': 'visible_expression', 'label': 'happy' if i % 2 else 'sad'} for i in range(8)]


class AccuracyTests(unittest.TestCase):
    def test_subject_split_is_stable_and_disjoint(self):
        truth = rows()
        a = evaluate_records(truth, truth, reference_kind='synthetic_fixture')
        b = evaluate_records(list(reversed(truth)), truth, reference_kind='synthetic_fixture')
        self.assertEqual(a['split'], b['split'])
        self.assertFalse(set(a['split']['test_subjects']) & set(a['split']['calibration_subjects']))
        self.assertFalse(a['project_accuracy_validated'])
        self.assertEqual(a['status'], 'synthetic_engineering_check_only')
        self.assertEqual(a['test']['visible_expression']['coverage'], 1)

    def test_abstentions_count_in_recall_and_coverage(self):
        truth = rows()
        predicted = [dict(v, label='happy' if v['label'] == 'happy' else None) for v in truth]
        result = evaluate_records(truth, predicted)['test']['visible_expression']
        self.assertEqual(result['coverage'], .5)
        self.assertEqual(result['accuracy_covered_only'], 1)
        self.assertEqual(result['accuracy_all_reference_samples'], .5)
        self.assertEqual(result['per_class']['sad']['recall'], 0)
        self.assertIsNone(result['per_class']['sad']['precision'])

    def test_empty_data_remains_unvalidated(self):
        result = evaluate_records([], [])
        self.assertEqual(result['status'], 'unvalidated_no_reference_data')
        self.assertEqual(result['test'], {})
        self.assertFalse(result['project_accuracy_validated'])

    def test_identity_interval_duplicate_and_unknown_labels_rejected(self):
        truth = rows()
        bad = [dict(truth[0], subject_id='wrong')]
        with self.assertRaises(ValueError): evaluate_records(truth, bad)
        with self.assertRaises(ValueError): evaluate_records(truth + [truth[0]], [])
        with self.assertRaises(ValueError): evaluate_records([dict(v, label='honest') for v in truth], [])
        with self.assertRaises(ValueError): evaluate_records(truth[:1], [])
        with self.assertRaises(ValueError): evaluate_records(truth, [dict(truth[0], start_ms=0, end_ms=500)])

    def test_missing_predictions_are_explicit_abstentions(self):
        result = evaluate_records(rows(), [])['test']['visible_expression']
        self.assertEqual(result['coverage'], 0)
        self.assertIsNone(result['accuracy_covered_only'])
        self.assertGreater(result['confusion_matrix']['happy']['unavailable'], 0)

    def test_cli_reads_jsonl_and_never_overwrites_result(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            reference, predicted, output = root / 'reference.jsonl', root / 'prediction.jsonl', root / 'result.json'
            text = '\n'.join(json.dumps(v) for v in rows())
            reference.write_text(text, encoding='utf-8'); predicted.write_text(text, encoding='utf-8')
            args = ['--annotations', str(reference), '--predictions', str(predicted),
                    '--out', str(output), '--reference-kind', 'synthetic_fixture']
            result = main(args)
            self.assertEqual(json.loads(output.read_text(encoding='utf-8')), result)
            with self.assertRaises(FileExistsError): main(args)


if __name__ == '__main__':
    unittest.main()
