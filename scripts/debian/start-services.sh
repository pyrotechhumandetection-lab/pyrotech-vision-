#!/usr/bin/env bash
# PYROTECH VISION - start the services now (backend first, then the web UI).
# They also start by themselves at every boot once install-services.sh has run.
set -euo pipefail
# shellcheck source=scripts/debian/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

for u in "${UNITS[@]}"; do unit_installed "$u" || die "$u is not installed - run scripts/debian/install-services.sh"; done
say "Starting $BACKEND_UNIT"
as_root systemctl start "$BACKEND_UNIT"
say "Starting $FRONTEND_UNIT"
as_root systemctl start "$FRONTEND_UNIT"
sleep 2
exec "$(dirname "${BASH_SOURCE[0]}")/status-services.sh" --wait 60
