"""Harvest training material from YOUR cameras: tracked two-wheelers -> sharp, well-spread crops + pre-labels to correct.

    python3 harvest.py --source clip.mp4 --out to_label [--crop x1,y1,x2,y2] [--stride 1] [--per-track 6]

For every two-wheeler the tracker follows (COCO detector + ByteTrack, same as the live module) it keeps up to --per-track frames
spread over the track (sharpest first, large enough to see a head), cuts the bike + riders with a margin and writes
  to_label/images/<clip>_<track>_<n>.jpg   and   to_label/labels/<same>.txt   (YOLO format, classes from classes.py)
The label file is a PRE-LABEL from the installed rider_helmet.pt: open the folder in CVAT / Label Studio / Roboflow, CORRECT every
box, add the missed ones (bare heads, helmets, plates, the bike). Tracks whose helmet answer was unsure or contradictory are
listed first in manifest.csv ('hard' = 1): those are the pictures the model learns most from.
Pre-labels are never ground truth. Only boxes a person has checked belong in a training set."""
import argparse
import csv
import sys
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[2]))
from classes import CLASSES  # noqa: E402


def sharpness(img):
    return float(cv2.Laplacian(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), cv2.CV_64F).var())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--crop", help="x1,y1,x2,y2 of the camera picture inside the video (e.g. a screen recording)")
    ap.add_argument("--stride", type=int, default=1)
    ap.add_argument("--per-track", type=int, default=6)
    ap.add_argument("--min-bike-px", type=int, default=70, help="skip bikes whose box is lower than this")
    ap.add_argument("--imgsz", type=int, default=960)
    ap.add_argument("--conf", type=float, default=0.2)
    a = ap.parse_args()

    from ultralytics import YOLO
    from backend import config
    from backend.detection.crossing import engine as ce
    coco = YOLO(str(config.BACKEND_DIR / "models" / "yolov8n.pt"))
    rider = YOLO(str(config.BACKEND_DIR / "models" / "twhelmet" / "rider_helmet.pt"))
    names = {int(k): str(v).lower() for k, v in rider.names.items()}
    out = Path(a.out)
    (out / "images").mkdir(parents=True, exist_ok=True)
    (out / "labels").mkdir(parents=True, exist_ok=True)
    cap = cv2.VideoCapture(a.source)
    box = [int(v) for v in a.crop.split(",")] if a.crop else None
    cands, i = {}, -1                                           # track id -> [(sharp, frame_no, bike_box, frame)]
    while True:
        ok, fr = cap.read()
        if not ok:
            break
        i += 1
        if i % a.stride:
            continue
        if box:
            fr = fr[box[1]:box[3], box[0]:box[2]]
        r = coco.track(fr, imgsz=a.imgsz, conf=a.conf, classes=[0, 3], persist=True, tracker=str(config.BACKEND_DIR / "models" / "bytetrack_stable.yaml"), verbose=False)[0]
        if r.boxes is None or r.boxes.id is None:
            continue
        for b in r.boxes:
            if int(b.cls) != 3:
                continue
            x1, y1, x2, y2 = (float(v) for v in b.xyxy[0])
            if y2 - y1 < a.min_bike_px:
                continue
            crop = fr[max(0, int(y1 - 0.9 * (y2 - y1))):int(y2 + 0.1 * (y2 - y1)), max(0, int(x1 - 0.25 * (x2 - x1))):int(x2 + 0.25 * (x2 - x1))]
            if crop.size == 0:
                continue
            cands.setdefault(int(b.id), []).append((sharpness(crop), i, crop))
    rows = []
    stem = Path(a.source).stem
    for tid, lst in cands.items():
        lst.sort(key=lambda t: t[1])
        pick = [lst[int(k * (len(lst) - 1) / max(1, a.per_track - 1))] for k in range(min(a.per_track, len(lst)))] if len(lst) > a.per_track else lst
        verdicts = []
        for sh, fno, crop in pick:
            res = rider.predict(crop, imgsz=640, conf=0.25, verbose=False)[0]
            h, w = crop.shape[:2]
            lines, kinds = [], set()
            for b in res.boxes:
                n = names.get(int(b.cls), "")
                cls = {"helmet": "helmet", "no-helmet": "no-helmet", "number-plate": "number-plate", "bike": "bike"}.get(n)
                if cls is None:
                    continue
                kinds.add(cls)
                x1, y1, x2, y2 = (float(v) for v in b.xyxy[0])
                lines.append(f"{CLASSES.index(cls)} {(x1 + x2) / 2 / w:.6f} {(y1 + y2) / 2 / h:.6f} {(x2 - x1) / w:.6f} {(y2 - y1) / h:.6f}")
            verdicts.append("H" if "helmet" in kinds and "no-helmet" not in kinds else "M" if "no-helmet" in kinds and "helmet" not in kinds else "?")
            name = f"{stem}_t{tid}_{fno}"
            cv2.imwrite(str(out / "images" / f"{name}.jpg"), crop, [cv2.IMWRITE_JPEG_QUALITY, 95])
            (out / "labels" / f"{name}.txt").write_text("\n".join(lines) + ("\n" if lines else ""))
            rows.append({"image": f"{name}.jpg", "track": tid, "frame": fno, "sharpness": round(sh, 1), "prelabel": verdicts[-1]})
        hard = len(set(verdicts)) > 1 or "?" in verdicts
        for r_ in rows:
            if r_["track"] == tid:
                r_["hard"] = int(hard)
    rows.sort(key=lambda r_: (-r_["hard"], r_["track"], r_["frame"]))
    with open(out / "manifest.csv", "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["image", "track", "frame", "sharpness", "prelabel", "hard"])
        wr.writeheader()
        wr.writerows(rows)
    (out / "data.yaml").write_text("path: .\ntrain: images\nval: images\nnames:\n" + "".join(f"  {k}: {v}\n" for k, v in enumerate(CLASSES)))
    print(f"{len(cands)} two-wheeler tracks, {len(rows)} images, {sum(r_['hard'] for r_ in rows)} flagged hard -> {out}")


if __name__ == "__main__":
    main()
