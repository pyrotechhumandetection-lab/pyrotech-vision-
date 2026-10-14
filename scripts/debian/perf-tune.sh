#!/usr/bin/env bash
# PYROTECH VISION - performance monitor and CPU optimizer (scripts/perf/pyrotech_perf.py).
#
#   scripts/debian/perf-tune.sh status              hardware, CPU settings in effect, per-camera performance, findings
#   scripts/debian/perf-tune.sh monitor             live view (timestamps) until Ctrl+C;  --log backend/data/perf/monitor.jsonl
#   scripts/debian/perf-tune.sh auto                start if needed, measure, benchmark, apply the best setting (validated)
#   scripts/debian/perf-tune.sh auto --dry-run      the same, but only recommend
#   scripts/debian/perf-tune.sh bench               benchmark only (idle priority next to the running app)
#   scripts/debian/perf-tune.sh apply --threads 4 --concurrency 2     set values (backup, restart, measure, keep/roll back)
#   scripts/debian/perf-tune.sh rollback            undo the last applied change (restores the .env backup, restarts)
#   scripts/debian/perf-tune.sh history             what was changed and what it measured
#   scripts/debian/perf-tune.sh start | stop | restart
#   scripts/debian/perf-tune.sh <command> --help    all options
#
# Runs with the backend's Python (backend/.venv) at reduced priority, so it never competes with detection.
set -euo pipefail
# shellcheck source=scripts/debian/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$REPO"
if [[ "${1:-}" == "" || "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then sed -n '2,15p' "$0"; exit 0; fi
PY="$VENV_PY"
if [[ ! -x "$PY" ]]; then
  note "backend/.venv not found - monitor/status work with the system python3, 'bench' needs ./setup-backend.sh first"
  PY="$(command -v python3)"
fi
exec nice -n 10 "$PY" "$REPO/scripts/perf/pyrotech_perf.py" "$@"
