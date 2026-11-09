"""Risk-focused fixtures: persistence, concurrency, evidence, late results and totals.

These are synthetic domain fixtures, not real applicant/model-validity evidence.
"""
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from interview_core import DomainError, InterviewCore


def key():
    return str(uuid.uuid4())


class InterviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.env = patch.dict(os.environ, {"DASHSCOPE_API_KEY": "", "INTERVIEW_CREDENTIALS_FILE": str(Path(self.directory.name) / 'no-credentials.json')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.core = InterviewCore(self.directory.name, config_path=Path(__file__).resolve().parents[1] / 'config' / 'interview.v1.json')
        self.creation_key = key()
        self.created = self.core.create_interview(self.creation_key, "synthetic-test-only")
        self.sid = self.created["session_id"]

    def answer(self, text="测试案例：我分配任务并逐日检查交付结果。", media=False, replace=None):
        state = self.core.get_state(self.sid)
        path = None
        if media:
            path = Path(self.directory.name) / "media" / (key() + ".wav")
            path.write_bytes(b"domain fixture, not actual media")
        return self.core.submit_answer(self.sid, state["current_round"]["round_id"], key(), key(), text, path=path, media_hash="fixture" if media else None, media_meta={"duration_ms": 1000, "mime_type": "audio/wav"}, replace_answer_id=replace)

    def advance(self):
        state = self.core.get_state(self.sid)
        return self.core.next_round(self.sid, key(), state["current_round"]["round_id"] if state["current_round"] else "done")

    def finish(self):
        answers = []
        for _ in range(3):
            answers.append(self.answer())
            self.advance()
        return answers

    def test_dpapi_configuration_waits_for_model_and_reports_configured(self):
        self.answer()
        self.advance()
        self.answer()
        self.advance()
        self.answer()
        with patch('interview_credentials._record', return_value={'encrypted_key': 'synthetic-ciphertext'}):
            state = self.advance()
            self.assertEqual(state['current_round']['ordinal'], 3)
            self.assertEqual(state['status'], 'answering')
            self.assertEqual(self.core.get_report(self.sid)['automatic_scoring']['status'], 'awaiting_results')

    @staticmethod
    def evidence(answer):
        return {"answer_id": answer["answer_id"], "transcript_revision": answer["transcript_revision"], "quote": answer["transcript"]}

    def ratings(self, answers, scores=(4, 3, 4)):
        return [{"question_id": a["question_id"], "score": s, "reason": "依据合成测试案例原话与该题锚点复核", "evidence": [self.evidence(a)] if s is not None else []} for a, s in zip(answers, scores)]

    def evaluate(self, answer, score=4, status="content_sufficient", branch=None):
        evidence = [self.evidence(answer)]
        result = {"question_id": answer["question_id"], "rating": {"score": score, "reason": "合成结构fixture，仅验证校验器", "evidence": evidence}, "followup": {"status": status, "branch_id": branch, "reason": "合成内容缺口判断", "evidence": evidence}}
        return self.core.apply_model_evaluation(self.sid, answer["question_id"], result, {answer["answer_id"]: answer["transcript_revision"]}, {"model": "synthetic-fixture"})

    def test_creation_durable_idempotency_and_hashed_tokens(self):
        again = InterviewCore(self.directory.name, config_path=Path(__file__).resolve().parents[1] / 'config' / 'interview.v1.json').create_interview(self.creation_key, "synthetic-test-only")
        self.assertEqual(self.created["candidate_token"], again["candidate_token"])
        self.assertEqual(self.sid, again["session_id"])
        with self.assertRaises(DomainError) as error:
            self.core.create_interview(self.creation_key, "changed")
        self.assertEqual(error.exception.code, "idempotency_conflict")
        with self.core._db() as db:
            stored = dict(db.execute("SELECT * FROM sessions").fetchone())
            operations = list(db.execute("SELECT response FROM operations"))
        self.assertNotIn(self.created["candidate_token"], str(stored))
        self.assertNotIn(self.created["hr_token"], str(operations))
        with self.assertRaises(DomainError):
            self.core.create_interview("guessable-key")

    def test_roles_and_cross_session_token_isolation(self):
        self.assertEqual(self.core.authenticate(self.sid, self.created["candidate_token"]), "candidate")
        with self.assertRaises(DomainError):
            self.core.authenticate(self.sid, self.created["candidate_token"], ("hr",))
        other = self.core.create_interview(key())
        with self.assertRaises(DomainError):
            self.core.authenticate(other["session_id"], self.created["hr_token"])
        state = self.core.get_state(self.sid)
        self.assertNotIn("reviews", state)
        self.assertNotIn("hr_token", state)

    def test_atomic_concurrent_next_and_four_question_budget(self):
        self.answer()
        first_round = self.core.get_state(self.sid)["current_round"]["round_id"]
        def step(_):
            try:
                return self.core.next_round(self.sid, key(), first_round)
            except DomainError as e:
                return e.code
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(step, range(16)))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(len(self.core.get_state(self.sid)["rounds"]), 2)
        self.answer()
        self.advance()
        self.answer()
        self.advance()
        for _ in range(10):
            self.core.next_round(self.sid, key(), "closed")
        self.assertEqual(self.core.get_state(self.sid)["budget"]["total"], 3)

    def test_answer_idempotency_changed_payload_and_explicit_replacement(self):
        rid = self.created["current_round"]["round_id"]
        idem, attempt = key(), key()
        first = self.core.submit_answer(self.sid, rid, attempt, idem, "原文一")
        replay = self.core.submit_answer(self.sid, rid, attempt, idem, "原文一")
        self.assertEqual(first["answer_id"], replay["answer_id"])
        with self.assertRaises(DomainError):
            self.core.submit_answer(self.sid, rid, attempt, idem, "篡改原文")
        with self.assertRaises(DomainError) as error:
            self.answer()
        self.assertEqual(error.exception.code, "explicit_replacement_required")
        new = self.answer(replace=first["answer_id"])
        self.assertNotEqual(new["answer_id"], first["answer_id"])
        self.assertEqual(sum(a["current"] for a in self.core.get_state(self.sid)["answers"]), 1)
        self.assertEqual(len(self.core.get_state(self.sid)["rounds"]), 1)

    def test_legal_followup_parent_then_stop(self):
        first = self.answer()
        self.evaluate(first, status="eligible", branch="leadership_F3_Q1_action")
        self.advance()
        second = self.answer()
        self.evaluate(second, status="no_legal_branch")
        self.advance()
        third = self.answer()
        self.evaluate(third, status="content_sufficient")
        state = self.advance()
        self.assertEqual(state["current_round"]["question_id"], "leadership_F3_Q1_action")
        self.assertEqual(state["budget"]["total"], 4)
        self.answer("合成追问回答：我负责分工及每日检查。")
        self.advance()
        self.assertEqual(self.core.get_state(self.sid)["status"], "interview_finished")
        decision = self.core.get_report(self.sid)["decisions"][0]
        self.assertEqual(decision["selected_branch_id"], "leadership_F3_Q1_action")
        self.assertTrue(decision["used_evidence_ids"])

    def test_content_sufficient_and_no_legal_branch_paths(self):
        for index in range(3):
            answer = self.answer()
            self.evaluate(answer, status="content_sufficient")
            self.advance()
        report = self.core.get_report(self.sid)
        self.assertEqual(report["decisions"][0]["stop_reason"], "content_sufficient")
        self.assertEqual(report["budget"]["total"], 3)

    def test_no_provider_does_not_fabricate_scores_or_fourth_question(self):
        self.finish()
        report = self.core.get_report(self.sid)
        self.assertIsNone(report["final_score"])
        self.assertEqual(report["scorable_main_count"], 0)
        self.assertEqual(report["decisions"][0]["stop_reason"], "content_evaluation_unavailable")
        self.assertEqual(len(report["coverage"]["uncovered_factor_ids"]), 5)

    def test_scores_mean_na_version_conflict_and_quote_validation(self):
        answers = self.finish()
        report = self.core.get_report(self.sid)
        version = report["version"]
        reviewed = self.core.submit_review(self.sid, version, "合成案例核验", self.ratings(answers), key())
        self.assertAlmostEqual(reviewed["final_score"], 200 / 3)
        self.assertEqual(len(reviewed["questions"]), 3)
        with self.assertRaises(DomainError) as error:
            self.core.submit_review(self.sid, version, "过时", self.ratings(answers), key())
        self.assertEqual(error.exception.code, "version_conflict")
        changed = self.core.submit_review(self.sid, reviewed["version"], "调整第三题", self.ratings(answers, (4, 3, 5)), key())
        self.assertEqual(changed["final_score"], 75)
        partial = self.core.submit_review(self.sid, changed["version"], "第三题N/A", self.ratings(answers, (4, 3, None)), key())
        self.assertIsNone(partial["final_score"])
        self.assertEqual(partial["provisional_scored_mean"], 62.5)
        bad = self.ratings(answers)
        bad[0]["evidence"][0]["quote"] = "不存在的原话"
        with self.assertRaises(DomainError) as error:
            self.core.submit_review(self.sid, partial["version"], "非法", bad, key())
        self.assertEqual(error.exception.code, "invalid_quote")
        self.assertEqual(self.core.get_report(self.sid, reviewed["version"])["final_score"], reviewed["final_score"])

    def test_revision_audit_invalidates_review_but_not_sealed_decision(self):
        answers = self.finish()
        report = self.core.get_report(self.sid)
        before_decision = copy.deepcopy(report["decisions"])
        self.core.submit_review(self.sid, report["version"], "核验", self.ratings(answers), key())
        self.core.revise_transcript(self.sid, answers[0]["answer_id"], 1, "修订后的测试原话", "核对原声修订", key(), "hr")
        report = self.core.get_report(self.sid)
        self.assertIsNone(report["final_score"])
        self.assertTrue(report["questions"][0]["review_stale"])
        self.assertEqual(before_decision, report["decisions"])
        self.assertEqual(len(report["answers"][0]["revisions"]), 2)

    def test_media_job_lease_fencing_and_late_asr_no_overwrite(self):
        answer = self.answer("候选人核对过的文字", media=True)
        job = self.core.claim_job(lease_seconds=-1)
        second = self.core.claim_job()
        self.assertNotEqual(job["lease_token"], second["lease_token"])
        with self.assertRaises(DomainError):
            self.core.complete_job(job["job_id"], job["lease_token"], {})
        self.core.heartbeat_job(second["job_id"], second["lease_token"])
        self.core.complete_job(second["job_id"], second["lease_token"], {"transcription": {"status": "succeeded", "text": "不同ASR文字"}, "audio": {"status": "succeeded", "windows": []}})
        after = self.core.get_state(self.sid)["answers"][0]
        self.assertEqual(after["transcript"], answer["transcript"])
        self.assertEqual(after["transcript_revision"], 1)
        with self.assertRaises(DomainError):
            self.core.complete_job(second["job_id"], second["lease_token"], {})

    def test_asr_creates_revision_and_content_job(self):
        answer = self.answer("", media=True)
        job = self.core.claim_job()
        self.core.complete_job(job["job_id"], job["lease_token"], {"transcription": {"status": "succeeded", "text": "真实适配器格式的合成测试转写"}})
        after = self.core.get_state(self.sid)["answers"][0]
        self.assertEqual(after["transcript_revision"], 2)
        content = self.core.claim_job(job_type="content")
        self.assertEqual(content["input_versions"], {answer["answer_id"]: 2})
        self.assertIn("model_config", content)
        self.core.block_job(content["job_id"], content["lease_token"], "No key")
        self.assertEqual(self.core.resume_blocked_content_jobs()["requeued"], 1)

    def test_model_validation_stale_inputs_no_overwrite_hr_and_late_decision(self):
        answers = self.finish()
        decision = self.core.get_report(self.sid)["decisions"]
        for answer in answers:
            self.evaluate(answer, 4)
        report = self.core.get_report(self.sid)
        self.assertEqual(report["final_score"], 75)
        self.assertEqual(report["decisions"], decision)
        self.assertEqual(report["questions"][0]["score_source"], "model_draft")
        self.core.submit_review(self.sid, report["version"], "人工复核", self.ratings(answers, (3, 3, 3)), key())
        self.evaluate(answers[0], 5)
        report = self.core.get_report(self.sid)
        self.assertEqual(report["final_score"], 50)
        self.assertEqual(report["reviews"][-1]["ratings"][0]["ai_score"], 4)
        with self.assertRaises(DomainError):
            self.evaluate(answers[0], 6)
        self.core.revise_transcript(self.sid, answers[0]["answer_id"], 1, "修订内容", "人工核对", key())
        with self.assertRaises(DomainError) as error:
            self.evaluate(answers[0], 4)
        self.assertEqual(error.exception.code, "stale_model_input")

    def test_content_completion_contract_and_cross_question_evidence(self):
        answer = self.answer()
        job = self.core.claim_job(job_type="content")
        result = {"evaluation": {"question_id": answer["question_id"], "rating": {"score": 3, "reason": "fixture", "evidence": [self.evidence(answer)]}, "followup": {"status": "content_sufficient", "branch_id": None, "reason": "fixture", "evidence": [self.evidence(answer)]}}, "provider": {"model": "fixture"}, "input_versions": job["input_versions"]}
        self.core.complete_content_job(job["job_id"], job["lease_token"], result)
        self.assertEqual(self.core.get_report(self.sid)["questions"][0]["ai_score"], 3)
        self.advance()
        other = self.answer()
        invalid = copy.deepcopy(result["evaluation"])
        invalid["question_id"] = other["question_id"]
        with self.assertRaises(DomainError) as error:
            self.core.apply_model_evaluation(self.sid, other["question_id"], invalid, {other["answer_id"]: 1}, {"model": "fixture"})
        self.assertEqual(error.exception.code, "wrong_question_evidence")


if __name__ == "__main__":
    unittest.main()
