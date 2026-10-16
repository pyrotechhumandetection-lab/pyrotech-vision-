#!/usr/bin/env bash
# ==========================================================================================
#  PYROTECH VISION - setup for Debian / Ubuntu (Linux version of setup-backend.cmd)
#  start.sh runs this automatically; run it yourself only to prepare without starting.
#
#    1. installs missing system packages with apt (Python venv, Node.js, libraries OpenCV needs)
#    2. creates .env from .env.example
#    3. creates backend/.venv (Python 3.13/3.14; on Debian 12 fetches Python 3.13 with uv)
#    4. installs the pinned packages of backend/requirements-lock.txt (about 1 GB, internet),
#       or OFFLINE from backend/wheels/linux (see backend/tools/download-wheels.sh)
#    5. makes the contrib OpenCV build active and runs the self-check
#  Steps that are already done are skipped, so running it again is quick.
#
#  Options:  --reinstall   rebuild backend/.venv from scratch
#  .env / environment:  AIV_TORCH=cpu|cuda   AIV_OPENVINO=off
# ==========================================================================================
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=backend/tools/lib.sh
source backend/tools/lib.sh

REINSTALL=0
for a in "$@"; do
  case "$a" in
    --reinstall) REINSTALL=1 ;;
    -h|--help)   sed -n '2,17p' "$0"; exit 0 ;;
    *) die "Unknown option: $a (see ./setup-backend.sh --help)" ;;
  esac
done

PY="$VENV/bin/python"
STAMP="$VENV/.pyrotech-installed"
WHEELS="backend/wheels/linux"
TORCH_VER="2.14.0"
TORCHVISION_VER="0.29.0"
OPENCV_VER="5.0.0.93"

[[ "$(uname -s)" == "Linux" ]] || die "This script is for Debian/Ubuntu Linux. On Windows use setup-backend.cmd."
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
LDCONFIG="$(command -v ldconfig || echo /sbin/ldconfig)"     # /sbin is not in a normal user's PATH
has_lib() { "$LDCONFIG" -p 2>/dev/null | grep -F "$1" >/dev/null; }

need=()
command -v python3 >/dev/null 2>&1 || need+=(python3)
# venv needs the ensurepip package of the python3 actually in use (e.g. python3.13-venv when python3 is a
# deadsnakes 3.13, as on Google Colab) - the generic python3-venv only covers the distribution's own Python
if command -v python3 >/dev/null 2>&1; then
  PY3V="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  _vt="$(mktemp -d)"
  if ! python3 -m venv "$_vt/v" >/dev/null 2>&1; then
    need+=(python3-venv)
    pv="$(apt_pick "python${PY3V}-venv")"; [[ -n "$pv" ]] && need+=("$pv")
  fi
  rm -rf "$_vt"
fi
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
  [[ -n "$SUDO" ]] && ! command -v sudo >/dev/null 2>&1 && die "sudo not found - run as root or install: ${need[*]}"
  say "Installing system packages: ${need[*]}"
  $SUDO apt-get update
  $SUDO apt-get install -y "${need[@]}" ca-certificates
fi
NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
(( NODE_MAJOR >= 16 )) || die "Node.js 16 or newer is needed (found $(node -v 2>/dev/null || echo none))."

# ---------------------------------------------------------------- 2. .env
if [[ ! -f .env ]]; then
  say "Creating .env from .env.example"
  cp .env.example .env
  echo "    Edit .env to change ports, host or other settings, then start again."
fi

# AIV_PYTHON in .env = use that Python instead of backend/.venv (nothing is installed into it)
CUSTOM_PY="$(env_get AIV_PYTHON "")"
if [[ -n "$CUSTOM_PY" ]]; then
  warn "AIV_PYTHON=$CUSTOM_PY is set - backend/.venv is not set up. That Python must have $LOCK installed."
  exit 0
fi

# ---------------------------------------------------------------- 3. Python environment
# The pinned packages are tested with Python 3.13 (Linux) and 3.14 (Windows).
py_ok() { "$1" -c 'import sys; sys.exit(0 if sys.version_info[:2] in ((3,13),(3,14)) else 1)' >/dev/null 2>&1; }

OFFLINE=0
compgen -G "$WHEELS/*.whl" >/dev/null && OFFLINE=1

TORCH_MODE="$(env_get AIV_TORCH "")"
if [[ -z "$TORCH_MODE" ]]; then
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then TORCH_MODE=cuda; else TORCH_MODE=cpu; fi
fi
[[ "$TORCH_MODE" == "cpu" || "$TORCH_MODE" == "cuda" ]] || die "AIV_TORCH must be cpu or cuda (is '$TORCH_MODE')."
WANT_STAMP="$(sha256sum "$LOCK" | cut -c1-16) torch=$TORCH_MODE opencv=contrib-$OPENCV_VER openvino=$(env_get AIV_OPENVINO on)"

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
    (( OFFLINE )) && die "System $(python3 -V 2>&1) is too old and there is no internet setup for Python 3.13 offline."
    # Debian 12 ships Python 3.11: fetch Python 3.13 with uv (no change to the system Python)
    say "System $(python3 -V 2>&1) is too old - getting Python 3.13 with uv"
    UVDIR="$PWD/backend/.uv"
    if [[ ! -x "$UVDIR/bin/uv" ]]; then
      python3 -m venv "$UVDIR"
      "$UVDIR/bin/pip" install -q --disable-pip-version-check uv
    fi
    export UV_PYTHON_INSTALL_DIR="$UVDIR/python"
    "$UVDIR/bin/uv" python install 3.13
    "$UVDIR/bin/uv" venv --seed --python 3.13 "$VENV"
  fi
fi

if [[ "$(cat "$STAMP" 2>/dev/null)" == "$WANT_STAMP" ]]; then
  echo "Backend environment is up to date ($VENV)."
  exit 0
fi

# A venv without pip (interrupted earlier run, or created without --seed): add pip, else rebuild it with uv
if ! "$PY" -m pip --version >/dev/null 2>&1; then
  warn "$VENV has no pip - adding it"
  if ! "$PY" -m ensurepip --upgrade >/dev/null 2>&1; then
    UVDIR="$PWD/backend/.uv"
    if [[ ! -x "$UVDIR/bin/uv" ]]; then python3 -m venv "$UVDIR" && "$UVDIR/bin/pip" install -q --disable-pip-version-check uv; fi
    PYVER="$("$PY" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    rm -rf "$VENV"
    export UV_PYTHON_INSTALL_DIR="$UVDIR/python"
    "$UVDIR/bin/uv" python install "$PYVER"
    "$UVDIR/bin/uv" venv --seed --python "$PYVER" "$VENV"
  fi
  "$PY" -m pip --version >/dev/null 2>&1 || die "could not add pip to $VENV - delete backend/.venv and run again"
fi
# a venv left without pip by an interrupted earlier run: add pip, else ask to rebuild
if ! "$PY" -m pip --version >/dev/null 2>&1; then
  warn "$VENV has no pip - adding it"
  "$PY" -m ensurepip --upgrade >/dev/null 2>&1 || { rm -rf "$VENV"; die "$VENV was broken (no pip) and has been removed - run this script again"; }
fi
PIP=("$PY" -m pip --disable-pip-version-check)
SRC=()
if (( OFFLINE )); then
  say "Installing Python packages OFFLINE from $WHEELS"
  SRC=(--no-index --find-links "$WHEELS")
else
  say "Installing Python packages ($TORCH_MODE PyTorch; about 1 GB the first time, a few minutes)"
  "${PIP[@]}" install -q --upgrade pip
  if [[ "$TORCH_MODE" == "cpu" ]]; then
    # CPU build: avoids about 3 GB of NVIDIA libraries that a PC without an NVIDIA GPU cannot use
    if ! "${PIP[@]}" install "torch==$TORCH_VER" "torchvision==$TORCHVISION_VER" --index-url https://download.pytorch.org/whl/cpu; then
      warn "CPU PyTorch $TORCH_VER not available - installing the standard build from PyPI instead."
    fi
  fi
fi

# OpenVINO only speeds up CPU inference; every module falls back to PyTorch without it. Its wheels
# exist only for some platforms (e.g. aarch64 needs glibc 2.35+), so it is installed separately.
CORE_REQ="$(mktemp)"; OPT_REQ="$(mktemp)"
trap 'rm -f "$CORE_REQ" "$OPT_REQ"' EXIT
grep -viE '^openvino(-telemetry)?==' "$LOCK" > "$CORE_REQ"
grep -iE '^openvino(-telemetry)?==' "$LOCK" > "$OPT_REQ" || true
"${PIP[@]}" install "${SRC[@]}" -r "$CORE_REQ"
if [[ "$(env_get AIV_OPENVINO on)" == "off" ]]; then
  echo "    OpenVINO skipped (AIV_OPENVINO=off) - detection uses PyTorch."
elif [[ -s "$OPT_REQ" ]] && ! "${PIP[@]}" install "${SRC[@]}" -r "$OPT_REQ"; then
  warn "OpenVINO is not available for this PC ($ARCH, glibc $(ldd --version 2>/dev/null | head -n1 | grep -oE '[0-9]+\.[0-9]+$' || echo '?'), $("$PY" -V)).
         Continuing without it: all modules work with PyTorch, only CPU inference is slower."
fi

# Object Monitoring needs the CONTRIB OpenCV build (CSRT tracker); ultralytics pulls in plain
# opencv-python, which writes the same cv2 files - make the contrib build the one on disk.
say "Activating the contrib OpenCV build"
"${PIP[@]}" uninstall -y -q opencv-python opencv-contrib-python || true
"${PIP[@]}" install "${SRC[@]}" --no-deps --force-reinstall "opencv-contrib-python==$OPENCV_VER"

say "Running the self-check"
"$PY" backend/main.py --check || die "Self-check failed - see the messages above."
printf '%s' "$WANT_STAMP" > "$STAMP"
say "Setup complete. Start everything with ./start.sh"
