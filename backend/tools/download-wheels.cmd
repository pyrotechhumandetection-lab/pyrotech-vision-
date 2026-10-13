@echo off
rem Download every pinned package for Windows x64 / Python 3.14 into backend\wheels,
rem so setup-backend.cmd can install on a computer WITHOUT internet.
rem Run this on any Windows PC that has Python + internet, then copy the whole UI folder.
setlocal
cd /d "%~dp0..\.."
set "PY=python"
if exist "backend\.venv\Scripts\python.exe" set "PY=backend\.venv\Scripts\python.exe"
"%PY%" -m pip download --only-binary=:all: --platform win_amd64 --python-version 3.14 --implementation cp --no-deps -r backend\requirements-lock.txt -d backend\wheels || (echo Download failed. & exit /b 1)
echo.
echo Wheels saved in backend\wheels. Copy the UI folder to the offline PC and run setup-backend.cmd there.
