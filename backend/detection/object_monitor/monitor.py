"""
monitor.py (Object Monitoring Layer 1 - the standalone monitor's src/monitor/object_monitor.py, unchanged
            except the "remove one object" command for the web UI)
-----------------
LAYER 1 - watches every object the user marked and decides, from its
appearance only (never from class labels), whether it is IN PLACE, MOVED,
MISSING, or just COVERED.

Every frame:     trackers follow displaced objects (smooth box on screen).
Every N frames:  a full check of each object gives one piece of evidence:
  1. at home?          template score >= present_score, or 2+ quadrants in place
  2. moved nearby?     search around the tracker box and the home position
  3. covered?          surroundings changed, or a person box covers it -> OCCLUDED
  4. moved far away?   whole-frame search (template + ORB/SIFT), at most every 1 s
  5. otherwise         home score < lost_score -> MISSING, else UNCERTAIN (hold)
  Every match elsewhere must also pass the colour-histogram check.

Hysteresis turns evidence into alerts:
  * alert only after `confirm_checks` bad checks in a row that also lasted
    `min_moved_s` / `min_missing_s` (a person walking past never gets there)
  * clear only after `restore_checks` good checks (-> RESTORED message)
  * OCCLUDED pauses the count (up to `max_occlusion_s`, then it counts as missing)
  * `cooldown_s` between two alerts of the same kind for the same object
  * if the whole picture changes (lights off, IR night mode, camera bumped),
    all checks pause until it looks normal again (or the user presses R)
"""
from __future__ import annotations

import math
import threading
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np

from .common import Box, box_center, box_diag, expand_box, overlap_fraction
from .matcher import ObjectReference, pearson, to_gray
from .tracker import BoxTracker


class State(str, Enum):
    STABLE = "STABLE"          # in place
    CHECKING = "CHECKING"      # bad evidence, not confirmed yet
    OCCLUDED = "OCCLUDED"      # something is in front of it
    MOVED = "MOVED"            # alert: found somewhere else
    MISSING = "MISSING"        # alert: not found anywhere
    PAUSED = "PAUSED"          # whole picture changed


@dataclass
class Evidence:
    kind: str                  # present | moved | missing | occluded | uncertain | scene
    score: float = 0.0         # best match score
    box: Box | None = None     # where the object is now (if found)
    distance: float = 0.0      # centre shift from home, px
    method: str = ""


class MarkedObject:
    """One object the user drew a box around, with its hysteresis counters."""

    def __init__(self, oid: int, ref: ObjectReference, tracker_kind: str, name: str | None, now: float):
        self.id = oid
        self.ref = ref
        self.home: Box = ref.box
        self.name = name or f"object {oid}"
        self.named_by = "user" if name else ""
        self.state = State.STABLE
        self.current: Box = ref.box
        self.score = 1.0
        self.distance = 0.0
        self.last_evidence = "present"
        self.good = 0
        self.bad_kind: str | None = None
        self.bad_count = 0
        self.bad_since = now
        self.alert_kind: str | None = None
        self.last_alert: dict[str, float] = {}
        self.occluded_since: float | None = None
        self.stable_since = now
        self.last_refresh = now
        self.last_global = -1e9
        self.tracker = BoxTracker(tracker_kind)
        self.tracker_box: Box | None = None

    def view(self) -> dict:
        return {"id": self.id, "name": self.name, "named_by": self.named_by, "state": self.state.value,
                "home": self.home, "current": self.current, "score": self.score, "distance": self.distance,
                "alert": self.alert_kind, "tracker_box": self.tracker_box}


class ObjectMonitor:
    TRACK_WIDTH = 960       # trackers run on a copy this wide
    GLOBAL_EVERY_S = 1.0    # whole-frame search at most this often per object

    def __init__(self, cfg: dict, on_alert=None, on_info=None, clock=time.monotonic):
        self.cfg = cfg
        self.on_alert = on_alert or (lambda obj, kind, ev, frame: None)
        self.on_info = on_info or (lambda text: None)
        self.clock = clock
        self.objects: list[MarkedObject] = []
        self._lock = threading.Lock()
        self._commands: deque = deque()
        self._next_id = 1
        self.frame_count = 0
        self.scene_ref: np.ndarray | None = None
        self.scene_paused = False
        self.checks = 0
        self._outbox: list[tuple] = []  # alerts/messages, sent after the lock is released

    # -- commands (thread-safe; executed by the monitoring thread) --------------------
    def request_add(self, frame: np.ndarray, box: Box, name: str | None = None, callback=None) -> None:
        self._commands.append(("add", frame, box, name, callback))

    def request(self, command: str, *args) -> None:
        """'undo' (remove last object), 'clear' (remove all), 'reset' (accept current positions),
        'remove', id (one object - added for the web UI)."""
        self._commands.append((command,) + args)

    NAME_PRIORITY = {"": 0, "detector": 1, "dino": 2, "user": 3}

    def set_name(self, oid: int, name: str, source: str) -> bool:
        """Name from Layer 2 ('detector') or Layer 3 ('dino'); never overrides a better source
        (a name given with --mark by the user always wins)."""
        with self._lock:
            for obj in self.objects:
                if obj.id == oid and self.NAME_PRIORITY.get(source, 0) >= self.NAME_PRIORITY.get(obj.named_by, 0):
                    obj.name, obj.named_by = name, source
                    return True
        return False

    def view(self) -> list[dict]:
        with self._lock:
            return [o.view() for o in self.objects]

    # -- main entry point: call for every new frame ---------------------------------
    def process(self, frame: np.ndarray, people: list[Box] | None = None) -> None:
        self.frame_count += 1
        self._run_commands(frame)
        if not self.objects:
            return
        H, W = frame.shape[:2]
        scale = min(1.0, self.TRACK_WIDTH / W)
        small = None
        with self._lock:
            active = [o for o in self.objects if o.tracker.active]
        if active:
            small = cv2.resize(frame, (int(W * scale), int(H * scale))) if scale < 1 else frame
            for obj in active:
                obj.tracker_box = obj.tracker.update(small, scale)
        if self.frame_count % max(1, int(self.cfg["check_every_n_frames"])) == 0:
            self.check_all(frame, people or [], small, scale)

    def check_all(self, frame: np.ndarray, people: list[Box], small=None, scale: float = 1.0) -> None:
        now = self.clock()
        self.checks += 1
        scene_ok = self._scene_ok(frame)
        for obj in list(self.objects):
            ev = Evidence("scene") if not scene_ok else self._evidence(obj, frame, people, now)
            with self._lock:
                self._apply(obj, ev, frame, now)
            self._update_tracker(obj, ev, frame, small, scale)
        outbox, self._outbox = self._outbox, []
        for item in outbox:  # printing / saving snapshots happens outside the lock
            if item[0] == "alert":
                self.on_alert(*item[1:])
            else:
                self.on_info(item[1])

    # -- evidence ---------------------------------------------------------------------
    def _evidence(self, obj: MarkedObject, frame: np.ndarray, people: list[Box], now: float) -> Evidence:
        c = self.cfg
        ref = obj.ref
        W, H = ref.frame_size
        if frame.shape[1] != W or frame.shape[0] != H:
            return Evidence("scene")  # camera resolution changed
        home_cx, home_cy = box_center(obj.home)
        diag = box_diag(obj.home)

        def classify(cand) -> Evidence | None:
            if cand is None or cand.score < c["found_score"] or ref.hist_similarity(frame, cand.box) < c["hist_min"]:
                return None
            cx, cy = box_center(cand.box)
            dist = math.hypot(cx - home_cx, cy - home_cy)
            moved = dist > c["move_threshold_px"] and dist > diag * c["move_threshold_pct"] / 100.0
            return Evidence("moved" if moved else "present", cand.score, cand.box, dist, cand.method)

        # 1. at home (or partly covered but still there)?
        score, (dx, dy), parts = ref.home_score(frame)
        if score >= c["present_score"] or (parts >= 2 and score >= c["lost_score"]):
            x, y, w, h = obj.home
            return Evidence("present", score, (int(x + dx), int(y + dy), w, h), math.hypot(dx, dy), "home")
        # 2. moved nearby? (tracker box first, then around the home position)
        windows = []
        if obj.tracker_box is not None:
            windows.append(expand_box(obj.tracker_box, 2.5, W, H, min_pad=10))
        windows.append(expand_box(obj.home, 3.0, W, H, min_pad=10))
        for win in windows:
            ev = classify(ref.search(frame, win))
            if ev is not None:
                return ev
        # 3. covered by something (a person) rather than gone?
        covered = any(overlap_fraction(obj.home, p) >= 0.3 for p in people)
        if covered or ref.ring_similarity(frame) < c["occlusion_ring_min"]:
            return Evidence("occluded", score)
        # 4. moved far away? (whole frame, rate-limited)
        if now - obj.last_global >= self.GLOBAL_EVERY_S:
            obj.last_global = now
            full = (0, 0, W, H)
            extra = 0.5 if min(ref.template.shape) * 0.5 >= 16 and max(W, H) > 1000 else 1.0
            for cand in (ref.search(frame, full, extra), ref.feature_search(frame, full)):
                ev = classify(cand)
                if ev is not None and ev.kind == "moved":
                    return ev
        # 5. not found anywhere
        return Evidence("missing" if score < c["lost_score"] else "uncertain", score)

    # -- hysteresis state machine ----------------------------------------------------------
    def _apply(self, obj: MarkedObject, ev: Evidence, frame: np.ndarray, now: float) -> None:
        c = self.cfg
        obj.last_evidence = ev.kind
        if ev.kind == "scene":
            obj.state = State.PAUSED
            return
        if obj.state == State.PAUSED:  # picture is back: resume from the alert state (or re-check)
            obj.state = self._alert_state(obj) or State.CHECKING
        obj.score = ev.score
        if ev.kind == "present":
            obj.good += 1
            obj.bad_kind, obj.bad_count, obj.occluded_since = None, 0, None
            obj.current, obj.distance = ev.box or obj.home, ev.distance
            if obj.alert_kind and obj.good >= c["restore_checks"]:
                self._outbox.append(("info", f"RESTORED | #{obj.id} '{obj.name}' is back in place"))
                obj.alert_kind = None
            if not obj.alert_kind:
                if obj.state != State.STABLE:
                    obj.stable_since = now
                obj.state = State.STABLE
                self._maybe_refresh(obj, ev, frame, now)
            return
        obj.good = 0
        obj.stable_since = now
        if ev.kind == "uncertain":
            return  # inside the hysteresis band: keep the current state and counters
        if ev.kind == "occluded":
            obj.occluded_since = obj.occluded_since if obj.occluded_since is not None else now
            if now - obj.occluded_since < c["max_occlusion_s"]:
                if not obj.alert_kind:
                    obj.state = State.OCCLUDED
                return
            kind = "missing"  # covered for too long
        else:
            obj.occluded_since = None
            kind = ev.kind
        if ev.box is not None:
            obj.current, obj.distance = ev.box, ev.distance
        if obj.bad_kind != kind:
            obj.bad_kind, obj.bad_count, obj.bad_since = kind, 1, now
        else:
            obj.bad_count += 1
        if obj.alert_kind == kind:
            obj.state = self._alert_state(obj)  # already alerted for this
            return
        min_s = c["min_moved_s"] if kind == "moved" else c["min_missing_s"]
        if obj.bad_count >= c["confirm_checks"] and now - obj.bad_since >= min_s:
            obj.alert_kind = kind
            obj.state = State.MOVED if kind == "moved" else State.MISSING
            if now - obj.last_alert.get(kind, -1e9) >= c["cooldown_s"]:
                obj.last_alert[kind] = now
                ev.distance = ev.distance if kind == "moved" else 0.0
                self._outbox.append(("alert", obj.view() | {"since_s": now - obj.bad_since}, kind, ev, frame))
            else:
                self._outbox.append(("info", f"{kind.upper()} again | #{obj.id} '{obj.name}' "
                                             f"(alert suppressed: cooldown)"))
        elif not obj.alert_kind:
            obj.state = State.CHECKING

    @staticmethod
    def _alert_state(obj: MarkedObject) -> State | None:
        return {"moved": State.MOVED, "missing": State.MISSING}.get(obj.alert_kind or "")

    def _maybe_refresh(self, obj: MarkedObject, ev: Evidence, frame: np.ndarray, now: float) -> None:
        """While confidently in place, keep a fresh second reference (slow daylight changes)."""
        refresh_s = self.cfg["reference_refresh_s"]
        if ev.score >= 0.75 and now - obj.stable_since >= refresh_s and now - obj.last_refresh >= refresh_s:
            obj.ref.refresh(frame, ev.box or obj.home)
            obj.last_refresh = now
            if all(o.state == State.STABLE for o in self.objects):
                self.scene_ref = self._thumb(frame)

    def _update_tracker(self, obj: MarkedObject, ev: Evidence, frame, small, scale: float) -> None:
        """Trackers only run while an object is away from home (saves CPU)."""
        if ev.kind == "moved" and ev.box is not None:
            if small is None:
                H, W = frame.shape[:2]
                scale = min(1.0, self.TRACK_WIDTH / W)
                small = cv2.resize(frame, (int(W * scale), int(H * scale))) if scale < 1 else frame
            obj.tracker.start(small, scale, ev.box)
            obj.tracker_box = ev.box
        elif ev.kind in ("present", "missing"):
            obj.tracker.stop()
            obj.tracker_box = None

    # -- whole-picture change guard ----------------------------------------------------------
    @staticmethod
    def _thumb(frame: np.ndarray) -> np.ndarray:
        return cv2.resize(to_gray(frame), (64, 36), interpolation=cv2.INTER_AREA)

    def _scene_ok(self, frame: np.ndarray) -> bool:
        thumb = self._thumb(frame)
        if self.scene_ref is None or self.scene_ref.shape != thumb.shape:
            self.scene_ref = thumb
            return True
        ok = pearson(thumb, self.scene_ref) >= self.cfg["scene_change_min"]
        if ok == self.scene_paused:  # state change -> tell the user once
            self.scene_paused = not ok
            self.on_info("Whole picture changed (lights / IR mode / camera moved?) - checks PAUSED. "
                         "Press R to accept the new view." if not ok else "Picture back to normal - checks resumed")
        return ok

    # -- commands ------------------------------------------------------------------------------
    def _run_commands(self, frame: np.ndarray) -> None:
        while self._commands:
            cmd = self._commands.popleft()
            now = self.clock()
            if cmd[0] == "add":
                _, cframe, box, name, callback = cmd
                ref = ObjectReference(cframe, box, int(self.cfg["min_side_px"]), str(self.cfg["features"]))
                obj = MarkedObject(self._next_id, ref, str(self.cfg["tracker"]), name, now)
                self._next_id += 1
                with self._lock:
                    self.objects.append(obj)
                if self.scene_ref is None:
                    self.scene_ref = self._thumb(cframe)
                x, y, w, h = obj.home
                feats = len(ref.kp) if ref.kp is not None else 0
                self.on_info(f"MARKED | #{obj.id} '{obj.name}' at ({x},{y}) {w}x{h} px | "
                             f"{feats} {ref.feature_kind.upper()} keypoints{' | plain object' if ref.flat else ''}")
                if callback:
                    callback(obj)
            elif cmd[0] == "remove":   # (shared backend) remove one object by id - the web UI's delete button
                with self._lock:
                    gone = [o for o in self.objects if o.id == cmd[1]]
                    self.objects = [o for o in self.objects if o.id != cmd[1]]
                for obj in gone:
                    self.on_info(f"REMOVED | #{obj.id} '{obj.name}'")
            elif cmd[0] == "undo" and self.objects:
                with self._lock:
                    obj = self.objects.pop()
                self.on_info(f"REMOVED | #{obj.id} '{obj.name}'")
            elif cmd[0] == "clear":
                with self._lock:
                    n, self.objects = len(self.objects), []
                self.on_info(f"CLEARED | {n} object(s) - draw new boxes")
            elif cmd[0] == "reset":
                renewed, kept = [], 0
                for o in list(self.objects):  # new reference = where each object is now
                    if o.state == State.MISSING:  # nothing to take a picture of: keep the old reference
                        renewed.append(o)
                        kept += 1
                        continue
                    ref = ObjectReference(frame, o.current, int(self.cfg["min_side_px"]), str(self.cfg["features"]))
                    new = MarkedObject(o.id, ref, str(self.cfg["tracker"]), None, now)
                    new.name, new.named_by = o.name, o.named_by
                    renewed.append(new)
                with self._lock:
                    self.objects = renewed
                self.scene_ref = self._thumb(frame)
                self.scene_paused = False
                self.on_info(f"RE-REFERENCED | {len(renewed) - kept} object(s): current positions are the new home"
                             + (f" ({kept} missing object(s) unchanged)" if kept else ""))
