"""Human Head Count: count every visible human head inside a detection area of ONE still frame.

Model
    models/head_yolov8m/  - a dedicated single-class ("head") YOLOv8m detector trained on a crowd
    head dataset at 1280 px (see models/README.md for its source). Heads are detected from the
    front, side and back, looking up/down and partly occluded; it is NOT the COCO person model.
    The files are stored in parts (< 30 MB each) and joined on first use:
        head_yolov8m.pt.part*            -> head_yolov8m.pt               (PyTorch)
        head_yolov8m_openvino_model/*.bin.part* -> head_yolov8m.bin    (OpenVINO FP16, ~6x faster on CPU)

Pipeline for one captured frame
    1. crop the frame to the bounding box of the detection area(s) (+ a small margin), so the
       model sees the area at the highest possible resolution;
    2. detect heads in the crop at HEAD_IMG_SIZE (1280); very large crops are additionally
       processed as 2x2 overlapping tiles (tiling "auto") so small/distant heads keep enough pixels;
    3. merge with class-agnostic NMS + containment suppression -> one box per head;
    4. keep only heads whose centre lies inside a detection-area polygon;
    5. drop boxes below the configured minimum head size (no guessing on unreadable blobs).
Nothing is interpolated or invented: a head is counted only if the model detected it.
"""
import hashlib
import logging
import threading
import time
from pathlib import Path

import cv2
import numpy as np

from backend import config

log = logging.getLogger("aivision.headcount")

DEFAULT_SETTINGS = {
    "confidence": 0.35,     # minimum head confidence
    "iou": 0.45,            # NMS IoU: boxes overlapping more than this are the same head
    "imgSize": 1280,        # model input size (the model was trained at 1280)
    "tiling": "auto",       # auto | on | off  (auto: tiles only for crops much larger than imgSize)
    "minHeadPx": 7,         # smaller boxes are ignored (too small to tell a head from a cone, sign, etc.)
}
LIMITS = {"confidence": (0.05, 0.95), "iou": (0.1, 0.9), "imgSize": (320, 1920), "minHeadPx": (2, 200)}

MODEL_DIR = config.BACKEND_DIR / "models" / "head_yolov8m"
PT_FILE = MODEL_DIR / "head_yolov8m.pt"
OV_DIR = MODEL_DIR / "head_yolov8m_openvino_model"
OV_BIN = OV_DIR / "head_yolov8m.bin"
SHA256 = {
    PT_FILE.name: "ddcbf60f7639a6d1b8bba19ab5c4e31d0196a1fc195ee29a5e2a184453197d50",
    OV_BIN.name: "7c2af28b068d83640d243c13fb0511cb864cca938d5c49a0a7e89e1c9b3d5b3e",
}
MODEL_NAME = "head_yolov8m (head detector)"

_assemble_lock = threading.Lock()


class HeadModelError(RuntimeError):
    pass


def validate_settings(s):
    """Return (clean settings, [errors])."""
    out, errs = dict(DEFAULT_SETTINGS), []
    for k, v in (s or {}).items():
        if k not in DEFAULT_SETTINGS:
            continue
        if k == "tiling":
            if v not in ("auto", "on", "off"):
                errs.append("tiling must be auto, on or off")
            else:
                out[k] = v
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            errs.append(f"{k} must be a number")
            continue
        lo, hi = LIMITS[k]
        if not lo <= v <= hi:
            errs.append(f"{k} must be between {lo} and {hi}")
            continue
        out[k] = int(round(v)) if k in ("imgSize", "minHeadPx") else round(v, 3)
    if out["imgSize"] % 32:
        out["imgSize"] = int(round(out["imgSize"] / 32.0) * 32)
    return out, errs


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def assemble(target):
    """Join target.part0, part1, ... into target (once) and verify its SHA-256."""
    target = Path(target)
    with _assemble_lock:
        if target.is_file():
            return target
        parts = sorted(target.parent.glob(target.name + ".part*"), key=lambda p: int(p.suffix[5:] or 0))
        if not parts:
            raise HeadModelError(f"head model file missing: {target.name} (and no {target.name}.part* files)")
        tmp = target.with_name(target.name + ".tmp")
        with open(tmp, "wb") as out:
            for p in parts:
                out.write(p.read_bytes())
        want = SHA256.get(target.name)
        if want and _sha256(tmp) != want:
            tmp.unlink(missing_ok=True)
            raise HeadModelError(f"{target.name}: checksum mismatch after joining {len(parts)} parts - the model files are incomplete")
        tmp.replace(target)
        log.info("head model %s assembled from %d parts", target.name, len(parts))
        return target


def resolve_engine():
    """('openvino' | 'pytorch', path). AIV_HEAD_ENGINE = auto | openvino | pytorch."""
    want = config.HEAD_ENGINE
    if config.DEVICE not in (None, "cpu") and want != "openvino":
        return "pytorch", PT_FILE
    if want in ("auto", "openvino") and (OV_DIR / "head_yolov8m.xml").is_file() and config.openvino_available():
        return "openvino", OV_DIR
    return "pytorch", PT_FILE


class HeadDetector:
    """Loaded once, shared by all cameras. Thread-safe (one inference at a time)."""

    def __init__(self):
        from ultralytics import YOLO
        engine, path = resolve_engine()
        try:
            if engine == "openvino":
                assemble(OV_BIN)
            else:
                assemble(PT_FILE)
            self.model = YOLO(str(path), task="detect")
        except Exception as e:
            if engine != "openvino":
                raise
            log.warning("OpenVINO head model unavailable (%s) - using PyTorch", e)
            engine, path = "pytorch", assemble(PT_FILE)
            self.model = YOLO(str(path), task="detect")
        self.engine = engine
        self.lock = threading.Lock()
        log.info("head detector loaded (%s)", engine)

    def detect(self, img, conf, imgsz):
        kw = dict(imgsz=imgsz, conf=conf, iou=0.6, verbose=False, max_det=3000)
        if self.engine == "pytorch" and config.DEVICE is not None:
            kw["device"] = config.DEVICE
        with self.lock:
            r = self.model.predict(img, **kw)[0]
        if r.boxes is None or not len(r.boxes):
            return np.zeros((0, 5), np.float32)
        return np.concatenate([r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy()[:, None]], 1).astype(np.float32)


_detector = None
_detector_lock = threading.Lock()


def get_detector():
    global _detector
    with _detector_lock:
        if _detector is None:
            _detector = HeadDetector()
        return _detector


# ---------------------------------------------------------------------------- geometry
def zone_polygon_px(zone, w, h):
    if zone.get("type") == "rectangle":
        x, y, zw, zh = (float(zone[k]) for k in ("x", "y", "width", "height"))
        pts = [(x, y), (x + zw, y), (x + zw, y + zh), (x, y + zh)]
    else:
        pts = [(float(p["x"]), float(p["y"])) for p in zone.get("points", [])]
    if len(pts) < 3:
        return None
    return np.array([(min(max(px, 0.0), 1.0) * w, min(max(py, 0.0), 1.0) * h) for px, py in pts], np.float32)


def nms_merge(dets, iou_thr):
    """Class-agnostic NMS, then drop a box mostly (>= 70 %) inside a higher-scoring box
    (a partial head from a tile edge or a double detection of the same head)."""
    if not len(dets):
        return dets
    order = np.argsort(-dets[:, 4])
    dets = dets[order]
    x1, y1, x2, y2 = dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3]
    area = np.maximum(x2 - x1, 0) * np.maximum(y2 - y1, 0)
    keep, alive = [], np.ones(len(dets), bool)
    for i in range(len(dets)):
        if not alive[i]:
            continue
        keep.append(i)
        ix1, iy1 = np.maximum(x1[i], x1), np.maximum(y1[i], y1)
        ix2, iy2 = np.minimum(x2[i], x2), np.minimum(y2[i], y2)
        inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
        iou = inter / np.maximum(area[i] + area - inter, 1e-6)
        inside = inter / np.maximum(area, 1e-6)
        dup = (iou > iou_thr) | (inside >= 0.7)
        dup[: i + 1] = False
        alive &= ~dup
    return dets[keep]


def _tiles(w, h, size):
    """2x2 (or more) overlapping tiles covering a w x h crop, each about `size` px on its long side."""
    nx = max(2, int(np.ceil(w / (size * 0.8))))
    ny = max(2, int(np.ceil(h / (size * 0.8))))
    tw, th = int(np.ceil(w / nx * 1.25)), int(np.ceil(h / ny * 1.25))
    xs = np.linspace(0, max(w - tw, 0), nx).astype(int)
    ys = np.linspace(0, max(h - th, 0), ny).astype(int)
    return [(int(x), int(y), min(tw, w), min(th, h)) for y in ys for x in xs]


def count_heads(frame, zones, settings, detector=None):
    """Detect heads of `frame` inside the union of `zones` (normalised camera zones; [] = whole frame).

    Returns dict(heads=[{id, box:[x1,y1,x2,y2], confidence}], polygons=[np.array], timings, crop)."""
    s, _ = validate_settings(settings)
    det = detector or get_detector()
    h, w = frame.shape[:2]
    polys = [p for p in (zone_polygon_px(z, w, h) for z in zones or []) if p is not None]
    if not polys:
        polys = [np.array([(0, 0), (w, 0), (w, h), (0, h)], np.float32)]
    allp = np.concatenate(polys)
    pad = max(16, int(0.03 * max(w, h)))
    x0, y0 = max(int(allp[:, 0].min()) - pad, 0), max(int(allp[:, 1].min()) - pad, 0)
    x1, y1 = min(int(np.ceil(allp[:, 0].max())) + pad, w), min(int(np.ceil(allp[:, 1].max())) + pad, h)
    crop = frame[y0:y1, x0:x1]
    cw, ch = crop.shape[1], crop.shape[0]
    t0 = time.perf_counter()
    found = [det.detect(crop, s["confidence"], s["imgSize"])]
    passes = 1
    use_tiles = s["tiling"] == "on" or (s["tiling"] == "auto" and max(cw, ch) > 1.6 * s["imgSize"])
    if use_tiles:
        for tx, ty, tw, th in _tiles(cw, ch, s["imgSize"]):
            d = det.detect(crop[ty:ty + th, tx:tx + tw], s["confidence"], s["imgSize"])
            passes += 1
            if not len(d):
                continue
            d[:, [0, 2]] += tx
            d[:, [1, 3]] += ty
            # a head cut by an inner tile border is seen whole by the neighbouring tile / full pass
            m = np.ones(len(d), bool)
            if tx > 0:
                m &= d[:, 0] > tx + 2
            if ty > 0:
                m &= d[:, 1] > ty + 2
            if tx + tw < cw:
                m &= d[:, 2] < tx + tw - 2
            if ty + th < ch:
                m &= d[:, 3] < ty + th - 2
            found.append(d[m])
    infer_ms = (time.perf_counter() - t0) * 1000.0
    dets = np.concatenate(found) if found else np.zeros((0, 5), np.float32)
    dets[:, [0, 2]] += x0
    dets[:, [1, 3]] += y0
    dets = nms_merge(dets, s["iou"])
    heads = []
    for x_1, y_1, x_2, y_2, c in dets:
        bw, bh = x_2 - x_1, y_2 - y_1
        if min(bw, bh) < s["minHeadPx"]:
            continue
        centre = (float((x_1 + x_2) / 2), float((y_1 + y_2) / 2))
        if not any(cv2.pointPolygonTest(p, centre, False) >= 0 for p in polys):
            continue
        heads.append({"box": [int(round(x_1)), int(round(y_1)), int(round(x_2)), int(round(y_2))], "confidence": round(float(c), 3)})
    # sequential numbers in reading order: rows of about one head height, left to right
    if heads:
        row = max(8.0, float(np.median([b["box"][3] - b["box"][1] for b in heads])))
        heads.sort(key=lambda b: (int(((b["box"][1] + b["box"][3]) / 2) // row), b["box"][0]))
    for i, b in enumerate(heads, 1):
        b["id"] = i
    return {"heads": heads, "polygons": polys, "inferenceMs": round(infer_ms, 1), "passes": passes,
            "crop": [x0, y0, x1, y1], "settings": s, "engine": det.engine}


# ---------------------------------------------------------------------------- drawing
HEAD_COLOR = (0, 200, 255)      # amber (BGR)
TEXT_BG = (24, 24, 24)


def _bgr(hex_color, fb=(154, 217, 19)):
    try:
        c = hex_color.lstrip("#")
        return (int(c[4:6], 16), int(c[2:4], 16), int(c[0:2], 16))
    except Exception:
        return fb


def annotate(frame, result, zones, info):
    """Annotated copy: dimmed outside the area, area boundary, numbered head boxes and an info panel
    (camera IP, detection type, total heads, capture timestamp)."""
    img = frame.copy()
    h, w = img.shape[:2]
    polys = result["polygons"]
    mask = np.zeros((h, w), np.uint8)
    for p in polys:
        cv2.fillPoly(mask, [p.astype(np.int32)], 255)
    img[mask == 0] = (img[mask == 0] * 0.45).astype(np.uint8)
    sc = max(0.5, w / 1920.0)
    th = max(1, int(round(2 * sc)))
    colors = [_bgr(z.get("color")) for z in zones or []] or [(154, 217, 19)]
    for i, p in enumerate(polys):
        cv2.polylines(img, [p.astype(np.int32)], True, colors[i % len(colors)], th + 1, cv2.LINE_AA)
    font = cv2.FONT_HERSHEY_SIMPLEX
    for b in result["heads"]:
        x1, y1, x2, y2 = b["box"]
        cv2.rectangle(img, (x1, y1), (x2, y2), HEAD_COLOR, th, cv2.LINE_AA)
        label = str(b["id"])
        fs = 0.5 * sc
        (tw, tht), _ = cv2.getTextSize(label, font, fs, th)
        ly = y1 - 3 if y1 - tht - 6 >= 0 else y2 + tht + 6
        cv2.rectangle(img, (x1, ly - tht - 4), (x1 + tw + 6, ly + 2), HEAD_COLOR, -1)
        cv2.putText(img, label, (x1 + 3, ly - 1), font, fs, (0, 0, 0), th, cv2.LINE_AA)
    n = len(result["heads"])
    lines = [f"Camera IP: {info.get('cameraIp') or '-'}", "Detection Type: Human Head Count",
             f"Total Heads Detected: {n}", f"Captured At: {info.get('capturedAt')}"]
    if n == 0:
        lines.append("No heads detected in the selected area")
    fs = 0.62 * sc
    lh = int(30 * sc)
    pw = max(cv2.getTextSize(t, font, fs, th)[0][0] for t in lines) + int(24 * sc)
    ph = lh * len(lines) + int(14 * sc)
    ov = img.copy()
    cv2.rectangle(ov, (0, 0), (pw, ph), TEXT_BG, -1)
    cv2.addWeighted(ov, 0.72, img, 0.28, 0, img)
    for i, t in enumerate(lines):
        color = (0, 220, 255) if i == 2 else (255, 255, 255)
        cv2.putText(img, t, (int(12 * sc), int(12 * sc) + lh * (i + 1) - int(8 * sc)), font, fs,
                    color, th + (1 if i == 2 else 0), cv2.LINE_AA)
    return img
