"""Lightweight motion gate (Human Detection and Object Monitoring only).

Small grey-scale frame difference: the frame is shrunk to MOTION_WIDTH px, blurred, and compared with the
previous sample; motion = more than min_area_pct % of the pixels changed by more than DIFF_THRESHOLD.
Sampled at most every SAMPLE_SEC (about 1-2 ms per sample on a 1080p frame) in the camera's AI loop thread,
never in the capture (reader) thread.

It is a TRIGGER, not a detector: no motion does not mean nobody is there (a person standing still, a small /
distant person, slow movement). The scheduler therefore keeps running the AI for motionHoldSec after the last
motion and every fallbackSec without motion.
"""
import time

import cv2
import numpy as np

MOTION_WIDTH = 160
DIFF_THRESHOLD = 25
SAMPLE_SEC = 0.2


class MotionGate:
    def __init__(self):
        self.prev = None
        self.last_sample = 0.0
        self.last_motion_at = None
        self.changed_pct = 0.0
        self.samples = 0
        self.diff = None                 # bool mask (small frame) of the last sample: pixel changed
        self.sample_id = 0               # increments with every new sample
        self._masks = {}                 # zone key -> bool mask (small frame)

    def update(self, frame, now=None, min_area_pct=0.5):
        """Feed a frame (BGR). Returns True when motion was seen in this sample (False between samples)."""
        now = time.time() if now is None else now
        if now - self.last_sample < SAMPLE_SEC:
            return False
        self.last_sample = now
        h, w = frame.shape[:2]
        small = cv2.resize(frame, (MOTION_WIDTH, max(1, int(h * MOTION_WIDTH / w))), interpolation=cv2.INTER_AREA)
        g = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)
        prev, self.prev = self.prev, g
        self.samples += 1
        if prev is None or prev.shape != g.shape:
            return False
        diff = cv2.absdiff(prev, g)
        self.diff = diff > DIFF_THRESHOLD
        self.sample_id += 1
        self.changed_pct = float(self.diff.mean() * 100.0)
        if self.changed_pct >= min_area_pct:
            self.last_motion_at = now
            return True
        return False

    def motion_in(self, zones, min_area_pct):
        """Motion of the LAST sample inside the given detection zones (normalised rectangle / polygon dicts as
        stored on the camera); no zones = the whole frame. min_area_pct is relative to the zone area, so a
        small zone still reacts to a person entering it and trees / sky outside the zones are ignored."""
        if self.diff is None:
            return False
        if not zones:
            return self.changed_pct >= min_area_pct
        h, w = self.diff.shape
        key = (h, w, tuple(sorted(str(z.get("id")) + str(z.get("points") or (z.get("x"), z.get("y"), z.get("width"), z.get("height")))
                                  for z in zones)))
        mask = self._masks.get(key)
        if mask is None:
            m = np.zeros((h, w), np.uint8)
            for z in zones:
                if z.get("type") == "rectangle":
                    pts = [(z["x"], z["y"]), (z["x"] + z["width"], z["y"]), (z["x"] + z["width"], z["y"] + z["height"]), (z["x"], z["y"] + z["height"])]
                else:
                    pts = [(p["x"], p["y"]) for p in z.get("points", [])]
                if len(pts) >= 3:
                    cv2.fillPoly(m, [np.array([(int(x * w), int(y * h)) for x, y in pts], np.int32)], 1)
            mask = m.astype(bool)
            if len(self._masks) > 32:
                self._masks.clear()
            self._masks[key] = mask
        n = int(mask.sum())
        if n == 0:
            return self.changed_pct >= min_area_pct
        return float(self.diff[mask].sum()) * 100.0 / n >= min_area_pct

    def active(self, now, hold_sec):
        """Motion within the last hold_sec seconds."""
        return self.last_motion_at is not None and now - self.last_motion_at <= hold_sec
