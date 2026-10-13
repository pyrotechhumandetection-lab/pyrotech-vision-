#!/usr/bin/env bash
# ==========================================================================================
#  PYROTECH VISION - one script for Debian / Ubuntu desktops
#
#      git clone https://github.com/pyrotechhumandetection-lab/pyrotech-vision-.git
#      cd pyrotech-vision-
#      ./start.sh
#
#  What it does (only the steps that are still needed, so later starts are quick):
#    1. installs the system packages it needs with apt (asks for your sudo password once)
#    2. creates the Python environment backend/.venv and installs the pinned packages
#       (backend/requirements-lock.txt, about 1 GB the first time; needs internet)
#    3. creates .env from .env.example (edit it to change ports / host, then run again)
#    4. runs the self-check once after an install (models, packages, detection on a sample image)
#    5. starts the web UI (node server.js) in the background and the backend in this terminal,
#       opens http://127.0.0.1:5173 in the browser. Ctrl+C stops both.
#
#  Options:
#    --setup-only   prepare everything, do not start
#    --reinstall    rebuild backend/.venv from scratch
#    --check        run the self-check and exit
#    --no-browser   do not open the browser
#  Environment (or .env): AIV_TORCH=cpu|cuda  (default: cuda when an NVIDIA GPU is found, else cpu)
#                         AIV_OPENVINO=off     (skip the optional OpenVINO speed-up)
# ==========================================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

VENV="backend/.venv"
PY="$VENV/bin/python"
LOCK="backend/requirements-lock.txt"
STAMP="$VENV/.pyrotech-installed"
LOGDIR="logs"
TORCH_VER="2.14.0"
TORCHVISION_VER="0.29.0"
OPENCV_VER="5.0.0.93"

SETUP_ONLY=0; REINSTALL=0; CHECK_ONLY=0; OPEN_BROWSER=1
for a in "$@"; do
  case "$a" in
    --setup-only) SETUP_ONLY=1 ;;
    --reinstall)  REINSTALL=1 ;;
    --check)      CHECK_ONLY=1 ;;
    --no-browser) OPEN_BROWSER=0 ;;
    -h|--help)    sed -n '2,27p' "$0"; exit 0 ;;
    *) echo "Unknown option: $a (see ./start.sh --help)"; exit 2 ;;
  esac
done

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# read KEY from .env (real environment variables win)
env_get() {
  local key="$1" def="${2:-}" val=""
  if [[ -n "${!key:-}" ]]; then printf '%s' "${!key}"; return; fi
  if [[ -f .env ]]; then
    val="$( (grep -E "^[[:space:]]*${key}=" .env || true) | tail -n1 | cut -d= -f2- | tr -d '\r' | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/')"
  fi
  printf '%s' "${val:-$def}"
}

[[ "$(uname -s)" == "Linux" ]] || die "This script is for Debian/Ubuntu Linux. On Windows use start_backend.cmd."
ARCH="$(uname -m)"
case "$ARCH" in
  x86_64|aarch64) ;;
  *) die "This PC is '$ARCH'. PyTorch (needed by all detection modules) is only available for 64-bit
       systems (x86_64 or aarch64). On a Raspberry Pi install the 64-bit Raspberry Pi OS." ;;
esac

# ---------------------------------------------------------------- 1. system packages
SUDO=""
if [[ $EUID -ne 0 ]]; then SUDO="sudo"; fi

apt_pick() {   # first package name that apt knows (Debian 13 renamed some libraries)
  local p
  for p in "$@"; do
    if apt-cache show "$p" >/dev/null 2>&1; then printf '%s' "$p"; return; fi
  done
}

has_lib() { ldconfig -p 2>/dev/null | grep -F "$1" >/dev/null; }
need=()
command -v python3 >/dev/null 2>&1 || need+=(python3)
python3 -c 'import venv, ensurepip' >/dev/null 2>&1 || need+=(python3-venv)
command -v node >/dev/null 2>&1 || need+=(nodejs)
command -v curl >/dev/null 2>&1 || need+=(curl)
command -v ss >/dev/null 2>&1 || need+=(iproute2)
has_lib 'libGL.so.1' || need+=(libgl1)
has_lib 'libgomp.so.1' || need+=(libgomp1)
if ! has_lib 'libgthread-2.0.so.0'; then
  glib="$(apt_pick libglib2.0-0t64 libglib2.0-0)"
  need+=("${glib:-libglib2.0-0}")
fi

if ((${#need[@]})); then
  command -v apt-get >/dev/null 2>&1 || die "Missing: ${need[*]} - install them with your package manager."
  say "Installing system packages: ${need[*]}"
  [[ -n "$SUDO" ]] && ! command -v sudo >/dev/null 2>&1 && die "sudo not found - run as root or install: ${need[*]}"
  $SUDO apt-get update
  $SUDO apt-get install -y "${need[@]}" ca-certificates
fi

NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
(( NODE_MAJOR >= 16 )) || die "Node.js 16 or newer is needed (found $(node -v 2>/dev/null || echo none))."

# ---------------------------------------------------------------- 2. .env
if [[ ! -f .env ]]; then
  say "Creating .env from .env.example"
  cp .env.example .env
  echo "    Edit .env to change ports, host or other settings, then run ./start.sh again."
fi

# ---------------------------------------------------------------- 3. Python environment
# The pinned packages are tested with Python 3.13 (Linux) and 3.14 (Windows).
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] in ((3,13),(3,14)) else 1)' >/dev/null 2>&1; }

TORCH_MODE="$(env_get AIV_TORCH "")"
if [[ -z "$TORCH_MODE" ]]; then
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then TORCH_MODE=cuda; else TORCH_MODE=cpu; fi
fi
[[ "$TORCH_MODE" == "cpu" || "$TORCH_MODE" == "cuda" ]] || die "AIV_TORCH must be cpu or cuda (is '$TORCH_MODE')."
WANT_STAMP="$(sha256sum "$LOCK" | cut -c1-16) torch=$TORCH_MODE opencv=contrib-$OPENCV_VER"

if (( REINSTALL )) && [[ -d "$VENV" ]]; then
  say "Removing $VENV (--reinstall)"
  rm -rf "$VENV"
fi

if [[ -x "$PY" ]] && ! py_ok "$PY"; then
  warn "$VENV uses an unsupported Python version - rebuilding it."
  rm -rf "$VENV"
fi

if [[ ! -x "$PY" ]]; then
  if py_ok python3; then
    say "Creating $VENV with $(python3 -V)"
    python3 -m venv "$VENV"
  else
    # Debian 12 ships Python 3.11: fetch Python 3.13 with uv (no change to the system Python)
    say "System $(python3 -V 2>&1) is too old - getting Python 3.13 with uv"
    UVDIR="$ROOT/backend/.uv"
    if [[ ! -x "$UVDIR/bin/uv" ]]; then
      python3 -m venv "$UVDIR"
      "$UVDIR/bin/pip" install -q --disable-pip-version-check uv
    fi
    export UV_PYTHON_INSTALL_DIR="$UVDIR/python"
    "$UVDIR/bin/uv" python install 3.13
    "$UVDIR/bin/uv" venv --seed --python 3.13 "$VENV"
  fi
fi

if [[ "$(cat "$STAMP" 2>/dev/null)" != "$WANT_STAMP" ]]; then
  PIP=("$PY" -m pip --disable-pip-version-check)
  say "Installing Python packages ($TORCH_MODE PyTorch; about 1 GB the first time, a few minutes)"
  "${PIP[@]}" install -q --upgrade pip
  if [[ "$TORCH_MODE" == "cpu" ]]; then
    # CPU build: avoids about 3 GB of NVIDIA libraries that a PC without an NVIDIA GPU cannot use
    if ! "${PIP[@]}" install "torch==$TORCH_VER" "torchvision==$TORCHVISION_VER" --index-url https://download.pytorch.org/whl/cpu; then
      warn "CPU PyTorch $TORCH_VER not available - installing the standard build from PyPI instead."
    fi
  fi
  # OpenVINO only speeds up CPU inference; every module falls back to PyTorch without it. Its wheels
  # exist only for some platforms (e.g. aarch64 needs glibc 2.35+), so it is installed separately.
  CORE_REQ="$(mktemp)"; OPT_REQ="$(mktemp)"
  grep -viE '^openvino(-telemetry)?==' "$LOCK" > "$CORE_REQ"
  grep -iE '^openvino(-telemetry)?==' "$LOCK" > "$OPT_REQ" || true
  "${PIP[@]}" install -r "$CORE_REQ"
  if [[ "$(env_get AIV_OPENVINO on)" == "off" ]]; then
    echo "    OpenVINO skipped (AIV_OPENVINO=off) - detection uses PyTorch."
  elif [[ -s "$OPT_REQ" ]] && ! "${PIP[@]}" install -r "$OPT_REQ"; then
    warn "OpenVINO is not available for this PC ($ARCH, glibc $(ldd --version 2>/dev/null | head -n1 | grep -oE '[0-9]+\.[0-9]+$' || echo '?'), $("$PY" -V)).
         Continuing without it: all modules work with PyTorch, only CPU inference is slower."
  fi
  rm -f "$CORE_REQ" "$OPT_REQ"
  # Object Monitoring needs the CONTRIB OpenCV build (CSRT tracker); ultralytics pulls in plain
  # opencv-python, which writes the same cv2 files - make the contrib build the one on disk.
  say "Activating the contrib OpenCV build"
  "${PIP[@]}" uninstall -y -q opencv-python opencv-contrib-python || true
  "${PIP[@]}" install --no-deps --force-reinstall "opencv-contrib-python==$OPENCV_VER"
  say "Running the self-check"
  "$PY" backend/main.py --check || die "Self-check failed - see the messages above."
  printf '%s' "$WANT_STAMP" > "$STAMP"
fi

if (( CHECK_ONLY )); then
  "$PY" backend/main.py --check
  exit $?
fi
if (( SETUP_ONLY )); then
  say "Setup complete. Start the application with: ./start.sh"
  exit 0
fi

# ---------------------------------------------------------------- 4. start
API_PORT="$(env_get AIV_PORT 8000)"
UI_PORT="$(env_get PORT 5173)"
# the web UI forwards /api to BACKEND_URL - keep it on the backend's port when it points at this PC
BU="$(env_get BACKEND_URL "")"
if [[ -z "$BU" || "$BU" =~ ^http://(127\.0\.0\.1|localhost):[0-9]+/api/?$ ]]; then
  export BACKEND_URL="http://127.0.0.1:$API_PORT/api"
fi

port_pid() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2; }
port_busy() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

if port_busy "$API_PORT"; then
  pid="$(port_pid "$API_PORT")"
  echo
  echo "Port $API_PORT is already in use${pid:+ by process $pid ($(ps -o args= -p "$pid" 2>/dev/null | cut -c1-80))}."
  echo "This is usually an older backend that is still running."
  if [[ -n "$pid" ]]; then
    read -r -p "Stop process $pid and start the new backend? [y/N] " ans
    if [[ "$ans" =~ ^[Yy]$ ]]; then kill "$pid"; sleep 2; else die "Not started."; fi
  else
    die "Stop the program that uses port $API_PORT, then run ./start.sh again."
  fi
fi

mkdir -p "$LOGDIR"
UI_PID=""
cleanup() {
  if [[ -n "$UI_PID" ]] && kill -0 "$UI_PID" 2>/dev/null; then kill "$UI_PID" 2>/dev/null || true; fi
}
trap cleanup EXIT INT TERM

if port_busy "$UI_PORT"; then
  say "Web UI port $UI_PORT already in use - using the web UI that is already running"
else
  say "Starting the web UI on port $UI_PORT (log: $LOGDIR/frontend.log)"
  node server.js > "$LOGDIR/frontend.log" 2>&1 &
  UI_PID=$!
  sleep 1
  kill -0 "$UI_PID" 2>/dev/null || { cat "$LOGDIR/frontend.log"; die "The web UI did not start."; }
fi

URL="http://127.0.0.1:$UI_PORT"
if (( OPEN_BROWSER )) && [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]] && command -v xdg-open >/dev/null 2>&1; then
  (sleep 4; xdg-open "$URL" >/dev/null 2>&1 || true) &
fi

say "Starting the common backend: Human Detection, Vehicle Detection, Human Count, Fire & Smoke, Object Monitoring"
echo "    Open $URL in a browser. Press Ctrl+C here to stop the backend and the web UI."
set +e
"$PY" backend/main.py
RC=$?
set -e
echo "Backend stopped (exit code $RC)."
exit "$RC"
