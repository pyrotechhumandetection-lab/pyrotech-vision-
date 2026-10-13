"""
verifier.py (Object Monitoring Layer 3, optional - ported from the standalone marked-object monitor)
-----------
In the shared backend the model is read ONLY from backend/models/object_monitor/hf (offline); it is not
shipped with the repository (689 MB) - see backend/models/README.md.
LAYER 3 (optional) - Grounding DINO (Apache-2.0, via HuggingFace transformers)
suggests a NAME for a marked object. It runs ONLY when the user draws a box or
when an alert fires - never per frame - in its own thread. The model is loaded
on first use (downloaded once into models/hf) and unloaded again after
`unload_after_s` idle seconds to give the RAM back.
"""
from __future__ import annotations

import gc
import os
import queue
import threading
import time
from pathlib import Path

os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")  # Windows without developer mode: harmless

import cv2
import numpy as np

from .common import Box, box_center, log


class GroundingDinoVerifier(threading.Thread):
    def __init__(self, cfg: dict, vocabulary: dict, models_dir: Path):
        super().__init__(name="GroundingDINO", daemon=True)
        self.cfg = cfg
        self.names = [n for n in vocabulary if n != "person"]
        self.cache_dir = Path(models_dir) / "hf"
        self._jobs: queue.Queue = queue.Queue()
        self._stop_evt = threading.Event()
        self._model = self._processor = None
        self._last_use = 0.0
        self.last_seconds = 0.0

    def request(self, frame: np.ndarray, box: Box, callback) -> None:
        """callback(name, score) - name is None if nothing was recognised."""
        self._jobs.put((frame, box, callback))

    def stop(self) -> None:
        self._stop_evt.set()
        self._jobs.put(None)

    def _load(self) -> None:
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor
        from transformers.utils import logging as hf_logging
        hf_logging.disable_progress_bar()
        t0 = time.perf_counter()
        mid = self.cfg["model_id"]
        self._processor = AutoProcessor.from_pretrained(mid, cache_dir=self.cache_dir, local_files_only=True)
        self._model = AutoModelForZeroShotObjectDetection.from_pretrained(mid, cache_dir=self.cache_dir,
                                                                          local_files_only=True).eval()
        log("DINO", f"Grounding DINO loaded in {time.perf_counter() - t0:.1f} s")

    def _unload(self) -> None:
        self._model = self._processor = None
        gc.collect()
        log("DINO", "Grounding DINO unloaded (idle)")

    def suggest(self, frame: np.ndarray, box: Box) -> tuple[str | None, float]:
        """Best vocabulary name for the object in `box` (runs on a padded crop)."""
        import torch
        from PIL import Image
        if self._model is None:
            self._load()
        x, y, w, h = box
        H, W = frame.shape[:2]
        px, py = int(w * 0.4) + 10, int(h * 0.4) + 10
        x1, y1, x2, y2 = max(0, x - px), max(0, y - py), min(W, x + w + px), min(H, y + h + py)
        crop = Image.fromarray(cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2RGB))
        text = ". ".join(self.names) + "."
        t0 = time.perf_counter()
        edge = int(self.cfg.get("image_size", 800))  # crops are small: 800 px (the default) mostly costs time
        inputs = self._processor(images=crop, text=text, return_tensors="pt",
                                 size={"shortest_edge": edge, "longest_edge": int(edge * 1.333)})
        with torch.inference_mode():
            outputs = self._model(**inputs)
        res = self._processor.post_process_grounded_object_detection(
            outputs, inputs.input_ids, threshold=float(self.cfg["box_threshold"]),
            text_threshold=float(self.cfg["text_threshold"]), target_sizes=[crop.size[::-1]])[0]
        self.last_seconds = time.perf_counter() - t0
        labels = res["text_labels"] if "text_labels" in res else res.get("labels", [])
        cx, cy = box_center((x - x1, y - y1, w, h))
        best, best_score = None, 0.0
        for score, label, (bx1, by1, bx2, by2) in zip(res["scores"].tolist(), labels, res["boxes"].tolist()):
            inside = bx1 <= cx <= bx2 and by1 <= cy <= by2
            score = score * (1.0 if inside else 0.4)
            name = self._to_vocab(str(label))
            if name and score > best_score:
                best, best_score = name, score
        return best, best_score

    def _to_vocab(self, label: str) -> str | None:
        label = label.strip().lower()
        for name in sorted(self.names, key=len, reverse=True):  # longest name first ("lunch box" before "box")
            if name in label or label in name:
                return name
        return None

    def run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                job = self._jobs.get(timeout=5)
            except queue.Empty:
                if self._model is not None and time.monotonic() - self._last_use > float(self.cfg["unload_after_s"]):
                    self._unload()
                continue
            if job is None:
                break
            frame, box, callback = job
            try:
                name, score = self.suggest(frame, box)
                callback(name, score)
            except Exception as exc:
                log("ERROR", f"Grounding DINO failed: {exc!r}")
            self._last_use = time.monotonic()
