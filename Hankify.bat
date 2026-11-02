@echo off
rem Starts Hankify. Keep this window open while it runs; errors show up here.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Hankify is not installed yet. Double-click "Install Hankify.bat" first.
  pause
  exit /b 1
)
set "PATH=%~dp0tools\ffmpeg\bin;%PATH%"
set "HANKIFIER_CONFIG=%~dp0config.json"
set PYTHONUTF8=1
".venv\Scripts\python.exe" "hankify\app.py"
if errorlevel 1 pause
