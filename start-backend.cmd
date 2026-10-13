@echo off
rem Backend ONLY, without checks or web UI (advanced). Normally use start_backend.cmd, which also checks the
rem Python environment and starts the web UI. API on http://127.0.0.1:8000/api (all five detection modules).
rem Uses this folder's own Python environment (backend\.venv, created by setup-backend.cmd).
rem AIV_PYTHON in .env can point to a different Python, but is not needed.
setlocal
cd /d "%~dp0"
if not defined AIV_PYTHON if exist ".env" for /f "usebackq tokens=1,* delims==" %%A in (".env") do if /i "%%A"=="AIV_PYTHON" set "AIV_PYTHON=%%B"
set "PY="
if defined AIV_PYTHON set "PY=%AIV_PYTHON%"
if not defined PY if exist "backend\.venv\Scripts\python.exe" set "PY=backend\.venv\Scripts\python.exe"
if not defined PY (
  echo.
  echo  The backend has no Python environment yet. Run start_backend.cmd ^(it sets everything up^).
  echo.
  pause
  exit /b 1
)
echo Using Python: %PY%
"%PY%" backend\main.py %*
