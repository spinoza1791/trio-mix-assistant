@echo off
setlocal
cd /d "%~dp0"
echo ============================================================
echo   Trio Mix-Assistant - one-time setup (Windows)
echo ============================================================

set PY=python
where python >nul 2>nul || set PY=py
where %PY% >nul 2>nul || (
  echo.
  echo Python 3.10+ was not found.
  echo Install it from https://www.python.org/downloads/  ^(tick "Add to PATH"^),
  echo then double-click this setup.bat again.
  echo.
  pause
  exit /b 1
)

echo Creating virtual environment (.venv) ...
%PY% -m venv .venv || ( echo Failed to create the virtual environment. & pause & exit /b 1 )

if exist wheels (
  echo Installing dependencies from the bundled wheels folder (offline) ...
  ".venv\Scripts\python" -m pip install --no-index --find-links wheels -r requirements-run.txt
) else (
  echo Installing dependencies from the internet ^(first time only^) ...
  ".venv\Scripts\python" -m pip install --upgrade pip
  ".venv\Scripts\python" -m pip install -r requirements-run.txt
)
if errorlevel 1 ( echo. & echo Dependency install failed. Check your internet connection. & pause & exit /b 1 )

echo.
echo ============================================================
echo   Setup complete.
echo   1) Open  show.conf  and set CONSOLE_IP and AUDIO_DEVICE
echo      ^(run  list-devices.bat  to find the audio device^).
echo   2) Double-click  start.bat
echo ============================================================
pause
