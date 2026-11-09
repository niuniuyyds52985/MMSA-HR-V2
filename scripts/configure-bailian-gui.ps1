param([switch]$Force, [switch]$StatusOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $projectRoot '.venv-suit\Scripts\python.exe'
$credentialScript = Join-Path $projectRoot 'suit-demo\interview_credentials.py'
$prepareScript = Join-Path $projectRoot 'suit-demo\configure-interview.ps1'
$fixedModel = 'qwen3.7-plus'
$fixedAsrModel = 'qwen3-asr-flash'
$fixedBaseUrl = 'https://dashscope.aliyuncs.com/compatible-mode/v1'
$keyGuide = 'https://help.aliyun.com/zh/model-studio/get-api-key/'
$modelGuide = 'https://help.aliyun.com/en/model-studio/text-generation-model/'

function New-PythonProcess([string]$Arguments, [bool]$WithInput = $false) {
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo.FileName = $pythonExe
    $process.StartInfo.Arguments = $Arguments
    $process.StartInfo.WorkingDirectory = $projectRoot
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.CreateNoWindow = $true
    $process.StartInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.RedirectStandardError = $true
    $process.StartInfo.RedirectStandardInput = $WithInput
    $process.StartInfo.EnvironmentVariables['PYTHONUTF8'] = '1'
    $process.StartInfo.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    $process.StartInfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $process.StartInfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8
    return $process
}

function Get-SafeConfigurationStatus {
    $child = New-PythonProcess '-B -X utf8 -m src.mmsa_v2.cloud_policy --status'
    try {
        if (-not $child.Start()) { throw 'Status process failed.' }
        if (-not $child.WaitForExit(20000)) { $child.Kill(); throw 'Status timed out.' }
        if ($child.ExitCode -ne 0) { throw 'Status unavailable.' }
        # cloud_policy reads only public settings and credential presence, never get_api_key.
        return ($child.StandardOutput.ReadToEnd() | ConvertFrom-Json)
    } finally { $child.Dispose() }
}

function Test-InteractiveDesktop {
    if (-not [Environment]::UserInteractive -or [System.Diagnostics.Process]::GetCurrentProcess().SessionId -eq 0) { return $false }
    if (-not ('ZhenjianSetupDesktop' -as [type])) {
        Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class ZhenjianSetupDesktop {
    [DllImport("user32.dll", SetLastError=true)] public static extern IntPtr OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll")] public static extern bool CloseDesktop(IntPtr desktop);
}
'@
    }
    $desktop = [ZhenjianSetupDesktop]::OpenInputDesktop(0, $false, 0x0100)
    if ($desktop -eq [IntPtr]::Zero) { return $false }
    [void][ZhenjianSetupDesktop]::CloseDesktop($desktop)
    return $true
}

try {
    if (-not (Test-Path -LiteralPath $pythonExe -PathType Leaf)) { throw 'Environment unavailable.' }
    $status = Get-SafeConfigurationStatus
    if ($StatusOnly) { $status | ConvertTo-Json -Compress; exit 0 }
    if ($status.configured -and -not $Force) {
        Write-Output '已存在符合本版本模型策略的本机配置；未读取明文密钥、未测试付费 API。'
        exit 0
    }
    if (-not (Test-InteractiveDesktop)) {
        [Console]::Error.WriteLine('当前会话没有可交互桌面。请在自己的 Windows 桌面双击项目根目录的“开始使用真才实鉴.cmd”。未保存密钥。')
        exit 3
    }
    Add-Type -AssemblyName System.Windows.Forms
    Add-Type -AssemblyName System.Drawing
    [System.Windows.Forms.Application]::EnableVisualStyles()
    if ($status.environment_overrides) {
        [void][System.Windows.Forms.MessageBox]::Show('当前进程存在百炼环境变量覆盖。请先在本机移除或修正覆盖配置，再重新打开配置窗口；本程序不会偷偷修改你的环境变量。', '真才实鉴 · 配置未完成', 'OK', 'Warning')
        exit 4
    }

    $form = New-Object System.Windows.Forms.Form
    $form.Text = '真才实鉴 V2 · 配置你自己的百炼'
    $form.ClientSize = New-Object System.Drawing.Size(620, 390)
    $form.StartPosition = 'CenterScreen'
    $form.FormBorderStyle = 'FixedDialog'
    $form.MaximizeBox = $false
    $form.MinimizeBox = $false
    $form.TopMost = $true
    $form.Font = New-Object System.Drawing.Font('Microsoft YaHei UI', 10)
    $intro = New-Object System.Windows.Forms.Label
    $intro.SetBounds(24, 20, 570, 96)
    $intro.Text = "使用你自己的阿里云百炼账户，调用费用由该账户承担。`r`n文本评分：$fixedModel    云转写：$fixedAsrModel`r`n使用北京地域的普通百炼 API Key；不要填写 Token 或 Coding Plan Key。"
    $form.Controls.Add($intro)
    $links = New-Object System.Windows.Forms.LinkLabel
    $links.SetBounds(24, 119, 570, 28)
    $links.Text = '官方创建 API Key 指南'
    $links.Add_LinkClicked({ Start-Process -FilePath $keyGuide | Out-Null })
    $form.Controls.Add($links)
    $modelLink = New-Object System.Windows.Forms.LinkLabel
    $modelLink.SetBounds(24, 151, 570, 28)
    $modelLink.Text = '官方模型使用说明'
    $modelLink.Add_LinkClicked({ Start-Process -FilePath $modelGuide | Out-Null })
    $form.Controls.Add($modelLink)
    $label = New-Object System.Windows.Forms.Label
    $label.SetBounds(24, 191, 570, 25)
    $label.Text = '百炼 API Key（输入内容隐藏，仅在这台电脑加密保存）'
    $form.Controls.Add($label)
    $passwordBox = New-Object System.Windows.Forms.TextBox
    $passwordBox.SetBounds(24, 222, 570, 32)
    $passwordBox.UseSystemPasswordChar = $true
    $passwordBox.MaxLength = 512
    $form.Controls.Add($passwordBox)
    $hint = New-Object System.Windows.Forms.Label
    $hint.SetBounds(24, 265, 570, 42)
    $hint.Text = '不把密钥发到千问聊天。保存配置不会发送面试资料，也不会调用付费 API。'
    $form.Controls.Add($hint)
    $save = New-Object System.Windows.Forms.Button
    $save.SetBounds(332, 326, 126, 38)
    $save.Text = '保存本机配置'
    $cancel = New-Object System.Windows.Forms.Button
    $cancel.SetBounds(470, 326, 124, 38)
    $cancel.Text = '取消'
    $cancel.DialogResult = [System.Windows.Forms.DialogResult]::Cancel
    $form.Controls.AddRange(@($save, $cancel))
    $form.AcceptButton = $save
    $form.CancelButton = $cancel
    $script:savedLocally = $false
    $save.Add_Click({
        $apiKey = $null
        $child = $null
        try {
            $apiKey = $passwordBox.Text
            if ([string]::IsNullOrWhiteSpace($apiKey) -or $apiKey -match '\s') {
                [void][System.Windows.Forms.MessageBox]::Show('请输入不含空格的百炼 API Key。', '检查输入', 'OK', 'Warning')
                return
            }
            $save.Enabled = $false
            # Keep the ACL preparation used by the existing CurrentUser DPAPI writer.
            & $prepareScript -PrepareOnly | Out-Null
            $arguments = '-B -X utf8 "' + $credentialScript + '" --stdin --model ' + $fixedModel + ' --base-url ' + $fixedBaseUrl
            $child = New-PythonProcess $arguments $true
            if (-not $child.Start()) { throw 'Credential process failed.' }
            # The key is never placed in command arguments, environment or a temporary file.
            $child.StandardInput.WriteLine($apiKey)
            $child.StandardInput.Close()
            $apiKey = $null
            $passwordBox.Clear()
            if (-not $child.WaitForExit(20000)) { $child.Kill(); throw 'Credential save timed out.' }
            if ($child.ExitCode -ne 0) { throw 'Credential save failed.' }
            $after = Get-SafeConfigurationStatus
            if (-not $after.configured) { throw 'Saved configuration is unavailable.' }
            $script:savedLocally = $true
            $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
            $form.Close()
        } catch {
            [void][System.Windows.Forms.MessageBox]::Show('本机配置未成功保存，请检查当前 Windows 账户是否能写入项目目录，然后重试。密钥和内部错误未输出。', '真才实鉴 · 保存失败', 'OK', 'Error')
        } finally {
            $apiKey = $null
            $passwordBox.Clear()
            if ($null -ne $child) { $child.Dispose() }
            if (-not $form.IsDisposed) { $save.Enabled = $true }
        }
    })
    $form.Add_Shown({ $form.Activate(); $passwordBox.Focus() | Out-Null })
    try { [void]$form.ShowDialog() } finally { $passwordBox.Clear(); $form.Dispose() }
    if ($script:savedLocally) {
        Write-Output '本机百炼配置已保存。费用由使用者自己的账户承担；未进行付费调用。'
        exit 0
    }
    Write-Output '已取消百炼配置。'
    exit 2
} catch {
    if ($StatusOnly) {
        @{configured=$false; credential_present=$false; policy_compliant=$false; api_verified=$false; text_model=$fixedModel; asr_model=$fixedAsrModel; error='status_unavailable'} | ConvertTo-Json -Compress
    } else {
        [Console]::Error.WriteLine('百炼配置入口未能启动。请先完成环境安装，再双击根目录“开始使用真才实鉴.cmd”；未输出密钥或内部错误。')
    }
    exit 1
}
