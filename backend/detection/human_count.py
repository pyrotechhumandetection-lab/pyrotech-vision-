"""Human Count - people counting module (written for PYROTECH VISION, independent of the
existing Person/Vehicle pipeline).

    frame -> PersonDetector (pretrained COCO yolov8n, class "person" only)
          -> PeopleTracker  (multi-object tracker written for this module)
          -> HumanCounter   (live count, unique count, peak, average, minute history, alerts)

Detector
    The yolov8n weights already shipped in backend/models (OpenVINO when installed, else PyTorch).
    Detection only (model.predict); no Ultralytics tracker is used.

Tracker (no double counting)
    * every track has a constant-velocity motion model of its box centre (px/s) and a smoothed
      reference size, so its position is predicted at the capture time of the next analysed frame
      and a box truncated by partial occlusion does not break the track
    * association cost = 1 - IoU(predicted box, detection), with a centre-distance gate so a person
      who moved further than their own box between frames is still matched; size changes beyond 2.5x
      are rejected. Optimal assignment (lap.lapjv), confirmed tracks are matched first.
    * two thresholds (like ByteTrack): weaker detections (>= 70 % of the confidence) can continue an
      existing track, only confident ones (>= confidence) start a new one
    * a new track must be matched MIN_HITS times (gaps of at most 0.6 s) before it is CONFIRMED;
      flickering false detections never reach this
    * a confirmed person is counted ONCE, when confirmed (inside the selected zones, if any); the same
      track id is kept while the person is visible and for KEEP_LOST_SEC after they disappear
      (occlusion, missed detection), so they are not counted again when they reappear
    * people now = confirmed tracks detected in the current frame

Limits (stated honestly in the UI and README): a person who is hidden for longer than KEEP_LOST_SEC,
or leaves and re-enters the view, is a new track and counted again - there is no face/appearance
re-identification. Very dense crowds with heavy overlap are under-counted by any box detector.
"""
import logging
import time
from collections import deque
from datetime import datetime, timezone

import cv2
import numpy as np

log = logging.getLogger("aivision.humancount")

PERSON_COCO_ID = 0
LOW_CONF_FACTOR = 0.7          # detections >= confidence * 0.7 may continue an existing track
TENTATIVE_MAX_GAP_SEC = 0.6    # a not-yet-confirmed track dies if unseen for this long
MAX_SIZE_RATIO = 2.5           # reject matches whose box area changes more than this
CENTRE_GATE = 1.0              # max centre distance, in units of the larger box diagonal
COLOR_CONFIRMED = (80, 200, 60)
COLOR_TENTATIVE = (160, 160, 160)
COLOR_OUT_OF_ZONE = (140, 140, 140)


def iou_matrix(a, b):
    """IoU between every box in a (N,4) and b (M,4), xyxy."""
    if not len(a) or not len(b):
        return np.zeros((len(a), len(b)), np.float32)
    ix1 = np.maximum(a[:, None, 0], b[None, :, 0])
    iy1 = np.maximum(a[:, None, 1], b[None, :, 1])
    ix2 = np.minimum(a[:, None, 2], b[None, :, 2])
    iy2 = np.minimum(a[:, None, 3], b[None, :, 3])
    inter = np.clip(ix2 - ix1, 0, None) * np.clip(iy2 - iy1, 0, None)
    area_a = (a[:, 2] - a[:, 0]) * (a[:, 3] - a[:, 1])
    area_b = (b[:, 2] - b[:, 0]) * (b[:, 3] - b[:, 1])
    return inter / (area_a[:, None] + area_b[None, :] - inter + 1e-6)


def assign(cost, limit):
    """Minimum-cost assignment; pairs with cost > limit stay unmatched. Returns [(row, col)]."""
    if cost.size == 0:
        return []
    try:
        import lap   # installed with Ultralytics (ByteTrack dependency)
        _, x, _ = lap.lapjv(cost.astype(np.float64), extend_cost=True, cost_limit=limit)
        return [(i, int(j)) for i, j in enumerate(x) if j >= 0]
    except Exception:   # greedy fallback
        pairs, used_r, used_c = [], set(), set()
        for flat in np.argsort(cost, axis=None):
            i, j = divmod(int(flat), cost.shape[1])
            if cost[i, j] > limit:
                break
            if i not in used_r and j not in used_c:
                pairs.append((i, j))
                used_r.add(i)
                used_c.add(j)
        return pairs


class Track:
    """One person. Motion model: box centre with constant velocity (px/s); box size is smoothed
    separately (ref_w / ref_h), so a box truncated by partial occlusion neither drags the prediction
    nor blocks the match when the full person is visible again."""
    __slots__ = ("id", "box", "c", "v", "wh", "ref", "score", "hits", "first", "last", "confirmed", "counted", "in_zone", "seen_now")

    def __init__(self, tid, box, score, t):
        self.id = tid
        self.box = np.asarray(box, np.float32)
        self.c = np.array([(box[0] + box[2]) / 2, (box[1] + box[3]) / 2], np.float32)
        self.v = np.zeros(2, np.float32)
        self.wh = np.array([box[2] - box[0], box[3] - box[1]], np.float32)
        self.ref = self.wh.copy()               # slowly-adapting reference size (largest recent extent)
        self.score = float(score)
        self.hits = 1
        self.first = self.last = t
        self.confirmed = False
        self.counted = False
        self.in_zone = False
        self.seen_now = True

    def predict(self, t):
        dt = min(max(t - self.last, 0.0), 1.0)      # do not extrapolate further than 1 s
        c = self.c + self.v * dt
        w, h = self.wh
        return np.array([c[0] - w / 2, c[1] - h / 2, c[0] + w / 2, c[1] + h / 2], np.float32)

    def update(self, box, score, t):
        box = np.asarray(box, np.float32)
        c = np.array([(box[0] + box[2]) / 2, (box[1] + box[3]) / 2], np.float32)
        wh = np.array([box[2] - box[0], box[3] - box[1]], np.float32)
        dt = t - self.last
        if dt > 1e-3:
            v = (c - self.c) / dt
            self.v = v if self.hits == 1 else 0.6 * self.v + 0.4 * v
            lim = 3.0 * float(np.hypot(*self.ref))     # clamp implausible jumps (detector noise)
            sp = float(np.hypot(*self.v))
            if sp > lim:
                self.v *= lim / max(sp, 1e-6)
        self.wh = 0.5 * self.wh + 0.5 * wh
        self.ref = np.maximum(wh, 0.9 * self.ref + 0.1 * wh)
        self.box, self.c = box, c
        self.score = float(score)
        self.hits += 1
        self.last = t
        self.seen_now = True


class PeopleTracker:
    def __init__(self, min_hits=3, keep_lost_sec=2.0):
        self.min_hits = int(min_hits)
        self.keep_lost_sec = float(keep_lost_sec)
        self.tracks = []
        self.next_id = 1

    def reset(self):
        self.tracks = []
        self.next_id = 1

    def update(self, dets, t, new_conf):
        """dets: (N,5) [x1,y1,x2,y2,score]. Returns the list of tracks seen in this frame."""
        for tr in self.tracks:
            tr.seen_now = False
        boxes = dets[:, :4] if len(dets) else np.zeros((0, 4), np.float32)
        free = list(range(len(dets)))
        for group in ([tr for tr in self.tracks if tr.confirmed], [tr for tr in self.tracks if not tr.confirmed]):
            if not group or not free:
                continue
            pred = np.array([tr.predict(t) for tr in group], np.float32)
            cand = boxes[free]
            cost = 1.0 - iou_matrix(pred, cand)
            # centre-distance gate + size gate; pairs with no overlap but close centres get a cost < 1
            pc = np.stack([(pred[:, 0] + pred[:, 2]) / 2, (pred[:, 1] + pred[:, 3]) / 2], 1)
            dc = np.stack([(cand[:, 0] + cand[:, 2]) / 2, (cand[:, 1] + cand[:, 3]) / 2], 1)
            dist = np.linalg.norm(pc[:, None, :] - dc[None, :, :], axis=2)
            pd = np.array([np.hypot(*tr.ref) for tr in group], np.float32)
            dd = np.hypot(cand[:, 2] - cand[:, 0], cand[:, 3] - cand[:, 1])
            norm = dist / np.maximum(pd[:, None], dd[None, :]).clip(min=1.0)
            pa = (pred[:, 2] - pred[:, 0]) * (pred[:, 3] - pred[:, 1])
            ra = np.array([tr.ref[0] * tr.ref[1] for tr in group], np.float32)
            ca = (cand[:, 2] - cand[:, 0]) * (cand[:, 3] - cand[:, 1])
            rr = lambda x: np.maximum(x[:, None], ca[None, :]) / np.maximum(np.minimum(x[:, None], ca[None, :]), 1.0)   # noqa: E731
            ratio = np.minimum(rr(pa), rr(ra))
            no_overlap = cost >= 0.999
            cost = np.where(no_overlap, 0.98 + 0.01 * norm, cost)            # distance-only match ranks after any overlap
            cost = np.where((norm > CENTRE_GATE) | (ratio > MAX_SIZE_RATIO), 9.0, cost)
            used = set()
            for i, j in assign(cost, 0.995):
                tr = group[i]
                d = dets[free[j]]
                tr.update(d[:4], d[4], t)
                if not tr.confirmed and tr.hits >= self.min_hits:
                    tr.confirmed = True
                used.add(free[j])
            free = [k for k in free if k not in used]
        # unmatched confident detections start new tentative tracks
        for k in free:
            if dets[k, 4] >= new_conf:
                self.tracks.append(Track(self.next_id, dets[k, :4], dets[k, 4], t))
                self.next_id += 1
                if self.min_hits <= 1:
                    self.tracks[-1].confirmed = True
        # drop stale tracks
        self.tracks = [tr for tr in self.tracks
                       if (tr.confirmed and t - tr.last <= self.keep_lost_sec) or (not tr.confirmed and t - tr.last <= TENTATIVE_MAX_GAP_SEC)]
        return [tr for tr in self.tracks if tr.seen_now]


class PersonDetector:
    """Pretrained COCO yolov8n, person class only (OpenVINO or PyTorch - see config.resolve_engine)."""

    def __init__(self, model_path, img_size=640, device=None, engine="pytorch"):
        from ultralytics import YOLO
        self.model = YOLO(str(model_path), task="detect")
        self.img_size = img_size
        self.device = device
        self.engine = engine

    def detect(self, frame, conf):
        kwargs = dict(imgsz=self.img_size, conf=conf, classes=[PERSON_COCO_ID], verbose=False)
        if self.device is not None:
            kwargs["device"] = self.device
        r = self.model.predict(frame, **kwargs)[0]
        if r.boxes is None or not len(r.boxes):
            return np.zeros((0, 5), np.float32)
        return np.concatenate([r.boxes.xyxy.cpu().numpy(), r.boxes.conf.cpu().numpy()[:, None]], 1).astype(np.float32)


def _zone_polys(zones, w, h):
    out = []
    for z in zones or []:
        if z.get("type") == "rectangle":
            pts = [(z["x"], z["y"]), (z["x"] + z["width"], z["y"]), (z["x"] + z["width"], z["y"] + z["height"]), (z["x"], z["y"] + z["height"])]
        else:
            pts = [(p["x"], p["y"]) for p in z.get("points", [])]
        if len(pts) >= 3:
            out.append((z, np.array([(int(x * w), int(y * h)) for x, y in pts], np.int32)))
    return out


def _hex_bgr(c, fb=(19, 217, 154)):
    try:
        c = c.lstrip("#")
        return (int(c[4:6], 16), int(c[2:4], 16), int(c[0:2], 16))
    except Exception:
        return fb


class HumanCounter:
    """Per-camera people counter with live statistics."""

    HISTORY_MINUTES = 60

    def __init__(self, model_path, img_size=640, device=None, engine="pytorch"):
        self.detector = PersonDetector(model_path, img_size, device, engine)
        self.engine = engine
        self.tracker = PeopleTracker()
        self.reset()

    def reset(self):
        self.tracker.reset()
        self.started_at = time.time()
        self.unique = 0
        self.current = 0
        self.detected = 0
        self.peak = 0
        self.peak_at = None
        self.last_person_at = None
        self.samples = deque()                 # (t, current) for the 60 s average
        self.minutes = deque(maxlen=self.HISTORY_MINUTES)
        self._minute = None                    # [minute_start, max, sum, n, new]
        self.recent = deque(maxlen=20)         # recently counted people (results table)
        self.completed_minutes = []            # finished minute records waiting to be persisted

    def configure(self, cfg, camera_zones):
        cnt = cfg.get("counting") or {}
        self.conf = float(cfg.get("confidence", 0.4))
        self.tracker.min_hits = int(cnt.get("minFrames", 3))
        self.tracker.keep_lost_sec = float(cnt.get("keepLostSec", 2.0))
        zones = {z["id"]: z for z in (camera_zones or []) if z.get("enabled", True)}
        self.zones = [zones[i] for i in (cfg.get("zones") or []) if i in zones]

    def _in_zone(self, box, polys):
        if not polys:
            return True
        foot = (float((box[0] + box[2]) / 2), float(box[3]))       # people are counted where they stand
        return any(cv2.pointPolygonTest(p, foot, False) >= 0 for _, p in polys)

    def process(self, raw, annotated, t):
        """raw: clean frame for inference; annotated: frame to draw on (may be the same array);
        t: capture time of the frame. Returns (annotated, inference_ms, newly_counted)."""
        h, w = raw.shape[:2]
        t0 = time.perf_counter()
        dets = self.detector.detect(raw, round(self.conf * LOW_CONF_FACTOR, 3))
        inf_ms = (time.perf_counter() - t0) * 1000.0
        seen = self.tracker.update(dets, t, self.conf)
        polys = _zone_polys(self.zones, w, h)
        new = []
        current = 0
        for tr in seen:
            tr.in_zone = self._in_zone(tr.box, polys)
            if tr.confirmed and tr.in_zone:
                current += 1
                if not tr.counted:
                    tr.counted = True
                    self.unique += 1
                    item = {"trackId": tr.id, "confidence": round(tr.score, 3), "countedAt": datetime.fromtimestamp(t, timezone.utc).astimezone().isoformat(timespec="milliseconds"),
                            "box": [round(float(tr.box[0]) / w, 4), round(float(tr.box[1]) / h, 4), round(float(tr.box[2]) / w, 4), round(float(tr.box[3]) / h, 4)]}
                    self.recent.appendleft(item)
                    new.append(item)
        self.current = current
        self.detected = int((dets[:, 4] >= self.conf).sum()) if len(dets) else 0
        if current:
            self.last_person_at = t
        if current > self.peak:
            self.peak, self.peak_at = current, t
        self._sample(t, current, len(new))
        self._draw(annotated, seen, polys)
        return annotated, inf_ms, new

    # ------------------------------------------------------------------ statistics
    def _sample(self, t, current, new):
        self.samples.append((t, current))
        while self.samples and t - self.samples[0][0] > 60:
            self.samples.popleft()
        m0 = int(t // 60) * 60
        if self._minute and self._minute[0] != m0:
            self._close_minute()
        if not self._minute:
            self._minute = [m0, 0, 0.0, 0, 0]
        mm = self._minute
        mm[1] = max(mm[1], current)
        mm[2] += current
        mm[3] += 1
        mm[4] += new

    def _close_minute(self):
        m0, mx, s, n, new = self._minute
        rec = {"t": datetime.fromtimestamp(m0, timezone.utc).astimezone().isoformat(timespec="seconds"), "max": mx,
               "avg": round(s / n, 2) if n else 0.0, "new": new, "frames": n}
        self.minutes.append(rec)
        self.completed_minutes.append(rec)
        self._minute = None

    def flush(self):
        """Close the current (partial) minute and return every unsaved minute record."""
        if self._minute:
            self._close_minute()
        return self.take_completed_minutes()

    def take_completed_minutes(self):
        out, self.completed_minutes = self.completed_minutes, []
        return out

    def stats(self):
        avg = (sum(c for _, c in self.samples) / len(self.samples)) if self.samples else 0.0
        cur_min = None
        if self._minute:
            m0, mx, s, n, new = self._minute
            cur_min = {"t": datetime.fromtimestamp(m0, timezone.utc).astimezone().isoformat(timespec="seconds"), "max": mx,
                       "avg": round(s / n, 2) if n else 0.0, "new": new, "frames": n, "partial": True}
        iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="milliseconds") if ts else None   # noqa: E731
        return {
            "current": self.current, "detected": self.detected, "unique": self.unique,
            "peak": self.peak, "peakAt": iso(self.peak_at), "average60s": round(avg, 2),
            "tracksActive": sum(1 for tr in self.tracker.tracks if tr.confirmed),
            "since": iso(self.started_at), "lastPersonAt": iso(self.last_person_at),
            "minutes": list(self.minutes) + ([cur_min] if cur_min else []),
            "recent": list(self.recent),
            "settings": {"confidence": self.conf, "minFrames": self.tracker.min_hits, "keepLostSec": self.tracker.keep_lost_sec,
                         "zones": [z.get("name") for z in self.zones]},
        }

    # ------------------------------------------------------------------ drawing
    def _draw(self, img, seen, polys):
        for z, p in polys:
            cv2.polylines(img, [p], True, _hex_bgr(z.get("color", "#13D99A")), 2)
        for tr in seen:
            x1, y1, x2, y2 = (int(v) for v in tr.box)
            if tr.confirmed:
                col = COLOR_CONFIRMED if tr.in_zone else COLOR_OUT_OF_ZONE
                cv2.rectangle(img, (x1, y1), (x2, y2), col, 2)
                cv2.putText(img, f"ID {tr.id}", (x1, max(y1 - 7, 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, col, 2)
            else:
                cv2.rectangle(img, (x1, y1), (x2, y2), COLOR_TENTATIVE, 1)
        label = f"People now: {self.current}   Counted: {self.unique}"
        scale = max(0.6, img.shape[1] / 1600)
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        x0 = img.shape[1] - tw - 22          # top-right: the UI puts its LIVE badge top-left
        cv2.rectangle(img, (x0 - 6, 8), (img.shape[1] - 8, 22 + th), (20, 20, 20), -1)
        cv2.putText(img, label, (x0, 14 + th), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), 2)
