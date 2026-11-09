param([switch]$BootstrapRecord)
$ErrorActionPreference = 'Stop'
$pythonExe = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv-suit\Scripts\python.exe'
$runtimeTool = Join-Path $PSScriptRoot 'interview_runtime.py'
$env:PYTHONUTF8 = '1'
$arguments = @($runtimeTool, 'stop')
if ($BootstrapRecord) { $arguments += '--legacy-record' }
& $pythonExe @arguments
if ($LASTEXITCODE -ne 0) { throw 'Managed stop was refused or incomplete. No unrelated process was selected.' }
Write-Output 'Verified project processes stopped. Data, models, credentials and Qwen/MCP processes were not removed.'
