"""Measure the two-wheeler helmet judge on YOUR labelled rider photos (nothing is assumed, nothing is retrained).

    python3 backend/tools/eval_twhelmet.py <folder> [--hmin 0.5] [--nmin 0.6]

<folder>/helmet/*.jpg      photos of a rider WITH a helmet (one two-wheeler + rider per photo)
<folder>/no_helmet/*.jpg   photos of a rider WITHOUT a helmet

Each photo: the project's COCO detector finds the motorcycle and its rider, the helmet judge (stock PPE detector, or
backend/models/twhelmet/rider_helmet.pt when present) answers HELMET / MISSING / UNCERTAIN. The table shows how often
each answer was given per label, so you can read precision / recall of the rule on YOUR footage before relying on it.
A photo with no rider found is counted separately (detection problem, not a judge problem)."""
import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import cv2  # noqa: E402

from backend.detection.crossing import engine as ce, judge as jd  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--hmin", type=float, default=0.6)
    ap.add_argument("--nmin", type=float, default=0.6)
    a = ap.parse_args()
    model, engine, imgsz, _p = ce.load_model()
    print("judge:", jd.status())
    res = {"helmet": Counter(), "no_helmet": Counter()}
    for label in res:
        for f in sorted((Path(a.folder) / label).glob("*")):
            img = cv2.imread(str(f))
            if img is None:
                continue
            r = model.predict(img, imgsz=imgsz, conf=0.3, classes=[0, 3], verbose=False)[0]
            dets = [{"cls": "person" if int(b.cls) == 0 else "motorcycle", "box": b.xyxy[0].tolist(), "conf": float(b.conf), "tid": None} for b in r.boxes]
            persons = [d for d in dets if d["cls"] == "person"]
            bikes = [d for d in dets if d["cls"] == "motorcycle"]
            riders = ce.associate_riders(persons, bikes)
            if not riders:
                res[label]["NO_RIDER_FOUND"] += 1
                continue
            pi = riders[next(iter(riders))][0]
            res[label][jd.judge_rider(img, persons[pi]["box"], a.hmin, a.nmin)["verdict"]] += 1
    print(f"{'label':<11}{'HELMET':>9}{'MISSING':>9}{'UNCERTAIN':>11}{'NO_RIDER':>10}{'total':>7}")
    for label, c in res.items():
        print(f"{label:<11}{c['HELMET']:>9}{c['MISSING']:>9}{c['UNCERTAIN']:>11}{c['NO_RIDER_FOUND']:>10}{sum(c.values()):>7}")
    h, n = res["helmet"], res["no_helmet"]
    fa = h["MISSING"]; tp = n["MISSING"]
    print(f"false 'missing' on helmeted riders: {fa} of {sum(h.values())};  'missing' found on bare-head riders: {tp} of {sum(n.values())}")
    print(f"'helmet' wrongly given to bare-head riders: {n['HELMET']} of {sum(n.values())}")


if __name__ == "__main__":
    main()
