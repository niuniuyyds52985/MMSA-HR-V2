import unittest
from io import BytesIO
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from src.mmsa_v2.product_html import _headline, personal_html
from src.mmsa_v2.scoring import STATE_POINTS, calculate_report
from src.mmsa_v2.sources import load_bank
from src.mmsa_v2.workflow import Workflow


def report(questions, **component_changes):
    components = dict.fromkeys(("basic_info", "experience", "potential", "operation"), 80)
    components.update(component_changes)
    return calculate_report({
        "purpose": "simulation", "candidate": "模拟样本",
        "components": components, "questions": questions,
    })


def answer(question_id, seconds, spans=(), capture="recorded"):
    return {
        "id": question_id, "title": f"模拟题 {question_id}",
        "answer_seconds": seconds, "capture": capture,
        "segments": [{"start": start, "end": end, "state": state} for start, end, state in spans],
    }


class ScoringTests(unittest.TestCase):
    def test_five_keyed_choices_three_cited_answers_and_signed_adjustment(self):
        tmp=TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store=Workflow(tmp.name)
        batch=store.new_batch("评分验收")
        doc=BytesIO()
        with ZipFile(doc,"w") as archive:
            archive.writestr("word/document.xml",'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                             '<w:body><w:p><w:r><w:t>模拟学生</w:t></w:r></w:p></w:body></w:document>')
        batch=store.import_files(batch["id"],[("模拟学生.docx",doc.getvalue())])
        session=store.create_session(batch["id"],batch["candidates"][0]["id"])
        sid=session["id"]
        keys={q["id"]:q["answer_key"] for f in load_bank()["factors"] for q in f["questions"] if "answer_key" in q}
        choice_questions=session["plan"]
        for i,q in enumerate(choice_questions):
            points=1 if i==0 else 5
            option=next(key for key,value in keys[q["id"]].items() if value==points)
            session=store.answer(sid,q["id"],{"value":option})
        partial=store.report(sid)
        self.assertEqual(sorted(f["rating"] for f in partial["factors"] if f["rating"] is not None),[1,5,5,5,5])
        self.assertIsNone(partial["components"]["potential"]["score"])
        self.assertEqual(partial["components"]["potential"]["partial_contribution"], 31.5)
        self.assertEqual(partial["components"]["potential"]["rated_raw"], 21)
        self.assertEqual(_headline(partial)[1], "31.5 / 100")
        partial_html = personal_html(partial)
        self.assertIn("21 / 25（已评因子原分）", partial_html)
        self.assertIn('href="#factor-overview"', partial_html)
        self.assertIn('<div class="lbl">八因子当前累计</div><div class="num b">52.5 / 100</div>', partial_html)
        self.assertIn('已有 5/8 项得分 · 还缺 3 项，非最终成绩 · 加权贡献 31.5/60 分', partial_html)
        self.assertNotIn('已评因子均分', partial_html)
        self.assertIn('class="factor-item low"', partial_html)
        self.assertIn('情境单选原分 <b>21 / 25</b>', partial_html)
        self.assertEqual(partial_html.count('class="factor-item pending"'), 3)
        for f in partial["factors"]:
            self.assertIn(f'href="#factor-{f["factor_id"]}"', partial_html)
            self.assertIn(f'<details id="factor-{f["factor_id"]}">', partial_html)
        open_questions=[q for q in session["plan"] if q["kind"]=="text"]
        for q in open_questions:
            store.answer(sid,q["id"],{"value":"我核对了两份记录，并向团队说明差异。"})
        finished=store.finish(sid,{})
        refs=[{"factor_id":q["factor_id"],"rating":3,"reason":"描述了核对行动",
               "evidence":[{"question_id":q["id"],"quote":"核对了两份记录"}]} for q in open_questions]
        body={"input_version":finished["input_version"],"source":"local_review","author":"记录者","factors":refs,
              "summary":"问答有核对行动记录。","experience_score":70,"experience_reason":"举例说明了核对过程。",
              "experience_evidence":refs[0]["evidence"][0]}
        with self.assertRaises(ValueError):
            store.draft(sid,{**body,"experience_evidence":{"question_id":choice_questions[0]["id"],"quote":"核对了两份记录"}})
        store.draft(sid,body)
        self.assertEqual(store.report(sid)["components"]["potential"]["score"],75)
        q=choice_questions[0]
        choice_ref={"question_id":q["id"],"quote":session["answers"][q["id"]]["answer_label"]}
        with self.assertRaises(ValueError):
            store.draft(sid,{**body,"factors":[{"factor_id":q["factor_id"],"rating":2,"reason":"引用内部选项编号",
                       "evidence":[{**choice_ref,"quote":session["answers"][q["id"]]["value"]}]}]})
        draft=store.draft(sid,{**body,"factors":[{"factor_id":q["factor_id"],"rating":2,
                                             "reason":"复核了本题选择","evidence":[choice_ref]}]})
        self.assertEqual(store.report(sid)["components"]["potential"]["score"],75)
        store.review(sid,{"version":draft["version"],"decision":"approve","reviewer":"复核老师"})
        reviewed=store.report(sid)
        self.assertEqual(reviewed["components"]["potential"]["score"],77.5)
        adjusted=next(f for f in reviewed["factors"] if f["factor_id"]==q["factor_id"])
        self.assertEqual((adjusted["choice_signal"]["score"],adjusted["rating"]),(1,2))
        html=personal_html(reviewed)
        self.assertIn('<div class="lbl">潜力八因子</div><div class="num b">77.5 / 100</div>', html)
        self.assertIn('已有 8/8 项得分 · 加权贡献 46.5/60 分', html)
        self.assertIn('情境单选原分 <b>21 / 25</b>', html)
        self.assertIn('教师调整 · 已复核', html)
        self.assertNotIn('class="factor-item pending"', html)
        for visible in ("选项原分 1 / 5","老师复核后采用 2 / 5","八因子提问清单!J","经历评分引文","复核老师"):
            self.assertIn(visible,html)

    def test_session_pools_seconds_and_weights_score_once(self):
        # First question is 100% 平和; the second is three times as long.
        q1 = answer("q1", 30, [(0, 30, "平和")])
        q2 = answer("q2", 90, [(0, 90, "洋溢")])
        q3 = answer("choice", None, capture="not_applicable")
        result = report([q1, q2, q3])
        self.assertEqual(result["session"]["proportions"]["平和"], 0.25)
        self.assertEqual(result["session"]["score"], 87.5)
        self.assertEqual(result["session"]["band"], "洋溢")
        self.assertEqual(result["session"]["coverage"], 1)
        self.assertEqual(result["components"]["multimodal"], {
            "score": 87.5, "weight": 0.25, "contribution": 21.875,
        })
        self.assertEqual(result["components"]["potential"]["weight"], 0.60)
        self.assertEqual(result["total"], 81.875)
        self.assertEqual(result["questions"][2]["status"], "not_applicable")
        self.assertIsNone(result["questions"][2]["proportions"])
        self.assertIsNone(result["questions"][2]["band_level"])

    def test_five_state_reference_distribution(self):
        shares = [20, 35, 30, 10, 5]
        points = 0
        spans = []
        for state, duration in zip(STATE_POINTS, shares):
            spans.append((points, points + duration, state))
            points += duration
        # A caller may deliver disjoint windows out of chronological order.
        result = report([answer("q", 100, reversed(spans))])
        self.assertEqual(result["session"]["score"], 63.75)
        self.assertEqual(result["session"]["band"], "饱满")
        self.assertEqual(result["components"]["multimodal"]["contribution"], 15.9375)
        self.assertEqual(result["total"], 75.9375)

    def test_weighted_estimate_visible_while_another_component_is_pending(self):
        observed = [answer("q1", 10, [(0, 7, "饱满")]), answer("q2", 10, [(0, 7, "平和")])]
        partial = report(observed, experience=None)
        self.assertEqual(partial["components"]["multimodal"]["estimated_contribution"], 15.625)
        self.assertIsNone(partial["components"]["multimodal"]["contribution"])
        self.assertIsNone(partial["total"])
        self.assertIsNone(partial["estimated_total"])
        self.assertEqual(_headline(partial)[1], "69.23 / 100")
        estimated = report(observed)
        self.assertAlmostEqual(estimated["estimated_total"], 75.625)
        self.assertEqual(_headline(estimated)[1], "75.63 / 100")
        missing = report(observed + [answer("q3", 10, capture="missing")], experience=None)
        self.assertNotIn("estimated_contribution", missing["components"]["multimodal"])
        self.assertEqual(_headline(missing)[1], "53.6 / 100")
        complete = report([answer("q1", 10, [(0, 10, "平和")])])
        self.assertEqual(_headline(complete)[1], "72.5 / 100")

    def test_tiny_or_uneven_coverage_cannot_contribute_or_rank(self):
        tiny = report([answer(f"q{i}",30,[(0,.2,"洋溢")]) for i in range(3)])
        self.assertEqual(tiny["session"]["score"],100)
        self.assertAlmostEqual(tiny["session"]["coverage"],.2/30)
        self.assertIsNone(tiny["estimated_total"])
        self.assertNotIn("estimated_contribution", tiny["components"]["multimodal"])
        uneven = report([answer("one",10,[(0,6.99,"洋溢")]),answer("two",100,[(0,100,"平和")])])
        self.assertGreater(uneven["session"]["coverage"],.7)
        self.assertFalse(uneven["session"]["meets_coverage_threshold"])
        self.assertEqual(uneven["session"]["insufficient_question_ids"],["one"])
        self.assertIsNone(uneven["estimated_total"])
        exact = report([answer("one",10,[(0,7,"平和")])])
        self.assertTrue(exact["session"]["meets_coverage_threshold"])
        self.assertIsNotNone(exact["estimated_total"])

    def test_bands_use_unrounded_scores(self):
        cases = [(0, "非常不好", 1), (19.999, "非常不好", 1), (20, "略微 down", 2),
                 (40, "平和", 3), (60, "饱满", 4), (79.999, "饱满", 4), (80, "洋溢", 5), (100, "洋溢", 5)]
        for score, expected, level in cases:
            with self.subTest(score=score):
                spans = []
                if score:
                    spans.append((0, score, "洋溢"))
                if score < 100:
                    spans.append((score, 100, "非常不好"))
                result = report([answer("q", 100, spans)])
                self.assertEqual(result["session"]["score"], score)
                self.assertEqual(result["session"]["band"], expected)
                self.assertEqual(result["session"]["band_level"], level)
                self.assertEqual(result["questions"][0]["band_level"], level)

    def test_coverage_and_nullable_scores(self):
        valid = answer("q1", 6, [(0, 2, "平和"), (3, 4, "饱满")])
        missing = answer("q2", 4, capture="missing")
        result = report([valid, missing])
        self.assertEqual(result["questions"][0]["coverage"], 0.5)
        self.assertEqual(result["session"]["coverage"], 0.3)
        self.assertEqual(result["session"]["status"], "partial")
        self.assertEqual(result["components"]["multimodal"]["score"], result["session"]["score"])
        self.assertEqual(result["session"]["band_level"], 3)
        self.assertIsNone(result["components"]["multimodal"]["contribution"])
        self.assertEqual(result["components"]["potential"]["contribution"], 48)
        self.assertIsNone(result["total"])
        missing["answer_seconds"] = None
        result = report([valid, missing])
        self.assertIsNone(result["session"]["coverage"])
        self.assertIsNone(result["session"]["answer_seconds"])
        self.assertEqual(result["session"]["valid_seconds"], 3)
        blank = report([missing])
        self.assertEqual(blank["session"]["status"], "unavailable")
        self.assertIsNone(blank["session"]["proportions"])
        self.assertIsNone(blank["session"]["band_level"])
        self.assertIsNone(blank["components"]["multimodal"]["score"])
        complete_media = answer("q3", 1, [(0, 1, "洋溢")])
        self.assertIsNone(report([complete_media], potential=None)["total"])
        self.assertEqual(report([])["session"]["status"], "unavailable")

    def test_invalid_evidence_is_not_counted(self):
        bad = [
            answer("q", 1, [(0, 0.6, "平和"), (0.5, 1, "平和")]),
            answer("q", 1, [(0, 2, "平和")]),
            answer("q", 1, [(1, 1, "平和")]),
            answer("q", 1, [(0, 1, "unknown")]),
            answer("q", 1, [(0, 1, "平和")], "missing"),
            answer("q", None, capture="recorded"),
            answer("q", 2, capture="not_applicable"),
            answer("q", float("nan")),
        ]
        for q in bad:
            with self.subTest(q=q), self.assertRaises(ValueError):
                report([q])
        good = answer("q", 1, [(0, 1, "平和")])
        with self.assertRaises(ValueError):
            report([good, good])
        for value in (-1, 101, True, float("inf")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                report([good], operation=value)
        with self.assertRaises(ValueError):
            calculate_report({"purpose": "employment"})
