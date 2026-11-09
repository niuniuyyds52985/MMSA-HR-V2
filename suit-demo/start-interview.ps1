param([ValidateRange(1,65535)][int]$Port = 8766, [switch]$OpenBrowser, [string]$MaintenanceOwner = '')
$ErrorActionPreference = 'Stop'
$projectDir = [IO.Path]::GetFullPath($PSScriptRoot)
$pythonExe = Join-Path (Split-Path $projectDir -Parent) '.venv-suit\Scripts\python.exe'
$runtimeDir = Join-Path $projectDir 'interview-data'
$runtimeTool = Join-Path $projectDir 'interview_runtime.py'
if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw "Missing Python environment: $pythonExe" }
$maintenanceFile = Join-Path $runtimeDir 'maintenance.json'
if (Test-Path -LiteralPath $maintenanceFile) {
    $maintenance = Get-Content -LiteralPath $maintenanceFile -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $MaintenanceOwner -or $maintenance.owner -ne $MaintenanceOwner) { throw 'Maintenance is active. Use the updater that owns this marker.' }
} elseif ($MaintenanceOwner) { throw 'The updater maintenance marker is missing.' }
New-Item -ItemType Directory -Path $runtimeDir -Force | Out-Null
$env:PYTHONUTF8 = '1'
$env:INTERVIEW_DATA_DIR = $runtimeDir
$env:OMP_NUM_THREADS = '4'
$env:TEMP = $runtimeDir
$env:TMP = $runtimeDir
$localUrl = "http://127.0.0.1:$Port"
$identityOutput = & $pythonExe $runtimeTool build
if ($LASTEXITCODE -ne 0) { throw 'Cannot calculate the local backend build identity.' }
$identity = $identityOutput | ConvertFrom-Json
$ffmpegCommand = Get-Command ffmpeg -ErrorAction SilentlyContinue
if ($ffmpegCommand) {
    $ffmpegItem = Get-Item -LiteralPath $ffmpegCommand.Source
    $ffmpegActual = if ($ffmpegItem.Target) { $ffmpegItem.Target } else { $ffmpegCommand.Source }
    $env:PATH = (Split-Path $ffmpegActual -Parent) + ';' + $env:PATH
} else { Write-Warning 'FFmpeg unavailable. Media submission will report an error until configured.' }
$healthy = $false
try {
    $health = Invoke-RestMethod "$localUrl/health" -TimeoutSec 2
    if ($health.service -eq 'zhenjian-interview') { $healthy = $true }
    else { throw 'The requested port belongs to another service.' }
} catch {
    if (Get-NetTCPConnection -State Listen -ErrorAction Stop | Where-Object LocalPort -eq $Port) {
        throw "Port $Port is occupied by an unverified service."
    }
}
if ($healthy) {
    if ($health.config_version -ne 'interview.v4' -or $health.backend_build_id -ne $identity.backend_build_id) {
        throw 'Running backend differs from local code. Use update-interview.ps1 -Action status, then -Action apply; legacy runtime needs explicit bootstrap.'
    }
    & $pythonExe $runtimeTool verify
    if ($LASTEXITCODE -ne 0) { throw 'Managed process identities are incomplete. Use the controlled updater; no duplicate workers were started.' }
} else {
    & $pythonExe $runtimeTool assert-stopped --port $Port
    if ($LASTEXITCODE -ne 0) { throw 'Existing project processes must be resolved before starting another runtime.' }
    $record = @{ project = $projectDir; port = $Port; workflow_version = 'interview.v4'; product_version = $identity.product_version; backend_build_id = $identity.backend_build_id }
    $pidFile = Join-Path $runtimeDir 'processes.json'
    function Save-ProcessRecord {
        $temporaryRecord = Join-Path $runtimeDir 'processes.json.tmp'
        $record | ConvertTo-Json | Set-Content -LiteralPath $temporaryRecord -Encoding UTF8
        Move-Item -LiteralPath $temporaryRecord -Destination $pidFile -Force
    }
    function Register-Process([string]$Role, [object]$Process) {
        $item = Get-CimInstance Win32_Process -Filter "ProcessId = $($Process.Id)" -ErrorAction Stop
        if (-not $item) { throw "Managed process exited before registration: $Role" }
        $record[$Role + '_pid'] = [int]$Process.Id
        $record[$Role + '_created_at'] = $item.CreationDate.ToUniversalTime().ToString('o')
        Save-ProcessRecord
    }
    $server = Start-Process -FilePath $pythonExe -ArgumentList '-m','uvicorn','interview_entry:app','--app-dir',('"' + $projectDir + '"'),'--host','127.0.0.1','--port',$Port -WorkingDirectory $projectDir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimeDir 'server.stdout.log') -RedirectStandardError (Join-Path $runtimeDir 'server.stderr.log')
    Register-Process 'server' $server
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $server.Refresh()
        if ($server.HasExited) { throw 'Interview service exited. See interview-data/server.stderr.log.' }
        try {
            $health = Invoke-RestMethod "$localUrl/health" -TimeoutSec 1
            if ($health.service -eq 'zhenjian-interview' -and $health.backend_build_id -eq $identity.backend_build_id) { $healthy = $true; break }
        } catch {}
        Start-Sleep -Milliseconds 500
    }
    if (-not $healthy) { throw 'Interview service did not confirm the expected build. Runtime logs and partial process record are retained.' }
    function Start-InterviewWorker([string]$Role, [string]$ScriptName, [string]$Lane, [string]$LogName) {
        $workerScript = Join-Path $projectDir $ScriptName
        $created = Start-Process -FilePath $pythonExe -ArgumentList ('"' + $workerScript + '"'),'--lane',$Lane -WorkingDirectory $projectDir -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $runtimeDir "$LogName.stdout.log") -RedirectStandardError (Join-Path $runtimeDir "$LogName.stderr.log")
        Register-Process $Role $created
    }
    Start-InterviewWorker 'worker' 'interview_worker.py' 'media' 'worker'
    Start-InterviewWorker 'content_worker' 'interview_worker.py' 'content' 'content'
    Start-InterviewWorker 'content_worker2' 'interview_worker.py' 'content' 'content2'
    Start-InterviewWorker 'routing_worker' 'interview_worker.py' 'routing' 'routing'
    Start-InterviewWorker 'research_worker' 'interview_research_worker.py' 'core' 'research'
    Start-InterviewWorker 'extended_worker' 'interview_research_worker.py' 'extended' 'extended'
    & $pythonExe $runtimeTool verify
    if ($LASTEXITCODE -ne 0) { throw 'Started runtime did not pass process identity checks. Logs and process record are retained.' }
}
Write-Output "Interview workbench: $localUrl/workbench"
Write-Output "Backend build: $($identity.backend_build_id)"
if ($OpenBrowser) {
    & $pythonExe (Join-Path $projectDir 'interview_workbench.py') open --base-url $localUrl
    if ($LASTEXITCODE -ne 0) { throw 'Cannot open the protected workbench. Check the local Windows account and secret directory permissions.' }
}
