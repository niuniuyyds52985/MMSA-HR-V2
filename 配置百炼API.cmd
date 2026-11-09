@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0.venv-suit\Scripts\python.exe" (
    echo 请先双击“安装真才实鉴.cmd”完成本机环境安装。
    pause
    exit /b 1
)
echo 密钥仅在本机隐藏输入，请不要把密钥发送到聊天中。
powershell.exe -NoProfile -STA -ExecutionPolicy Bypass -File "%~dp0scripts\configure-bailian-gui.ps1" -Force
set "CONFIG_RESULT=%ERRORLEVEL%"
if "%CONFIG_RESULT%"=="2" (
    echo 已取消配置，现有安装保留。
) else if not "%CONFIG_RESULT%"=="0" (
    echo 配置失败，请查看上方提示后重试。
) else (
    echo 配置已保存。下一步请双击“启动面试工作台.cmd”。
)
pause
exit /b %CONFIG_RESULT%
