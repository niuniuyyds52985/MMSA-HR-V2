import json
from pathlib import Path
import unittest

from src.mmsa_v2.auto_content import content_draft
from src.mmsa_v2.sources import load_bank
from src.mmsa_v2.workflow import Workflow


class ContentDraftTests(unittest.TestCase):
    def setUp(self):
        self.bank = load_bank()
        self.questions = {q["id"]: q for f in self.bank["factors"] for q in f["questions"]}

    def draft(self, replies):
        state = {"plan":[self.questions[qid] for qid in replies],
                 "answers":{qid:{"transcript":text} for qid,text in replies.items()}}
        draft = content_draft(state, self.bank)
        for item in draft["factors"]:
            Workflow.checked_factor(state, item)
        return draft

    def test_three_bank_questions_retain_evidence(self):
        replies = json.loads((Path(__file__).parent/"fixtures/short_oral_replies.json").read_text(encoding="utf-8"))
        draft = self.draft(replies)
        self.assertEqual(len(draft["factors"]), 3)
        for entry in draft["factors"]:
            self.assertIn(entry["rating"], (2,3,4,5))
            self.assertIn("八因子提问清单!H",entry["reason"])
            for ref in entry["evidence"]:
                self.assertIn(ref["quote"], replies[ref["question_id"]])
        self.assertGreater(draft["experience_score"], 0)
        self.assertLessEqual(draft["experience_score"],100)
        self.assertEqual(draft["source"],"local_auto")

    def test_insufficient_and_hypothetical_are_not_rated(self):
        replies = {"F3-G": self.questions["F3-G"]["prompt"], "F5-G":"",
                   "F6-G":"我认为你能够做好吗我认为你能够做好吗我认为你能够做好吗"}
        def assert_gaps(replies):
            draft = self.draft(replies)
            self.assertTrue(all(f["rating"] is None for f in draft["factors"]))
            self.assertIsNone(draft["experience_score"])
        assert_gaps(replies)
        assert_gaps({"F6-G":"如果接下来有团队项目，我会在当天调整方案并做试点验证。"})
        bystander = self.draft({"F6-G":"在项目中我旁观。导师调整方案，他试点验证，最终完成了项目。"})
        self.assertIsNone(bystander["factors"][0]["rating"])
        self.assertIsNone(bystander["experience_score"])

    def test_reverse_context_not_a_keyword_penalty(self):
        reverse = self.draft({"F7-G":"过去半年我从未主动征求反馈。"})
        self.assertEqual(reverse["factors"][0]["rating"], 1)
        negative_action = self.draft({"F6-G":"项目变化时，我没有调整方案，团队最终完成了项目。"})
        self.assertIsNone(negative_action["factors"][0]["rating"])
        self.assertIsNone(negative_action["experience_score"])

    def test_same_event_is_not_double_counted(self):
        event = "在市场调研项目中，我对接了上下游，梳理了不同环节。最终完成了调研。复盘后我学到了如何安排工作，这对管培岗位很有价值。"
        once = self.draft({"F5-G":event})
        twice = self.draft({"F5-G":event, "F6-G":event})
        self.assertEqual(once["experience_score"],50)
        self.assertEqual(twice["experience_score"],50)
        extra = "在校园竞赛中，我调整了调研方案，还按时交付。我意识到管培岗位需要及时核对反馈。"
        self.assertEqual(self.draft({"F5-G":event,"F6-G":extra})["experience_score"],100)


if __name__ == "__main__":
    unittest.main()
