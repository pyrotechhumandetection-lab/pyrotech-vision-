#!/usr/bin/env bash
# PYROTECH VISION - remove the systemd services (stop, disable, delete the two unit files).
# Only unit files created by install-services.sh are removed. The project folder, backend/.venv,
# models, .env, cameras, alerts and snapshots are NOT touched - ./start.sh still works afterwards,
# and install-services.sh can install the services again at any time.
#
#   scripts/debian/uninstall-services.sh [--close-firewall]
#     --close-firewall   also remove the ufw rule for the web UI port (if install added one)
set -euo pipefail
# shellcheck source=scripts/debian/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

CLOSE_FW=0
for a in "$@"; do
  case "$a" in
    --close-firewall) CLOSE_FW=1 ;;
    -h|--help) sed -n '2,9p' "$0"; exit 0 ;;
    *) die "Unknown option: $a" ;;
  esac
done

UI_PORT="$(ui_port)"
say "Removing the PYROTECH VISION services (project data is kept)"
removed=0
for u in "$FRONTEND_UNIT" "$BACKEND_UNIT"; do
  if ! unit_installed "$u"; then info "$u is not installed"; continue; fi
  if ! unit_ours "$u"; then note "$UNIT_DIR/$u was not created by install-services.sh - left untouched"; continue; fi
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
echo "  Start by hand: ./start.sh      Install the services again: scripts/debian/install-services.sh"
