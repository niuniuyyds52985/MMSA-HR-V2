import hashlib
import io
import json
from pathlib import Path
import unittest
from xml.etree import ElementTree
from zipfile import ZipFile

from src.mmsa_v2 import assessment, sources


ROOT = Path(__file__).resolve().parents[2]


class SourcesTests(unittest.TestCase):
    def test_compiled_bank_preserves_real_question_blocks_and_rules(self):
        bank = sources.load_bank()
        self.assertEqual(len(bank["factors"]), 8)
        for i, factor in enumerate(bank["factors"], 1):
            self.assertEqual(factor["id"], f"F{i}")
            self.assertEqual(len(factor["source_cells"]), 13)
            self.assertTrue(factor["risk_terms"]["neutral"])
            self.assertTrue(factor["positive_terms"]["revised"])
            sjt = factor["questions"][3]
            self.assertEqual(len(sjt["answer_key"]), 4)
            for option in sjt["options"]:
                self.assertNotRegex(option["text"], "（[1-5]分）")
                self.assertIn(assessment.sjt_score(sjt["id"], option["id"], bank), (1, 2, 3, 4, 5))
        self.assertIn("代价", bank["rules"]["B15"] + bank["rules"]["A15"])
        plan = sources.make_plan({}, bank)
        self.assertEqual(len(plan), 33)
        atom_count = sum(len(q["fields"]) if q["kind"] == "compound" else 1 for q in plan[1:])
        self.assertEqual(atom_count, 34)
        self.assertNotIn("answer_key", json.dumps(plan))
        self.assertNotIn("anchors", json.dumps(plan))

    def test_question_validation_and_bank_conditional_branches(self):
        bank = sources.load_bank()
        by_id = {q["id"]: q for q in sources.make_plan({}, bank)}
        ranking = by_id["F3-F"]
        ordered = [o["id"] for o in ranking["options"]]
        self.assertEqual(assessment.validate_answer(ranking, ordered[::-1]), ordered[::-1])
        with self.assertRaises(ValueError):
            assessment.validate_answer(ranking, [ordered[1], ordered[1], ordered[2]])
        compound = by_id["F4-E"]
        fields = {f["id"]: f["options"][0]["id"] for f in compound["fields"]}
        self.assertEqual(assessment.validate_answer(compound, fields), fields)
        self.assertEqual(assessment.answer_label(compound, fields), "目标数：0个；达成率：<50%")
        with self.assertRaises(ValueError):
            assessment.validate_answer(compound, {"count": fields["count"]})
        question = by_id["F1-E"]
        self.assertFalse(sources.conditional_questions(question, question["options"][0]["id"], bank))
        self.assertEqual(sources.conditional_questions(question, question["options"][2]["id"], bank)[0]["parent_id"], "F1-E")
        self.assertEqual(len(sources.verification_questions("F3", bank)), 3)

    def test_source_provenance_and_30_provided_resume_files(self):
        files = sorted((ROOT / "模拟简历").glob("*.docx"))
        if not files:
            self.skipTest("本机模拟简历集合未提供")
        self.assertEqual(len(files), 30)
        batch = [sources.parse_resume(f) for f in files]
        self.assertEqual(len({p["source_hash"] for p in batch}), 30)
        self.assertEqual(sum(p["education"] == "硕士" for p in batch), 6)
        self.assertEqual(sum(any(e["exam"] == "CET6" for e in p["english"]) for p in batch), 27)
        self.assertEqual(sum(any(e["exam"] == "托福" for e in p["english"]) for p in batch), 3)
        sequences = set()
        for f, resume in zip(files, batch):
            self.assertEqual(resume["source_hash"], hashlib.sha256(f.read_bytes()).hexdigest())
            self.assertEqual(resume["education_status"], "在读")
            self.assertEqual(resume["internship"], "有")
            self.assertTrue(resume["school_tags"])
            self.assertTrue(all(e["source"] and e["text"] for e in resume["experiences"]))
            self.assertTrue(all(e["score"] is not None for e in resume["english"]))
            self.assertTrue({"english","school","school_tier","education","internship"} <= {e["field"] for e in resume["facts"]})
            plan = sources.make_plan(resume)
            self.assertTrue(any(q["derived_from"] for q in plan if q["id"].endswith("-G")))
            sequences.add(tuple(q["id"] for q in plan))
        self.assertGreater(len(sequences), 1)

    def test_unstated_and_explicit_absence_are_distinct(self):
        def docx(lines):
            doc = ElementTree.Element(sources.WORD+"document")
            for line in lines:
                p = ElementTree.SubElement(doc, sources.WORD+"p")
                run = ElementTree.SubElement(p, sources.WORD+"r")
                ElementTree.SubElement(run, sources.WORD+"t").text = line
            data = io.BytesIO()
            with ZipFile(data, "w") as z:
                z.writestr("word/document.xml", ElementTree.tostring(doc, encoding="utf-8"))
            return data.getvalue()
        unknown = sources.parse_resume(docx(["示例", "校园经历", "组织社团活动"]))
        absent = sources.parse_resume(docx(["示例", "实习经历", "无", "英语水平", "暂无"]))
        self.assertEqual(unknown["internship"], "未提供")
        self.assertEqual(absent["internship"], "无")
        self.assertEqual(assessment.basic_info_breakdown(absent)["raw"]["internship"], 0)
        self.assertIsNone(assessment.basic_info_score(unknown))
        with self.assertRaises(ValueError):
            sources.parse_resume(b"not a zip")

    def test_assessment_bounds(self):
        self.assertEqual(assessment.experience_score([4, 5]), 90)
        self.assertEqual(assessment.experience_score([5,5,5]), 100)
        self.assertIsNone(assessment.potential_score({"F1":5}))
        self.assertEqual(assessment.potential_score({f"F{i}":4 for i in range(1,9)}),80)
        with self.assertRaises(ValueError):
            assessment.experience_score([6])

    def test_english_result_uses_declared_qualifications_and_respects_corrections(self):
        cet4 = {"exam": "CET4", "score": 549}
        profile = {"english": [cet4], "skills": ["英语(CET-6)"], "school_tags": ["985", "211"],
                   "education": "本科", "internships": [{"dates": "2024"}, {"dates": "2025"}]}
        result = assessment.basic_info_breakdown(profile)
        self.assertEqual(result["raw"], {"education": 4, "internship": 2, "english": 5})
        self.assertAlmostEqual(result["score"], 11 / 15 * 100)
        self.assertIn("英语(CET-6)", result["english_reason"])
        self.assertIn("CET4 549", result["english_reason"])
        for changes, expected in [
                ({"skills": []}, 0), ({"english": [], "skills": []}, None),
                ({"english": [{"exam": "CET6", "score": 400}]}, 0),
                ({"english": [{"exam": "CET6"}, {"exam": "CET6", "score": 399}]}, 0),
                ({"english": [{"exam": "CET6", "score": 425}]}, 5),
                ({"english": [{"exam": "雅思", "score": 7}], "skills": []}, 5),
                ({"english": [{"exam": "雅思", "score": None}], "skills": []}, None),
                ({"skills": ["正在备考CET6"]}, 0), ({"skills": ["CET6未通过"]}, 0),
                ({"english": [{"exam": "CET6", "text": "CET6 不及格"}]}, 0),
                ({"skills": ["CET6成绩待公布"]}, None),
                ({"english_none": True}, 0),
                ({"corrections": [{"changes": {"english": [cet4]}}]}, 0),
                ({"corrections": [{"changes": {"english": []}}], "english": []}, None)]:
            with self.subTest(changes=changes):
                result = assessment.basic_info_breakdown({**profile, **changes})
                self.assertEqual(result["raw"]["english"], expected)

    def test_keyword_leads_preserve_negation_and_neutral_context(self):
        factor = sources.load_bank()["factors"][0]
        text = "我怕越界，但不是没人安排我就没管。我主动牵头组织项目。"
        cues = assessment.evidence_signals(factor, text)
        self.assertTrue({"中性近似","风险线索","正向中"} <= {c["category"] for c in cues})
        for c in cues:
            self.assertEqual(text[c["start"]:c["end"]], c["phrase"])
            self.assertIn("不是没人安排我就没管", c["quote"])
            self.assertNotIn("score", c)
