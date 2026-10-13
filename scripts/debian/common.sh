# shellcheck shell=bash disable=SC2034
# PYROTECH VISION - shared settings and helpers for scripts/debian/*.sh (sourced, not run).

# repository root = two levels above this file
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd -P)"

BACKEND_UNIT="pyrotech-vision-backend.service"
FRONTEND_UNIT="pyrotech-vision-frontend.service"
UNITS=("$BACKEND_UNIT" "$FRONTEND_UNIT")
UNIT_DIR="/etc/systemd/system"
# every unit file written by install-services.sh carries this line; files without it are never touched
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
