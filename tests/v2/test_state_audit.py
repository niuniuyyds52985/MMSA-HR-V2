import unittest

from src.mmsa_v2.state_audit import METHOD_V1, render_state_audit

# A frozen historic report must keep its saved rule even after METHOD advances.
LEGACY_METHOD = "v2.observable-delivery.fer-au-audio.2"


class StateAuditTests(unittest.TestCase):
    def test_rule_exclusions_and_sample_counts_come_from_this_answer(self):
        estimate = {"method": METHOD_V1, "duration_seconds": 3.1,
                    "segments": [{"start": 0.0, "end": 1.0, "state": "饱满"}],
                    "evidence": [{"start": 1.0, "end": 3.0, "state": None, "reason": "缺少<交叉依据>"}]}
        samples = [{"start_ms": t, "values": {"label": "happy"}} for t in (0, 500, 1000)]
        answer = {"state_estimate": estimate,
                  "processing": {"v1": {"modules": {"expression": {"observations": samples}}}}}
        html = render_state_audit({"answers": {"Q1": answer}})
        self.assertIn("首选愉悦 3 个，其中 2 个通过", html)
        self.assertIn("缺少&lt;交叉依据&gt;</td><td>2 秒", html)
        self.assertIn("采样窗口的时长（含片尾）</td><td>0.1 秒", html)
        self.assertIn("本项目演示口径", html)
        self.assertIn("8373812", html)
        answer["observations"] = {"reviewer": "老师", "segments": []}
        self.assertEqual(render_state_audit({"answers": {"Q1": answer}}), "")

    def test_thresholds_come_from_the_saved_rule_version(self):
        def answer(method):
            return {"state_estimate": {"method": method, "duration_seconds": 1,
                                       "segments": [], "evidence": []}}
        old = render_state_audit({"answers": {"q1": answer(METHOD_V1)}})
        current = render_state_audit({"answers": {"q2": answer(LEGACY_METHOD)}})
        mixed = render_state_audit({"answers": {"q1": answer(METHOD_V1), "q2": answer(LEGACY_METHOD)}})
        self.assertIn("AU12 ≥ 2.5", old)
        self.assertNotIn("AU12 ≥ 0.75", old)
        self.assertIn("AU12 ≥ 0.75", current)
        self.assertIn("AU12 ≥ 2.25", current)
        self.assertNotIn("AU12 ≥ 2.5", current)
        self.assertIn("v1（q1）、v2（q2）", mixed)
        self.assertIn("洋溢（v1）", mixed)
        self.assertIn("洋溢（v2）", mixed)
