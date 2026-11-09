"""V2 submission contract: mocked cloud transports, real API/database/report path.

All people, utterances, audio and cloud responses in this file are fixtures.
No production data or network service is used.
"""

import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.mmsa_v2.access import operator_key
from src.mmsa_v2.api import create_app
from src.mmsa_v2.automatic import LocalScoring
from src.mmsa_v2.workflow import Workflow
from tests.v2.test_auto_submission import simulation_docx, stub_analysis, wav_fixture


class CloudSubmissionTests(unittest.TestCase):
    def setUp(self):
        provider = patch.dict(os.environ, {"MMSA_V2_CONTENT_PROVIDER": "bailian"})
        provider.start()
        self.addCleanup(provider.stop)
        network = patch("requests.post", side_effect=AssertionError("Tests must not call a real provider"))
        self.network = network.start()
        self.addCleanup(network.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.client = TestClient(create_app(directory.name))
        self.addCleanup(self.client.close)
        self.store = self.client.app.state.workflow
        self.headers = {"Authorization": "Bearer " + operator_key(Path(directory.name))}
        self.replies = json.loads((Path(__file__).parent / "fixtures/short_oral_replies.json").read_text(encoding="utf-8"))
        batch = self.post("/batches", {"name": "云评分隔离测试"}, teacher=True)
        response = self.client.post(f"/v2/api/batches/{batch['id']}/resumes",
                                    files={"file": ("测试学生.docx", simulation_docx())}, headers=self.headers)
        response.raise_for_status()
        candidate = response.json()["candidates"][0]
        self.initial = self.post(f"/batches/{batch['id']}/sessions", {"candidate_id": candidate["id"]}, teacher=True)
        self.sid = self.initial["id"]
        self.teacher = "/sessions/" + self.sid
        self.student = "/c/" + self.initial["token"]
        self.content_inputs = []

    def post(self, path, body, *, teacher=False, expected=200):
        response = self.client.post("/v2/api" + path, json=body, headers=self.headers if teacher else {})
        self.assertEqual(response.status_code, expected, response.text[:350])
        return response.json()

    def report(self, version=None):
        response = self.client.get("/v2/api" + self.teacher + "/report", headers=self.headers,
                                   params={} if version is None else {"version": version})
        response.raise_for_status()
        return response.json()

    def answer_all(self, *, text=False, introduction=False):
        if introduction:
            response = self.client.post("/v2/api" + self.student + "/introduction/media",
                files={"file": ("warmup-only.wav", wav_fixture(), "audio/wav")},
                data={"version": str(self.initial["version"]), "text": "暖场介绍不参与计分。"})
            response.raise_for_status()
        for question in self.initial["plan"]:
            state = self.post(self.student + "/answers/" + question["id"], {"value": question["options"][0]["id"]})
        oral = [question["id"] for question in state["plan"] if question["kind"] == "text"]
        self.assertCountEqual(oral, self.replies)
        for qid in oral:
            if text:
                self.post(self.student + "/answers/" + qid, {"value": self.replies[qid]})
            response = self.client.post("/v2/api" + self.student + "/answers/" + qid + "/media",
                files={"file": (qid + ".wav", wav_fixture(), "audio/wav")})
            response.raise_for_status()

    def cloud_asr(self, path, answer):
        qid = Path(answer["media"]["name"]).stem
        self.assertIn(qid, self.replies)
        self.assertTrue(path.is_file())
        return {"transcript": self.replies[qid],
                "provenance": {"provider": "aliyun-model-studio", "model": "qwen3-asr-flash",
                               "status": "succeeded", "request_id": "fixture-asr-" + qid}}

    def cloud_question(self, state, bank, qid):
        self.content_inputs.append(copy.deepcopy(state))
        text = state["answers"][qid].get("text") or state["answers"][qid].get("transcript")
        self.assertTrue(text)
        factor = {"factor_id": qid.split("-")[0], "rating": 3,
                  "reason": "测试响应：对照本场冻结锚点引用本人行动。",
                  "evidence": [{"question_id": qid, "quote": text}]}
        return {"source": "bailian_auto", "author": "Qwen fixture", "factors": [factor],
                "model_provenance": {"provider": "aliyun-model-studio", "model": "qwen-fixture",
                                     "request_id": "fixture-question-" + qid, "validation": "passed",
                                     "factor_details": [{**factor, "policy_result": {
                                         "proposed_rating": 3, "accepted_rating": 3, "adjustments": []}}],
                                     "input_sha256": "f" * 64, "bank_version": bank["version"]}}

    def cloud_summary(self, state, bank, results):
        self.assertEqual(len(results), 3)
        self.assertCountEqual([f["factor_id"] for result in results for f in result["factors"]],
                              [qid.split("-")[0] for qid in self.replies])
        qid = "F3-G"
        text = state["answers"][qid].get("text") or state["answers"][qid].get("transcript")
        return {"source": "bailian_auto", "author": "Qwen fixture",
                "experience_score": 60,
                "experience_reason": "测试响应：本场两段经历分别计三分。",
                "experience_evidence": {"question_id": qid, "quote": text},
                "summary": "测试响应：三项正式问答已按原话核对。", "next_steps": [],
                "model_provenance": {"provider": "aliyun-model-studio", "model": "qwen-fixture",
                                     "request_id": "fixture-content-1", "validation": "passed",
                                     "factor_details": [],
                                     "input_sha256": "f" * 64, "bank_version": bank["version"],
                                     "usage": {"total_tokens": 123}}}

    def test_audio_only_answers_reach_factor_scores_and_pinned_report(self):
        self.answer_all(introduction=True)
        before = self.store.session(self.sid)
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis) as local, \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=self.cloud_asr) as asr, \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question) as content, \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary) as summary:
            queued = self.post(self.student + "/finish", {})
        self.assertEqual(local.call_count, 3)
        self.assertEqual(asr.call_count, 3)
        self.assertEqual(content.call_count, 3)
        summary.assert_called_once()
        self.network.assert_not_called()
        current = self.report()
        self.assertEqual(current["auto_scoring"]["status"], "done")
        self.assertEqual(current["auto_scoring"]["engine"], "bailian-nodes-v2")
        self.assertEqual(current["auto_scoring"]["content_rated"], 3)
        self.assertEqual(current["input_version"], before["input_version"] + 3)
        self.assertEqual(current["draft"]["input_version"], current["input_version"])
        self.assertEqual(current["draft"]["source"], "mixed")
        self.assertEqual(current["draft"]["model_provenance"]["request_id"], "fixture-content-1")
        self.assertEqual(len(current["draft"]["model_provenance"]["factor_details"]), 3)
        self.assertEqual(current["draft"]["provenance"]["operation_score"]["source"], "local_auto")
        self.assertEqual(len(current["answers"]), 8)
        self.assertEqual(len(current["questions"]), 8)
        self.assertFalse(current["introduction"].get("processing"))
        for qid, expected in self.replies.items():
            self.assertEqual(current["answers"][qid]["transcript"], expected)
            self.assertEqual(current["answers"][qid]["transcription_provenance"]["request_id"], "fixture-asr-" + qid)
            factor = next(f for f in current["factors"] if f["factor_id"] == qid.split("-")[0])
            self.assertEqual(factor["source"], "bailian_auto")
            self.assertEqual(factor["evidence"][0]["quote"], expected)
        self.assertTrue(all(f["rating"] is not None for f in current["factors"]))
        self.assertAlmostEqual(current["total"], sum(c["contribution"] for c in current["components"].values()))
        historic = self.report(queued["version"])
        self.assertIsNone(historic["total"])
        self.assertTrue(all(not historic["answers"][qid].get("transcript") for qid in self.replies))
        html = self.client.get("/v2/api" + self.teacher + "/report.html", headers=self.headers).text
        self.assertIn("Qwen fixture", html)
        self.assertIn(self.replies["F3-G"], html)

    def test_changed_affect_method_reopens_done_job_without_repeating_text_models(self):
        self.answer_all(text=True)
        with patch("src.mmsa_v2.auto_media.affect_method_fingerprint", return_value="previous-policy"), \
             patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis), \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question), \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary):
            self.post(self.student + "/finish", {})
        before = self.report()
        self.assertEqual(before["auto_scoring"]["status"], "done")
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis) as media, \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=AssertionError("Keep text")) as asr, \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=AssertionError("Keep text scores")) as questions, \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=AssertionError("Keep summary")) as summary:
            self.post(self.teacher + "/auto-score", {"version": before["version"]}, teacher=True)
        self.assertEqual(media.call_count, 3)
        asr.assert_not_called()
        questions.assert_not_called()
        summary.assert_not_called()
        after = self.report()
        self.assertEqual(after["auto_scoring"]["status"], "done")
        self.assertEqual(after["input_version"], before["input_version"])
        self.assertNotEqual(after["auto_scoring"]["method_fingerprint"], "previous-policy")
        self.assertEqual(self.report(before["version"])["auto_scoring"]["method_fingerprint"], "previous-policy")
        self.assertTrue(all(after["auto_scoring"]["nodes"]["question:" + qid]["cached"] for qid in self.replies))

    def test_failed_new_affect_model_is_retryable_and_does_not_keep_old_current_score(self):
        from src.mmsa_v2.auto_media import AffectAnalysisError
        from src.mmsa_v2.automatic import media_checkpoint_key
        self.answer_all(text=True)
        with patch("src.mmsa_v2.auto_media.affect_method_fingerprint", return_value="previous-policy"), \
             patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis), \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question), \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary):
            self.post(self.student + "/finish", {})
        before = self.report()
        def unavailable(path, answer):
            raise AffectAnalysisError("声音情绪模型未安装，请安装后重试。",
                processing={"media_sha256": answer["media"]["sha256"], "native_fixture_saved": True})
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=unavailable), \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=AssertionError("Keep text scores")) as questions, \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=AssertionError("Keep summary")) as summary:
            self.post(self.teacher + "/auto-score", {"version": before["version"]}, teacher=True)
        questions.assert_not_called()
        summary.assert_not_called()
        failed = self.report()
        self.assertEqual(failed["auto_scoring"]["status"], "partial")
        self.assertIsNone(failed["components"]["multimodal"]["score"])
        self.assertEqual(failed["auto_scoring"]["content_rated"], 3)
        worker = LocalScoring(self.store)
        for qid in self.replies:
            answer = failed["answers"][qid]
            self.assertIsNone(answer["state_estimate"])
            self.assertIn("未安装", answer["state_estimate_error"]["message"])
            self.assertTrue(answer["processing"]["native_fixture_saved"])
            key = media_checkpoint_key(answer["media"]["sha256"])
            self.assertIsNone(worker.checkpoints.get(self.sid, "media:" + qid, key))
            self.assertEqual(failed["auto_scoring"]["nodes"]["media:" + qid]["status"], "failed")
        self.assertEqual(self.report(before["version"])["components"]["multimodal"]["score"], 50)
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis) as media, \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=AssertionError("Keep text scores")) as questions, \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=AssertionError("Keep summary")) as summary:
            self.post(self.teacher + "/auto-score", {"version": failed["version"]}, teacher=True)
        self.assertEqual(media.call_count, 3)
        questions.assert_not_called()
        summary.assert_not_called()
        ready = self.report()
        self.assertEqual(ready["auto_scoring"]["status"], "done")
        self.assertTrue(all(ready["answers"][qid]["state_estimate_error"] is None for qid in self.replies))

    def test_empty_transcription_is_partial_and_never_calls_a_text_model(self):
        self.answer_all()
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis), \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", return_value={"transcript": "", "provenance": {"status": "empty"}}), \
             patch("src.mmsa_v2.auto_content.content_draft", side_effect=AssertionError("No silent regex fallback")) as local:
            self.post(self.student + "/finish", {})
        current = self.report()
        self.network.assert_not_called()
        local.assert_not_called()
        self.assertEqual(current["auto_scoring"]["status"], "partial")
        self.assertEqual(current["auto_scoring"]["content_rated"], 0)
        self.assertIsNone(current["total"])
        self.assertEqual(sum(f["rating"] is not None for f in current["factors"]), 5)
        for factor in current["draft"]["factors"]:
            self.assertIsNone(factor["rating"])
            self.assertIn("转写", factor["reason"])
            self.assertNotIn("未辨认出本人行为", factor["reason"])
        self.assertTrue(any("转写" in issue for issue in current["auto_scoring"]["issues"]))

    def test_content_failure_keeps_transcripts_retry_does_not_repeat_asr_and_teacher_wins(self):
        self.answer_all()
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis), \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=self.cloud_asr), \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=ValueError("fixture unavailable")), \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=AssertionError("Failed question cannot summarize")) as summary, \
             patch("src.mmsa_v2.auto_content.content_draft", side_effect=AssertionError("No silent regex fallback")) as local:
            self.post(self.student + "/finish", {})
        failed = self.report()
        self.assertEqual(failed["auto_scoring"]["status"], "partial")
        self.assertEqual(sum(f["rating"] is not None for f in failed["factors"]), 5)
        self.assertTrue(all(failed["answers"][qid]["transcript"] == text for qid, text in self.replies.items()))
        local.assert_not_called()
        summary.assert_not_called()
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis), \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=AssertionError("Already transcribed")) as asr, \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question), \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary):
            self.post(self.teacher + "/auto-score", {"version": failed["version"]}, teacher=True)
        asr.assert_not_called()
        ready = self.report()
        self.assertEqual(ready["auto_scoring"]["status"], "done")
        self.assertEqual(ready["input_version"], failed["input_version"])
        self.assertIsNotNone(ready["total"])
        self.assertIsNone(self.report(failed["version"])["total"])
        manual = self.post(self.teacher + "/local-draft", {
            "version": ready["version"], "input_version": ready["input_version"], "author": "测试复核老师",
            "operation_score": 80, "operation_reason": "人工核对测试记录。"}, teacher=True)
        self.post(self.teacher + "/auto-score", {"version": manual["version"]}, teacher=True, expected=409)
        self.assertEqual(self.report()["components"]["operation"]["score"], 80)
        self.network.assert_not_called()

    def test_old_local_draft_can_upgrade_without_changing_choices(self):
        self.answer_all(text=True)
        with patch.dict(os.environ, {"MMSA_V2_CONTENT_PROVIDER": "local_demo"}), \
             patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis):
            self.post(self.student + "/finish", {})
        old = self.report()
        self.assertEqual(old["draft"]["source"], "local_auto")
        self.assertEqual(old["auto_scoring"]["status"], "done")
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis), \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=AssertionError("Typed answers do not need ASR")) as asr, \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question), \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary):
            self.post(self.teacher + "/auto-score", {"version": old["version"]}, teacher=True)
        asr.assert_not_called()
        current = self.report()
        self.assertEqual(current["auto_scoring"]["engine"], "bailian-nodes-v2")
        self.assertEqual(current["draft"]["source"], "mixed")
        for before, after in zip(old["factors"], current["factors"]):
            if before.get("rating_source") == "choice_key":
                self.assertEqual(before["rating"], after["rating"])
        self.assertEqual(self.report(old["version"])["draft"]["source"], "local_auto")

    def test_newer_teacher_edit_supersedes_inflight_cloud_result(self):
        self.answer_all()
        def update_during_request(state, bank, qid):
            draft = self.cloud_question(state, bank, qid)
            live = self.store.session(self.sid)
            self.store.draft(self.sid, {"source": "local_review", "author": "并发复核老师",
                "version": live["version"], "input_version": live["input_version"],
                "operation_score": 70, "operation_reason": "保留在模型返回前保存的人工修订。"})
            return draft
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis), \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=self.cloud_asr), \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=update_during_request) as question, \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=AssertionError("Manual evaluation wins")) as summary:
            self.post(self.student + "/finish", {})
        current = self.report()
        self.assertEqual(current["auto_scoring"]["status"], "superseded")
        self.assertEqual(current["draft"]["source"], "local_review")
        self.assertEqual(current["components"]["operation"]["score"], 70)
        self.assertFalse(current["draft"].get("model_provenance"))
        self.assertTrue(all(current["answers"][qid].get("transcript") == text for qid, text in self.replies.items()))
        question.assert_called_once()
        summary.assert_not_called()
        self.network.assert_not_called()

    def test_one_failed_question_retains_other_scores_and_retry_only_repeats_failure(self):
        self.answer_all()
        failed_qid = "F5-G"

        def fail_one(state, bank, qid):
            if qid == failed_qid:
                raise ValueError("synthetic single-node failure")
            return self.cloud_question(state, bank, qid)

        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis), \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=self.cloud_asr), \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=fail_one) as first, \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary) as summary:
            self.post(self.student + "/finish", {})
        failed = self.report()
        self.assertEqual(first.call_count, 3)
        summary.assert_not_called()
        self.assertEqual(failed["auto_scoring"]["status"], "partial")
        self.assertEqual(failed["auto_scoring"]["content_rated"], 2)
        self.assertEqual(sum(f["rating"] is not None for f in failed["factors"]), 7)
        preserved = {f["factor_id"]: f for f in failed["factors"] if f.get("source") == "bailian_auto"}
        self.assertEqual(set(preserved), {"F3", "F6"})

        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=AssertionError("Media cache must survive")) as media, \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=AssertionError("Transcript must survive")) as asr, \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question) as retry, \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary) as summary:
            self.post(self.teacher + "/auto-score", {"version": failed["version"]}, teacher=True)
        media.assert_not_called()
        asr.assert_not_called()
        retry.assert_called_once()
        self.assertEqual(retry.call_args.args[2], failed_qid)
        summary.assert_called_once()
        current = self.report()
        self.assertEqual(current["auto_scoring"]["status"], "done")
        self.assertEqual(current["input_version"], failed["input_version"])
        for factor in current["factors"]:
            if factor["factor_id"] in preserved:
                self.assertEqual(factor, preserved[factor["factor_id"]])
                self.assertTrue(current["auto_scoring"]["nodes"]["question:" + factor["factor_id"] + "-G"]["cached"])
        self.assertIsNone(self.report(failed["version"])["total"])
        self.network.assert_not_called()

    def test_correcting_one_transcript_reuses_other_questions_and_local_observations(self):
        self.answer_all()
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis), \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=self.cloud_asr), \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question), \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary):
            self.post(self.student + "/finish", {})
        before = self.report()
        qid = "F3-G"
        corrected_text = self.replies[qid] + "这句话是本测试核对原片后补正的本人行动。"
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=AssertionError("Unchanged media uses cache")) as media, \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=AssertionError("Human correction is authoritative")) as asr, \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question) as question, \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary) as summary:
            self.post(self.teacher + "/answers/" + qid + "/correct-transcript", {
                "version": before["version"], "input_version": before["input_version"],
                "media_sha256": before["answers"][qid]["media"]["sha256"],
                "reviewer": "测试核对老师", "reason": "核对测试音轨后补正原话。",
                "transcript": corrected_text}, teacher=True)
        media.assert_not_called()
        asr.assert_not_called()
        question.assert_called_once()
        self.assertEqual(question.call_args.args[2], qid)
        summary.assert_called_once()
        current = self.report()
        self.assertEqual(current["auto_scoring"]["status"], "done")
        self.assertEqual(current["input_version"], before["input_version"] + 1)
        self.assertEqual(current["answers"][qid]["transcript"], corrected_text)
        self.assertEqual(current["answers"][qid]["transcription_provenance"]["provider"], "human_review")
        for old, new in zip(before["factors"], current["factors"]):
            if old["factor_id"] != "F3":
                self.assertEqual(old, new)
        self.assertEqual(len(current["draft"]["model_provenance"]["factor_details"]), 3)
        self.assertEqual(self.report(before["version"])["answers"][qid]["transcript"], self.replies[qid])
        self.network.assert_not_called()

    def test_summary_failure_retries_summary_without_repeating_accepted_question_scores(self):
        self.answer_all()
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis), \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=self.cloud_asr), \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question), \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=ValueError("synthetic summary failure")):
            self.post(self.student + "/finish", {})
        partial = self.report()
        self.assertEqual(partial["auto_scoring"]["content_rated"], 3)
        self.assertEqual(partial["auto_scoring"]["nodes"]["summary"]["status"], "failed")
        self.assertTrue(all(f["rating"] is not None for f in partial["factors"]))
        self.assertIsNone(partial["components"]["experience"]["score"])
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=AssertionError("Media already analyzed")) as media, \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=AssertionError("Transcript already saved")) as asr, \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=AssertionError("Accepted factors must be cached")) as question, \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary) as summary:
            self.post(self.teacher + "/auto-score", {"version": partial["version"]}, teacher=True)
        media.assert_not_called()
        asr.assert_not_called()
        question.assert_not_called()
        summary.assert_called_once()
        ready = self.report()
        self.assertEqual(ready["auto_scoring"]["status"], "done")
        self.assertEqual(ready["factors"], partial["factors"])
        self.assertEqual(len(ready["draft"]["model_provenance"]["factor_details"]), 3)
        self.network.assert_not_called()

    def _assert_corrected_words_survive_old_media_checkpoint(self, source):
        self.answer_all()
        qid = "F3-G"

        def analysis_with_old_asr(path, answer):
            result = stub_analysis(path, answer)
            answer_qid = Path(answer["media"]["name"]).stem
            old_text = self.replies[answer_qid]
            result["transcript"] = old_text
            result["processing"].update(transcript=old_text, transcript_segments=[{
                "start": 0, "end": answer["media"]["duration_seconds"], "text": old_text}])
            return result

        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=analysis_with_old_asr), \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=AssertionError("Local transcript exists")) as initial_asr, \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question), \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary):
            self.post(self.student + "/finish", {})
        initial_asr.assert_not_called()
        before = self.report()
        self.assertEqual(before["answers"][qid]["processing"]["transcript"], self.replies[qid])
        self.assertTrue(before["answers"][qid]["processing"]["transcript_segments"])
        checkpoint_key = before["auto_scoring"]["nodes"]["media:" + qid]["input_key"]
        cache = LocalScoring(self.store).checkpoints
        cached = cache.get(self.sid, "media:" + qid, checkpoint_key)
        self.assertEqual(cached["processing"]["transcript"], self.replies[qid])
        self.assertTrue(cached["processing"]["transcript_segments"])

        corrected_text = self.replies[qid] + "本次原话补正后不能沿用旧转写时间戳。"
        correction_body = {
            "version": before["version"], "input_version": before["input_version"],
            "media_sha256": before["answers"][qid]["media"]["sha256"],
            "reviewer": "测试核对老师", "reason": "仅校正本题文字，原片和测量保持一致。"}
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=AssertionError("Reuse immutable media measurements")) as media, \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=AssertionError("Corrected words are authoritative")) as asr, \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question) as question, \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary):
            if source == "teacher_corrected_transcript":
                self.post(self.teacher + "/answers/" + qid + "/correct-transcript",
                          {**correction_body, "transcript": corrected_text}, teacher=True)
            else:
                reopened = self.post(self.teacher + "/answers/" + qid + "/reopen-answer",
                                     correction_body, teacher=True)
                answered = self.post(self.student + "/answers/" + qid,
                                     {"version": reopened["version"], "value": corrected_text})
                self.post(self.student + "/finish", {"version": answered["version"]})
        media.assert_not_called()
        asr.assert_not_called()
        question.assert_called_once()
        self.assertEqual(question.call_args.args[2], qid)
        current = self.report()
        answer = current["answers"][qid]
        self.assertEqual(current["auto_scoring"]["status"], "done")
        self.assertTrue(current["auto_scoring"]["nodes"]["media:" + qid]["cached"])
        self.assertEqual(answer["text_source"], source)
        self.assertEqual(answer.get("text") or answer.get("transcript"), corrected_text)
        self.assertEqual(answer["processing"]["transcript"], corrected_text)
        self.assertEqual(answer["processing"]["transcript_segments"], [])
        # The checkpoint deliberately remains old; publishing must sanitize stale words.
        self.assertEqual(cache.get(self.sid, "media:" + qid, checkpoint_key), cached)
        self.assertEqual(self.report(before["version"])["answers"][qid]["processing"]["transcript"], self.replies[qid])
        self.network.assert_not_called()

    def test_teacher_corrected_transcript_does_not_resurrect_cached_words_or_timestamps(self):
        self._assert_corrected_words_survive_old_media_checkpoint("teacher_corrected_transcript")

    def test_candidate_corrected_text_does_not_resurrect_cached_words_or_timestamps(self):
        self._assert_corrected_words_survive_old_media_checkpoint("candidate_corrected_text")

    def test_rebuilt_worker_recovers_running_job_from_durable_successful_nodes(self):
        self.answer_all()

        def fail_one(state, bank, qid):
            if qid == "F5-G":
                raise ValueError("synthetic process boundary")
            return self.cloud_question(state, bank, qid)

        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis), \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=self.cloud_asr), \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=fail_one):
            self.post(self.student + "/finish", {})
        partial = self.report()
        replacement_store = Workflow(self.directory)
        replacement_worker = LocalScoring(replacement_store)
        replacement_worker.queue(self.sid, {"version": partial["version"]})
        # Reproduce a process that was interrupted after claiming its next run.
        replacement_store.change(self.sid, {}, lambda s, b: s["auto_scoring"].update(status="running"))
        another_worker = LocalScoring(Workflow(self.directory))
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=AssertionError("Persistent media cache expected")) as media, \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=AssertionError("Persistent transcript expected")) as asr, \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question) as question, \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary) as summary:
            another_worker.recover()
        media.assert_not_called()
        asr.assert_not_called()
        question.assert_called_once()
        self.assertEqual(question.call_args.args[2], "F5-G")
        summary.assert_called_once()
        current = self.report()
        self.assertEqual(current["auto_scoring"]["status"], "done")
        self.assertEqual(current["auto_scoring"]["content_rated"], 3)
        self.assertEqual(current["input_version"], partial["input_version"])
        self.network.assert_not_called()

    def test_existing_teacher_factor_is_preserved_when_a_different_answer_is_corrected(self):
        self.answer_all()
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis), \
             patch("src.mmsa_v2.cloud_asr.transcribe_answer", side_effect=self.cloud_asr), \
             patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=self.cloud_question), \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=self.cloud_summary):
            self.post(self.student + "/finish", {})
        ready = self.report()
        manual = self.post(self.teacher + "/local-draft", {
            "version": ready["version"], "input_version": ready["input_version"], "author": "先前复核老师",
            "factors": [{"factor_id": "F5", "rating": 2, "reason": "老师依据原话复核确定。",
                         "evidence": [{"question_id": "F5-G", "quote": self.replies["F5-G"]}]}]}, teacher=True)
        qid = "F3-G"
        with patch("src.mmsa_v2.cloud_content.evaluate_question", side_effect=AssertionError("Manual review must be preserved")) as question, \
             patch("src.mmsa_v2.cloud_content.summarize_interview", side_effect=AssertionError("Manual review must be preserved")) as summary:
            corrected = self.post(self.teacher + "/answers/" + qid + "/correct-transcript", {
                "version": manual["version"], "input_version": manual["input_version"],
                "media_sha256": ready["answers"][qid]["media"]["sha256"],
                "reviewer": "测试核对老师", "reason": "补正另一个因子的原话。",
                "transcript": self.replies[qid] + "这是一句校正补充。"}, teacher=True)
            self.post(self.teacher + "/auto-score", {"version": corrected["version"]}, teacher=True, expected=409)
        question.assert_not_called()
        summary.assert_not_called()
        current = self.report()
        teacher_factor = next(f for f in current["factors"] if f["factor_id"] == "F5")
        self.assertEqual(teacher_factor["rating"], 2)
        self.assertEqual(teacher_factor["source"], "local_review")
        self.assertEqual(current["auto_scoring"]["status"], "manual_review_required")
        self.network.assert_not_called()


if __name__ == "__main__":
    unittest.main()
