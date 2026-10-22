"""Per-camera, per-module AI frame scheduling.

One ModuleSchedule per running module of a camera session. The camera session reads every frame (the live
view keeps its frame rate); for each frame it asks every module's schedule whether the AI should analyse it.

Modes (camera.detection.modules.<module>.schedule.mode):

    adaptive (default)  IDLE  - nothing happening: no AI; a tiny motion check (inside this module's detection
                                zones) about 5x per second; one AI "idle check" every idleCheckSec as a safety net
                        ACTIVE - motion in the zones, or an object the tracker has not confirmed yet, or (Human
                                Count) people present: EVERY frame is analysed with the original tracker-based
                                confirmation -> the same results as analysing every frame
                        ACTIVE -> IDLE after idleAfterSec without motion / unconfirmed objects / people
    interval            one analysed frame every intervalSec (sampled confirmation, see pipeline.process),
                        optional motion gate (Human Detection / Object Monitoring)
    every               every frame (the original behaviour)

Every frame is accounted for exactly once per module:
    selected / processed / failed     analysed
    idle        skipped while IDLE (AI call avoided because nothing happened)
    sampling    skipped by the interval            motion   interval due but no motion (AI call avoided)
    stale       rejected: older than the stale limit           replaced  older queued frames skipped (newest used)
"""
import time

MODES = ("adaptive", "interval", "every")


class ModuleSchedule:
    RATE_WINDOW = 30.0

    def __init__(self, module_id):
        self.module = module_id
        self.mode = "adaptive"
        self.interval = 0.0
        self.motion_gate = False
        self.hold = 10.0
        self.fallback = 30.0
        self.idle_check = 10.0
        self.idle_after = 10.0
        self.state = "active"            # adaptive: start ACTIVE so the scene is analysed right away
        self.state_since = time.time()
        self.keep_until = time.time() + 10.0
        self.last_motion = None          # last motion inside this module's zones (set by the session)
        self.last_run = None
        self.next_slot = 0.0
        self.c = {"selected": 0, "processed": 0, "sampling": 0, "motion": 0, "fallback": 0, "stale": 0,
                  "replaced": 0, "failed": 0, "idle": 0, "idleChecks": 0, "wakeMotion": 0, "wakeDetection": 0,
                  "toIdle": 0}
        self.active_sec = 0.0
        self.idle_sec = 0.0
        self.last = {"frameId": None, "capturedAt": None, "ageMs": None, "inferenceMs": None, "reason": None}
        self.hist = []

    def configure(self, sch, gate_allowed):
        sch = sch or {}
        mode = sch.get("mode") or "adaptive"
        self.mode = mode if mode in MODES else "adaptive"
        self.interval = max(0.0, float(sch.get("intervalSec", 0) or 0)) if self.mode == "interval" else 0.0
        self.motion_gate = bool(gate_allowed and sch.get("motionGate")) if self.mode == "interval" else False
        self.hold = float(sch.get("motionHoldSec", 10) or 0)
        self.fallback = max(1.0, float(sch.get("fallbackSec", 30) or 30))
        self.idle_check = max(1.0, float(sch.get("idleCheckSec", 10) or 10))
        self.idle_after = max(1.0, float(sch.get("idleAfterSec", 10) or 10))
        if self.mode != "interval":
            self.next_slot = 0.0

    @property
    def sampled(self):
        """True = single-frame (sampled) confirmation; only the fixed-interval mode."""
        return self.mode == "interval" and self.interval > 0

    @property
    def adaptive(self):
        return self.mode == "adaptive"

    # ------------------------------------------------------------------ signals from the session
    def motion_seen(self, now):
        """Motion inside this module's zones (from the session's motion check)."""
        self.last_motion = now
        if self.adaptive:
            self.keepalive(now, "motion")

    def keepalive(self, now, why="detection"):
        """Something needs following (motion, an unconfirmed object, people present): stay / become ACTIVE."""
        if not self.adaptive:
            return
        self.keep_until = max(self.keep_until, now + self.idle_after)
        if self.state != "active":
            self._set_state("active", now)
            self.c["wakeMotion" if why == "motion" else "wakeDetection"] += 1

    def _set_state(self, st, now):
        dt = now - self.state_since
        if self.state == "active":
            self.active_sec += dt
        else:
            self.idle_sec += dt
        self.state, self.state_since = st, now

    def motion_recent(self, now, hold):
        return self.last_motion is not None and now - self.last_motion <= hold

    # ------------------------------------------------------------------ per frame
    def decide(self, now, motion=None):
        """(run, reason) for the frame at time `now`. motion: optional callable(now) -> bool (interval mode)."""
        if self.mode == "every":
            return True, "every-frame"
        if self.adaptive:
            if self.state == "active":
                if now > self.keep_until:
                    self._set_state("idle", now)
                    self.c["toIdle"] += 1
                else:
                    return True, "active"
            if self.last_run is None or now - self.last_run >= self.idle_check:
                self.c["idleChecks"] += 1
                return True, "idle-check"
            self.c["idle"] += 1
            return False, None
        # fixed interval (+ optional motion gate)
        motion = motion or (lambda t: self.motion_recent(t, self.hold))
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

    def snapshot(self, prev_counts, el, now=None):
        """Metrics (cumulative counters + analyses per second measured over the last RATE_WINDOW s)."""
        now = time.time() if now is None else now
        c = dict(self.c)
        self.hist.append((now, c["processed"]))
        while len(self.hist) > 2 and now - self.hist[0][0] > self.RATE_WINDOW:
            self.hist.pop(0)
        t0, p0 = self.hist[0]
        rate = round((c["processed"] - p0) / (now - t0), 3) if now - t0 >= 1.0 else None
        cur = now - self.state_since
        act = self.active_sec + (cur if self.state == "active" else 0.0)
        idl = self.idle_sec + (cur if self.state == "idle" else 0.0)
        return {"mode": self.mode, "state": self.state if self.adaptive else None,
                "intervalSec": self.interval, "motionGate": self.motion_gate,
                "idleCheckSec": self.idle_check if self.adaptive else None,
                "idleAfterSec": self.idle_after if self.adaptive else None,
                "activeSharePct": round(100.0 * act / (act + idl), 1) if self.adaptive and act + idl > 0 else None,
                "aiFps": rate, "aiFpsWindowSec": round(now - t0, 1),
                "framesSelected": c["selected"], "framesProcessed": c["processed"], "framesFailed": c["failed"],
                "skippedIdle": c["idle"], "idleChecks": c["idleChecks"], "wakeByMotion": c["wakeMotion"],
                "wakeByDetection": c["wakeDetection"], "toIdle": c["toIdle"],
                "skippedSampling": c["sampling"], "skippedNoMotion": c["motion"], "fallbackRuns": c["fallback"],
                "rejectedStale": c["stale"], "replacedQueue": c["replaced"],
                "lastFrameId": self.last["frameId"], "lastCapturedAt": self.last["capturedAt"],
                "lastFrameAgeMs": self.last["ageMs"], "lastInferenceMs": self.last["inferenceMs"],
                "lastReason": self.last["reason"]}, c
