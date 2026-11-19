#!/usr/bin/env python3
"""Pack every snapshot of the two-wheeler helmet module (the crossing events: compliant / missing / unverified / vehicle only)
into ONE zip, with a table of what the judge said about each.

    python3 backend/tools/twhelmet_train/pack_snapshots.py                  # everything
    python3 backend/tools/twhelmet_train/pack_snapshots.py 2026-11-19       # one day
    python3 backend/tools/twhelmet_train/pack_snapshots.py --since 2026-11-15
    python3 backend/tools/twhelmet_train/pack_snapshots.py --new            # only snapshots not packed before
    python3 backend/tools/twhelmet_train/pack_snapshots.py --label unverified   # one verdict only (compliant|missing|unverified|vehicle_only)

The zip lands in ~/twhelmet_snapshots_<stamp>.zip (or $OUT_DIR). Inside:
    <label>/<snapshot id>.jpg            the annotated crossing picture (what the UI shows)
    <label>/<snapshot id>_original.jpg   the clean frame, when the module saved one
    events.csv                           one line per snapshot: time, camera, track, verdict, category, reason, decidedAfterSec,
                                         per-rider votes / confidences / head size, the crossing box and the line
    events.json                          the same records in full
Paths come from the backend's own index (DATA_DIR/snapshots.json + detections.json) and SNAPSHOT_DIR, so it works whatever
AIV_SNAPSHOT_DIR / AIV_DATA_DIR are set to. Run it on the box, then copy the zip to the PC:  scp box:~/twhelmet_snapshots_*.zip .
"""
import csv
import json
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from backend import config  # noqa: E402

MODULE = "twhelmet"


def main(argv):
    mode, arg, label = "all", None, None
    args = list(argv)
    while args:
        a = args.pop(0)
        if a == "--new":
            mode = "new"
        elif a == "--since":
            mode, arg = "since", args.pop(0)
        elif a == "--label":
            label = args.pop(0).strip().lower().replace("-", "_")
        elif a.startswith("-"):
            print(__doc__); return 2
        else:
            mode, arg = "date", a
    data_dir = Path(config.DATA_DIR)
    snap_root = Path(config.SNAPSHOT_DIR).resolve()
    snaps = _load(data_dir / "snapshots.json")
    dets = {d.get("eventId"): d for d in _load(data_dir / "detections.json") if d.get("eventId")}
    seen_file = snap_root / f"{MODULE}_detection" / ".packed_snapshots"
    seen = set(seen_file.read_text().split()) if seen_file.exists() else set()
    rows = []
    for s in snaps:
        if s.get("module") != MODULE:
            continue
        day = (s.get("timestamp") or "")[:10]
        if mode == "date" and day != arg:
            continue
        if mode == "since" and day < arg:
            continue
        if mode == "new" and s["id"] in seen:
            continue
        lab = (s.get("type") or "").replace(f"{MODULE}_", "") or "unknown"
        if label and lab != label:
            continue
        rows.append((s, lab, dets.get(s.get("eventId"), {})))
    if not rows:
        print(f"nothing to pack ({mode}{' ' + arg if arg else ''}{' label ' + label if label else ''}) - "
              f"{sum(1 for s in snaps if s.get('module') == MODULE)} {MODULE} snapshots in the index")
        return 0
    out_dir = Path(os.environ.get("OUT_DIR") or Path.home())
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    zip_path = out_dir / f"{MODULE}_snapshots_{stamp}.zip"
    counts, missing, size = {}, 0, 0
    fields = ["snapshotId", "eventId", "timestamp", "cameraId", "label", "category", "confirmed", "verdict", "reason", "decidedAfterSec",
              "trackId", "direction", "confidence", "riders", "box", "line", "frameSize", "file", "originalFile"]
    records, csv_rows = [], []
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for s, lab, d in rows:
            rec = {k: s.get(k, d.get(k)) for k in ("eventId", "timestamp", "cameraId", "label", "category", "confirmed", "verdict", "reason",
                                                  "decidedAfterSec", "trackId", "direction", "confidence", "box", "line", "frameSize")}
            rec["snapshotId"] = s["id"]
            riders = s.get("riders") or d.get("riders") or []
            rec["riders"] = "; ".join(f"{r.get('verdict')} votes={','.join(map(str, r.get('votes') or []))} helmet={r.get('helmetConf')} "
                                      f"noHelmet={r.get('noHelmetConf')} headPx={r.get('headPx')} ({r.get('reason')})" for r in riders)
            rec["file"] = rec["originalFile"] = ""
            for key, suffix in (("path", ""), ("originalPath", "_original")):
                rel = s.get(key)
                if not rel:
                    continue
                src = snap_root / rel
                if not src.exists():
                    missing += 1
                    continue
                arc = f"{lab}/{s['id']}{suffix}.jpg"
                z.write(src, arc)
                size += src.stat().st_size
                rec["originalFile" if suffix else "file"] = arc
            counts[lab] = counts.get(lab, 0) + 1
            records.append(dict(d, snapshot=s, file=rec["file"], originalFile=rec["originalFile"]))
            csv_rows.append({k: (json.dumps(rec.get(k)) if k in ("box", "line", "frameSize") else rec.get(k)) for k in fields})
        import io
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fields)
        w.writeheader()
        w.writerows(csv_rows)
        z.writestr("events.csv", buf.getvalue())
        z.writestr("events.json", json.dumps(records, indent=1, default=str))
    seen_file.parent.mkdir(parents=True, exist_ok=True)
    with open(seen_file, "a") as f:                       # remembered for --new
        f.write("".join(s["id"] + "\n" for s, _l, _d in rows))
    print(f"{len(rows)} snapshots | " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())) + f" | {size / 1e6:.0f} MB of JPEG"
          + (f" | {missing} image file(s) listed in the index but missing on disk" if missing else ""))
    print(f"written {zip_path} ({zip_path.stat().st_size / 1e6:.0f} MB)")
    print(f"copy to the PC:  scp box:{zip_path} .")
    return 0


def _load(path):
    try:
        return json.load(open(path))
    except (OSError, ValueError):
        return []


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
