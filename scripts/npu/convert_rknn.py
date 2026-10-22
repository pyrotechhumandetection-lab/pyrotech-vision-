#!/usr/bin/env python3
"""Convert the shipped yolov8n weights to RKNN (Rockchip NPU) and check the result in the RKNN simulator.

NOT needed on the AI box: the converted models are in the repository (backend/models/yolov8n_rknn_model = FP16,
backend/models/yolov8n_int8_rknn_model = INT8). Use this only to rebuild them, e.g. INT8 calibrated on your own
camera images. Same weights, same 640x640 input, same classes - nothing is retrained.

Two steps (they can run in different Python environments):

  1. onnx   - any Python with ultralytics + torch (e.g. the backend venv):
                 python scripts/npu/convert_rknn.py onnx
              -> build/rknn/yolov8n.onnx  (opset 19, 1x3x640x640, output 1x84x8400)
  2. build  - x86-64 Linux, Python 3.8-3.12 with rknn-toolkit2 2.3.2 (no wheel for 3.13):
                 python3.12 -m venv rk && . rk/bin/activate
                 pip install rknn-toolkit2==2.3.2 "onnx>=1.16.1,<1.19.0" "setuptools<82" onnxruntime opencv-python-headless
                 python scripts/npu/convert_rknn.py build [--int8 --calib DIR] [--images DIR]
              -> backend/models/yolov8n_rknn_model/ (FP16)  or  yolov8n_int8_rknn_model/ (INT8)
              then runs the simulator check: RKNN simulator vs the FP32 ONNX model (= the PyTorch weights) on
              test images - person / vehicle boxes matched at IoU >= 0.5, confidence difference.

INT8: box coordinates are divided by the input size inside the model (as Ultralytics' own RKNN INT8 export does),
so one per-tensor INT8 scale keeps the class scores precise; Ultralytics' RKNN backend multiplies them back
(metadata.yaml args.quantize = 8). Calibrate on 50-300 frames of YOUR cameras (day and night).
Toolkit 2.3.2 matches the runtime librknnrt 2.3.2 installed by ./aivision.sh npu setup.
"""
import argparse
import ast
import json
import sys
from pathlib import Path

import numpy as np

REPO = Path(__file__).resolve().parents[2]
MODELS = REPO / "backend" / "models"
WORK = REPO / "build" / "rknn"
CLASSES = [0, 1, 2, 3, 5, 7]          # person, bicycle, car, motorcycle, bus, truck


def letterbox(im, size=640):
    import cv2
    h, w = im.shape[:2]
    r = min(size / h, size / w)
    nh, nw = round(h * r), round(w * r)
    out = np.full((size, size, 3), 114, np.uint8)
    top, left = (size - nh) // 2, (size - nw) // 2
    out[top:top + nh, left:left + nw] = cv2.resize(im, (nw, nh), interpolation=cv2.INTER_LINEAR)
    return out


def step_onnx(args):
    from ultralytics import YOLO
    WORK.mkdir(parents=True, exist_ok=True)
    import shutil
    w = WORK / "yolov8n.pt"
    shutil.copy(args.weights, w)
    f = Path(YOLO(str(w)).export(format="onnx", imgsz=args.imgsz, opset=19, simplify=True, dynamic=False, batch=1))
    print("ONNX:", f)


def normalized_onnx(src, dst, imgsz):
    """Copy of the ONNX model whose output box channels (0-3) are divided by the input size."""
    import onnx
    from onnx import helper, numpy_helper
    m = onnx.load(str(src))
    g = m.graph
    out = g.output[0]
    name = out.name
    raw = name + "_raw"
    for n in g.node:
        n.output[:] = [raw if o == name else o for o in n.output]
    c = lambda nm, arr: g.initializer.append(numpy_helper.from_array(np.asarray(arr), nm))   # noqa: E731
    c("nc_s0", np.array([0], np.int64)); c("nc_s4", np.array([4], np.int64)); c("nc_end", np.array([1 << 30], np.int64))
    c("nc_ax", np.array([1], np.int64)); c("nc_div", np.full((1, 4, 1), float(imgsz), np.float32))
    g.node.extend([
        helper.make_node("Slice", [raw, "nc_s0", "nc_s4", "nc_ax"], ["nc_box"]),
        helper.make_node("Slice", [raw, "nc_s4", "nc_end", "nc_ax"], ["nc_cls"]),
        helper.make_node("Div", ["nc_box", "nc_div"], ["nc_boxn"]),
        helper.make_node("Concat", ["nc_boxn", "nc_cls"], [name], axis=1),
    ])
    onnx.checker.check_model(m)
    onnx.save(m, str(dst))


def nms(pred, conf=0.25, iou=0.7, classes=CLASSES, max_det=300):
    """pred: (84, N) of one image (cx cy w h + class scores) -> (n, 6) x1 y1 x2 y2 conf cls, class-aware NMS."""
    p = pred.T
    cls = p[:, 4:].argmax(1)
    sc = p[np.arange(len(p)), 4 + cls]
    k = (sc > conf) & np.isin(cls, classes)
    p, cls, sc = p[k], cls[k], sc[k]
    if not len(p):
        return np.zeros((0, 6), np.float32)
    b = np.stack([p[:, 0] - p[:, 2] / 2, p[:, 1] - p[:, 3] / 2, p[:, 0] + p[:, 2] / 2, p[:, 1] + p[:, 3] / 2], 1)
    off = b + cls[:, None] * 7680.0                    # class-aware, as Ultralytics
    order = np.argsort(-sc)
    keep = []
    while len(order) and len(keep) < max_det:
        i = order[0]
        keep.append(i)
        o = order[1:]
        xx1 = np.maximum(off[i, 0], off[o, 0]); yy1 = np.maximum(off[i, 1], off[o, 1])
        xx2 = np.minimum(off[i, 2], off[o, 2]); yy2 = np.minimum(off[i, 3], off[o, 3])
        inter = np.clip(xx2 - xx1, 0, None) * np.clip(yy2 - yy1, 0, None)
        a = (off[i, 2] - off[i, 0]) * (off[i, 3] - off[i, 1])
        ao = (off[o, 2] - off[o, 0]) * (off[o, 3] - off[o, 1])
        order = o[inter / (a + ao - inter + 1e-9) <= iou]
    return np.concatenate([b[keep], sc[keep, None], cls[keep, None]], 1).astype(np.float32)


def box_iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix
    return ix / u if u > 0 else 0.0


def compare(ref, det):
    used, pairs, miss = set(), [], []
    for i in np.argsort(-ref[:, 4]) if len(ref) else []:
        best, bj = 0.5, None
        for j in range(len(det)):
            if j not in used and int(det[j, 5]) == int(ref[i, 5]):
                v = box_iou(ref[i], det[j])
                if v >= best:
                    best, bj = v, j
        if bj is None:
            miss.append(i)
        else:
            used.add(bj)
            pairs.append((i, bj, best))
    return pairs, miss, len(det) - len(used)


def step_build(args):
    import cv2
    import yaml
    from rknn.api import RKNN
    onnx_f = Path(args.onnx or WORK / "yolov8n.onnx")
    if not onnx_f.is_file():
        sys.exit(f"{onnx_f} not found - run the 'onnx' step first")
    import onnx
    meta = {p.key: p.value for p in onnx.load(str(onnx_f), load_external_data=False).metadata_props}
    src = onnx_f
    WORK.mkdir(parents=True, exist_ok=True)
    if args.int8:
        src = WORK / "yolov8n_norm.onnx"
        normalized_onnx(onnx_f, src, args.imgsz)

    rk = RKNN(verbose=False)
    rk.config(mean_values=[[0, 0, 0]], std_values=[[255, 255, 255]], target_platform=args.target)
    assert rk.load_onnx(model=str(src)) == 0, "load_onnx failed"
    ds = None
    if args.int8:
        if not args.calib:
            sys.exit("--int8 needs --calib DIR (your own camera images)")
        files = sorted(p for p in Path(args.calib).rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png"))[:300]
        if len(files) < 10:
            sys.exit(f"only {len(files)} calibration images in {args.calib}")
        cal = WORK / "calib"
        cal.mkdir(exist_ok=True)
        lines = []
        for i, p in enumerate(files):
            im = cv2.imread(str(p))
            if im is not None:
                q = cal / f"{i:04d}.png"
                cv2.imwrite(str(q), letterbox(im, args.imgsz))     # the toolkit reads it as RGB
                lines.append(str(q))
        ds = WORK / "dataset.txt"
        ds.write_text("\n".join(lines) + "\n")
        print(f"INT8 calibration on {len(lines)} images")
    assert rk.build(do_quantization=bool(args.int8), dataset=str(ds) if ds else None) == 0, "build failed"

    out = MODELS / ("yolov8n_int8_rknn_model" if args.int8 else "yolov8n_rknn_model")
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"yolov8n-{args.target}.rknn"
    assert rk.export_rknn(str(f)) == 0, "export failed"
    metadata = {"description": "Ultralytics YOLOv8n model trained on COCO - RKNN export for PYROTECH VISION "
                               "(scripts/npu/convert_rknn.py)",
                "author": "Ultralytics", "license": "AGPL-3.0 License (https://ultralytics.com/license)",
                "version": meta.get("version"), "stride": int(meta.get("stride", 32)), "task": "detect", "batch": 1,
                "imgsz": [args.imgsz, args.imgsz], "names": ast.literal_eval(meta.get("names", "{}")),
                "args": {"batch": 1, "quantize": 8 if args.int8 else 16, "nms": False, "name": args.target},
                "channels": 3, "end2end": False, "rknnToolkit": "2.3.2"}
    (out / "metadata.yaml").write_text(yaml.safe_dump(metadata, sort_keys=False))
    print(f"RKNN: {f}  {f.stat().st_size / 2**20:.1f} MB")

    # ---- simulator check: RKNN simulator vs FP32 ONNX (the same weights) on the test images
    import onnxruntime as ort
    assert rk.init_runtime(target=None) == 0, "simulator init failed"
    sess = ort.InferenceSession(str(onnx_f), providers=["CPUExecutionProvider"])
    if args.images:
        imgs = sorted(p for p in Path(args.images).rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png"))
    else:
        try:
            from ultralytics.utils import ASSETS
            imgs = sorted(Path(ASSETS).glob("*.jpg"))
        except Exception:
            imgs = []
    if not imgs:
        print("simulator check skipped: no test images (--images DIR)")
        rk.release()
        return
    tot = {"ref": 0, "found": 0, "missed": 0, "extra": 0, "strong": 0, "strongMissed": 0}
    cdiff, ious, per = [], [], {}
    for p in imgs:
        im = cv2.imread(str(p))
        if im is None:
            continue
        rgb = letterbox(im, args.imgsz)[:, :, ::-1]
        x = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None], np.float32) / 255.0
        ref = nms(sess.run(None, {sess.get_inputs()[0].name: x})[0][0])
        y = np.asarray(rk.inference(inputs=[np.ascontiguousarray(rgb[None])], data_format="nhwc")[0], np.float32)[0]
        if args.int8:
            y[:4] *= args.imgsz
        det = nms(y)
        pairs, miss, extra = compare(ref, det)
        strong = ref[:, 4] >= 0.5
        tot["ref"] += len(ref); tot["found"] += len(pairs); tot["missed"] += len(miss); tot["extra"] += extra
        tot["strong"] += int(strong.sum()); tot["strongMissed"] += int(sum(strong[i] for i in miss))
        cdiff += [abs(float(ref[i, 4] - det[j, 4])) for i, j, _ in pairs]
        ious += [v for _, _, v in pairs]
        for i in range(len(ref)):
            c = int(ref[i, 5])
            per.setdefault(c, [0, 0])
            per[c][0] += 1
            per[c][1] += 0 if i in miss else 1
    res = {**tot, "images": len(imgs), "confDiffMean": round(float(np.mean(cdiff)), 4) if cdiff else None,
           "confDiffMax": round(float(np.max(cdiff)), 4) if cdiff else None, "iouMean": round(float(np.mean(ious)), 4) if ious else None,
           "perClass": {str(k): v for k, v in per.items()}}
    print("simulator check (RKNN vs FP32 ONNX):", json.dumps(res))
    (out / "simulator_check.json").write_text(json.dumps(res, indent=1) + "\n")
    rk.release()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["onnx", "build"])
    ap.add_argument("--weights", default=str(MODELS / "yolov8n.pt"))
    ap.add_argument("--onnx", help="ONNX file for the build step (default build/rknn/yolov8n.onnx)")
    ap.add_argument("--int8", action="store_true")
    ap.add_argument("--calib", help="folder of your camera images for INT8 calibration")
    ap.add_argument("--images", help="test images for the simulator check")
    ap.add_argument("--target", default="rk3588")
    ap.add_argument("--imgsz", type=int, default=640)
    args = ap.parse_args()
    step_onnx(args) if args.step == "onnx" else step_build(args)


if __name__ == "__main__":
    main()
