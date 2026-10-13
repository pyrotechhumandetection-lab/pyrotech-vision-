@echo off
rem ======================================================================
rem  PYROTECH VISION - one-time backend setup (creates backend\.venv)
rem  Needs Python 3.14 (same as the working detector) installed on this PC.
rem  Online : downloads the pinned packages from PyPI (about 1 GB).
rem  Offline: if backend\wheels\ contains the wheels (see backend\tools\download-wheels.cmd),
rem           installs from there without internet.
rem ======================================================================
setlocal
cd /d "%~dp0"
set "VENV=backend\.venv"
set "LOCK=backend\requirements-lock.txt"

if exist "%VENV%\Scripts\python.exe" goto install

set "PYEXE="
py -3.14 -c "import sys" >nul 2>&1 && set "PYEXE=py -3.14"
if not defined PYEXE python -c "import sys; sys.exit(0 if sys.version_info[:2]==(3,14) else 1)" >nul 2>&1 && set "PYEXE=python"
if not defined PYEXE (
  echo.
  echo  Python 3.14 was not found. Install it from https://www.python.org/downloads/
  echo  ^(tick "Add python.exe to PATH" or use the "py" launcher^), then run this script again.
  exit /b 1
)
echo Creating %VENV% with %PYEXE% ...
%PYEXE% -m venv "%VENV%" || (echo Failed to create the virtual environment. & exit /b 1)

:install
call :venv_in_use || goto fail
set "PIP=%VENV%\Scripts\python.exe -m pip"
if exist "backend\wheels\*.whl" (
  echo Installing packages OFFLINE from backend\wheels ...
  %PIP% install --no-index --find-links backend\wheels -r "%LOCK%" || goto fail
) else (
  echo Installing packages from PyPI ^(about 1 GB, a few minutes^) ...
  %PIP% install --disable-pip-version-check -r "%LOCK%" || goto fail
)

rem Object Monitoring needs the CONTRIB OpenCV build (CSRT/KCF trackers). ultralytics pulls in plain
rem opencv-python, which writes the same cv2 files - make the contrib build the one on disk.
echo Activating the contrib OpenCV build ...
%PIP% uninstall -y -q opencv-python opencv-contrib-python >nul 2>&1
if exist "backend\wheels\opencv_contrib_python-5.0.0.93*.whl" (
  %PIP% install --no-index --find-links backend\wheels --no-deps --force-reinstall opencv-contrib-python==5.0.0.93 || goto fail
) else (
  %PIP% install --disable-pip-version-check --no-deps --force-reinstall opencv-contrib-python==5.0.0.93 || goto fail
)

echo.
echo Running the backend self-check ...
"%VENV%\Scripts\python.exe" backend\main.py --check || goto fail
echo.
echo Setup complete. Start everything with start_backend.cmd
exit /b 0

:fail
echo.
echo Setup FAILED - see the messages above.
exit /b 1

rem ---- a running program (usually an old backend window) that has backend\.venv files open would make
rem      pip fail with "[WinError 5] Access is denied ... cv2.pyd" - find it and offer to stop it
:venv_in_use
powershell -NoProfile -ExecutionPolicy Bypass -File "backend\tools\venv-in-use.ps1" -Venv "%VENV%"
if not "%ERRORLEVEL%"=="3" exit /b 0
echo  Windows cannot update these files while a program has them open ^(usually an OLD backend window^).
choice /C YN /M " Stop these programs and continue"
if errorlevel 2 (
  echo  Close them, then run this again.
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "backend\tools\venv-in-use.ps1" -Venv "%VENV%" -Stop
exit /b %ERRORLEVEL%
