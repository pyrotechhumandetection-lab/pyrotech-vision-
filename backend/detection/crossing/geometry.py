"""Virtual-line crossing: geometry and per-track state. No model and no I/O, so every rule is unit-testable.

Line: two points A -> B in NORMALISED picture coordinates (0..1) plus the resolution it was drawn on
(refWidth / refHeight). All distances are computed in pixels of the analysed frame, so a line drawn on a
1280x720 picture is the same physical line on a 640x360 substream (same aspect ratio).

Sides: s(P) = cross(B-A, P-A) / |B-A| in pixels, positive on the side the arrow points to (the normal
(-dy, dx) of A->B). A track that moves from the negative to the positive side crosses "in", the other way "out".

A crossing is counted once when ALL of these hold:
  * the track was seen firmly (outside the hysteresis band) on one side and later firmly on the other side;
  * the straight path between those two observations cuts the FINITE line segment, which is extended at both ends by
    `end_extend` x the object's box width (a person passing right at A or B still counts; walking clearly around an end
    does not);
  * the track has been observed in at least `min_frames` frames (a one-frame ghost never counts);
  * the same track has not crossed within `recross_sec` (cooldown);
  * no other crossing in the same direction happened a moment ago at the same spot (`dedupe_sec`): this catches
    the tracker giving one person a new id right at the line.
A track that disappears and is replaced by a new id close to where it vanished (`handover_sec`) inherits the
old state, so an occlusion does not turn one person into two.
"""
import math

IN, OUT = "in", "out"


def clamp01(v):
    return min(1.0, max(0.0, float(v)))


class Line:
    def __init__(self, x1, y1, x2, y2, ref_w=None, ref_h=None):
        self.x1, self.y1, self.x2, self.y2 = clamp01(x1), clamp01(y1), clamp01(x2), clamp01(y2)
        self.ref_w, self.ref_h = ref_w, ref_h

    @classmethod
    def from_cfg(cls, cfg):
        ln = (cfg or {}).get("line") or None
        if not ln:
            return None
        try:
            line = cls(ln["x1"], ln["y1"], ln["x2"], ln["y2"], ln.get("refWidth"), ln.get("refHeight"))
        except (KeyError, TypeError, ValueError):
            return None
        return line if line.length_norm() > 0.01 else None

    def length_norm(self):
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)

    def px(self, w, h):
        return self.x1 * w, self.y1 * h, self.x2 * w, self.y2 * h

    def signed(self, p, w, h):
        """Signed distance (pixels) of point p from the line; > 0 on the arrow side."""
        ax, ay, bx, by = self.px(w, h)
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy) or 1.0
        return (dx * (p[1] - ay) - dy * (p[0] - ax)) / n

    def arrow_normal(self, w, h):
        ax, ay, bx, by = self.px(w, h)
        dx, dy = bx - ax, by - ay
        n = math.hypot(dx, dy) or 1.0
        return -dy / n, dx / n

    def aspect_mismatch(self, w, h, tol=0.03):
        """True when the stream's aspect ratio differs from the one the line was drawn on (the line would shift)."""
        if not self.ref_w or not self.ref_h or not w or not h:
            return False
        return abs((w / h) / (self.ref_w / self.ref_h) - 1.0) > tol


def ref_point(box, mode="bottom"):
    x1, y1, x2, y2 = box
    return ((x1 + x2) / 2.0, y2 if mode == "bottom" else (y1 + y2) / 2.0)


def cuts_segment(line, a, b, w, h, tol_px=0.0):
    """Does the path a -> b (pixel points on opposite sides of the infinite line) cut the finite segment?"""
    d1, d2 = line.signed(a, w, h), line.signed(b, w, h)
    if d1 == d2 or (d1 > 0) == (d2 > 0):
        return False
    t = d1 / (d1 - d2)
    ix, iy = a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])
    ax, ay, bx, by = line.px(w, h)
    dx, dy = bx - ax, by - ay
    ll = dx * dx + dy * dy
    u = ((ix - ax) * dx + (iy - ay) * dy) / ll
    ext = tol_px / math.sqrt(ll)
    return -ext <= u <= 1.0 + ext


class _Track:
    __slots__ = ("tid", "cls", "n", "first_t", "last_t", "side", "anchor", "last", "cool_until", "crossed", "height",
                 "width", "born_fid", "handed_from", "short_counted", "origin", "travel")

    def __init__(self, tid, cls, t, fid):
        self.tid, self.cls, self.n = tid, cls, 0
        self.first_t = self.last_t = t
        self.side, self.anchor, self.last = 0, None, None
        self.cool_until, self.crossed = 0.0, 0
        self.height = self.width = 0.0
        self.born_fid, self.handed_from, self.short_counted = fid, None, False
        self.origin, self.travel = None, 0.0           # where the track started; farthest it has been from there (pixels)


class CrossingTracker:
    """Crossing decisions for the tracked objects of ONE line (one module, one camera)."""

    def __init__(self, line, width, height, *, ref="bottom", min_frames=3, hysteresis=0.05, recross_sec=2.0,
                 handover_sec=1.0, dedupe_sec=1.0, forget_sec=3.0, end_extend=0.5):
        self.line, self.w, self.h, self.ref = line, int(width), int(height), ref
        self.min_frames, self.hyst, self.recross = int(min_frames), float(hysteresis), float(recross_sec)
        self.handover_sec, self.dedupe_sec, self.forget_sec = float(handover_sec), float(dedupe_sec), float(forget_sec)
        self.end_extend = float(end_extend)
        self.tracks = {}
        self.recent = []                      # (t, direction, x, y, width) of accepted crossings
        self.stats = {"tracksStarted": 0, "tracksLost": 0, "tracksShort": 0, "handovers": 0, "untracked": 0,
                      "duplicatesSuppressed": 0, "shortSuppressed": 0, "cooldownSuppressed": 0, "aroundEnd": 0,
                      "framesSeen": 0, "trackFramesTotal": 0, "lifeFramesLost": 0}

    def set_frame_size(self, w, h):
        self.w, self.h = int(w), int(h)

    def update(self, dets, t, fid=None):
        """dets: dicts with tid (int or None), box [x1,y1,x2,y2] (pixels of the frame), cls, conf. Returns crossing events."""
        self.stats["framesSeen"] += 1
        events, seen = [], set()
        ids_now = {d["tid"] for d in dets if d.get("tid") is not None}
        for d in dets:
            tid = d.get("tid")
            if tid is None:
                self.stats["untracked"] += 1
                continue
            if tid in seen:
                continue
            seen.add(tid)
            x1, y1, x2, y2 = d["box"]
            pt = ref_point(d["box"], self.ref)
            tr = self.tracks.get(tid)
            if tr is None:
                tr = _Track(tid, d.get("cls"), t, fid)
                self.stats["tracksStarted"] += 1
                self._handover(tr, pt, max(x2 - x1, y2 - y1), ids_now, t)
                self.tracks[tid] = tr
            gap = t - tr.last_t
            tr.n += 1
            tr.height, tr.width = max(1.0, y2 - y1), max(1.0, x2 - x1)
            if tr.origin is None:
                tr.origin = pt
            tr.travel = max(tr.travel, math.hypot(pt[0] - tr.origin[0], pt[1] - tr.origin[1]))
            s = self.line.signed(pt, self.w, self.h)
            dead = max(3.0, self.hyst * tr.height)
            firm = abs(s) >= dead
            if tr.side == 0:
                if firm:
                    tr.side, tr.anchor = (1 if s > 0 else -1), pt
            elif firm and (s > 0) == (tr.side > 0):
                tr.anchor = pt                             # still on its side: the anchor follows
            elif firm:
                new_side = 1 if s > 0 else -1
                hit = cuts_segment(self.line, tr.anchor, pt, self.w, self.h, tol_px=max(dead, self.end_extend * tr.width))
                if hit:
                    ev = self._accept(tr, d, pt, new_side, t, fid, gap)
                    if ev:
                        events.append(ev)
                else:
                    self.stats["aroundEnd"] += 1           # the path passed beyond an end of the line
                tr.side, tr.anchor = new_side, pt
            tr.last, tr.last_t = pt, t
            self.stats["trackFramesTotal"] += 1
        for tid in [k for k, tr in self.tracks.items() if k not in seen and t - tr.last_t > max(self.forget_sec, self.handover_sec)]:
            self._retire(self.tracks.pop(tid))
        self.recent = [r for r in self.recent if t - r[0] <= max(self.dedupe_sec, 2.0)]
        return events

    def _accept(self, tr, d, pt, new_side, t, fid, gap):
        direction = IN if new_side > 0 else OUT
        if tr.n < self.min_frames:
            self.stats["shortSuppressed"] += 1
            return None
        if t < tr.cool_until:
            self.stats["cooldownSuppressed"] += 1
            return None
        tr.cool_until = t + self.recross
        w = tr.width
        for rt, rdir, rx, ry, rw in self.recent:
            if rdir == direction and t - rt <= self.dedupe_sec and math.hypot(pt[0] - rx, pt[1] - ry) <= 0.35 * max(w, rw):
                self.stats["duplicatesSuppressed"] += 1
                return None
        self.recent.append((t, direction, pt[0], pt[1], w))
        tr.crossed += 1
        return {"tid": tr.tid, "direction": direction, "t": t, "frameId": fid, "point": [round(pt[0], 1), round(pt[1], 1)],
                "box": [round(float(v), 1) for v in d["box"]], "cls": d.get("cls"), "conf": d.get("conf"),
                "gapSec": round(gap, 3), "trackFrames": tr.n, "handedOver": tr.handed_from is not None,
                "crossingNo": tr.crossed, "travel": round(tr.travel / max(tr.height, tr.width), 2)}   # in units of the object's own size

    def _handover(self, new, pt, size, ids_now, t):
        """A new id starting close to where a recently lost track of the same class vanished continues that track."""
        best, best_d = None, None
        for old in self.tracks.values():
            if old.tid in ids_now or old.cls != new.cls or old.last is None:
                continue
            age = t - old.last_t
            if age <= 0 or age > self.handover_sec:
                continue
            dist = math.hypot(pt[0] - old.last[0], pt[1] - old.last[1])
            if dist <= 1.0 * max(old.height, old.width, size * 0.5) and (best is None or dist < best_d):
                best, best_d = old, dist
        if best is None:
            return
        new.side, new.anchor, new.cool_until, new.crossed = best.side, best.anchor, best.cool_until, best.crossed
        new.n, new.first_t, new.handed_from = best.n, best.first_t, best.tid
        new.origin, new.travel = best.origin, best.travel
        self.stats["handovers"] += 1
        self.tracks.pop(best.tid, None)               # the old id is gone for good

    def _retire(self, tr):
        self.stats["tracksLost"] += 1
        self.stats["lifeFramesLost"] += tr.n
        if tr.n < self.min_frames:
            self.stats["tracksShort"] += 1

    def reliability(self):
        """Measured, not predicted: how stable were the tracks this tracker saw."""
        s = self.stats
        started = s["tracksStarted"]
        return {"tracksStarted": started, "tracksLost": s["tracksLost"], "tracksShort": s["tracksShort"],
                "handovers": s["handovers"], "duplicatesSuppressed": s["duplicatesSuppressed"],
                "untrackedDetections": s["untracked"],
                "shortTrackRatio": round(s["tracksShort"] / s["tracksLost"], 3) if s["tracksLost"] else None,
                "meanTrackFrames": round(s["lifeFramesLost"] / s["tracksLost"], 1) if s["tracksLost"] else None,
                "activeTracks": len(self.tracks)}
