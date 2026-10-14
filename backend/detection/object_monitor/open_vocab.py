"""
open_vocab.py (Object Monitoring Layer 2 - ported from the standalone marked-object monitor)
-------------
LAYER 2 - open-vocabulary detector. It gives marked objects a NAME and spots
people covering them. It never raises alarms (that is Layer 1's job).

Supported models (ultralytics):
  * YOLOE-26   e.g. "yoloe-26s-seg.pt"   (text prompts via MobileCLIP2)
  * YOLO-World e.g. "yolov8s-worldv2.pt" (text prompts via CLIP)
  * plain COCO YOLO, e.g. "yolov8n.pt"   (80 fixed classes - baseline only)

The office vocabulary comes from config.yaml as {name: [prompts]}. Every prompt
becomes one model class; detections are reported under the vocabulary name.
Text embeddings are computed once and cached in models/ (so the big text
encoder is not loaded on every start).
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from backend.runtime import gated, name_thread

from .common import Box, box_center, log

# COCO names -> office vocabulary names (only used for the COCO baseline).
COCO_TO_VOCAB = {
    "tv": "computer monitor", "laptop": "laptop", "keyboard": "keyboard", "mouse": "mouse",
    "cell phone": "phone", "bottle": "water bottle", "cup": "mug", "chair": "chair",
    "backpack": "backpack", "handbag": "backpack", "book": "book", "person": "person",
    "scissors": "scissors", "clock": "clock", "dining table": "table",
}


@dataclass(frozen=True)
class Det:
    name: str      # vocabulary name, e.g. "lunch box"
    prompt: str    # the prompt that matched, e.g. "tiffin box"
    conf: float
    box: Box       # x, y, w, h in full-frame pixels


def prompt_table(vocabulary: dict) -> tuple[list[str], list[str]]:
    """{name: [prompts]} -> (prompts, names) with one entry per prompt."""
    prompts, names = [], []
    for name, plist in (vocabulary or {}).items():
        for prompt in (plist or [name]):
            prompts.append(str(prompt).strip().lower())
            names.append(str(name).strip())
    return prompts, names


@contextlib.contextmanager
def _working_dir(path: Path):
    """Ultralytics downloads bare file names (text encoders) into the current folder:
    switch to models/ so they land there (and are found there next time)."""
    old = Path.cwd()
    path.mkdir(parents=True, exist_ok=True)
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def _nms(dets: list[Det], iou_thr: float) -> list[Det]:
    """Class-aware NMS for merging tile results."""
    import cv2
    out: list[Det] = []
    for name in {d.name for d in dets}:
        group = [d for d in dets if d.name == name]
        keep = cv2.dnn.NMSBoxes([list(d.box) for d in group], [d.conf for d in group], 0.0, iou_thr)
        out.extend(group[int(i)] for i in np.array(keep).reshape(-1))
    return out


class OpenVocabDetector:
    """Loads one model with the office vocabulary; detect(frame) -> list[Det]."""

    def __init__(self, model: str, vocabulary: dict, models_dir: Path, imgsz: int = 960,
                 conf: float = 0.15, iou: float = 0.5, tiles: int = 0):
        self.model_name = model
        self.imgsz, self.conf, self.iou, self.tiles = int(imgsz), float(conf), float(iou), int(tiles)
        self.models_dir = Path(models_dir)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        weights = Path(model) if Path(model).is_absolute() else self.models_dir / model
        lower = weights.name.lower()
        self.kind = "yoloe" if "yoloe" in lower else "world" if "world" in lower else "coco"
        self.prompts, self.prompt_names = prompt_table(vocabulary)
        t0 = time.perf_counter()
        if self.kind == "yoloe":
            self.model = self._load_yoloe(weights)
        elif self.kind == "world":
            self.model = self._load_world(weights)
        else:
            from ultralytics import YOLO
            self.model = YOLO(str(self._ensure(weights)))
        self.class_names = {int(k): str(v) for k, v in self.model.names.items()}
        log("DET", f"Layer 2 detector {weights.name} ({self.kind}) ready in {time.perf_counter() - t0:.1f} s "
                   f"| {len(set(self.prompt_names)) if self.kind != 'coco' else 80} classes | imgsz {self.imgsz}"
                   f"{' | tiles 2x2' if self.tiles else ''}")

    # -- loading ---------------------------------------------------------------
    def _ensure(self, weights: Path) -> Path:
        """The weights must be in backend/models/object_monitor (no download at runtime - the backend
        works without internet; the original downloaded official ultralytics weights on first use)."""
        if not weights.is_file():
            raise FileNotFoundError(f"Object Monitoring model missing: {weights.name} (expected in {weights.parent})")
        return weights

    def _vocab_hash(self) -> str:
        return hashlib.sha1("\n".join(self.prompts).encode("utf-8")).hexdigest()[:10]

    def _load_yoloe(self, weights: Path):
        from ultralytics import YOLOE
        model = YOLOE(str(self._ensure(weights)))
        cache = self.models_dir / f"{weights.stem}_vocab_{self._vocab_hash()}.npz"
        if cache.is_file():
            model.load_prompt_embeddings(cache)
        else:
            # A changed vocabulary needs the MobileCLIP2 text encoder (mobileclip2_b.ts, ~254 MB) once to
            # build a new embedding cache. It is not shipped: the cache for the original vocabulary is.
            if not (self.models_dir / "mobileclip2_b.ts").is_file():
                raise FileNotFoundError(f"no text-embedding cache {cache.name} for this vocabulary and no text encoder "
                                        f"(mobileclip2_b.ts) in {self.models_dir} - use the original vocabulary or add the encoder")
            log("DET", f"Computing text embeddings for {len(self.prompts)} prompts (first run only) ...")
            with _working_dir(self.models_dir):
                model.set_classes(self.prompts, model.get_text_pe(self.prompts))
            model.save_prompt_embeddings(cache)
        return model

    def _load_world(self, weights: Path):
        from ultralytics import YOLOWorld
        cache = self.models_dir / f"{weights.stem}_vocab_{self._vocab_hash()}.pt"
        if cache.is_file():
            return YOLOWorld(str(cache))  # classes + text features saved inside
        model = YOLOWorld(str(self._ensure(weights)))
        log("DET", f"Computing CLIP text embeddings for {len(self.prompts)} prompts (first run only) ...")
        with _working_dir(self.models_dir):
            model.set_classes(list(self.prompts))
        model.model.clip_model = None  # keep only the text features: 26 MB file instead of ~330 MB with CLIP
        model.save(str(cache))
        return YOLOWorld(str(cache))

    # -- inference -------------------------------------------------------------
    def _name_of(self, cls: int) -> tuple[str, str]:
        raw = self.class_names.get(cls, str(cls))
        if self.kind == "coco":
            return COCO_TO_VOCAB.get(raw, raw), raw
        return (self.prompt_names[cls] if cls < len(self.prompt_names) else raw), raw

    def _predict(self, image: np.ndarray, imgsz: int, conf: float, offset=(0, 0)) -> list[Det]:
        with gated():      # waits for a free inference slot (AIV_INFER_CONCURRENCY)
            res = self.model.predict(image, imgsz=imgsz, conf=conf, iou=self.iou, device="cpu", verbose=False)[0]
        if res.boxes is None or len(res.boxes) == 0:
            return []
        ox, oy = offset
        out = []
        for (x1, y1, x2, y2), c, k in zip(res.boxes.xyxy.tolist(), res.boxes.conf.tolist(), res.boxes.cls.tolist()):
            name, prompt = self._name_of(int(k))
            out.append(Det(name, prompt, float(c), (int(x1) + ox, int(y1) + oy, int(x2 - x1), int(y2 - y1))))
        return out

    def detect(self, frame: np.ndarray, conf: float | None = None) -> list[Det]:
        conf = self.conf if conf is None else conf
        if not self.tiles:
            return self._predict(frame, self.imgsz, conf)
        # 2x2 overlapping tiles: small objects get ~2x more pixels per inference.
        h, w = frame.shape[:2]
        tw, th = int(w * 0.6), int(h * 0.6)  # 60% tiles -> 20% overlap
        dets: list[Det] = []
        for ty in (0, h - th):
            for tx in (0, w - tw):
                dets += self._predict(frame[ty:ty + th, tx:tx + tw], self.imgsz, conf, (tx, ty))
        return _nms(dets, self.iou)

    def suggest_name(self, frame: np.ndarray, box: Box, crop_conf: float, full_conf: float) -> tuple[Det | None, str]:
        """Name for a marked object: a zoomed crop first (precise), else a full-frame pass (finds more).
        Returns (detection, 'crop' | 'full frame' | 'unsure'); 'unsure' = best weak guess, not a name."""
        d = self.name_box(frame, box)
        if d is not None and d.conf >= crop_conf:
            return d, "crop"
        f = best_match(self.detect(frame, conf=min(full_conf, self.conf)), box)
        if f is not None and f.conf >= full_conf:
            return f, "full frame"
        return (d or f), "unsure"

    def name_box(self, frame: np.ndarray, box: Box) -> Det | None:
        """Best non-person detection for a user-drawn box. Runs on a padded crop at 640 px, so small
        objects get zoomed in. Returns the best guess even if weak - the caller applies name_conf."""
        x, y, w, h = box
        fh, fw = frame.shape[:2]
        px, py = int(w * 0.6) + 16, int(h * 0.6) + 16
        x1, y1, x2, y2 = max(0, x - px), max(0, y - py), min(fw, x + w + px), min(fh, y + h + py)
        return best_match(self._predict(frame[y1:y2, x1:x2], 640, 0.05, (x1, y1)), box)


def best_match(dets: list[Det], box: Box) -> Det | None:
    """The detection that IS the marked object: it must overlap it closely (IoU >= 0.25, or have
    its centre inside with a similar size). A desk/table box around a small object is rejected."""
    from .common import iou
    x, y, w, h = box
    best, best_score = None, 0.0
    for d in dets:
        if d.name == "person":
            continue
        dcx, dcy = box_center(d.box)
        ratio = (d.box[2] * d.box[3]) / max(1, w * h)
        overlap = iou(d.box, box)
        if overlap < 0.25 and not (x <= dcx <= x + w and y <= dcy <= y + h and 0.3 <= ratio <= 3.0):
            continue
        score = d.conf * (0.5 + overlap)
        if score > best_score:
            best, best_score = d, score
    return best


class DetectorWorker(threading.Thread):
    """Runs the detector in the background: the full frame every `interval_s`, plus on-demand
    name requests for newly marked objects (those go first). Never blocks the video."""

    def __init__(self, detector: OpenVocabDetector, interval_s: float, name_conf: float = 0.3,
                 name_conf_full: float = 0.3):
        super().__init__(name="DetectorWorker", daemon=True)
        self.detector = detector
        self.interval_s = float(interval_s)
        self.name_conf, self.name_conf_full = float(name_conf), float(name_conf_full)
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._name_jobs: list[tuple] = []
        self._stop_evt = threading.Event()
        self.latest: list[Det] = []
        self.latest_time = 0.0          # monotonic time of the analysed frame
        self.latency = 0.0
        self.runs = 0

    def offer_frame(self, frame: np.ndarray) -> None:
        with self._cond:
            self._frame = frame
            self._cond.notify()

    def request_name(self, frame: np.ndarray, box: Box, callback) -> None:
        with self._cond:
            self._name_jobs.append((frame, box, callback))
            self._cond.notify()

    def stop(self) -> None:
        self._stop_evt.set()
        with self._cond:
            self._cond.notify_all()

    def people(self, max_age_s: float) -> list[Box]:
        """Recent person boxes (used by Layer 1 to tell 'covered' from 'gone')."""
        if time.monotonic() - self.latest_time > max_age_s:
            return []
        return [d.box for d in self.latest if d.name == "person"]

    def run(self) -> None:
        name_thread("obj-detector")      # OS thread name: CPU of this worker shows up in the perf monitor
        next_run = 0.0
        while not self._stop_evt.is_set():
            with self._cond:
                while not self._stop_evt.is_set() and not self._name_jobs and (
                        self._frame is None or time.monotonic() < next_run):
                    self._cond.wait(0.2)
                if self._stop_evt.is_set():
                    return
                job = self._name_jobs.pop(0) if self._name_jobs else None
                frame = None if job else self._frame
            try:
                if job:
                    jframe, box, callback = job
                    callback(*self.detector.suggest_name(jframe, box, self.name_conf, self.name_conf_full))
                    continue
                t_frame = time.monotonic()
                t0 = time.perf_counter()
                dets = self.detector.detect(frame)
                self.latency = time.perf_counter() - t0
                self.latest, self.latest_time = dets, t_frame
                self.runs += 1
                next_run = t_frame + self.interval_s
            except Exception as exc:  # keep the app running
                log("ERROR", f"Detector failed: {exc!r}")
                next_run = time.monotonic() + self.interval_s
