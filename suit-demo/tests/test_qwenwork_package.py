"""Portable packaging only: no live API, credentials, model or native client."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

APP = Path(__file__).resolve().parents[1]


class QwenWorkPackageTests(unittest.TestCase):
    def setUp(self):
        (APP / 'tmp').mkdir(exist_ok=True)
        temporary = tempfile.TemporaryDirectory(prefix='qwenwork-package-', dir=APP / 'tmp')
        self.addCleanup(temporary.cleanup)
        self.temp = Path(temporary.name)
        self.workspace = self.temp / '迁移目录 with spaces'
        self.app = self.workspace / 'suit-demo'
        self.qwen = self.app / 'qwenwork'
        (self.qwen / 'zhenjian-research').mkdir(parents=True)
        for name in ('prepare-qwenwork.py', 'prepare-qwenwork.ps1'):
            shutil.copyfile(APP / 'qwenwork' / name, self.qwen / name)
        shutil.copyfile(APP / 'qwenwork/zhenjian-research/SKILL.md', self.qwen / 'zhenjian-research/SKILL.md')
        (self.app / 'interview_mcp.py').write_text('# Synthetic connector path only.\n', encoding='utf-8')
        spec = importlib.util.spec_from_file_location('synthetic_qwenwork_prepare', self.qwen / 'prepare-qwenwork.py')
        self.prepare_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.prepare_module)

    def prepare(self, **kwargs):
        return self.prepare_module.prepare(python_executable=sys.executable, **kwargs)

    def test_relocated_paths_and_port_are_generated_without_services(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('no network')):
            result = self.prepare(port=8877)
        config = json.loads(Path(result['config']).read_text(encoding='utf-8'))
        service = config['mcpServers']['zhenjian-interview']
        self.assertEqual(service['type'], 'stdio')
        self.assertEqual(service['command'], str(Path(sys.executable).resolve()))
        self.assertEqual(service['args'], [str((self.app / 'interview_mcp.py').resolve()), '--base-url', 'http://127.0.0.1:8877'])
        self.assertEqual(service['env'], {'PYTHONUTF8': '1'})
        self.assertNotIn('Authorization', json.dumps(config))
        self.assertEqual(result['package_version'], '1.0.0')
        self.assertEqual(result['native_client_acceptance'], 'not_verified')

    def test_skill_zip_is_reproducible_and_contains_only_skill_source(self):
        # Unrelated files in the checkout must never enter the skill package.
        (self.app / 'interview-data').mkdir()
        (self.app / 'interview-data/private.json').write_text('SYNTHETIC-DO-NOT-PACK', encoding='utf-8')
        (self.qwen / 'zhenjian-research/unrelated.txt').write_text('SYNTHETIC-DO-NOT-PACK', encoding='utf-8')
        first = self.prepare(output_dir=self.temp / 'first')
        second = self.prepare(output_dir=self.temp / 'second')
        data = Path(first['skill_package']).read_bytes()
        self.assertEqual(data, Path(second['skill_package']).read_bytes())
        self.assertEqual(hashlib.sha256(data).hexdigest(), first['skill_sha256'])
        with zipfile.ZipFile(first['skill_package']) as bundle:
            self.assertEqual(bundle.namelist(), ['zhenjian-research/SKILL.md'])
            text = bundle.read('zhenjian-research/SKILL.md').decode('utf-8')
            self.assertTrue(text.startswith('---\nname: zhenjian-research\n'))
            self.assertNotIn('SYNTHETIC-DO-NOT-PACK', text)
        self.assertLess(len(data), 10 * 1024 * 1024)

    def test_manifest_hashes_match_actual_files_and_distinguish_local_config(self):
        result = self.prepare()
        manifest = json.loads(Path(result['manifest']).read_text(encoding='utf-8'))
        self.assertFalse(manifest['contains_credentials'])
        self.assertFalse(manifest['contains_participant_data'])
        self.assertFalse(manifest['contains_model_weights'])
        self.assertTrue(manifest['local_import_requires_regeneration_after_move'])
        self.assertEqual(manifest['runtime_api_config'], 'interview.v4')
        for item in manifest['files']:
            actual = (self.qwen / item['path']).read_bytes()
            self.assertEqual(item['sha256'], hashlib.sha256(actual).hexdigest())
            self.assertEqual(item['bytes'], len(actual))
            self.assertEqual(item['portable'], item['path'].endswith('.zip'))

    def test_invalid_parameters_do_not_create_output(self):
        for port in (0, 65536, True, '8766'):
            output = self.temp / ('bad-' + str(port))
            with self.assertRaises(ValueError):
                self.prepare(output_dir=output, port=port)
            self.assertFalse(output.exists())
        output = self.temp / 'bad-python'
        with self.assertRaises(ValueError):
            self.prepare_module.prepare(output, self.temp / 'missing-python.exe')
        self.assertFalse(output.exists())

    def test_invalid_skill_or_credential_shape_stops_before_output(self):
        skill = self.qwen / 'zhenjian-research/SKILL.md'
        original = skill.read_text(encoding='utf-8')
        for name, content in (
            ('wrong-name', original.replace('name: zhenjian-research', 'name: wrong-name', 1)),
            ('key-shaped', original + '\n' + 'sk-' + 'a' * 40),
        ):
            skill.write_text(content, encoding='utf-8')
            output = self.temp / name
            with self.assertRaises(ValueError):
                self.prepare(output_dir=output)
            self.assertFalse(output.exists())

    def test_missing_connector_does_not_modify_existing_generated_files(self):
        result = self.prepare()
        before = {key: Path(result[key]).read_bytes() for key in ('config', 'skill_package', 'manifest')}
        (self.app / 'interview_mcp.py').unlink()
        with self.assertRaises(ValueError):
            self.prepare()
        for key, content in before.items():
            self.assertEqual(Path(result[key]).read_bytes(), content)

    @unittest.skipUnless(os.name == 'nt', 'Windows wrapper')
    def test_windows_wrapper_uses_its_own_project_from_other_working_directory(self):
        output = self.temp / 'wrapper output'
        powershell = Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        run = subprocess.run([str(powershell), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
            str(self.qwen / 'prepare-qwenwork.ps1'), '-PythonExecutable', sys.executable,
            '-OutputDirectory', str(output), '-Port', '8878'], cwd=self.temp,
            capture_output=True, timeout=30, check=False)
        self.assertEqual(run.returncode, 0, run.stderr.decode('utf-8', errors='replace'))
        config = json.loads((output / 'mcp-import.local.json').read_text(encoding='utf-8'))
        service = config['mcpServers']['zhenjian-interview']
        self.assertEqual(service['args'][0], str((self.app / 'interview_mcp.py').resolve()))
        self.assertEqual(service['args'][-1], 'http://127.0.0.1:8878')
        self.assertTrue((output / 'zhenjian-research.zip').is_file())


if __name__ == '__main__':
    unittest.main()
