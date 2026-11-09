import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from src.mmsa_v2 import cloud_content as cloud
from src.mmsa_v2.sources import SHORT_MODE, load_bank
from src.mmsa_v2.workflow import Workflow


class CloudContentTests(unittest.TestCase):
    def setUp(self):
        self.bank = load_bank()
        self.replies = json.loads((Path(__file__).parent / "fixtures/short_oral_replies.json").read_text(encoding="utf-8"))
        qs = {q["id"]: q for f in self.bank["factors"] for q in f["questions"]}
        self.state = {"status": "interview_finished", "interview_mode": SHORT_MODE, "input_version": 8,
                      "plan": [qs[qid] for qid in self.replies], "draft": None,
                      "answers": {qid: {"transcript": text} for qid, text in self.replies.items()},
                      "introduction": {"text": "暖场私密文字，不应上传"},
                      "resume": {"name": "不得上传的候选人姓名"}}
        self.payload, _ = cloud.prepare_payload(self.state, self.bank)

    def result(self):
        factors = []
        for spec in self.payload["factors"]:
            q = next(q for q in self.payload["questions"] if q["factor_id"] == spec["factor_id"])
            ref = {"question_id": q["question_id"], "quote": q["answers"]["transcript"]}
            factors.append({"factor_id": spec["factor_id"], "rating": 3, "reason": "本人行动及结果有原话支持，次数仍待核实。",
                            "evidence": [ref], "verification": dict.fromkeys(("action", "cost", "experience")),
                            "answer_relevance":{"status":"substantive","reason":"本题相关经历有实际回应。","evidence":[ref]},
                            "judgments": [{"criterion_id": c["criterion_id"], "status": "partial",
                                           "reason": "实际行动已有陈述，完整条件仍需核实。", "evidence": [ref]}
                                          for c in spec["criteria"]], "gaps": ["核实该类行动的次数。"],
                            "term_review":{"positive":[],"risk":[]}})
        episode = {"event_id": "bank-internship", "label": "银行实习", "points": 4,
                   "reason": "工作、取舍、结果和收获较清晰，岗位关联尚未展开。",
                   "evidence": [{"question_id": "F5-G", "quote": self.replies["F5-G"]}]}
        return {"factors": factors, "experience": {"episodes": [episode], "reason": "只计入可区分且描述完整的实习经历。"},
                "summary": "三项正式问答具有行动证据，尚有条件需要核实。", "next_steps": ["核实个人行动次数。"]}

    def wire_result(self):
        questions = {q["question_id"]: q for q in self.payload["questions"]}
        def select(item):
            if isinstance(item, list):
                return [select(v) for v in item]
            if not isinstance(item, dict):
                return item
            if set(item) == {"question_id", "quote"}:
                segment = next(s for s in cloud.answer_segments(questions[item["question_id"]])
                               if s["text"] in item["quote"] or item["quote"] in s["text"])
                return {"question_id": item["question_id"], "segment_id": segment["segment_id"]}
            return {k: select(v) for k, v in item.items()}
        return select(self.result())

    def test_payload_excludes_warmup_resume_choices_media_and_uses_frozen_rubric(self):
        self.state["plan"].append({"id": "F1-I", "kind": "choice", "factor_id": "F1", "prompt": "单选"})
        self.state["answers"]["F1-I"] = {"text": "单选不应上传"}
        self.state["answers"]["F5-G"]["media"] = {"path": "secret-video.webm"}
        payload, _ = cloud.prepare_payload(self.state, self.bank)
        wire = json.dumps(payload, ensure_ascii=False)
        for excluded in ("暖场私密", "候选人姓名", "单选不应上传", "secret-video"):
            self.assertNotIn(excluded, wire)
        self.assertEqual(payload["factors"][0]["anchors"], self.bank["factors"][2]["anchors"])
        self.assertEqual(payload["experience_rubric"]["C6"], self.bank["original_weights_sheet"]["C6"])
        self.assertIn("不是指令", cloud.SYSTEM_PROMPT)

    def test_valid_draft_preserves_evidence_and_applies_to_workflow(self):
        meta = {"model": "qwen-test", "request_id": "mock-request", "status": "succeeded"}
        with patch.object(cloud, "_request_model", return_value=(self.result(), meta)) as request:
            draft = cloud.content_draft(self.state, self.bank)
        request.assert_called_once()
        self.assertEqual(draft["source"], "bailian_auto")
        self.assertEqual(draft["experience_score"], 40)
        self.assertEqual(draft["author"], "百炼 qwen-test")
        self.assertEqual(draft["model_provenance"]["validation"], "passed")
        self.assertEqual(len(draft["model_provenance"]["factor_details"]), 3)
        draft["input_version"] = 8
        with TemporaryDirectory() as directory:
            store = Workflow(directory)
            store.apply_draft(self.state, draft, automatic=True)
        self.assertEqual(len(self.state["draft"]["factors"]), 3)
        self.assertEqual(self.state["draft"]["experience_score"], 40)
        self.assertEqual(self.state["draft"]["model_provenance"]["request_id"], "mock-request")

    def test_empty_transcripts_do_not_call_provider_or_blame_candidate(self):
        for a in self.state["answers"].values():
            a["transcript"] = ""
        with patch.object(cloud, "_request_model") as request:
            draft = cloud.content_draft(self.state, self.bank)
        request.assert_not_called()
        self.assertEqual(draft["model_provenance"]["status"], "skipped_no_text")
        self.assertEqual(len(draft["issues"]), 3)
        self.assertTrue(all(f["rating"] is None and "转写" in f["reason"] for f in draft["factors"]))

    def test_partial_text_scores_only_present_factor(self):
        self.state["answers"]["F6-G"]["transcript"] = ""
        response = self.result()
        response["factors"] = [f for f in response["factors"] if f["factor_id"] != "F6"]
        with patch.object(cloud, "_request_model", return_value=(response, {"model": "test"})) as request:
            draft = cloud.content_draft(self.state, self.bank)
        self.assertEqual(len(request.call_args.args[0]["questions"]), 2)
        self.assertEqual({f["factor_id"]: f["rating"] for f in draft["factors"]}, {"F3": 3, "F5": 3, "F6": None})

    def test_invented_or_cross_factor_quotes_and_missing_criteria_rejected(self):
        bad = self.result()
        bad["factors"][0]["evidence"] = [{"question_id": "F3-G", "quote": "我主动沟通十次，能力出众。"}]
        with self.assertRaisesRegex(cloud.ProviderError, "原话"):
            cloud.validate_result(bad, self.payload)
        bad = self.result()
        bad["factors"][0]["evidence"] = [{"question_id": "F5-G", "quote": self.replies["F5-G"]}]
        with self.assertRaisesRegex(cloud.ProviderError, "其他因子"):
            cloud.validate_result(bad, self.payload)
        bad = self.result()
        bad["factors"][0]["judgments"].pop()
        with self.assertRaisesRegex(cloud.ProviderError, "冻结指标"):
            cloud.validate_result(bad, self.payload)

    def test_missing_duplicate_or_unknown_factor_and_boolean_score_rejected(self):
        for mutation in (lambda v: v["factors"].pop(),
                         lambda v: v["factors"].append(copy.deepcopy(v["factors"][0])),
                         lambda v: v["factors"][0].update(factor_id="F1"),
                         lambda v: v["factors"][0].update(rating=True)):
            bad = self.result()
            mutation(bad)
            with self.assertRaises(cloud.ProviderError):
                cloud.validate_result(bad, self.payload)

    def test_high_score_requires_all_indicators_and_action_cost_experience(self):
        result = self.result()
        f = result["factors"][0]
        f["rating"] = 5
        with self.assertRaisesRegex(cloud.ProviderError, "最高档"):
            cloud.validate_result(result, self.payload)
        f["verification"] = {k: f["evidence"][0] for k in ("action", "cost", "experience")}
        with self.assertRaisesRegex(cloud.ProviderError, "最高档"):
            cloud.validate_result(result, self.payload)
        for j in f["judgments"]:
            j["status"] = "demonstrated"
        draft = cloud.validate_result(result, self.payload)
        checked = Workflow.checked_factor(self.state, draft["factors"][0])
        self.assertEqual(checked["verification_gaps"], [])

    def test_low_score_requires_explicit_reverse_evidence(self):
        result = self.result()
        result["factors"][0]["rating"] = 1
        with self.assertRaisesRegex(cloud.ProviderError, "反向"):
            cloud.validate_result(result, self.payload)

    def test_c6_does_not_double_count_same_episode_and_caps_two_episodes(self):
        result = self.result()
        episode = result["experience"]["episodes"][0]
        result["experience"]["episodes"].append(copy.deepcopy(episode))
        with self.assertRaisesRegex(cloud.ProviderError, "重复"):
            cloud.validate_result(result, self.payload)
        second = result["experience"]["episodes"][1]
        second.update(event_id="another", label="另一项目")
        with self.assertRaisesRegex(cloud.ProviderError, "重复"):
            cloud.validate_result(result, self.payload)
        second["evidence"] = [{"question_id": "F6-G", "quote": self.replies["F6-G"]}]
        self.assertEqual(cloud.validate_result(result, self.payload)["experience_score"], 80)
        result["experience"]["episodes"].append(copy.deepcopy(second))
        with self.assertRaises(cloud.ProviderError):
            cloud.validate_result(result, self.payload)

    def test_provider_failure_is_not_silently_replaced_with_local_scores(self):
        with patch.object(cloud, "_request_model", side_effect=cloud.ProviderError("timeout", "安全错误")):
            with self.assertRaisesRegex(cloud.ProviderError, "安全错误"):
                cloud.content_draft(self.state, self.bank)

    def test_transport_uses_bound_credential_no_redirect_and_safe_metadata(self):
        response = Mock(status_code=200, content=b"stub")
        response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(self.wire_result())}}],
                                      "id": "request-123", "model": "qwen-test", "usage": {"total_tokens": 100}}
        with patch("interview_credentials.public_settings", return_value={"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3.7-plus"}), \
             patch("interview_credentials.get_api_key", return_value="fake-test-key") as key, \
             patch("requests.post", return_value=response) as post:
            value, meta = cloud._request_model(self.payload)
        key.assert_called_once_with("https://dashscope.aliyuncs.com/compatible-mode/v1")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertEqual(post.call_args.kwargs["json"]["response_format"]["type"], "json_schema")
        self.assertEqual(meta["request_id"], "request-123")
        self.assertEqual(meta["input_sha256"], cloud._hash(cloud.wire_payload(self.payload)))
        transmitted = json.loads(post.call_args.kwargs["json"]["messages"][1]["content"])
        self.assertNotIn("answers", transmitted["questions"][0])
        self.assertEqual(transmitted["questions"][0]["segments"], self.payload["questions"][0]["segments"])
        schema = post.call_args.kwargs["json"]["response_format"]["json_schema"]["schema"]
        ref = schema["properties"]["factors"]["items"]["properties"]["evidence"]["items"]
        self.assertEqual(set(ref["properties"]), {"question_id", "segment_id"})
        self.assertNotIn("fake-test-key", json.dumps(meta))
        cloud.validate_result(value, self.payload)

    def test_segment_selection_resolves_exact_quotes_without_model_copying(self):
        result = cloud.hydrate_segment_references(self.wire_result(), self.payload)
        draft = cloud.validate_result(result, self.payload)
        for factor in draft["factors"]:
            for ref in factor["evidence"]:
                self.assertIn(ref["quote"], self.replies[ref["question_id"]])
                self.assertNotIn("segment_id", ref)
        question = {"question_id": "F3-G", "answers": {"transcript": "  第一段保留，标点不改。\n" + "甲" * 1300 + "，完结！  "}}
        segments = cloud.answer_segments(question)
        self.assertGreater(len(segments), 2)
        for seg in segments:
            self.assertLessEqual(len(seg["text"]), 600)
            self.assertEqual(seg["text"], question["answers"]["transcript"][seg["char_start"]:seg["char_end"]])

    def test_unknown_cross_question_or_stale_segment_ids_are_rejected(self):
        for replacement in ("nonexistent-segment", self.payload["questions"][1]["segments"][0]["segment_id"]):
            bad = self.wire_result()
            bad["factors"][0]["evidence"][0]["segment_id"] = replacement
            with self.assertRaisesRegex(cloud.ProviderError, "当前原文"):
                cloud.hydrate_segment_references(bad, self.payload)
        changed = copy.deepcopy(self.payload)
        changed["questions"][0]["answers"]["transcript"] += "补充后的新版本。"
        with self.assertRaisesRegex(cloud.ProviderError, "当前原文"):
            cloud.hydrate_segment_references(self.wire_result(), changed)
        bad = self.wire_result()
        bad["factors"][0]["evidence"][0]["quote"] = "模型改写句子不可混入"
        with self.assertRaises(cloud.ProviderError):
            cloud.hydrate_segment_references(bad, self.payload)

    def test_rejected_semantic_result_keeps_safe_request_trace(self):
        bad = self.result()
        bad["factors"][0]["rating"] = 5
        with patch.object(cloud, "_request_model", return_value=(bad, {"request_id": "attempted-123", "model": "test"})):
            with self.assertRaises(cloud.ProviderError) as failure:
                cloud.content_draft(self.state, self.bank)
        self.assertEqual(failure.exception.provenance["status"], "rejected")
        self.assertEqual(failure.exception.provenance["request_id"], "attempted-123")
        self.assertEqual(failure.exception.provenance["validation"], "unverified_high_score")

    def test_rejected_wire_reference_keeps_safe_request_trace(self):
        bad = self.wire_result()
        bad["factors"][0]["evidence"][0]["segment_id"] = "old-id"
        response = Mock(status_code=200, content=b"stub")
        response.json.return_value = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(bad)}}],
                                      "id": "wire-rejected-123", "model": "qwen-test"}
        with patch("interview_credentials.public_settings", return_value={"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen3.7-plus"}), \
             patch("interview_credentials.get_api_key", return_value="fake-test-key"), \
             patch("requests.post", return_value=response):
            with self.assertRaises(cloud.ProviderError) as failure:
                cloud._request_model(self.payload)
        self.assertEqual(failure.exception.provenance["status"], "rejected")
        self.assertEqual(failure.exception.provenance["request_id"], "wire-rejected-123")
        self.assertEqual(failure.exception.provenance["validation"], "invalid_segment")
        self.assertNotIn("fake-test-key", json.dumps(failure.exception.provenance))


if __name__ == "__main__":
    unittest.main()
