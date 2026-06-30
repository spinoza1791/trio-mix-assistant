@echo off
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Please run setup.bat first.
  pause
  exit /b 1
)
echo Running the FULL app against a simulated console (no hardware needed)...
".venv\Scripts\python" launcher.py --rehearse
pause
