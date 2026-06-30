@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
".venv\Scripts\python" launcher.py --list-devices
echo.
echo Put the device NUMBER (or exact NAME) into AUDIO_DEVICE in show.conf
pause
