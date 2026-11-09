@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\first-run-v2.ps1" %*
set "SETUP_RESULT=%ERRORLEVEL%"
if "%SETUP_RESULT%"=="0" exit /b 0
if "%SETUP_RESULT%"=="2" echo API setup cancelled. Run this entry again when you are ready.
if not "%SETUP_RESULT%"=="2" echo Setup did not complete. Read the message above and retry.
pause
exit /b %SETUP_RESULT%
