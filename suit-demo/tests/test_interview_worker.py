import sys
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import interview_worker
import interview_research_worker
from interview_runtime import maintenance_path


class WorkerPrivacyTests(unittest.TestCase):
    def test_historical_queued_content_is_blocked_before_network(self):
        core = Mock()
        core.cloud_analysis_authorized.return_value = False
        core.claim_job.return_value = {'session_id': 'old-session', 'job_id': 'old-job',
            'lease_token': 'fixture', 'job_type': 'content',
            'answers': [{'created_at': '2020-01-01T00:00:00+00:00'}]}
        with patch.object(sys, 'argv', ['interview_worker.py', '--once']), \
             patch('interview_core.InterviewCore', return_value=core), \
             patch('interview_runtime.maintenance_active', return_value=False), \
             patch('interview_credentials._record', return_value={'enabled_at': '2026-10-24T00:00:00+00:00'}), \
             patch.object(interview_worker, 'process_job') as execute:
            interview_worker.main()
        core.block_job.assert_called_once()
        core.resume_blocked_content_jobs.assert_not_called()
        execute.assert_not_called()

    def test_maintenance_once_does_not_claim_or_execute_normal_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Mock(data_dir=Path(directory))
            maintenance_path(directory).write_text('{}', encoding='utf-8')
            with patch.object(sys, 'argv', ['interview_worker.py', '--once', '--lane', 'content']), \
                 patch('interview_core.InterviewCore', return_value=core), \
                 patch.object(interview_worker, 'process_job') as execute, \
                 contextlib.redirect_stdout(io.StringIO()):
                interview_worker.main()
            core.claim_job.assert_not_called()
            core.resume_blocked_content_jobs.assert_not_called()
            execute.assert_not_called()

    def test_maintenance_once_does_not_claim_or_execute_research_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            core = Mock(data_dir=Path(directory))
            store = Mock()
            maintenance_path(directory).write_text('{}', encoding='utf-8')
            with patch.object(sys, 'argv', ['interview_research_worker.py', '--once']), \
                 patch('interview_core.InterviewCore', return_value=core), \
                 patch('interview_research.ResearchStore', return_value=store), \
                 patch.object(interview_research_worker, 'process_research_job') as execute, \
                 contextlib.redirect_stdout(io.StringIO()):
                interview_research_worker.main()
            store.claim.assert_not_called()
            execute.assert_not_called()


if __name__ == '__main__':
    unittest.main()
