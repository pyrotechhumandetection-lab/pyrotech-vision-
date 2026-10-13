"""
matcher.py
----------
LAYER 1 building blocks: remember what a marked object looks like, and find it
again - without any class label.

ObjectReference (made once, when the user draws the box):
  * template      grayscale crop, stored at an "analysis scale" where the
                  object's short side is ~min_side_px (small objects are kept
                  at full resolution, big ones are shrunk for speed)
  * fresh         a refreshed copy of the template, taken while the object is
                  confirmed in place (follows slow daylight changes; the
                  original is always kept, so the reference cannot drift away)
  * quadrants     2x2 parts of the template: if 2+ parts still match in place,
                  the object is there but partly covered (e.g. by a hand)
  * ring          the surroundings of the box: if they changed too, something
                  big is in front of the object (a person) -> "occluded", not
                  "missing"
  * hist          hue/saturation histogram - rejects look-alike matches
  * keypoints     ORB (or SIFT) features - find the object even rotated/tilted

Matching uses normalised cross-correlation (cv2.TM_CCOEFF_NORMED), which does
not change when the whole patch gets brighter or darker.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .common import Box, clip_box, expand_box


def to_gray(img: np.ndarray) -> np.ndarray:
    return img if img.ndim == 2 else cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)


def scaled_crop(frame: np.ndarray, rect: Box, scale: float, gray: bool = True) -> np.ndarray:
    """Crop `rect` (full-resolution pixels) and resize it by `scale`."""
    x, y, w, h = rect
    crop = frame[y:y + h, x:x + w]
    if scale != 1.0:
        size = (max(1, round(w * scale)), max(1, round(h * scale)))
        crop = cv2.resize(crop, size, interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
    return to_gray(crop) if gray else crop


def hs_histogram(bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hist = cv2.calcHist([hsv], [0, 1], None, [30, 32], [0, 180, 0, 256])
    cv2.normalize(hist, hist, alpha=1.0, norm_type=cv2.NORM_L1)
    return hist


def hist_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(max(0.0, cv2.compareHist(a, b, cv2.HISTCMP_CORREL)))


def pearson(a: np.ndarray, b: np.ndarray) -> float:
    """Correlation of two equally shaped pixel arrays (1 = same picture, even if brighter/darker).
    Nearly flat areas (a plain wall) have no structure to correlate, so compare levels instead."""
    a = a.astype(np.float32).ravel()
    b = b.astype(np.float32).ravel()
    if a.std() < 3.0 or b.std() < 3.0:
        return float(max(0.0, 1.0 - abs(float(a.mean()) - float(b.mean())) / 40.0))
    return float(np.corrcoef(a, b)[0, 1])


def match_map(image: np.ndarray, templ: np.ndarray, flat: bool) -> np.ndarray | None:
    """Correlation map of `templ` over `image` (None if the image is smaller than the template)."""
    if image.shape[0] < templ.shape[0] or image.shape[1] < templ.shape[1] or templ.size < 16:
        return None
    method = cv2.TM_CCORR_NORMED if flat else cv2.TM_CCOEFF_NORMED  # plain objects: no mean removal
    res = cv2.matchTemplate(image, templ, method)
    return np.nan_to_num(res, nan=0.0, posinf=0.0, neginf=0.0)


@dataclass
class Candidate:
    score: float     # 0..1 match quality
    box: Box         # where the object is (full-resolution pixels)
    method: str      # "template" or "features"


class ObjectReference:
    """Appearance of a marked object at the moment it was marked (see module docstring)."""

    def __init__(self, frame: np.ndarray, box: Box, min_side_px: int = 64, features: str = "orb"):
        H, W = frame.shape[:2]
        self.frame_size = (W, H)
        self.box = clip_box(box, W, H)
        x, y, w, h = self.box
        self.scale = float(min(1.0, max(0.2, min_side_px / max(1, min(w, h)))))
        self.template = scaled_crop(frame, self.box, self.scale)
        self.flat = float(self.template.std()) < 4.0   # plain, texture-less object
        self.fresh: np.ndarray | None = None
        self.hist = hs_histogram(frame[y:y + h, x:x + w])
        self._make_quadrants()
        self._make_ring(frame)
        # keypoint features on a crop at least ~120 px on its short side
        self.feature_kind = "sift" if features.lower() == "sift" else "orb"
        self.feat = cv2.SIFT_create(nfeatures=400) if self.feature_kind == "sift" else cv2.ORB_create(nfeatures=500)
        self.feat_scale = float(min(2.0, max(self.scale, 120.0 / max(1, min(w, h)))))
        img = scaled_crop(frame, self.box, self.feat_scale)
        self.kp, self.des = self.feat.detectAndCompute(img, None)
        self.feat_size = img.shape[1], img.shape[0]

    # -- parts ---------------------------------------------------------------
    def _make_quadrants(self) -> None:
        th, tw = self.template.shape
        self.quadrants = []  # (template part, x offset, y offset) in template pixels
        for oy in (0, th // 2):
            for ox in (0, tw // 2):
                part = self.template[oy:oy + th // 2, ox:ox + tw // 2]
                if part.size >= 64 and float(part.std()) >= 4.0:
                    self.quadrants.append((part, ox, oy))

    def _make_ring(self, frame: np.ndarray) -> None:
        W, H = self.frame_size
        self.ring_rect = expand_box(self.box, 2.0, W, H, min_pad=12)
        ring = scaled_crop(frame, self.ring_rect, self.scale)
        mask = np.ones(ring.shape, bool)
        rx, ry = self.ring_rect[:2]
        x, y, w, h = self.box
        x0, y0 = round((x - rx) * self.scale), round((y - ry) * self.scale)
        mask[y0:y0 + round(h * self.scale), x0:x0 + round(w * self.scale)] = False
        self.ring_mask, self.ring_ref = mask, ring

    def templates(self) -> list[np.ndarray]:
        return [t for t in (self.template, self.fresh) if t is not None]

    def refresh(self, frame: np.ndarray, box: Box) -> None:
        """Take a fresh reference while the object is confirmed in place (slow light changes)."""
        th, tw = self.template.shape
        crop = scaled_crop(frame, clip_box(box, *self.frame_size), self.scale)
        self.fresh = cv2.resize(crop, (tw, th)) if crop.shape != (th, tw) else crop
        ring = scaled_crop(frame, self.ring_rect, self.scale)
        if ring.shape == self.ring_ref.shape:
            self.ring_ref = ring

    # -- checks --------------------------------------------------------------
    def home_score(self, frame: np.ndarray) -> tuple[float, tuple[float, float], int]:
        """Best match close to the home position -> (score, (dx, dy) shift in px, parts in place)."""
        x, y, w, h = self.box
        margin = max(6, int(0.15 * max(w, h)))
        win = clip_box((x - margin, y - margin, w + 2 * margin, h + 2 * margin), *self.frame_size)
        img = scaled_crop(frame, win, self.scale)
        best, loc = 0.0, (0, 0)
        for templ in self.templates():
            res = match_map(img, templ, self.flat)
            if res is not None:
                _, mx, _, ml = cv2.minMaxLoc(res)
                if mx > best:
                    best, loc = float(mx), ml
        dx = win[0] + loc[0] / self.scale - x
        dy = win[1] + loc[1] / self.scale - y
        # partial-occlusion test: how many quadrants still match at their own place
        parts = 0
        mpx = max(2, round(margin * self.scale * 0.5))
        ox0, oy0 = round((x - win[0]) * self.scale), round((y - win[1]) * self.scale)
        for part, ox, oy in self.quadrants:
            px, py = ox0 + ox - mpx, oy0 + oy - mpx
            region = img[max(0, py):py + part.shape[0] + 2 * mpx, max(0, px):px + part.shape[1] + 2 * mpx]
            res = match_map(region, part, False)
            if res is not None and float(res.max()) >= 0.6:
                parts += 1
        return best, (dx, dy), parts

    def ring_similarity(self, frame: np.ndarray) -> float:
        """How much the surroundings still look like when the object was marked (1 = same)."""
        ring = scaled_crop(frame, self.ring_rect, self.scale)
        if ring.shape != self.ring_ref.shape:
            ring = cv2.resize(ring, (self.ring_ref.shape[1], self.ring_ref.shape[0]))
        return pearson(ring[self.ring_mask], self.ring_ref[self.ring_mask])

    def search(self, frame: np.ndarray, window: Box, extra: float = 1.0,
               scales=(0.85, 1.0, 1.18)) -> Candidate | None:
        """Multi-scale template search inside `window` (full-res px). `extra` < 1 searches a
        further-shrunk image (fast whole-frame search), then the result is refined locally."""
        s = self.scale * extra
        img = scaled_crop(frame, window, s)
        best: Candidate | None = None
        for templ in self.templates():
            for k in scales:
                size = (max(4, round(templ.shape[1] * extra * k)), max(4, round(templ.shape[0] * extra * k)))
                t = cv2.resize(templ, size, interpolation=cv2.INTER_AREA) if size != templ.shape[::-1] else templ
                res = match_map(img, t, self.flat)
                if res is None:
                    continue
                _, mx, _, ml = cv2.minMaxLoc(res)
                if best is None or mx > best.score:
                    box = (int(window[0] + ml[0] / s), int(window[1] + ml[1] / s), int(t.shape[1] / s), int(t.shape[0] / s))
                    best = Candidate(float(mx), box, "template")
        if best is not None and extra < 1.0:  # refine at full analysis scale around the coarse hit
            refined = self.search(frame, expand_box(best.box, 1.6, *self.frame_size, min_pad=8), 1.0, scales)
            if refined is not None:
                best = refined
        return best

    def feature_search(self, frame: np.ndarray, window: Box) -> Candidate | None:
        """ORB/SIFT keypoints + RANSAC homography: finds the object even if turned or tilted."""
        if self.des is None or len(self.kp) < 12:
            return None
        # ORB/SIFT work across scales, so never search an image larger than ~1400 px
        s = min(self.feat_scale, 1400.0 / max(window[2], window[3]))
        img = scaled_crop(frame, window, s)
        kp2, des2 = self.feat.detectAndCompute(img, None)
        if des2 is None or len(kp2) < 12:
            return None
        norm = cv2.NORM_HAMMING if self.feature_kind == "orb" else cv2.NORM_L2
        pairs = cv2.BFMatcher(norm).knnMatch(self.des, des2, k=2)
        good = [m for m, *rest in pairs if rest and m.distance < 0.75 * rest[0].distance]
        if len(good) < 10:
            return None
        src = np.float32([self.kp[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        H, inl = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
        if H is None or inl is None:
            return None
        inliers = int(inl.sum())
        if inliers < 10 or inliers < 0.3 * len(good):
            return None
        fw, fh = self.feat_size
        corners = cv2.perspectiveTransform(np.float32([[0, 0], [fw, 0], [fw, fh], [0, fh]]).reshape(-1, 1, 2), H)
        bx, by, bw, bh = cv2.boundingRect(corners)
        # size check in full-resolution pixels (reference crop was taken at feat_scale)
        area_ratio = (bw / s * bh / s) / max(1.0, (fw / self.feat_scale) * (fh / self.feat_scale))
        if not 0.4 <= area_ratio <= 2.5:
            return None
        box = clip_box((int(window[0] + bx / s), int(window[1] + by / s), int(bw / s), int(bh / s)), *self.frame_size)
        return Candidate(min(1.0, 0.55 + 0.01 * inliers), box, "features")

    def hist_similarity(self, frame: np.ndarray, box: Box) -> float:
        x, y, w, h = clip_box(box, *self.frame_size)
        return hist_similarity(self.hist, hs_histogram(frame[y:y + h, x:x + w]))
