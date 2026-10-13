@echo off
rem Start the PYROTECH VISION frontend on http://127.0.0.1:5173 (proxies /api to the backend).
cd /d "%~dp0"
node server.js
