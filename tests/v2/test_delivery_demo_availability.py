"""A source-only installation can start with the recipient's own resumes."""
from io import BytesIO
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from fastapi.testclient import TestClient

from src.mmsa_v2.access import operator_key
from src.mmsa_v2.api import create_app


def resume_bytes():
    with BytesIO() as data:
        with ZipFile(data, "w") as archive:
            archive.writestr("word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                '<w:body><w:p><w:r><w:t>姓名：独立使用测试</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>教育背景</w:t></w:r></w:p>'
                '<w:p><w:r><w:t>测试大学 本科</w:t></w:r></w:p></w:body></w:document>')
        return data.getvalue()


class DeliveryDemoAvailabilityTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.folder = Path(directory.name)
        root = patch("src.mmsa_v2.api.ROOT", self.folder / "checkout")
        root.start()
        self.addCleanup(root.stop)
        provider = patch.dict(os.environ, {"MMSA_V2_CONTENT_PROVIDER": "local_demo"})
        provider.start()
        self.addCleanup(provider.stop)
        self.app = create_app(self.folder / "data")
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.headers = {"Authorization": "Bearer " + operator_key(self.folder / "data")}

    def test_missing_samples_do_not_block_own_resume_and_short_interview(self):
        status = self.client.get("/v2/api/status").json()
        self.assertEqual(status["demo_resume_count"], 0)
        batch = self.client.post("/v2/api/batches", headers=self.headers,
                                 json={"name": "自己的候选人"}).json()
        url = "/v2/api/batches/" + batch["id"]
        uploaded = self.client.post(url + "/resumes", headers=self.headers,
                                   files={"file": ("独立使用测试.docx", resume_bytes())})
        self.assertEqual(uploaded.status_code, 200, uploaded.text)
        people = uploaded.json()["candidates"]
        self.assertEqual(len(people), 1)
        started = self.client.post(url + "/sessions", headers=self.headers,
                                   json={"candidate_id": people[0]["id"]})
        self.assertEqual(started.status_code, 200, started.text)
        self.assertEqual(started.json()["expected_questions"], 8)
        self.assertEqual(len(started.json()["plan"]), 5)
        self.assertFalse((self.folder / "checkout/模拟简历").exists())

    def test_availability_counts_regular_docx_and_excludes_word_locks(self):
        samples = self.folder / "checkout/模拟简历"
        samples.mkdir(parents=True)
        for name in ("甲.docx", "乙.docx", "~$甲.docx", "说明.txt"):
            (samples / name).write_bytes(resume_bytes())
        (samples / "文件夹.docx").mkdir()
        self.assertEqual(self.client.get("/v2/api/status").json()["demo_resume_count"], 2)
        batch = self.client.post("/v2/api/batches", headers=self.headers,
                                 json={"name": "可用样本"}).json()
        imported = self.client.post("/v2/api/batches/" + batch["id"] + "/import-demo",
                                    headers=self.headers, json={})
        self.assertEqual(imported.status_code, 200, imported.text)
        # Same bytes are deduplicated by import; neither the lock nor folder is read.
        self.assertEqual(imported.json()["stats"]["count"], 1)

    def test_empty_import_guides_to_own_documents_and_does_not_add_candidates(self):
        batch = self.client.post("/v2/api/batches", headers=self.headers,
                                 json={"name": "空安装"}).json()
        url = "/v2/api/batches/" + batch["id"]
        response = self.client.post(url + "/import-demo", headers=self.headers, json={})
        self.assertEqual(response.status_code, 404)
        self.assertIn("自己的 DOCX", response.json()["detail"])
        self.assertEqual(self.client.get(url, headers=self.headers).json()["candidates"], [])


if __name__ == "__main__":
    unittest.main()
