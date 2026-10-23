"""PPE Detection engine for one camera session (Construction PPE: helmet + colour, orange vest, gloves).

Same pattern as Fire & Smoke: the session hands the newest frame over (submit) at most once per `intervalSec`;
a worker thread runs the PPE detector + ByteTrack, the person-first rules (rules.py) and the helmet-crop
classifier, then reports confirmed missing PPE through on_alert with the exact frame that was assessed.
Camera capture and the live view never wait for it (frames handed over while it is busy are skipped and counted).

Models (backend/models/ppe, see README.md there):
    ppe_yolo11n.pt   YOLO11n, classes helmet gloves vest boots goggles none Person no_helmet no_goggle no_gloves no_boots
    helmet_cls.pt    YOLO11n-cls on helmet crops: construction / other / road   (shared by all cameras)
"""
import logging
import threading
import time
from datetime import datetime

import cv2
import numpy as np

from backend import config
from backend.detection.ppe import rules
from backend.runtime import gated, name_thread

log = logging.getLogger("aivision.ppe")

MODELS_DIR = config.BACKEND_DIR / "models" / "ppe"
DETECTOR = MODELS_DIR / "ppe_yolo11n.pt"
HELMET_CLS = MODELS_DIR / "helmet_cls.pt"
TRACKER = "bytetrack.yaml"
ZOOM = {"min_person_h": 0.16, "max_persons": 3, "pad": 0.10, "conf": 0.25}    # edgeppe/config.py ZOOM
FORGET_SEC = 30.0                    # a person not seen for this long is dropped from the live table

COLORS = {rules.COMPLIANT: (60, 170, 60), rules.NON_COMPLIANT: (40, 40, 220), rules.NEEDS_REVIEW: (0, 191, 255),
          rules.PARTIAL: (150, 150, 150)}
HELMET_BGR = {"white": (240, 240, 240), "yellow": (60, 200, 235), "orange": (60, 130, 250), "red": (40, 40, 220),
              "green": (60, 170, 60), "blue": (200, 120, 40), "black": (30, 30, 30), "other": (128, 128, 128),
              "unknown": (128, 128, 128)}
SHORT = {rules.PRESENT: "OK", rules.MISSING: "MISSING", rules.UNCERTAIN: "?", rules.NOT_ASSESSABLE: "n/a"}

_CLS = None
_CLS_LOCK = threading.Lock()


def helmet_classifier():
    """The helmet-crop classifier, loaded once per process and shared by every camera (stateless)."""
    global _CLS
    with _CLS_LOCK:
        if _CLS is None and HELMET_CLS.is_file():
            from ultralytics import YOLO
            _CLS = YOLO(str(HELMET_CLS), task="classify")
        return _CLS


# ---- rider check: the project's COCO detector (yolov8n; RKNN NPU / OpenVINO / PyTorch as configured) --------------
RIDER_COCO = (1, 3)                 # bicycle, motorcycle
RIDER_CONF = 0.35
_RIDER = {"model": None, "imgsz": 640, "tried": False, "error": None}
_RIDER_LOCK = threading.Lock()


def rider_model():
    """The main COCO detector, loaded once per process for the PPE rider check (None if it cannot load)."""
    with _RIDER_LOCK:
        if not _RIDER["tried"]:
            _RIDER["tried"] = True
            try:
                from ultralytics import YOLO
                engine, path = config.resolve_engine()
                imgsz = 640
                if engine == "rknn":
                    from backend.detection.pipeline import load_rknn_prereqs
                    imgsz, _p = load_rknn_prereqs(path)
                _RIDER.update(model=YOLO(str(path), task="detect"), imgsz=imgsz, engine=engine)
            except Exception as exc:            # the PPE check keeps working; riders are then not recognised
                _RIDER["error"] = repr(exc)
                log.warning("PPE rider check unavailable: %s", exc)
        return _RIDER["model"]


def two_wheelers(frame):
    """[x1, y1, x2, y2] of bicycles / motorcycles in the frame."""
    m = rider_model()
    if m is None:
        return []
    with _RIDER_LOCK, gated():
        r = m.predict(frame, imgsz=_RIDER["imgsz"], conf=RIDER_CONF, classes=list(RIDER_COCO), verbose=False)[0]
    return [[float(v) for v in b.xyxy[0].tolist()] for b in r.boxes] if r.boxes is not None else []


def on_two_wheeler(pbox, vehicles):
    """Person sits on a bicycle / motorcycle: person centre over the vehicle (horizontally), vehicle in the lower
    part of the person box, real overlap."""
    px1, py1, px2, py2 = pbox
    ph, pcx, pcy = py2 - py1, (px1 + px2) / 2, (py1 + py2) / 2
    for vx1, vy1, vx2, vy2 in vehicles:
        if not (vx1 <= pcx <= vx2):
            continue
        if vy1 > py1 + 0.85 * ph or vy2 < pcy:          # vehicle must cover the lower body
            continue
        ix = max(0.0, min(px2, vx2) - max(px1, vx1))
        iy = max(0.0, min(py2, vy2) - max(py1, vy1))
        if ix * iy >= 0.15 * (px2 - px1) * ph:
            return True
    return False


def helmet_kind(frame, box, cls_model=None, min_prob=rules.HELMET_CLS_MIN):
    """("construction" | "road" | "other", probability), on the helmet crop with a 10 % context margin (as the
    classifier's training crops). "road" is checked first: a motorcycle / bicycle shell must not pass."""
    cls_model = cls_model or helmet_classifier()
    if cls_model is None:
        return "unverified", None
    crop = rules._crop(frame, box, pad=0.10)
    if crop is None or min(crop.shape[:2]) < 8:
        return "other", 0.0
    with _CLS_LOCK, gated():
        r = cls_model.predict(crop, verbose=False)[0]
    probs = {str(n).lower(): float(r.probs.data[i]) for i, n in r.names.items()}
    if probs.get("road", 0.0) >= min_prob:
        return "road", probs["road"]
    if probs.get("construction", 0.0) >= min_prob:
        return "construction", probs["construction"]
    return "other", max(probs.values()) if probs else 0.0


EVIDENCE_CONF = 0.25     # lowest detector score kept (a weak helmet makes the person UNCERTAIN, not MISSING)


class PPEEngine:
    def __init__(self, on_alert, thread_name=None):
        if not DETECTOR.is_file():
            raise FileNotFoundError(f"PPE model missing: {DETECTOR.relative_to(config.BACKEND_DIR)}")
        from ultralytics import YOLO
        self.model = YOLO(str(DETECTOR), task="detect")
        self.names = self.model.names
        self.on_alert = on_alert
        self.cfg = None
        self.zones, self._polys, self._polys_for = [], [], None
        self.people = {}             # track id -> rules.PersonState
        self.result = None           # (frame shape, [(view, item boxes)], generation)
        self.generation = 0
        self._lock = threading.Condition()
        self._job, self._busy = None, False
        self._stop = threading.Event()
        self._last_submit = 0.0
        self.runs = self.skipped = self.errors = 0
        self.latency_ms = None
        self.last_error = None
        self.last_assessed_at = None
        self.summary_ids = {k: set() for k in ("assessed", "compliant", "nonCompliant", "review",
                                               "helmetMissing", "vestMissing", "glovesMissing", "vestColor", "roadHelmet")}
        self._synthetic = 0
        self.riders_checked = 0
        self.thread_name = thread_name
        self._worker = threading.Thread(target=self._run, name=thread_name or "ppe-worker", daemon=True)
        self._worker.start()

    # ---- configuration ----
    def configure(self, cfg, camera_zones):
        zones = {z["id"]: z for z in camera_zones or [] if z.get("enabled", True)}
        self.zones = [zones[i] for i in (cfg.get("zones") or []) if i in zones]
        self._polys_for = None
        self.cfg = cfg

    def reset(self):
        self.generation += 1
        self.people = {}
        pred = getattr(self.model, "predictor", None)          # fresh track ids after a restart
        for t in (getattr(pred, "trackers", None) or []):
            try:
                t.reset()
            except Exception:
                pass
        self.result = None
        for s in self.summary_ids.values():
            s.clear()

    def required(self):
        r = (self.cfg or {}).get("required") or {}
        return {i: r.get(i, True) is not False for i in rules.ITEMS}

    # ---- frames ----
    def submit(self, frame, captured_at):
        now = time.monotonic()
        if now - self._last_submit < float((self.cfg or {}).get("intervalSec", 1.0)):
            return False
        with self._lock:
            if self._busy or self._job is not None:
                self.skipped += 1
                return False
            self._job = (frame, captured_at, self.generation)
            self._last_submit = now
            self._lock.notify()
        return True

    def draw(self, annotated):
        res = self.result
        if res is None or res[2] != self.generation:
            return annotated
        shape, people, _ = res
        sx, sy = annotated.shape[1] / shape[1], annotated.shape[0] / shape[0]
        for v in people:
            draw_person(annotated, v, sx, sy)
        return annotated

    def close(self):
        self._stop.set()
        with self._lock:
            self._lock.notify_all()

    # ---- worker ----
    def _run(self):
        if self.thread_name:
            name_thread(self.thread_name)
        while not self._stop.is_set():
            with self._lock:
                while self._job is None and not self._stop.is_set():
                    self._lock.wait(0.5)
                if self._stop.is_set():
                    return
                (frame, captured_at, gen), self._job = self._job, None
                self._busy = True
            try:
                t0 = time.perf_counter()
                views, due = self.assess(frame, time.time())
                el = (time.perf_counter() - t0) * 1000.0
                self.latency_ms = el if self.latency_ms is None else 0.8 * self.latency_ms + 0.2 * el
                self.runs += 1
                if gen != self.generation:
                    continue
                self.result = (frame.shape[:2], views, gen)
                self.last_assessed_at = captured_at or time.time()
                if due:
                    when = datetime.fromtimestamp(captured_at or time.time())
                    for view, kind, label, item, reason in due:
                        snap = annotate_snapshot(frame, views, view, label, when)
                        self.on_alert(kind, label, item, reason, view, snap, frame, captured_at)
            except Exception as exc:          # keep running; the session reports the error
                self.errors += 1
                self.last_error = repr(exc)
                log.exception("PPE detection failed")
            finally:
                with self._lock:
                    self._busy = False

    def detect(self, frame):
        cfg = self.cfg or {}
        person_conf = float(cfg.get("confidence", rules.PERSON_MIN_CONF))   # the setting = minimum person confidence
        conf = EVIDENCE_CONF      # PPE item boxes: weak ones are kept as evidence; the rules decide (never PRESENT alone)
        with gated():
            r = self.model.track(frame, persist=True, tracker=TRACKER, imgsz=int(cfg.get("imgSize", 640)), conf=conf, verbose=False)[0]
        dets = []
        for b in r.boxes:
            dets.append({"cls": self.names[int(b.cls)], "box": [float(v) for v in b.xyxy[0].tolist()],
                         "conf": float(b.conf), "tid": int(b.id) if b.id is not None else None})
        dets = rules.filter_persons(dets, person_conf)
        # zoom: small persons get a second, upscaled pass for helmets / gloves (edgeppe monitor's zoom)
        persons = [d for d in dets if d["cls"] == "Person"]
        fh, fw = frame.shape[:2]
        small = sorted((p for p in persons if (p["box"][3] - p["box"][1]) < ZOOM["min_person_h"] * fh),
                       key=lambda p: p["box"][3] - p["box"][1])[:ZOOM["max_persons"]]
        for p in small:
            x1, y1, x2, y2 = p["box"]
            px, py = (x2 - x1) * ZOOM["pad"], (y2 - y1) * ZOOM["pad"]
            cx1, cy1, cx2, cy2 = max(0, int(x1 - px)), max(0, int(y1 - py)), min(fw, int(x2 + px)), min(fh, int(y2 + py))
            if cx2 - cx1 < 8 or cy2 - cy1 < 8:
                continue
            with gated():
                zr = self.model.predict(frame[cy1:cy2, cx1:cx2], imgsz=int(cfg.get("imgSize", 640)), conf=ZOOM["conf"], verbose=False)[0]
            for b in zr.boxes:
                name = self.names[int(b.cls)]
                if name == "Person":
                    continue
                bx = [float(v) for v in b.xyxy[0].tolist()]
                cand = {"cls": name, "box": [bx[0] + cx1, bx[1] + cy1, bx[2] + cx1, bx[3] + cy1], "conf": float(b.conf), "tid": None}
                if all(cand["cls"] != d["cls"] or rules.iou(cand["box"], d["box"]) < 0.45 for d in dets):
                    dets.append(cand)
        return rules.suppress_helmet_conflicts(dets)

    def assess(self, frame, now):
        """One assessment: detections -> per-person evidence -> state -> (views for drawing, alerts due)."""
        cfg = self.cfg or {}
        dets = self.detect(frame)
        persons = [d for d in dets if d["cls"] == "Person"]
        persons = [p for p in persons if self._in_zones(p["box"], frame.shape)]
        items = {i: [] for i in range(len(persons))}
        for d in dets:
            if d["cls"] in ("Person", "none"):
                continue
            i = rules.assign_person(d["box"], persons, d["cls"])
            if i is not None:                       # an item that belongs to nobody is never reported
                items[i].append(d)
        req = self.required()
        views, due = [], []
        cls_model = helmet_classifier() if cfg.get("helmetClassifier", True) else None
        kind_fn = (lambda f, b: helmet_kind(f, b, cls_model)) if cls_model is not None else None
        # rider check only when someone has a helmet (the extra COCO pass is skipped otherwise)
        helmeted = [i for i in items if any(d["cls"] == "helmet" and d["conf"] >= rules.CLASS_CONF["helmet"][0] for d in items[i])]
        vehicles = two_wheelers(frame) if helmeted and cfg.get("riderCheck", True) else []
        self.riders_checked += 1 if helmeted and cfg.get("riderCheck", True) else 0
        grey = rules.grey_picture(frame)
        for i, p in enumerate(persons):
            tid = p["tid"]
            if tid is None:                         # untracked this frame: own short-lived id
                self._synthetic -= 1
                tid = self._synthetic
            ps = self.people.get(tid) or rules.PersonState(tid)
            self.people[tid] = ps
            ev = rules.assess_person(p, items[i], frame, cfg, kind_fn,
                                     rider=bool(vehicles) and on_two_wheeler(p["box"], vehicles), grey=grey)
            ps.update(ev, cfg, now)
            ps.box = [round(v, 1) for v in p["box"]]
            v = ps.view(req)
            v["assessedAt"] = now
            v["personConf"] = round(p["conf"], 3)
            views.append(v)
            self._count(tid, v)
            for kind, label, item, reason in rules.alerts_due(ps, req, cfg, now):
                due.append((v, kind, label, item, reason))
        for tid in [t for t, s in self.people.items() if now - s.last_seen > FORGET_SEC]:
            self.people.pop(tid, None)
        return views, due

    def _count(self, tid, v):
        s = self.summary_ids
        s["assessed"].add(tid)
        key = {rules.COMPLIANT: "compliant", rules.NON_COMPLIANT: "nonCompliant", rules.NEEDS_REVIEW: "review"}.get(v["overall"])
        if key:
            s[key].add(tid)
        it = v["items"]
        for item in rules.ITEMS:
            if it[item]["status"] == rules.MISSING:
                s[item + "Missing"].add(tid)
        if it["helmet"].get("type") in rules.NON_PPE_HELMET and it["helmet"]["status"] == rules.MISSING:
            s["roadHelmet"].add(tid)
        if it["vest"].get("colorCompliance") == "non-compliant":
            s["vestColor"].add(tid)

    def _in_zones(self, box, shape):
        if not self.zones:
            return True
        h, w = shape[:2]
        key = (h, w, id(self.zones))
        if self._polys_for != key:
            from backend.detection.pipeline import zone_polygon
            self._polys = [zone_polygon(z, w, h) for z in self.zones]
            self._polys_for = key
        foot = (float((box[0] + box[2]) / 2), float(box[3]))    # people are judged where they stand
        return any(len(p) >= 3 and cv2.pointPolygonTest(p, foot, False) >= 0 for p in self._polys)

    # ---- reporting ----
    def view(self):
        res = self.result
        people = res[1] if res and res[2] == self.generation else []
        s = self.summary_ids
        return {"people": people, "assessedAt": self.last_assessed_at,
                "summary": {"peopleAssessed": len(s["assessed"]), "compliant": len(s["compliant"]),
                            "nonCompliant": len(s["nonCompliant"]), "needsReview": len(s["review"]),
                            "missingHelmet": len(s["helmetMissing"]), "missingVest": len(s["vestMissing"]),
                            "missingGloves": len(s["glovesMissing"]), "roadHelmet": len(s["roadHelmet"]),
                            "vestWrongColor": len(s["vestColor"])},
                "summaryNote": "unique tracked people since start (ByteTrack can give one person a new id after occlusion)",
                "required": self.required(), "vestColor": (self.cfg or {}).get("vestColor"),
                "helmetClassifier": "on" if (self.cfg or {}).get("helmetClassifier", True) and HELMET_CLS.is_file() else "off",
                "riderCheck": ("off" if not (self.cfg or {}).get("riderCheck", True) else
                               "unavailable" if _RIDER["tried"] and _RIDER["model"] is None else "on"),
                "rejectBlackHelmet": bool((self.cfg or {}).get("rejectBlackHelmet", True)),
                "runs": self.runs, "skipped": self.skipped, "errors": self.errors, "lastError": self.last_error,
                "inferenceMs": round(self.latency_ms, 1) if self.latency_ms is not None else None,
                "intervalSec": (self.cfg or {}).get("intervalSec")}

    def current_counts(self):
        res = self.result
        if not res:
            return {}
        out = {}
        for v in res[1]:
            out[v["overall"]] = out.get(v["overall"], 0) + 1
        return out


# ---- drawing -------------------------------------------------------------------------------------------------------
def _box(img, b, sx, sy, col, label, thick=2):
    x1, y1, x2, y2 = int(b[0] * sx), int(b[1] * sy), int(b[2] * sx), int(b[3] * sy)
    cv2.rectangle(img, (x1, y1), (x2, y2), col, thick)
    if label:
        cv2.putText(img, label, (x1, max(y1 - 6, 12)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3)
        cv2.putText(img, label, (x1, max(y1 - 6, 12)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1)


def draw_person(img, v, sx=1.0, sy=1.0, highlight=False):
    col = COLORS.get(v["overall"], (150, 150, 150))
    pb = v["box"]
    _box(img, pb, sx, sy, col, f"P{v['personId']} {v['overall'].replace('_', ' ')}", 3 if highlight else 2)
    it = v["items"]
    h = it["helmet"]
    if h.get("box"):
        _box(img, h["box"], sx, sy, HELMET_BGR.get(h.get("color"), (128, 128, 128)),
             ({"road": "road helmet", "rider": "rider helmet", "black": "black helmet"}.get(h.get("type"))
              or f"helmet-{h.get('color', 'unknown')}"), 2)
    vs = it["vest"]
    if vs.get("box") and vs["status"] != rules.MISSING:
        _box(img, vs["box"], sx, sy, (60, 130, 250), f"vest-{vs.get('color', 'unknown')}", 1)
    g = it["gloves"]
    if g.get("box"):
        _box(img, g["box"], sx, sy, (60, 170, 60) if g["status"] == rules.PRESENT else (40, 40, 220),
             "gloves" if g["status"] == rules.PRESENT else "no gloves", 1)
    x, y = int(pb[0] * sx) + 3, int(pb[3] * sy) - 40
    for k, name in (("helmet", "H"), ("vest", "V"), ("gloves", "G")):
        st = it[k]["status"]
        c = (60, 170, 60) if st == rules.PRESENT else (40, 40, 220) if st == rules.MISSING else (0, 191, 255) if st == rules.UNCERTAIN else (170, 170, 170)
        t = f"{name}:{SHORT[st]}"
        cv2.putText(img, t, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 0, 0), 3)
        cv2.putText(img, t, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.45, c, 1)
        y += 15


def annotate_snapshot(frame, views, target, label, when):
    """The assessed frame itself with every person's PPE boxes, the alerting person highlighted and a header."""
    img = frame.copy()
    for v in views:
        draw_person(img, v, highlight=v is target)
    head = f"PPE: {label} - person P{target['personId']} - {when:%Y-%m-%d %H:%M:%S}"
    cv2.rectangle(img, (0, 0), (img.shape[1], 28), (0, 0, 0), -1)
    cv2.putText(img, head, (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 255), 1)
    return img


def models_present():
    return DETECTOR.is_file()


def np_frame(h=360, w=640):        # helper for tests
    return np.zeros((h, w, 3), np.uint8)
