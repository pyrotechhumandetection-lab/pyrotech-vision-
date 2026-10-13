#!/usr/bin/env bash
# PYROTECH VISION - stop the services gracefully (web UI first, then the backend).
# The backend closes its cameras and saves counts / pending alerts before it exits (up to 60 s).
# Detections that were running are resumed at the next start. The services still start at the
# next boot; to stop that too: sudo systemctl disable pyrotech-vision-backend pyrotech-vision-frontend
set -euo pipefail
# shellcheck source=scripts/debian/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

for u in "$FRONTEND_UNIT" "$BACKEND_UNIT"; do
  if ! unit_installed "$u"; then info "$u is not installed"; continue; fi
  say "Stopping $u"
  as_root systemctl stop "$u"
  ok "$u: $(systemctl is-active "$u" 2>/dev/null || true)"
done
