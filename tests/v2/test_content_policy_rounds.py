"""Contextual term rules and bounded Qwen nodes; all model responses are fixtures."""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from src.mmsa_v2 import cloud_content as cloud, content_policy
from src.mmsa_v2.sources import load_bank


class PolicyRoundTests(unittest.TestCase):
    def setUp(self):
        self.bank=load_bank()
        replies=json.loads((Path(__file__).parent/"fixtures/short_oral_replies.json").read_text(encoding="utf-8"))
        qs={q["id"]:q for f in self.bank["factors"] for q in f["questions"]}
        self.state={"plan":[qs[qid] for qid in replies],"answers":{qid:{"transcript":t} for qid,t in replies.items()}}
        provider=patch.object(cloud,"provider_identity",return_value={"model":"test-model","base_url":"https://test.invalid"})
        provider.start();self.addCleanup(provider.stop)

    def response(self,payload):
        factors=[]
        for spec in payload["factors"]:
            q=next(q for q in payload["questions"] if q["factor_id"]==spec["factor_id"])
            ref={"question_id":q["question_id"],"quote":next(iter(q["answers"].values()))}
            risks=[{"cue_id":cue,"status":"neutral","own_actual_behavior":False,"neutral_exception_excluded":False,
                    "criterion_ids":[],"reason":"仅有线索，当前上下文属中性描述。","evidence":[ref]}
                   for cue in spec["literal_risk_candidates"]]
            factors.append({"factor_id":spec["factor_id"],"rating":4,"reason":"部分实际行为支持较高档，但不推断完整高档。",
                "evidence":[ref],"verification":dict.fromkeys(("action","cost","experience")),
                "answer_relevance":{"status":"substantive","reason":"包含与本题有关的行动回应。","evidence":[ref]},
                "judgments":[{"criterion_id":c["criterion_id"],"status":"partial","reason":"有部分对应行为。","evidence":[ref]} for c in spec["criteria"]],
                "gaps":["高档条件需核实。"],"term_review":{"positive":[],"risk":risks}})
        if payload.get("task")=="question":return {"factors":factors}
        q=payload["questions"][0]
        result={"experience":{"episodes":[{"event_id":"one-event","label":"正式作答中的事件","points":3,
            "reason":"个人工作有描述。","evidence":[{"question_id":q["question_id"],"quote":next(iter(q["answers"].values()))}]}],
            "reason":"只计可区分的一段经历。"},"summary":"根据固定评级整理事实与待核实内容。","next_steps":["核实行为条件。"]}
        if payload.get("task")!="summary":result["factors"]=factors
        return result

    def risk_payload(self):
        self.state["answers"]["F5-G"]["transcript"]="在部门项目里，我只负责我这块，其他部门的交付我没有考虑。"
        return cloud.prepare_payload(self.state,self.bank)[0]

    def confirm(self,result):
        factor=next(f for f in result["factors"] if f["factor_id"]=="F5")
        factor["judgments"][0]["status"]="not_met"
        risk=factor["term_review"]["risk"][0]
        risk.update(status="confirmed_reverse",own_actual_behavior=True,neutral_exception_excluded=True,
                    criterion_ids=[factor["judgments"][0]["criterion_id"]],reason="本人明确未考虑上下游交付，非边界协商或引述。")
        return factor,risk

    def test_full_revised_tables_neutral_exceptions_and_source_policy_are_present(self):
        payload=self.risk_payload();f=next(f for f in payload["factors"] if f["factor_id"]=="F5")
        original=self.bank["factors"][4]
        self.assertEqual(f["positive_terms"],original["positive_terms"])
        self.assertEqual(f["risk_terms"]["neutral"],original["risk_terms"]["neutral"])
        self.assertTrue(f["literal_risk_candidates"])
        self.assertEqual(payload["scoring_policy"]["risk_cap"]["source"],content_policy.RISK_FILE+"!E1")
        self.assertEqual(payload["scoring_policy"]["high_rating_min"],5)

    def test_confirmed_reverse_caps_only_its_factor_and_retains_original_model_rating(self):
        payload=self.risk_payload();response=self.response(payload);self.confirm(response)
        with patch.object(cloud,"_request_model",return_value=(response,{"model":"fixture","request_id":"mock"})):
            draft=cloud.content_draft(self.state,self.bank)
        self.assertEqual({f["factor_id"]:f["rating"] for f in draft["factors"]},{"F3":4,"F5":2,"F6":4})
        detail=next(f for f in draft["model_provenance"]["factor_details"] if f["factor_id"]=="F5")
        self.assertEqual(detail["policy_result"]["proposed_rating"],4)
        self.assertEqual(detail["policy_result"]["accepted_rating"],2)
        self.assertEqual(detail["policy_result"]["adjustments"][0]["source"],content_policy.RISK_FILE+"!E1")

    def test_literal_matches_never_penalize_neutral_negated_other_or_corrected_context(self):
        payload=self.risk_payload()
        for status in ("neutral","negated","hypothetical","other_person","corrected","unclear"):
            result=self.response(payload)
            for f in result["factors"]:
                for r in f["term_review"]["risk"]:r["status"]=status
            ratings={f["factor_id"]:f["rating"] for f in cloud.validate_result(result,payload)["factors"]}
            self.assertEqual(ratings["F5"],4,status)

    def test_confirmed_risk_requires_actual_own_behavior_exception_check_and_reverse_criterion(self):
        payload=self.risk_payload()
        for field,value in (("own_actual_behavior",False),("neutral_exception_excluded",False),("criterion_ids",[]),("evidence",[])):
            result=self.response(payload);_,risk=self.confirm(result);risk[field]=value
            with self.assertRaises(cloud.ProviderError):cloud.validate_result(result,payload)
        result=self.response(payload)
        next(f for f in result["factors"] if f["factor_id"]=="F5")["term_review"]["risk"]=[]
        with self.assertRaisesRegex(cloud.ProviderError,"语境"):cloud.validate_result(result,payload)

    def test_changed_source_does_not_inherit_old_cap(self):
        self.bank=copy.deepcopy(self.bank);self.bank["sources"][content_policy.RISK_FILE]="new-unknown-source"
        payload=self.risk_payload();result=self.response(payload);self.confirm(result)
        self.assertIsNone(next(f for f in cloud.validate_result(result,payload)["factors"] if f["factor_id"]=="F5")["rating"])

    def test_question_and_summary_have_disjoint_output_contracts(self):
        seen=[]
        def fake(payload):
            seen.append(payload)
            return self.response(payload),{"model":"fixture","request_id":"mock"}
        with patch.object(cloud,"_request_model",side_effect=fake):
            nodes=[cloud.evaluate_question(self.state,self.bank,q["id"]) for q in self.state["plan"]]
            summary=cloud.summarize_interview(self.state,self.bank,nodes)
        self.assertEqual([p["task"] for p in seen],["question"]*3+["summary"])
        self.assertTrue(all(len(p["questions"])==1 for p in seen[:3]))
        self.assertTrue(all("experience_score" not in n for n in nodes))
        self.assertNotIn("factors",summary)
        self.assertEqual(len(seen[3]["accepted_factors"]),3)
        bad=self.response(seen[3]);bad["factors"]=[]
        with self.assertRaises(cloud.ProviderError):cloud.validate_result(bad,seen[3])

    def test_input_keys_reuse_unchanged_questions_and_invalidate_model_policy_and_summary(self):
        k3=cloud.question_input_key(self.state,self.bank,"F3-G")
        k5=cloud.question_input_key(self.state,self.bank,"F5-G")
        self.state["input_version"]=999
        self.state["introduction"]={"text":"暖场改变"}
        self.assertEqual(k3,cloud.question_input_key(self.state,self.bank,"F3-G"))
        self.state["answers"]["F5-G"]["transcript"]+="另有新事实。"
        self.assertEqual(k3,cloud.question_input_key(self.state,self.bank,"F3-G"))
        self.assertNotEqual(k5,cloud.question_input_key(self.state,self.bank,"F5-G"))
        with patch.object(cloud,"provider_identity",return_value={"model":"changed","base_url":"https://test.invalid"}):
            self.assertNotEqual(k3,cloud.question_input_key(self.state,self.bank,"F3-G"))
        with patch.object(content_policy,"HIGH_RATING_MIN",4):
            self.assertNotEqual(k3,cloud.question_input_key(self.state,self.bank,"F3-G"))
        first=cloud.summary_input_key(self.state,self.bank,[{"factors":[{"factor_id":"F3","rating":3}]}])
        self.assertNotEqual(first,cloud.summary_input_key(self.state,self.bank,[{"factors":[{"factor_id":"F3","rating":4}]}]))

    def test_one_targeted_correction_and_no_transport_retry(self):
        payload=cloud._question_payload(self.state,self.bank,"F3-G")[0]
        good=self.response(payload);bad=copy.deepcopy(good);bad["factors"][0]["evidence"][0]["quote"]="不存在原话"
        with patch.object(cloud,"_request_model",side_effect=[(bad,{"request_id":"bad"}),(good,{"request_id":"good"})]) as call:
            draft=cloud.evaluate_question(self.state,self.bank,"F3-G")
        self.assertEqual(call.call_count,2)
        self.assertEqual(call.call_args.args[0]["correction"]["code"],"invented_quote")
        self.assertEqual(draft["model_provenance"]["attempt_count"],2)
        with patch.object(cloud,"_request_model",side_effect=cloud.ProviderError("timeout","超时")) as call:
            with self.assertRaises(cloud.ProviderError):cloud.evaluate_question(self.state,self.bank,"F3-G")
        self.assertEqual(call.call_count,1)
        with patch.object(cloud,"_request_model",return_value=(bad,{"request_id":"bad"})) as call:
            with self.assertRaises(cloud.ProviderError):cloud.evaluate_question(self.state,self.bank,"F3-G")
        self.assertEqual(call.call_count,2)


if __name__=="__main__":unittest.main()
