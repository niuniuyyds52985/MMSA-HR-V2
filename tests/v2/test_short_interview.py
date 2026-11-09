import os
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave
from zipfile import ZipFile

from fastapi.testclient import TestClient

from src.mmsa_v2.access import operator_key
from src.mmsa_v2.api import create_app
from src.mmsa_v2.sources import load_bank


class ShortInterviewTests(unittest.TestCase):
    def setUp(self):
        provider = patch.dict(os.environ, {"MMSA_V2_CONTENT_PROVIDER":"local_demo"})
        provider.start()
        self.addCleanup(provider.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.client = TestClient(create_app(directory.name))
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer " + operator_key(Path(directory.name))}
        self.bank = {q["id"]: q for f in load_bank()["factors"] for q in f["questions"]}

    def start(self, legacy=False, batch_name="短卷测试"):
        batch = self.post("/batches", {"name":batch_name}, teacher=True)
        base = "/batches/" + batch["id"]
        with BytesIO() as file:
            with ZipFile(file, "w") as archive:
                archive.writestr("word/document.xml",
                    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                    '<w:body><w:p><w:r><w:t>测试同学</w:t></w:r></w:p></w:body></w:document>')
            response = self.client.post("/v2/api"+base+"/resumes",
                files={"file": ("测试同学.docx", file.getvalue())}, headers=self.headers)
        response.raise_for_status()
        body = {"candidate_id": response.json()["candidates"][0]["id"]}
        if legacy:
            body["interview_mode"] = "legacy_full"
        state = self.post(base+"/sessions", body, teacher=True)
        self.session = "/sessions/"+state["id"]
        self.candidate = "/c/"+state["token"]
        return state

    def post(self, path, body, status=200, teacher=False):
        response = self.client.post("/v2/api"+path, json=body, headers=self.headers if teacher else {})
        self.assertEqual(response.status_code, status, response.text[:300])
        return response.json()

    def test_source_routing_private_scores_and_saved_recording(self):
        state = self.start()
        screening = state["plan"]
        self.assertEqual(len(screening), 5)
        self.assertEqual(state["expected_questions"], 8)
        self.post(self.candidate+"/finish", {}, 422)
        for number, q in enumerate(screening, 1):
            original = self.bank[q["id"]]
            self.assertEqual((q["kind"],q["stage"]), ("choice","screening"))
            self.assertEqual((q["prompt"],q["options"],q["source"],q["score_source"]),
                             (original["prompt"],original["options"],original["source"],original["score_source"]))
            value = next(k for k,v in original["answer_key"].items() if v == 5)
            state = self.post(self.candidate+"/answers/"+q["id"], {"value": value})
            self.assertEqual(len(state["plan"]), 8 if number == 5 else 5)
            self.assertNotIn("sjt_score", state["answers"][q["id"]])
        open_ids = [q["id"] for q in state["plan"][5:]]
        self.assertEqual(len({q["factor_id"] for q in state["plan"]}), 8)
        for q in state["plan"]:
            self.assertFalse({"reason","sjt_score","score_source","answer_key"}.intersection(q))
            self.assertEqual(q["prompt"], self.bank[q["id"]]["prompt"])
        # The test resume has no experience signals. Lower the growth-dimension SJT response.
        old = next(q for q in screening if q["factor_id"] == "F7")
        low = next(k for k,v in self.bank[old["id"]]["answer_key"].items() if v == 1)
        choice_url = self.candidate+"/answers/"+old["id"]
        state = self.post(choice_url, {"value": low})
        reordered = [q["id"] for q in state["plan"][5:]]
        self.assertCountEqual(open_ids, reordered)
        self.assertNotEqual(open_ids, reordered)
        self.assertEqual(reordered[0], "F8-G")
        teacher = self.client.get("/v2/api"+self.session, headers=self.headers).json()
        self.assertEqual(teacher["answers"][old["id"]]["sjt_score"], 1)
        self.assertIn(old["score_source"], teacher["plan"][5]["reason"])
        self.assertEqual(self.client.get("/v2/api"+self.candidate).json(), state)

        with BytesIO() as audio:
            with wave.open(audio, "wb") as wav:
                wav.setparams((1,2,8000,0,"NONE","not compressed"))
                wav.writeframes(bytes(16000))
            media_url = "/v2/api"+self.candidate+"/answers/"+reordered[0]+"/media"
            response = self.client.post(media_url, files={"file":("作答.wav",audio.getvalue(),"audio/wav")})
        response.raise_for_status()
        saved = response.json()
        self.assertEqual(self.post(choice_url, {"value": low})["version"], saved["version"])
        other = next(o["id"] for o in old["options"] if o["id"] != low)
        self.post(choice_url, {"value": other}, 409)
        for qid in reordered[1:]:
            self.post(self.candidate+"/answers/"+qid, {"value": "这是我的作答。"})
        final = self.post(self.candidate+"/finish", {})
        self.assertEqual(final["status"], "interview_finished")
        self.assertEqual(final["answers"][reordered[0]], saved["answers"][reordered[0]])

    def test_only_empty_open_legacy_session_can_be_migrated(self):
        old = self.start(legacy=True)
        route = self.session+"/short-interview"
        self.post(route, {"version":old["version"]}, 401)
        self.post(route, {"version":old["version"]+1}, 409, teacher=True)
        s = self.post(route, {"version":old["version"]}, teacher=True)
        self.assertEqual((s["id"],s["token"],s["resume"]), (old["id"],old["token"],old["resume"]))
        self.assertEqual(s["interview_mode"], "short_5_3")
        self.assertEqual(self.post(route, {"version":old["version"]}, teacher=True)["version"], s["version"])
        history = self.client.get("/v2/api"+self.session+"?version=1", headers=self.headers).json()
        self.assertEqual(history["plan"], old["plan"])
        old = self.start(legacy=True, batch_name="已有长卷作答")
        s = self.post(self.candidate+"/answers/EXPERIENCE", {"value":"已有作答"})
        self.post(self.session+"/short-interview", {"version":s["version"]}, 409, teacher=True)
        s = self.post(self.session+"/finish", {"allow_incomplete":True}, teacher=True)
        self.post(self.session+"/short-interview", {"version":s["version"]}, 409, teacher=True)
        self.assertEqual(s["plan"], old["plan"])
