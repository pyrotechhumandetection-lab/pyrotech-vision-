#!/usr/bin/env python3
"""PYROTECH VISION - vehicle detection benchmark on YOUR camera footage (Colab T4 or any machine).

Measures, never estimates:
  * speed per setting: input size (imgsz) x precision (FP32 / FP16 on CUDA) - ms per frame (p50 / p95),
    frames per second, peak GPU memory
  * detections per setting: vehicles per frame, small vehicles (< 32x32 px), FP16-vs-FP32 agreement
  * accuracy per setting when you give labels (--labels): precision / recall / F1 per vehicle class and
    for small vehicles, for a grid of confidence thresholds and NMS IoU values
  * without labels: only a PROXY ("agreement with the high-confidence detections of the largest FP32
    setting") - it is printed as a proxy, it is NOT accuracy, and no threshold is recommended from it.

Steps (Colab, project folder, backend running with Vehicle Detection on the camera):
  1) capture representative frames from the running session (no camera login needed, clean frames):
       backend/.venv/bin/python scripts/colab/bench_vehicle.py capture --camera COLAB-CAM1 --count 300 --interval 0.5
     or use a recording:  ... capture --video clip.mp4 --count 300
  2) (optional, recommended) label a subset in YOLO format (COCO class ids: 1 bicycle, 2 car, 3 motorcycle,
     5 bus, 7 truck), one <frame>.txt per image, e.g. with CVAT / Label Studio export "YOLO 1.1"
  3) STOP detection in the UI (the benchmark must have the GPU to itself), then
       backend/.venv/bin/python scripts/colab/bench_vehicle.py run --frames backend/data/bench/frames/COLAB-CAM1 \
            [--labels DIR] [--imgsz 640,960,1280] [--target-fps 15]
Results: backend/data/bench/vehicle-<time>/results.json + report.md (printed too).
"""
import argparse
import json
import os
import statistics
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

VEHICLE_COCO = {1: "2-Wheeler", 3: "2-Wheeler", 2: "4-Wheeler", 5: "Bus", 7: "Truck"}
CLASS_THR = {"2-Wheeler": 0.35, "4-Wheeler": 0.35, "Bus": 0.55, "Truck": 0.55}     # current module defaults
SMALL_PX = 32 * 32
OUT_BASE = ROOT / "backend" / "data" / "bench"


# ------------------------------------------------------------------ capture
def capture(a):
    import cv2
    out = Path(a.out or OUT_BASE / "frames" / (a.camera or Path(a.video).stem))
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    if a.video:
        cap = cv2.VideoCapture(a.video)
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        step = max(1, total // a.count) if total else 1
        i = 0
        while n < a.count:
            ok, f = cap.read()
            if not ok:
                break
            if i % step == 0:
                cv2.imwrite(str(out / f"f{n:05d}.jpg"), f, [cv2.IMWRITE_JPEG_QUALITY, 95])
                n += 1
            i += 1
    else:
        url = a.api.rstrip("/") + f"/cameras/{a.camera}/frame.jpg"
        last = None
        while n < a.count:
            try:
                with urllib.request.urlopen(url, timeout=10) as r:
                    data = r.read()
            except Exception as e:
                sys.exit(f"ERROR: {url}: {e} (is Vehicle Detection running on {a.camera}?)")
            if data != last:                      # skip an unchanged frame
                (out / f"f{n:05d}.jpg").write_bytes(data)
                last = data
                n += 1
            time.sleep(a.interval)
    print(f"OK: {n} frames in {out}")


# ------------------------------------------------------------------ helpers
def iou(a, b):
    ix1, iy1, ix2, iy2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def match(preds, gts, thr=0.5):
    """Greedy one-to-one matching by confidence, same category. -> (tp, fp, fn, matched gt indices)"""
    used, tp = set(), 0
    for p in sorted(preds, key=lambda d: -d["conf"]):
        best, bi = thr, None
        for i, g in enumerate(gts):
            if i in used or g["cat"] != p["cat"]:
                continue
            v = iou(p["box"], g["box"])
            if v >= best:
                best, bi = v, i
        if bi is not None:
            used.add(bi)
            tp += 1
    return tp, len(preds) - tp, len(gts) - len(used), used


def load_labels(d, name, w, h):
    f = Path(d) / (Path(name).stem + ".txt")
    if not f.is_file():
        return None
    gts = []
    for line in f.read_text().split("\n"):
        p = line.split()
        if len(p) < 5 or int(float(p[0])) not in VEHICLE_COCO:
            continue
        c, cx, cy, bw, bh = int(float(p[0])), *map(float, p[1:5])
        gts.append({"cat": VEHICLE_COCO[c], "box": ((cx - bw / 2) * w, (cy - bh / 2) * h, (cx + bw / 2) * w, (cy + bh / 2) * h)})
    return gts


def pct(v, q):
    v = sorted(v)
    return v[min(len(v) - 1, int(round(q / 100.0 * (len(v) - 1))))] if v else None


# ------------------------------------------------------------------ run
def run(a):
    import cv2
    import torch
    from ultralytics import YOLO
    from backend import config
    from backend.detection.pipeline import fp16_kwargs

    frames = sorted(p for p in Path(a.frames).iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    if not frames:
        sys.exit(f"ERROR: no images in {a.frames}")
    imgs = [cv2.imread(str(p)) for p in frames[: a.max_frames]]
    names = [p.name for p in frames[: a.max_frames]]
    cuda = torch.cuda.is_available() and a.device != "cpu"
    device = 0 if cuda else "cpu"
    gpu_name = torch.cuda.get_device_name(0) if cuda else None
    sizes = [int(x) for x in a.imgsz.split(",")]
    precs = ["fp32", "fp16"] if cuda and not a.fp32_only else ["fp32"]
    ious = [float(x) for x in a.iou.split(",")]
    confs = [float(x) for x in a.conf_grid.split(",")]
    model_path = a.model or str(config.MODEL_PATH)
    h0, w0 = imgs[0].shape[:2]
    print(f"{len(imgs)} frames {w0}x{h0}  device={'cuda:0 ' + gpu_name if cuda else 'CPU'}  model={Path(model_path).name}")
    if cuda:
        try:
            import subprocess
            q = subprocess.run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader"],
                               capture_output=True, text=True, timeout=10).stdout.strip()
            others = [ln for ln in q.split("\n") if ln and not ln.startswith(str(os.getpid()))]
            if others:
                print("WARNING: other processes use the GPU (timings are shared):", "; ".join(others))
        except Exception:
            pass

    labels = None
    if a.labels:
        labels = {n: load_labels(a.labels, n, im.shape[1], im.shape[0]) for n, im in zip(names, imgs)}
        labels = {k: v for k, v in labels.items() if v is not None}
        print(f"labels: {len(labels)} labelled frames ({sum(len(v) for v in labels.values())} vehicles)")
        if not labels:
            labels = None

    results, dets_by = [], {}
    for sz in sizes:
        for prec in precs:
            model = YOLO(model_path, task="detect")
            kw = dict(imgsz=sz, device=device, classes=sorted(VEHICLE_COCO), verbose=False)
            if prec == "fp16":
                kw.update(fp16_kwargs())
            for _ in range(a.warmup):
                model.predict(imgs[0], conf=0.25, **kw)
            if cuda:
                torch.cuda.reset_peak_memory_stats()
                torch.cuda.synchronize()
            ms = []
            for im in imgs:                          # timing pass: the backend's settings (default NMS)
                t0 = time.perf_counter()
                model.predict(im, conf=0.25, **kw)
                if cuda:
                    torch.cuda.synchronize()
                ms.append((time.perf_counter() - t0) * 1000.0)
            peak = round(torch.cuda.max_memory_allocated() / 1048576) if cuda else None
            for nms in ious:                         # detection pass: low conf, thresholds applied afterwards
                per = {}
                for n, im in zip(names, imgs):
                    r = model.predict(im, conf=min(confs + [0.05]), iou=nms, **kw)[0]
                    b = r.boxes
                    per[n] = [{"cat": VEHICLE_COCO[int(c)], "conf": float(s), "box": tuple(map(float, xy))}
                              for xy, c, s in zip(b.xyxy.tolist(), b.cls.tolist(), b.conf.tolist())]
                dets_by[(sz, prec, nms)] = per
            row = {"imgsz": sz, "precision": prec, "msP50": round(statistics.median(ms), 1), "msP95": round(pct(ms, 95), 1),
                   "fps": round(1000.0 / statistics.mean(ms), 1), "gpuPeakMb": peak}
            results.append(row)
            print(f"  imgsz {sz:5d} {prec}: p50 {row['msP50']:6.1f} ms  p95 {row['msP95']:6.1f} ms  {row['fps']:6.1f} fps"
                  + (f"  peak {peak} MB" if peak is not None else ""))
            del model
            if cuda:
                torch.cuda.empty_cache()

    # ---- detections at the CURRENT class thresholds (per setting)
    def at_thr(dets, scale=0.0, conf=None):
        return [d for d in dets if d["conf"] >= (conf if conf is not None else CLASS_THR[d["cat"]] + scale)]

    for row in results:
        per = dets_by[(row["imgsz"], row["precision"], ious[-1] if 0.7 not in ious else 0.7)]
        kept = [d for n in names for d in at_thr(per[n])]
        row["vehiclesPerFrame"] = round(len(kept) / len(names), 2)
        row["smallPerFrame"] = round(sum(1 for d in kept if (d["box"][2] - d["box"][0]) * (d["box"][3] - d["box"][1]) < SMALL_PX) / len(names), 2)

    # ---- FP16 vs FP32 agreement (same imgsz): boxes matched at IoU >= 0.9, same class
    parity = {}
    if "fp16" in precs:
        nms = 0.7 if 0.7 in ious else ious[-1]
        for sz in sizes:
            tp = tot = 0
            for n in names:
                a32, a16 = at_thr(dets_by[(sz, "fp32", nms)][n]), at_thr(dets_by[(sz, "fp16", nms)][n])
                t, _, _, _ = match(a16, [{"cat": d["cat"], "box": d["box"]} for d in a32], 0.9)
                tp += t
                tot += max(len(a32), len(a16))
            parity[sz] = round(tp / tot, 4) if tot else None

    # ---- accuracy with labels: P / R / F1 over the grid; small-vehicle recall
    grid = []
    if labels:
        for (sz, prec, nms), per in dets_by.items():
            for c in confs + ["class-defaults"]:
                TP = FP = FN = sTP = sN = 0
                for n, gts in labels.items():
                    preds = at_thr(per[n]) if c == "class-defaults" else at_thr(per[n], conf=c)
                    tp, fp, fn, used = match(preds, gts)
                    TP, FP, FN = TP + tp, FP + fp, FN + fn
                    for i, g in enumerate(gts):
                        if (g["box"][2] - g["box"][0]) * (g["box"][3] - g["box"][1]) < SMALL_PX:
                            sN += 1
                            sTP += i in used
                p = TP / (TP + FP) if TP + FP else 0.0
                r = TP / (TP + FN) if TP + FN else 0.0
                grid.append({"imgsz": sz, "precision": prec, "nmsIou": nms, "conf": c, "P": round(p, 3), "R": round(r, 3),
                             "F1": round(2 * p * r / (p + r), 3) if p + r else 0.0, "fpPerFrame": round(FP / len(labels), 3),
                             "smallRecall": round(sTP / sN, 3) if sN else None})
    else:
        # proxy only: reference = detections >= 0.5 of the largest FP32 setting
        ref_key = (max(sizes), "fp32", 0.7 if 0.7 in ious else ious[-1])
        ref = {n: [d for d in dets_by[ref_key][n] if d["conf"] >= 0.5] for n in names}
        for (sz, prec, nms), per in dets_by.items():
            TP = FP = FN = 0
            for n in names:
                tp, fp, fn, _ = match(at_thr(per[n]), ref[n])
                TP, FP, FN = TP + tp, FP + fp, FN + fn
            grid.append({"imgsz": sz, "precision": prec, "nmsIou": nms, "conf": "class-defaults",
                         "agreementRecallProxy": round(TP / (TP + FN), 3) if TP + FN else None,
                         "extraBoxesPerFrame": round(FP / len(names), 3)})

    # ---- recommendation (only from measured numbers)
    rec = None
    fast = [r for r in results if r["fps"] >= a.target_fps]
    if labels and fast:
        ok_keys = {(r["imgsz"], r["precision"]) for r in fast}
        cand = [g for g in grid if (g["imgsz"], g["precision"]) in ok_keys]
        base = next((g for g in grid if g["imgsz"] == 640 and g["precision"] == "fp32" and g["conf"] == "class-defaults"
                     and g["nmsIou"] == (0.7 if 0.7 in ious else ious[-1])), None)
        if base:          # do not trade false positives for recall: at most the current FP rate
            cand = [g for g in cand if g["fpPerFrame"] <= base["fpPerFrame"]] or cand
        fps_of = {(r["imgsz"], r["precision"]): r["fps"] for r in results}
        best = max(cand, key=lambda g: (g["F1"], -g["fpPerFrame"], fps_of[(g["imgsz"], g["precision"])]))   # ties: faster
        rec = {"basis": "labelled frames: best F1 among settings >= target fps with no more false positives per frame than 640/FP32/defaults",
               **best, "baseline": base}
    elif fast:
        par_ok = all(v is not None and v >= 0.98 for v in parity.values()) if parity else False
        r = max(fast, key=lambda r: (r["imgsz"], r["precision"] == "fp16" and par_ok))
        rec = {"basis": "no labels: largest input size that still reaches the target fps; thresholds NOT tuned (needs labels)",
               "imgsz": r["imgsz"], "precision": r["precision"] if (r["precision"] == "fp32" or par_ok) else "fp32"}

    out = Path(a.out or OUT_BASE / time.strftime("vehicle-%Y%m%d-%H%M%S"))
    out.mkdir(parents=True, exist_ok=True)
    data = {"frames": len(names), "resolution": f"{w0}x{h0}", "device": gpu_name or "CPU", "model": Path(model_path).name,
            "speed": results, "fp16Parity": parity, "grid": grid, "labelled": bool(labels), "targetFps": a.target_fps,
            "recommendation": rec}
    (out / "results.json").write_text(json.dumps(data, indent=2))
    md = [f"# Vehicle benchmark - {data['device']}, {len(names)} frames {w0}x{h0}", "",
          "| imgsz | precision | p50 ms | p95 ms | fps | GPU peak MB | vehicles/frame | small/frame |", "|---|---|---|---|---|---|---|---|"]
    md += [f"| {r['imgsz']} | {r['precision']} | {r['msP50']} | {r['msP95']} | {r['fps']} | {r['gpuPeakMb']} | {r['vehiclesPerFrame']} | {r['smallPerFrame']} |" for r in results]
    if parity:
        md += ["", "FP16 vs FP32 (same boxes, IoU>=0.9): " + ", ".join(f"{k}: {v}" for k, v in parity.items())]
    md += ["", "## " + ("Accuracy (your labels)" if labels else "PROXY only (no labels) - not accuracy"), ""]
    keys = list(grid[0].keys()) if grid else []
    if grid:
        md += ["| " + " | ".join(keys) + " |", "|" + "---|" * len(keys)]
        md += ["| " + " | ".join(str(g[k]) for k in keys) + " |" for g in sorted(grid, key=lambda g: -(g.get("F1") or g.get("agreementRecallProxy") or 0))[:25]]
    md += ["", "## Suggested settings", "", json.dumps(rec, indent=2) if rec else f"none reaches {a.target_fps} fps"]
    if rec:
        env = [f"AIV_IMG_SIZE={rec['imgsz']}", f"AIV_HALF={'1' if rec['precision'] == 'fp16' else '0'}"]
        if labels and rec.get("nmsIou") is not None:
            env.append(f"AIV_NMS_IOU={rec['nmsIou']}")
        md += ["", "Start the backend with: `" + " ".join(env) + "`" +
               (f" and set the vehicle confidence to {rec['conf']} in the module settings" if labels and rec.get("conf") not in (None, "class-defaults") else "")]
    (out / "report.md").write_text("\n".join(md) + "\n")
    print("\n".join(md))
    print(f"\nsaved: {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("capture")
    c.add_argument("--camera")
    c.add_argument("--video")
    c.add_argument("--api", default="http://127.0.0.1:8000/api")
    c.add_argument("--count", type=int, default=300)
    c.add_argument("--interval", type=float, default=0.5)
    c.add_argument("--out")
    r = sub.add_parser("run")
    r.add_argument("--frames", required=True)
    r.add_argument("--labels")
    r.add_argument("--model")
    r.add_argument("--imgsz", default="640,960,1280")
    r.add_argument("--iou", default="0.5,0.7")
    r.add_argument("--conf-grid", default="0.25,0.3,0.35,0.4,0.45,0.5")
    r.add_argument("--target-fps", type=float, default=15.0)
    r.add_argument("--warmup", type=int, default=10)
    r.add_argument("--max-frames", type=int, default=500)
    r.add_argument("--device", default="auto")
    r.add_argument("--fp32-only", action="store_true")
    r.add_argument("--out")
    a = ap.parse_args()
    if a.cmd == "capture" and not (a.camera or a.video):
        ap.error("capture needs --camera ID (running session) or --video FILE")
    capture(a) if a.cmd == "capture" else run(a)


if __name__ == "__main__":
    main()
