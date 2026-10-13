#!/usr/bin/env bash
# PYROTECH VISION - restart the services (backend first, then the web UI) and wait until they answer.
#   scripts/debian/restart-services.sh             both
#   scripts/debian/restart-services.sh backend     only the backend (e.g. after changing .env)
#   scripts/debian/restart-services.sh frontend    only the web UI
set -euo pipefail
# shellcheck source=scripts/debian/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

case "${1:-all}" in
  all)      targets=("$BACKEND_UNIT" "$FRONTEND_UNIT") ;;
  backend)  targets=("$BACKEND_UNIT") ;;
  frontend) targets=("$FRONTEND_UNIT") ;;
  -h|--help) sed -n '2,5p' "$0"; exit 0 ;;
  *) die "Usage: restart-services.sh [all|backend|frontend]" ;;
esac
for u in "${targets[@]}"; do
  unit_installed "$u" || die "$u is not installed - run scripts/debian/install-services.sh"
  say "Restarting $u"
  as_root systemctl restart "$u"
done
sleep 2
exec "$(dirname "${BASH_SOURCE[0]}")/status-services.sh" --wait 60
