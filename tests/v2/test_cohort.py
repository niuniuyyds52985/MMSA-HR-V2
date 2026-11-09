import copy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from src.mmsa_v2.export_html import export_batch
from src.mmsa_v2.product_html import _headline, batch_html
from src.mmsa_v2.scoring import score_summary
from src.mmsa_v2.workflow import ROOT, Workflow


def resume_document(name):
    with BytesIO() as data:
        with ZipFile(data, "w") as archive:
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:body><w:p><w:r><w:t>{name}</w:t></w:r></w:p></w:body></w:document>',
            )
        return name + ".docx", data.getvalue()


class CohortTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Workflow(directory.name)
        self.batch = self.store.new_batch("批次回归测试")

    def test_thirty_resumes_import_once_and_have_independent_student_sessions(self):
        files = sorted((ROOT / "模拟简历").glob("*.docx"))
        files = [path for path in files if not path.name.startswith("~$")]
        if not files:
            self.skipTest("需要本机模拟简历")
        self.assertEqual(len(files), 30)
        payload = [(path.name, path.read_bytes()) for path in files]
        first = self.store.import_files(self.batch["id"], payload)
        second = self.store.import_files(self.batch["id"], payload)
        self.assertEqual(second["stats"]["count"], 30)
        self.assertEqual([p["id"] for p in first["candidates"]],
                         [p["id"] for p in second["candidates"]])
        sessions = [self.store.create_session(self.batch["id"], p["id"])
                    for p in first["candidates"]]
        self.assertEqual(len({s["id"] for s in sessions}), 30)
        self.assertEqual(len({s["token"] for s in sessions}), 30)
        self.assertTrue(all(not s["answers"] and s["expected_questions"] == 8 for s in sessions))
        first_session = sessions[0]
        question = first_session["plan"][0]
        self.store.answer(first_session["id"], question["id"],
                          {"value": question["options"][0]["id"], "version": first_session["version"]})
        self.assertTrue(all(not self.store.session(s["id"])["answers"] for s in sessions[1:]))
        result = self.store.batch(self.batch["id"])
        self.assertEqual(result["stats"]["interview_count"], 30)
        self.assertEqual(result["stats"]["ranked_count"], 0)
        self.assertEqual(len({p["student_url"] for p in result["candidates"]}), 30)
        self.assertEqual(sum(p["answered_questions"] for p in result["candidates"]), 1)

    def test_rank_uses_complete_scores_handles_ties_and_keeps_partial_scores_pending(self):
        names = ["并列甲", "并列乙", "估分丙", "零分丁", "缺分戊", "作答己", "未开始庚"]
        people = self.store.import_files(self.batch["id"], [resume_document(name) for name in names])["candidates"]
        sessions = {}
        reports = {}
        for person in people[:-1]:
            session = self.store.create_session(self.batch["id"], person["id"])
            if person["name"] != "作答己":
                session = self.store.change(session["id"], {}, lambda state, bank: state.update(status="draft"))
            sessions[person["name"]] = session
            report = self.store.report(session["id"])
            report["components"] = {key: {**item, "contribution": None}
                                    for key, item in report["components"].items()}
            reports[session["id"]] = report
        for name, score in (("并列甲", 81.231), ("并列乙", 81.234), ("零分丁", 0), ("作答己", 100)):
            reports[sessions[name]["id"]]["total"] = score
        reports[sessions["估分丙"]["id"]]["estimated_total"] = 70
        partial = reports[sessions["缺分戊"]["id"]]["components"]
        partial["basic_info"]["contribution"] = 5
        partial["potential"]["partial_contribution"] = 52.5
        partial["operation"]["contribution"] = 2
        partial["multimodal"]["contribution"] = 25
        with patch.object(self.store, "report_from", side_effect=lambda state, bank: copy.deepcopy(reports[state["id"]])):
            cohort = self.store.batch(self.batch["id"])
        self.assertEqual(cohort["stats"]["ranked_count"], 4)
        by_name = {p["name"]: p for p in cohort["candidates"]}
        self.assertEqual([p["name"] for p in cohort["candidates"][:4]], names[:4])
        self.assertEqual([by_name[name]["rank"] for name in names], [1, 1, 3, 4, None, None, None])
        self.assertEqual(by_name["估分丙"]["score_kind"], "estimate")
        self.assertEqual(by_name["零分丁"]["display_score"], 0)
        self.assertEqual(by_name["缺分戊"]["display_score"], 84.5)
        self.assertIsNone(by_name["未开始庚"]["display_score"])
        html = batch_html(cohort)
        ranked, pending = html.split("待完成与待评分", 1)
        self.assertIn("零分丁", ranked)
        self.assertIn("0 / 100", ranked)
        self.assertNotIn("缺分戊", ranked)
        self.assertIn("缺分戊", pending)
        self.assertIn("已评分项累计贡献", pending)
        self.assertIn("待评分", pending)
        for name in names[:-1]:
            self.assertIn(f'/sessions/{sessions[name]["id"]}/report.html?version={sessions[name]["version"]}', html)

    def test_personal_headline_and_cohort_score_share_partial_and_zero_semantics(self):
        components = {
            "basic_info": {"contribution": 11 / 15 * 5},
            "experience": {"contribution": None},
            "potential": {"contribution": None, "partial_contribution": 34.5},
            "operation": {"contribution": 2},
            "multimodal": {"contribution": None, "estimated_contribution": 12.644175317185697},
        }
        base = {"total": None, "estimated_total": None, "components": components, "session": {"coverage": .62}}
        for changes, kind, value, shown in (
            ({}, "partial", 52.81084198385236, "52.81 / 100"),
            ({"total": 0}, "total", 0, "0 / 100"),
            ({"estimated_total": 80.1234}, "estimate", 80.1234, "80.12 / 100"),
            ({"components": {}}, "pending", None, "待评 / 100"),
        ):
            with self.subTest(kind=kind):
                report = {**base, **changes}
                summary = score_summary(report)
                self.assertEqual(summary["score_kind"], kind)
                self.assertEqual(summary["display_score"], value)
                self.assertEqual(_headline(report)[1], shown)

    def test_offline_cohort_links_to_the_frozen_personal_versions(self):
        people = self.store.import_files(self.batch["id"], [resume_document(name) for name in ("导出甲", "导出乙")])["candidates"]
        sessions = [self.store.create_session(self.batch["id"], p["id"]) for p in people]
        original_batch = self.store.batch

        def snapshot_then_update(batch_id):
            snapshot = original_batch(batch_id)
            first = sessions[0]
            question = first["plan"][0]
            self.store.answer(first["id"], question["id"],
                              {"value": question["options"][0]["id"], "version": first["version"]})
            return snapshot

        with patch.object(self.store, "batch", side_effect=snapshot_then_update):
            output = export_batch(self.store, self.batch["id"], self.store.root / "exports")
        html = output.read_text(encoding="utf-8")
        for session in sessions:
            name = f'personal-{session["id"]}-v1.html'
            self.assertIn(f'href="{name}"', html)
            report = (output.parent / name).read_text(encoding="utf-8")
            self.assertIn("已作答 0 / 8 题", report)
            self.assertIn(session["candidate"], report)
        self.assertEqual(self.store.session(sessions[0]["id"])["version"], 2)


if __name__ == "__main__":
    unittest.main()
