import os
"""Submission-to-report checks. Use only a temporary database and fictional answers."""

from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave
from zipfile import ZipFile

from fastapi.testclient import TestClient

from src.mmsa_v2.access import operator_key
from src.mmsa_v2.api import create_app
from src.mmsa_v2.workflow import Workflow


FIXTURE = Path(__file__).parent / "fixtures/short_oral_replies.json"


def simulation_docx():
    lines = ("测试学生", "教育背景", "模拟大学（985/211） 本科 在读", "英语学习成绩", "CET6 500",
             "实习经历", "调研实习生", "2025.05 - 2025.08", "牵头分工，指导带教，达成目标。学习建模、研究，复盘并收集反馈。")
    document = BytesIO()
    with ZipFile(document, "w") as archive:
        archive.writestr("word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>' +
            "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in lines) + "</w:body></w:document>")
    return document.getvalue()


def wav_fixture():
    blob = BytesIO()
    with wave.open(blob, "wb") as track:
        track.setparams((1,2,16000,0,"NONE","not compressed"))
        track.writeframes(bytes(32000))
    return blob.getvalue()


def stub_analysis(path, answer):
    # Test only the worker-to-report contract with a fixture observation.
    # Real V1 vision/audio inference is validated separately against real tracks.
    duration = answer["media"]["duration_seconds"]
    return {"processing":{"observations":[],"method":"fixture"},
            "state_estimate":{"duration_seconds":duration,"method":"fixture","source":"local_estimate",
                              "segments":[{"start":0,"end":duration,"state":"平和"}]},
            "transcript":""}


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        provider = patch.dict(os.environ, {"MMSA_V2_CONTENT_PROVIDER":"local_demo"})
        provider.start()
        self.addCleanup(provider.stop)
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Workflow(temp.name)
        self.client = TestClient(create_app(temp.name))
        self.addCleanup(self.client.close)
        self.headers = {"Authorization":"Bearer "+operator_key(Path(temp.name))}
        self.replies = json.loads(FIXTURE.read_text(encoding="utf-8"))
        bid = self.post("/batches", {"name":"模拟评分测试"}, operator=True)["id"]
        imported = self.client.post(f"/v2/api/batches/{bid}/resumes", files={"file":("模拟学生.docx",simulation_docx())},
                                    headers=self.headers)
        imported.raise_for_status()
        cid = imported.json()["candidates"][0]["id"]
        self.opened = self.post(f"/batches/{bid}/sessions", {"candidate_id":cid}, operator=True)
        self.sid = self.opened["id"]
        self.operator_url = "/sessions/"+self.sid
        self.candidate_url = "/c/"+self.opened["token"]

    def post(self, path, data, operator=False, expected=200):
        r = self.client.post("/v2/api"+path, json=data, headers=self.headers if operator else {})
        self.assertEqual(r.status_code, expected, r.text[:350])
        return r.json()

    def report(self, version=None):
        url = "/v2/api"+self.operator_url+"/report"
        r = self.client.get(url, headers=self.headers, params={} if version is None else {"version":version})
        r.raise_for_status()
        return r.json()

    def answer_all(self, media=False):
        for q in self.opened["plan"]:
            s = self.post(self.candidate_url+"/answers/"+q["id"], {"value":q["options"][0]["id"]})
        orals = [q["id"] for q in s["plan"] if q["kind"]=="text"]
        self.assertCountEqual(orals, self.replies)
        for qid in orals:
            self.post(self.candidate_url+"/answers/"+qid, {"value":self.replies[qid]})
            if media:
                response = self.client.post("/v2/api"+self.candidate_url+"/answers/"+qid+"/media",
                    files={"file":("合成样片.wav",wav_fixture(),"audio/wav")})
                response.raise_for_status()

    def test_submission_computes_pinned_total_and_preserves_manual_update(self):
        for _ in range(2):
            self.post(self.candidate_url+"/finish", {}, expected=422)
            self.post(self.candidate_url+"/operation-event", {"code":"empty_save","question_id":self.opened["plan"][0]["id"]})
        self.answer_all(media=True)
        with patch("src.mmsa_v2.auto_media.analyze_answer", side_effect=stub_analysis) as analyze:
            queued = self.post(self.candidate_url+"/finish", {})
        self.assertEqual(analyze.call_count,3)
        report = self.report()
        self.assertEqual(report["auto_scoring"]["status"],"done")
        self.assertEqual(report["draft"]["source"],"local_auto")
        self.assertEqual(len(report["operation_log"]["errors"]),2)
        self.assertEqual(report["components"]["operation"]["score"],80)
        self.assertEqual(report["components"]["multimodal"]["score"],50)
        self.assertTrue(all(f["rating"] is not None for f in report["factors"]))
        self.assertAlmostEqual(report["total"],sum(c["contribution"] for c in report["components"].values()))
        historical = self.report(queued["version"])
        self.assertIsNone(historical["total"])
        html = self.client.get("/v2/api"+self.operator_url+"/report.html",headers=self.headers)
        self.assertIn(f'{report["total"]:.2f}'.rstrip("0").rstrip("."),html.text)
        self.post(self.operator_url+"/auto-score",{"version":report["version"]},operator=True)
        self.assertEqual(self.report()["version"],report["version"])
        body = {"author":"复核者","version":report["version"],"input_version":report["input_version"],
                "operation_score":70,"operation_reason":"复核训练记录后调整。"}
        changed = self.post(self.operator_url+"/local-draft",body,operator=True)
        self.post(self.operator_url+"/auto-score",{"version":changed["version"]},operator=True,expected=409)
        self.assertEqual(self.report()["components"]["operation"]["score"],70)
        self.assertEqual(self.report(report["version"])["total"],report["total"])

    def test_historical_missing_log_and_text_only_keep_missing_evidence(self):
        self.store.change(self.sid, {}, lambda state, bank: state.pop("operation_log"))
        self.answer_all(media=False)
        self.post(self.candidate_url+"/finish", {})
        report = self.report()
        self.assertEqual(report["auto_scoring"]["status"],"partial")
        self.assertGreater(report["components"]["experience"]["score"],0)
        self.assertGreater(report["components"]["potential"]["score"],0)
        self.assertIsNone(report["components"]["operation"]["score"])
        self.assertIsNone(report["components"]["multimodal"]["score"])
        self.assertIsNone(report["total"])
