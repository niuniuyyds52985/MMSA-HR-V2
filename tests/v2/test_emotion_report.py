import unittest

from src.mmsa_v2.media_evidence import whole_state_proportions, render_state_denominators
from src.mmsa_v2.product_html import _state_view
from src.mmsa_v2.scoring import STATE_POINTS
from src.mmsa_v2.state_audit import render_state_audit, render_state_error, render_state_window_values


class EmotionReportTests(unittest.TestCase):
    def observation(self):
        return {
            "answer_seconds": 200, "valid_seconds": 50, "coverage": .25,
            "proportions": {**dict.fromkeys(STATE_POINTS, 0), "平和": .98, "饱满": .02},
            "score": 50.5, "status": "partial", "band": "平和",
        }

    def test_classified_majority_does_not_become_whole_recording_majority(self):
        obs = self.observation()
        whole = whole_state_proportions(obs)
        self.assertAlmostEqual(whole["平和"], .245)
        self.assertAlmostEqual(whole["饱满"], .005)
        self.assertEqual(whole["未分类"], .75)
        self.assertAlmostEqual(sum(whole.values()), 1)
        html = render_state_denominators(obs)
        self.assertIn("<td>平和</td><td>24.5%</td><td>98%</td>", html)
        self.assertIn("<td>未分类</td><td>75%</td>", html)
        report = {"session":obs, "answers":{}, "questions":[],
                  "components":{"multimodal":{"weight":.25,"contribution":None}}}
        bars, _ = _state_view(report)
        self.assertIn("已分类时段内分布", bars)
        self.assertIn("分母 50 秒 / 整场 200 秒", bars)
        self.assertIn("整场分布（含未分类）", bars)
        self.assertIn("平和 24.5%", bars)

    def test_no_classification_is_not_imputed_as_neutral(self):
        obs = {"answer_seconds":10, "valid_seconds":0, "proportions":None}
        whole = whole_state_proportions(obs)
        self.assertEqual(whole["平和"], 0)
        self.assertEqual(whole["未分类"], 1)
        self.assertIn("<td>平和</td><td>0%</td><td>—</td>", render_state_denominators(obs))

    def test_missing_duration_stays_unknown(self):
        for duration in (None, 0, float("nan")):
            obs = {**self.observation(), "answer_seconds":duration}
            self.assertIsNone(whole_state_proportions(obs))
            self.assertIn("总时长未确认", render_state_denominators(obs))

    def test_joint_model_evidence_shows_actual_audio_window_and_escapes_labels(self):
        segment = {"start":.2,"end":.4,"state":"饱满","speech_start":0,"speech_end":5,
                   "values":{"au_intensity":{"AU06":1.2,"AU12":2.4},"au_presence":{"AU06":1,"AU12":1},
                             "au_patterns":{"happy":{"aus":["AU06","AU12"],"valid_seconds":.2,
                                "active_seconds":.1,"matched_fraction":.5,"strength":.2}},"face_state":"饱满",
                             "speech_label":"happy","speech_scores":{"happy":.8,"neutral":.2},
                             "speech_state":"饱满","fusion_rule":"同向 <复核>"}}
        html = render_state_window_values(segment)
        self.assertIn("AU12 2.4（检出）",html)
        self.assertIn("语音模型窗口 0–5 秒",html)
        self.assertIn("愉悦 0.8",html)
        self.assertIn("不代表情绪强度",html)
        self.assertIn("同向 &lt;复核&gt;",html)
        self.assertIn("AU06 + AU12",html)
        self.assertIn("50%（0.1 / 0.2 秒）",html)
        self.assertIn("不是情绪强度",html)
        self.assertNotIn("独立视觉模型对照",html)
        self.assertNotIn("0.2–0.4 秒",html)

    def test_policy_is_saved_not_reconstructed_and_manual_annotation_takes_precedence(self):
        estimate = {"method":"v2.multimodal-affect.au-ser.3", "fusion_policy":{
            "version":"fixture.7","description":"保存的规则 <script>oops</script>",
            "face_rules":{"happy":"AU06与AU12"},"voice_mapping":{"happy":"饱满"}},
            "models":{"face":{"model_id":"OpenFace/AU","provenance":{"weights_manifest_sha256":"face-manifest-hash"}},
                      "speech":{"model_id":"SER-fixture","provenance":{"weights_sha256":"voice-hash"}}}}
        answer = {"state_estimate":estimate}
        html = render_state_audit({"answers":{"Q1":answer}})
        self.assertIn("fixture.7",html)
        self.assertIn("voice-hash",html)
        self.assertIn("权重清单 SHA256：face-manifest-hash",html)
        self.assertIn("SER-fixture",html)
        self.assertIn("&lt;script&gt;oops&lt;/script&gt;",html)
        self.assertNotIn("<script>",html)
        self.assertNotIn("AU12 ≥ 0.75",html)
        self.assertNotIn("Progressive Teacher",html)
        answer["observations"]={"reviewer":"老师","segments":[]}
        self.assertEqual(render_state_audit({"answers":{"Q1":answer}}),"")

    def test_missing_voice_window_does_not_display_invented_times(self):
        segment={"start":0,"end":1,"state":None,"values":{"au_intensity":{},"speech_start":None,
                 "speech_end":None,"speech_scores":{},"face_state":None,"speech_state":None,
                 "fusion_rule":"采集或模型推理缺失"}}
        html=render_state_window_values(segment)
        self.assertIn("采集或模型推理缺失",html)
        self.assertNotIn("语音模型窗口",html)
        self.assertNotIn("None",html)

    def test_failed_analysis_shows_saved_reason_without_inventing_a_score(self):
        answer={"state_estimate":None,"state_estimate_error":{
            "method":"v2.fixture","message":"声音模型 <尚未就绪>"}}
        html=render_state_error(answer)
        self.assertIn("多模态分析未完成",html)
        self.assertIn("声音模型 &lt;尚未就绪&gt;",html)
        self.assertIn("v2.fixture",html)
        self.assertIn("不按平和或零分",html)
        self.assertNotIn("<尚未就绪>",html)
        answer["observations"]={"reviewer":"老师","segments":[]}
        self.assertEqual(render_state_error(answer),"")


if __name__ == "__main__":
    unittest.main()
