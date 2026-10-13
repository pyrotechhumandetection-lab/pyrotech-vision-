"""
tracker.py
----------
Short-term OpenCV tracker (CSRT / KCF / MIL) that follows a marked object
BETWEEN the full checks, e.g. while someone carries it. Trackers cannot tell
that an object has gone (they happily follow the empty desk), so they only
give the display a smooth box and the next check a place to look first; the
decisions are always made by the matcher.

CSRT and KCF need opencv-contrib-python; MIL is in every OpenCV build.
"""
from __future__ import annotations

import cv2
import numpy as np

from .common import Box

_FACTORIES = {
    "csrt": ("TrackerCSRT_create", "legacy.TrackerCSRT_create"),
    "kcf": ("TrackerKCF_create", "legacy.TrackerKCF_create"),
    "mil": ("TrackerMIL_create",),
}


def create_tracker(kind: str):
    """cv2 tracker instance, falling back to MIL if CSRT/KCF are not available."""
    for name in _FACTORIES.get(kind.lower(), ()) + _FACTORIES["mil"]:
        obj = cv2
        for part in name.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                break
        if callable(obj):
            return obj()
    return None


class BoxTracker:
    """One tracker working on a shared downscaled copy of the frame (`small`, factor `scale`)."""

    def __init__(self, kind: str):
        self.kind = kind
        self._tracker = None

    def start(self, small: np.ndarray, scale: float, box: Box) -> None:
        self._tracker = create_tracker(self.kind)
        if self._tracker is None:
            return
        x, y, w, h = box
        rect = (int(x * scale), int(y * scale), max(4, int(w * scale)), max(4, int(h * scale)))
        try:
            self._tracker.init(small, rect)
        except cv2.error:
            self._tracker = None

    def update(self, small: np.ndarray, scale: float) -> Box | None:
        if self._tracker is None:
            return None
        try:
            ok, (x, y, w, h) = self._tracker.update(small)
        except cv2.error:
            ok = False
        if not ok:
            return None
        return int(x / scale), int(y / scale), int(w / scale), int(h / scale)

    def stop(self) -> None:
        self._tracker = None

    @property
    def active(self) -> bool:
        return self._tracker is not None
