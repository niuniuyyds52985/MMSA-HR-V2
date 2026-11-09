"""Exercise PowerShell control flow with mocked installation, GUI and launch.

No network, API key, user credential store, browser or running service is used.
"""
import base64
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(shutil.which('powershell.exe'), 'Windows PowerShell required')
class FirstRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def ps(self, script):
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                                 '-EncodedCommand', encoded], capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        stdout = result.stdout.decode('utf-8', errors='replace')
        return json.loads(next(line[7:] for line in stdout.splitlines() if line.startswith('RESULT:')))

    def first_run(self, doctor=0, install=0, credential=0, qwen=0, flags='', qwen_throws=False):
        qwen_action = "throw 'fixture preparation failure'" if qwen_throws else f'return {qwen}'
        return self.ps(f"""
. {quote(ROOT / 'scripts/first-run-v2.ps1')}
$script:steps = [System.Collections.Generic.List[string]]::new()
function Invoke-V2SetupStage {{ param($Root,$Mode,$Log); $script:steps.Add($Mode); if ($Mode -eq 'Doctor') {{return {doctor}}}; return {install} }}
function Invoke-V2QwenPreparation {{ param($Root,$Log); $script:steps.Add('Qwen'); {qwen_action} }}
function Invoke-V2CredentialDialog {{ param($Root); $script:steps.Add('Credentials'); return {credential} }}
function Open-V2HrWorkbench {{ param($Root); $script:steps.Add('OpenHR'); return 0 }}
$code = Invoke-V2FirstRun -Root {quote(self.root)} {flags}
Write-Output ('RESULT:' + (@{{code=$code;steps=@($script:steps)}} | ConvertTo-Json -Compress))
""")

    def test_ready_machine_skips_install_and_opens_hr_after_credentials(self):
        self.assertEqual(self.first_run(), {'code': 0, 'steps': ['Doctor', 'Qwen', 'Credentials', 'OpenHR']})

    def test_first_install_runs_once_before_local_credentials_and_hr(self):
        self.assertEqual(self.first_run(doctor=2),
                         {'code': 0, 'steps': ['Doctor', 'Install', 'Qwen', 'Credentials', 'OpenHR']})

    def test_cancelled_credentials_never_open_hr(self):
        self.assertEqual(self.first_run(credential=2), {'code': 2, 'steps': ['Doctor', 'Qwen', 'Credentials']})

    def test_install_failure_never_reaches_credentials(self):
        self.assertEqual(self.first_run(doctor=1, install=1), {'code': 1, 'steps': ['Doctor', 'Install']})

    def test_skip_install_fails_without_bootstrap(self):
        self.assertEqual(self.first_run(doctor=2, flags='-SkipInstall'), {'code': 1, 'steps': ['Doctor']})

    def test_optional_qwen_prepare_failure_still_allows_local_hr(self):
        self.assertEqual(self.first_run(qwen=1), {'code': 0, 'steps': ['Doctor', 'Qwen', 'Credentials', 'OpenHR']})

    def test_optional_qwen_exception_still_allows_local_hr(self):
        self.assertEqual(self.first_run(qwen_throws=True), {'code': 0, 'steps': ['Doctor', 'Qwen', 'Credentials', 'OpenHR']})

    def test_plan_has_no_side_effects(self):
        self.assertEqual(self.first_run(flags='-Plan'), {'code': 0, 'steps': []})
        self.assertEqual(list(self.root.iterdir()), [])

    def test_native_warning_is_logged_but_exit_code_controls_setup_result(self):
        scripts = self.root / 'scripts'
        scripts.mkdir()
        (scripts / 'install-v2.ps1').write_text("param($Mode)\n[Console]::Error.WriteLine('fixture warning')\nexit 0\n")
        log = self.root / 'setup.log'
        result = self.ps(f"""
. {quote(ROOT / 'scripts/first-run-v2.ps1')}
$code = Invoke-V2SetupStage -Root {quote(self.root)} -Mode Doctor -Log {quote(log)}
Write-Output ('RESULT:' + (@{{code=$code}} | ConvertTo-Json -Compress))
""")
        self.assertEqual(result['code'], 0)
        self.assertTrue(log.is_file())

    def test_no_python_plan_and_doctor_never_download(self):
        (self.root / 'config').mkdir()
        (self.root / 'config/v2-install-assets.json').write_text(json.dumps({'python_runtime': {'version': '3.12.10'}}))
        result = self.ps(f"""
. {quote(ROOT / 'scripts/install-v2.ps1')}
function Find-V2Python {{ return $null }}
function Install-V2Python {{ throw 'MUST NOT INSTALL' }}
$plan = Invoke-V2InstallEntry -Root {quote(self.root)} -RequestedMode Plan
$doctor = Invoke-V2InstallEntry -Root {quote(self.root)} -RequestedMode Doctor
Write-Output ('RESULT:' + (@{{plan=$plan;doctor=$doctor}} | ConvertTo-Json -Compress))
""")
        self.assertEqual(result, {'plan': 0, 'doctor': 2})
        self.assertFalse((self.root / '.runtime').exists())

    def test_bad_python_checksum_rejected_before_signature_or_execution(self):
        artifact = self.root / 'python.exe'
        artifact.write_bytes(b'bad')
        result = self.ps(f"""
. {quote(ROOT / 'scripts/install-v2.ps1')}
function Get-AuthenticodeSignature {{ throw 'SIGNATURE CHECK SHOULD NOT BE REACHED' }}
$ok = $false
try {{ Assert-V2PythonInstaller -Path {quote(artifact)} -Asset @{{sha256=('0'*64)}} }}
catch {{ $ok = $_.Exception.Message -like '*checksum mismatch*' }}
Write-Output ('RESULT:' + (@{{rejected=$ok}} | ConvertTo-Json -Compress))
""")
        self.assertTrue(result['rejected'])

    def test_bootstrap_has_per_user_no_path_no_restart_flags(self):
        path = self.root / '.runtime/downloads/python-3.12.10-amd64.exe'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'fixture')
        sha = hashlib.sha256(b'fixture').hexdigest()
        result = self.ps(f"""
. {quote(ROOT / 'scripts/install-v2.ps1')}
$script:arguments = @()
function Assert-V2PythonInstaller {{ param($Path,$Asset); if ((Get-V2Sha256 $Path) -ne $Asset.sha256) {{throw 'bad fixture'}} }}
function Invoke-WebRequest {{ throw 'CACHED ASSET MUST NOT DOWNLOAD' }}
function Find-V2Python {{ param($Root,$Preferred); return [PSCustomObject]@{{Executable=$Preferred;Prefix=@()}} }}
function Start-Process {{ param($FilePath,$ArgumentList,[switch]$Wait,[switch]$PassThru,$WindowStyle); $script:arguments=$ArgumentList; return @{{ExitCode=0}} }}
$asset=@{{url='https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe';sha256='{sha}'}}
$resolved=Install-V2Python -Root {quote(self.root)} -Asset $asset
Write-Output ('RESULT:' + (@{{arguments=$script:arguments;python=$resolved.Executable}} | ConvertTo-Json -Compress))
""")
        for expected in ['/quiet', '/norestart', 'InstallAllUsers=0', 'PrependPath=0',
                         'AppendPath=0', 'Include_launcher=0', 'AssociateFiles=0', 'Shortcuts=0']:
            self.assertIn(expected, result['arguments'])
        self.assertEqual(Path(result['python']), self.root / '.runtime/python312/python.exe')

    def test_python_signature_requires_valid_status_and_psf_publisher(self):
        artifact = self.root / 'fixture.exe'
        artifact.write_bytes(b'fixture')
        sha = hashlib.sha256(b'fixture').hexdigest()
        result = self.ps(f"""
. {quote(ROOT / 'scripts/install-v2.ps1')}
$script:status='NotSigned'
$script:publisher='Python Software Foundation'
function Get-AuthenticodeSignature {{
    $certificate = [PSCustomObject]@{{}}
    $certificate | Add-Member -MemberType ScriptMethod -Name GetNameInfo -Value {{param($type,$issuer); return $script:publisher}}
    return [PSCustomObject]@{{Status=$script:status;SignerCertificate=$certificate}}
}}
$asset=@{{sha256='{sha}';publisher='Python Software Foundation'}}
$unsigned=$false; $wrongPublisher=$false; $accepted=$false
try {{Assert-V2PythonInstaller -Path {quote(artifact)} -Asset $asset}} catch {{$unsigned=$true}}
$script:status='Valid'; $script:publisher='Other Vendor'
try {{Assert-V2PythonInstaller -Path {quote(artifact)} -Asset $asset}} catch {{$wrongPublisher=$true}}
$script:publisher='Python Software Foundation'
try {{Assert-V2PythonInstaller -Path {quote(artifact)} -Asset $asset; $accepted=$true}} catch {{}}
Write-Output ('RESULT:' + (@{{unsigned=$unsigned;wrongPublisher=$wrongPublisher;accepted=$accepted}} | ConvertTo-Json -Compress))
""")
        self.assertEqual(result, {'unsigned': True, 'wrongPublisher': True, 'accepted': True})

    def test_official_python_sigstore_digest_is_pinned_separately(self):
        manifest = json.loads((ROOT / 'config/v2-install-assets.json').read_text(encoding='utf-8'))
        runtime = manifest['python_runtime']
        self.assertEqual(runtime['sha256'], base64.b64decode('Z7VjXoDqUQcrh5QTEtAOyJJ8Tbm6GJOPetLSezKLlfs=').hex())
        self.assertEqual(runtime['publisher'], 'Python Software Foundation')
        self.assertFalse(any('python-3.12.10' in a['id'] for a in manifest['assets']))


if __name__ == '__main__':
    unittest.main()
