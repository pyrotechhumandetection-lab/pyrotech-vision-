#!/usr/bin/env bash
# PYROTECH VISION - status of the services, step by step:
#   service process -> listening ports -> web UI -> backend API -> model files -> per camera:
#   connected / AI model loaded / inference running -> detections resumed after restart -> recent errors
#
#   scripts/debian/status-services.sh [--wait SECONDS]
#     --wait N   wait up to N seconds for the API to answer (used after start / restart)
# Exit code 0 = both services active and the API answers, 1 = something is not ready.
set -euo pipefail
# shellcheck source=scripts/debian/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"

WAIT=0
while (($#)); do
  case "$1" in
    --wait) WAIT="${2:?--wait needs seconds}"; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) die "Unknown option: $1" ;;
  esac
  shift
done

UI_PORT="$(ui_port)"; API_PORT="$(api_port)"
RC=0

say "1. Service processes"
for u in "${UNITS[@]}"; do
  if ! unit_installed "$u"; then bad "$u is not installed (scripts/debian/install-services.sh)"; RC=1; continue; fi
  active="$(systemctl is-active "$u" 2>/dev/null || true)"
  enabled="$(systemctl is-enabled "$u" 2>/dev/null || true)"
  pid="$(systemctl show -p MainPID --value "$u")"
  restarts="$(systemctl show -p NRestarts --value "$u" 2>/dev/null || echo '?')"
  since="$(systemctl show -p ActiveEnterTimestamp --value "$u")"
  mem="$(systemctl show -p MemoryCurrent --value "$u" 2>/dev/null || true)"
  [[ "$mem" =~ ^[0-9]+$ ]] && mem="$((mem / 1048576)) MB" || mem="-"
  line="$u: $active, boot start: $enabled, PID ${pid:-0}, memory $mem, automatic restarts: ${restarts:-0}, since ${since:--}"
  if [[ "$active" == "active" ]]; then ok "$line"; else bad "$line"; RC=1; fi
  [[ "$enabled" == "enabled" ]] || note "$u does not start at boot (sudo systemctl enable $u)"
done

if (( WAIT > 0 )); then
  for _ in $(seq 1 "$WAIT"); do
    [[ "$(http_status "http://127.0.0.1:$API_PORT/api/system/health" 2)" == "200" ]] && break
    sleep 1
  done
fi

say "2. Listening ports"
ui_l="$(ss -ltnH "sport = :$UI_PORT" 2>/dev/null | awk '{print $4}' | paste -sd' ' -)"
api_l="$(ss -ltnH "sport = :$API_PORT" 2>/dev/null | awk '{print $4}' | paste -sd' ' -)"
if [[ -n "$ui_l" ]]; then ok "web UI port $UI_PORT: $ui_l"; else bad "nothing listens on web UI port $UI_PORT"; RC=1; fi
if [[ -n "$api_l" ]]; then
  ok "backend port $API_PORT: $api_l"
  [[ "$api_l" == *127.0.0.1:* ]] || note "the backend listens beyond 127.0.0.1 - it should only be reached through the web UI"
else bad "nothing listens on backend port $API_PORT"; RC=1; fi

say "3. Web UI, API and detection state"
python3 - "$UI_PORT" "$API_PORT" <<'PY' || RC=1
import json, sys, time, urllib.request, urllib.error
from datetime import datetime, timezone
ui, api = sys.argv[1], sys.argv[2]
OK, FAIL, WARN, INFO = "  \033[1;32m[ OK ]\033[0m", "  \033[1;31m[FAIL]\033[0m", "  \033[1;33m[WARN]\033[0m", "  [INFO]"
rc = 0

def get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return 0, str(e).encode()

st, _ = get(f"http://127.0.0.1:{ui}/")
if st == 200: print(OK, f"web UI answers (http://127.0.0.1:{ui}/)")
else: print(FAIL, f"web UI does not answer (HTTP {st})"); rc = 1

st, body = get(f"http://127.0.0.1:{api}/api/system/health")
health = None
if st == 200:
    health = json.loads(body)
    print(OK, f"backend API answers - version {health.get('version')}, engine {health.get('engine')}")
else:
    print(FAIL, f"backend API does not answer (HTTP {st}) - still starting? journalctl -u pyrotech-vision-backend -n 50"); rc = 1
st2, _ = get(f"http://127.0.0.1:{ui}/api/system/health")
if st2 == 200: print(OK, "web UI forwards /api to the backend (what browsers on other PCs use)")
elif health: print(FAIL, f"web UI cannot reach the backend (HTTP {st2}) - BACKEND_URL / AIV_PORT mismatch? re-run install-services.sh"); rc = 1

if health:
    if health.get("modelAvailable"): print(OK, f"model files present ({health.get('modelFile')}; models load when a detection starts)")
    else: print(FAIL, f"model file missing: {health.get('modelFile')}"); rc = 1
    st, body = get(f"http://127.0.0.1:{api}/api/cameras")
    cams = {c["id"]: c for c in (json.loads(body) if st == 200 else [])}
    st, body = get(f"http://127.0.0.1:{api}/api/system/metrics")
    met = json.loads(body) if st == 200 else {}
    sessions = met.get("sessions", [])
    if met.get("processCpu") is not None:
        cores = met.get("cpuCores"); threads = met.get("cpuThreads")
        print(INFO, f"backend process: CPU {met['processCpu']}% of one core ({cores} cores / {threads} threads on this box), "
                    f"RAM {met.get('processRssMb')} MB; box RAM free {met.get('ramAvailableMb')} MB")
    print(INFO, f"{len(cams)} camera(s) configured, {len(sessions)} open")
    now = datetime.now(timezone.utc)
    for s in sessions:
        cid, state, mods = s.get("cameraId"), s.get("state"), ", ".join(s.get("modules") or []) or "-"
        name = (cams.get(cid) or {}).get("name", "")
        age = None
        if s.get("lastFrameAt"):
            try: age = (now - datetime.fromisoformat(s["lastFrameAt"])).total_seconds()
            except ValueError: age = None
        connected = state in ("connected", "processing") and age is not None and age < 15
        inferring = state == "processing" and (s.get("framesProcessed") or 0) > 0 and connected
        tag = OK if inferring else (WARN if connected else FAIL)
        res = f" ({s['resolution']})" if s.get("resolution") else ""
        model = f"model loaded ({s['engine']})" if s.get("engine") else "model loaded per module"
        infer = f"running {s.get('processingFps') or 0} fps" if inferring else "NOT running"
        detail = (f"camera {'connected' if connected else 'NOT connected'}{res}, AI {model}, "
                  f"inference {infer}, frames analysed {s.get('framesProcessed') or 0}")
        print(tag, f"{cid} {name} [{mods}]: state {state} - {detail}")
        ai = s.get("aiStream") or {}
        extra = [f"AI stream {ai.get('active', 'same')} {s.get('aiResolution') or ''}".strip()]
        if ai.get("requested") not in (None, "same") and ai.get("active") == "main":
            extra.append(f"FALLBACK to main ({ai.get('reason')})")
        if s.get("queueCapacity"):
            extra.append(f"queue {s.get('queueLen', 0)}/{s['queueCapacity']}, latency {s.get('latencyMs')} ms")
        extra.append(f"dropped {s.get('framesDropped', 0)} (app), {s.get('cameraGaps', 0)} missing in stream")
        if s.get("fireAnalysedFps") is not None:
            extra.append(f"fire model {s['fireAnalysedFps']} fps")
        print("        " + " | ".join(extra))
    if not sessions:
        print(INFO, "no detection running (start one in the web UI; running detections resume after a restart)")
    a = health.get("autostart") or {}
    if a:
        rem = a.get("remembered") or []
        if not a.get("enabled"): print(INFO, "resume after restart: off (AIV_AUTOSTART=0)")
        elif rem:
            for r in rem:
                t = OK if r.get("status") == "running" else (FAIL if r.get("status") == "failed" else WARN)
                msg = f" - {r['message']}" if r.get("message") and r.get("status") != "running" else ""
                print(t, f"resume after restart: {r['module']} on {r['cameraId']}: {r.get('status')}{msg}")
        else: print(INFO, "resume after restart: nothing to resume")
sys.exit(rc)
PY

say "4. Recent backend warnings and errors (last hour)"
# the backend logs to stdout (journal priority "info"), so match the log level text
if out="$(journalctl -u "$BACKEND_UNIT" --since "-1h" --no-pager -q -o cat 2>&1)"; then
  out="$(printf '%s\n' "$out" | grep -E ' (WARNING|ERROR|CRITICAL) |Traceback' | grep -v 'DPAPI unavailable' | tail -n 8 || true)"
  if [[ -n "$out" ]]; then printf '%s\n' "$out" | sed 's/^/  /'; else info "none"; fi
else
  info "no permission to read the journal - run with sudo, or: sudo usermod -aG systemd-journal $(id -un)"
fi

say "Access"
mapfile -t ADDRS < <(box_addresses)
for a in "${ADDRS[@]}"; do echo "  http://$a:$UI_PORT"; done
((${#ADDRS[@]})) || echo "  http://<box-ip>:$UI_PORT"
echo "  Logs: journalctl -u $BACKEND_UNIT -f    journalctl -u $FRONTEND_UNIT -f"
exit "$RC"
