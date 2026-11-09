param(
    [ValidateRange(1, 65535)][int]$Port = 8766,
    [string]$PythonExecutable = '',
    [string]$OutputDirectory = ''
)
$ErrorActionPreference = 'Stop'
$qwenworkDirectory = [System.IO.Path]::GetFullPath($PSScriptRoot)
$applicationDirectory = [System.IO.Path]::GetDirectoryName($qwenworkDirectory)
$workspaceDirectory = [System.IO.Path]::GetDirectoryName($applicationDirectory)
if ([string]::IsNullOrWhiteSpace($PythonExecutable)) {
    $PythonExecutable = Join-Path $workspaceDirectory '.venv-suit\Scripts\python.exe'
}
if (-not (Test-Path -LiteralPath $PythonExecutable -PathType Leaf)) {
    throw 'Project Python is missing. Run suit-demo/setup-delivery.ps1 first, or pass -PythonExecutable.'
}
$prepareScript = Join-Path $qwenworkDirectory 'prepare-qwenwork.py'
$prepareArguments = @($prepareScript, '--port', [string]$Port)
if (-not [string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $prepareArguments += @('--output-dir', $OutputDirectory)
}
& $PythonExecutable @prepareArguments
if ($LASTEXITCODE -ne 0) { throw 'QwenWork package preparation failed.' }
Write-Output 'Prepared local MCP JSON, portable skill ZIP, and checksum manifest.'
Write-Output 'Keep the workbench running under the same Windows account. Import the local JSON in QwenWork, enable it, then open a new task.'
