"""Object Monitoring engine for one camera session (replaces the standalone monitor's app.py main loop).

Same three layers, same threads as the original:
  Layer 1  ObjectMonitor (monitor.py)        every new frame, in the camera session loop
  Layer 2  OpenVocabDetector + DetectorWorker background thread, every `interval_s`; names objects
           (open_vocab.py)                    and spots people covering them (never alarms)
  Layer 3  GroundingDinoVerifier (optional)   only on mark / alert; needs the Grounding DINO files in
                                              backend/models/object_monitor/hf (not shipped)
The mouse box-drawing and keys of the desktop window become API calls (mark / remove / clear /
accept positions) from the web UI. Marked objects are remembered per camera (data/object_marks.json)
and re-referenced from the first frame after the module starts - like `app.py --mark`, the objects
must be in place when monitoring starts.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from backend import config
from backend.services.util import JsonFile

from .alerts import annotate_alert, draw_objects
from .common import clip_box
from .monitor import ObjectMonitor

log = logging.getLogger("aivision.object")

MODELS_DIR = config.BACKEND_DIR / "models" / "object_monitor"
DINO_DIR = MODELS_DIR / "hf" / "models--IDEA-Research--grounding-dino-tiny"


def dino_available() -> tuple[bool, str]:
    """Layer 3 can run only if its model files and the transformers package are present."""
    if not (DINO_DIR / "snapshots").is_dir() or not any((DINO_DIR / "snapshots").glob("*/model.safetensors")):
        return False, "Grounding DINO files not installed (backend/models/object_monitor/hf)"
    try:
        import transformers  # noqa: F401
    except Exception:
        return False, "Python package 'transformers' not installed"
    return True, ""


class MarkStore:
    """Marked objects per camera, normalised 0-1 (survive a backend restart)."""

    def __init__(self):
        self.file = JsonFile(str(config.DATA_DIR / "object_marks.json"), {})
        self.lock = threading.RLock()

    def get(self, cam_id):
        with self.lock:
            return list(self.file.data.get(cam_id, []))

    def put(self, cam_id, marks):
        with self.lock:
            self.file.data[cam_id] = marks
            self.file.save()


class ObjectEngine:
    def __init__(self, camera_id: str, cfg: dict, marks: MarkStore, on_alert, on_info):
        self.camera_id = camera_id
        self.cfg = cfg
        self.marks = marks
        self._on_alert, self._on_info = on_alert, on_info
        self.monitor = ObjectMonitor(self._monitor_cfg(cfg), on_alert=self._alert, on_info=self._info)
        self.frame_size = None
        self.last_frame = None
        self._pending = list(marks.get(camera_id))       # restored marks, added on the first frame
        self.messages = []                               # recent info lines for the UI
        self.alert_count = 0
        self.detector = self.detector_label = self.detector_error = None
        self.verifier = None
        self.verifier_state = "off"
        self._start_layers(cfg)

    # ------------------------------------------------------------------ setup
    @staticmethod
    def _monitor_cfg(cfg):
        return dict(cfg["monitor"])

    def _start_layers(self, cfg):
        d = cfg["detector"]
        if d.get("enabled"):
            try:
                from .open_vocab import DetectorWorker, OpenVocabDetector
                det = OpenVocabDetector(d["model"], d["vocabulary"], MODELS_DIR, d["imgsz"], d["conf"], d["iou"], d["tiles"])
                self.detector = DetectorWorker(det, d["intervalS"], d["nameConf"], d["nameConfFull"])
                self.detector.start()
                self.detector_label = f"{Path(d['model']).stem}@{d['imgsz']}"
            except Exception as exc:     # the original also kept monitoring without Layer 2
                self.detector_error = str(exc)
                log.error("Object Monitoring Layer 2 detector not available (%r) - monitoring continues without names", exc)
        v = cfg.get("verifier") or {}
        if v.get("enabled"):
            ok, why = dino_available()
            if ok:
                from .verifier import GroundingDinoVerifier
                vcfg = {"model_id": "IDEA-Research/grounding-dino-tiny", "image_size": v["imageSize"],
                        "box_threshold": v["boxThreshold"], "text_threshold": v["textThreshold"],
                        "unload_after_s": v["unloadAfterS"]}
                self.verifier = GroundingDinoVerifier(vcfg, d["vocabulary"], MODELS_DIR)
                self.verifier.start()
                self.verifier_state = "on"
            else:
                self.verifier_state = "unavailable: " + why

    def configure(self, cfg):
        """Live settings change: Layer 1 thresholds apply on the next check; Layer 2 interval / naming
        thresholds immediately (model, size and vocabulary changes need a module restart)."""
        self.cfg = cfg
        self.monitor.cfg = self._monitor_cfg(cfg)
        if self.detector:
            d = cfg["detector"]
            self.detector.interval_s = float(d["intervalS"])
            self.detector.name_conf, self.detector.name_conf_full = float(d["nameConf"]), float(d["nameConfFull"])
            self.detector.detector.conf = float(d["conf"])

    def close(self):
        for t in (self.detector, self.verifier):
            if t:
                t.stop()

    # ------------------------------------------------------------------ per frame (session loop)
    def process(self, raw, annotated):
        h, w = raw.shape[:2]
        self.frame_size = (w, h)
        self.last_frame = raw
        if self._pending:
            pending, self._pending = self._pending, []
            for m in pending:
                self._add(raw, m["box"], m.get("name") if m.get("namedBy") == "user" else None,
                          restored_name=m.get("name"), restored_by=m.get("namedBy"))
        people = self.detector.people(max(6.0, 2.5 * float(self.cfg["detector"]["intervalS"]))) if self.detector else []
        self.monitor.process(raw, people)
        if self.detector:
            self.detector.offer_frame(raw)
        dets = self.detector.latest if (self.detector and self.cfg["detector"].get("showLabels")) else []
        return draw_objects(annotated, self.monitor.view(), dets, bool(dets))

    # ------------------------------------------------------------------ commands from the UI
    def mark(self, box_norm, name=None):
        """box_norm: (x, y, w, h) 0-1 of the frame. Uses the newest frame as the object's reference."""
        frame = self.last_frame
        if frame is None:
            raise RuntimeError("no camera frame yet - wait until the stream is running")
        self._add(frame, box_norm, (name or "").strip() or None)

    def _add(self, frame, box_norm, name, restored_name=None, restored_by=None):
        H, W = frame.shape[:2]
        x, y, bw, bh = box_norm
        box = clip_box((int(x * W), int(y * H), int(bw * W), int(bh * H)), W, H)
        if box[2] < 8 or box[3] < 8:
            raise ValueError("the box is too small (at least 8 x 8 pixels of the camera frame)")

        def added(obj):
            if not name and restored_name and restored_by in ("detector", "dino"):
                self.monitor.set_name(obj.id, restored_name, restored_by)
            if not name:
                self._name_object(obj, frame, box)
            self._save_marks()
        self.monitor.request_add(frame.copy(), box, name, callback=added)

    def remove(self, oid):
        self.monitor.request("remove", int(oid))
        self._defer_save()

    def clear(self):
        self.monitor.request("clear")
        self._defer_save()

    def accept_positions(self):
        self.monitor.request("reset")
        self._defer_save()

    def rename(self, oid, name):
        ok = self.monitor.set_name(int(oid), name, "user")
        if ok:
            self._save_marks()
        return ok

    def _defer_save(self):
        # commands run on the next frame; save after that
        def later():
            time.sleep(0.6)
            self._save_marks()
        threading.Thread(target=later, daemon=True).start()

    def _save_marks(self):
        if not self.frame_size:
            return
        W, H = self.frame_size
        marks = [{"name": o["name"], "namedBy": o["named_by"],
                  "box": [round(o["home"][0] / W, 5), round(o["home"][1] / H, 5), round(o["home"][2] / W, 5), round(o["home"][3] / H, 5)]}
                 for o in self.monitor.view()]
        self.marks.put(self.camera_id, marks)

    # ------------------------------------------------------------------ naming (Layer 2 / 3), as app.py name_object()
    def _name_object(self, obj, frame, box):
        if self.detector:
            label = self.detector_label

            def from_detector(d, how):
                if d is None:
                    self._info(f"NAME | #{obj.id}: detector has no name for it - keeps '{obj.name}'")
                elif how == "unsure":  # a weak guess is not presented as a fact
                    self._info(f"NAME | #{obj.id}: detector unsure (best guess '{d.name}' {d.conf:.2f}) - keeps '{obj.name}'")
                elif self.monitor.set_name(obj.id, d.name, "detector"):
                    self._info(f"NAME | #{obj.id} -> '{d.name}' ({label}, {how}, conf {d.conf:.2f})")
                    self._save_marks()
            self.detector.request_name(frame, box, from_detector)
        if self.verifier:
            def from_dino(name, score):
                if name and self.monitor.set_name(obj.id, name, "dino"):
                    self._info(f"NAME | #{obj.id} -> '{name}' (Grounding DINO {score:.2f}, {self.verifier.last_seconds:.1f} s)")
                    self._save_marks()
                elif not name:
                    self._info(f"NAME | #{obj.id}: Grounding DINO found no vocabulary name")
            self.verifier.request(frame, box, from_dino)

    # ------------------------------------------------------------------ alerts / info
    def _alert(self, view, kind, ev, frame):
        when = datetime.now()
        distance = float(getattr(ev, "distance", 0.0))
        self.alert_count += 1
        img = annotate_alert(frame, view, kind, when, distance)
        self._on_alert(view, kind, distance, img)
        if self.verifier and not view.get("named_by"):  # still unnamed: ask Layer 3 once (as app.py on_alert)
            self._name_object(SimpleNamespace(id=view["id"], name=view["name"]), frame,
                              view["current"] if kind == "moved" else view["home"])

    def _info(self, text):
        log.info("camera %s: %s", self.camera_id, text)
        self.messages.insert(0, {"t": datetime.now().astimezone().isoformat(timespec="seconds"), "text": text})
        del self.messages[40:]
        self._on_info(text)

    # ------------------------------------------------------------------ status
    def view(self):
        W, H = self.frame_size or (1, 1)
        objs = []
        for o in self.monitor.view():
            norm = lambda b: [round(b[0] / W, 5), round(b[1] / H, 5), round(b[2] / W, 5), round(b[3] / H, 5)] if b else None
            objs.append({"id": o["id"], "name": o["name"], "namedBy": o["named_by"] or "", "state": o["state"],
                         "score": round(float(o["score"]), 3), "distance": round(float(o["distance"]), 1),
                         "alert": o["alert"], "home": norm(o["home"]), "current": norm(o["current"])})
        return {
            "objects": objs, "scenePaused": self.monitor.scene_paused, "checks": self.monitor.checks,
            "pendingRestore": len(self._pending), "alerts": self.alert_count, "messages": self.messages[:20],
            "frameSize": list(self.frame_size) if self.frame_size else None,
            "layer2": {"model": self.detector_label, "error": self.detector_error,
                       "latencyS": round(self.detector.latency, 2) if self.detector and self.detector.runs else None,
                       "runs": self.detector.runs if self.detector else 0,
                       "people": len(self.detector.people(9999)) if self.detector else 0},
            "layer3": self.verifier_state,
        }
