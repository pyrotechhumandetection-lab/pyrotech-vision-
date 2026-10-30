"""Compare two rider-helmet weight files: the installed one against a fine-tuned candidate.

    python3 compare_models.py --data data/twhelmet_in/data.yaml --site site_test old=../../models/twhelmet/rider_helmet.pt new=runs/in_v1/weights/best.pt

Two measurements, printed side by side, nothing assumed:
 1. box metrics (P / R / mAP50 / F1 per class) on the dataset's `val` and `test` splits (Ultralytics validator, conf 0.001);
 2. the project's own single-frame judgement on YOUR site photos, sorted by a person into
        <site>/helmet_detected/  <site>/helmet_not_detected/  <site>/only_vehicle_detected/
    (optional <site>/event_boxes.json = {"photo.jpg": [x1, y1, x2, y2]}: the two-wheeler each photo is about, so that
    other two-wheelers in the picture do not decide the row).
    Each photo goes through the same path as a live crossing frame: COCO detector -> riders linked to the two-wheeler
    (bike-area fallback when nobody is linked) -> judge_crop -> one frame-level verdict per two-wheeler. The table is the
    confusion matrix of that verdict against the folder; "confirmed wrong" is the number that matters for a client.
Single frames carry no votes, so live results (several frames + vote thresholds) are at least as cautious as this table."""
import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[3]))

import cv2  # noqa: E402

from backend.detection.crossing import engine as ce, judge as jd  # noqa: E402

CLASSES = ["helmet_detected", "helmet_not_detected", "only_vehicle_detected"]
VERDICT_TO_CLASS = {jd.HELMET: "helmet_detected", jd.MISSING: "helmet_not_detected", jd.NO_RIDER: "only_vehicle_detected"}


def box_metrics(weights, data, split):
    from ultralytics import YOLO
    r = YOLO(weights).val(data=data, split=split, imgsz=640, device="cpu", conf=0.001, iou=0.6, plots=False, verbose=False,
                          project="/tmp/aiv_compare", name="val", exist_ok=True)
    out = {"all": {"P": float(r.box.mp), "R": float(r.box.mr), "mAP50": float(r.box.map50), "mAP50-95": float(r.box.map)}}
    idx = r.box.ap_class_index.tolist()
    for i, n in r.names.items():
        if i in idx:
            k = idx.index(i)
            out[n] = {"P": float(r.box.p[k]), "R": float(r.box.r[k]), "mAP50": float(r.box.ap50[k]), "F1": float(r.box.f1[k])}
    return out


def use_weights(weights):
    """Point the judge at one weights file (what the backend does on start when rider_helmet.pt exists)."""
    from ultralytics import YOLO
    m = YOLO(weights, task="detect")
    jd._STATE.update(model=m, names={int(k): str(v).lower() for k, v in m.names.items()}, custom=True, tried=True, error=None)


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    i = ix * iy
    return i / ((a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - i + 1e-9)


def judge_photo(det, img, imgsz, hmin, nmin, event_box=None):
    """One frame-level verdict per two-wheeler in the photo, combined the way the engine combines riders of one crossing.
    With `event_box` (the two-wheeler the snapshot was taken for, frame pixels) only that two-wheeler is judged: the
    detector's box that overlaps it best, or the event box itself when the detector misses it in this one frame (the
    live tracker still had it)."""
    r = det.predict(img, imgsz=imgsz, conf=0.3, classes=[0, 3], verbose=False)[0]
    dets = [{"cls": "person" if int(b.cls) == 0 else "motorcycle", "box": b.xyxy[0].tolist(), "conf": float(b.conf), "tid": None} for b in r.boxes]
    persons = [d for d in dets if d["cls"] == "person"]
    bikes = [d for d in dets if d["cls"] == "motorcycle"]
    if event_box is not None:
        best = max(bikes, key=lambda d: _iou(d["box"], event_box), default=None)
        bikes = [best] if best is not None and _iou(best["box"], event_box) >= 0.2 else [{"cls": "motorcycle", "box": list(event_box), "conf": 1.0, "tid": None}]
    if not bikes:
        return "no_two_wheeler", []
    riders = ce.associate_riders(persons, bikes)
    h, w = img.shape[:2]
    per_bike = []
    reasons = []
    for vi, bike in enumerate(bikes):
        vs = []
        for key, _pid, rbox in ce.CrossingEngine._rider_items(riders.get(vi, []), persons, bike, h, w)[:3]:
            top = jd.head_crop(img, rbox, 1.0 if key == "bike-area" else 0.5)
            if top is None:
                vs.append(jd.UNCERTAIN); reasons.append("rider too small"); continue
            res = jd.judge_crop(top, hmin, nmin, bike_area=(key == "bike-area"))
            vs.append(res["verdict"]); reasons.append(res["reason"])
        if not vs:
            per_bike.append(jd.UNCERTAIN)
        elif jd.MISSING in vs:
            per_bike.append(jd.MISSING)
        elif all(v == jd.HELMET for v in vs):
            per_bike.append(jd.HELMET)
        elif all(v == jd.NO_RIDER for v in vs):
            per_bike.append(jd.NO_RIDER)
        else:
            per_bike.append(jd.UNCERTAIN)
    # the photo's class is decided by the most informative two-wheeler in it: a rider verdict beats "vehicle only"
    for v in (jd.MISSING, jd.HELMET, jd.UNCERTAIN, jd.NO_RIDER):
        if v in per_bike:
            return VERDICT_TO_CLASS.get(v, "unverified"), reasons
    return "unverified", reasons


def site_eval(weights, site, hmin, nmin):
    use_weights(weights)
    det, _eng, imgsz, _p = ce.load_model()
    conf = {c: Counter() for c in CLASSES}
    why = defaultdict(Counter)
    eb = Path(site) / "event_boxes.json"                       # optional: {"photo.jpg": [x1, y1, x2, y2]} of the event's two-wheeler
    boxes = json.loads(eb.read_text()) if eb.is_file() else {}
    for c in CLASSES:
        for f in sorted((Path(site) / c).glob("*.jpg")):
            img = cv2.imread(str(f))
            if img is None:
                continue
            got, reasons = judge_photo(det, img, imgsz, hmin, nmin, boxes.get(f.name))
            conf[c][got] += 1
            for r in reasons:
                why[c][r] += 1
    return conf, why


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", help="data.yaml of the labelled dataset (box metrics on its val and test splits)")
    ap.add_argument("--site", help="folder with helmet_detected/ helmet_not_detected/ only_vehicle_detected/")
    ap.add_argument("--hmin", type=float, default=0.6)
    ap.add_argument("--nmin", type=float, default=0.6)
    ap.add_argument("--json", help="write all numbers here")
    ap.add_argument("models", nargs="+", help="name=weights.pt ...")
    a = ap.parse_args()
    models = dict(m.split("=", 1) for m in a.models)
    report = {}
    for name, wts in models.items():
        rep = report.setdefault(name, {"weights": wts})
        if a.data:
            for split in ("val", "test"):
                rep[split] = box_metrics(wts, a.data, split)
        if a.site:
            conf, why = site_eval(wts, a.site, a.hmin, a.nmin)
            rep["site"] = {c: dict(conf[c]) for c in CLASSES}
            rep["site_reasons"] = {c: dict(why[c]) for c in CLASSES}
    if a.data:
        for split in ("val", "test"):
            print(f"\n== box metrics, {split} split ==")
            print(f"{'class':<14}" + "".join(f"{n + ' mAP50':>16}{n + ' P':>10}{n + ' R':>10}" for n in models))
            for cls in ("all", "bike", "helmet", "no-helmet"):
                row = f"{cls:<14}"
                for n in models:
                    m = report[n][split].get(cls)
                    row += f"{m['mAP50']:>16.3f}{m['P']:>10.3f}{m['R']:>10.3f}" if m else f"{'-':>16}{'-':>10}{'-':>10}"
                print(row)
    if a.site:
        cols = CLASSES + ["unverified", "no_two_wheeler"]
        for n in models:
            print(f"\n== site photos, {n} ({models[n]}) : rows = truth (folder), columns = frame-level result ==")
            print(f"{'truth \\ result':<24}" + "".join(f"{c[:18]:>20}" for c in cols))
            wrong = confirmed = total = 0
            for c in CLASSES:
                r = report[n]["site"][c]
                print(f"{c:<24}" + "".join(f"{r.get(k, 0):>20}" for k in cols))
                total += sum(r.values())
                confirmed += sum(r.get(k, 0) for k in CLASSES)
                wrong += sum(r.get(k, 0) for k in CLASSES if k != c)
            print(f"confirmed {confirmed} of {total} photos, confirmed WRONG {wrong} (the number to minimise), "
                  f"left unverified {total - confirmed - sum(report[n]['site'][c].get('no_two_wheeler', 0) for c in CLASSES)}")
    if a.json:
        Path(a.json).write_text(json.dumps(report, indent=1))
        print("\nwritten", a.json)


if __name__ == "__main__":
    main()
