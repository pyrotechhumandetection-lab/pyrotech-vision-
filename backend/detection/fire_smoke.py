"""Fire & Smoke Detection module (ported from the standalone fire & smoke detector, main.py).

The detection logic is unchanged:
  * Detector        Ultralytics YOLO fire/smoke model (models/fire_smoke/firedetect-11s.pt) on the CPU,
                    with the camera's green overlay boxes erased first (the model calls them smoke)
  * SmokeVerifier   second opinion on every smoke box - smoke, cloud or dust? (smoke_verifier.npz)
  * SmokeGate       weak smoke (low_conf <= conf < smoke threshold) only counts with a fire nearby,
                    or when it rises/grows for persist_s, or persists for long_persist_s
  * AlertWindow     a class must be seen in M of the last K analysed frames, then one alert per
                    class per cooldown (the original AlertManager rule)

What changed for the shared backend:
  * frames come from the camera's shared session (one RTSP connection for all modules),
  * detection runs in its own worker thread per camera (FireEngine) - like the original
    DetectorWorker it never blocks the video and skips frames while the model is busy,
  * alerts/snapshots go to the backend's event store (Alerts + Snapshots pages) instead of a
    console line + a JPG in ./snapshots,
  * settings are the per-camera module config (UI) instead of config.yaml,
  * the weights are a local file in backend/models/fire_smoke (no Hugging Face download at runtime).
"""
from __future__ import annotations

import hashlib
import logging
import os
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from backend import config

log = logging.getLogger("aivision.fire")

LABELS = ("fire", "smoke")
DISPLAY = {"fire": "Fire", "smoke": "Smoke"}
COLORS = {"fire": (0, 0, 255), "smoke": (255, 144, 30)}  # BGR: red, blue
FONT = cv2.FONT_HERSHEY_SIMPLEX

MODEL_DIR = config.BACKEND_DIR / "models" / "fire_smoke"
WEIGHTS = MODEL_DIR / "firedetect-11s.pt"
# the same weights exported to OpenVINO (FP16, dynamic input size): about 5x faster on a CPU,
# detections verified against the PyTorch model (see backend/models/README.md)
OV_DIR = MODEL_DIR / "firedetect-11s_openvino_model"
VERIFIER_FILE = MODEL_DIR / "smoke_verifier.npz"
MODEL_NAME = "firedetect-11s.pt (leeyunjai/yolo11-firedetect)"


def canonical_label(name: object) -> str | None:
    """Map a model class name ('Fire', 'smoke', ...) to 'fire' / 'smoke' (None = ignore)."""
    text = str(name).strip().lower()
    if "smoke" in text:
        return "smoke"
    if "fire" in text or "flame" in text:
        return "fire"
    return None


def _green_mask(img: np.ndarray) -> np.ndarray:
    b, g, r = cv2.split(img)
    g16 = g.astype(np.int16)
    return ((g > 150) & (g16 - r > 80) & (g16 - b > 60)).astype(np.uint8)


def remove_green_overlay(frame: np.ndarray) -> tuple[np.ndarray, int]:
    """Erase bright-green lines (the camera's own person/motion boxes burned into the video).
    The fire model calls any green rectangle 'smoke' (0.8-0.9), so they must not reach it.
    Each line pixel is replaced by a clean pixel 10 px beside it. Returns (frame, green pixels)."""
    mask = _green_mask(frame)
    count = int(cv2.countNonZero(mask))
    if count < 50:
        return frame, count
    ys, xs = np.nonzero(mask)
    pad = 12
    y0, y1 = max(0, int(ys.min()) - pad), min(frame.shape[0], int(ys.max()) + pad + 1)
    x0, x1 = max(0, int(xs.min()) - pad), min(frame.shape[1], int(xs.max()) + pad + 1)
    out = frame.copy()
    roi = out[y0:y1, x0:x1]
    todo = cv2.dilate(mask[y0:y1, x0:x1], np.ones((5, 5), np.uint8)).astype(bool)
    bad = todo.copy()
    for dy, dx in ((0, 10), (0, -10), (10, 0), (-10, 0), (10, 10), (-10, -10), (10, -10), (-10, 10)):
        if not todo.any():
            break
        src = np.roll(roi, (dy, dx), axis=(0, 1))
        ok = todo & ~np.roll(bad, (dy, dx), axis=(0, 1))  # the pixel we copy from must be clean
        roi[ok] = src[ok]
        todo &= ~ok
    if todo.any():  # leftovers (e.g. thick crossings): blur them
        roi[todo] = cv2.medianBlur(roi, 11)[todo]
    return out, count


@dataclass(frozen=True)
class Detection:
    label: str                       # "fire" or "smoke"
    conf: float
    box: tuple[int, int, int, int]   # x1, y1, x2, y2 in frame pixels
    weak: bool = False               # below class_conf, above smoke_rules.low_conf (needs more evidence)
    look: str = ""                   # smoke verifier verdict when it is NOT smoke: "cloud" / "dust"
    p_smoke: float = -1.0            # smoke verifier probability (-1 = not checked)


class SmokeVerifier:
    """Second opinion on every smoke box: smoke, cloud or dust?
    A small classifier (smoke_verifier.npz) on the detector's own backbone features of the box.
    Boxes it calls cloud/dust become weak smoke, so they can only alert through the smoke rules."""

    def __init__(self, path: Path, yolo, weights: Path, min_prob: float, max_boxes: int):
        d = np.load(path, allow_pickle=False)
        self.mean, self.scale = d["mean"], d["scale"]
        self.coef, self.intercept = d["coef"], d["intercept"]
        self.classes = [str(c) for c in d["classes"]]
        self.size = int(d["size"])
        self.layers = tuple(int(x) for x in d["layers"])
        self.pad = float(d["pad"])
        self.min_prob, self.max_boxes = min_prob, max_boxes
        sha = hashlib.sha256(weights.read_bytes()).hexdigest()[:16]
        if sha != str(d["weights_sha"]):
            raise ValueError(f"{path.name} was trained for other model weights (not {weights.name})")
        self.net = yolo.model.model[:max(self.layers) + 1]

    def probs(self, frame: np.ndarray, boxes) -> np.ndarray:
        import torch

        h, w = frame.shape[:2]
        crops = []
        for x1, y1, x2, y2 in boxes:
            bw, bh = x2 - x1, y2 - y1
            a, b = max(0, int(x1 - self.pad * bw)), min(w, int(x2 + self.pad * bw))
            c, e = max(0, int(y1 - self.pad * bh)), min(h, int(y2 + self.pad * bh))
            crop = frame[c:e, a:b] if b - a >= 8 and e - c >= 8 else frame
            crops.append(cv2.resize(crop, (self.size, self.size), interpolation=cv2.INTER_AREA)[:, :, ::-1])
        x = torch.from_numpy(np.ascontiguousarray(np.stack(crops))).permute(0, 3, 1, 2).float() / 255.0
        feats = []
        with torch.no_grad():
            for i, layer in enumerate(self.net):
                x = layer(x)
                if i in self.layers:
                    feats += [x.mean((2, 3)), x.amax((2, 3)), x.std((2, 3))]
        f = (torch.cat(feats, 1).numpy() - self.mean) / self.scale
        z = f @ self.coef.T + self.intercept
        z = np.exp(z - z.max(1, keepdims=True))
        return z / z.sum(1, keepdims=True)


def resolve_engine():
    """('openvino' | 'pytorch', model path). AIV_FIRE_ENGINE = auto (default) | openvino | pytorch."""
    want = str(config._env("AIV_FIRE_ENGINE", "auto")).strip().lower()
    gpu = config.DEVICE not in (None, "", "cpu")
    if want == "openvino" or (want == "auto" and not gpu):
        if (OV_DIR / f"{WEIGHTS.stem}.xml").is_file() and config.openvino_available():
            return "openvino", OV_DIR
        if want == "openvino":
            log.warning("AIV_FIRE_ENGINE=openvino but the OpenVINO fire model or package is missing - using PyTorch")
    return "pytorch", WEIGHTS


class Detector:
    """Ultralytics YOLO fire/smoke model running on the CPU (original Detector, settings from the module config).

    Detection runs on OpenVINO when available (same weights, exported); the smoke verifier always uses
    the PyTorch model's backbone, which is then loaded as well."""

    def __init__(self, weights: Path, imgsz: int, iou: float, engine: str = "pytorch", model_path: Path | None = None):
        from ultralytics import YOLO  # heavy import (PyTorch): only when the module starts

        self.weights = weights
        self.imgsz = imgsz
        self.iou = iou
        self.engine = engine
        self._torch = None
        if engine == "openvino":
            try:
                self.model = YOLO(str(model_path), task="detect")
            except Exception as exc:  # noqa: BLE001 - broken export / runtime: fall back, never fail the module
                log.warning("OpenVINO fire model could not be loaded (%s) - using PyTorch", exc)
                self.engine = "pytorch"
        if self.engine == "pytorch":
            self.model = self._torch = YOLO(str(weights), task="detect")
        self.names = {int(k): str(v) for k, v in self.model.names.items()}
        self.label_of = {k: canonical_label(v) for k, v in self.names.items()}
        if not any(self.label_of.values()):
            raise ValueError(f"{weights.name} has no fire/smoke classes: {self.names}")
        self.thresholds = dict.fromkeys(LABELS, 0.5)
        self.pass_conf = dict(self.thresholds)
        self.min_conf = 0.5
        self.degreen = True
        self.overlay_seen = False
        self.verifier: SmokeVerifier | None = None
        self.verifier_error = None

    def configure(self, thresholds: dict, pass_conf: dict, iou: float, imgsz: int, degreen: bool):
        self.thresholds = dict(thresholds)
        self.pass_conf = {k: min(self.thresholds[k], pass_conf.get(k, self.thresholds[k])) for k in LABELS}
        self.min_conf = min(self.pass_conf.values())
        self.iou, self.imgsz, self.degreen = iou, imgsz, degreen

    def enable_verifier(self, path: Path, min_prob: float, max_boxes: int):
        try:
            if self._torch is None:          # OpenVINO detection: the verifier needs the PyTorch backbone
                from ultralytics import YOLO
                self._torch = YOLO(str(self.weights), task="detect")
            self.verifier = SmokeVerifier(path, self._torch, self.weights, min_prob, max_boxes)
            self.verifier_error = None
        except Exception as exc:
            self.verifier = None
            self.verifier_error = str(exc)
            log.warning("smoke verifier not used (%s)", exc)

    @property
    def label(self) -> str:
        return f"{self.weights.stem}@{self.imgsz}"

    def detect(self, frame: np.ndarray) -> list[Detection]:
        if self.degreen:
            frame, green = remove_green_overlay(frame)
            if green >= 50 and not self.overlay_seen:
                self.overlay_seen = True
                log.warning("green overlay boxes found in the video (camera's own person/motion boxes) - they are "
                            "erased before fire detection; better switch the overlay off in the camera")
        result = self.model.predict(frame, imgsz=self.imgsz, conf=self.min_conf, iou=self.iou,
                                    device="cpu", verbose=False)[0]
        boxes = result.boxes
        if boxes is None or len(boxes) == 0:
            return []
        found = []
        for (x1, y1, x2, y2), conf, cls in zip(boxes.xyxy.tolist(), boxes.conf.tolist(), boxes.cls.tolist()):
            label = self.label_of.get(int(cls))
            if label and conf >= self.pass_conf[label]:
                found.append(Detection(label, float(conf), (int(x1), int(y1), int(x2), int(y2)),
                                       weak=conf < self.thresholds[label]))
        if self.verifier is not None:
            found = self._verify(frame, found)
        return found

    def _verify(self, frame: np.ndarray, found: list[Detection]) -> list[Detection]:
        """Ask the smoke/cloud/dust classifier about the (most confident) smoke boxes."""
        idx = sorted((i for i, d in enumerate(found) if d.label == "smoke"), key=lambda i: -found[i].conf)
        idx = idx[:self.verifier.max_boxes]
        if not idx:
            return found
        try:
            probs = self.verifier.probs(frame, [found[i].box for i in idx])
        except Exception as exc:
            log.warning("smoke verifier failed: %r", exc)
            return found
        out = list(found)
        for i, p in zip(idx, probs):
            d = found[i]
            ps = float(p[self.verifier.classes.index("smoke")])
            if ps < self.verifier.min_prob:
                look = max((c for c in self.verifier.classes if c != "smoke"),
                           key=lambda c: p[self.verifier.classes.index(c)])
                out[i] = Detection(d.label, d.conf, d.box, weak=True, look=look, p_smoke=ps)
            else:
                out[i] = Detection(d.label, d.conf, d.box, weak=d.weak, p_smoke=ps)
        return out


# --------------------------------------------------------------------------- drawing (original functions)
def draw_detections(img: np.ndarray, detections, sx: float = 1.0, sy: float = 1.0) -> None:
    h, w = img.shape[:2]
    thickness = max(2, round(min(h, w) / 320))
    scale = max(0.45, min(h, w) / 1000)
    text_thick = max(1, round(scale * 1.6))
    for det in detections:
        x1, y1, x2, y2 = (int(det.box[0] * sx), int(det.box[1] * sy), int(det.box[2] * sx), int(det.box[3] * sy))
        weak = getattr(det, "weak", False)
        color = (200, 170, 120) if weak else COLORS.get(det.label, (0, 255, 255))
        cv2.rectangle(img, (x1, y1), (x2, y2), color, 1 if weak else thickness)
        look = getattr(det, "look", "")
        text = f"{det.label}{'?' if weak else ''} {det.conf:.2f}" + (f" {look}?" if look else "")
        (tw, th), base = cv2.getTextSize(text, FONT, scale, text_thick)
        top = y1 - th - base - 4
        if top < 0:  # no room above the box: put the label inside it
            top = y1
        cv2.rectangle(img, (x1, top), (x1 + tw + 6, top + th + base + 4), color, -1)
        cv2.putText(img, text, (x1 + 3, top + th + 2), FONT, scale, (255, 255, 255), text_thick, cv2.LINE_AA)


def draw_panel(img: np.ndarray, lines: list[str], scale: float = 0.55) -> None:
    """White text on a darkened box in the top-left corner."""
    thick = 1 if scale < 0.8 else 2
    line_h = int(40 * scale)
    width = max(cv2.getTextSize(line, FONT, scale, thick)[0][0] for line in lines) + 16
    height = line_h * len(lines) + int(18 * scale)
    roi = img[:height, :width]
    roi[:] = (roi * 0.35).astype(np.uint8)
    for i, line in enumerate(lines):
        cv2.putText(img, line, (8, line_h * (i + 1)), FONT, scale, (255, 255, 255), thick, cv2.LINE_AA)


def draw_alert_banner(img: np.ndarray, labels) -> None:
    h, w = img.shape[:2]
    cv2.rectangle(img, (0, 0), (w - 1, h - 1), (0, 0, 255), 6)
    text = " + ".join(label.upper() for label in labels) + " DETECTED"
    (tw, th), base = cv2.getTextSize(text, FONT, 0.9, 2)
    x, y = (w - tw) // 2, h - 20
    cv2.rectangle(img, (x - 12, y - th - 12), (x + tw + 12, y + base + 6), (0, 0, 255), -1)
    cv2.putText(img, text, (x, y), FONT, 0.9, (255, 255, 255), 2, cv2.LINE_AA)


def annotate_snapshot(frame: np.ndarray, detections, when: datetime) -> np.ndarray:
    img = frame.copy()
    draw_detections(img, detections)
    best: dict[str, float] = {}
    for det in detections:
        if not getattr(det, "weak", False):
            best[det.label] = max(best.get(det.label, 0.0), det.conf)
    summary = "  ".join(f"{label.upper()} {conf:.2f}" for label, conf in best.items())
    draw_panel(img, [f"{when:%Y-%m-%d %H:%M:%S}  {summary}"], scale=max(0.55, min(img.shape[:2]) / 900))
    return img


# --------------------------------------------------------------------------- smoke motion / time rules (original)
def box_gap(a, b) -> float:
    """Pixel gap between two boxes (0 if they touch or overlap)."""
    dx = max(0, max(a[0], b[0]) - min(a[2], b[2]))
    dy = max(0, max(a[1], b[1]) - min(a[3], b[3]))
    return float((dx * dx + dy * dy) ** 0.5)


def box_iou(a, b) -> float:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class SmokeTrack:
    _next_id = 1

    def __init__(self, box, conf: float, now: float):
        self.id = SmokeTrack._next_id
        SmokeTrack._next_id += 1
        self.samples: list[tuple[float, tuple, float]] = [(now, box, conf)]
        self.first_t = self.last_t = now
        self.confirmed = ""     # reason, once confirmed
        self.rejected = ""      # reason, logged once

    @property
    def box(self):
        return self.samples[-1][1]

    def add(self, box, conf: float, now: float) -> None:
        self.samples.append((now, box, conf))
        self.last_t = now
        if len(self.samples) > 400:
            del self.samples[1:200]  # keep the first sample (start of the track) and the recent ones

    def motion(self, w: int, h: int) -> dict:
        """Rise, growth and sideways drift between the start and now (median of 3 samples each)."""
        def med(boxes, f):
            return statistics.median(f(b) for b in boxes)
        head = [s[1] for s in self.samples[:3]]
        tail = [s[1] for s in self.samples[-3:]]
        top0, top1 = med(head, lambda b: b[1]), med(tail, lambda b: b[1])
        area0 = max(1.0, med(head, lambda b: (b[2] - b[0]) * (b[3] - b[1])))
        area1 = med(tail, lambda b: (b[2] - b[0]) * (b[3] - b[1]))
        cx0, cx1 = med(head, lambda b: (b[0] + b[2]) / 2), med(tail, lambda b: (b[0] + b[2]) / 2)
        return {"age": self.last_t - self.first_t, "rise": (top0 - top1) / h,
                "growth": area1 / area0, "dx": abs(cx1 - cx0) / w}


class SmokeGate:
    """Decides whether WEAK smoke (low_conf <= conf < class_conf.smoke) counts as smoke:
         - a fire box is close to it in the same frame, or
         - the same smoke has been seen for persist_s and it rises or grows (real smoke), or
         - it has been seen for long_persist_s (very persistent haze/smoke).
       Rejected: tracks that mainly move sideways without rising/growing (clouds drifting,
       people walking, vehicles) and short-lived ones (dust behind a vehicle)."""

    def __init__(self, cfg: dict):
        self.low = float(cfg["lowConf"])
        self.fire_near = float(cfg["fireNearFrac"])
        self.persist = float(cfg["persistS"])
        self.min_rise = float(cfg["minRiseFrac"])
        self.min_growth = float(cfg["minGrowth"])
        self.long_persist = float(cfg.get("longPersistS") or 0)
        self.max_gap = float(cfg["maxGapS"])
        self.drift = float(cfg["driftFrac"])
        self.tracks: list[SmokeTrack] = []

    def reset(self) -> None:
        self.tracks = []

    def _match(self, smoke, now: float) -> None:
        unmatched = list(self.tracks)
        for det in sorted(smoke, key=lambda d: -d.conf):
            best, best_iou = None, 0.15
            for tr in unmatched:
                iou = box_iou(det.box, tr.box)
                if iou > best_iou:
                    best, best_iou = tr, iou
            if best is None:
                self.tracks.append(SmokeTrack(det.box, det.conf, now))
            else:
                best.add(det.box, det.conf, now)
                unmatched.remove(best)
        self.tracks = [t for t in self.tracks if now - t.last_t <= self.max_gap]

    def check(self, detections, frame_shape, now: float) -> tuple[bool, float, str]:
        """(weak smoke confirmed in this frame?, its best confidence, reason)"""
        h, w = frame_shape[:2]
        smoke = [d for d in detections if d.label == "smoke"]
        self._match(smoke, now)
        weak = [d for d in smoke if d.weak]
        if not weak:
            return False, 0.0, ""
        fires = [d.box for d in detections if d.label == "fire" and not d.weak]
        for det in weak:
            if any(box_gap(det.box, f) <= self.fire_near * h for f in fires):
                return True, det.conf, "fire nearby"
        hit, conf, why = False, 0.0, ""
        for tr in self.tracks:
            cur = next((d for d in weak if d.box == tr.box), None)
            if tr.last_t != now or cur is None:
                continue
            if not tr.confirmed:
                m = tr.motion(w, h)
                rising = m["rise"] >= self.min_rise or m["growth"] >= self.min_growth
                if m["dx"] >= self.drift and not rising:
                    if not tr.rejected:
                        tr.rejected = (f"moves sideways {m['dx']:.0%} of width without rising/growing "
                                       f"(cloud / passer-by)")
                        log.info("weak smoke #%d ignored: %s", tr.id, tr.rejected)
                    continue
                if m["age"] >= self.persist and rising:
                    tr.confirmed = (f"rising/growing smoke for {m['age']:.0f} s "
                                    f"(rise {m['rise']:.0%}, area x{m['growth']:.1f})")
                elif self.long_persist and m["age"] >= self.long_persist and not cur.look:
                    # (smoke the verifier calls cloud/dust must rise or grow - a dust storm or a
                    #  cloud bank sitting in view for minutes is not enough)
                    tr.confirmed = f"persistent smoke for {m['age']:.0f} s"
                if tr.confirmed:
                    log.info("weak smoke #%d confirmed: %s", tr.id, tr.confirmed)
            if tr.confirmed:
                c = tr.samples[-1][2]
                if c > conf:
                    hit, conf, why = True, c, tr.confirmed
        return hit, conf, why


class AlertWindow:
    """Fire/smoke must be seen in at least M of the last K analysed frames (per class); then one alert
    per class, at most once per cooldown (the original AlertManager rule, without the file saving)."""

    def __init__(self, rule: dict, cooldown_s: float, smoke_cfg: dict | None):
        self.smoke_gate = SmokeGate(smoke_cfg) if smoke_cfg and smoke_cfg.get("enabled") else None
        self.needed = max(1, int(rule.get("consecutiveFrames", 3)))
        self.window = max(self.needed, int(rule.get("windowFrames") or self.needed))
        hits = rule.get("minHits") or {}
        # min_hits can never exceed the window, otherwise that class could never alert
        self.min_hits = {l: min(self.window, max(1, int(hits.get(l, self.needed)))) for l in LABELS}
        self.hist = {l: deque(maxlen=self.window) for l in LABELS}
        self.cooldown = max(0.0, float(cooldown_s))
        self.last_alert: dict[str, float | None] = dict.fromkeys(LABELS, None)
        self.last_conf: dict[str, float] = dict.fromkeys(LABELS, 0.0)
        self.counts = dict.fromkeys(LABELS, 0)
        self.active: frozenset[str] = frozenset()

    def reset_streaks(self) -> None:
        if self.smoke_gate:
            self.smoke_gate.reset()
        self.hist = {l: deque(maxlen=self.window) for l in LABELS}
        self.active = frozenset()

    def update(self, detections, frame_shape, now: float) -> list[tuple[str, float, str]]:
        """-> [(label, best confidence, reason)] of the alerts due now."""
        best: dict[str, float] = {}
        for det in detections:
            if not det.weak:
                best[det.label] = max(best.get(det.label, 0.0), det.conf)
        reason: dict[str, str] = {}
        if self.smoke_gate:  # weak smoke can still count, with extra evidence
            ok, conf, why = self.smoke_gate.check(detections, frame_shape, now)
            if ok and "smoke" not in best:
                best["smoke"], reason["smoke"] = conf, why
        due = []
        for label in LABELS:
            self.hist[label].append(label in best)
            if label in best:
                self.last_conf[label] = best[label]
            hits = sum(self.hist[label])
            last = self.last_alert[label]
            if hits >= self.min_hits[label] and (last is None or now - last >= self.cooldown):
                self.last_alert[label] = now
                self.counts[label] += 1
                # (fix) the class can reach M of K from earlier frames while absent from THIS frame; the
                # original then raised KeyError on best[label] and the alert was lost - report the class's
                # most recent confidence instead.
                due.append((label, best.get(label, self.last_conf[label]), reason.get(label, "")))
        self.active = frozenset(l for l in LABELS if sum(self.hist[l]) >= self.min_hits[l])
        return due


def _in_zones(box, polys) -> bool:
    if not polys:
        return True
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    return any(cv2.pointPolygonTest(p, (float(cx), float(cy)), False) >= 0 for p in polys)


def _zone_polys(zones, w, h):
    out = []
    for z in zones or []:
        if z.get("type") == "rectangle":
            pts = [(z["x"], z["y"]), (z["x"] + z["width"], z["y"]), (z["x"] + z["width"], z["y"] + z["height"]), (z["x"], z["y"] + z["height"])]
        else:
            pts = [(p["x"], p["y"]) for p in z.get("points", [])]
        if len(pts) >= 3:
            out.append(np.array([(x * w, y * h) for x, y in pts], np.float32))
    return out


# --------------------------------------------------------------------------- engine (one per camera session)
class FireEngine:
    """Fire & smoke detection for one camera session.

    submit(frame) hands the newest frame to a background worker every `everyNFrames` frames (skipped
    while the model is still busy, as in the original); draw(annotated) paints the latest boxes and the
    alert banner on the shared annotated stream; confirmed alerts are reported through on_alert."""

    def __init__(self, on_alert):
        if not WEIGHTS.is_file():
            raise FileNotFoundError(f"fire/smoke model missing: {WEIGHTS.relative_to(config.BACKEND_DIR)}")
        self.on_alert = on_alert
        engine, path = resolve_engine()
        self.detector = Detector(WEIGHTS, 480, 0.45, engine=engine, model_path=path)
        log.info("fire & smoke detection engine: %s", "OpenVINO" if self.detector.engine == "openvino" else "PyTorch")
        self.cfg = None
        self.zones = []
        self.window = None
        self._polys_for = None
        self._polys = []
        self._lock = threading.Condition()
        self._job = None
        self._busy = False
        self._stop = threading.Event()
        self._since = 0
        self.result = None           # (detections, frame shape, generation)
        self.generation = 0
        self.latency_ms = None
        self.runs = 0
        self.errors = 0
        self.skipped = 0             # frames handed over while the model was still busy (not analysed)
        self.last_error = None
        self.last_seen = {"fire": None, "smoke": None}
        self.current = {}
        self._worker = threading.Thread(target=self._run, name="fire-worker", daemon=True)
        self._worker.start()

    # ---- configuration (live, from the module config) ----
    def configure(self, cfg: dict, camera_zones: list) -> None:
        cc = cfg.get("classConf") or {}
        thresholds = {"fire": float(cc.get("Fire", cfg["confidence"])), "smoke": float(cc.get("Smoke", cfg["confidence"]))}
        rules = cfg.get("smokeRules") or {}
        classes = cfg.get("classes") or {}
        for lbl in LABELS:                    # a class switched off in the UI never alerts
            if classes.get(lbl, True) is False:
                thresholds[lbl] = 1.01
        smoke_on = classes.get("smoke", True) is not False
        pass_conf = {"smoke": float(rules["lowConf"])} if rules.get("enabled") and smoke_on else {}
        if not smoke_on:
            rules = dict(rules, enabled=False)
        self.detector.configure(thresholds, pass_conf, float(cfg["iou"]), int(cfg["imgSize"]), bool(cfg["removeGreenOverlay"]))
        v = cfg.get("verifier") or {}
        if v.get("enabled") and VERIFIER_FILE.is_file():
            if self.detector.verifier is None or (self.detector.verifier.min_prob, self.detector.verifier.max_boxes) != (
                    float(v["minSmokeProb"]), int(v["maxBoxes"])):
                self.detector.enable_verifier(VERIFIER_FILE, float(v["minSmokeProb"]), int(v["maxBoxes"]))
        else:
            self.detector.verifier = None
        old = self.cfg
        rule_keys = ("alertRule", "smokeRules")
        if old is None or any(old.get(k) != cfg.get(k) for k in rule_keys) or (old.get("alert") or {}).get("cooldownSec") != (cfg.get("alert") or {}).get("cooldownSec"):
            self.window = AlertWindow(cfg["alertRule"], float((cfg.get("alert") or {}).get("cooldownSec", 10)), rules)
        zones = {z["id"]: z for z in camera_zones or [] if z.get("enabled", True)}
        self.zones = [zones[i] for i in (cfg.get("zones") or []) if i in zones]
        self._polys_for = None
        self.cfg = cfg

    def reset(self) -> None:
        self.generation += 1
        if self.window:
            self.window.reset_streaks()
        self.result = None
        self.current = {}

    # ---- frames ----
    def submit(self, frame: np.ndarray, captured_at: float) -> None:
        self._since += 1
        every = max(1, int((self.cfg or {}).get("everyNFrames", 2)))
        if self._since < every:
            return
        with self._lock:
            if self._busy or self._job is not None:
                self.skipped += 1                     # model still busy: skip this frame (original behaviour), counted
                return
            self._job = (frame, captured_at, self.generation)
            self._since = 0
            self._lock.notify()

    def draw(self, annotated: np.ndarray) -> np.ndarray:
        res = self.result
        if res is None or res[2] != self.generation:
            return annotated
        dets, shape, _ = res
        if dets:
            draw_detections(annotated, dets, annotated.shape[1] / shape[1], annotated.shape[0] / shape[0])
        if self.window and self.window.active:
            draw_alert_banner(annotated, sorted(self.window.active))
        return annotated

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            self._lock.notify_all()

    # ---- worker ----
    def _run(self) -> None:
        threads = int(os.environ.get("AIV_FIRE_THREADS", "0") or 0)
        if threads > 0:
            import torch
            torch.set_num_threads(threads)
        while not self._stop.is_set():
            with self._lock:
                while self._job is None and not self._stop.is_set():
                    self._lock.wait(0.5)
                if self._stop.is_set():
                    return
                (frame, captured_at, gen), self._job = self._job, None
                self._busy = True
            try:
                t0 = time.perf_counter()
                dets = self.detector.detect(frame)
                if self.zones:
                    key = (frame.shape[:2], id(self.zones))
                    if self._polys_for != key:
                        self._polys = _zone_polys(self.zones, frame.shape[1], frame.shape[0])
                        self._polys_for = key
                    dets = [d for d in dets if _in_zones(d.box, self._polys)]
                el = (time.perf_counter() - t0) * 1000.0
                self.latency_ms = el if self.latency_ms is None else 0.8 * self.latency_ms + 0.2 * el
                self.runs += 1
                if gen != self.generation:
                    continue
                self.result = (dets, frame.shape[:2], gen)
                now = time.monotonic()
                cur = {}
                for d in dets:
                    if not d.weak:
                        cur[DISPLAY[d.label]] = cur.get(DISPLAY[d.label], 0) + 1
                        self.last_seen[d.label] = time.time()
                self.current = cur
                due = self.window.update(dets, frame.shape, now) if self.window else []
                if due:
                    when = datetime.fromtimestamp(captured_at or time.time())
                    snap = annotate_snapshot(frame, dets, when)
                    for label, conf, reason in due:
                        self.on_alert(label, conf, reason, snap, captured_at, dets)
            except Exception as exc:  # keep running; the session reports the error
                self.errors += 1
                self.last_error = repr(exc)
                log.exception("fire/smoke detection failed")
            finally:
                with self._lock:
                    self._busy = False

    def stats(self) -> dict:
        w = self.window
        v = self.detector.verifier
        return {
            "model": self.detector.label, "engine": "OpenVINO" if self.detector.engine == "openvino" else "PyTorch",
            "inferenceMs": round(self.latency_ms, 1) if self.latency_ms else None,
            "runs": self.runs, "errors": self.errors, "skippedBusy": self.skipped,
            "active": sorted(DISPLAY[l] for l in (w.active if w else ())),
            "alerts": {DISPLAY[k]: v2 for k, v2 in (w.counts if w else {}).items()},
            "thresholds": {DISPLAY[k]: round(v2, 2) for k, v2 in self.detector.thresholds.items()},
            "verifier": "on" if v else ("off" if not (self.cfg or {}).get("verifier", {}).get("enabled") else f"unavailable: {self.detector.verifier_error or 'file missing'}"),
            "smokeRules": bool(w and w.smoke_gate),
            "greenOverlay": self.detector.overlay_seen,
            "rule": {DISPLAY[k]: f"{v2} of last {w.window}" for k, v2 in w.min_hits.items()} if w else {},
        }
