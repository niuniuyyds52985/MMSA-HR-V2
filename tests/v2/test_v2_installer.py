"""No network, credentials, installed models or real project writes."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from scripts import install_v2 as installer


class Response(io.BytesIO):
    def geturl(self):
        return 'https://example.test/asset'


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def asset(self, payload=b'good'):
        return {'id': 'example', 'url': 'https://example.test/asset', 'target': '.runtime/asset',
                'sha256': hashlib.sha256(payload).hexdigest(), 'max_bytes': len(payload)}

    def test_existing_verified_file_never_uses_network(self):
        asset = self.asset()
        target = installer.inside(self.root, asset['target'])
        target.parent.mkdir()
        target.write_bytes(b'good')
        opener = Mock()
        self.assertEqual(installer.download_verified(self.root, asset, opener), target)
        opener.open.assert_not_called()

    def test_bad_download_preserves_existing_file_and_removes_partial(self):
        asset = self.asset()
        target = installer.inside(self.root, asset['target'])
        target.parent.mkdir()
        target.write_bytes(b'old')
        opener = Mock()
        opener.open.return_value = Response(b'evil')
        with self.assertRaisesRegex(ValueError, 'checksum'):
            installer.download_verified(self.root, asset, opener)
        self.assertEqual(target.read_bytes(), b'old')
        self.assertFalse(target.with_name('asset.download').exists())

    def test_size_bound_stops_download(self):
        opener = Mock()
        opener.open.return_value = Response(b'too large')
        with self.assertRaisesRegex(ValueError, 'size limit'):
            installer.download_verified(self.root, self.asset(), opener)
        self.assertFalse((self.root / '.runtime/asset').exists())

    def test_escape_and_windows_drives_rejected(self):
        for path in ('../secret', '..\\secret', 'C:\\Windows\\x', '/x', 'dir/../../x'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                installer.inside(self.root, path)

    def test_zip_slip_rejected(self):
        archive = self.root / 'archive.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('upstream/../../escape', 'bad')
        asset = {**self.asset(), 'extract_to': 'tools', 'strip_prefix': 'upstream', 'source_target': 'source.json'}
        with self.assertRaises(ValueError):
            installer.extract_verified(self.root, asset, archive)

    def test_missing_extracted_member_does_not_write_success_manifest(self):
        archive = self.root / 'archive.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('upstream/file', 'good')
        asset = {**self.asset(), 'extract_to': 'tools', 'strip_prefix': 'upstream', 'source_target': 'source.json',
                 'source_record': {'files': {'file': hashlib.sha256(b'good').hexdigest(), 'missing': '0' * 64}}}
        with self.assertRaisesRegex(ValueError, 'missing required'):
            installer.extract_verified(self.root, asset, archive)
        self.assertFalse((self.root / 'source.json').exists())

    def test_verified_archive_files_detect_corruption(self):
        archive = self.root / 'archive.zip'
        with zipfile.ZipFile(archive, 'w') as output:
            output.writestr('upstream/bin/a.exe', 'good')
        asset = {**self.asset(), 'extract_to': 'tools', 'strip_prefix': 'upstream', 'source_target': 'source.json'}
        installer.extract_verified(self.root, asset, archive)
        self.assertTrue(installer.verify_extracted(self.root, asset))
        (self.root / 'tools/bin/a.exe').write_text('broken')
        self.assertFalse(installer.verify_extracted(self.root, asset))

    def test_doctor_missing_environment_is_read_only_and_incomplete(self):
        with patch.object(installer, 'run', side_effect=FileNotFoundError):
            report = installer.doctor(self.root, {'assets': [self.asset()], 'torch': '2.14.0+cpu'})
        self.assertEqual(report['status'], 'incomplete')
        self.assertEqual(list(self.root.iterdir()), [])
        self.assertIn('Qwen host installation', report['not_checked'])

    def test_shipped_manifest_uses_pins_and_has_required_assets(self):
        manifest = installer.load_manifest(installer.PROJECT)
        ids = {a['id'] for a in manifest['assets']}
        self.assertTrue({'openface-2.2.0', 'ffmpeg-8.1.2', 'whisper-base', 'asr-model.bin',
                         'speech-model.pt', 'face_detection_yunet_2023mar.onnx'} <= ids)
        self.assertTrue(all('/main/' not in a['url'] or 'whisper/models/' in a['url'] for a in manifest['assets']))

    def test_openface_probe_uses_clnf_and_absent_input_not_camera(self):
        response = Mock(stdout='Reading the AU predictors ... Failed to open the video file')
        with patch.object(installer, 'run', return_value=response) as run:
            installer.probe_openface(self.root)
        args = run.call_args.args[0]
        self.assertIn('model/main_clnf_general.txt', args)
        self.assertIn('-f', args)
        self.assertNotIn('-device', args)
        self.assertTrue(args[args.index('-f') + 1].startswith('install-probe-'))

    def test_openface_incomplete_initialization_is_not_ready(self):
        with patch.object(installer, 'run', return_value=Mock(stdout='Could not load the landmark detector')):
            with self.assertRaises(RuntimeError):
                installer.probe_openface(self.root)

    def test_optional_qwen_failure_does_not_fail_a_ready_local_install(self):
        with patch.object(installer, 'load_manifest', return_value={'assets': []}), \
             patch.object(installer, 'install_dependencies'), \
             patch.object(installer, 'install_assets'), \
             patch.object(installer, 'run', side_effect=FileNotFoundError), \
             patch.object(installer, 'doctor', return_value={'status': 'ready'}):
            code = installer.main(['--project-root', str(self.root)])
        self.assertEqual(code, 0)
        report = json.loads((self.root / '.runtime/install-v2-report.json').read_text())
        self.assertEqual(report['status'], 'ready')
        self.assertEqual(report['qwen_import_preparation']['status'], 'failed')


if __name__ == '__main__':
    unittest.main()
