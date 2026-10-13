#!/usr/bin/env bash
# Start the PYROTECH VISION web UI on http://127.0.0.1:5173 (Linux version of start-frontend.cmd).
# It serves the UI and forwards /api to the backend (BACKEND_URL in .env). No npm install needed.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
# shellcheck source=backend/tools/lib.sh
source backend/tools/lib.sh

command -v node >/dev/null 2>&1 || die "Node.js was not found - run ./start.sh once (it installs it) or: sudo apt install nodejs"
exec node server.js "$@"
