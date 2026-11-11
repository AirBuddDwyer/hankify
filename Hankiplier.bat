@echo off
rem Starts Hankiplier (multi-voice Hankify: one song, several singers). Keep this window open while it runs.
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Hankify is not installed yet. Double-click "Install Hankify.bat" first.
  pause
  exit /b 1
)
set "PATH=%~dp0tools\ffmpeg\bin;%PATH%"
set "HANKIFIER_CONFIG=%~dp0config.json"
set "HANKIPLIER_WHISPER_DIR=%~dp0models\whisper"
set PYTHONUTF8=1
".venv\Scripts\python.exe" "hankiplier\app.py"
if errorlevel 1 pause
