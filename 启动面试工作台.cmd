@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0.venv-suit\Scripts\python.exe" (
    echo 尚未安装本机环境。请先双击“安装真才实鉴.cmd”。
    pause
    exit /b 1
)
"%~dp0.venv-suit\Scripts\python.exe" -X utf8 -m src.mmsa_v2.launch --open
if errorlevel 1 pause
endlocal
