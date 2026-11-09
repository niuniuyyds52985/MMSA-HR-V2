"""ASR provenance/fallback and warm-process lifecycle without model execution."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fetch_asr
import interview_asr
import interview_worker


class ASRTests(unittest.TestCase):
    def test_verify_assets_is_offline_and_detects_modified_weights(self):
        with tempfile.TemporaryDirectory() as directory:
            data = b'synthetic-model-asset'
            path = Path(directory) / 'model.bin'
            path.write_bytes(data)
            manifest = {'model.bin': {'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}}
            with patch.object(fetch_asr, 'FILES', manifest):
                self.assertEqual(fetch_asr.verify_assets(directory)['model.bin'], manifest['model.bin']['sha256'])
                path.write_bytes(b'x' * len(data))
                with self.assertRaises(ValueError):
                    fetch_asr.verify_assets(directory)

    def test_fast_generator_is_consumed_and_provenance_is_complete(self):
        model = Mock()
        model.transcribe.return_value = (iter([types.SimpleNamespace(start=0.0, end=1.5, text=' synthetic')]),
                                        types.SimpleNamespace(language='en'))
        with patch.object(interview_asr, '_load_fast', return_value=(model, {'model.bin': 'a' * 64}, False)), \
             patch.object(interview_asr.importlib.metadata, 'version', return_value='fixture'):
            result = interview_asr.transcribe([0.1], 'en', interview_asr.FAST_ENGINE)
        self.assertEqual(result['text'], 'synthetic')
        self.assertEqual(result['segments'][0]['end'], 1.5)
        self.assertEqual(result['provenance']['repository_commit'], fetch_asr.COMMIT)
        self.assertFalse(result['provenance']['network_used'])
        options = model.transcribe.call_args.kwargs
        self.assertEqual(options['beam_size'], 1)
        self.assertFalse(options['condition_on_previous_text'])
        self.assertFalse(options['vad_filter'])

    def test_missing_fast_engine_is_explicit_local_legacy_fallback(self):
        model = Mock()
        model.transcribe.return_value = {'text': 'fixture', 'segments': [], 'language': 'en'}
        with patch.object(interview_asr, '_load_fast', side_effect=ValueError('private-path-not-disclosed')), \
             patch('interview_media._load_model', return_value=(model, 'b' * 64)):
            result = interview_asr.transcribe([0.1], 'en', interview_asr.FAST_ENGINE)
        self.assertEqual(result['provenance']['engine'], interview_asr.LEGACY_ENGINE)
        self.assertEqual(result['provenance']['fallback']['error_type'], 'ValueError')
        self.assertNotIn('private-path', json.dumps(result))
        model.transcribe.assert_called_once_with([0.1], language='en', fp16=False, condition_on_previous_text=False)

    def test_historical_engine_does_not_load_fast_model(self):
        model = Mock()
        model.transcribe.return_value = {'text': 'fixture', 'segments': []}
        with patch.object(interview_asr, '_load_fast') as fast, \
             patch('interview_media._load_model', return_value=(model, 'c' * 64)):
            result = interview_asr.transcribe([0.1], 'zh')
        fast.assert_not_called()
        self.assertIsNone(result['provenance']['fallback'])

    def test_fast_model_is_verified_once_and_kept_local(self):
        model_type = Mock(return_value=Mock())
        with patch.object(interview_asr, '_FAST_MODEL', None), patch.object(interview_asr, '_FAST_HASHES', None), \
             patch.object(interview_asr, 'verify_assets', return_value={'model.bin': 'd' * 64}) as verify, \
             patch.dict(sys.modules, {'faster_whisper': types.SimpleNamespace(WhisperModel=model_type)}):
            first = interview_asr._load_fast()
            second = interview_asr._load_fast()
        self.assertIs(first[0], second[0])
        self.assertTrue(first[2])
        self.assertFalse(second[2])
        verify.assert_called_once()
        self.assertTrue(model_type.call_args.kwargs['local_files_only'])
        self.assertEqual(model_type.call_args.kwargs['compute_type'], 'int8')

    def test_warm_process_preserves_engine_and_restarts_after_failure(self):
        first, second = Mock(), Mock()
        first.poll.return_value = None
        second.poll.return_value = None
        first.stdin, second.stdin = io.StringIO(), io.StringIO()
        job = {'path': 'synthetic.wav', 'language': 'zh', 'asr_engine': interview_asr.FAST_ENGINE}
        with patch.object(interview_worker.subprocess, 'Popen', side_effect=[first, second]) as popen, \
             patch.object(interview_worker, 'stop_process'):
            process = interview_worker.PersistentMediaProcess()
            self.assertIs(process.submit(job, 'output-one.json'), first)
            self.assertIs(process.submit(job, 'output-two.json'), first)
            self.assertEqual(popen.call_count, 1)
            requests = [json.loads(line) for line in first.stdin.getvalue().splitlines()]
            self.assertTrue(all(r['asr_engine'] == interview_asr.FAST_ENGINE for r in requests))
            first.poll.return_value = 1
            self.assertIs(process.submit(job, 'output-three.json'), second)
            self.assertEqual(popen.call_count, 2)
            process.close()

    def test_warm_timeout_keeps_lease_failure_and_cleans_temp_result(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Mock(data_dir=directory)
            proc = Mock()
            proc.poll.return_value = None
            process = Mock()
            process.submit.return_value = proc
            job = {'job_type': 'media', 'job_id': 'fixture-job', 'lease_token': 'fixture',
                'path': 'synthetic.wav', 'language': 'zh', 'asr_engine': interview_asr.FAST_ENGINE}
            with patch.object(interview_worker.time, 'monotonic', side_effect=[0, 31]), \
                 patch.object(interview_worker, 'stop_process') as stop:
                interview_worker.process_job(core, job, 30, media_process=process)
            self.assertTrue(stop.called)
            self.assertTrue(core.fail_job.call_args.kwargs['retryable'])
            core.complete_job.assert_not_called()
            self.assertEqual(list((Path(directory) / 'worker-tmp').iterdir()), [])

    def test_success_keeps_warm_process_but_cleans_per_answer_output(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Mock(data_dir=directory)
            proc = Mock(returncode=None)
            proc.poll.return_value = None
            process = Mock()
            def submit(job, output):
                interview_worker._write_result(output, {'ok': True, 'result': {'transcription': {'status': 'silent'}}})
                return proc
            process.submit.side_effect = submit
            job = {'job_type': 'media', 'job_id': 'fixture-job', 'lease_token': 'fixture', 'path': 'synthetic.wav', 'language': 'zh'}
            with patch.object(interview_worker, 'stop_process') as stop:
                interview_worker.process_job(core, job, 30, media_process=process)
            stop.assert_not_called()
            core.complete_job.assert_called_once()
            self.assertEqual(list((Path(directory) / 'worker-tmp').iterdir()), [])

    def test_broken_warm_pipe_is_retryable_and_child_is_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Mock(data_dir=directory)
            process = Mock()
            process.submit.side_effect = BrokenPipeError()
            job = {'job_type': 'media', 'job_id': 'fixture-job', 'lease_token': 'fixture', 'path': 'synthetic.wav', 'language': 'zh'}
            interview_worker.process_job(core, job, 30, media_process=process)
            process.close.assert_called_once()
            self.assertTrue(core.fail_job.call_args.kwargs['retryable'])
            core.complete_job.assert_not_called()


if __name__ == '__main__':
    unittest.main()
