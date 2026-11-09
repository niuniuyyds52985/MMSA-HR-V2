"""Portable Qwen package, real JSON-RPC framing, and secret-free projections.

No host Skill registration, running production service or cloud calls are used.
"""
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch
import zipfile

from scripts.prepare_qwenwork_v2 import prepare
from src.mmsa_v2 import launch
from src.mmsa_v2.connector import v2_client
from src.mmsa_v2.qwen_stdio import V2Server, protocol


class ConnectorTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        env = patch.dict(os.environ, {"MMSA_V2_DATA_DIR": str(self.root), "MMSA_V2_BASE_URL": "http://127.0.0.1:18767"})
        env.start()
        self.addCleanup(env.stop)
        self.client = v2_client(protocol)
        starter = patch("src.mmsa_v2.launch.start", return_value={"ready": True, "build": "test", "update_available": False})
        self.start = starter.start()
        self.addCleanup(starter.stop)
        request = patch.object(self.client.v2, "_request")
        self.request = request.start()
        self.addCleanup(request.stop)

    def test_stdio_is_framed_and_does_not_start_for_handshake(self):
        server = V2Server(self.client)
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        response = server.dispatch(initialize)
        self.assertEqual(response["result"]["serverInfo"]["name"], "zhenjian-v2")
        self.start.assert_not_called()
        self.request.side_effect = [{"demo_resume_count": 0}, {"batches": []}]
        def noisy_start(*args, **kwargs):
            print("native helper diagnostic")
            return {"ready": True}
        self.start.side_effect = noisy_start
        frames = [initialize, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
                  {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "v2_status", "arguments": {}}}]
        incoming = io.BytesIO(("\n".join(json.dumps(v) for v in frames) + "\n").encode())
        outgoing, diagnostic, stray = io.BytesIO(), io.StringIO(), io.StringIO()
        from contextlib import redirect_stderr, redirect_stdout
        with redirect_stdout(stray), redirect_stderr(diagnostic):
            server.serve(incoming, outgoing)
        self.assertFalse(stray.getvalue())
        replies = [json.loads(line) for line in outgoing.getvalue().splitlines()]
        self.assertEqual([v["id"] for v in replies], [1, 2, 3])
        names = [v["name"] for v in replies[1]["result"]["tools"]]
        self.assertIn("v2_open_workbench", names)
        self.assertEqual(replies[2]["result"]["structuredContent"]["status"]["demo_resume_count"], 0)
        self.assertIn("native helper diagnostic", diagnostic.getvalue())
        self.start.assert_called_once_with(18767, False, announce=False, restart_stale=False)

    def test_browser_request_uses_key_without_returning_it(self):
        self.request.return_value = {"id": "scene"}
        with patch("src.mmsa_v2.connector.webbrowser.open", return_value=True) as browser:
            result = self.client.call("v2_open_workbench", {"session_id": "scene"})
        url = browser.call_args.args[0]
        key = (self.root / ".operator-key").read_text()
        self.assertTrue(url.endswith("#access=" + key))
        self.assertEqual(result["workbench_url"], "http://127.0.0.1:18767/v2/?session=scene")
        self.assertNotIn(key, json.dumps(result))
        self.assertTrue(result["browser_open_requested"])
        self.assertNotIn("opened", result)

    def test_invalid_open_target_has_no_start_or_read_side_effect(self):
        with self.assertRaises(protocol.ToolError):
            self.client.call("v2_open_workbench", {"session_id": "s", "batch_id": "b"})
        self.start.assert_not_called()
        self.request.assert_not_called()

    def test_failed_service_has_bounded_error(self):
        self.start.side_effect = RuntimeError("private path and sensitive native error")
        with self.assertRaises(protocol.ToolError) as raised:
            self.client.call("v2_status", {})
        self.assertEqual(raised.exception.code, "workbench_start_failed")
        self.assertNotIn("private", str(raised.exception))

    def test_report_preserves_scoring_and_model_sources_without_raw_media(self):
        self.request.return_value = {"id": "s", "version": 28, "total": None, "estimated_total": 63.71,
            "components": {"potential": {"score": 70, "contribution": 42}},
            "auto_scoring": {"status": "done", "engine": "bailian", "nodes": {"content": {"status": "succeeded", "input_key": "not-needed"}}},
            "draft": {"source": "bailian_auto", "author": "百炼", "model_provenance": {"provider": "aliyun", "model": "qwen-plus", "api_key": "secret"}},
            "factors": [{"id": "F1", "name": "领导意愿", "rating": 3, "rating_source": "model", "evidence": ["huge"]}],
            "answers": {"oral": {"transcript": "原话", "media": {"path": "PRIVATE"}, "transcription_provenance": {"model": "qwen-asr", "provider": "aliyun"}}},
            "session": {"coverage": .88}, "token": "private-token"}
        result = self.client.call("v2_report", {"session_id": "s"})
        self.assertEqual(result["display_score"], 63.71)
        self.assertEqual(result["score_kind"], "estimate")
        self.assertEqual(result["auto_scoring"]["status"], "done")
        self.assertEqual(result["draft"]["model_provenance"]["model"], "qwen-plus")
        self.assertEqual(result["transcriptions"][0]["provenance"]["model"], "qwen-asr")
        self.assertEqual(result["factors"][0]["rating"], 3)
        for excluded in ("PRIVATE", "private-token", "secret", "huge", "not-needed"):
            self.assertNotIn(excluded, json.dumps(result))

    def test_partial_score_is_not_scaled_to_full_total(self):
        self.request.return_value = {"total": None, "estimated_total": None,
            "components": {"potential": {"score": None, "partial_contribution": 37.5}, "operation": {"contribution": 2}}}
        result = self.client.call("v2_report", {"session_id": "s"})
        self.assertEqual(result["display_score"], 39.5)
        self.assertEqual(result["score_kind"], "partial")

    def test_batch_contains_available_student_link_and_rank(self):
        self.request.return_value = {"id": "b", "candidates": [{"id": "p", "display_score": 72,
            "score_kind": "estimate", "rank": 1, "student_url": "/v2/c/example", "analysis_source": "bailian_auto",
            "resume": {"private": "not-projected"}}]}
        result = self.client.call("v2_batch_people", {"batch_id": "b"})
        encoded = json.dumps(result)
        self.assertIn("/v2/c/example", encoded)
        self.assertIn('"rank": 1', encoded)
        self.assertNotIn("not-projected", encoded)


class PackageTests(unittest.TestCase):
    def make_project(self, root):
        for relative, content in ((".venv-suit/Scripts/python.exe", "fixture"),
                ("src/mmsa_v2/qwen_stdio.py", "fixture"),
                ("docs/qwenwork-v2-staging/zhenjian-v2/SKILL.md", "---\nname: zhenjian-v2\ndescription: fixture\n---\n")):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

    def test_portable_unicode_path_and_no_host_install(self):
        with TemporaryDirectory() as folder:
            root = Path(folder).resolve() / "中文 项目"
            self.make_project(root)
            result = prepare(root, port=18867)
            config = json.loads(Path(result["config_file"]).read_text(encoding="utf-8"))["mcpServers"]["zhenjian-v2"]
            self.assertEqual(config["command"], str(root / ".venv-suit/Scripts/python.exe"))
            self.assertEqual(config["args"], ["-X", "utf8", str(root / "src/mmsa_v2/qwen_stdio.py"), "--port", "18867"])
            self.assertFalse(result["host_installed"])
            self.assertFalse(result["contains_credentials"])
            with zipfile.ZipFile(result["skill_zip"]) as packed:
                self.assertEqual(set(packed.namelist()), {"zhenjian-v2/SKILL.md", "zhenjian-v2/references/local-install.md"})
            self.assertEqual(set(config["env"]), {"PYTHONUTF8", "PYTHONIOENCODING"})
            self.assertNotIn("X:\\Private\\Workspace", json.dumps(config))
            # Re-running is safe and regenerates this machine's configuration.
            self.assertEqual(prepare(root, port=18867)["config_file"], result["config_file"])

    def test_no_configuration_claim_before_environment_exists(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            with self.assertRaises(FileNotFoundError):
                prepare(root)
            self.assertFalse((root / "delivery").exists())

    def test_cannot_copy_into_host_active_skills(self):
        with TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            self.make_project(root)
            with self.assertRaises(ValueError):
                prepare(root, Path(folder) / ".qwenworkcn/skills/zhenjian-v2")


class RuntimeTests(unittest.TestCase):
    def test_simultaneous_launch_requests_are_serialized(self):
        from concurrent.futures import ThreadPoolExecutor
        active, maximum = 0, 0
        def work(*args, **kwargs):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            time.sleep(.08)
            active -= 1
            return {"ready": True}
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"MMSA_V2_DATA_DIR": folder}), \
                patch.object(launch, "_start", side_effect=work):
            with ThreadPoolExecutor(max_workers=2) as workers:
                results = list(workers.map(lambda _: launch.start(18767, False), range(2)))
        self.assertEqual(maximum, 1)
        self.assertTrue(all(result["ready"] for result in results))

    def test_connector_reuses_older_service_without_stopping_jobs(self):
        with TemporaryDirectory() as folder, patch.dict(os.environ, {"MMSA_V2_DATA_DIR": folder}), \
                patch.object(launch, "build_id", return_value="new"), patch.object(launch, "build_opener") as opener, \
                patch.object(launch.subprocess, "Popen") as process:
            opener.return_value.open.side_effect = lambda *a, **k: io.BytesIO(b'{"service":"mmsa-v2-local","build":"old"}')
            result = launch.start(18767, False, announce=False, restart_stale=False)
            self.assertTrue(result["update_available"])
            self.assertEqual(result["build"], "old")
            self.assertEqual(opener.return_value.open.call_count, 1)
            process.assert_not_called()

    def test_real_stdio_starts_isolated_empty_service(self):
        """Exercise the distributed executable entry, auth and HTTP transport."""
        from urllib.request import Request, ProxyHandler, build_opener
        from urllib.error import URLError
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        opener = build_opener(ProxyHandler({}))
        with TemporaryDirectory() as folder:
            env = {**os.environ, "MMSA_V2_DATA_DIR": folder, "MMSA_V2_CONTENT_PROVIDER": "local_demo"}
            frames = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                      {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "v2_status", "arguments": {}}},
                      {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "v2_batches", "arguments": {}}}]
            try:
                result = subprocess.run([sys.executable, "-X", "utf8", str(launch.PROJECT / "src/mmsa_v2/qwen_stdio.py"),
                                         "--port", str(port)], input="\n".join(json.dumps(f) for f in frames) + "\n",
                                        encoding="utf-8", capture_output=True, env=env, cwd=folder, timeout=40)
                self.assertEqual(result.returncode, 0, result.stderr)
                replies = [json.loads(line) for line in result.stdout.splitlines()]
                self.assertEqual(len(replies), 3)
                status = replies[1]["result"]["structuredContent"]
                self.assertTrue(status.get("authenticated"), status)
                self.assertEqual(replies[2]["result"]["structuredContent"], {"batches": []})
                key = (Path(folder) / ".operator-key").read_text()
                self.assertNotIn(key, result.stdout)
                self.assertEqual(status["verification"]["bailian_api"], "not_checked_by_this_tool")
            finally:
                keyfile = Path(folder) / ".operator-key"
                if keyfile.exists():
                    headers = {"Authorization": "Bearer " + keyfile.read_text()}
                    base = f"http://127.0.0.1:{port}/v2/api/runtime"
                    try:
                        opener.open(Request(base + "/shutdown", method="POST", headers=headers), timeout=3).close()
                    except (URLError, OSError):
                        pass
                    for _ in range(60):
                        try:
                            opener.open(Request(base, headers=headers), timeout=.25).close()
                        except (URLError, OSError):
                            break
                        time.sleep(.1)
                    time.sleep(.3)


if __name__ == "__main__":
    unittest.main()
