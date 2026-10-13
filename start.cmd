@echo off
rem Same as start_backend.cmd (kept so existing shortcuts keep working): checks the Python
rem environment, starts the web UI and the common backend with all five detection modules.
call "%~dp0start_backend.cmd" %*
