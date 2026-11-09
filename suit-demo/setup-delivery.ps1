param([string]$Python = 'python', [switch]$DownloadModels, [switch]$DownloadOriginalAuNet, [switch]$DownloadGaze360, [switch]$DownloadEmotic, [switch]$DownloadFastAsr)
$ErrorActionPreference = 'Stop'
$projectDir = $PSScriptRoot
$workspaceDir = Split-Path $projectDir -Parent
$venvDir = Join-Path $workspaceDir '.venv-suit'
$pythonExe = Join-Path $venvDir 'Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    & $Python -c "import sys; assert sys.version_info[:2] == (3,12), 'Use Python 3.12 x64 for this Windows delivery'"
    if ($LASTEXITCODE -ne 0) { throw 'Python 3.12 is required.' }
    & $Python -m venv $venvDir
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the project Python environment.' }
}
& $pythonExe -m pip install --upgrade pip
if ($LASTEXITCODE -ne 0) { throw 'pip preparation failed.' }
& $pythonExe -m pip install 'torch==2.14.0+cpu' --index-url 'https://download.pytorch.org/whl/cpu'
if ($LASTEXITCODE -ne 0) { throw 'PyTorch CPU installation failed.' }
& $pythonExe -m pip install -r (Join-Path $projectDir 'requirements-interview.txt')
if ($LASTEXITCODE -ne 0) { throw 'Interview dependencies installation failed.' }
& (Join-Path $projectDir 'configure-interview.ps1') -PrepareOnly
if ($DownloadModels) {
    & $pythonExe -c "import sys,whisper; whisper.load_model('base', device='cpu', download_root=sys.argv[1])" (Join-Path $projectDir 'models')
    if ($LASTEXITCODE -ne 0) { throw 'Whisper base setup failed.' }
    & $pythonExe (Join-Path $projectDir 'fetch_visual_model.py')
    if ($LASTEXITCODE -ne 0) { throw 'YuNet setup failed.' }
    & $pythonExe (Join-Path $projectDir 'fetch_research_models.py')
    if ($LASTEXITCODE -ne 0) { throw 'Research asset setup failed.' }
}
if ($DownloadFastAsr) {
    & $pythonExe (Join-Path $projectDir 'fetch_asr.py')
    if ($LASTEXITCODE -ne 0) { throw 'Fast local ASR asset setup failed.' }
}
if ($DownloadOriginalAuNet) {
    & $pythonExe (Join-Path $projectDir 'fetch_original_au_net.py')
    if ($LASTEXITCODE -ne 0) { throw 'Optional original AU-Net asset setup failed.' }
}
if ($DownloadGaze360) {
    & $pythonExe (Join-Path $projectDir 'fetch_gaze360.py')
    if ($LASTEXITCODE -ne 0) { throw 'Optional Gaze360 asset setup failed. See its source and download status.' }
}
if ($DownloadEmotic) {
    & $pythonExe (Join-Path $projectDir 'fetch_emotic.py')
    if ($LASTEXITCODE -ne 0) { throw 'Optional EMOTIC asset setup failed. See its source and download status.' }
}
& $pythonExe (Join-Path $projectDir 'qwenwork\prepare-qwenwork.py')
if ($LASTEXITCODE -ne 0) { throw 'QwenWork import package generation failed.' }
& $pythonExe (Join-Path $projectDir 'delivery_tools.py') doctor
Write-Output 'Setup completed. Check doctor output, install FFmpeg/ffprobe in PATH, then configure the API locally if required.'
