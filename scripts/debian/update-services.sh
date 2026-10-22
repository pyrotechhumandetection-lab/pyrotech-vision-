#!/usr/bin/env bash
# ==========================================================================================
#  PYROTECH VISION - update the AI box and (re)start the backend + web UI services
#
#      scripts/debian/update-services.sh [options]        (run as the normal user; asks for sudo)
#
#  Steps:
#    1. backup        .env and the event/camera lists (backend/data/*.json) -> <backup dir>/<time>/
#                     (snapshot images are never copied or deleted)
#    2. update        git pull --ff-only (refuses - and changes nothing - when the folder has local edits)
#    3. data folder   optional --data-dir: move cameras/events/snapshots to a bigger partition (copy, keep the
#                     old folder), AIV_DATA_DIR / AIV_SNAPSHOT_DIR written to .env (backed up first)
#    4. environment   setup-backend.sh only when backend/requirements*.txt or setup-backend.sh changed
#    5. services      scripts/debian/install-services.sh (writes/validates the systemd units, restarts both)
#    6. check         backend and web UI health through the real ports, last log lines on failure
#
#  Options:
#    --data-dir DIR    keep cameras, events and snapshots in DIR (e.g. /userdata/pyrotech-vision)
#    --backup-dir DIR  where backups go (default: /userdata/pyrotech-backups if /userdata exists, else ~/pyrotech-backups)
#    --no-pull         do not run git pull (re-install / restart the current version)
#    --setup           always run setup-backend.sh
#    --                everything after this is passed to install-services.sh (e.g. -- --ui-port 5173)
# ==========================================================================================
set -euo pipefail
# shellcheck source=scripts/debian/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
cd "$REPO"

DATA_DIR=""; BACKUP_DIR=""; PULL=1; FORCE_SETUP=0; PASS=()
while (($#)); do
  case "$1" in
    --data-dir)   DATA_DIR="${2:?--data-dir needs a folder}"; shift ;;
    --backup-dir) BACKUP_DIR="${2:?--backup-dir needs a folder}"; shift ;;
    --no-pull)    PULL=0 ;;
    --setup)      FORCE_SETUP=1 ;;
    --)           shift; PASS=("$@"); break ;;
    -h|--help)    sed -n '2,26p' "$0"; exit 0 ;;
    *) die "Unknown option: $1 (see --help)" ;;
  esac
  shift
done
[[ $EUID -ne 0 ]] || die "Run as the normal user that owns $REPO (the script asks for sudo where needed)."
command -v git >/dev/null || die "git is not installed (sudo apt install git)."

TS="$(date +%Y%m%d-%H%M%S)"
if [[ -z "$BACKUP_DIR" ]]; then
  if [[ -d /userdata && -w /userdata ]] || as_root test -d /userdata 2>/dev/null; then BACKUP_DIR=/userdata/pyrotech-backups
  else BACKUP_DIR="$HOME/pyrotech-backups"; fi
fi

# ---------------------------------------------------------------- 1. backup
say "1/6 Backup -> $BACKUP_DIR/$TS"
as_root mkdir -p "$BACKUP_DIR/$TS"
as_root chown "$(id -un):$(id -gn)" "$BACKUP_DIR" "$BACKUP_DIR/$TS" 2>/dev/null || true
chmod 700 "$BACKUP_DIR/$TS"
[[ -f .env ]] && cp -p .env "$BACKUP_DIR/$TS/.env" && ok ".env saved"
CUR_DATA="$(env_get AIV_DATA_DIR "$REPO/backend/data")"
if [[ -d "$CUR_DATA" ]]; then
  mkdir -p "$BACKUP_DIR/$TS/data"
  find "$CUR_DATA" -maxdepth 1 -name '*.json' -exec cp -p {} "$BACKUP_DIR/$TS/data/" \; 2>/dev/null || true
  ok "camera / event lists saved ($(ls "$BACKUP_DIR/$TS/data" | wc -l) files from $CUR_DATA; snapshot images not copied)"
fi
git rev-parse HEAD > "$BACKUP_DIR/$TS/git-commit.txt" 2>/dev/null || true
info "previous version: $(git log --oneline -1 2>/dev/null || echo unknown)"

# ---------------------------------------------------------------- 2. update
say "2/6 Update the code"
REQ_BEFORE="$(cat backend/requirements*.txt setup-backend.sh 2>/dev/null | sha256sum | cut -c1-16)"
if (( PULL )); then
  if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
    git status --short --untracked-files=no | head -20
    die "The project folder has local changes (listed above) - nothing was updated.
       Keep them:    git stash        (then run this script again; 'git stash pop' brings them back)
       Discard them: git checkout -- <file>"
  fi
  git pull --ff-only || die "git pull failed (network, or the branch has diverged) - nothing was changed."
  ok "now at: $(git log --oneline -1)"
else
  info "--no-pull: keeping $(git log --oneline -1)"
fi
REQ_AFTER="$(cat backend/requirements*.txt setup-backend.sh 2>/dev/null | sha256sum | cut -c1-16)"

# ---------------------------------------------------------------- 3. data folder
say "3/6 Data folder"
if [[ -n "$DATA_DIR" ]]; then
  DATA_DIR="$(realpath -m "$DATA_DIR")"
  as_root mkdir -p "$DATA_DIR/snapshots"
  as_root chown -R "$(stat -c %U "$REPO"):$(stat -c %G "$REPO")" "$DATA_DIR"
  OLD_SNAP="$(env_get AIV_SNAPSHOT_DIR "$CUR_DATA/snapshots")"
  if [[ "$CUR_DATA" != "$DATA_DIR" && -d "$CUR_DATA" ]]; then
    info "copying $CUR_DATA -> $DATA_DIR (the old folder is kept; delete it yourself once everything works)"
    cp -a -n "$CUR_DATA/." "$DATA_DIR/" 2>/dev/null || true
    if [[ -d "$OLD_SNAP" && "$OLD_SNAP" != "$DATA_DIR/snapshots" ]]; then cp -a -n "$OLD_SNAP/." "$DATA_DIR/snapshots/" 2>/dev/null || true; fi
  fi
  [[ -f .env ]] || cp .env.example .env
  cp -p .env "$BACKUP_DIR/$TS/.env.before-data-dir"
  for kv in "AIV_DATA_DIR=$DATA_DIR" "AIV_SNAPSHOT_DIR=$DATA_DIR/snapshots"; do
    k="${kv%%=*}"
    if grep -qE "^[[:space:]]*$k=" .env; then sed -i "s#^[[:space:]]*$k=.*#$kv#" .env; else printf '%s\n' "$kv" >> .env; fi
  done
  ok "data: $DATA_DIR (free: $(df -h --output=avail "$DATA_DIR" | tail -1 | tr -d ' '))"
else
  info "unchanged: $CUR_DATA (free: $(df -h --output=avail "$CUR_DATA" 2>/dev/null | tail -1 | tr -d ' ' || echo ?))"
  ROOT_USE="$(df --output=pcent / | tail -1 | tr -dc '0-9')"
  if (( ROOT_USE >= 90 )) && [[ -d /userdata ]]; then
    note "the root partition is ${ROOT_USE}% full - consider:  $0 --data-dir /userdata/pyrotech-vision"
  fi
fi

# ---------------------------------------------------------------- 4 + 5. environment and services
say "4/6 Backend environment"
INSTALL_ARGS=("${PASS[@]}")
if (( FORCE_SETUP )) || [[ "$REQ_BEFORE" != "$REQ_AFTER" ]] || [[ ! -x backend/.venv/bin/python ]]; then
  info "requirements or setup changed - setup-backend.sh will run (installs only what is missing)"
else
  INSTALL_ARGS+=("--skip-setup")
  ok "requirements unchanged - environment kept"
fi

say "5/6 Services"
bash scripts/debian/install-services.sh "${INSTALL_ARGS[@]}"
as_root systemctl restart "$BACKEND_UNIT" "$FRONTEND_UNIT"
ok "restarted $BACKEND_UNIT and $FRONTEND_UNIT"

# ---------------------------------------------------------------- 6. check
say "6/6 Health check"
API="http://127.0.0.1:$(api_port)/api/system/health"
UI="http://127.0.0.1:$(ui_port)/api/system/health"
okb=0; oku=0
for _ in $(seq 1 60); do
  [[ "$(http_status "$API" 3)" == "200" ]] && okb=1
  [[ "$(http_status "$UI" 3)" == "200" ]] && oku=1
  (( okb && oku )) && break
  sleep 1
done
if (( okb )); then ok "backend answers ($API)"; else bad "backend does not answer"; as_root journalctl -u "$BACKEND_UNIT" -n 30 --no-pager | sed -E 's#://[^/@ ]*@#://***@#g'; fi
if (( oku )); then ok "web UI answers and reaches the backend"; else bad "web UI does not answer"; as_root journalctl -u "$FRONTEND_UNIT" -n 20 --no-pager; fi
for a in $(box_addresses); do info "open:  http://$a:$(ui_port)"; done
info "backup of this update: $BACKUP_DIR/$TS   (roll back: git checkout \$(cat $BACKUP_DIR/$TS/git-commit.txt); cp $BACKUP_DIR/$TS/.env .env; $0 --no-pull)"
(( okb && oku )) || exit 1
