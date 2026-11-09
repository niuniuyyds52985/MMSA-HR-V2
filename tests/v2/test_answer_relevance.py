"""User-confirmed nonresponse scoring; every model response here is a fixture."""
import copy
import unittest
from unittest.mock import patch

from src.mmsa_v2 import cloud_content as cloud
from src.mmsa_v2.sources import load_bank
from src.mmsa_v2.workflow import Workflow


class AnswerRelevanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bank = load_bank()
        cls.questions = {q["id"]: q for f in cls.bank["factors"] for q in f["questions"]}

    def setUp(self):
        network = patch("requests.post", side_effect=AssertionError("No real model calls in this test"))
        network.start()
        self.addCleanup(network.stop)
        identity = patch.object(cloud, "provider_identity", return_value={"model":"fixture", "base_url":"https://test.invalid"})
        identity.start()
        self.addCleanup(identity.stop)

    def prepare(self, text, qid="F5-G", status="off_topic", rating=1):
        state = {"plan":[self.questions[qid]], "answers":{qid:{"transcript":text}}}
        payload = cloud._question_payload(state, self.bank, qid)[0]
        spec = payload["factors"][0]
        ref = {"question_id":qid, "quote":payload["questions"][0]["segments"][0]["text"]}
        response = {"factors":[{
            "factor_id":spec["factor_id"], "rating":rating,
            "reason":"针对本题回应情况作出判定，不推断未陈述的行为。",
            "evidence":[ref] if rating is not None or status in ("off_topic", "explicit_non_answer") else [],
            "answer_relevance":{"status":status, "reason":"按本题全部原话判断回应与问题的关系。", "evidence":[ref]},
            "verification":dict.fromkeys(("action","cost","experience")),
            "judgments":[{"criterion_id":c["criterion_id"], "status":"insufficient_evidence",
                          "reason":"原话未提供这一指标的行为证据。", "evidence":[]} for c in spec["criteria"]],
            "gaps":[], "term_review":{"positive":[], "risk":[
                {"cue_id":cue, "status":"unclear", "own_actual_behavior":False, "neutral_exception_excluded":False,
                 "criterion_ids":[], "reason":"线索没有足够语境支持本人反向行为。", "evidence":[ref]}
                for cue in spec["literal_risk_candidates"]]}}]}
        return state, payload, response

    def test_off_topic_or_explicit_non_answer_gets_response_lowest_rating_with_exact_quotes(self):
        cases = [("F5-G", "今天我只想谈晚饭吃什么，不谈这道取舍问题。", "off_topic"),
                 ("F6-G", "对对对，是的，好的，不知道，然后搞不懂，结束吧。", "explicit_non_answer"),
                 ("F2-G", "这道题我不会回答，跳过，不作答了。", "explicit_non_answer")]
        for qid, text, status in cases:
            with self.subTest(qid=qid):
                state, payload, response = self.prepare(text, qid, status)
                with patch.object(cloud, "_request_model", return_value=(response, {"model":"fixture"})) as call:
                    draft = cloud.evaluate_question(state, self.bank, qid)
                call.assert_called_once()
                factor = draft["factors"][0]
                self.assertEqual(factor["rating"], 1)
                self.assertEqual(factor["evidence"][0]["quote"], text)
                self.assertIn(cloud.ANSWER_RELEVANCE_POLICY["source"], factor["reason"])
                self.assertIn(cloud.ANSWER_RELEVANCE_POLICY["version"], factor["reason"])
                self.assertIn("不据此认定本人有反向行为", factor["reason"])
                detail = draft["model_provenance"]["factor_details"][0]
                self.assertEqual(detail["answer_relevance"]["status"], status)
                self.assertEqual(detail["answer_relevance"]["policy_version"], cloud.ANSWER_RELEVANCE_POLICY["version"])
                self.assertTrue(all(j["status"] == "insufficient_evidence" for j in detail["judgments"]))
                # The persisted factor remains accepted without inventing not_met.
                self.assertEqual(Workflow.checked_factor(state, factor)["rating"], 1)

    def test_missing_text_does_not_call_provider(self):
        state = {"plan":[self.questions["F5-G"]], "answers":{"F5-G":{"transcript":""}}}
        with patch.object(cloud, "_request_model") as call:
            result = cloud.evaluate_question(state, self.bank, "F5-G")
        call.assert_not_called()
        self.assertIsNone(result["factors"][0]["rating"])
        self.assertEqual(result["model_provenance"]["status"], "skipped_no_text")

    def test_unclear_text_is_null_and_cannot_be_scored(self):
        _, payload, response = self.prepare("阿鲁阿鲁（后半句无法辨认）", status="unclear", rating=None)
        self.assertIsNone(cloud.validate_result(response, payload)["factors"][0]["rating"])
        factor = response["factors"][0]
        factor.update(rating=1, evidence=factor["answer_relevance"]["evidence"])
        with self.assertRaisesRegex(cloud.ProviderError, "语义或转写不清"):
            cloud.validate_result(response, payload)

    def test_prompt_distinguishes_disfluency_from_unreadable_intent(self):
        # Prompt contract only; this does not claim that a live model was run.
        for boundary in ("重复口语填充词、断句不齐、语法不完整", "本身不证明 ASR/转写故障",
                         "局部词句不清但其余部分明确表达", "不能只截“不会/不知道”忽略此前或后续",
                         "全文既没有可辨的相关回应，也没有可辨的不作答/结束意图",
                         "请求澄清题目，不等于 off_topic/explicit_non_answer"):
            self.assertIn(boundary, cloud.SYSTEM_PROMPT)
        self.assertIn("能力评级和本题回应分是不同层级", cloud.SYSTEM_PROMPT)
        self.assertIn("不要为了保留能力评级 null 将明确不作答改判 unclear", cloud.SYSTEM_PROMPT)
        self.assertEqual(cloud.PROMPT_VERSION, "v2.bank-evidence.zh.4.2.answer-relevance")

    def test_repeated_fillers_or_partly_unclear_words_do_not_preclude_supported_nonanswer(self):
        for text in ("嗯嗯这个我答不上来就到这里吧", "噜咕噜，这题我不会答，跳过。"):
            _, payload, response = self.prepare(text, status="explicit_non_answer", rating=1)
            accepted = cloud.validate_result(response, payload)["factors"][0]
            self.assertEqual(accepted["rating"], 1)
            self.assertEqual(accepted["evidence"][0]["quote"], text)

    def test_initial_dont_know_followed_by_substantive_answer_retains_anchor_score(self):
        text = "起初我不知道怎么处理，后来我先找上下游确认需求，再调整交付顺序，最终按期完成。"
        _, payload, response = self.prepare(text, status="substantive", rating=3)
        factor = response["factors"][0]
        factor["answer_relevance"]["reason"] = "不知道描述起初状态，后文有协调上下游和交付结果，非不作答。"
        for judgment in factor["judgments"]:
            judgment.update(status="partial", reason="后文给出本题相关实际行动，具体指标仍待核实。", evidence=factor["evidence"])
        accepted = cloud.validate_result(response, payload)["factors"][0]
        self.assertEqual(accepted["rating"], 3)
        self.assertEqual(accepted["answer_relevance"]["status"], "substantive")
        self.assertNotIn("本次题目1分依据：", accepted["reason"])

    def test_related_but_sparse_response_remains_null_or_original_low_anchor(self):
        _, payload, response = self.prepare("我曾考虑其他部门，不过细节记不清了。", status="substantive", rating=None)
        self.assertIsNone(cloud.validate_result(response, payload)["factors"][0]["rating"])
        f = response["factors"][0]
        f.update(rating=2, evidence=f["answer_relevance"]["evidence"])
        f["judgments"][0].update(status="partial", evidence=f["evidence"])
        self.assertEqual(cloud.validate_result(response, payload)["factors"][0]["rating"], 2)
        f["rating"] = 1
        with self.assertRaisesRegex(cloud.ProviderError, "反向"):
            cloud.validate_result(response, payload)

    def test_related_explicit_original_low_anchor_keeps_its_own_basis(self):
        _, payload, response = self.prepare("过去两年我没有持续帮助过任何同学或同事。", qid="F2-G", status="substantive", rating=1)
        factor = response["factors"][0]
        factor["judgments"][0].update(status="not_met", reason="本人明确持续帮助人数为0，符合原表低档锚点。", evidence=factor["evidence"])
        accepted = cloud.validate_result(response, payload)["factors"][0]
        self.assertEqual(accepted["rating"], 1)
        self.assertNotIn("本次题目1分依据：", accepted["reason"])

    def test_nonresponsive_label_alone_or_truncated_invented_reference_cannot_get_one(self):
        _, payload, valid = self.prepare("我不知道之前为什么会失败，但后来我调整分工完成交付。")
        for refs, message in (([], "原话"),
                              ([{"question_id":"F5-G", "quote":"我不知道"}], "完整原句段"),
                              ([{"question_id":"F5-G", "quote":"我拒绝回答这个问题。"}], "原话")):
            response = copy.deepcopy(valid)
            response["factors"][0]["answer_relevance"]["evidence"] = refs
            with self.assertRaisesRegex(cloud.ProviderError, message):
                cloud.validate_result(response, payload)
        response = copy.deepcopy(valid)
        response["factors"][0]["evidence"] = [{"question_id":"F5-G", "quote":"我不知道"}]
        with self.assertRaisesRegex(cloud.ProviderError, "相同原话"):
            cloud.validate_result(response, payload)

    def test_null_model_capability_proposal_receives_local_nonresponse_score_with_audit(self):
        for status in ("off_topic", "explicit_non_answer"):
            state, payload, response = self.prepare("这个问题我不回答了。", status=status, rating=None)
            with patch.object(cloud, "_request_model", return_value=(response, {"model":"fixture"})) as call:
                draft = cloud.evaluate_question(state, self.bank, "F5-G")
            call.assert_called_once()  # A valid null capability proposal needs no model correction.
            factor = draft["factors"][0]
            detail = draft["model_provenance"]["factor_details"][0]
            decision = detail["policy_result"]
            self.assertEqual(factor["rating"], 1)
            self.assertIsNone(detail["rating"])
            self.assertIsNone(decision["proposed_rating"])
            self.assertEqual(decision["accepted_rating"], factor["rating"])
            self.assertEqual(decision, cloud.rating_decision(response["factors"][0], payload["factors"][0], payload))
            adjustment = decision["adjustments"][-1]
            self.assertIsNone(adjustment["from"])
            self.assertEqual(adjustment["to"], 1)
            self.assertEqual(adjustment["evidence"], factor["evidence"])
            self.assertEqual(adjustment["source"], cloud.ANSWER_RELEVANCE_POLICY["source"])
            self.assertIn("原模型未给出能力评级", factor["reason"])

    def test_null_nonresponse_still_needs_matching_precise_evidence(self):
        _, payload, response = self.prepare("这题不作答了。", status="explicit_non_answer", rating=None)
        response["factors"][0]["evidence"] = []
        with self.assertRaisesRegex(cloud.ProviderError, "相同原话"):
            cloud.validate_result(response, payload)
        response["factors"][0]["answer_relevance"]["evidence"] = []
        with self.assertRaisesRegex(cloud.ProviderError, "原话"):
            cloud.validate_result(response, payload)

    def test_nonresponse_cannot_invent_behavior_or_conflicting_capability_rating(self):
        _, payload, valid = self.prepare("这个问题我不回答了。", status="explicit_non_answer")
        for rating in (2, 3, 4, 5):
            response = copy.deepcopy(valid)
            response["factors"][0]["rating"] = rating
            with self.assertRaisesRegex(cloud.ProviderError, "能力评级冲突"):
                cloud.validate_result(response, payload)
        response = copy.deepcopy(valid)
        response["factors"][0]["judgments"][0].update(status="not_met", evidence=response["factors"][0]["evidence"])
        with self.assertRaisesRegex(cloud.ProviderError, "不能被转换为本人行为"):
            cloud.validate_result(response, payload)

    def test_new_policy_is_separate_from_original_workbook_and_invalidates_checkpoint(self):
        state, payload, response = self.prepare("这个问题我不回答了。", status="explicit_non_answer")
        self.assertEqual(payload["general_rules"], self.bank["rules"])
        self.assertIn("回答过于简略", payload["general_rules"]["B10"])
        self.assertEqual(payload["answer_relevance_policy"]["source"], cloud.ANSWER_RELEVANCE_POLICY["source"])
        key = cloud.question_input_key(state, self.bank, "F5-G")
        with patch.object(cloud, "PROMPT_VERSION", "v2.bank-evidence.zh.3.context-rounds"):
            self.assertNotEqual(key, cloud.question_input_key(state, self.bank, "F5-G"))
        with patch.object(cloud, "PROMPT_VERSION", "v2.bank-evidence.zh.4.answer-relevance"):
            self.assertNotEqual(key, cloud.question_input_key(state, self.bank, "F5-G"))
        with patch.object(cloud, "PROMPT_VERSION", "v2.bank-evidence.zh.4.1.answer-relevance"):
            self.assertNotEqual(key, cloud.question_input_key(state, self.bank, "F5-G"))
        payload["answer_relevance_policy"] = {**payload["answer_relevance_policy"], "nonresponsive_rating":2}
        with self.assertRaisesRegex(cloud.ProviderError, "版本未确认"):
            cloud.validate_result(response, payload)

    def test_relevance_wire_refs_use_current_segments_not_freeform_quotes(self):
        _, payload, response = self.prepare("这个问题我不回答了。", status="explicit_non_answer")
        q = payload["questions"][0]
        segment = q["segments"][0]["segment_id"]
        def wire(item):
            if isinstance(item, list): return [wire(x) for x in item]
            if not isinstance(item, dict): return item
            if set(item) == {"question_id", "quote"}: return {"question_id":item["question_id"], "segment_id":segment}
            return {k:wire(v) for k,v in item.items()}
        value = wire(response)
        hydrated = cloud.hydrate_segment_references(value, payload)
        self.assertEqual(cloud.validate_result(hydrated, payload)["factors"][0]["rating"], 1)
        value["factors"][0]["answer_relevance"]["evidence"][0]["segment_id"] = "old-segment"
        with self.assertRaisesRegex(cloud.ProviderError, "当前原文"):
            cloud.hydrate_segment_references(value, payload)


if __name__ == "__main__":
    unittest.main()
