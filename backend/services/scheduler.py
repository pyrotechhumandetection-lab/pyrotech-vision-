"""Per-camera, per-module AI frame scheduling (multi-camera optimisation, Methods 1 / 6 / 8).

One ModuleSchedule per running module of a camera session. The camera session reads every frame (live view
keeps its frame rate); for each frame it asks every module's schedule whether the AI should run on it:

    intervalSec == 0   every frame (the previous behaviour)
    intervalSec  > 0   at most one analysed frame per interval; the newest eligible frame is used
    motion gate        (Human Detection / Object Monitoring only) when the interval is due, the AI runs only if
                       the picture changed within motionHoldSec, or when fallbackSec passed without an analysis

Every frame is accounted for exactly once per module:
    selected   picked for the AI          processed  analysed successfully
    sampling   skipped by the interval    motion     skipped: interval due but no motion (= AI call avoided)
    stale      rejected: older than the stale limit when it would have been analysed
    replaced   older queued frames skipped so the NEWEST frame is analysed (queue pressure)
"""

class ModuleSchedule:
    def __init__(self, module_id):
        self.module = module_id
        self.interval = 0.0
        self.motion_gate = False
        self.hold = 10.0
        self.fallback = 30.0
        self.last_run = None            # time of the last analysis (selection)
        self.next_slot = 0.0            # earliest time of the next analysis
        self.c = {"selected": 0, "processed": 0, "sampling": 0, "motion": 0, "fallback": 0, "stale": 0,
                  "replaced": 0, "failed": 0}
        self.last = {"frameId": None, "capturedAt": None, "ageMs": None, "inferenceMs": None, "reason": None}
        self.hist = []                  # (time, processed) for the AI fps over the last RATE_WINDOW seconds

    def configure(self, sch, gate_allowed):
        sch = sch or {}
        self.interval = max(0.0, float(sch.get("intervalSec", 0) or 0))
        self.motion_gate = bool(gate_allowed and sch.get("motionGate"))
        self.hold = float(sch.get("motionHoldSec", 10) or 0)
        self.fallback = max(1.0, float(sch.get("fallbackSec", 30) or 30))
        if self.interval == 0:
            self.next_slot = 0.0

    @property
    def sampled(self):
        return self.interval > 0

    def decide(self, now, motion):
        """(run, reason) for the frame at time `now`. motion: callable(now) -> bool (motion within hold)."""
        if not self.sampled and not self.motion_gate:
            return True, "every-frame"
        if now < self.next_slot:
            self.c["sampling"] += 1
            return False, None
        if self.motion_gate:
            if motion(now):
                return True, "motion"
            if self.last_run is None or now - self.last_run >= self.fallback:
                self.c["fallback"] += 1
                return True, "fallback"
            # interval due but nothing moves: AI call avoided; next check one interval later (or next frame
            # when the module analyses every frame)
            self.c["motion"] += 1
            self.next_slot = now + (self.interval if self.sampled else 0.0)
            return False, None
        return True, "interval"

    def stale(self):
        self.c["stale"] += 1

    def replaced(self, n):
        self.c["replaced"] += n

    def selected(self, now, frame_id, captured, reason):
        self.c["selected"] += 1
        self.last_run = now
        if self.sampled:
            self.next_slot = now + self.interval
        self.last.update(frameId=frame_id, capturedAt=captured, reason=reason,
                         ageMs=round((now - captured) * 1000.0, 1) if captured else None)

    def done(self, ok, inference_ms=None):
        self.c["processed" if ok else "failed"] += 1
        if inference_ms is not None:
            self.last["inferenceMs"] = round(inference_ms, 1)

    RATE_WINDOW = 30.0

    def snapshot(self, prev_counts, el, now=None):
        """Metrics (cumulative counters + AI analyses per second measured over the last RATE_WINDOW s;
        at one analysis every few seconds a 1-second window would only show 0 or 1)."""
        import time as _t
        now = _t.time() if now is None else now
        c = dict(self.c)
        self.hist.append((now, c["processed"]))
        while len(self.hist) > 2 and now - self.hist[0][0] > self.RATE_WINDOW:
            self.hist.pop(0)
        t0, p0 = self.hist[0]
        rate = round((c["processed"] - p0) / (now - t0), 3) if now - t0 >= 1.0 else None
        return {"intervalSec": self.interval, "motionGate": self.motion_gate,
                "aiFps": rate, "aiFpsWindowSec": round(now - t0, 1),
                "framesSelected": c["selected"], "framesProcessed": c["processed"], "framesFailed": c["failed"],
                "skippedSampling": c["sampling"], "skippedNoMotion": c["motion"], "fallbackRuns": c["fallback"],
                "rejectedStale": c["stale"], "replacedQueue": c["replaced"],
                "lastFrameId": self.last["frameId"], "lastCapturedAt": self.last["capturedAt"],
                "lastFrameAgeMs": self.last["ageMs"], "lastInferenceMs": self.last["inferenceMs"],
                "lastReason": self.last["reason"]}, c

