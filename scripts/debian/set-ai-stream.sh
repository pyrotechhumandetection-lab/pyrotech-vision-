#!/usr/bin/env bash
# ==========================================================================================
#  PYROTECH VISION - choose the stream the AI analyses, per camera (the live view / snapshots keep the
#  main stream). Uses the running backend's API on this box; camera passwords are never shown or changed.
#
#      scripts/debian/set-ai-stream.sh list
#      scripts/debian/set-ai-stream.sh sub   CAM-ID [CAM-ID ...] | --all   substream derived from the camera URL
#                                                                         (Hikvision /Streaming/Channels/101 -> 102,
#                                                                         /h264/ch1/main -> sub, Dahua subtype=0 -> 1)
#      scripts/debian/set-ai-stream.sh custom CAM-ID /Streaming/Channels/102   any path on the same camera
#      scripts/debian/set-ai-stream.sh same  CAM-ID [...] | --all             AI uses the viewing stream again
#
#  The camera's running detections reconnect automatically. Snapshot originals stay full resolution
#  (snapshotSource = main). Set the substream itself on the camera: H.264, 640x360 or 704x576, 10-15 fps.
# ==========================================================================================
set -euo pipefail
# shellcheck source=scripts/debian/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/common.sh"
[[ $# -ge 1 ]] || { sed -n '2,17p' "$0"; exit 2; }
API="http://127.0.0.1:$(api_port)/api"
python3 - "$API" "$@" <<'PY'
import json, re, sys, urllib.error, urllib.request
api, cmd, args = sys.argv[1], sys.argv[2], sys.argv[3:]

def call(method, path, body=None):
    req = urllib.request.Request(api + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")
    except Exception as e:
        sys.exit(f"ERROR: backend not reachable at {api} ({e}) - is pyrotech-vision-backend running?")

mask = lambda u: re.sub(r"://[^/@\s]*@", "://***@", u or "")
st, cams = call("GET", "/cameras")
cams = cams if isinstance(cams, list) else (cams or {}).get("items", [])
if cmd == "list":
    print(f"{'camera':16} {'AI stream':10} {'snapshots':9} source")
    for c in cams:
        ai = c.get("aiStream") or "same"
        print(f"{c['id']:16} {ai + ('=' + c.get('aiCustomStream', '') if ai == 'custom' else ''):10} {c.get('snapshotSource') or 'main':9} {mask(c.get('sourceUrl'))}")
    sys.exit(0)
if cmd not in ("sub", "custom", "same"):
    sys.exit("usage: list | sub CAM... | custom CAM PATH | same CAM...   (--all = every camera)")
if cmd == "custom":
    if len(args) != 2 or not args[1].startswith("/"):
        sys.exit("usage: custom CAM-ID /path/of/the/substream")
    targets, path = [args[0]], args[1]
else:
    targets, path = ([c["id"] for c in cams] if args == ["--all"] else args), None
if not targets:
    sys.exit("name at least one camera id (or --all)")
bad = 0
for cid in targets:
    st, cam = call("GET", "/cameras/" + cid)
    if st != 200:
        print(f"{cid}: not found"); bad += 1; continue
    body = {k: v for k, v in cam.items() if k not in ("status", "lastError", "lastConnectedAt", "hasCredentials", "createdAt",
                                                      "updatedAt", "source", "ip", "host")}
    body["aiStream"] = cmd
    body["aiCustomStream"] = path or ""
    body["snapshotSource"] = "main"
    st, r = call("PUT", "/cameras/" + cid, body)
    if st == 200:
        print(f"{cid}: AI stream = {cmd}{' ' + path if path else ''} (snapshots from the main stream)")
    else:
        bad += 1
        print(f"{cid}: NOT changed - {(r or {}).get('message')}")
        if cmd == "sub":
            print("   the substream cannot be derived from this URL - use:  custom " + cid + " /path/of/the/substream")
sys.exit(1 if bad else 0)
PY
