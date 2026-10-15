@echo off
REM ============================================================================================
REM  PYROTECH VISION - camera relay on this WINDOWS PC for the Google Colab backend (GPU)
REM
REM    scripts\colab\relay-windows.cmd setup     once: camera RTSP URL(s) only, downloads MediaMTX
REM    scripts\colab\relay-windows.cmd start     run the relay (keep the window open, Ctrl+C stops it)
REM    scripts\colab\relay-windows.cmd test      second window: relay -> camera check, must say OK
REM    scripts\colab\relay-windows.cmd show      values to enter in Colab (RELAY_HOST, RELAY_PASSWORD, paths)
REM    scripts\colab\relay-windows.cmd firewall [port]  (as Administrator, once) allow the relay ONLY from Tailscale
REM
REM  Needs: Tailscale for Windows installed and logged in (https://tailscale.com/download/windows).
REM  The camera password stays on this PC (%USERPROFILE%\.pyrotech-relay). Nothing is opened on the LAN.
REM ============================================================================================
setlocal
cd /d "%~dp0..\.."
set "CMD=%~1"
if "%CMD%"=="" (
  echo Usage: scripts\colab\relay-windows.cmd setup ^| start ^| test ^| show ^| firewall
  exit /b 2
)

REM --- Python: the project's own environment first, then the system Python
set "PY="
if exist "backend\.venv\Scripts\python.exe" set "PY=backend\.venv\Scripts\python.exe"
if not defined PY (
  where py >nul 2>&1 && set "PY=py -3"
)
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo ERROR: Python not found. Run setup-backend.cmd once, or install Python 3 from python.org.
  exit /b 1
)

REM --- Tailscale
if not exist "C:\Program Files\Tailscale\tailscale.exe" (
  where tailscale >nul 2>&1 || (
    echo ERROR: Tailscale is not installed. Install it from https://tailscale.com/download/windows and log in.
    exit /b 1
  )
)

if /i "%CMD%"=="firewall" goto firewall

%PY% scripts\colab\local_relay.py %*
exit /b %ERRORLEVEL%

:firewall
net session >nul 2>&1 || (
  echo ERROR: run this once in a terminal opened with "Run as administrator".
  exit /b 1
)
set "EXE=%USERPROFILE%\.pyrotech-relay\mediamtx.exe"
if not exist "%EXE%" (
  echo ERROR: run "setup" first - %EXE% is missing.
  exit /b 1
)
netsh advfirewall firewall delete rule name="PYROTECH VISION relay (Tailscale only)" >nul 2>&1
set "RPORT=%~2"
if "%RPORT%"=="" set "RPORT=8554"
netsh advfirewall firewall add rule name="PYROTECH VISION relay (Tailscale only)" dir=in action=allow ^
  program="%EXE%" protocol=TCP localport=%RPORT% remoteip=100.64.0.0/10 profile=any
if errorlevel 1 exit /b 1
echo OK: inbound TCP %RPORT% to MediaMTX allowed only from Tailscale addresses (100.64.0.0/10).
exit /b 0
