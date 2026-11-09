param(
    [ValidateSet('status','apply')][string]$Action = 'status',
    [ValidateRange(1,65535)][int]$Port = 8766,
    [ValidateRange(0,600)][int]$WaitSeconds = 0,
    [switch]$Bootstrap
)
$ErrorActionPreference = 'Stop'
$pythonExe = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv-suit\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw 'Existing project Python is required. This command does not install dependencies.' }
$env:PYTHONUTF8 = '1'
$arguments = @((Join-Path $PSScriptRoot 'update-interview.py'), $Action, '--port', [string]$Port, '--wait-seconds', [string]$WaitSeconds)
if ($Bootstrap) { $arguments += '--bootstrap' }
& $pythonExe @arguments
exit $LASTEXITCODE
