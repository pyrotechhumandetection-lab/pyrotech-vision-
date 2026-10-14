#!/usr/bin/env bash
# PYROTECH VISION - read-only check of the AI box before / after installing the services.
# Changes nothing, installs nothing, prints no passwords. Send its output when asking for help.
#
#   scripts/debian/check-environment.sh
set -uo pipefail
# shellcheck source=scripts/debian/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$REPO" || exit 1
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
  note ".env missing - setup-backend.sh / install-services.sh create it from .env.example"
fi
for d in backend/data backend/snapshots; do
  [[ -w "$d" || ! -e "$d" ]] && ok "$d writable by $(id -un)" || problem "$d not writable by $(id -un)"
done

say "Runtimes"
if command -v python3 >/dev/null 2>&1; then
  v="$(python3 -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
  case "$v" in 3.13|3.14) ok "system Python $v" ;; *) note "system Python $v - setup-backend.sh fetches Python 3.13 with uv" ;; esac
else problem "python3 not installed"; fi
if command -v node >/dev/null 2>&1; then
  nm="$(node -p 'process.versions.node.split(".")[0]')"
  (( nm >= 16 )) && ok "Node.js $(node -v) (web UI; no npm packages needed)" || problem "Node.js $(node -v) too old (16+ needed)"
else problem "Node.js not installed (sudo apt install nodejs)"; fi
info "package managers: the web UI has no dependencies (no npm/pnpm/yarn needed); Python packages come from pip"

say "Backend Python environment (backend/.venv)"
if [[ -x "$VENV_PY" ]]; then
  ok "$("$VENV_PY" -V 2>&1) at backend/.venv"
  "$VENV_PY" - <<'PY' || problem "the backend environment is incomplete - run ./setup-backend.sh"
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
    print(OK if has else FAIL, "OpenCV contrib build (CSRT tracker for Object Monitoring)" + ("" if has else " MISSING - run ./setup-backend.sh --reinstall"))
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
  [[ -n "$stamp" ]] && ok "setup-backend.sh completed for this lock file ($stamp)" || note "setup-backend.sh has not completed in this environment"
  ov_need="$(grep -iE '^openvino==' backend/requirements-lock.txt | cut -d= -f3)"
  if [[ "$ARCH" == "aarch64" ]] && [[ "$(printf '%s\n2.35\n' "$GLIBC" | sort -V | head -n1)" != "2.35" ]]; then
    note "OpenVINO $ov_need wheels for aarch64 need glibc 2.35+ (this box: $GLIBC) - PyTorch is used instead"
  fi
else
  problem "backend/.venv missing - run ./setup-backend.sh (or ./start.sh --setup-only)"
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
