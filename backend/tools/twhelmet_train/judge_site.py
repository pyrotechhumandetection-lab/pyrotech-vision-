"""Run the shipped single-frame judge (backend.detection.crossing.judge) over a labelled own-camera set and score every labelled
head: does the judge confirm the right class, the wrong class, or leave it unverified? This is the number a client cares about
(box mAP on web photos is not), and it is per head position / size, which is what the camera-zone map is built from.

    AIV_TWHELMET_MODEL=<weights.pt> python3 judge_site.py --set <labelled dir> [--set ...] --raw <capture dir with the .json files> [--raw ...] \
        --json out.json [--hmin 0.6 --nmin 0.6 --min-head 28]

<labelled dir> = images/ + labels/ (YOLO: 0 bike, 1 helmet, 2 no-helmet). The .json next to each captured frame (riders linked to
each two-wheeler) gives the crop the engine would have judged: the linked person box, or the two-wheeler's area stretched upward
when no person was linked (judge.bike_rider_box) - exactly what the live module does at the line.
Output: one record per labelled head (label, verdict, confidences, headPx, centre, size, day, crop kind) and summary tables."""
import argparse
import collections
import glob
import json
import os
import sys

import cv2

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", ".."))
import backend  # noqa: E402,F401
from backend.detection.crossing import judge as jd  # noqa: E402

CLS = {1: "helmet", 2: "no-helmet"}


def centre_in(box, x, y, up=0.0):
    x1, y1, x2, y2 = box
    return x1 <= x <= x2 and y1 - up * (y2 - y1) <= y <= y2


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", action="append", required=True)
    ap.add_argument("--raw", action="append", required=True)
    ap.add_argument("--json", required=True)
    ap.add_argument("--hmin", type=float, default=0.6)
    ap.add_argument("--nmin", type=float, default=0.6)
    ap.add_argument("--min-head", type=int, default=28)
    a = ap.parse_args()
    metas = {}
    for r in a.raw:
        for jf in glob.glob(os.path.join(r, "**", "*.json"), recursive=True):
            metas[os.path.basename(jf)[:-5]] = json.load(open(jf))
    recs = []
    for s in a.set:
        for lf in sorted(glob.glob(os.path.join(s, "labels", "*.txt"))):
            base = os.path.basename(lf)[:-4]
            heads = []
            for l in open(lf):
                p = l.split()
                if len(p) == 5 and int(p[0]) in CLS:
                    heads.append((int(p[0]), *map(float, p[1:])))
            if not heads:
                continue
            img = cv2.imread(os.path.join(s, "images", base + ".jpg"))
            fh, fw = img.shape[:2]
            m = metas.get(base, {"twoWheelers": []})
            for c, cx, cy, w, h in heads:
                x, y, hw, hh = cx * fw, cy * fh, w * fw, h * fh
                pbox, kind, bike_area, bike = None, None, False, None
                # the engine judges the person linked to the two-wheeler; a head in the top 40 % of a person box belongs to it
                for t in m["twoWheelers"]:
                    for p in t.get("riders", []):
                        px1, py1, px2, py2 = p["box"]
                        if px1 <= x <= px2 and py1 - 0.1 * (py2 - py1) <= y <= py1 + 0.4 * (py2 - py1):
                            pbox, kind, bike = p["box"], "person", t["box"]
                if pbox is None:
                    for t in m["twoWheelers"]:
                        if centre_in(t["box"], x, y, up=1.0):
                            pbox, kind, bike_area, bike = jd.bike_rider_box(t["box"], fh, fw), "bike-area", True, t["box"]
                if pbox is None:                                   # no capture metadata: a stand-in rider box around the head
                    pbox, kind = [x - 2.5 * hw, y - 0.8 * hh, x + 2.5 * hw, y + 6 * hh], "synthetic"
                    pbox = [max(0, pbox[0]), max(0, pbox[1]), min(fw, pbox[2]), min(fh, pbox[3])]
                top = jd.head_crop(img, pbox, mx=1.0 if bike_area else 0.5)
                if top is None:
                    res = {"verdict": jd.UNCERTAIN, "helmetConf": 0, "noHelmetConf": 0, "reason": "rider too small to judge", "headPx": None}
                else:
                    res = jd.judge_crop(top, a.hmin, a.nmin, bike_area=bike_area, min_head_px=a.min_head)
                recs.append({"frame": base, "day": base.split("_")[1] if "_" in base else "", "label": CLS[c], "verdict": res["verdict"],
                             "helmetConf": res["helmetConf"], "noHelmetConf": res["noHelmetConf"], "reason": res["reason"],
                             "headPx": res.get("headPx"), "cx": round(x), "cy": round(y), "w": round(hw), "h": round(hh), "crop": kind,
                             # where the two-wheeler touches the ground (the point that crosses the line) - for zone_map.py --at ground
                             "gx": round((bike[0] + bike[2]) / 2) if bike else round(x), "gy": round(bike[3]) if bike else round(y + 5 * hh)})
    # summaries
    def table(rows):
        t = collections.Counter((r["label"], r["verdict"]) for r in rows)
        out = {}
        for lab in ("helmet", "no-helmet"):
            n = sum(v for (l, _v), v in t.items() if l == lab)
            out[lab] = {"n": n, "confirmed helmet": t[(lab, jd.HELMET)], "confirmed no-helmet": t[(lab, jd.MISSING)],
                        "unverified": n - t[(lab, jd.HELMET)] - t[(lab, jd.MISSING)]}
        conf = t[("helmet", jd.HELMET)] + t[("no-helmet", jd.MISSING)]
        wrong = t[("helmet", jd.MISSING)] + t[("no-helmet", jd.HELMET)]
        out["confirmed"] = conf + wrong
        out["confirmed right"] = conf
        out["confirmed wrong"] = wrong
        out["precision of confirmed"] = round(conf / (conf + wrong), 3) if conf + wrong else None
        out["coverage (confirmed / heads)"] = round((conf + wrong) / len(rows), 3) if rows else None
        return out
    summary = {"all": table(recs)}
    for d in sorted({r["day"] for r in recs}):
        summary["day " + d] = table([r for r in recs if r["day"] == d])
    for lo, hi in ((0, 28), (28, 40), (40, 60), (60, 1000)):
        rows = [r for r in recs if lo <= min(r["w"], r["h"]) < hi]
        if rows:
            summary[f"head {lo}-{hi} px"] = table(rows)
    json.dump({"weights": os.environ.get("AIV_TWHELMET_MODEL") or jd.CUSTOM, "hmin": a.hmin, "nmin": a.nmin, "minHeadPx": a.min_head,
               "heads": len(recs), "summary": summary, "records": recs}, open(a.json, "w"), indent=1)
    for k, v in summary.items():
        print(k, json.dumps(v))


if __name__ == "__main__":
    main()
