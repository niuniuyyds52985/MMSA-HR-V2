"""Fixed models, approved endpoints and a non-decrypting setup status contract."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from src.mmsa_v2 import cloud_policy as policy
from src.mmsa_v2 import cloud_content as content
from src.mmsa_v2 import cloud_asr as asr


ROOT = Path(__file__).resolve().parents[2]


def clean_environment():
    environment = dict(os.environ)
    for name in ("INTERVIEW_CREDENTIALS_FILE", "DASHSCOPE_API_KEY", "INTERVIEW_LLM_MODEL", "INTERVIEW_LLM_BASE_URL"):
        environment.pop(name, None)
    return environment


def fixture_credentials(path, model=policy.TEXT_MODEL, base=policy.DEFAULT_BASE_URL):
    # Deliberately not decryptable. The status path must not try to decrypt it.
    path.write_text(json.dumps({"format": "windows-dpapi-current-user.v1", "model": model,
        "base_url": base, "encrypted_key": "not-a-real-encrypted-key"}), encoding="utf-8")


class PolicyTests(unittest.TestCase):
    def test_official_regions_and_existing_workspace_routes(self):
        cases = {policy.DEFAULT_BASE_URL: "cn-beijing",
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1": "ap-southeast-1",
            "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1": "cn-beijing",
            "https://workspace.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1": "ap-southeast-1"}
        for endpoint, region in cases.items():
            with self.subTest(endpoint=endpoint):
                self.assertEqual(policy.validate_endpoint(endpoint + "/"), endpoint)
                self.assertEqual(policy.endpoint_region(endpoint), region)

    def test_endpoint_policy_rejects_credentials_redirects_and_other_hosts(self):
        invalid = [None, "", "http://dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com.evil.example/compatible-mode/v1",
            "https://user:private@dashscope.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com:99999/compatible-mode/v1",
            "https://dashscope.aliyuncs.com:444/compatible-mode/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1?key=private",
            "https://dashscope.aliyuncs.com/compatible-mode/v1#private",
            "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
            "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "https://dashscope.aliyuncs.com/compatible-mode/v1\n"]
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint), self.assertRaises(policy.CloudPolicyError) as caught:
                policy.validate_endpoint(endpoint)
            self.assertNotIn("private", str(caught.exception))

    def test_model_and_environment_override_are_both_checked(self):
        allowed = {"model": policy.TEXT_MODEL, "base_url": policy.DEFAULT_BASE_URL}
        self.assertEqual(policy.validate_text_settings(allowed, environ={}), allowed)
        for configured, environment in (({**allowed, "model": "other-model"}, {}),
                (allowed, {"INTERVIEW_LLM_MODEL": "other-model"})):
            with self.assertRaises(policy.CloudPolicyError) as caught:
                policy.validate_text_settings(configured, environ=environment)
            self.assertEqual(caught.exception.code, "unsupported_model")

    def test_text_model_change_rejected_before_key_read_or_network(self):
        for model in ("qwen-plus", "another-provider", "qwen3.7-plus-private"):
            with self.subTest(model=model), patch.dict(os.environ, clean_environment(), clear=True), \
                    patch("interview_credentials.public_settings", return_value={"model": model, "base_url": policy.DEFAULT_BASE_URL}), \
                    patch("interview_credentials.get_api_key") as key, patch("requests.post") as post:
                with self.assertRaises(content.ProviderError) as caught:
                    content._request_model({})
                self.assertEqual(caught.exception.code, "unsupported_model")
                key.assert_not_called()
                post.assert_not_called()

    def test_endpoint_change_rejected_before_key_read_or_network(self):
        with patch("interview_credentials.public_settings", return_value={"model": policy.TEXT_MODEL, "base_url": "https://example.com/compatible-mode/v1"}), \
                patch("interview_credentials.get_api_key") as key, patch("requests.post") as post:
            with self.assertRaises(content.ProviderError) as caught:
                content._request_model({})
            self.assertEqual(caught.exception.code, "unsupported_endpoint")
            key.assert_not_called()
            post.assert_not_called()

    def test_persisted_model_cannot_switch_new_requests(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "credentials.json"
            fixture_credentials(path, model="other-model")
            with patch.dict(os.environ, {**clean_environment(), "INTERVIEW_CREDENTIALS_FILE": str(path)}, clear=True), \
                    patch("interview_credentials.get_api_key") as key, patch("requests.post") as post:
                with self.assertRaises(content.ProviderError) as caught:
                    content._request_model({})
                self.assertEqual(caught.exception.code, "unsupported_model")
                key.assert_not_called()
                post.assert_not_called()

    def test_audio_model_is_policy_constant_and_bad_url_never_sends_audio(self):
        self.assertEqual(asr.MODEL, policy.ASR_MODEL)
        with patch.object(asr.requests, "post") as post:
            with self.assertRaises(asr.CloudASRError):
                asr._request(b"private-audio", "https://example.com/compatible-mode/v1", "private-key")
            post.assert_not_called()

    def test_status_never_decrypts_or_echoes_untrusted_settings(self):
        with patch("interview_credentials.public_settings", return_value={"model": "private-value", "base_url": "https://evil.example/?key=private-value"}), \
                patch("interview_credentials.credential_available", return_value=True), \
                patch("interview_credentials.get_api_key", side_effect=AssertionError("must not decrypt")) as key:
            status = policy.configuration_status()
        self.assertFalse(status["configured"])
        self.assertFalse(status["api_verified"])
        self.assertEqual(status["error"], "unsupported_model")
        self.assertNotIn("private-value", json.dumps(status))
        key.assert_not_called()

    def test_status_module_needs_only_standard_library(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "credentials.json"
            fixture_credentials(path)
            environment = {**clean_environment(), "INTERVIEW_CREDENTIALS_FILE": str(path)}
            process = subprocess.run([sys.executable, "-S", "-X", "utf8", "-m", "src.mmsa_v2.cloud_policy", "--status"],
                                     cwd=ROOT, env=environment, capture_output=True, encoding="utf-8", timeout=20)
            self.assertEqual(process.returncode, 0, process.stderr)
            result = json.loads(process.stdout)
            self.assertTrue(result["configured"])
            self.assertFalse(result["api_verified"])
            self.assertNotIn("not-a-real-encrypted-key", process.stdout)


@unittest.skipUnless(os.name == "nt", "Windows PowerShell/WinForms delivery")
class GuiContractTests(unittest.TestCase):
    script = ROOT / "scripts/configure-bailian-gui.ps1"

    def run_script(self, args, fixture):
        environment = {**clean_environment(), "INTERVIEW_CREDENTIALS_FILE": str(fixture)}
        return subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(self.script), *args],
                              cwd=ROOT, env=environment, capture_output=True, encoding="utf-8", errors="replace", timeout=30)

    def test_windows_powershell_51_parser_and_password_stdin_contract(self):
        raw = self.script.read_bytes()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), "PS 5.1 must recognize Chinese text as UTF-8")
        body = raw.decode("utf-8-sig")
        cmd = "$tokens=$null; $errors=$null; [void][System.Management.Automation.Language.Parser]::ParseFile($args[0],[ref]$tokens,[ref]$errors); if($errors.Count){exit 1}; Write-Output 'parsed'"
        # -Command trailing script arguments are expression text; use stdin with a single safe literal path.
        quoted = str(self.script).replace("'", "''")
        cmd = cmd.replace("$args[0]", "'" + quoted + "'")
        process = subprocess.run(["powershell.exe", "-NoProfile", "-Command", cmd], capture_output=True, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("$passwordBox.UseSystemPasswordChar = $true", body)
        self.assertIn("$child.StandardInput.WriteLine($apiKey)", body)
        self.assertIn("& $prepareScript -PrepareOnly", body)
        self.assertIn("$process.StartInfo.CreateNoWindow = $true", body)
        self.assertIn("if (-not (Test-InteractiveDesktop))", body)
        self.assertIn("exit 2", body)
        self.assertNotIn("X:\\Private\\Workspace", body)
        self.assertNotIn("Administrator", body)
        for line in body.splitlines():
            if "Arguments =" in line or "$arguments =" in line:
                self.assertNotIn("$apiKey", line)
        self.assertNotIn("ComboBox", body)

    def test_status_only_and_existing_config_skip_never_show_password_window(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "credentials.json"
            fixture_credentials(path)
            before = path.read_bytes()
            result = self.run_script(["-StatusOnly"], path)
            self.assertEqual(result.returncode, 0, result.stderr)
            status = json.loads(result.stdout)
            self.assertTrue(status["configured"])
            self.assertFalse(status["api_verified"])
            result = self.run_script([], path)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("not-a-real-encrypted-key", result.stdout + result.stderr)
            self.assertEqual(path.read_bytes(), before)

    def test_no_interactive_desktop_exits_before_any_winforms_creation(self):
        # Exercise the actual top-level branch with only metadata and desktop
        # discovery stubbed. No real password dialog or credential writer runs.
        source = self.script.read_text(encoding="utf-8-sig")
        source = re.sub(r"function Get-SafeConfigurationStatus \{.*?\n\}",
            "function Get-SafeConfigurationStatus { return [pscustomobject]@{configured=$false; environment_overrides=$false} }",
            source, count=1, flags=re.S)
        start = source.index("function Test-InteractiveDesktop {")
        end = source.index("\ntry {\n", start)
        source = source[:start] + "function Test-InteractiveDesktop { return $false }\n" + source[end:]
        with TemporaryDirectory() as folder:
            root = Path(folder)
            script = root / "scripts/configure-bailian-gui.ps1"
            script.parent.mkdir()
            script.write_text(source, encoding="utf-8-sig")
            placeholder = root / ".venv-suit/Scripts/python.exe"
            placeholder.parent.mkdir(parents=True)
            placeholder.write_bytes(b"must-never-execute")
            result = subprocess.run(["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(script)],
                                    capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 3, result.stderr)
            self.assertFalse((root / "suit-demo").exists())


if __name__ == "__main__":
    unittest.main()
