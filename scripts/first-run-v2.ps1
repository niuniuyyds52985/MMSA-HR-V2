param([switch]$Plan, [switch]$SkipInstall)
$ErrorActionPreference = 'Stop'

function Invoke-V2SetupStage {
    param([string]$Root, [ValidateSet('Install','Doctor')][string]$Mode, [string]$Log)
    $previousErrorPreference = $ErrorActionPreference
    try {
        # Windows PowerShell can turn a harmless native stderr warning into a
        # terminating error. Installation success is determined by its exit code.
        $ErrorActionPreference = 'Continue'
        & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $Root 'scripts\install-v2.ps1') -Mode $Mode *> $Log
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousErrorPreference }
}

function Invoke-V2QwenPreparation {
    param([string]$Root, [string]$Log)
    & (Join-Path $Root '.venv-suit\Scripts\python.exe') -B -X utf8 (Join-Path $Root 'scripts\prepare_qwenwork_v2.py') --project-root $Root *> $Log
    return $LASTEXITCODE
}

function Invoke-V2CredentialDialog {
    param([string]$Root)
    # The child opens a local masked input window. No API value is passed as an
    # argument, written to setup logs or returned to the parent conversation.
    & powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File (Join-Path $Root 'scripts\configure-bailian-gui.ps1') | Out-Host
    return $LASTEXITCODE
}

function Open-V2HrWorkbench {
    param([string]$Root)
    Push-Location -LiteralPath $Root
    try {
        & (Join-Path $Root '.venv-suit\Scripts\python.exe') -B -X utf8 -m src.mmsa_v2.launch --open | Out-Host
        return $LASTEXITCODE
    } finally { Pop-Location }
}

function Invoke-V2FirstRun {
    param([string]$Root, [switch]$Plan, [switch]$SkipInstall)
    if ($Plan) {
        [ordered]@{mode='plan'; install_if_missing=(-not $SkipInstall.IsPresent);
            steps=@('Check local runtime and verified assets',
                    'If required, install Python/dependencies/models with pinned checksums',
                    'Generate local Qwen Skill and MCP import files (no host registration)',
                    'Request API key in a local masked window only when not configured',
                    'Start or reuse the local service and open HR control panel');
            writes_performed=$false; network_used=$false; opens_credentials_window=$false;
            launches_service=$false} | ConvertTo-Json -Depth 4 | Out-Host
        return 0
    }
    $runtime = Join-Path $Root '.runtime'
    New-Item -ItemType Directory -Path $runtime -Force | Out-Null
    $doctorLog = Join-Path $runtime 'first-run-doctor.log'
    Write-Host '[1/4] Checking the local installation...'
    $status = Invoke-V2SetupStage -Root $Root -Mode Doctor -Log $doctorLog
    if ($status -ne 0) {
        if ($SkipInstall) {
            Write-Host ('Installation is incomplete. Run again without -SkipInstall. Details: ' + $doctorLog)
            return 1
        }
        $installLog = Join-Path $runtime 'first-run-install.log'
        Write-Host ('[1/4] Installing required components. First download may take a while. Log: ' + $installLog)
        $status = Invoke-V2SetupStage -Root $Root -Mode Install -Log $installLog
        if ($status -ne 0) {
            Write-Host ('Installation did not complete. Existing interview data is preserved. Details: ' + $installLog)
            return 1
        }
    }
    Write-Host '[2/4] Preparing local Qwen import files...'
    $qwenLog = Join-Path $runtime 'first-run-qwen.log'
    try { $qwenStatus = Invoke-V2QwenPreparation -Root $Root -Log $qwenLog }
    catch { $qwenStatus = 1 }
    if ($qwenStatus -ne 0) {
        Write-Host ('Optional Qwen import files could not be generated; local setup will continue. Retry later. Details: ' + $qwenLog)
    }
    Write-Host '[3/4] Checking local API configuration (a masked input window may open)...'
    $credentialStatus = Invoke-V2CredentialDialog -Root $Root
    if ($credentialStatus -eq 2) {
        Write-Host 'API configuration was cancelled. Run this entry again when ready; installed components are retained.'
        return 2
    }
    if ($credentialStatus -ne 0) {
        Write-Host 'Local API configuration failed. The HR panel was not opened.'
        return 1
    }
    Write-Host '[4/4] Opening the HR control panel...'
    return (Open-V2HrWorkbench -Root $Root)
}

if ($MyInvocation.InvocationName -eq '.') { return }
try { exit (Invoke-V2FirstRun -Root (Split-Path $PSScriptRoot -Parent) -Plan:$Plan -SkipInstall:$SkipInstall) }
catch { Write-Error $_; exit 1 }
