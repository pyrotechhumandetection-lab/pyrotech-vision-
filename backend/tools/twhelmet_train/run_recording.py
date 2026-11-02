"""Offline run of the real two-wheeler helmet engine on a recording, for measuring weights / settings before deploying them.

    python3 run_recording.py <video> <out.json> <rider weights> <zone 0|1> [max_frames] [--line x1,y1,x2,y2] [--screen]

Frames go through CrossingEngine.process() synchronously, in order, with the video's own timestamps - the same code path as
live. Output JSON: every crossing (label, category, why, travel, riders with votes / confidences / headPx), counts, tracker
stats, engine ms per frame; annotated crossing frames in <out.json>_snaps/ for a person to check by eye.
--line: the line in normalised picture coordinates (default: the entrance-road line used for the 2026-11-02 measurement).
--screen: the video is a screen recording of the operator page: the camera picture is located in every frame
(locate_screen_recording.py), cut out and scaled to 1920x1080 so pixel thresholds (minHeadPx) mean the same as live;
frames where the picture is not visible are skipped. Without --screen the whole frame is the camera picture.
Used 2026-11-02 on a 9-minute recording, v1 / v2 x zone off / on: crossings found 2 -> 10 with the zone, helmet verdicts
0 confirmed with either model (readable bare heads scored no-helmet 0.47-0.53, below the 0.6 bar), one wrong "vehicle only"
on an 80 px bike at the picture edge -> the bike-height gate for that verdict."""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3]))
sys.path.insert(0, str(HERE.parent))
os.environ["AIV_TWHELMET_MODEL"] = sys.argv[3]
import cv2  # noqa: E402
import numpy as np  # noqa: E402
import backend  # noqa: E402,F401
from backend.detection.crossing import engine as ce, judge as jd  # noqa: E402
from backend.detection.modules import DEFAULTS  # noqa: E402
from locate_screen_recording import locate  # noqa: E402

video, out_path, zone = sys.argv[1], sys.argv[2], sys.argv[4] == "1"
rest = sys.argv[5:]
max_frames = int(rest[0]) if rest and rest[0].isdigit() else 10 ** 9
SCREEN = "--screen" in rest
LINE = [0.62, 0.06, 0.62, 0.42]
if "--line" in rest:
    LINE = [float(v) for v in rest[rest.index("--line") + 1].split(",")]
events = []
eng = ce.CrossingEngine(lambda mod, ev, snap, fr: events.append((mod, ev, snap)), camera_id="vid", depth_max=1)
cfg = json.loads(json.dumps(DEFAULTS["twhelmet"]))
cfg.update(line={"x1": LINE[0], "y1": LINE[1], "x2": LINE[2], "y2": LINE[3], "refWidth": 1920, "refHeight": 1080},
           zoneAroundLine=zone, zoneMargin=0.25, minTrackFrames=2, snapshot={"mode": "crossing"})
eng.set_module("twhelmet", cfg)
cap = cv2.VideoCapture(video); fps = cap.get(5) or 10.0; n = int(cap.get(7))
seen = skipped = 0; ms = []
t0 = time.time()
for fi in range(min(n, max_frames)):
    ok, f = cap.read()
    if not ok:
        break
    if SCREEN:
        box = locate(f)
        if box is None:
            skipped += 1; continue
        x1, y1, x2, y2 = box
        frame = cv2.resize(f[y1:y2, x1:x2], (1920, 1080), interpolation=cv2.INTER_CUBIC)
    else:
        frame = f
    t = time.perf_counter()
    eng.process(frame, fi / fps, fi)
    ms.append((time.perf_counter() - t) * 1000); seen += 1
    if seen % 500 == 0:
        print(f"{seen} frames, {len(events)} events, {np.mean(ms[-500:]):.0f} ms/frame", flush=True)
time.sleep(3)                       # let the judge thread finalise the last crossings
eng.close()
view = eng.view("twhelmet")
rows = []
for mod, ev, snap in events:
    rows.append({"frameId": ev["frameId"], "t": round(ev["t"], 1), "tid": ev["tid"], "direction": ev["direction"], "label": ev["label"],
                 "category": ev.get("category"), "why": ev.get("why"), "travel": ev.get("travel"), "box": ev["box"],
                 "riders": [{k: r.get(k) for k in ("personId", "verdict", "votes", "helmetConf", "noHelmetConf", "headPx", "reason")} for r in ev.get("riders") or []]})
    if snap is not None:
        os.makedirs(out_path + "_snaps", exist_ok=True)
        cv2.imwrite(f"{out_path}_snaps/{ev['frameId']:06d}_{ev['tid']}_{(ev.get('category') or 'unverified').replace(' ', '_')}.jpg", snap)
res = {"video": video, "weights": sys.argv[3], "zone": zone, "frames": seen, "skipped": skipped, "events": rows,
       "counts": view["counts"], "crossings": view["crossings"], "tracking": view.get("tracking"),
       "engine_ms_mean": float(np.mean(ms)), "engine_ms_p95": float(np.percentile(ms, 95)), "wall_s": time.time() - t0,
       "judge": jd.status(), "metrics": eng.metrics()}
json.dump(res, open(out_path, "w"), indent=1, default=str)
print("events", len(rows), "counts", view["counts"], "engine ms mean %.0f p95 %.0f" % (res["engine_ms_mean"], res["engine_ms_p95"]), "skipped", skipped)
