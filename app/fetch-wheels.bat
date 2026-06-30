@echo off
setlocal
cd /d "%~dp0"
echo Downloading dependency wheels for THIS Windows machine into .\wheels ...
echo (Run this on a Windows PC WITH internet that matches the FOH laptop's
echo  Python version + architecture, then copy the whole folder over.)
set PY=python
where python >nul 2>nul || set PY=py
%PY% -m pip download -r requirements-run.txt -d wheels
echo.
echo Done. The wheels\ folder now lets setup.bat install fully OFFLINE.
pause
