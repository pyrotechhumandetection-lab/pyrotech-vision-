#!/usr/bin/env bash
# ==========================================================================================
#  PYROTECH VISION - ONE script for Debian / Ubuntu (AI box): install, run and maintain
#
#      ./aivision.sh install        first time (and after moving the folder): downloads and installs every
#                                   requirement, installs the backend + web UI as services that start at
#                                   every boot, starts them and checks them
#      ./aivision.sh update         later updates (backup, git pull, reinstall, check)
#      ./aivision.sh <command> --help
#
#  Commands:
#    setup      download and install everything (system packages, Node.js, Python env, all requirements)
#    install    setup + systemd services (start at boot) + start + check
#    start      start the services
#    stop       stop the services
#    restart    restart [all|backend|frontend]
#    status     step-by-step status [--wait N]
#    update     backup, git pull, (data dir), setup if needed, reinstall, check
#    uninstall  remove the services (keeps all data)
#    check      read-only environment check
#    run        run in this terminal without services (Ctrl+C stops)
#    ai-stream  AI on the camera substream: list | sub ID..|--all | custom ID PATH | same ID
#    perf       performance monitor / CPU tuning (status | monitor | auto ...)
#    npu        Rockchip NPU (RK3588): setup | status | check | bench | on | off | model | calib-pack
# ==========================================================================================
set -euo pipefail
SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)/$(basename "${BASH_SOURCE[0]}")"

# ---------------------------------------------------------------- shared helpers
# repository root = two levels above this file
REPO="$(dirname "$SELF")"

BACKEND_UNIT="pyrotech-vision-backend.service"
FRONTEND_UNIT="pyrotech-vision-frontend.service"
UNITS=("$BACKEND_UNIT" "$FRONTEND_UNIT")
UNIT_DIR="/etc/systemd/system"
# every unit file written by "aivision.sh install" carries this line (text kept from the older
# scripts/debian/install-services.sh so already installed units are recognised); others are never touched
MARKER="# Managed by PYROTECH VISION scripts/debian/install-services.sh"

VENV_PY="$REPO/backend/.venv/bin/python"

# ufw lives in /usr/sbin, which is not in a normal user's PATH on Debian
UFW="$(command -v ufw 2>/dev/null || { [[ -x /usr/sbin/ufw ]] && echo /usr/sbin/ufw; } || true)"

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[1;32m[ OK ]\033[0m %s\n' "$*"; }
bad()  { printf '  \033[1;31m[FAIL]\033[0m %s\n' "$*"; }
note() { printf '  \033[1;33m[WARN]\033[0m %s\n' "$*"; }
info() { printf '  [INFO] %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

# sudo only where root is needed
SUDO=""
if [[ $EUID -ne 0 ]]; then SUDO="sudo"; fi
as_root() {
  if [[ -n "$SUDO" ]] && ! command -v sudo >/dev/null 2>&1; then
    die "This step needs root rights and sudo is not installed: $*"
  fi
  $SUDO "$@"
}

# env_get KEY [DEFAULT] - value from the project's .env (real environment wins), else DEFAULT
env_get() {
  local key="$1" def="${2:-}" val=""
  if [[ -n "${!key:-}" ]]; then printf '%s' "${!key}"; return; fi
  if [[ -f "$REPO/.env" ]]; then
    val="$( (grep -E "^[[:space:]]*${key}=" "$REPO/.env" || true) | tail -n1 | cut -d= -f2- | tr -d '\r' \
            | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/')"
  fi
  printf '%s' "${val:-$def}"
}

# values baked into the installed backend unit (empty when not installed)
unit_env() {   # unit_env UNIT KEY
  systemctl show -p Environment --value "$1" 2>/dev/null | tr ' ' '\n' | sed -n "s/^$2=//p" | head -n1
}

# ports actually used by the installed services, else the .env / defaults
api_port() { local p; p="$(unit_env "$BACKEND_UNIT" AIV_PORT)"; printf '%s' "${p:-$(env_get AIV_PORT 8000)}"; }
ui_port()  { local p; p="$(unit_env "$FRONTEND_UNIT" PORT)";   printf '%s' "${p:-$(env_get PORT 5173)}"; }

unit_installed() { [[ -f "$UNIT_DIR/$1" ]]; }
unit_ours()      { [[ -f "$UNIT_DIR/$1" ]] && grep -qxF "$MARKER" "$UNIT_DIR/$1"; }

listening_pid() { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2; }
port_in_use()   { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }

# LAN addresses of this box (for the "open in a browser" hints)
box_addresses() { hostname -I 2>/dev/null | tr ' ' '\n' | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | grep -vE '^(127|169\.254)\.' || true; }

# http_status URL [TIMEOUT] -> prints the HTTP status code only (000 = no answer)
http_status() { http_get "$1" "${2:-5}" | head -n1 | cut -f1; }

# http_get URL [TIMEOUT] -> prints "STATUS<TAB>BODY" (python3 is always present on Debian; curl may not be)
http_get() {
  python3 - "$1" "${2:-5}" <<'PY'
import sys, urllib.request, urllib.error
url, timeout = sys.argv[1], float(sys.argv[2])
try:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        body = r.read(200000).decode("utf-8", "replace")
        print(f"{r.status}\t{body}")
except urllib.error.HTTPError as e:
    print(f"{e.code}\t{e.read(2000).decode('utf-8', 'replace')}")
except Exception as e:  # noqa: BLE001
    print(f"000\t{type(e).__name__}: {e}")
PY
}

warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*"; }
VENV="backend/.venv"
LOCK="backend/requirements-lock.txt"
# backend_python - the Python that runs the backend (AIV_PYTHON in .env, else backend/.venv)
backend_python() {
  local py
  py="$(env_get AIV_PYTHON "")"
  if [[ -n "$py" ]]; then printf '%s' "$py"; else printf '%s' "$VENV/bin/python"; fi
}
need_backend_python() {
  local py; py="$(backend_python)"
  if [[ ! -x "$py" ]] && ! command -v "$py" >/dev/null 2>&1; then
    die "The backend has no Python environment yet. Run: ./aivision.sh setup"
  fi
  printf '%s' "$py"
}
need_node() { command -v node >/dev/null 2>&1 || die "Node.js was not found - run ./aivision.sh setup (it installs it)"; }
port_busy() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }
port_pid()  { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2; }

# env_set KEY VALUE - write KEY=VALUE into the project's .env (created from .env.example when missing)
env_set() {
  local k="$1" kv="$1=$2"
  [[ -f "$REPO/.env" ]] || cp "$REPO/.env.example" "$REPO/.env"
  if grep -qE "^[[:space:]]*$k=" "$REPO/.env"; then sed -i "s#^[[:space:]]*$k=.*#$kv#" "$REPO/.env"; else printf '%s\n' "$kv" >> "$REPO/.env"; fi
}

# ---------------------------------------------------------------- Rockchip NPU runtime
# librknnrt.so of rknn-toolkit2 v2.3.2 (same version as the toolkit that built backend/models/*_rknn_model),
# kept in backend/npu/ - the system's own library (e.g. the box vendor's) is not touched.
RKNN_VER="2.3.2"
RKNN_LIB_URL="https://raw.githubusercontent.com/airockchip/rknn-toolkit2/v$RKNN_VER/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so"
RKNN_LIB_SHA="d31fc19c85b85f6091b2bd0f6af9d962d5264a4e410bfb536402ec92bac738e8"
RKNN_LIB="$REPO/backend/npu/librknnrt.so"
rockchip_soc() {   # AIV_RKNN_SOC in the environment overrides the device tree (tests only)
  if [[ -n "${AIV_RKNN_SOC:-}" ]]; then echo "$AIV_RKNN_SOC"; return; fi
  [[ -r /proc/device-tree/compatible ]] || return 0
  tr '\0' ',' < /proc/device-tree/compatible 2>/dev/null | tr ',' '\n' | grep -oE '^(rk3588|rk3576|rk3568|rk3566|rk3562|rv1126b)' | head -n1 || true
}
npu_lib_ok() { [[ -f "$RKNN_LIB" ]] && [[ "$(sha256sum "$RKNN_LIB" | cut -d' ' -f1)" == "$RKNN_LIB_SHA" ]]; }
npu_lib_fetch() {   # npu_lib_fetch [FILE] - install librknnrt.so from FILE, else download it (2 ways)
  npu_lib_ok && return 0
  mkdir -p "$REPO/backend/npu"
  local tmp="$RKNN_LIB.part" src="${1:-}"
  rm -f "$tmp"
  if [[ -n "$src" ]]; then
    cp -f "$src" "$tmp" || { warn "cannot read $src"; return 1; }
  else
    [[ "$(uname -m)" == "aarch64" ]] || { warn "not an aarch64 board - NPU runtime not installed"; return 1; }
    echo "    downloading librknnrt.so $RKNN_VER (7.4 MB) from github.com/airockchip/rknn-toolkit2"
    python3 - "$RKNN_LIB_URL" "$tmp" <<'PY' >/dev/null 2>&1 || rm -f "$tmp"
import sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=120) as r, open(sys.argv[2], "wb") as f:
    f.write(r.read())
PY
    if [[ ! -s "$tmp" ]] && command -v git >/dev/null 2>&1; then      # second way: sparse git checkout
      echo "    direct download failed - trying git"
      local gd; gd="$(mktemp -d)"
      if git clone -q --depth 1 --branch "v$RKNN_VER" --filter=blob:none --sparse \
           https://github.com/airockchip/rknn-toolkit2 "$gd/tk" 2>/dev/null \
         && git -C "$gd/tk" sparse-checkout set rknpu2/runtime/Linux/librknn_api/aarch64 2>/dev/null; then
        cp -f "$gd/tk/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so" "$tmp" 2>/dev/null || true
      fi
      rm -rf "$gd"
    fi
    [[ -s "$tmp" ]] || { rm -f "$tmp"; warn "download failed - check the internet connection, or copy librknnrt.so $RKNN_VER (aarch64) to the box and run: ./aivision.sh npu setup --lib FILE"; return 1; }
  fi
  if [[ "$(sha256sum "$tmp" | cut -d' ' -f1)" != "$RKNN_LIB_SHA" ]]; then
    rm -f "$tmp"; warn "librknnrt.so is not the expected version $RKNN_VER (checksum) - not installed"; return 1
  fi
  mv -f "$tmp" "$RKNN_LIB"
  ok "NPU runtime librknnrt.so $RKNN_VER installed in backend/npu/"
}
# groups that may open the NPU device (/dev/dri/renderD* is group render, older images: video)
npu_groups() { local g out=(); for g in render video; do getent group "$g" >/dev/null && out+=("$g"); done; echo "${out[*]}"; }

cd "$REPO"

usage() {
  case "${1:-}" in
    setup) printf '%s\n' './aivision.sh setup [--reinstall]
Downloads and installs everything the backend and the web UI need (only what is missing):
  1. system packages with apt (Python venv, Node.js, libraries OpenCV needs)
  2. .env from .env.example
  3. backend/.venv (Python 3.13/3.14; on Debian 12 fetches Python 3.13 with uv)
  4. the pinned packages of backend/requirements-lock.txt (about 1 GB, internet),
     or OFFLINE from backend/wheels/linux (backend/tools/download-wheels.sh)
  5. the contrib OpenCV build, then the self-check
  --reinstall   rebuild backend/.venv from scratch
.env / environment:  AIV_TORCH=cpu|cuda   AIV_OPENVINO=off' ;;
    install) printf '%s\n' '==========================================================================================
 PYROTECH VISION - install the backend and the web UI as systemd services (Debian AI box)

     ./aivision.sh install            (run as the normal user; asks for sudo)

 After this the box starts both services at every boot, restarts them if they crash, and
 keeps them running after logout. Open  http://<box-ip>:<ui-port>  from any PC on the LAN.

   pyrotech-vision-backend.service   backend/.venv/bin/python backend/main.py
                                     API on 127.0.0.1:<AIV_PORT, 8000> (not reachable from the LAN)
   pyrotech-vision-frontend.service  node server.js - web UI on 0.0.0.0:<PORT, 5173>, forwards /api
                                     (including the live MJPEG video) to the backend

 Steps: checks -> backend environment (./aivision.sh setup, skipped when up to date) -> unit files
 validated with systemd-analyze -> install + enable -> start -> readiness check -> report.
 Run it again after changing PORT / AIV_PORT in .env, after moving the folder, or after updates.

 Options:
   --user NAME       account that runs the services (default: owner of the project folder)
   --ui-port N       web UI port (default: PORT in .env, else 5173; port 80 is refused)
   --api-port N      backend port on 127.0.0.1 (default: AIV_PORT in .env, else 8000)
   --bind ADDR       address the web UI listens on (default 0.0.0.0 = every network interface)
   --open-firewall   if ufw is active: allow the web UI port (otherwise only printed)
   --skip-setup      do not run ./aivision.sh setup (the environment must already be complete)
   --no-start        install and enable for the next boot, but do not start now
   --allow-root      allow running the services as root (not recommended)
==========================================================================================' ;;
    start) printf '%s\n' 'PYROTECH VISION - start the services now (backend first, then the web UI).
They also start by themselves at every boot once ./aivision.sh install has run.' ;;
    stop) printf '%s\n' 'PYROTECH VISION - stop the services gracefully (web UI first, then the backend).
The backend closes its cameras and saves counts / pending alerts before it exits (up to 60 s).
Detections that were running are resumed at the next start. The services still start at the
next boot; to stop that too: sudo systemctl disable pyrotech-vision-backend pyrotech-vision-frontend' ;;
    restart) printf '%s\n' 'PYROTECH VISION - restart the services (backend first, then the web UI) and wait until they answer.
  ./aivision.sh restart             both
  ./aivision.sh restart backend     only the backend (e.g. after changing .env)
  ./aivision.sh restart frontend    only the web UI' ;;
    status) printf '%s\n' 'PYROTECH VISION - status of the services, step by step:
  service process -> listening ports -> web UI -> backend API -> model files -> per camera:
  connected / AI model loaded / inference running -> detections resumed after restart -> recent errors

  ./aivision.sh status [--wait SECONDS]
    --wait N   wait up to N seconds for the API to answer (used after start / restart)
Exit code 0 = both services active and the API answers, 1 = something is not ready.' ;;
    update) printf '%s\n' '==========================================================================================
 PYROTECH VISION - update the AI box and (re)start the backend + web UI services

     ./aivision.sh update [options]        (run as the normal user; asks for sudo)

 Steps:
   1. backup        .env and the event/camera lists (backend/data/*.json) -> <backup dir>/<time>/
                    (snapshot images are never copied or deleted)
   2. update        git pull --ff-only (refuses - and changes nothing - when the folder has local edits)
   3. data folder   optional --data-dir: move cameras/events/snapshots to a bigger partition (copy, keep the
                    old folder), AIV_DATA_DIR / AIV_SNAPSHOT_DIR written to .env (backed up first)
   4. environment   ./aivision.sh setup only when backend/requirements*.txt or ./aivision.sh setup changed
   5. services      ./aivision.sh install (writes/validates the systemd units, restarts both)
   6. check         backend and web UI health through the real ports, last log lines on failure

 Options:
   --data-dir DIR    keep cameras, events and snapshots in DIR (e.g. /userdata/pyrotech-vision)
   --backup-dir DIR  where backups go (default: /userdata/pyrotech-backups if /userdata exists, else ~/pyrotech-backups)
   --no-pull         do not run git pull (re-install / restart the current version)
   --setup           always run ./aivision.sh setup
   --                everything after this is passed to ./aivision.sh install (e.g. -- --ui-port 5173)
==========================================================================================' ;;
    uninstall) printf '%s\n' 'PYROTECH VISION - remove the systemd services (stop, disable, delete the two unit files).
Only unit files created by ./aivision.sh install are removed. The project folder, backend/.venv,
models, .env, cameras, alerts and snapshots are NOT touched - ./aivision.sh run still works afterwards,
and ./aivision.sh install can install the services again at any time.

  ./aivision.sh uninstall [--close-firewall]
    --close-firewall   also remove the ufw rule for the web UI port (if install added one)' ;;
    check) printf '%s\n' 'PYROTECH VISION - read-only check of the AI box before / after installing the services.
Changes nothing, installs nothing, prints no passwords. Send its output when asking for help.

  ./aivision.sh check' ;;
    run) printf '%s\n' './aivision.sh run [--setup-only] [--reinstall] [--check] [--no-browser]
Runs the application in THIS terminal without services (development / testing):
setup (only missing steps) -> web UI in the background (logs/frontend.log) -> backend in the foreground.
Ctrl+C stops both. Refused while the services are running (./aivision.sh stop first).' ;;
    ai-stream) printf '%s\n' '==========================================================================================
 PYROTECH VISION - choose the stream the AI analyses, per camera (the live view / snapshots keep the
 main stream). Uses the running backend'\''s API on this box; camera passwords are never shown or changed.

     ./aivision.sh ai-stream list
     ./aivision.sh ai-stream sub   CAM-ID [CAM-ID ...] | --all   substream derived from the camera URL
                                                                        (Hikvision /Streaming/Channels/101 -> 102,
                                                                        /h264/ch1/main -> sub, Dahua subtype=0 -> 1)
     ./aivision.sh ai-stream custom CAM-ID /Streaming/Channels/102   any path on the same camera
     ./aivision.sh ai-stream same  CAM-ID [...] | --all             AI uses the viewing stream again

 The camera'\''s running detections reconnect automatically. Snapshot originals stay full resolution
 (snapshotSource = main). Set the substream itself on the camera: H.264, 640x360 or 704x576, 10-15 fps.
==========================================================================================' ;;
    perf) printf '%s\n' 'PYROTECH VISION - performance monitor and CPU optimizer (scripts/perf/pyrotech_perf.py).

  ./aivision.sh perf status              hardware, CPU settings in effect, per-camera performance, findings
  ./aivision.sh perf monitor             live view (timestamps) until Ctrl+C;  --log backend/data/perf/monitor.jsonl
  ./aivision.sh perf auto                start if needed, measure, benchmark, apply the best setting (validated)
  ./aivision.sh perf auto --dry-run      the same, but only recommend
  ./aivision.sh perf bench               benchmark only (idle priority next to the running app)
  ./aivision.sh perf apply --threads 4 --concurrency 2     set values (backup, restart, measure, keep/roll back)
  ./aivision.sh perf rollback            undo the last applied change (restores the .env backup, restarts)
  ./aivision.sh perf history             what was changed and what it measured
  ./aivision.sh perf start | stop | restart
  ./aivision.sh perf <command> --help    all options

Runs with the backend'\''s Python (backend/.venv) at reduced priority, so it never competes with detection.' ;;
    npu) printf '%s\n' 'PYROTECH VISION - Rockchip NPU (RK3588) for Human / Vehicle / Human Count detection.

  ./aivision.sh npu setup         NPU runtime (librknnrt.so 2.3.2 -> backend/npu/), NPU device access for the
                                  service user, NPU engine on, restart, show the engine in use
                                  (no internet: --lib FILE = a copy of librknnrt.so 2.3.2 aarch64)
  ./aivision.sh npu status        engine in use and NPU core / precision per camera
  ./aivision.sh npu check [opts]  full analysis (read-only): system, NPU driver, runtime, models, benchmark,
                                  NPU vs CPU accuracy on your snapshot images -> logs/npu_report_<date>.txt
  ./aivision.sh npu bench [opts]  benchmark only;  --streams 8 = 8 cameras in parallel
  ./aivision.sh npu on | off      use the NPU (AIV_ENGINE=auto) | back to the CPU engine (AIV_ENGINE=openvino)
  ./aivision.sh npu model fp16|int8   FP16 = same accuracy as the CPU (default); INT8 = faster, check accuracy first
  ./aivision.sh npu calib-pack    zip of your snapshot images + live camera frames (logs/npu_calib_<date>.zip)
                                  for an INT8 model calibrated on YOUR cameras

check / bench options: --camera ID (frames from that camera for the accuracy test), --images DIR, --frames N,
  --runs N, --streams N, --seconds S, --only system,runtime,models,bench,pipeline,accuracy  (sudo: NPU load too)
The models are the shipped yolov8n weights converted with scripts/npu/convert_rknn.py (nothing retrained).' ;;
    *) sed -n '2,25p' "$SELF" | sed 's/^# \{0,1\}//' ;;
  esac
}

# ================================================================ setup
cmd_setup() {
REINSTALL=0
for a in "$@"; do
  case "$a" in
    --reinstall) REINSTALL=1 ;;
    -h|--help)   usage setup; exit 0 ;;
    *) die "Unknown option: $a (see ./aivision.sh setup --help)" ;;
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

if [[ -n "$(rockchip_soc)" ]]; then      # Rockchip NPU board: runtime for the RKNN engine (non-fatal)
  say "Rockchip $(rockchip_soc) NPU found - NPU runtime"
  npu_lib_fetch || warn "the NPU engine stays off until ./aivision.sh npu setup succeeds (CPU engine used)"
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
say "Setup complete. Install the services with ./aivision.sh install (or run in this terminal: ./aivision.sh run)"
}

# ================================================================ install
cmd_install() {
SVC_USER=""; UI_PORT=""; API_PORT=""; BIND="0.0.0.0"
OPEN_FW=0; SKIP_SETUP=0; NO_START=0; ALLOW_ROOT=0
while (($#)); do
  case "$1" in
    --user)          SVC_USER="${2:?--user needs a name}"; shift ;;
    --ui-port)       UI_PORT="${2:?--ui-port needs a number}"; shift ;;
    --api-port)      API_PORT="${2:?--api-port needs a number}"; shift ;;
    --bind)          BIND="${2:?--bind needs an address}"; shift ;;
    --open-firewall) OPEN_FW=1 ;;
    --skip-setup)    SKIP_SETUP=1 ;;
    --no-start)      NO_START=1 ;;
    --allow-root)    ALLOW_ROOT=1 ;;
    -h|--help)       usage install; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
  shift
done

[[ "$(uname -s)" == "Linux" ]] || die "Linux only."
command -v systemctl >/dev/null 2>&1 || die "systemctl not found - this script needs a systemd-based Debian."
[[ -d /run/systemd/system ]] || die "systemd is not running as the init system on this machine."

say "Checking the project in $REPO"
FAIL=0
for f in backend/main.py server.js index.html .env.example backend/requirements-lock.txt aivision.sh \
         backend/models/yolov8n.pt backend/models/bytetrack_stable.yaml \
         backend/models/fire_smoke/firedetect-11s.pt backend/models/object_monitor/yoloe-26n-seg.pt; do
  if [[ -e "$f" ]]; then ok "$f"; else bad "$f is missing"; FAIL=1; fi
done
((FAIL == 0)) || die "Required files are missing - is this a complete clone of the repository?"
case "$REPO" in
  *%*|*\"*|*\\*|*$'\n'*) die "The project path contains characters systemd cannot use (% \" \\ newline): $REPO" ;;
esac

# ---------------------------------------------------------------- service account
if [[ -z "$SVC_USER" ]]; then SVC_USER="$(stat -c %U "$REPO")"; fi
id "$SVC_USER" >/dev/null 2>&1 || die "User '$SVC_USER' does not exist."
if [[ "$SVC_USER" == "root" && $ALLOW_ROOT -eq 0 ]]; then
  die "The project folder belongs to root. Run the services as a normal user: --user <name>
       (that user must own the folder: sudo chown -R <name>: \"$REPO\"), or pass --allow-root."
fi
SVC_GROUP="$(id -gn "$SVC_USER")"
NPU_GROUPS=""
[[ -n "$(rockchip_soc)" ]] && NPU_GROUPS="$(npu_groups)"     # NPU device access for the backend service
ok "services run as user '$SVC_USER' (group '$SVC_GROUP')"

as_user() {   # run a command as the service account
  if [[ "$(id -un)" == "$SVC_USER" ]]; then "$@"
  elif [[ $EUID -eq 0 ]]; then runuser -u "$SVC_USER" -- "$@"
  else sudo -u "$SVC_USER" -H -- "$@"
  fi
}
for d in "$REPO" "$REPO/backend" "$REPO/backend/models/head_yolov8m"; do
  as_user test -r "$d" -a -x "$d" || die "'$SVC_USER' cannot read $d"
done
for d in "$REPO/backend/data" "$REPO/backend/snapshots" "$REPO/backend/models/head_yolov8m" "$REPO/backend/models/head_yolov8m/head_yolov8m_openvino_model"; do
  [[ -d "$d" ]] || as_user mkdir -p "$d"
  as_user test -w "$d" || die "'$SVC_USER' cannot write to $d (cameras, alerts, snapshots, joined head model).
       Fix: sudo chown -R $SVC_USER: \"$REPO\""
done
ok "'$SVC_USER' can read the project and write backend/data, backend/snapshots and the head model folder"

# ---------------------------------------------------------------- .env and ports
if [[ ! -f .env ]]; then
  as_user cp .env.example .env
  ok "created .env from .env.example"
fi
as_user test -r .env || die "'$SVC_USER' cannot read .env"
ok ".env present (values are not printed)"

UI_PORT="${UI_PORT:-$(env_get PORT 5173)}"
API_PORT="${API_PORT:-$(env_get AIV_PORT 8000)}"
for p in "$UI_PORT" "$API_PORT"; do
  [[ "$p" =~ ^[0-9]+$ ]] && (( p >= 1 && p <= 65535 )) || die "Invalid port: $p"
done
(( UI_PORT != 80 )) || die "Port 80 is reserved on this box - choose another web UI port (--ui-port 5173)."
(( UI_PORT != API_PORT )) || die "Web UI port and backend port must differ ($UI_PORT)."
(( UI_PORT >= 1024 )) || die "Ports below 1024 need root; the services run as '$SVC_USER'. Use a port >= 1024."
[[ "$BIND" =~ ^[0-9.]+$|^::$|^0\.0\.0\.0$ ]] || die "--bind must be an IP address (e.g. 0.0.0.0)."
ok "web UI port $UI_PORT on $BIND, backend 127.0.0.1:$API_PORT (LAN access only through the web UI)"

# a backend or web UI started by hand (./aivision.sh run) would block the ports - and would be a second copy
for p in "$API_PORT" "$UI_PORT"; do
  if port_in_use "$p"; then
    pid="$(listening_pid "$p")"
    owner_unit=""
    [[ -n "$pid" ]] && owner_unit="$(ps -o unit= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [[ "$owner_unit" == "$BACKEND_UNIT" || "$owner_unit" == "$FRONTEND_UNIT" ]]; then
      info "port $p is used by $owner_unit (will be restarted)"
    else
      die "Port $p is already in use${pid:+ by PID $pid ($(ps -o args= -p "$pid" 2>/dev/null | cut -c1-70))}.
       Stop that program first (a backend or web UI started by hand with ./aivision.sh run: press Ctrl+C there),
       or choose another port with --ui-port / --api-port."
    fi
  fi
done

# ---------------------------------------------------------------- runtimes
NODE="$(command -v node || true)"
if [[ -z "$NODE" && $SKIP_SETUP -eq 1 ]]; then die "Node.js not found (sudo apt install nodejs)."; fi

say "Preparing the backend environment"
if (( SKIP_SETUP )); then
  info "./aivision.sh setup skipped (--skip-setup)"
else
  # installs only what is missing (apt packages, backend/.venv, pinned packages, self-check);
  # a complete environment is left untouched - nothing is deleted or downgraded
  as_user "$SELF" setup
fi
NODE="$(command -v node || true)"
[[ -n "$NODE" ]] || die "Node.js not found (sudo apt install nodejs)."
NODE="$(readlink -f "$NODE")"
NODE_MAJOR="$("$NODE" -p 'process.versions.node.split(".")[0]')"
(( NODE_MAJOR >= 16 )) || die "Node.js 16 or newer is needed (found $("$NODE" -v))."
ok "Node.js $("$NODE" -v) at $NODE (no npm packages needed)"

PYV="$(env_get AIV_PYTHON "")"
PY="${PYV:-$VENV_PY}"
[[ -x "$PY" ]] || die "Backend Python not found: $PY - run ./aivision.sh setup"
PY="$(cd "$(dirname "$PY")" && pwd -P)/$(basename "$PY")"     # absolute, venv symlink kept
as_user "$PY" -c 'import ultralytics, torch, cv2, numpy; assert hasattr(cv2, "TrackerCSRT_create")' \
  || die "The backend environment is incomplete ($PY cannot import ultralytics/torch/OpenCV contrib).
       Run ./aivision.sh setup  and check its messages."
ok "backend Python $("$PY" -V 2>&1 | cut -d' ' -f2): ultralytics, torch, OpenCV contrib import"
if as_user "$PY" -c 'import openvino' 2>/dev/null; then ok "OpenVINO available (faster CPU inference)"; else note "OpenVINO not installed - detection uses PyTorch (slower, works)"; fi

# ---------------------------------------------------------------- unit files
say "Writing the systemd unit files"
q() { local s="${1//%/%%}"; printf '"%s"' "$s"; }     # quote a path for ExecStart
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
BACKEND_FILE="$TMP/$BACKEND_UNIT"; FRONTEND_FILE="$TMP/$FRONTEND_UNIT"

cat > "$BACKEND_FILE" <<EOF
$MARKER
# Re-run ./aivision.sh install to change it; edits here are overwritten.
[Unit]
Description=PYROTECH VISION backend (detection API: human, vehicle, human count, fire & smoke, object)
Documentation=file://${REPO// /%20}/docs/DEBIAN_AI_BOX_DEPLOYMENT.md
# cameras are on the network; the backend itself retries cameras that are not reachable yet
After=network-online.target
Wants=network-online.target
# never give up restarting (each failure is logged in the journal)
StartLimitIntervalSec=0

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_GROUP
WorkingDirectory=${REPO//%/%%}
Environment=PYTHONUNBUFFERED=1
Environment=AIV_HOST=127.0.0.1
Environment=AIV_PORT=$API_PORT
${NPU_GROUPS:+SupplementaryGroups=$NPU_GROUPS}
ExecStart=$(q "$PY") $(q "$REPO/backend/main.py")
# SIGINT = the backend's clean shutdown (closes cameras, saves counts / pending alerts)
KillSignal=SIGINT
TimeoutStopSec=60
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pyrotech-vision-backend
NoNewPrivileges=true
UMask=0027

[Install]
WantedBy=multi-user.target
EOF

cat > "$FRONTEND_FILE" <<EOF
$MARKER
# Re-run ./aivision.sh install to change it; edits here are overwritten.
[Unit]
Description=PYROTECH VISION web UI (serves the operator console, forwards /api to the backend)
Documentation=file://${REPO// /%20}/docs/DEBIAN_AI_BOX_DEPLOYMENT.md
After=network-online.target
Wants=network-online.target
# independent of the backend: the UI stays reachable and shows "backend not reachable" meanwhile
StartLimitIntervalSec=0

[Service]
Type=simple
User=$SVC_USER
Group=$SVC_GROUP
WorkingDirectory=${REPO//%/%%}
Environment=NODE_ENV=production
Environment=HOST=$BIND
Environment=PORT=$UI_PORT
Environment=BACKEND_URL=http://127.0.0.1:$API_PORT/api
ExecStart=$(q "$NODE") $(q "$REPO/server.js")
TimeoutStopSec=15
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=pyrotech-vision-frontend
NoNewPrivileges=true
UMask=0027

[Install]
WantedBy=multi-user.target
EOF

if command -v systemd-analyze >/dev/null 2>&1; then
  if out="$(systemd-analyze verify "$BACKEND_FILE" "$FRONTEND_FILE" 2>&1)"; then
    ok "unit files pass systemd-analyze verify"
  else
    # only report problems that concern our two units
    rel="$(printf '%s\n' "$out" | grep -E 'pyrotech-vision-(backend|frontend)' || true)"
    if [[ -n "$rel" ]]; then printf '%s\n' "$rel"; die "systemd rejected the generated unit files (see above)."; fi
    ok "unit files pass systemd-analyze verify"
  fi
fi

for u in "${UNITS[@]}"; do
  if unit_installed "$u" && ! unit_ours "$u"; then
    die "$UNIT_DIR/$u exists but was not created by this installer - not overwritten. Rename or remove it first."
  fi
done
as_root install -m 0644 -o root -g root "$BACKEND_FILE" "$UNIT_DIR/$BACKEND_UNIT"
as_root install -m 0644 -o root -g root "$FRONTEND_FILE" "$UNIT_DIR/$FRONTEND_UNIT"
as_root systemctl daemon-reload
as_root systemctl enable "${UNITS[@]}" >/dev/null 2>&1
ok "installed and enabled for every boot: ${UNITS[*]}"

# ---------------------------------------------------------------- firewall
if [[ -n "$UFW" ]] && as_root "$UFW" status 2>/dev/null | grep -q '^Status: active'; then
  if as_root "$UFW" status 2>/dev/null | grep -qE "^$UI_PORT(/tcp)?[[:space:]]+ALLOW"; then
    ok "ufw allows port $UI_PORT"
  elif (( OPEN_FW )); then
    as_root "$UFW" allow "$UI_PORT/tcp" comment 'PYROTECH VISION web UI' >/dev/null
    ok "ufw: allowed port $UI_PORT/tcp"
  else
    note "ufw is active and does not allow port $UI_PORT - other PCs cannot connect yet. Run:
         sudo ufw allow $UI_PORT/tcp     (or re-run with --open-firewall)"
  fi
else
  info "no active ufw firewall found (check other firewalls: sudo nft list ruleset)"
fi

# ---------------------------------------------------------------- start + readiness
if (( NO_START )); then
  say "Installed. The services start at the next boot (or now with ./aivision.sh start)."
  exit 0
fi
say "Starting the services"
as_root systemctl restart "$BACKEND_UNIT"
as_root systemctl restart "$FRONTEND_UNIT"

ready_ui=0; ready_api=0
for _ in $(seq 1 60); do
  [[ $ready_ui -eq 1 ]] || { [[ "$(http_status "http://127.0.0.1:$UI_PORT/" 3)" == "200" ]] && ready_ui=1; }
  [[ $ready_api -eq 1 ]] || { [[ "$(http_status "http://127.0.0.1:$UI_PORT/api/system/health" 3)" == "200" ]] && ready_api=1; }
  (( ready_ui && ready_api )) && break
  sleep 1
done
if (( ready_ui )); then ok "web UI answers on port $UI_PORT"; else bad "web UI does not answer - journalctl -u $FRONTEND_UNIT -n 50"; fi
if (( ready_api )); then ok "backend API answers through the web UI (/api/system/health)"; else bad "backend API does not answer yet - journalctl -u $BACKEND_UNIT -n 50"; fi

say "Done"
echo "  Open the console from any PC on the network:"
mapfile -t ADDRS < <(box_addresses)
if ((${#ADDRS[@]})); then for a in "${ADDRS[@]}"; do echo "    http://$a:$UI_PORT"; done; else echo "    http://<box-ip>:$UI_PORT"; fi
echo "  Status: ./aivision.sh status     Logs: journalctl -u $BACKEND_UNIT -f"
(( ready_ui && ready_api )) || exit 1
}

# ================================================================ start
cmd_start() {
for u in "${UNITS[@]}"; do unit_installed "$u" || die "$u is not installed - run ./aivision.sh install"; done
say "Starting $BACKEND_UNIT"
as_root systemctl start "$BACKEND_UNIT"
say "Starting $FRONTEND_UNIT"
as_root systemctl start "$FRONTEND_UNIT"
sleep 2
exec "$SELF" status --wait 60
}

# ================================================================ stop
cmd_stop() {
for u in "$FRONTEND_UNIT" "$BACKEND_UNIT"; do
  if ! unit_installed "$u"; then info "$u is not installed"; continue; fi
  say "Stopping $u"
  as_root systemctl stop "$u"
  ok "$u: $(systemctl is-active "$u" 2>/dev/null || true)"
done
}

# ================================================================ restart
cmd_restart() {
case "${1:-all}" in
  all)      targets=("$BACKEND_UNIT" "$FRONTEND_UNIT") ;;
  backend)  targets=("$BACKEND_UNIT") ;;
  frontend) targets=("$FRONTEND_UNIT") ;;
  -h|--help) usage restart; exit 0 ;;
  *) die "Usage: restart-services.sh [all|backend|frontend]" ;;
esac
for u in "${targets[@]}"; do
  unit_installed "$u" || die "$u is not installed - run ./aivision.sh install"
  say "Restarting $u"
  as_root systemctl restart "$u"
done
sleep 2
exec "$SELF" status --wait 60
}

# ================================================================ status
cmd_status() {
WAIT=0
while (($#)); do
  case "$1" in
    --wait) WAIT="${2:?--wait needs seconds}"; shift ;;
    -h|--help) usage status; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

UI_PORT="$(ui_port)"; API_PORT="$(api_port)"
RC=0

say "1. Service processes"
for u in "${UNITS[@]}"; do
  if ! unit_installed "$u"; then bad "$u is not installed (./aivision.sh install)"; RC=1; continue; fi
  active="$(systemctl is-active "$u" 2>/dev/null || true)"
  enabled="$(systemctl is-enabled "$u" 2>/dev/null || true)"
  pid="$(systemctl show -p MainPID --value "$u")"
  restarts="$(systemctl show -p NRestarts --value "$u" 2>/dev/null || echo '?')"
  since="$(systemctl show -p ActiveEnterTimestamp --value "$u")"
  mem="$(systemctl show -p MemoryCurrent --value "$u" 2>/dev/null || true)"
  [[ "$mem" =~ ^[0-9]+$ ]] && mem="$((mem / 1048576)) MB" || mem="-"
  line="$u: $active, boot start: $enabled, PID ${pid:-0}, memory $mem, automatic restarts: ${restarts:-0}, since ${since:--}"
  if [[ "$active" == "active" ]]; then ok "$line"; else bad "$line"; RC=1; fi
  [[ "$enabled" == "enabled" ]] || note "$u does not start at boot (sudo systemctl enable $u)"
done

if (( WAIT > 0 )); then
  for _ in $(seq 1 "$WAIT"); do
    [[ "$(http_status "http://127.0.0.1:$API_PORT/api/system/health" 2)" == "200" ]] && break
    sleep 1
  done
fi

say "2. Listening ports"
ui_l="$(ss -ltnH "sport = :$UI_PORT" 2>/dev/null | awk '{print $4}' | paste -sd' ' -)"
api_l="$(ss -ltnH "sport = :$API_PORT" 2>/dev/null | awk '{print $4}' | paste -sd' ' -)"
if [[ -n "$ui_l" ]]; then ok "web UI port $UI_PORT: $ui_l"; else bad "nothing listens on web UI port $UI_PORT"; RC=1; fi
if [[ -n "$api_l" ]]; then
  ok "backend port $API_PORT: $api_l"
  [[ "$api_l" == *127.0.0.1:* ]] || note "the backend listens beyond 127.0.0.1 - it should only be reached through the web UI"
else bad "nothing listens on backend port $API_PORT"; RC=1; fi

say "3. Web UI, API and detection state"
python3 - "$UI_PORT" "$API_PORT" <<'PY' || RC=1
import json, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone
ui, api = sys.argv[1], sys.argv[2]
OK, FAIL, WARN, INFO = "  \033[1;32m[ OK ]\033[0m", "  \033[1;31m[FAIL]\033[0m", "  \033[1;33m[WARN]\033[0m", "  [INFO]"
rc = 0

def get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode()

st, _ = get(f"http://127.0.0.1:{ui}/")
if st == 200: print(OK, f"web UI answers (http://127.0.0.1:{ui}/)")
else: print(FAIL, f"web UI does not answer (HTTP {st})"); rc = 1

st, body = get(f"http://127.0.0.1:{api}/api/system/health")
health = None
if st == 200:
    health = json.loads(body)
    print(OK, f"backend API answers - version {health.get('version')}, engine {health.get('engine')}")
else:
    print(FAIL, f"backend API does not answer (HTTP {st}) - still starting? journalctl -u pyrotech-vision-backend -n 50"); rc = 1
st2, _ = get(f"http://127.0.0.1:{ui}/api/system/health")
if st2 == 200: print(OK, "web UI forwards /api to the backend (what browsers on other PCs use)")
elif health: print(FAIL, f"web UI cannot reach the backend (HTTP {st2}) - BACKEND_URL / AIV_PORT mismatch? re-run ./aivision.sh install"); rc = 1

if health:
    if health.get("modelAvailable"): print(OK, f"model files present ({health.get('modelFile')}; models load when a detection starts)")
    else: print(FAIL, f"model file missing: {health.get('modelFile')}"); rc = 1
    st, body = get(f"http://127.0.0.1:{api}/api/cameras")
    cams = {c["id"]: c for c in (json.loads(body) if st == 200 else [])}
    st, body = get(f"http://127.0.0.1:{api}/api/system/metrics")
    met = json.loads(body) if st == 200 else {}
    sessions = met.get("sessions", [])
    if met.get("processCpu") is not None:
        cores = met.get("cpuCores"); threads = met.get("cpuThreads")
        print(INFO, f"backend process: CPU {met['processCpu']}% of one core ({cores} cores / {threads} threads on this box), "
                    f"RAM {met.get('processRssMb')} MB; box RAM free {met.get('ramAvailableMb')} MB")
    rt = met.get("runtime") or {}
    if rt:
        print(INFO, f"CPU settings in effect: PyTorch intra-op {rt.get('torchThreads') if rt.get('torchThreads') is not None else '(no model loaded yet)'}"
                    f", inter-op {rt.get('torchInteropThreads') if rt.get('torchInteropThreads') is not None else '-'}"
                    f", inference slots {rt.get('inferConcurrency') or 'unlimited'}, CPUs {rt.get('cpuAffinity')}"
                    f" (tune: ./aivision.sh perf)")
        for e in rt.get("errors") or []:
            print(WARN, f"CPU setting: {e}")
    print(INFO, f"{len(cams)} camera(s) configured, {len(sessions)} open")
    now = datetime.now(timezone.utc)
    for s in sessions:
        cid, state, mods = s.get("cameraId"), s.get("state"), ", ".join(s.get("modules") or []) or "-"
        name = (cams.get(cid) or {}).get("name", "")
        age = None
        if s.get("lastFrameAt"):
            try: age = (now - datetime.fromisoformat(s["lastFrameAt"])).total_seconds()
            except ValueError: age = None
        connected = state in ("connected", "processing") and age is not None and age < 15
        inferring = state == "processing" and (s.get("framesProcessed") or 0) > 0 and connected
        tag = OK if inferring else (WARN if connected else FAIL)
        res = f" ({s['resolution']})" if s.get("resolution") else ""
        model = f"model loaded ({s['engine']})" if s.get("engine") else "model loaded per module"
        infer = f"running {s.get('processingFps') or 0} fps" if inferring else "NOT running"
        detail = (f"camera {'connected' if connected else 'NOT connected'}{res}, AI {model}, "
                  f"inference {infer}, frames analysed {s.get('framesProcessed') or 0}")
        print(tag, f"{cid} {name} [{mods}]: state {state} - {detail}")
        ai = s.get("aiStream") or {}
        extra = [f"AI stream {ai.get('active', 'same')} {s.get('aiResolution') or ''}".strip()]
        if ai.get("requested") not in (None, "same") and ai.get("active") == "main":
            extra.append(f"FALLBACK to main ({ai.get('reason')})")
        if s.get("queueCapacity"):
            extra.append(f"queue {s.get('queueLen', 0)}/{s['queueCapacity']}, latency {s.get('latencyMs')} ms")
        extra.append(f"dropped {s.get('framesDropped', 0)} (app), {s.get('cameraGaps', 0)} missing in stream")
        if s.get("fireAnalysedFps") is not None:
            extra.append(f"fire model {s['fireAnalysedFps']} fps")
        stg = s.get("stageMs") or {}
        if stg.get("inference") is not None:
            extra.append(f"model {stg['inference']} ms + tracking {stg.get('tracking')} ms per frame")
        print("        " + " | ".join(extra))
    if not sessions:
        print(INFO, "no detection running (start one in the web UI; running detections resume after a restart)")
    a = health.get("autostart") or {}
    if a:
        rem = a.get("remembered") or []
        if not a.get("enabled"): print(INFO, "resume after restart: off (AIV_AUTOSTART=0)")
        elif rem:
            for r in rem:
                t = OK if r.get("status") == "running" else (FAIL if r.get("status") == "failed" else WARN)
                msg = f" - {r['message']}" if r.get("message") and r.get("status") != "running" else ""
                print(t, f"resume after restart: {r['module']} on {r['cameraId']}: {r.get('status')}{msg}")
        else: print(INFO, "resume after restart: nothing to resume")
sys.exit(rc)
PY

say "4. Recent backend warnings and errors (last hour)"
# the backend logs to stdout (journal priority "info"), so match the log level text
if out="$(journalctl -u "$BACKEND_UNIT" --since "-1h" --no-pager -q -o cat 2>&1)"; then
  out="$(printf '%s\n' "$out" | grep -E ' (WARNING|ERROR|CRITICAL) |Traceback' | grep -v 'DPAPI unavailable' | tail -n 8 || true)"
  if [[ -n "$out" ]]; then printf '%s\n' "$out" | sed 's/^/  /'; else info "none"; fi
else
  info "no permission to read the journal - run with sudo, or: sudo usermod -aG systemd-journal $(id -un)"
fi

say "Access"
mapfile -t ADDRS < <(box_addresses)
for a in "${ADDRS[@]}"; do echo "  http://$a:$UI_PORT"; done
((${#ADDRS[@]})) || echo "  http://<box-ip>:$UI_PORT"
echo "  Logs: journalctl -u $BACKEND_UNIT -f    journalctl -u $FRONTEND_UNIT -f"
exit "$RC"
}

# ================================================================ update
cmd_update() {
DATA_DIR=""; BACKUP_DIR=""; PULL=1; FORCE_SETUP=0; PASS=()
while (($#)); do
  case "$1" in
    --data-dir)   DATA_DIR="${2:?--data-dir needs a folder}"; shift ;;
    --backup-dir) BACKUP_DIR="${2:?--backup-dir needs a folder}"; shift ;;
    --no-pull)    PULL=0 ;;
    --setup)      FORCE_SETUP=1 ;;
    --)           shift; PASS=("$@"); break ;;
    -h|--help)    usage update; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
  shift
done
[[ $EUID -ne 0 ]] || die "Run as the normal user that owns $REPO (the script asks for sudo where needed)."
(( PULL == 0 )) || command -v git >/dev/null || die "git is not installed (sudo apt install git) - or use --no-pull."

TS="$(date +%Y%m%d-%H%M%S)"
if [[ -z "$BACKUP_DIR" ]]; then
  if [[ -d /userdata && -w /userdata ]] || as_root test -d /userdata 2>/dev/null; then BACKUP_DIR=/userdata/pyrotech-backups
  else BACKUP_DIR="$HOME/pyrotech-backups"; fi
fi

# ---------------------------------------------------------------- 1. backup
say "1/6 Backup -> $BACKUP_DIR/$TS"
if ! mkdir -p "$BACKUP_DIR/$TS" 2>/dev/null; then          # sudo only when the folder is not writable
  as_root mkdir -p "$BACKUP_DIR/$TS"
  as_root chown "$(id -un):$(id -gn)" "$BACKUP_DIR" "$BACKUP_DIR/$TS"
fi
chmod 700 "$BACKUP_DIR/$TS"
[[ -f .env ]] && cp -p .env "$BACKUP_DIR/$TS/.env" && ok ".env saved"
# the backend's own rules (backend/config.py): relative paths are relative to backend/; default data =
# backend/data, default snapshot images = backend/snapshots (NOT inside the data folder)
abs_backend() { case "$1" in /*) printf '%s' "$1" ;; *) printf '%s' "$REPO/backend/$1" ;; esac; }
CUR_DATA="$(abs_backend "$(env_get AIV_DATA_DIR "$REPO/backend/data")")"
CUR_SNAP="$(abs_backend "$(env_get AIV_SNAPSHOT_DIR "$REPO/backend/snapshots")")"
if [[ -d "$CUR_DATA" ]]; then
  mkdir -p "$BACKUP_DIR/$TS/data"
  find "$CUR_DATA" -maxdepth 1 -name '*.json' -exec cp -p {} "$BACKUP_DIR/$TS/data/" \; 2>/dev/null || true
  ok "camera / event lists saved ($(ls "$BACKUP_DIR/$TS/data" | wc -l) files from $CUR_DATA; snapshot images not copied)"
fi
git rev-parse HEAD > "$BACKUP_DIR/$TS/git-commit.txt" 2>/dev/null || true
info "previous version: $(git log --oneline -1 2>/dev/null || echo unknown)"

# ---------------------------------------------------------------- 2. update
say "2/6 Update the code"
REQ_BEFORE="$(cat backend/requirements*.txt "$SELF" 2>/dev/null | sha256sum | cut -c1-16)"
if (( PULL )); then
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    git status --short --untracked-files=no | head -20
    die "The project folder has local changes (listed above) - nothing was updated.
       Keep them:    git stash        (then run this script again; 'git stash pop' brings them back)
       Discard them: git checkout -- <file>"
  fi
  git pull --ff-only || die "git pull failed (network, or the branch has diverged) - nothing was changed."
  ok "now at: $(git log --oneline -1)"
else
  info "--no-pull: keeping $(git log --oneline -1 2>/dev/null || echo "the current files")"
fi
REQ_AFTER="$(cat backend/requirements*.txt "$SELF" 2>/dev/null | sha256sum | cut -c1-16)"

# ---------------------------------------------------------------- 3. data folder
say "3/6 Data folder"
if [[ -n "$DATA_DIR" ]]; then
  DATA_DIR="$(realpath -m "$DATA_DIR")"
  as_root mkdir -p "$DATA_DIR/snapshots"
  as_root chown -R "$(stat -c %U "$REPO"):$(stat -c %G "$REPO")" "$DATA_DIR"
  if [[ "$CUR_DATA" != "$DATA_DIR" && -d "$CUR_DATA" ]]; then
    info "copying $CUR_DATA -> $DATA_DIR (the old folder is kept; delete it yourself once everything works)"
    as_root systemctl stop "$BACKEND_UNIT" 2>/dev/null || true      # no writes while copying
    cp -a -n "$CUR_DATA/." "$DATA_DIR/" || die "copying the data failed - .env NOT changed, nothing switched"
    # verify: every camera / event list must have arrived (an existing EMPTY list in the target is replaced)
    for f in "$CUR_DATA"/*.json; do
      [[ -f "$f" ]] || continue
      t="$DATA_DIR/$(basename "$f")"
      if ! cmp -s "$f" "$t"; then
        if [[ "$(stat -c %s "$t" 2>/dev/null || echo 0)" -le 2 ]]; then cp -a "$f" "$t"
        else note "$(basename "$f") differs in both folders - kept the one in $DATA_DIR (old one in $CUR_DATA)"; fi
      fi
    done
    [[ ! -f "$CUR_DATA/cameras.json" || -s "$DATA_DIR/cameras.json" ]] || die "cameras.json did not arrive - .env NOT changed"
    ok "cameras / events copied ($(ls "$DATA_DIR"/*.json 2>/dev/null | wc -l) files)"
  fi
  if [[ -d "$CUR_SNAP" && "$CUR_SNAP" != "$DATA_DIR/snapshots" ]]; then
    info "copying snapshot images $CUR_SNAP -> $DATA_DIR/snapshots (old folder kept)"
    cp -a -n "$CUR_SNAP/." "$DATA_DIR/snapshots/" || die "copying the snapshot images failed - .env NOT changed"
    ok "snapshot images copied ($(du -sh "$DATA_DIR/snapshots" | cut -f1))"
  fi
  [[ -f .env ]] || cp .env.example .env
  cp -p .env "$BACKUP_DIR/$TS/.env.before-data-dir"
  for kv in "AIV_DATA_DIR=$DATA_DIR" "AIV_SNAPSHOT_DIR=$DATA_DIR/snapshots"; do
    k="${kv%%=*}"
    if grep -qE "^[[:space:]]*$k=" .env; then sed -i "s#^[[:space:]]*$k=.*#$kv#" .env; else printf '%s\n' "$kv" >> .env; fi
  done
  ok "data: $DATA_DIR (free: $(df -h --output=avail "$DATA_DIR" | tail -1 | tr -d ' '))"
else
  info "unchanged: $CUR_DATA (free: $(df -h --output=avail "$CUR_DATA" 2>/dev/null | tail -1 | tr -d ' ' || echo ?))"
  ROOT_USE="$(df --output=pcent / | tail -1 | tr -dc '0-9')"
  if (( ROOT_USE >= 90 )) && [[ -d /userdata ]]; then
    note "the root partition is ${ROOT_USE}% full - consider:  ./aivision.sh update --data-dir /userdata/pyrotech-vision"
  fi
fi

# ---------------------------------------------------------------- 4 + 5. environment and services
say "4/6 Backend environment"
INSTALL_ARGS=("${PASS[@]}")
if (( FORCE_SETUP )) || [[ "$REQ_BEFORE" != "$REQ_AFTER" ]] || [[ ! -x backend/.venv/bin/python ]]; then
  info "requirements or setup changed - ./aivision.sh setup will run (installs only what is missing)"
else
  INSTALL_ARGS+=("--skip-setup")
  ok "requirements unchanged - environment kept"
fi

say "5/6 Services"
"$SELF" install "${INSTALL_ARGS[@]}"
as_root systemctl restart "$BACKEND_UNIT" "$FRONTEND_UNIT"
ok "restarted $BACKEND_UNIT and $FRONTEND_UNIT"

# ---------------------------------------------------------------- 6. check
say "6/6 Health check"
API="http://127.0.0.1:$(api_port)/api/system/health"
UI="http://127.0.0.1:$(ui_port)/api/system/health"
okb=0; oku=0
for _ in $(seq 1 60); do
  [[ "$(http_status "$API" 3)" == "200" ]] && okb=1
  [[ "$(http_status "$UI" 3)" == "200" ]] && oku=1
  (( okb && oku )) && break
  sleep 1
done
if (( okb )); then ok "backend answers ($API)"; else bad "backend does not answer"; as_root journalctl -u "$BACKEND_UNIT" -n 30 --no-pager | sed -E 's#://[^/@ ]*@#://***@#g'; fi
if (( oku )); then ok "web UI answers and reaches the backend"; else bad "web UI does not answer"; as_root journalctl -u "$FRONTEND_UNIT" -n 20 --no-pager; fi
for a in $(box_addresses); do info "open:  http://$a:$(ui_port)"; done
info "backup of this update: $BACKUP_DIR/$TS   (roll back: git checkout \$(cat $BACKUP_DIR/$TS/git-commit.txt); cp $BACKUP_DIR/$TS/.env .env; ./aivision.sh update --no-pull)"
(( okb && oku )) || exit 1
}

# ================================================================ uninstall
cmd_uninstall() {
CLOSE_FW=0
for a in "$@"; do
  case "$a" in
    --close-firewall) CLOSE_FW=1 ;;
    -h|--help) usage uninstall; exit 0 ;;
    *) die "Unknown option: $a" ;;
  esac
done

UI_PORT="$(ui_port)"
say "Removing the PYROTECH VISION services (project data is kept)"
removed=0
for u in "$FRONTEND_UNIT" "$BACKEND_UNIT"; do
  if ! unit_installed "$u"; then info "$u is not installed"; continue; fi
  if ! unit_ours "$u"; then note "$UNIT_DIR/$u was not created by ./aivision.sh install - left untouched"; continue; fi
  as_root systemctl disable --now "$u" >/dev/null 2>&1 || true
  as_root rm -f "$UNIT_DIR/$u"
  ok "stopped, disabled and removed $u"
  removed=1
done
if (( removed )); then
  as_root systemctl daemon-reload
  as_root systemctl reset-failed "${UNITS[@]}" >/dev/null 2>&1 || true
fi

if (( CLOSE_FW )) && [[ -n "$UFW" ]]; then
  if as_root "$UFW" delete allow "$UI_PORT/tcp" >/dev/null 2>&1; then ok "ufw rule for port $UI_PORT/tcp removed"; else info "no ufw rule for port $UI_PORT/tcp"; fi
fi

echo
echo "  Kept: $REPO (code, backend/.venv, models, .env, backend/data, backend/snapshots)"
echo "  Start by hand: ./aivision.sh run      Install the services again: ./aivision.sh install"
}

# ================================================================ check
cmd_check() {
set +e
PROBLEMS=0
problem() { bad "$*"; PROBLEMS=$((PROBLEMS + 1)); }

say "System"
if [[ -r /etc/os-release ]]; then
  # shellcheck disable=SC1091
  . /etc/os-release
  info "OS: ${PRETTY_NAME:-unknown} (Debian version file: $(cat /etc/debian_version 2>/dev/null || echo -))"
fi
ARCH="$(uname -m)"
GLIBC="$(ldd --version 2>/dev/null | head -n1 | grep -oE '[0-9]+\.[0-9]+$' || echo '?')"
info "kernel $(uname -r), CPU architecture $ARCH, glibc $GLIBC"
CPU_MODEL="$(lscpu 2>/dev/null | sed -n 's/^Model name:[[:space:]]*//p' | head -n1)"
TPC="$(lscpu 2>/dev/null | sed -n 's/^Thread(s) per core:[[:space:]]*//p' | head -n1)"
info "CPU: ${CPU_MODEL:-unknown}, $(nproc) logical CPUs, ${TPC:-?} thread(s) per core"
if [[ "${TPC:-1}" -ge 2 ]]; then
  note "2 threads per core: OpenVINO and PyTorch use the physical cores only, so 'top' shows about 50% CPU even when
         the AI is fully busy. Judge the load by the per-camera 'processed fps' / queue in status-services.sh."
fi
case "$ARCH" in
  x86_64|aarch64) ok "64-bit architecture ($ARCH) - PyTorch wheels exist" ;;
  *) problem "architecture $ARCH: no PyTorch wheels (needs x86_64 or aarch64)" ;;
esac
read -r _ total _ _ _ _ avail < <(free -m | awk '/^Mem:/{print $1,$2,$3,$4,$5,$6,$7}')
info "RAM: ${total:-?} MB total, ${avail:-?} MB available"
(( ${total:-0} >= 3500 )) || note "less than 4 GB RAM - run few detections at once"
DF="$(df -Pm "$REPO" | awk 'NR==2{print $4}')"
info "free disk space for $REPO: $((DF / 1024)) GB"
(( DF >= 4096 )) || note "less than 4 GB free - the Python environment needs about 3 GB, snapshots grow over time"
[[ -d /run/systemd/system ]] && ok "systemd is the init system" || problem "systemd is not running - the services cannot be installed"
mapfile -t ADDRS < <(box_addresses)
info "network addresses: ${ADDRS[*]:-none found}"

say "Project"
info "folder: $REPO (owner $(stat -c %U "$REPO"))"
for f in backend/main.py server.js index.html .env.example backend/requirements-lock.txt \
         backend/models/yolov8n.pt backend/models/fire_smoke/firedetect-11s.pt \
         backend/models/object_monitor/yoloe-26n-seg.pt backend/models/head_yolov8m/head_yolov8m.pt.part0; do
  [[ -e "$f" ]] && ok "$f" || problem "$f missing"
done
if [[ -f .env ]]; then
  ok ".env present (only non-secret settings shown): PORT=$(env_get PORT 5173) HOST=$(env_get HOST 127.0.0.1) AIV_PORT=$(env_get AIV_PORT 8000) AIV_ENGINE=$(env_get AIV_ENGINE auto) AIV_DEVICE=$(env_get AIV_DEVICE auto)"
  if unit_installed "$FRONTEND_UNIT"; then
    info "installed services use: web UI $(unit_env "$FRONTEND_UNIT" HOST):$(unit_env "$FRONTEND_UNIT" PORT), backend 127.0.0.1:$(unit_env "$BACKEND_UNIT" AIV_PORT) (these override HOST/PORT/AIV_PORT in .env)"
  fi
else
  note ".env missing - ./aivision.sh setup / ./aivision.sh install create it from .env.example"
fi
for d in backend/data backend/snapshots; do
  [[ -w "$d" || ! -e "$d" ]] && ok "$d writable by $(id -un)" || problem "$d not writable by $(id -un)"
done

say "Runtimes"
if command -v python3 >/dev/null 2>&1; then
  v="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  case "$v" in 3.13|3.14) ok "system Python $v" ;; *) note "system Python $v - ./aivision.sh setup fetches Python 3.13 with uv" ;; esac
else problem "python3 not installed"; fi
if command -v node >/dev/null 2>&1; then
  nm="$(node -p 'process.versions.node.split(".")[0]')"
  (( nm >= 16 )) && ok "Node.js $(node -v) (web UI; no npm packages needed)" || problem "Node.js $(node -v) too old (16+ needed)"
else problem "Node.js not installed (sudo apt install nodejs)"; fi
info "package managers: the web UI has no dependencies (no npm/pnpm/yarn needed); Python packages come from pip"

say "Backend Python environment (backend/.venv)"
if [[ -x "$VENV_PY" ]]; then
  ok "$("$VENV_PY" -V 2>&1) at backend/.venv"
  "$VENV_PY" - <<'PY' || problem "the backend environment is incomplete - run ./aivision.sh setup"
import importlib, sys
missing = 0
OK, FAIL, WARN = "  \033[1;32m[ OK ]\033[0m", "  \033[1;31m[FAIL]\033[0m", "  \033[1;33m[WARN]\033[0m"
def ver(name):
    try:
        m = importlib.import_module(name)
        return getattr(m, "__version__", "?"), None
    except Exception as e:  # noqa: BLE001
        return None, f"{type(e).__name__}: {e}"
for name, label, required in (("torch", "PyTorch", True), ("torchvision", "torchvision", True),
                              ("ultralytics", "Ultralytics", True), ("cv2", "OpenCV", True), ("numpy", "NumPy", True),
                              ("openvino", "OpenVINO (optional speed-up)", False), ("psutil", "psutil", False)):
    v, err = ver(name)
    if v: print(OK, f"{label} {v}")
    else:
        print(FAIL if required else WARN, f"{label}: {err}")
        missing += required
try:
    import cv2
    has = hasattr(cv2, "TrackerCSRT_create")
    print(OK if has else FAIL, "OpenCV contrib build (CSRT tracker for Object Monitoring)" + ("" if has else " MISSING - run ./aivision.sh setup --reinstall"))
    missing += not has
except Exception:
    pass
try:
    import torch
    print("  [INFO]", f"PyTorch device: {'CUDA ' + torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU'}")
except Exception:
    pass
sys.exit(1 if missing else 0)
PY
  stamp="$(cat backend/.venv/.pyrotech-installed 2>/dev/null || true)"
  [[ -n "$stamp" ]] && ok "./aivision.sh setup completed for this lock file ($stamp)" || note "./aivision.sh setup has not completed in this environment"
  ov_need="$(grep -iE '^openvino==' backend/requirements-lock.txt | cut -d= -f3)"
  if [[ "$ARCH" == "aarch64" ]] && [[ "$(printf '%s\n2.35\n' "$GLIBC" | sort -V | head -n1)" != "2.35" ]]; then
    note "OpenVINO $ov_need wheels for aarch64 need glibc 2.35+ (this box: $GLIBC) - PyTorch is used instead"
  fi
else
  problem "backend/.venv missing - run ./aivision.sh setup "
fi

say "Ports and services"
UI_PORT="$(ui_port)"; API_PORT="$(api_port)"
for p in "$UI_PORT" "$API_PORT"; do
  if port_in_use "$p"; then
    pid="$(listening_pid "$p")"; unit=""
    [[ -n "$pid" ]] && unit="$(ps -o unit= -p "$pid" 2>/dev/null | tr -d ' ')"
    if [[ "$unit" == pyrotech-vision-* ]]; then ok "port $p used by $unit"
    else note "port $p in use${pid:+ by PID $pid ($(ps -o args= -p "$pid" | cut -c1-60))}${unit:+ [$unit]}"; fi
  else info "port $p free"; fi
done
(( UI_PORT != 80 )) || problem "web UI port is 80 - reserved on this box, choose another (PORT in .env)"
for u in "${UNITS[@]}"; do
  if unit_installed "$u"; then info "$u: $(systemctl is-active "$u" 2>/dev/null), boot: $(systemctl is-enabled "$u" 2>/dev/null)"
  else info "$u: not installed"; fi
done
if [[ -n "$UFW" ]]; then
  fw="$(as_root "$UFW" status 2>/dev/null | head -n1 || true)"
  info "ufw: ${fw:-unknown (needs sudo)}"
  if [[ "$fw" == "Status: active" ]] && ! as_root "$UFW" status 2>/dev/null | grep -qE "^$UI_PORT(/tcp)?[[:space:]]+ALLOW"; then
    note "ufw blocks port $UI_PORT - sudo ufw allow $UI_PORT/tcp"
  fi
else info "ufw not installed (check other firewalls: sudo nft list ruleset)"; fi

say "Result"
if (( PROBLEMS == 0 )); then ok "no blocking problems found"; else bad "$PROBLEMS problem(s) found - see [FAIL] lines above"; fi
exit $(( PROBLEMS > 0 ))
}

# ================================================================ run
cmd_run() {
SETUP_ARGS=(); SETUP_ONLY=0; CHECK_ONLY=0; OPEN_BROWSER=1
for a in "$@"; do
  case "$a" in
    --setup-only) SETUP_ONLY=1 ;;
    --reinstall)  SETUP_ARGS+=(--reinstall) ;;
    --check)      CHECK_ONLY=1 ;;
    --no-browser) OPEN_BROWSER=0 ;;
    -h|--help)    usage run; exit 0 ;;
    *) die "Unknown option: $a (see ./aivision.sh run --help)" ;;
  esac
done

# the AI box services (./aivision.sh install) already run the application - never start a 2nd copy
if (( ! SETUP_ONLY && ! CHECK_ONLY )) && command -v systemctl >/dev/null 2>&1 \
   && systemctl is-active --quiet pyrotech-vision-backend.service pyrotech-vision-frontend.service 2>/dev/null; then
  die "PYROTECH VISION already runs as a system service on this box.
       Status: ./aivision.sh status   Restart: ./aivision.sh restart
       To run it by hand instead: ./aivision.sh stop first."
fi

# ---------------------------------------------------------------- 1. setup
"$SELF" setup "${SETUP_ARGS[@]}"

if (( CHECK_ONLY )); then exec "$(need_backend_python)" backend/main.py --check; fi
if (( SETUP_ONLY )); then exit 0; fi

# ---------------------------------------------------------------- 2. API port free?
API_PORT="$(env_get AIV_PORT 8000)"
UI_PORT="$(env_get PORT 5173)"
if port_busy "$API_PORT"; then
  pid="$(port_pid "$API_PORT")"
  echo
  echo "Port $API_PORT is already in use${pid:+ by process $pid ($(ps -o args= -p "$pid" 2>/dev/null | cut -c1-80))}."
  echo "This is usually an older backend that is still running."
  [[ -n "$pid" ]] || die "Stop the program that uses port $API_PORT, then run ./aivision.sh run again."
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
  need_node; node server.js > logs/frontend.log 2>&1 &
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
"$(need_backend_python)" backend/main.py
RC=$?
set -e
echo "Backend stopped (exit code $RC)."
exit "$RC"
}

# ================================================================ ai-stream
cmd_ai_stream() {
[[ $# -ge 1 ]] || { usage ai-stream; exit 2; }
API="http://127.0.0.1:$(api_port)/api"
python3 - "$API" "$@" <<'PY'
import json, re, sys, urllib.error, urllib.request
api, cmd, args = sys.argv[1], sys.argv[2], sys.argv[3:]

def call(method, path, body=None):
    req = urllib.request.Request(api + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")
    except Exception as e:
        sys.exit(f"ERROR: backend not reachable at {api} ({e}) - is pyrotech-vision-backend running?")

mask = lambda u: re.sub(r"://[^/@\s]*@", "://***@", u or "")
st, cams = call("GET", "/cameras")
cams = cams if isinstance(cams, list) else (cams or {}).get("items", [])
if cmd == "list":
    print(f"{'camera':16} {'AI stream':10} {'snapshots':9} source")
    for c in cams:
        ai = c.get("aiStream") or "same"
        print(f"{c['id']:16} {ai + ('=' + c.get('aiCustomStream', '') if ai == 'custom' else ''):10} {c.get('snapshotSource') or 'main':9} {mask(c.get('sourceUrl'))}")
    sys.exit(0)
if cmd not in ("sub", "custom", "same"):
    sys.exit("usage: list | sub CAM... | custom CAM PATH | same CAM...   (--all = every camera)")
if cmd == "custom":
    if len(args) != 2 or not args[1].startswith("/"):
        sys.exit("usage: custom CAM-ID /path/of/the/substream")
    targets, path = [args[0]], args[1]
else:
    targets, path = ([c["id"] for c in cams] if args == ["--all"] else args), None
if not targets:
    sys.exit("name at least one camera id (or --all)")
bad = 0
for cid in targets:
    st, cam = call("GET", "/cameras/" + cid)
    if st != 200:
        print(f"{cid}: not found"); bad += 1; continue
    body = {k: v for k, v in cam.items() if k not in ("status", "lastError", "lastConnectedAt", "hasCredentials", "createdAt",
                                                      "updatedAt", "source", "ip", "host")}
    body["aiStream"] = cmd
    body["aiCustomStream"] = path or ""
    body["snapshotSource"] = "main"
    st, r = call("PUT", "/cameras/" + cid, body)
    if st == 200:
        print(f"{cid}: AI stream = {cmd}{' ' + path if path else ''} (snapshots from the main stream)")
    else:
        bad += 1
        print(f"{cid}: NOT changed - {(r or {}).get('message')}")
        if cmd == "sub":
            print("   the substream cannot be derived from this URL - use:  custom " + cid + " /path/of/the/substream")
sys.exit(1 if bad else 0)
PY
}

# ================================================================ perf
cmd_perf() {
if [[ "${1:-}" == "" || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then usage perf; exit 0; fi
PY="$VENV_PY"
if [[ ! -x "$PY" ]]; then
  note "backend/.venv not found - monitor/status work with the system python3, 'bench' needs ./aivision.sh setup first"
  PY="$(command -v python3)"
fi
exec nice -n 10 "$PY" "$REPO/scripts/perf/pyrotech_perf.py" "$@"
}

# ================================================================ npu
cmd_npu() {
local sub="${1:-}"; shift || true
case "$sub" in
  ""|-h|--help|help) usage npu; exit 0 ;;
esac
SOC="$(rockchip_soc)"
npu_restart() {
  if systemctl is-active --quiet "$BACKEND_UNIT" 2>/dev/null; then
    say "Restarting the backend"
    as_root systemctl restart "$BACKEND_UNIT"
    "$SELF" npu status --wait
  else
    info "backend service not running - the setting is used at the next start"
  fi
}
# NPU test: the RKNN model in use loads on the NPU and finds the same people / bus as the CPU engine on the
# sample images; passed -> backend/npu/verified (AIV_ENGINE=auto uses the NPU only then), failed -> marker removed
npu_test() {
  local py model assets out name
  py="$(need_backend_python)"
  model="backend/$(env_get AIV_RKNN_MODEL models/yolov8n_rknn_model)"
  name="$(basename "$model")"
  [[ -d "$model" ]] || { warn "RKNN model folder $model not found"; return 1; }
  assets="$("$py" -c 'import pathlib, ultralytics; print(pathlib.Path(ultralytics.__file__).parent / "assets")' 2>/dev/null)"
  out="$(mktemp -d)"; chmod 755 "$out"
  say "Testing the NPU with $name (model load + detections compared with the CPU engine on sample images)"
  rm -f backend/npu/verified
  # as root: a group just added for this user only applies to new logins
  if ! as_root env AIV_ENGINE=rknn PYTHONDONTWRITEBYTECODE=1 "$py" "$REPO/scripts/npu/rknn_analyze.py" --only models,accuracy --model "$model" \
         --images "$assets" --out "$out" > "$out/test.log" 2>&1; then
    grep -E '^\s+(- |!!)|found by|strong' "$out/test.log" | head -n 12
    warn "NPU test failed (details: $out/test.log)"
    return 1
  fi
  grep -E 'found by|strong boxes' "$out/test.log" || true
  printf '%s\n' "$name" > backend/npu/verified
  ok "NPU test passed ($name)"
}
case "$sub" in
  setup)
    [[ -n "$SOC" ]] || die "No Rockchip NPU chip found in /proc/device-tree/compatible - the NPU engine needs an RK3588 class board."
    local lib=""
    [[ "${1:-}" == "--lib" ]] && { lib="${2:?--lib needs the path of librknnrt.so}"; }
    say "Rockchip $SOC - NPU runtime"
    npu_lib_fetch "$lib" || die "NPU runtime not installed."
    ls backend/models/*_rknn_model/*.rknn >/dev/null 2>&1 || die "No RKNN model in backend/models - run git pull."
    local u g; u="$(id -un)"
    if unit_installed "$BACKEND_UNIT"; then u="$(systemctl show -p User --value "$BACKEND_UNIT" 2>/dev/null)"; u="${u:-root}"; fi
    for g in $(npu_groups); do
      if [[ "$u" != "root" ]] && ! id -nG "$u" | tr ' ' '\n' | grep -qx "$g"; then
        as_root usermod -aG "$g" "$u" && ok "user $u added to group $g (NPU device access)"
      fi
    done
    npu_test || { npu_restart; die "NPU test failed - the NPU is NOT used; the CPU engine stays in use."; }
    env_set AIV_ENGINE auto
    ok "AIV_ENGINE=auto in .env (NPU first, CPU engine if the NPU cannot be used)"
    if unit_installed "$BACKEND_UNIT" && unit_ours "$BACKEND_UNIT" && ! grep -q '^SupplementaryGroups=' "$UNIT_DIR/$BACKEND_UNIT" \
       && [[ -n "$(npu_groups)" ]]; then
      say "Updating the services (NPU device access)"
      "$SELF" install --skip-setup --no-start >/dev/null && ok "services updated"
    fi
    npu_restart ;;
  status)
    local wait=0; [[ "${1:-}" == "--wait" ]] && wait=20
    python3 - "http://127.0.0.1:$(api_port)/api" "$wait" <<'PY'
import json, sys, time, urllib.request
api, wait = sys.argv[1], int(sys.argv[2])
def get(p):
    with urllib.request.urlopen(api + p, timeout=5) as r:
        return json.loads(r.read())
end = time.time() + wait
while True:
    try:
        h = get("/system/health"); m = get("/system/metrics")
        # after a restart: wait (up to the limit) until the cameras that start automatically are running
        ss = m.get("sessions") or []
        if (ss and all(x.get("engine") for x in ss)) or time.time() > end + 25 or not wait:
            break
    except Exception as e:
        if time.time() > end:
            sys.exit(f"backend not reachable ({e})")
    time.sleep(2)
print(f"  engine (new cameras): {h.get('engine')}")
cams = m.get("sessions") or []
if not cams:
    print("  no camera session running")
for c in cams:
    fps = c.get("processingFps")
    print(f"  {str(c.get('cameraId')):16} engine {c.get('engine') or '-':9} device {c.get('device') or '-':15} "
          f"{c.get('precision') or '':5} input {c.get('imgSize') or '-':4} AI {fps if fps is not None else '-'} fps, "
          f"camera {c.get('cameraFps') if c.get('cameraFps') is not None else '-'} fps")
    stg = {k: v for k, v in (c.get("stageMs") or {}).items() if v}
    if stg:   # where the time of one analysed frame goes (ms, last second)
        print("      time per analysed frame: " + ", ".join(f"{k} {v}" for k, v in stg.items())
              + f"  | loop {c.get('loopMs')} ms, frame age {c.get('frameAgeMs')} ms, live view {c.get('displayFps')} fps")
    for mod, sc in sorted((c.get("schedule") or {}).items()):
        st = sc.get("state")
        print(f"      {mod:8} {sc.get('mode') or '-'}{' ' + st.upper() if st else '':8} AI {sc.get('aiFps') if sc.get('aiFps') is not None else '-'} fps "
              f"(last {sc.get('aiFpsWindowSec') or '-'} s), last inference {sc.get('lastInferenceMs') if sc.get('lastInferenceMs') is not None else '-'} ms, "
              f"active {sc.get('activeSharePct') if sc.get('activeSharePct') is not None else '-'} % of the time, "
              f"woken by motion {sc.get('wakeByMotion', 0)}x")
    if any((sc.get("state") == "idle") for sc in (c.get("schedule") or {}).values()):
        print("      IDLE = nothing moving in the zone: AI only every idleCheckSec; it switches to every frame on motion")
print(f"  backend CPU {m.get('processCpu')}% of one core, system CPU {m.get('cpu')}%")
if h.get("engine") != "RKNN NPU":
    print("  NPU not in use - see: journalctl -u pyrotech-vision-backend | grep -i rknn   and   ./aivision.sh npu check")
PY
    ;;
  check|bench)
    local py; py="$(need_backend_python)"
    local only=()
    [[ "$sub" == "bench" ]] && only=(--only system,models,bench,pipeline)
    mkdir -p logs
    exec nice -n 5 "$py" "$REPO/scripts/npu/rknn_analyze.py" "${only[@]}" "$@" ;;
  calib-pack)
    exec nice -n 10 "$(need_backend_python)" "$REPO/scripts/npu/calib_pack.py" "$@" ;;
  on)
    npu_lib_ok || [[ -n "$(env_get AIV_RKNN_LIB "")" ]] || die "NPU runtime not installed - run ./aivision.sh npu setup"
    npu_test || { npu_restart; die "NPU test failed - the NPU is NOT used."; }
    env_set AIV_ENGINE auto; ok "NPU engine on (AIV_ENGINE=auto)"; npu_restart ;;
  off)
    env_set AIV_ENGINE openvino; ok "NPU engine off - CPU engine (AIV_ENGINE=openvino, PyTorch if OpenVINO is missing)"; npu_restart ;;
  model)
    local prev; prev="$(env_get AIV_RKNN_MODEL models/yolov8n_rknn_model)"
    case "${1:-}" in
      fp16) env_set AIV_RKNN_MODEL models/yolov8n_rknn_model ;;
      int8) [[ -f backend/models/yolov8n_int8_rknn_model/metadata.yaml ]] || die "INT8 model not found"
            env_set AIV_RKNN_MODEL models/yolov8n_int8_rknn_model
            note "INT8: check the accuracy on your cameras first: ./aivision.sh npu check --camera <ID>" ;;
      *) die "usage: ./aivision.sh npu model fp16|int8" ;;
    esac
    ok "NPU model: $1"
    # the model to go back to: the previous one, or FP16 when the previous one is this same model
    [[ "$prev" == "$(env_get AIV_RKNN_MODEL)" ]] && prev="models/yolov8n_rknn_model"
    if [[ "$(env_get AIV_ENGINE auto)" == "auto" ]] && ! npu_test; then
      # never leave the box on the CPU because a new model failed: go back to the previous model
      warn "the NPU test failed with this model - going back to $prev"
      env_set AIV_RKNN_MODEL "$prev"
      npu_test || warn "the previous model failed the test too - the CPU engine is used (./aivision.sh npu check)"
    fi
    npu_restart ;;
  *) usage npu; die "Unknown npu command: $sub" ;;
esac
}

# ================================================================ dispatch
CMD="${1:-}"
[[ -n "$CMD" ]] || { usage; exit 2; }
shift
case "$CMD" in
  setup) ( cmd_setup "$@" ) ;;
  install) ( cmd_install "$@" ) ;;
  start) ( cmd_start "$@" ) ;;
  stop) ( cmd_stop "$@" ) ;;
  restart) ( cmd_restart "$@" ) ;;
  status) ( cmd_status "$@" ) ;;
  update) ( cmd_update "$@" ) ;;
  uninstall) ( cmd_uninstall "$@" ) ;;
  check) ( cmd_check "$@" ) ;;
  run) ( cmd_run "$@" ) ;;
  ai-stream) ( cmd_ai_stream "$@" ) ;;
  perf) ( cmd_perf "$@" ) ;;
  npu) ( cmd_npu "$@" ) ;;
  -h|--help|help) usage ;;
  *) usage; die "Unknown command: $CMD" ;;
esac
