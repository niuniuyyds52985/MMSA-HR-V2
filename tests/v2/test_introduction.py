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


class IntroductionTests(unittest.TestCase):
    def setUp(self):
        provider = patch.dict(os.environ, {"MMSA_V2_CONTENT_PROVIDER":"local_demo"})
        provider.start()
        self.addCleanup(provider.stop)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.app = create_app(directory.name)
        self.store = self.app.state.workflow
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization":"Bearer " + operator_key(Path(directory.name))}
        batch = self.store.new_batch("自我介绍回归")
        with BytesIO() as file:
            with ZipFile(file, "w") as archive:
                archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>测试同学</w:t></w:r></w:p></w:body></w:document>')
            people = self.store.import_files(batch["id"], [("测试.docx",file.getvalue())])
        self.state = self.store.create_session(batch["id"],people["candidates"][0]["id"])
        self.sid = self.state["id"]
        self.base = "/v2/api/c/" + self.state["token"]

    def audio(self, value=0):
        with BytesIO() as file:
            with wave.open(file, "wb") as wav:
                wav.setparams((1,2,8000,0,"NONE","not compressed"))
                wav.writeframes(value.to_bytes(2,"little",signed=True)*8000)
            return file.getvalue()

    def upload(self, version, value=0):
        response = self.client.post(self.base+"/introduction/media",
            files={"file":("介绍.wav",self.audio(value),"audio/wav")},
            data={"version":str(version),"text":"这是我的暖场介绍。"})
        response.raise_for_status()
        return response.json()

    def test_optional_text_skip_and_conflicts_leave_scoring_inputs_alone(self):
        initial_report = self.store.report(self.sid)
        self.assertEqual(self.client.post(self.base+"/introduction",json={"text":""}).status_code,422)
        skipped = self.client.post(self.base+"/introduction",json={"skip":True,"version":1}).json()
        repeated = self.client.post(self.base+"/introduction",json={"skip":True,"version":1}).json()
        self.assertEqual(repeated["version"],skipped["version"])
        self.assertEqual(skipped["introduction"],{"status":"skipped"})
        self.assertEqual(skipped["answers"],{})
        self.assertEqual(self.client.post(self.base+"/introduction",json={"text":"过时的页面","version":1}).status_code,409)
        saved = self.client.post(self.base+"/introduction",json={"text":"我喜欢阅读。","version":skipped["version"]}).json()
        self.assertEqual(saved["introduction"]["text"],"我喜欢阅读。")
        self.assertEqual(saved["input_version"],self.state["input_version"])
        self.assertEqual(saved["expected_questions"],8)
        report = self.store.report(self.sid)
        for key in ("components","questions","factors","session","total"):
            self.assertEqual(report[key],initial_report[key])
        self.assertEqual(self.store.session(self.sid)["operation_log"]["errors"],[])

    def test_recording_history_and_scope(self):
        first = self.upload(1)
        source = first["introduction"]["media"]["url"]
        self.assertEqual(self.client.get(source).content,self.audio())
        self.assertNotIn("stored_name",first["introduction"]["media"])
        teacher_url = "/v2/api/sessions/"+self.sid+"/introduction/media?sha256="+first["introduction"]["media"]["sha256"]
        self.assertEqual(self.client.get(teacher_url).status_code,401)
        self.upload(first["version"],1)
        self.assertEqual(self.client.get(source).status_code,409)
        self.assertEqual(self.client.get(teacher_url,headers=self.headers).content,self.audio())
        history = self.store.report(self.sid,first["version"])
        self.assertEqual(history["introduction"]["media"]["sha256"],first["introduction"]["media"]["sha256"])

    def test_warmup_media_is_excluded_from_automatic_scoring_and_answer_count(self):
        saved = self.upload(1)
        for q in saved["plan"]:
            state = self.store.answer(self.sid,q["id"],{"value":q["options"][0]["id"]})
        for q in state["plan"][5:]:
            self.store.answer(self.sid,q["id"],{"value":"这是正式问答原文。"})
        with patch("src.mmsa_v2.auto_media.analyze_answer") as analyze:
            response = self.client.post(self.base+"/finish",json={})
            response.raise_for_status()
            analyze.assert_not_called()
        report = self.store.report(self.sid)
        self.assertEqual(len(report["answers"]),8)
        self.assertEqual(len(report["questions"]),8)
        self.assertEqual(report["session"]["valid_seconds"],0)
        self.assertIsNone(report["components"]["multimodal"]["score"])
        self.assertEqual(report["operation_log"]["errors"],[])
        self.assertNotIn("processing",report["introduction"])
        self.assertEqual(self.client.post(self.base+"/introduction",json={"text":"修改","version":report["version"]}).status_code,409)
