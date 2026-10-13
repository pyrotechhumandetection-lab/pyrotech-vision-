#!/usr/bin/env bash
# ==========================================================================================
#  PYROTECH VISION - install the backend and the web UI as systemd services (Debian AI box)
#
#      scripts/debian/install-services.sh            (run as the normal user; asks for sudo)
#
#  After this the box starts both services at every boot, restarts them if they crash, and
#  keeps them running after logout. Open  http://<box-ip>:<ui-port>  from any PC on the LAN.
#
#    pyrotech-vision-backend.service   backend/.venv/bin/python backend/main.py
#                                      API on 127.0.0.1:<AIV_PORT, 8000> (not reachable from the LAN)
#    pyrotech-vision-frontend.service  node server.js - web UI on 0.0.0.0:<PORT, 5173>, forwards /api
#                                      (including the live MJPEG video) to the backend
#
#  Steps: checks -> backend environment (setup-backend.sh, skipped when up to date) -> unit files
#  validated with systemd-analyze -> install + enable -> start -> readiness check -> report.
#  Run it again after changing PORT / AIV_PORT in .env, after moving the folder, or after updates.
#
#  Options:
#    --user NAME       account that runs the services (default: owner of the project folder)
#    --ui-port N       web UI port (default: PORT in .env, else 5173; port 80 is refused)
#    --api-port N      backend port on 127.0.0.1 (default: AIV_PORT in .env, else 8000)
#    --bind ADDR       address the web UI listens on (default 0.0.0.0 = every network interface)
#    --open-firewall   if ufw is active: allow the web UI port (otherwise only printed)
#    --skip-setup      do not run setup-backend.sh (the environment must already be complete)
#    --no-start        install and enable for the next boot, but do not start now
#    --allow-root      allow running the services as root (not recommended)
# ==========================================================================================
set -euo pipefail
# shellcheck source=scripts/debian/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$REPO"

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
    -h|--help)       sed -n '2,31p' "$0"; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
  shift
done

[[ "$(uname -s)" == "Linux" ]] || die "Linux only."
command -v systemctl >/dev/null 2>&1 || die "systemctl not found - this script needs a systemd-based Debian."
[[ -d /run/systemd/system ]] || die "systemd is not running as the init system on this machine."

say "Checking the project in $REPO"
FAIL=0
for f in backend/main.py server.js index.html .env.example backend/requirements-lock.txt setup-backend.sh \
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

# a backend or web UI started by hand (./start.sh) would block the ports - and would be a second copy
for p in "$API_PORT" "$UI_PORT"; do
  if port_in_use "$p"; then
    pid="$(listening_pid "$p")"
    owner_unit=""
    [[ -n "$pid" ]] && owner_unit="$(ps -o unit= -p "$pid" 2>/dev/null | tr -d ' ' || true)"
    if [[ "$owner_unit" == "$BACKEND_UNIT" || "$owner_unit" == "$FRONTEND_UNIT" ]]; then
      info "port $p is used by $owner_unit (will be restarted)"
    else
      die "Port $p is already in use${pid:+ by PID $pid ($(ps -o args= -p "$pid" 2>/dev/null | cut -c1-70))}.
       Stop that program first (a backend or web UI started by hand with ./start.sh: press Ctrl+C there),
       or choose another port with --ui-port / --api-port."
    fi
  fi
done

# ---------------------------------------------------------------- runtimes
NODE="$(command -v node || true)"
if [[ -z "$NODE" && $SKIP_SETUP -eq 1 ]]; then die "Node.js not found (sudo apt install nodejs)."; fi

say "Preparing the backend environment"
if (( SKIP_SETUP )); then
  info "setup-backend.sh skipped (--skip-setup)"
else
  # installs only what is missing (apt packages, backend/.venv, pinned packages, self-check);
  # a complete environment is left untouched - nothing is deleted or downgraded
  as_user ./setup-backend.sh
fi
NODE="$(command -v node || true)"
[[ -n "$NODE" ]] || die "Node.js not found (sudo apt install nodejs)."
NODE="$(readlink -f "$NODE")"
NODE_MAJOR="$("$NODE" -p 'process.versions.node.split(".")[0]')"
(( NODE_MAJOR >= 16 )) || die "Node.js 16 or newer is needed (found $("$NODE" -v))."
ok "Node.js $("$NODE" -v) at $NODE (no npm packages needed)"

PYV="$(env_get AIV_PYTHON "")"
PY="${PYV:-$VENV_PY}"
[[ -x "$PY" ]] || die "Backend Python not found: $PY - run ./setup-backend.sh"
PY="$(cd "$(dirname "$PY")" && pwd -P)/$(basename "$PY")"     # absolute, venv symlink kept
as_user "$PY" -c 'import ultralytics, torch, cv2, numpy; assert hasattr(cv2, "TrackerCSRT_create")' \
  || die "The backend environment is incomplete ($PY cannot import ultralytics/torch/OpenCV contrib).
       Run ./setup-backend.sh (or ./start.sh --setup-only) and check its messages."
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
# Re-run install-services.sh to change it; edits here are overwritten.
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
# Re-run install-services.sh to change it; edits here are overwritten.
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
  say "Installed. The services start at the next boot (or now with scripts/debian/start-services.sh)."
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
echo "  Status: scripts/debian/status-services.sh     Logs: journalctl -u $BACKEND_UNIT -f"
(( ready_ui && ready_api )) || exit 1
