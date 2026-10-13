@echo off
rem ==========================================================================================
rem  PYROTECH VISION - start everything with ONE command
rem
rem  1. finds an OLDER backend still running on the API port and offers to stop it
rem  2. checks this folder's Python environment (backend\.venv) and installs / updates it when
rem     it is missing or older than the five detection modules need (runs setup-backend.cmd)
rem  3. starts the web UI (node server.js, http://127.0.0.1:5173) in its own window and opens it
rem  4. runs the common backend in THIS window: Human Detection, Vehicle Detection, Human Count,
rem     Fire ^& Smoke Detection and Object Monitoring on http://127.0.0.1:8000/api
rem
rem  Everything comes from this folder - no other project folder is used.
rem  Close this window (or press Ctrl+C) to stop the backend.
rem ==========================================================================================
setlocal EnableExtensions
cd /d "%~dp0"
title PYROTECH VISION backend

set "AIV_PORT_RUN=8000"
if exist ".env" for /f "usebackq tokens=1,* delims==" %%A in (".env") do (
  if /i "%%A"=="AIV_PYTHON" if not defined AIV_PYTHON set "AIV_PYTHON=%%B"
  if /i "%%A"=="AIV_PORT" set "AIV_PORT_RUN=%%B"
)
if defined AIV_PORT set "AIV_PORT_RUN=%AIV_PORT%"

rem ---- 1. API port free? (checked first: an old backend keeps backend\.venv files open and blocks the update)
set "BPID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:"127.0.0.1:%AIV_PORT_RUN% .*LISTENING" /c:"0.0.0.0:%AIV_PORT_RUN% .*LISTENING"') do set "BPID=%%P"
if defined BPID (
  echo.
  echo  Port %AIV_PORT_RUN% is already in use by process %BPID%:
  tasklist /FI "PID eq %BPID%" /FO TABLE /NH
  echo  This is usually an OLDER backend window that is still open - it does not have the new modules.
  choice /C YN /M " Stop process %BPID% and start the new backend"
  if errorlevel 2 (
    echo  Not started. Close the other backend window, then run start_backend.cmd again.
    pause
    exit /b 1
  )
  taskkill /PID %BPID% /F >nul && echo  Stopped process %BPID%.
  timeout /t 2 /nobreak >nul
)

rem ---- 2. Python environment -----------------------------------------------------------------
set "PY="
if defined AIV_PYTHON set "PY=%AIV_PYTHON%"
if not defined PY if exist "backend\.venv\Scripts\python.exe" set "PY=backend\.venv\Scripts\python.exe"
if not defined PY (
  echo.
  echo  First start: creating the backend Python environment ^(needs Python 3.14 and internet, about 1 GB^) ...
  call setup-backend.cmd || goto setup_failed
  set "PY=backend\.venv\Scripts\python.exe"
)
"%PY%" -c "import sys, ultralytics, cv2, torch; sys.exit(0 if ultralytics.__version__ == '8.4.155' and hasattr(cv2, 'TrackerCSRT_create') else 3)" >nul 2>&1
if errorlevel 1 (
  if defined AIV_PYTHON (
    echo  WARNING: AIV_PYTHON=%AIV_PYTHON% does not have ultralytics 8.4.155 with the contrib OpenCV build.
    echo           Fire ^& Smoke and Object Monitoring may not work. Remove AIV_PYTHON from .env to use backend\.venv.
  ) else (
    echo.
    echo  Updating the backend Python environment for all five detection modules ^(one time^) ...
    call setup-backend.cmd || goto setup_failed
  )
)

rem ---- 3. web UI ------------------------------------------------------------------------------
set "UIPID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:"127.0.0.1:5173 .*LISTENING" /c:"0.0.0.0:5173 .*LISTENING"') do set "UIPID=%%P"
where node >nul 2>&1
if errorlevel 1 (
  echo  Node.js was not found - the web UI is not started ^(install Node.js from https://nodejs.org^).
  echo  The backend API still runs on http://127.0.0.1:%AIV_PORT_RUN%/api
) else (
  if not defined UIPID start "PYROTECH VISION web UI" /min cmd /c "node server.js"
  start "" http://127.0.0.1:5173
)

rem ---- 4. common backend (all five modules) ------------------------------------------------------
echo.
echo  Starting the common backend: Human Detection, Vehicle Detection, Human Count, Fire ^& Smoke, Object Monitoring
echo  Python: %PY%
"%PY%" backend\main.py %*
set "RC=%ERRORLEVEL%"
echo.
echo  Backend stopped (exit code %RC%).
pause
exit /b %RC%

:setup_failed
echo.
echo  The backend environment could not be prepared - see the messages above.
pause
exit /b 1
