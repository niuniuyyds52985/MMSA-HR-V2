param(
    [ValidateSet('Install','Plan','Doctor')][string]$Mode = 'Install',
    [string]$Python = ''
)
$ErrorActionPreference = 'Stop'

function Find-V2Python {
    param([string]$Root, [string]$Preferred = '')
    $candidates = @()
    if ($Preferred) { $candidates += ,@($Preferred) }
    foreach ($relative in @('.venv-suit\Scripts\python.exe', '.runtime\python312\python.exe')) {
        $existing = Join-Path $Root $relative
        if (Test-Path -LiteralPath $existing) { $candidates += ,@($existing) }
    }
    # A normal per-user Python installation can exist without PATH or launcher.
    # Reuse it before attempting another installation of the same product.
    foreach ($hive in @([Microsoft.Win32.Registry]::CurrentUser, [Microsoft.Win32.Registry]::LocalMachine)) {
        $key = $null
        try {
            $key = $hive.OpenSubKey('Software\Python\PythonCore\3.12\InstallPath')
            if ($null -ne $key) {
                $registered = [string]$key.GetValue('ExecutablePath')
                if (-not $registered -and $key.GetValue('')) { $registered = Join-Path ([string]$key.GetValue('')) 'python.exe' }
                if ($registered -and (Test-Path -LiteralPath $registered)) { $candidates += ,@($registered) }
            }
        } catch { } finally { if ($null -ne $key) { $key.Dispose() } }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) { $candidates += ,@('py','-3.12') }
    if (Get-Command python -ErrorAction SilentlyContinue) { $candidates += ,@('python') }
    foreach ($candidate in $candidates) {
        $command = $candidate[0]
        $prefix = @($candidate | Select-Object -Skip 1)
        try {
            & $command @prefix -B -c "import sys,struct; sys.exit(0 if sys.version_info[:2]==(3,12) and struct.calcsize('P')==8 else 1)" *> $null
            if ($LASTEXITCODE -eq 0) { return [PSCustomObject]@{Executable=$command; Prefix=$prefix} }
        } catch { }
    }
    return $null
}

function Get-V2Sha256 {
    param([string]$Path)
    $stream = [System.IO.File]::OpenRead($Path)
    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $algorithm.Dispose(); $stream.Dispose() }
}

function Assert-V2PythonInstaller {
    param([string]$Path, $Asset)
    $actual = Get-V2Sha256 -Path $Path
    if ($actual -ne $Asset.sha256) { throw 'Python installer checksum mismatch. Nothing was executed.' }
    # A host launched from PowerShell 7/Python may inherit a different module
    # search path. Load the running Windows PowerShell's own signature provider.
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Security\Microsoft.PowerShell.Security.psd1') -ErrorAction Stop
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) {
        throw 'Python installer Authenticode validation failed. Nothing was executed.'
    }
    $publisher = $signature.SignerCertificate.GetNameInfo([System.Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
    if ($publisher -ne $Asset.publisher -or $publisher -ne 'Python Software Foundation') {
        throw 'Python installer publisher mismatch. Nothing was executed.'
    }
}

function Install-V2Python {
    param([string]$Root, $Asset)
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'Windows x64 is required.' }
    if ($Asset.url -ne 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -or
        $Asset.sha256 -notmatch '^[a-f0-9]{64}$') { throw 'Invalid pinned Python installer manifest.' }
    $installer = Join-Path $Root '.runtime\downloads\python-3.12.10-amd64.exe'
    $target = Join-Path $Root '.runtime\python312'
    New-Item -ItemType Directory -Path (Split-Path $installer -Parent) -Force | Out-Null
    $cached = (Test-Path -LiteralPath $installer) -and ((Get-V2Sha256 -Path $installer) -eq $Asset.sha256)
    if (-not $cached) {
        $temporary = $installer + '.download'
        try {
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
            $previousProgress = $ProgressPreference
            $ProgressPreference = 'SilentlyContinue'
            try {
                Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
                Invoke-WebRequest -Uri $Asset.url -OutFile $temporary -UseBasicParsing -TimeoutSec 180
            }
            finally { $ProgressPreference = $previousProgress }
            Assert-V2PythonInstaller -Path $temporary -Asset $Asset
            Move-Item -LiteralPath $temporary -Destination $installer -Force
        } finally {
            if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
        }
    }
    Assert-V2PythonInstaller -Path $installer -Asset $Asset
    Write-Host 'Preparing private Python 3.12 x64 (no administrator, PATH changes or restart).'
    $arguments = @('/quiet','/norestart','InstallAllUsers=0',('TargetDir="' + $target + '"'),
                   'PrependPath=0','AppendPath=0','Include_launcher=0','InstallLauncherAllUsers=0',
                   'AssociateFiles=0','Shortcuts=0','Include_test=0','Include_doc=0','Include_dev=0',
                   'Include_exe=1','Include_lib=1','Include_pip=1')
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -notin @(0,3010)) { throw ('Python installation failed: ' + $process.ExitCode) }
    $python = Join-Path $target 'python.exe'
    $found = Find-V2Python -Root $Root -Preferred $python
    if ($null -eq $found) { throw 'Python installation did not produce a usable Python 3.12 x64. Retry after checking the installation.' }
    return $found
}

function Invoke-V2InstallEntry {
    param([string]$Root, [string]$RequestedMode, [string]$Preferred = '')
    $manifest = Get-Content -LiteralPath (Join-Path $Root 'config\v2-install-assets.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $pythonCommand = Find-V2Python -Root $Root -Preferred $Preferred
    if ($RequestedMode -eq 'Plan' -and $null -eq $pythonCommand) {
        [ordered]@{mode='plan'; python_present=$false; python_runtime=$manifest.python_runtime;
            steps=@('Download Python only during Install; verify SHA-256 and PSF signature',
                    'Prepare project dependencies and models','Generate Qwen import files','Run doctor');
            writes_performed=$false; network_used=$false} | ConvertTo-Json -Depth 8 | Out-Host
        return 0
    }
    if ($null -eq $pythonCommand -and $RequestedMode -eq 'Doctor') {
        Write-Host 'Python 3.12 x64 is missing. Doctor did not download or install anything.'
        return 2
    }
    if ($null -eq $pythonCommand) {
        Write-Host 'Python is missing; downloading the pinned official installer.'
        $pythonCommand = Install-V2Python -Root $Root -Asset $manifest.python_runtime
    }
    $command = $pythonCommand.Executable
    $prefix = @($pythonCommand.Prefix)
    $flags = @()
    if ($RequestedMode -eq 'Plan') { $flags += '--plan' }
    if ($RequestedMode -eq 'Doctor') { $flags += '--doctor' }
    $previousErrorPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $command @prefix -B -X utf8 (Join-Path $Root 'scripts\install_v2.py') @flags 2>&1 | Out-Host
        return $LASTEXITCODE
    } finally { $ErrorActionPreference = $previousErrorPreference }
}

# Dot-sourcing exposes functions for isolated tests; it never installs anything.
if ($MyInvocation.InvocationName -eq '.') { return }
try { exit (Invoke-V2InstallEntry -Root (Split-Path $PSScriptRoot -Parent) -RequestedMode $Mode -Preferred $Python) }
catch { Write-Error $_; exit 1 }
