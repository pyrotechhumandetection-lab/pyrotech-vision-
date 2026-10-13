#!/usr/bin/env bash
# Backend ONLY, without setup or web UI (advanced; Linux version of start-backend.cmd).
# Normally use ./start.sh, which also prepares the Python environment and starts the web UI.
# API on http://127.0.0.1:8000/api (all five detection modules). Uses backend/.venv, or the Python
# named by AIV_PYTHON in .env. Extra arguments go to backend/main.py (e.g. --check).
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=backend/tools/lib.sh
source backend/tools/lib.sh

PY="$(backend_python)"
if [[ ! -x "$PY" ]] && ! command -v "$PY" >/dev/null 2>&1; then
  die "The backend has no Python environment yet. Run ./start.sh (it sets everything up)."
fi
echo "Using Python: $PY"
exec "$PY" backend/main.py "$@"
