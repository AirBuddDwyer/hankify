@echo off
rem Installs Hankify into this folder. Safe to run again (it skips what is already done).
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0windows\install.ps1"
echo.
pause
