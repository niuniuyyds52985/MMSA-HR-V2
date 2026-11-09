"""Exercise the shipped tree from a different directory, without private inputs.

Uses the caller's Python dependencies, not a claim of a clean-machine install.
The child is offline, uses synthetic text only and has its own empty data root.
"""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


PROJECT = Path(__file__).resolve().parents[2]

SMOKE = r'''
from io import BytesIO
import json
from pathlib import Path
import socket
from zipfile import ZipFile

# Fail rather than making an accidental external request in a packaging test.
def offline(*args, **kwargs):
    raise AssertionError("Network is not allowed in this source-package smoke test")
socket.create_connection = offline

from fastapi.testclient import TestClient
from src.mmsa_v2 import api
from src.mmsa_v2.access import data_root, operator_key
assert Path(api.__file__).resolve().is_relative_to(Path.cwd().resolve()), (api.__file__, str(Path.cwd()))
assert not (Path.cwd() / "模拟简历").exists()
app = api.create_app(data_root())
headers = {"Authorization": "Bearer " + operator_key(data_root())}
client = TestClient(app)

def post(path, body=None, files=None):
    response = client.post("/v2/api" + path, json=body, files=files, headers=headers)
    response.raise_for_status()
    return response.json()

assert client.get("/v2/").status_code == 200
assert client.get("/v2/ui/opening.js").status_code == 200
status = client.get("/v2/api/status").json()
assert status["factors"] == 8
assert client.get("/v2/api/batches", headers=headers).json() == {"batches": []}
batch = post("/batches", {"name": "独立安装验收（合成资料）"})
path = "/batches/" + batch["id"]
with BytesIO() as payload:
    with ZipFile(payload, "w") as doc:
        doc.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>合成测试同学</w:t></w:r></w:p></w:body></w:document>')
    people = post(path + "/resumes", files={"file": ("合成测试简历.docx", payload.getvalue())})
state = post(path + "/sessions", {"candidate_id": people["candidates"][0]["id"]})
sid = state["id"]
candidate = "/c/" + state["token"]
assert len(state["plan"]) == 5
for question in state["plan"]:
    state = post(candidate + "/answers/" + question["id"], {"value": question["options"][0]["id"]})
assert len(state["plan"]) == 8
assert len({q["factor_id"] for q in state["plan"]}) == 8
for question in state["plan"][5:]:
    post(candidate + "/answers/" + question["id"], {"value": "这是合成验收回答。我在课程项目中负责整理资料，按时完成了任务。"})
post(candidate + "/finish", {})
report = client.get("/v2/api/sessions/" + sid + "/report", headers=headers)
report.raise_for_status()
assert len(report.json()["factors"]) == 8
html = client.get("/v2/api/sessions/" + sid + "/report.html", headers=headers)
html.raise_for_status()
assert "潜力八因子" in html.text
cohort = client.get("/v2/api" + path + "/report.html", headers=headers)
cohort.raise_for_status()
export = post("/sessions/" + sid + "/export", {})
export_path = Path(export["file"])
assert export_path.is_relative_to(data_root()) and export_path.stat().st_size > 1000
assert "合成测试同学" in export_path.read_text(encoding="utf-8")
print(json.dumps({"isolated_tree": True, "candidate_count": 1, "questions": 8,
    "factors": 8, "personal_html": True, "cohort_html": True, "export_html": True,
    "cloud_calls": False, "models_executed": False}))
'''


class PortableDeliveryTests(unittest.TestCase):
    def test_extracted_source_runs_without_original_documents_or_machine_paths(self):
        spec = importlib.util.spec_from_file_location("portable_packager", PROJECT / "suit-demo/delivery_tools.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory(prefix="mmsa-v2-portable-") as temporary:
            root = Path(temporary)
            packed = module.build_source(PROJECT, root / "archives")
            extracted = root / "另一个目录 with spaces"
            extracted.mkdir()
            with zipfile.ZipFile(packed["archive"]) as archive:
                names = archive.namelist()
                self.assertFalse(any(".secrets-user" in name or "local-data" in name for name in names))
                archive.extractall(extracted)
            env = dict(os.environ)
            for name in list(env):
                if name.startswith(("MMSA_", "INTERVIEW_", "DASHSCOPE_")) or name == "PYTHONPATH":
                    env.pop(name)
            env.update(MMSA_V2_CONTENT_PROVIDER="local_demo", MMSA_V2_DATA_DIR=str(root / "empty-data"))
            result = subprocess.run([sys.executable, "-X", "utf8", "-c", SMOKE],
                cwd=extracted, env=env, capture_output=True, text=True, encoding="utf-8", timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr[-5000:] + result.stdout[-1500:])
            result = json.loads(result.stdout.strip())
            self.assertEqual(result["questions"], 8)
            self.assertTrue(result["export_html"])


if __name__ == "__main__":
    unittest.main()
