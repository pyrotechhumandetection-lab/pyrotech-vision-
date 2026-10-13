# shellcheck shell=bash disable=SC2034
# PYROTECH VISION - shared helpers for the Linux scripts (sourced, not run).
# Every script cds to the repository root before sourcing this file.

say()  { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

VENV="backend/.venv"
LOCK="backend/requirements-lock.txt"

# env_get KEY [DEFAULT] - value from the real environment, else from .env, else DEFAULT
env_get() {
  local key="$1" def="${2:-}" val=""
  if [[ -n "${!key:-}" ]]; then printf '%s' "${!key}"; return; fi
  if [[ -f .env ]]; then
    val="$( (grep -E "^[[:space:]]*${key}=" .env || true) | tail -n1 | cut -d= -f2- | tr -d '\r' \
            | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/')"
  fi
  printf '%s' "${val:-$def}"
}

# backend_python - the Python that runs the backend (AIV_PYTHON in .env, else backend/.venv)
backend_python() {
  local py
  py="$(env_get AIV_PYTHON "")"
  if [[ -n "$py" ]]; then printf '%s' "$py"; else printf '%s' "$VENV/bin/python"; fi
}

port_busy() { ss -ltnH "sport = :$1" 2>/dev/null | grep -q .; }
port_pid()  { ss -ltnpH "sport = :$1" 2>/dev/null | grep -o 'pid=[0-9]*' | head -n1 | cut -d= -f2; }
