#!/usr/bin/env bash
# ==========================================================================================
#  PYROTECH VISION - start everything with ONE command on Debian / Ubuntu
#  (Linux version of start_backend.cmd / start.cmd)
#
#      git clone https://github.com/pyrotechhumandetection-lab/pyrotech-vision-.git
#      cd pyrotech-vision-
#      ./start.sh
#
#    1. ./setup-backend.sh   - system packages, .env, backend/.venv, pinned packages, self-check
#                              (only the missing steps; quick once everything is installed)
#    2. checks the API port - offers to stop an OLDER backend that still uses it
#    3. ./start-frontend.sh - web UI on http://127.0.0.1:5173 in the background (log: logs/frontend.log)
#    4. ./start-backend.sh  - the common backend in this terminal: Human Detection, Vehicle Detection,
#                             Human Count, Fire & Smoke Detection, Object Monitoring
#  Ctrl+C stops the backend and the web UI.
#
#  Options:
#    --setup-only   prepare everything, do not start
#    --reinstall    rebuild backend/.venv from scratch
#    --check        run the self-check and exit
#    --no-browser   do not open the browser
#  Settings: edit .env (ports, HOST, model, ...).  AIV_TORCH=cpu|cuda  AIV_OPENVINO=off
# ==========================================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=backend/tools/lib.sh
source backend/tools/lib.sh

SETUP_ARGS=(); SETUP_ONLY=0; CHECK_ONLY=0; OPEN_BROWSER=1
for a in "$@"; do
  case "$a" in
    --setup-only) SETUP_ONLY=1 ;;
    --reinstall)  SETUP_ARGS+=(--reinstall) ;;
    --check)      CHECK_ONLY=1 ;;
    --no-browser) OPEN_BROWSER=0 ;;
    -h|--help)    sed -n '2,24p' "$0"; exit 0 ;;
    *) die "Unknown option: $a (see ./start.sh --help)" ;;
  esac
done

# ---------------------------------------------------------------- 1. setup
./setup-backend.sh "${SETUP_ARGS[@]}"

if (( CHECK_ONLY )); then exec ./start-backend.sh --check; fi
if (( SETUP_ONLY )); then exit 0; fi

# ---------------------------------------------------------------- 2. API port free?
API_PORT="$(env_get AIV_PORT 8000)"
UI_PORT="$(env_get PORT 5173)"
if port_busy "$API_PORT"; then
  pid="$(port_pid "$API_PORT")"
  echo
  echo "Port $API_PORT is already in use${pid:+ by process $pid ($(ps -o args= -p "$pid" 2>/dev/null | cut -c1-80))}."
  echo "This is usually an older backend that is still running."
  [[ -n "$pid" ]] || die "Stop the program that uses port $API_PORT, then run ./start.sh again."
  read -r -p "Stop process $pid and start the new backend? [y/N] " ans
  [[ "$ans" =~ ^[Yy]$ ]] || die "Not started."
  kill "$pid"; sleep 2
fi

# the web UI forwards /api to BACKEND_URL - keep it on the backend's port when it points at this PC
BU="$(env_get BACKEND_URL "")"
if [[ -z "$BU" || "$BU" =~ ^http://(127\.0\.0\.1|localhost):[0-9]+/api/?$ ]]; then
  export BACKEND_URL="http://127.0.0.1:$API_PORT/api"
fi

# ---------------------------------------------------------------- 3. web UI
mkdir -p logs
UI_PID=""
cleanup() {
  if [[ -n "$UI_PID" ]] && kill -0 "$UI_PID" 2>/dev/null; then kill "$UI_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

if port_busy "$UI_PORT"; then
  say "Web UI port $UI_PORT already in use - using the web UI that is already running"
else
  say "Starting the web UI on port $UI_PORT (log: logs/frontend.log)"
  ./start-frontend.sh > logs/frontend.log 2>&1 &
  UI_PID=$!
  sleep 1
  kill -0 "$UI_PID" 2>/dev/null || { cat logs/frontend.log; die "The web UI did not start."; }
fi

URL="http://127.0.0.1:$UI_PORT"
if (( OPEN_BROWSER )) && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v xdg-open >/dev/null 2>&1; then
  (sleep 4; xdg-open "$URL" >/dev/null 2>&1 || true) &
fi

# ---------------------------------------------------------------- 4. backend
say "Starting the common backend: Human Detection, Vehicle Detection, Human Count, Fire & Smoke, Object Monitoring"
echo "    Open $URL in a browser. Press Ctrl+C here to stop the backend and the web UI."
set +e
./start-backend.sh
RC=$?
set -e
echo "Backend stopped (exit code $RC)."
exit "$RC"
