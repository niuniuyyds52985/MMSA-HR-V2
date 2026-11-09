"""Durable result cache tests; isolated SQLite files, no network or models."""

from tempfile import TemporaryDirectory
import unittest

from src.mmsa_v2.checkpoints import Checkpoints
from src.mmsa_v2.workflow import Workflow


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.cache = Checkpoints(Workflow(self.directory))

    def test_reconstruction_retains_exact_result_and_isolates_session_node_and_input(self):
        result = {"factors": [{"factor_id": "F3", "rating": 3,
                               "evidence": [{"question_id": "F3-G", "quote": "本人实际行动。"}]}],
                  "model_provenance": {"request_id": "fixture-only"}}
        self.cache.save("session-a", "question:F3-G", "input-1", result)
        rebuilt = Checkpoints(Workflow(self.directory))
        self.assertEqual(rebuilt.get("session-a", "question:F3-G", "input-1"), result)
        self.assertIsNone(rebuilt.get("session-b", "question:F3-G", "input-1"))
        self.assertIsNone(rebuilt.get("session-a", "question:F5-G", "input-1"))
        self.assertIsNone(rebuilt.get("session-a", "question:F3-G", "input-2"))
        returned = rebuilt.get("session-a", "question:F3-G", "input-1")
        returned["factors"][0]["rating"] = 1
        self.assertEqual(rebuilt.get("session-a", "question:F3-G", "input-1"), result)

    def test_failed_attempt_never_replaces_success_and_can_later_be_completed(self):
        self.cache.failed("s", "question:F3-G", "failed-input", "synthetic failure")
        self.assertIsNone(self.cache.get("s", "question:F3-G", "failed-input"))
        result = {"rating": 3, "original_evidence": "测试原话"}
        self.cache.save("s", "question:F3-G", "failed-input", result)
        self.cache.failed("s", "question:F3-G", "failed-input", "later failed attempt")
        self.assertEqual(self.cache.get("s", "question:F3-G", "failed-input"), result)
        with self.cache.store.connection() as db:
            row = db.execute("SELECT status,error FROM analysis_checkpoints WHERE session_id=?", ("s",)).fetchone()
        self.assertEqual(row["status"], "succeeded")
        self.assertIsNone(row["error"])


if __name__ == "__main__":
    unittest.main()
