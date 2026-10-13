"""
common.py (Object Monitoring - ported from the standalone marked-object monitor, src/common.py)
---------
Small helpers shared by all modules: terminal logging, rate meter, box maths.
Boxes are always (x, y, w, h) in full-resolution frame pixels unless noted.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

Box = tuple[int, int, int, int]  # x, y, w, h

_logger = logging.getLogger("aivision.object")


def log(tag: str, message: str) -> None:
    """Original console line '[TAG] message', now written to the backend log."""
    (_logger.error if tag == "ERROR" else _logger.info)("[%s] %s", tag, message)


def display_path(path: Path) -> str:
    """Path relative to the current folder (forward slashes) when possible."""
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in text.strip().lower()).strip("-") or "object"


def box_center(box: Box) -> tuple[float, float]:
    x, y, w, h = box
    return x + w / 2.0, y + h / 2.0


def box_diag(box: Box) -> float:
    return math.hypot(box[2], box[3])


def clip_box(box: Box, width: int, height: int) -> Box:
    x, y, w, h = box
    x1, y1 = max(0, min(int(x), width - 1)), max(0, min(int(y), height - 1))
    x2, y2 = max(x1 + 1, min(int(x + w), width)), max(y1 + 1, min(int(y + h), height))
    return x1, y1, x2 - x1, y2 - y1


def expand_box(box: Box, factor: float, width: int, height: int, min_pad: int = 0) -> Box:
    """Grow a box around its centre by `factor` (1.0 = same size), clipped to the frame."""
    x, y, w, h = box
    pad_x, pad_y = max(min_pad, w * (factor - 1) / 2), max(min_pad, h * (factor - 1) / 2)
    return clip_box((int(x - pad_x), int(y - pad_y), int(w + 2 * pad_x), int(h + 2 * pad_y)), width, height)


def overlap_fraction(inner: Box, outer: Box) -> float:
    """Fraction of `inner`'s area covered by `outer` (not IoU: a big person box fully covering a
    small object gives 1.0 here, where IoU would be tiny)."""
    ix1, iy1 = max(inner[0], outer[0]), max(inner[1], outer[1])
    ix2, iy2 = min(inner[0] + inner[2], outer[0] + outer[2]), min(inner[1] + inner[3], outer[1] + outer[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    return (ix2 - ix1) * (iy2 - iy1) / max(1.0, inner[2] * inner[3])


def iou(a: Box, b: Box) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    return inter / max(1.0, a[2] * a[3] + b[2] * b[3] - inter)


class RateMeter:
    """Events per second over a sliding window (thread-safe)."""

    def __init__(self, window_s: float = 2.0):
        self.window = window_s
        self._times: deque[float] = deque()
        self._lock = threading.Lock()
        self._start = time.monotonic()

    def tick(self) -> None:
        now = time.monotonic()
        with self._lock:
            self._times.append(now)
            while self._times and now - self._times[0] > self.window:
                self._times.popleft()

    def rate(self) -> float:
        now = time.monotonic()
        with self._lock:
            while self._times and now - self._times[0] > self.window:
                self._times.popleft()
            span = min(self.window, now - self._start)
            return len(self._times) / span if span > 0.5 else 0.0
