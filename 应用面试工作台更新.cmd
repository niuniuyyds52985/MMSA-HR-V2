@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0.venv-suit\Scripts\python.exe" -X utf8 -m src.mmsa_v2.launch
if errorlevel 1 pause
endlocal
