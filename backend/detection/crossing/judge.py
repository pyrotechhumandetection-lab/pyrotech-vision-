"""Helmet judge for riders of two-wheelers (module twhelmet).

What this can and cannot do (measured on staged photos, see backend/models/twhelmet/README.md):
  * The project's PPE detector (ppe_yolo11n.pt) was trained on CONSTRUCTION helmets. On motorcycle helmets it is
    unreliable: a black full-face helmet scored helmet 0.00, a rider got "no_helmet" 0.55 while wearing one.
  * Therefore the stock judge is deliberately conservative. It answers HELMET only on clear helmet evidence,
    MISSING only on strong "no helmet" evidence in two independent views of the head (body-top crop and a tight
    crop around the head found by the head detector), and UNCERTAIN otherwise. UNCERTAIN never raises a
    compliant / missing alert.
  * "Genuine helmet" (versus a cap, a hood, a hard hat) cannot be established with the stock models. For that,
    put a model trained on riders at backend/models/twhelmet/rider_helmet.pt (or AIV_TWHELMET_MODEL): its classes
    are matched by name (helmet / no_helmet / head ...) and it replaces the stock detector. Nothing is retrained
    or replaced automatically.
Votes of several frames of the same two-wheeler are combined in `decide` (one noisy frame never decides).
"""
import logging
import os
import threading
from collections import deque

import numpy as np

from backend import config
from backend.runtime import gated

log = logging.getLogger("aivision.twhelmet")

HELMET, MISSING, UNCERTAIN = "HELMET", "MISSING", "UNCERTAIN"
MODELS_DIR = config.BACKEND_DIR / "models" / "twhelmet"
CUSTOM = os.environ.get("AIV_TWHELMET_MODEL") or str(MODELS_DIR / "rider_helmet.pt")
POSITIVE = {"helmet", "with_helmet", "with helmet", "wearing_helmet", "helmet_on", "hardhat", "with-helmet"}
NEGATIVE = {"no_helmet", "no helmet", "without_helmet", "without helmet", "nohelmet", "head", "bare_head", "no-helmet"}
HEAD_BAND = (-0.10, 0.45)            # region of the rider box (fractions of its height) that can hold the head

_LOCK = threading.Lock()
_STATE = {"model": None, "names": None, "custom": False, "tried": False, "error": None, "head": None}


def _load():
    with _LOCK:
        if _STATE["tried"]:
            return _STATE["model"]
        _STATE["tried"] = True
        try:
            from ultralytics import YOLO
            from backend.detection.ppe import engine as ppe_engine
            from pathlib import Path
            if Path(CUSTOM).is_file():
                _STATE.update(model=YOLO(CUSTOM, task="detect"), custom=True)
                log.info("two-wheeler helmet judge: custom model %s", CUSTOM)
            elif ppe_engine.DETECTOR.is_file():
                _STATE.update(model=YOLO(str(ppe_engine.DETECTOR), task="detect"), custom=False)
            _STATE["names"] = {int(k): str(v).lower() for k, v in (_STATE["model"].names or {}).items()} if _STATE["model"] else {}
        except Exception as exc:
            _STATE["error"] = repr(exc)
            log.warning("two-wheeler helmet judge unavailable: %s", exc)
        return _STATE["model"]


def status():
    _load()
    return {"model": ("custom: " + os.path.basename(CUSTOM)) if _STATE["custom"] else ("stock PPE detector" if _STATE["model"] else None),
            "custom": _STATE["custom"], "validated": False, "error": _STATE["error"]}


def _head_boxes(crop):
    try:
        from backend.detection import head_count
        det = head_count.get_detector().detect(crop, 0.4, 320)
        return [list(map(float, d[:4])) + [float(d[4])] for d in (det if det is not None else [])]
    except Exception:
        return []


def _classify(model, names, crop, imgsz, custom):
    """(best helmet conf, best no_helmet conf) in one crop. With the rider model the crop is a whole rider: only a head
    in its upper part counts (a helmet in the hand or on the bike's mirror does not)."""
    with _LOCK, gated():
        r = model.predict(crop, imgsz=640 if custom else imgsz, conf=0.25, verbose=False)[0]
    pos = neg = 0.0
    ch = crop.shape[0]
    for b in (r.boxes if r.boxes is not None else []):
        n = names.get(int(b.cls), "")
        c = float(b.conf)
        if custom and (float(b.xyxy[0][1]) + float(b.xyxy[0][3])) / 2 > 0.6 * ch:
            continue
        if n in POSITIVE or (not custom and n == "helmet"):
            pos = max(pos, c)
        elif n in NEGATIVE or (not custom and n == "no_helmet"):
            neg = max(neg, c)
    return pos, neg


PLATE_NAMES = {"number-plate", "number_plate", "numberplate", "license-plate", "license_plate", "licence-plate", "plate"}


def find_plate(frame, bbox, min_conf=0.35):
    """Number plate on one two-wheeler, from the rider model's own number-plate class. Returns
    {"detected": bool, "conf": float|None, "box": [x1,y1,x2,y2]|None} in frame pixels, or None when the loaded model
    has no plate class (stock PPE detector) - so "no plate" is never reported without a model able to see one."""
    model = _load()
    if not model or not _STATE["custom"] or not any(n in PLATE_NAMES for n in (_STATE["names"] or {}).values()):
        return None
    fh, fw = frame.shape[:2]
    x1, y1, x2, y2 = bbox
    bw, bh = x2 - x1, y2 - y1
    cx1, cy1 = max(0, int(x1 - 0.1 * bw)), max(0, int(y1 - 0.1 * bh))
    cx2, cy2 = min(fw, int(x2 + 0.1 * bw)), min(fh, int(y2 + 0.1 * bh))
    if cx2 - cx1 < 24 or cy2 - cy1 < 24:
        return {"detected": False, "conf": None, "box": None}
    crop = frame[cy1:cy2, cx1:cx2]
    with _LOCK, gated():
        r = model.predict(crop, imgsz=640, conf=min_conf, verbose=False)[0]
    best = None
    for b in (r.boxes if r.boxes is not None else []):
        if _STATE["names"].get(int(b.cls), "") in PLATE_NAMES and (best is None or float(b.conf) > best[1]):
            best = (b.xyxy[0].tolist(), float(b.conf))
    if best is None:
        return {"detected": False, "conf": None, "box": None}
    bx = best[0]
    return {"detected": True, "conf": round(best[1], 3), "box": [round(cx1 + bx[0], 1), round(cy1 + bx[1], 1), round(cx1 + bx[2], 1), round(cy1 + bx[3], 1)]}


def view_verdict(pos, neg, hmin, nmin):
    if pos >= hmin and pos > neg:
        return HELMET
    if neg >= nmin and pos < 0.25:
        return MISSING
    return UNCERTAIN


def _custom_expected():
    from pathlib import Path
    return _STATE["custom"] if _STATE["tried"] else Path(CUSTOM).is_file()


def head_crop(frame, pbox, mx=0.5):
    """Copy of the part of the frame to judge, or None when too small. Stock PPE detector: only the band that can hold the
    head (top of the person box). Rider helmet model (trained on whole riders on their bike): the whole rider with a small
    margin, which is the scale it was trained at. Cut out at once so a queued job does not keep the whole frame alive."""
    fh, fw = frame.shape[:2]
    x1, y1, x2, y2 = pbox
    bw, bh = x2 - x1, y2 - y1
    if _custom_expected():
        cx1, cx2 = max(0, int(x1 - mx * bw)), min(fw, int(x2 + mx * bw))      # side margin = scene context the model was trained with
        cy1, cy2 = max(0, int(y1 - 0.10 * bh)), min(fh, int(y2 + 0.05 * bh))
        if cx2 - cx1 < 24 or cy2 - cy1 < 24:
            return None
        return frame[cy1:cy2, cx1:cx2].copy()
    cx1, cx2 = max(0, int(x1 - 0.15 * bw)), min(fw, int(x2 + 0.15 * bw))
    cy1, cy2 = max(0, int(y1 + HEAD_BAND[0] * bh)), min(fh, int(y1 + HEAD_BAND[1] * bh))
    if cx2 - cx1 < 12 or cy2 - cy1 < 12:
        return None
    return frame[cy1:cy2, cx1:cx2].copy()


def bike_rider_box(vbox, fh, fw, up=0.7):
    """Stand-in rider box when no person was linked to a two-wheeler: the bike box stretched upward over where the
    rider's head and body are. Only used with the rider model, which was trained on a whole rider on a bike and finds
    the head itself - so a rider the person detector missed (small, seen from behind) is still judged."""
    x1, y1, x2, y2 = vbox
    return [max(0.0, x1), max(0.0, y1 - up * (y2 - y1)), min(float(fw), x2), min(float(fh), y2)]


def judge_rider(frame, pbox, hmin=0.5, nmin=0.6, classify=None, heads=None):
    """One frame, one rider. `classify` / `heads` can be replaced (tests, custom pipelines)."""
    top = head_crop(frame, pbox)
    if top is None:
        return {"verdict": UNCERTAIN, "helmetConf": 0.0, "noHelmetConf": 0.0, "views": 0, "reason": "rider too small to judge", "source": None}
    return judge_crop(top, hmin, nmin, classify, heads)


def judge_crop(top, hmin=0.5, nmin=0.6, classify=None, heads=None):
    """Judge the head region of one rider (see head_crop)."""
    model = _load() if classify is None else True
    if not model:
        return {"verdict": UNCERTAIN, "helmetConf": 0.0, "noHelmetConf": 0.0, "views": 0, "reason": "no helmet model available", "source": None}
    custom = _STATE["custom"]
    classify = classify or (lambda crop: _classify(_STATE["model"], _STATE["names"], crop, 416, custom))
    heads = heads or _head_boxes
    cx1 = cy1 = 0
    views = [("body-top",) + tuple(classify(top))]
    head_box = None
    hs = [] if custom else heads(top)        # the rider model judges the whole rider in one pass; the stock model needs a second, tight view
    if hs:
        hx1, hy1, hx2, hy2, _c = max(hs, key=lambda h: h[4] - 0.001 * h[1])
        pw, ph = 0.35 * (hx2 - hx1), 0.35 * (hy2 - hy1)
        ax1, ay1 = max(0, int(hx1 - pw)), max(0, int(hy1 - ph))
        ax2, ay2 = min(top.shape[1], int(hx2 + pw)), min(top.shape[0], int(hy2 + ph))
        if ax2 - ax1 >= 10 and ay2 - ay1 >= 10:
            head_box = [cx1 + hx1, cy1 + hy1, cx1 + hx2, cy1 + hy2]
            views.append(("head",) + tuple(classify(top[ay1:ay2, ax1:ax2])))
    kinds = [view_verdict(p, n, hmin, nmin) for _name, p, n in views]
    if HELMET in kinds and MISSING not in kinds:
        verdict, reason = HELMET, "helmet evidence"
    elif custom and kinds.count(MISSING) >= 1 and HELMET not in kinds:
        verdict, reason = MISSING, "custom model: no helmet"
    elif kinds.count(MISSING) >= 2:
        verdict, reason = MISSING, "no-helmet evidence in both views of the head"
    else:
        verdict, reason = UNCERTAIN, ("conflicting views" if HELMET in kinds and MISSING in kinds else "weak or single-view evidence")
    return {"verdict": verdict, "helmetConf": round(max(p for _n, p, _m in views), 3), "noHelmetConf": round(max(m for _n, _p, m in views), 3),
            "views": len(views), "reason": reason, "source": "custom" if custom else "stock", "headBox": head_box}


def decide(votes, min_missing=3, min_helmet=2):
    """Combine per-frame verdicts of one rider. A single MISSING frame never decides; any confident HELMET frame
    keeps the rider from being called MISSING."""
    h, m = votes.count(HELMET), votes.count(MISSING)
    if h >= min_helmet and h > m:
        return HELMET
    if m >= min_missing and h == 0:
        return MISSING
    return UNCERTAIN


class RiderHistory:
    """Rolling per-frame verdicts of one rider (keyed by the rider's position on its two-wheeler, not by person id,
    so a person id switch does not lose the evidence)."""

    def __init__(self, n=8):
        self.votes = deque(maxlen=n)
        self.last = None

    def add(self, ev):
        self.votes.append(ev["verdict"])
        self.last = ev

    def verdict(self, **kw):
        return decide(list(self.votes), **kw)
