param(
    [string]$BaseUrl = 'https://dashscope.aliyuncs.com/compatible-mode/v1',
    [string]$Model = 'qwen3.7-plus',
    [switch]$PrepareOnly
)
$ErrorActionPreference = 'Stop'
$secretDir = Join-Path $PSScriptRoot '.secrets-user'
New-Item -ItemType Directory -Path $secretDir -Force | Out-Null
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = [System.Security.AccessControl.DirectorySecurity]::new()
$acl.SetOwner($identity)
$acl.SetAccessRuleProtection($true, $false)
$rule = [System.Security.AccessControl.FileSystemAccessRule]::new($identity, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
$acl.AddAccessRule($rule)
Set-Acl -LiteralPath $secretDir -AclObject $acl
if ($PrepareOnly) { Write-Output 'Private credential directory prepared.'; return }
$pythonExe = Join-Path (Split-Path $PSScriptRoot -Parent) '.venv-suit\Scripts\python.exe'
& $pythonExe (Join-Path $PSScriptRoot 'interview_credentials.py') --base-url $BaseUrl --model $Model
if ($LASTEXITCODE -ne 0) { throw 'Credential configuration failed.' }
Write-Output 'Restart the service after changing its model or API address.'
