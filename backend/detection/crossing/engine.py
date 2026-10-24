"""Line-crossing engine for one camera session: modules `twhelmet` (two-wheeler helmet check at a line) and
`pcross` (person line crossing and counting). One worker thread, one COCO model with its own ByteTrack instance
(the project's detector: RKNN NPU / OpenVINO / PyTorch as configured), one tracked pass per frame for both modules.

Frame flow (bounded, never accumulates):
    session loop --submit(frame, captured_at, frame_id)--> ONE slot holding the newest frame
    * at most one submit per 1/targetFps (default 10 fps); frames in between are counted as `skippedInterval`
    * a frame still waiting when a newer one arrives is replaced (`droppedReplaced`)
    * a frame older than `maxAgeSec` when the worker picks it up is not analysed (`droppedStale`)
Every crossing event carries the id and capture time of the frame that showed the object past the line, and its
snapshot is that very frame (annotated copy); the clean frame is kept as the original.
"""
import logging
import math
import threading
import time
from collections import deque
from datetime import datetime

import cv2
import numpy as np

from backend import config
from backend.detection.crossing import judge as helmet_judge
from backend.detection.crossing.geometry import CrossingTracker, Line, IN, OUT
from backend.runtime import gated, name_thread

log = logging.getLogger("aivision.crossing")

CROSS_MODULES = ("twhelmet", "pcross")
COCO = {"person": 0, "bicycle": 1, "motorcycle": 3}
COLORS = {"twhelmet": (255, 170, 0), "pcross": (0, 200, 255)}          # BGR line colours (blue-ish / amber)
VERDICT_BGR = {helmet_judge.HELMET: (60, 170, 60), helmet_judge.MISSING: (40, 40, 230), helmet_judge.UNCERTAIN: (0, 191, 255)}
FLASH_SEC = 1.5
MAX_AGE_SEC = 1.0
RECENT_KEEP = 60


def now_iso(ts=None):
    return datetime.fromtimestamp(ts or time.time()).astimezone().isoformat(timespec="milliseconds")


def on_two_wheeler(pbox, vbox):
    from backend.detection.ppe.engine import on_two_wheeler as f
    return f(pbox, [vbox])


def overlap_score(pbox, vbox):
    ix = max(0.0, min(pbox[2], vbox[2]) - max(pbox[0], vbox[0]))
    iy = max(0.0, min(pbox[3], vbox[3]) - max(pbox[1], vbox[1]))
    return ix * iy / max(1.0, (pbox[2] - pbox[0]) * (pbox[3] - pbox[1]))


def associate_riders(persons, vehicles, max_riders=3):
    """{vehicle index: [person index, ...]}: each person belongs to the ONE two-wheeler it sits on best."""
    out = {}
    for pi, p in enumerate(persons):
        best, best_s = None, 0.0
        for vi, v in enumerate(vehicles):
            if on_two_wheeler(p["box"], v["box"]):
                s = overlap_score(p["box"], v["box"])
                if s > best_s:
                    best, best_s = vi, s
        if best is not None:
            out.setdefault(best, []).append((best_s, pi))
    return {vi: [pi for _s, pi in sorted(lst, reverse=True)[:max_riders]] for vi, lst in out.items()}


def load_model():
    """The project's detector for this engine (own instance = own tracker). Same engine selection / fallback as the
    person+vehicle pipeline."""
    from ultralytics import YOLO
    from backend.detection import pipeline
    from backend.detection.tracker import install
    install()
    engine, path = config.resolve_engine()
    while True:
        try:
            imgsz = config.IMG_SIZE
            if engine == "rknn":
                imgsz, _p = pipeline.load_rknn_prereqs(path)
            model = YOLO(str(path), task="detect")
            if engine == "rknn":                      # load now so errors surface here and the engine can fall back
                model.predict(np.zeros((imgsz * 9 // 16, imgsz, 3), np.uint8), imgsz=imgsz, verbose=False)
            return model, engine, imgsz, path
        except Exception as exc:
            if engine == "pytorch":
                raise
            nxt, path = config.fallback_engine(engine)
            log.warning("%s could not be loaded (%s) - using %s", config.engine_label(engine), exc, config.engine_label(nxt))
            engine = nxt


class _JudgeWorker:
    """Runs the helmet judge on its own thread so the tracked 10 fps analysis never waits for it.
    Evidence jobs (frames while a two-wheeler approaches the line) are bounded and droppable; finalising a crossing is
    never dropped and runs first."""
    MAX_EVIDENCE = 6

    def __init__(self, name):
        self.cond = threading.Condition()
        self.evidence, self.final = deque(), deque()
        self.stats = {"judged": 0, "skippedBusy": 0, "finalised": 0, "ms": deque(maxlen=100), "errors": 0}
        self._stop = False
        self.thread = threading.Thread(target=self._run, name=(name or "cross") + "-judge", daemon=True)
        self.thread.start()

    def submit_evidence(self, fn):
        with self.cond:
            if len(self.evidence) >= self.MAX_EVIDENCE:
                self.stats["skippedBusy"] += 1
                return False
            self.evidence.append(fn)
            self.cond.notify()
        return True

    def submit_final(self, fn):
        with self.cond:
            self.final.append(fn)
            self.cond.notify()

    def close(self):
        with self.cond:
            self._stop = True
            self.cond.notify_all()

    def pending(self):
        return len(self.evidence) + len(self.final)

    def _run(self):
        while True:
            with self.cond:
                while not self._stop and not self.final and not self.evidence:
                    self.cond.wait(0.5)
                if self._stop:
                    return
                fn, is_final = (self.final.popleft(), True) if self.final else (self.evidence.popleft(), False)
            t0 = time.perf_counter()
            try:
                fn()
                self.stats["finalised" if is_final else "judged"] += 1
            except Exception:
                self.stats["errors"] += 1
                log.exception("helmet judge job failed")
            self.stats["ms"].append((time.perf_counter() - t0) * 1000.0)


class _Module:
    """State of one active module (line, tracker, counters, history)."""

    def __init__(self, mid, cfg):
        self.mid, self.cfg, self.paused = mid, cfg, False
        self.tracker, self.tracker_key = None, None
        self.started = time.time()
        self.lock = threading.Lock()
        self.counts = {"in": 0, "out": 0} if mid == "pcross" else {"compliant": 0, "missing": 0, "unverified": 0}
        self.crossings = 0
        self.recent = deque(maxlen=RECENT_KEEP)
        self.minute = {}                            # epoch minute -> {"in": n, "out": n}
        self.flash = []                             # (t, tid, direction/verdict) for the live overlay
        self.ignored = 0
        self.last_event_at = None
        self.riders = {}                            # twhelmet: vehicle tid -> {person tid -> RiderHistory}
        self.last_boxes = []
        self.warnings = []

    def stats_windows(self, now):
        m = int(now // 60)
        out = {}
        for name, span in (("last1Min", 1), ("last5Min", 5), ("last60Min", 60)):
            agg = {"in": 0, "out": 0}
            for k, v in self.minute.items():
                if m - k < span:
                    agg["in"] += v["in"]
                    agg["out"] += v["out"]
            out[name] = agg
        return out


class CrossingEngine:
    def __init__(self, on_event, camera_id="", thread_name=None, target_fps=10.0):
        self.on_event, self.camera_id, self.thread_name = on_event, camera_id, thread_name
        self.model, self.engine, self.imgsz, self.model_path = load_model()
        self.names = {v: k for k, v in COCO.items()}
        self.mods = {}                              # module id -> _Module
        self.target_fps = float(target_fps)
        self.lock = threading.Condition()
        self.job, self.busy = None, False
        self._stop = threading.Event()
        self._last_submit = 0.0
        self.frame_shape = None
        self.result = None                          # drawing data of the last analysed frame
        self.m = {"submitted": 0, "processed": 0, "droppedReplaced": 0, "droppedStale": 0, "skippedInterval": 0, "errors": 0,
                  "lastError": None, "lastFrameId": None, "lastCapturedAt": None}
        self.done_times = deque(maxlen=200)         # wall-clock end of each analysis (inference fps)
        self.infer_ms = deque(maxlen=200)           # model time per frame
        self.latency_ms = deque(maxlen=200)         # capture -> result (what the user waits)
        self.stage_ms = {}
        self.started = time.time()
        self.judger = _JudgeWorker(thread_name)
        self.worker = threading.Thread(target=self._run, name=thread_name or "cross-worker", daemon=True)
        self.worker.start()

    # ------------------------------------------------------------ config
    def set_module(self, mid, cfg, paused=False):
        with self.lock:
            mod = self.mods.get(mid)
            if mod is None:
                mod = self.mods[mid] = _Module(mid, cfg)
            mod.cfg, mod.paused = cfg, bool(paused)
            self.target_fps = max(1.0, min(30.0, float(max(m.cfg.get("targetFps", 10.0) for m in self.mods.values()))))

    def remove_module(self, mid):
        with self.lock:
            self.mods.pop(mid, None)
            return len(self.mods)

    def reset(self, mid):
        with self.lock:
            old = self.mods.get(mid)
            if old:
                new = _Module(mid, old.cfg)
                new.paused = old.paused
                self.mods[mid] = new

    def close(self):
        self._stop.set()
        self.judger.close()
        with self.lock:
            self.lock.notify_all()

    # ------------------------------------------------------------ frames
    def submit(self, frame, captured_at, frame_id):
        """Newest frame wins. Returns True when the frame was taken for analysis."""
        now = time.monotonic()
        if now - self._last_submit < 1.0 / self.target_fps - 0.004:
            self.m["skippedInterval"] += 1
            return False
        with self.lock:
            if self.job is not None:
                self.m["droppedReplaced"] += 1      # the older waiting frame is gone; only the fresh one is analysed
            self.job = (frame, captured_at or time.time(), frame_id)
            self._last_submit = now
            self.m["submitted"] += 1
            self.lock.notify()
        return True

    def _run(self):
        if self.thread_name:
            name_thread(self.thread_name)
        while not self._stop.is_set():
            with self.lock:
                while self.job is None and not self._stop.is_set():
                    self.lock.wait(0.5)
                if self._stop.is_set():
                    return
                (frame, captured_at, fid), self.job = self.job, None
                self.busy = True
            try:
                if time.time() - captured_at > MAX_AGE_SEC:
                    self.m["droppedStale"] += 1
                    continue
                self.process(frame, captured_at, fid)
            except Exception as exc:
                self.m["errors"] += 1
                self.m["lastError"] = repr(exc)
                log.exception("line crossing analysis failed")
            finally:
                self.busy = False

    # ------------------------------------------------------------ analysis
    def _detect(self, frame, mods):
        classes, conf = set(), 1.0
        for m in mods:
            conf = min(conf, float(m.cfg.get("confidence", 0.35)))
            if m.mid == "pcross":
                classes.add(COCO["person"])
                if m.cfg.get("excludeRiders", True):
                    classes |= {COCO["motorcycle"], COCO["bicycle"]}
            else:
                classes |= {COCO["person"], COCO["motorcycle"]}
                if m.cfg.get("includeBicycles"):
                    classes.add(COCO["bicycle"])
        kw = dict(imgsz=self.imgsz, conf=conf, classes=sorted(classes), verbose=False)
        if config.NMS_IOU is not None:
            kw["iou"] = config.NMS_IOU
        t0 = time.perf_counter()
        with gated():
            r = self.model.track(frame, persist=True, tracker=str(config.TRACKER_CONFIG), **kw)[0]
        ms = (time.perf_counter() - t0) * 1000.0
        dets = []
        if r.boxes is not None:
            for b in r.boxes:
                dets.append({"tid": int(b.id) if b.id is not None else None, "cls": self.names.get(int(b.cls), "?"),
                             "box": [float(v) for v in b.xyxy[0].tolist()], "conf": float(b.conf)})
        return dets, ms

    def _tracker_for(self, mod, w, h):
        c = mod.cfg
        line = Line.from_cfg(c)
        key = (w, h, None if line is None else (line.x1, line.y1, line.x2, line.y2), c.get("refPoint", "bottom"),
               c.get("minTrackFrames", 3), c.get("hysteresis", 0.05), c.get("recrossSec", 2.0), c.get("handoverSec", 1.0), c.get("dedupeSec", 1.0))
        if mod.tracker_key != key:
            keep = mod.tracker.stats if mod.tracker is not None and mod.tracker_key and mod.tracker_key[0:2] == key[0:2] else None
            mod.tracker = CrossingTracker(line, w, h, ref=c.get("refPoint", "bottom"), min_frames=c.get("minTrackFrames", 3),
                                          hysteresis=c.get("hysteresis", 0.05), recross_sec=c.get("recrossSec", 2.0),
                                          handover_sec=c.get("handoverSec", 1.0), dedupe_sec=c.get("dedupeSec", 1.0)) if line else None
            if keep is not None and mod.tracker is not None:
                mod.tracker.stats.update(keep)       # a line edit keeps the reliability numbers of the session
            mod.tracker_key = key
            mod.warnings = []
            if line is not None and line.aspect_mismatch(w, h):
                mod.warnings.append(f"The line was drawn on {line.ref_w}x{line.ref_h}; this stream is {w}x{h} (different aspect): check the line")
        return mod.tracker

    def process(self, frame, captured_at, fid):
        t_start = time.perf_counter()
        h, w = frame.shape[:2]
        self.frame_shape = (h, w)
        with self.lock:
            mods = [m for m in self.mods.values() if not m.paused]
        if not mods:
            return
        dets, infer = self._detect(frame, mods)
        persons = [d for d in dets if d["cls"] == "person"]
        bikes = [d for d in dets if d["cls"] in ("motorcycle", "bicycle")]
        riders = associate_riders(persons, bikes)
        rider_idx = {pi for lst in riders.values() for pi in lst}
        draw = {}
        for mod in mods:
            tr = self._tracker_for(mod, w, h)
            if tr is None:
                mod.warnings = ["No line drawn: draw it on the live picture"]
                draw[mod.mid] = {"boxes": []}
                continue
            if mod.mid == "pcross":
                draw[mod.mid] = self._pcross(mod, tr, frame, persons, rider_idx, captured_at, fid, w, h)
            else:
                draw[mod.mid] = self._twhelmet(mod, tr, frame, persons, bikes, riders, captured_at, fid, w, h)
        done = time.time()
        self.result = {"shape": (h, w), "t": done, "mods": draw}
        total = (time.perf_counter() - t_start) * 1000.0
        self.m.update(processed=self.m["processed"] + 1, lastFrameId=fid, lastCapturedAt=captured_at)
        self.done_times.append(done)
        self.infer_ms.append(infer)
        self.latency_ms.append((done - captured_at) * 1000.0)
        self.stage_ms = {"inference": round(infer, 1), "rules": round(total - infer, 1)}

    # ---- person line crossing & counting
    def _pcross(self, mod, tr, frame, persons, rider_idx, captured_at, fid, w, h):
        c = mod.cfg
        min_h = float(c.get("minBoxFrac", 0.04)) * h
        conf = float(c.get("confidence", 0.35))
        use = []
        for i, p in enumerate(persons):
            if p["conf"] < conf or p["box"][3] - p["box"][1] < min_h:
                continue
            if c.get("excludeRiders", True) and i in rider_idx:
                continue
            use.append(p)
        events = tr.update(use, captured_at, fid)
        boxes = [(p["box"], f"P{p['tid']}" if p["tid"] is not None else "P?", (0, 200, 255), p["tid"]) for p in use]
        mod.last_boxes = boxes
        direction = c.get("direction", "both")
        for ev in events:
            if direction != "both" and ev["direction"] != direction:
                mod.ignored += 1
                continue
            mod.counts[ev["direction"]] += 1
            mod.crossings += 1
            self._record(mod, ev, captured_at)
            ev.update(module="pcross", label="Person IN" if ev["direction"] == IN else "Person OUT",
                      counts=dict(mod.counts), capturedAt=captured_at, frameShape=(h, w))
            snap = self._snapshot(frame, mod, boxes, ev, (h, w), f"Person {ev['direction'].upper()}  P{ev['tid']}")
            self._emit(mod, ev, snap, frame)
        return {"boxes": boxes, "tracker": tr}

    def _record(self, mod, ev, captured_at, flash=True):
        mod.last_event_at = captured_at
        if flash:
            mod.flash.append((captured_at, ev["tid"], ev["direction"]))
            mod.flash = mod.flash[-8:]
        if mod.mid == "pcross":
            b = mod.minute.setdefault(int(captured_at // 60), {"in": 0, "out": 0})
            b[ev["direction"]] += 1
            for k in [k for k in mod.minute if int(captured_at // 60) - k > 70]:
                mod.minute.pop(k, None)
        mod.recent.appendleft({"at": now_iso(captured_at), "trackId": ev["tid"], "direction": ev["direction"], "frameId": ev["frameId"],
                               "verdict": ev.get("verdict"), "label": ev.get("label")})

    def _emit(self, mod, ev, snap, frame):
        try:
            self.on_event(mod.mid, ev, snap, frame)
        except Exception:
            log.exception("crossing event handler failed")

    # ---- two-wheeler helmet at the line
    def _twhelmet(self, mod, tr, frame, persons, bikes, riders, captured_at, fid, w, h):
        c = mod.cfg
        conf = float(c.get("confidence", 0.35))
        use_idx = [i for i, b in enumerate(bikes) if b["conf"] >= conf and (b["cls"] == "motorcycle" or c.get("includeBicycles"))
                   and b["box"][3] - b["box"][1] >= float(c.get("minBoxFrac", 0.05)) * h]
        use = [bikes[i] for i in use_idx]
        line = Line.from_cfg(c)
        hmin, nmin = float(c.get("helmetMinConf", 0.5)), float(c.get("noHelmetMinConf", 0.6))
        every = max(1, int(c.get("judgeEvery", 2)))
        budget = int(c.get("maxJudgePerFrame", 2))
        # 1. evidence while a two-wheeler approaches / passes the line: head crops go to the judge thread (bounded, droppable)
        for vi in use_idx:
            v = bikes[vi]
            if v["tid"] is None:
                continue
            near = abs(line.signed(((v["box"][0] + v["box"][2]) / 2, v["box"][3]), w, h)) <= max(2.0 * (v["box"][3] - v["box"][1]), 0.25 * h)
            hist = mod.riders.setdefault(v["tid"], {})
            for pi in riders.get(vi, []):
                p = persons[pi]
                rh = hist.setdefault(p["tid"] if p["tid"] is not None else f"slot{pi}", helmet_judge.RiderHistory())
                if near and budget > 0 and (fid or 0) % every == 0:
                    top = helmet_judge.head_crop(frame, p["box"])
                    if top is not None:
                        self.judger.submit_evidence(lambda top=top, rh=rh: rh.add(helmet_judge.judge_crop(top, hmin, nmin)))
                        budget -= 1
        events = tr.update(use, captured_at, fid)
        boxes = []
        for vi in use_idx:
            v = bikes[vi]
            boxes.append((v["box"], f"{v['cls']} {v['tid']}" if v["tid"] is not None else v["cls"], (255, 170, 0), v["tid"]))
            for pi in riders.get(vi, []):
                boxes.append((persons[pi]["box"], "rider", (200, 120, 40), None))
        mod.last_boxes = boxes
        direction = c.get("direction", "both")
        for ev in events:
            if direction != "both" and ev["direction"] != direction:
                mod.ignored += 1
                continue
            vi = next((i for i in use_idx if bikes[i]["tid"] == ev["tid"]), None)
            hist = mod.riders.setdefault(ev["tid"], {})
            jobs = []
            for pi in (riders.get(vi, []) if vi is not None else [])[:3]:
                p = persons[pi]
                key = p["tid"] if p["tid"] is not None else f"slot{pi}"
                jobs.append((hist.setdefault(key, helmet_judge.RiderHistory()), p["tid"], list(p["box"]), helmet_judge.head_crop(frame, p["box"])))
            mod.flash.append((captured_at, ev["tid"], ev["direction"]))        # the box turns red at once; the verdict follows
            mod.flash = mod.flash[-8:]
            ev.update(module="twhelmet", capturedAt=captured_at, frameShape=(h, w))
            self.judger.submit_final(lambda ev=ev, jobs=jobs, boxes=list(boxes), frame=frame, mod=mod:
                                     self._finalise_twhelmet(mod, ev, jobs, boxes, frame, (h, w), hmin, nmin))
        for tid in [t for t in mod.riders if t not in {b["tid"] for b in bikes} and tr.tracks.get(t) is None]:
            mod.riders.pop(tid, None)               # forget histories of two-wheelers the tracker has dropped
        return {"boxes": boxes, "tracker": tr}

    def _finalise_twhelmet(self, mod, ev, jobs, boxes, frame, shape, hmin, nmin):
        """Judge thread: the crossing frame's own evidence + the votes of the approach -> verdict -> event."""
        c = mod.cfg
        rl = []
        for rh, ptid, pbox, top in jobs:
            if top is not None:
                rh.add(helmet_judge.judge_crop(top, hmin, nmin))              # always judge the crossing frame itself
            verdict = rh.verdict(min_missing=int(c.get("minMissingVotes", 3)), min_helmet=int(c.get("minHelmetVotes", 2)))
            last = rh.last or {}
            rl.append({"personId": ptid, "box": [round(v_, 1) for v_ in pbox], "verdict": verdict, "votes": list(rh.votes),
                       "helmetConf": last.get("helmetConf"), "noHelmetConf": last.get("noHelmetConf"), "reason": last.get("reason"),
                       "source": last.get("source")})
        if not rl:
            verdict, why = helmet_judge.UNCERTAIN, "no rider found on the two-wheeler"
        else:
            vs = [r["verdict"] for r in rl]
            if helmet_judge.MISSING in vs:
                verdict, why = helmet_judge.MISSING, "rider without helmet"
            elif all(x == helmet_judge.HELMET for x in vs):
                verdict, why = helmet_judge.HELMET, "helmet on every rider"
            else:
                verdict, why = helmet_judge.UNCERTAIN, "helmet could not be verified"
        key = {helmet_judge.HELMET: "compliant", helmet_judge.MISSING: "missing"}.get(verdict, "unverified")
        label = {"compliant": "Helmet compliant", "missing": "Helmet missing", "unverified": "Helmet unverified"}[key]
        with mod.lock:
            mod.counts[key] += 1
            mod.crossings += 1
            ev.update(label=label, verdict=verdict, why=why, riders=rl, counts=dict(mod.counts), judge=helmet_judge.status())
            self._record(mod, ev, ev["capturedAt"], flash=False)
        snap = self._snapshot(frame, mod, boxes, ev, shape, f"{label.upper()}  {ev['cls']} {ev['tid']}  {ev['direction'].upper()}",
                              color=VERDICT_BGR[verdict])
        self._emit(mod, ev, snap, frame)

    # ------------------------------------------------------------ drawing
    def _draw_line(self, img, mod, w, h, thick=3):
        line = Line.from_cfg(mod.cfg)
        if line is None:
            return
        col = COLORS[mod.mid]
        ax, ay, bx, by = (int(v) for v in line.px(w, h))
        cv2.line(img, (ax, ay), (bx, by), col, thick, cv2.LINE_AA)
        mx, my = (ax + bx) // 2, (ay + by) // 2
        nx, ny = line.arrow_normal(w, h)
        L = max(18, int(0.05 * min(w, h)))
        d = mod.cfg.get("direction", "both")
        for sign, name in ((1, "IN"), (-1, "OUT")):
            if d not in ("both", name.lower()):
                continue
            tip = (int(mx + sign * nx * L * 1.6), int(my + sign * ny * L * 1.6))
            cv2.arrowedLine(img, (mx, my), tip, col, max(2, thick - 1), cv2.LINE_AA, tipLength=0.35)
            cv2.putText(img, name, (tip[0] + 4, tip[1] + 4), cv2.FONT_HERSHEY_SIMPLEX, 0.5 * max(1.0, w / 1280), col, 1, cv2.LINE_AA)
        cv2.circle(img, (ax, ay), 5, col, -1, cv2.LINE_AA)
        cv2.circle(img, (bx, by), 5, col, -1, cv2.LINE_AA)
        cv2.putText(img, "A", (ax + 6, ay - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
        cv2.putText(img, "B", (bx + 6, by - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)

    def _snapshot(self, frame, mod, boxes, ev, shape, title, color=None):
        img = frame.copy()
        h, w = shape
        self._draw_line(img, mod, w, h)
        for box, label, col, tid in boxes:
            hit = tid is not None and tid == ev["tid"]
            x1, y1, x2, y2 = (int(v) for v in box)
            cv2.rectangle(img, (x1, y1), (x2, y2), color if (hit and color) else col, 4 if hit else 1)
            cv2.putText(img, label, (x1, max(12, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, col, 1, cv2.LINE_AA)
        px, py = (int(v) for v in ev["point"])
        cv2.circle(img, (px, py), 7, (0, 0, 255), -1, cv2.LINE_AA)
        stamp = datetime.fromtimestamp(ev["capturedAt"]).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        text = f"{title}   frame {ev['frameId']}   {stamp}"
        scale = max(0.5, w / 1700)
        (tw, th), _b = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 2)
        cv2.rectangle(img, (0, 0), (min(w, tw + 20), th + 18), (0, 0, 0), -1)
        cv2.putText(img, text, (10, th + 8), cv2.FONT_HERSHEY_SIMPLEX, scale, color or COLORS[mod.mid], 2, cv2.LINE_AA)
        return img

    def draw(self, annotated):
        res = self.result
        if res is None or time.time() - res["t"] > 3.0:
            res = None
        ah, aw = annotated.shape[:2]
        for mid, mod in list(self.mods.items()):
            self._draw_line(annotated, mod, aw, ah, thick=2)
        if res is None:
            return annotated
        h, w = res["shape"]
        sx, sy = aw / w, ah / h
        now = time.time()
        for mid, d in res["mods"].items():
            mod = self.mods.get(mid)
            if mod is None:
                continue
            flashing = {tid for t, tid, _d in mod.flash if now - t <= FLASH_SEC}
            for box, label, col, tid in d.get("boxes", []):
                x1, y1, x2, y2 = int(box[0] * sx), int(box[1] * sy), int(box[2] * sx), int(box[3] * sy)
                hot = tid is not None and tid in flashing
                cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255) if hot else col, 3 if hot else 1)
                cv2.putText(annotated, label, (x1, max(12, y1 - 4)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1, cv2.LINE_AA)
        return annotated

    # ------------------------------------------------------------ status
    def metrics(self):
        now = time.time()
        ts = [t for t in self.done_times if now - t <= 5.0]
        fps = (len(ts) - 1) / (ts[-1] - ts[0]) if len(ts) >= 2 and ts[-1] > ts[0] else (0.0 if not ts else None)
        inf = sorted(self.infer_ms)
        lat = sorted(self.latency_ms)
        pick = lambda a, q: round(a[min(len(a) - 1, int(q * len(a)))], 1) if a else None
        m = self.m
        seen = m["processed"] + m["droppedReplaced"] + m["droppedStale"]
        return {"targetFps": self.target_fps, "inferenceFps": None if fps is None else round(fps, 2),
                "inferenceMsAvg": round(sum(inf) / len(inf), 1) if inf else None, "inferenceMsP95": pick(inf, 0.95),
                "latencyMsAvg": round(sum(lat) / len(lat), 1) if lat else None, "latencyMsP95": pick(lat, 0.95),
                "stageMs": dict(self.stage_ms), "queueSize": 1, "queueDepthNow": 0 if self.job is None else 1,
                "framesSubmitted": m["submitted"], "framesProcessed": m["processed"], "framesDropped": m["droppedReplaced"] + m["droppedStale"],
                "droppedReplaced": m["droppedReplaced"], "droppedStale": m["droppedStale"], "skippedInterval": m["skippedInterval"],
                "droppedPct": round(100.0 * (m["droppedReplaced"] + m["droppedStale"]) / seen, 1) if seen else 0.0,
                "errors": m["errors"], "lastError": m["lastError"], "lastFrameId": m["lastFrameId"],
                "judge": {"queued": self.judger.pending(), "evidenceJudged": self.judger.stats["judged"],
                          "evidenceSkippedBusy": self.judger.stats["skippedBusy"], "crossingsFinalised": self.judger.stats["finalised"],
                          "msAvg": round(sum(self.judger.stats["ms"]) / len(self.judger.stats["ms"]), 1) if self.judger.stats["ms"] else None,
                          "errors": self.judger.stats["errors"]},
                "model": self.model_path.name if hasattr(self.model_path, "name") else str(self.model_path), "engine": self.engine,
                "uptimeSec": round(now - self.started, 1)}

    def view(self, mid):
        mod = self.mods.get(mid)
        if mod is None:
            return None
        now = time.time()
        tr = mod.tracker
        out = {"module": mid, "since": now_iso(mod.started), "counts": dict(mod.counts), "crossings": mod.crossings, "ignoredDirection": mod.ignored,
               "recent": list(mod.recent)[:30], "lastEventAt": now_iso(mod.last_event_at) if mod.last_event_at else None,
               "line": mod.cfg.get("line"), "direction": mod.cfg.get("direction", "both"), "warnings": list(mod.warnings),
               "frame": {"width": self.frame_shape[1], "height": self.frame_shape[0]} if self.frame_shape else None,
               "metrics": self.metrics(), "tracking": tr.reliability() if tr else None,
               "tracksNow": [b[3] for b in mod.last_boxes if b[3] is not None]}
        if tr:
            out["tracking"]["suppressed"] = {"duplicate": tr.stats["duplicatesSuppressed"], "cooldown": tr.stats["cooldownSuppressed"],
                                              "shortTrack": tr.stats["shortSuppressed"], "pathAroundLineEnd": tr.stats["aroundEnd"]}
        if mid == "pcross":
            out["windows"] = mod.stats_windows(now)
            out["net"] = mod.counts["in"] - mod.counts["out"]
        else:
            out["judge"] = helmet_judge.status()
        return out
