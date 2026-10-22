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
        self.changed_pct = float((diff > DIFF_THRESHOLD).mean() * 100.0)
        if self.changed_pct >= min_area_pct:
            self.last_motion_at = now
            return True
        return False

    def active(self, now, hold_sec):
        """Motion within the last hold_sec seconds."""
        return self.last_motion_at is not None and now - self.last_motion_at <= hold_sec
