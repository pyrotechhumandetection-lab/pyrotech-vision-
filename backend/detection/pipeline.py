"""The existing person + vehicle detection algorithm, extracted from human_detect_yolo-2.py.

Preserved exactly from the original script:
  * Ultralytics YOLO (COCO weights) + ByteTrack (bytetrack_stable.yaml), model.track(persist=True)
  * class mapping: person -> Person; bicycle/motorcycle -> 2-Wheeler; car -> 4-Wheeler; bus -> Bus; truck -> Truck
  * per-class minimum confidence (CLASS_CONF_OVERRIDE) applied after the predictor's base conf
  * MIN_HITS_TO_CONFIRM = 2, VOTES_BEFORE_LOCKING = 5 (label voting per track id)
  * Rider = Person whose box centre lies inside / just above (RIDER_Y_MARGIN) a 2-Wheeler box
  * far people / vehicles: zoomed tiles over the top half of the full-resolution frame (personFar / vehicleFar,
    on by default), joined to the normal pass before the tracker (replaces the original upscaled far-person pass)
  * animal filter: animals are asked from the model too, so a dog / cow is not reported as a person (animalFilter)
  * same box colours and "<label> #<id> <conf>" annotation

Changed for the web service (no change to how objects are detected):
  * no cv2.imshow window; the annotated frame is returned to the caller
  * classes requested from the model follow the enabled modules (Human / Vehicle)
  * thresholds come from the per-camera module config (defaults = original values)
  * optional detection zones (point-in-polygon on the box centre, as in object-track.py)
  * reports newly confirmed tracks so the caller can raise events / snapshots
  * the same weights can run through OpenVINO (FP32 export of yolov8n.pt, identical detections, faster on CPU)

Changed for fast-moving vehicles (accuracy rule kept):
  * vehicle tracks are associated with buffered IoU (detection/tracker.py), so a vehicle that moves
    further than its own width between analysed frames keeps one track instead of being dropped
  * a VEHICLE track whose first UNANIMOUS_VOTES_TO_LOCK votes all agree on one class is locked after
    those votes instead of waiting for VOTES_BEFORE_LOCKING. Tracks with mixed votes still need the
    original 5-vote majority, and MIN_HITS_TO_CONFIRM and the per-class confidence thresholds are
    unchanged, so a fast car/truck/2-wheeler that is visible for only 3 analysed frames is no longer missed.
    The early lock also needs a mean confidence of class threshold + EARLY_LOCK_CONF_MARGIN, so borderline
    detections (e.g. a parked scooter scored 0.41 as "4-Wheeler") still need the original 5-vote majority.
"""
import logging
import os
import time

import cv2
import numpy as np

from backend.runtime import gated

log = logging.getLogger("aivision.pipeline")

# ---- constants from the original script ------------------------------------
PERSON_CLASS = 0
VEHICLE_CLASS_MAP = {
    1: ("2-Wheeler", (255, 200, 0)),
    3: ("2-Wheeler", (255, 200, 0)),
    2: ("4-Wheeler", (0, 200, 255)),
    5: ("Bus", (200, 0, 255)),
    7: ("Truck", (0, 0, 255)),
}
PERSON_CLASS_MAP = {PERSON_CLASS: ("Person", (0, 255, 0))}
ALL_CLASS_MAP = {**PERSON_CLASS_MAP, **VEHICLE_CLASS_MAP}
TWO_WHEELER_COCO_IDS = {1, 3}
RIDER_COLOR = (0, 140, 255)
FAR_COLOR = (0, 255, 255)

MIN_HITS_TO_CONFIRM = 2
VOTES_BEFORE_LOCKING = 5
UNANIMOUS_VOTES_TO_LOCK = 3        # vehicle tracks only - see module docstring
EARLY_LOCK_CONF_MARGIN = 0.10      # ... and only when those votes average >= class threshold + this margin
RIDER_Y_MARGIN = 40

# COCO animals: requested together with "person" so the model can say "dog" instead of a weak "person"
# (animal filter, see _pre_track); they are never reported or tracked.
ANIMAL_COCO_IDS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 23}   # bird cat dog horse sheep cow elephant bear zebra giraffe
ANIMAL_OVERLAP = 0.45            # person box overlapping an animal box this much (IoU, or 70 % of the person inside)
ANIMAL_CONF_RATIO = 0.8          # ... and the animal at least 0.8 x the person's confidence -> the "person" is the animal
# Far zoom (personFar / vehicleFar): the far (top) part of the FULL-RESOLUTION frame is cut into tiles and each tile
# is analysed at the model's input size, i.e. far objects are seen about FAR_ZOOM_TILES x larger. The boxes join the
# normal pass BEFORE the tracker, so far people / vehicles are tracked, confirmed and alerted like any other.
# (.env: AIV_FAR_ZOOM_REGION, AIV_FAR_ZOOM_TILES, AIV_FAR_ZOOM_EVERY). Test frames with ~140 px tall people in a
# 2304x1296 picture: the normal 640 px pass found 21 of 63, two zoom tiles 61.
FAR_ZOOM_REGION = min(1.0, max(0.2, float(os.environ.get("AIV_FAR_ZOOM_REGION", "0.5"))))   # top half (far objects)
FAR_ZOOM_TILES = min(4, max(1, int(os.environ.get("AIV_FAR_ZOOM_TILES", "2"))))   # side-by-side tiles, 2 = ~2x zoom
FAR_ZOOM_EVERY = max(1, int(os.environ.get("AIV_FAR_ZOOM_EVERY", "2")))   # every 2nd analysed frame
FAR_ZOOM_OVERLAP = 0.12          # tiles overlap so an object on the seam is whole in one of them

HUMAN_LABELS = ("Person", "Rider", "Person (far)")
LABEL_COLORS = {"Person": (0, 255, 0), "Rider": RIDER_COLOR, "Person (far)": FAR_COLOR,
                **{lbl: col for lbl, col in VEHICLE_CLASS_MAP.values()}}
VEHICLE_LABELS = ("2-Wheeler", "4-Wheeler", "Bus", "Truck")
LABEL_TO_COCO = {"Person": [0], "2-Wheeler": [1, 3], "4-Wheeler": [2], "Bus": [5], "Truck": [7]}


def load_rknn_prereqs(model_path):
    """Rockchip NPU: install the runtime for Ultralytics; (input size, precision label) of the RKNN model."""
    from backend.detection import rknn_runtime
    ok, why = rknn_runtime.available()
    if not ok:
        raise RuntimeError(f"RKNN NPU not available: {why}")
    rknn_runtime.install_for_ultralytics()
    info = rknn_runtime.model_info(model_path)
    if not info["file"]:
        raise RuntimeError(f"no .rknn file in {model_path}")
    return info["imgsz"], "INT8" if info["quantize"] == 8 else "FP16"


def npu_core_of(yolo):
    """NPU core of a loaded Ultralytics RKNN model ('core 0' ...), best effort."""
    try:
        from backend.detection import rknn_runtime
        ab = yolo.predictor.model                                    # AutoBackend
        be = getattr(ab, "backend", None) or getattr(ab, "model", None)   # RKNNBackend
        rk = getattr(be, "model", None)                              # RKNNLite (shim or real)
        mask = getattr(rk, "core_mask", None)
        if mask is None:
            mask = getattr(rk, "_aivision_core", None)
        return rknn_runtime.core_name(mask) if mask is not None else "auto"
    except Exception:
        return "auto"


def fp16_kwargs():
    """FP16 inference argument for the installed Ultralytics: 'quantize' (8.4+, 'half' is deprecated there)."""
    try:
        from ultralytics.cfg import DEFAULT_CFG_DICT
        if "quantize" in DEFAULT_CFG_DICT:
            return {"quantize": "fp16"}
    except Exception:
        pass
    return {"half": True}


def label_module(label):
    return "human" if label in HUMAN_LABELS else "vehicle"


def box_center(x1, y1, x2, y2):
    return (x1 + x2) / 2, (y1 + y2) / 2


def is_rider(person_box, vehicle_box, y_margin):
    px1, py1, px2, py2 = person_box
    vx1, vy1, vx2, vy2 = vehicle_box
    pcx, pcy = box_center(px1, py1, px2, py2)
    if not (vx1 <= pcx <= vx2):
        return False
    return (vy1 - y_margin) <= pcy <= vy2


def iou(box_a, box_b):
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    return inter / float(area_a + area_b - inter + 1e-6)


def zone_polygon(zone, w, h):
    """Normalised zone (rectangle or polygon, as saved by the UI) -> pixel polygon."""
    if zone.get("type") == "rectangle":
        x, y, zw, zh = zone["x"], zone["y"], zone["width"], zone["height"]
        pts = [(x, y), (x + zw, y), (x + zw, y + zh), (x, y + zh)]
    else:
        pts = [(p["x"], p["y"]) for p in zone.get("points", [])]
    return np.array([(int(px * w), int(py * h)) for px, py in pts], dtype=np.int32)


def hex_to_bgr(color, fallback=(255, 255, 255)):
    try:
        c = color.lstrip("#")
        return (int(c[4:6], 16), int(c[2:4], 16), int(c[0:2], 16))
    except Exception:
        return fallback


class PersonVehiclePipeline:
    """One instance per camera session (ByteTrack state is per model instance)."""

    def __init__(self, model_path, tracker_config, img_size=640, device=None, engine="pytorch"):
        from ultralytics import YOLO  # imported here so the API can start (and report) without it
        from backend.detection.tracker import install as install_tracker
        install_tracker()                # ByteTrack + buffered IoU for fast vehicles (see tracker.py)

        self.npu_core, self.npu_precision = None, None
        if engine == "rknn":             # Rockchip NPU: fixed input size of the exported model
            img_size, self.npu_precision = load_rknn_prereqs(model_path)
        self.model = YOLO(str(model_path), task="detect")
        self.model_path = str(model_path)
        self.engine = engine
        self.tracker_config = str(tracker_config)
        self.img_size = img_size
        self.device = None if engine == "rknn" else device
        if engine == "rknn":
            self._npu_warmup()
        from backend import config as _cfg
        # FP16 / NMS IoU only for the PyTorch model on a CUDA device (see config.use_half)
        self.half = engine == "pytorch" and _cfg.use_half()
        self.nms_iou = _cfg.NMS_IOU
        self.far_model = None
        self._pre = None             # far-zoom boxes / animal filter for the current model call (see _pre_track)
        self._far_n = 0
        self.model.callbacks["on_predict_postprocess_end"].insert(0, self._pre_track)
        self.last_stages = {}        # ms of the last frame: preprocess / inference / postprocess / tracking / gateWait
        self.shared_persons = None   # person detections of the last frame for Human Count (see process(share_person_conf))
        self._share = None           # active share request during one model call
        self._share_cb_installed = False
        self.reset_tracking()
        if self.device not in (None, "cpu") and engine == "pytorch":
            self._warmup(_cfg.GPU_WARMUP)

    def _npu_warmup(self):
        """Load the RKNN model now (Ultralytics loads lazily on the first call) so load errors surface here and
        the engine can fall back to the CPU; also reports the NPU core the model runs on."""
        blank = np.zeros((self.img_size * 9 // 16, self.img_size, 3), dtype=np.uint8)
        self.model.predict(blank, imgsz=self.img_size, verbose=False)
        self.npu_core = npu_core_of(self.model)

    def _warmup(self, n):
        """A few predict() passes on a blank frame: CUDA context, cuDNN algorithm search and FP16 conversion
        happen here and not on the first camera frames (which otherwise pile up in the queue)."""
        if n <= 0:
            return
        t0 = time.perf_counter()
        blank = np.zeros((self.img_size * 9 // 16, self.img_size, 3), dtype=np.uint8)    # 16:9 camera shape
        try:
            for _ in range(n):
                self.model.predict(blank, imgsz=self.img_size, device=self.device, verbose=False,
                                   **(fp16_kwargs() if self.half else {}))
            log.info("GPU warm-up: %d passes in %.0f ms (imgsz %d, %s)", n, (time.perf_counter() - t0) * 1000.0,
                     self.img_size, "FP16" if self.half else "FP32")
        except Exception as e:      # never block the session on the warm-up; the real call reports errors
            log.warning("GPU warm-up failed: %s", e)

    def _sample_is_new(self, module, label, box, dedupe_sec):
        """Sampled mode: True when no box of the same label confirmed within dedupe_sec overlaps this one.
        A matching earlier sighting is refreshed (a vehicle parked for hours is reported once)."""
        now = time.time()
        seen = [e for e in self.sample_seen.get(module, []) if now - e[2] <= dedupe_sec]
        for e in seen:
            if e[0] == label and iou(e[1], box) >= 0.3:
                e[1], e[2] = box, now
                self.sample_seen[module] = seen
                return False
        seen.append([label, box, now])
        self.sample_seen[module] = seen[-500:]
        return True

    def draw_held(self, frame, plan, dets, age_s):
        """Live view between two analysed frames: draw the zones and the LAST analysed frame's boxes (they show
        where objects were up to one interval ago - labelled with the age so they are not mistaken for live)."""
        h, w = frame.shape[:2]
        for zs in (plan or {}).get("zones", {}).values():
            for z in zs:
                poly = zone_polygon(z, w, h)
                if len(poly) >= 3:
                    cv2.polylines(frame, [poly], True, hex_to_bgr(z.get("color", "#3478FF")), 2)
        for d in dets or []:
            x1, y1, x2, y2 = (int(d["box"][0] * w), int(d["box"][1] * h), int(d["box"][2] * w), int(d["box"][3] * h))
            col = LABEL_COLORS.get(d["label"], (255, 255, 255)) if d.get("inZone", True) else (140, 140, 140)
            cv2.rectangle(frame, (x1, y1), (x2, y2), col, 1)
            cv2.putText(frame, f"{d['label']} {d['confidence']:.2f}", (x1, max(y1 - 8, 14)), cv2.FONT_HERSHEY_SIMPLEX, 0.45, col, 1)
        txt = f"AI result {age_s:.1f} s ago"
        cv2.putText(frame, txt, (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 3)
        cv2.putText(frame, txt, (10, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
        return frame

    def reset_tracking(self):
        self.sample_seen = {}
        self.hit_counts = {}
        self.first_seen = {}
        self.label_votes = {}
        self.vote_conf = {}
        self.locked_label = {}
        self.confirmed_ids = {lbl: set() for lbl in HUMAN_LABELS + VEHICLE_LABELS}
        self.promoted_far_ids = set()
        self.animal_suppressed = 0

    # ------------------------------------------------------------------ config
    @staticmethod
    def build_plan(human_cfg, vehicle_cfg, camera_zones):
        """Translate module configs into what the model is asked for.

        human_cfg / vehicle_cfg: dict or None (module inactive or paused).
        """
        coco = set()
        thresholds = {}
        base_confs = []
        draw_labels = set()
        zone_by_module = {}
        far, far_v, animals = False, False, False
        if human_cfg:
            base = float(human_cfg.get("confidence", 0.35))
            base_confs.append(base)
            classes = human_cfg.get("classes", {})
            if classes.get("person", True) or classes.get("rider", True):
                coco.add(PERSON_CLASS)
            if classes.get("rider", True):
                coco.update(TWO_WHEELER_COCO_IDS)   # rider rule needs 2-wheeler boxes
            for lbl in ("Person", "Rider"):
                key = lbl.lower()
                if classes.get(key, True):
                    draw_labels.add(lbl)
            far = bool(classes.get("personFar", True))
            animals = bool(classes.get("animalFilter", True))
            cc = human_cfg.get("classConf", {})
            thresholds["Person"] = max(base, float(cc.get("Person", 0.30)))
            thresholds["Rider"] = thresholds["Person"]
            zone_by_module["human"] = human_cfg.get("zones") or []
        if vehicle_cfg:
            base = float(vehicle_cfg.get("confidence", 0.35))
            base_confs.append(base)
            classes = vehicle_cfg.get("classes", {})
            keymap = {"2-Wheeler": "twoWheeler", "4-Wheeler": "fourWheeler", "Bus": "bus", "Truck": "truck"}
            cc = vehicle_cfg.get("classConf", {})
            defaults = {"2-Wheeler": 0.35, "4-Wheeler": 0.35, "Bus": 0.55, "Truck": 0.55}
            far_v = bool(classes.get("vehicleFar", True))
            for lbl, key in keymap.items():
                if classes.get(key, True):
                    coco.update(LABEL_TO_COCO[lbl])
                    draw_labels.add(lbl)
                thresholds[lbl] = max(base, float(cc.get(lbl, defaults[lbl])))
            zone_by_module["vehicle"] = vehicle_cfg.get("zones") or []
        zones = {z["id"]: z for z in (camera_zones or []) if z.get("enabled", True)}
        far_coco = set()
        if far and PERSON_CLASS in coco:
            far_coco.add(PERSON_CLASS)
        if far_v:
            far_coco.update(c for c in coco if c in VEHICLE_CLASS_MAP)
        if animals and PERSON_CLASS in coco:
            coco.update(ANIMAL_COCO_IDS)
            if far_coco:
                far_coco.update(ANIMAL_COCO_IDS)
        return {
            "animals": animals and PERSON_CLASS in coco,
            "farCoco": sorted(far_coco) if PERSON_CLASS in far_coco or any(c in VEHICLE_CLASS_MAP for c in far_coco) else [],
            "coco": sorted(coco),
            "predict_conf": min(base_confs) if base_confs else 0.35,
            "thresholds": thresholds,
            "draw_labels": draw_labels,
            "zones": {m: [zones[i] for i in ids if i in zones] for m, ids in zone_by_module.items()},
        }

    # ------------------------------------------------------------------ frame
    def process(self, frame, plan, share_person_conf=None, sampled=None, imgsz=None, hd=None):
        """Run the original detection logic on one frame.

        sampled: {module: {"margin": m, "dedupeSec": d}} for modules analysed at an interval (one frame every
        few seconds). Track-based confirmation (several consecutive sightings of one tracker ID) cannot work
        with seconds between frames, so for these modules a box is confirmed on THIS frame when its confidence
        >= class threshold + margin; a confirmed box overlapping (IoU >= 0.3) one of the same label confirmed
        within dedupeSec is not reported again (a parked car does not raise an alert every sample).
        Modules not listed keep the original track-based confirmation unchanged.
        imgsz: model input size for this pass (default: the pipeline's img_size).
        hd: full-resolution frame of the same moment (main stream) for the far zoom; None = zoom into `frame`.

        share_person_conf: Human Count runs on the same camera and needs person boxes above this
        confidence - they are taken from this model pass (self.shared_persons, Nx5 xyxy+conf) instead of
        a second pass. The boxes given to ByteTrack and everything below are unchanged.

        Returns (annotated_frame, detections, new_confirmations, inference_ms)  (inference_ms = model +
        tracker time, without time spent waiting for an inference slot)
        """
        h, w = frame.shape[:2]
        self.shared_persons = None
        self.last_stages = {}
        if not plan["coco"]:
            return frame, [], [], 0.0

        conf, classes = plan["predict_conf"], plan["coco"]
        if share_person_conf is not None:
            # One model pass for Human Detection AND Human Count: run at the lower of the two confidences
            # with the union of the classes, hand the raw person boxes to Human Count, then give the
            # tracker exactly the boxes the original call would have produced (see _split_shared).
            self._install_share_callback()
            self._share = {"conf": conf, "classes": set(classes), "person_conf": float(share_person_conf)}
            conf = min(conf, float(share_person_conf))
            classes = sorted(set(classes) | {PERSON_CLASS})
        if self.engine == "rknn":        # the RKNN model has one fixed input size
            imgsz = self.img_size
        kwargs = dict(imgsz=int(imgsz or self.img_size), conf=conf, classes=classes, verbose=False)
        if self.device is not None:
            kwargs["device"] = self.device
        if self.half:
            kwargs.update(fp16_kwargs())
        if self.nms_iou is not None:
            kwargs["iou"] = self.nms_iou
        far_ms, far_boxes = 0.0, None
        if plan.get("farCoco"):
            self._far_n += 1
            if self._far_n % FAR_ZOOM_EVERY == 1 or FAR_ZOOM_EVERY <= 1:
                tf = time.perf_counter()
                far_boxes = self._far_tiles(hd if hd is not None else frame, frame.shape, plan, kwargs.get("imgsz"))
                far_ms = (time.perf_counter() - tf) * 1000.0
        self._pre = {"far": far_boxes, "animals": plan.get("animals", False)}
        t0 = time.perf_counter()
        try:
            with gated() as slot:
                t1 = time.perf_counter()
                results = self.model.track(frame, persist=True, tracker=self.tracker_config, **kwargs)
                t2 = time.perf_counter()
        finally:
            self._share = None
            self._pre = None
        inference_ms = (time.perf_counter() - t0) * 1000.0 - slot.wait_ms + far_ms
        sp = getattr(results[0], "speed", None) or {}
        pre, inf, post = (float(sp.get(k) or 0.0) for k in ("preprocess", "inference", "postprocess"))
        self.last_stages = {"preprocess": pre, "inference": inf, "postprocess": post,
                            "tracking": max(0.0, (t2 - t1) * 1000.0 - pre - inf - post), "gateWait": slot.wait_ms}
        if far_ms:
            self.last_stages["farZoom"] = far_ms

        thresholds = plan["thresholds"]
        person_boxes, two_wheeler_boxes, all_boxes = [], [], []
        for box in results[0].boxes:
            if box.id is None:
                continue
            track_id = int(box.id[0])
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])
            frame_label, _ = ALL_CLASS_MAP.get(cls_id, ("Unknown", (255, 255, 255)))
            if conf < thresholds.get(frame_label, plan["predict_conf"]):
                continue
            x1, y1, x2, y2 = (int(v) for v in box.xyxy[0].tolist())
            all_boxes.append((track_id, cls_id, x1, y1, x2, y2, conf))

            self.hit_counts[track_id] = self.hit_counts.get(track_id, 0) + 1
            self.first_seen.setdefault(track_id, time.time())
            if track_id not in self.locked_label:
                votes = self.label_votes.setdefault(track_id, {})
                votes[frame_label] = votes.get(frame_label, 0) + 1
                self.vote_conf[track_id] = self.vote_conf.get(track_id, 0.0) + conf
                n_votes = sum(votes.values())
                if n_votes >= VOTES_BEFORE_LOCKING:
                    self.locked_label[track_id] = max(votes, key=votes.get)
                elif (n_votes >= UNANIMOUS_VOTES_TO_LOCK and len(votes) == 1 and frame_label in VEHICLE_LABELS and
                      self.vote_conf[track_id] / n_votes >= thresholds.get(frame_label, plan["predict_conf"]) + EARLY_LOCK_CONF_MARGIN):
                    self.locked_label[track_id] = frame_label

            if cls_id == PERSON_CLASS:
                person_boxes.append((track_id, x1, y1, x2, y2))
            elif cls_id in TWO_WHEELER_COCO_IDS:
                two_wheeler_boxes.append((track_id, x1, y1, x2, y2))

        rider_ids = set()
        if "Rider" in plan["draw_labels"]:
            for p_id, px1, py1, px2, py2 in person_boxes:
                for _v_id, vx1, vy1, vx2, vy2 in two_wheeler_boxes:
                    if is_rider((px1, py1, px2, py2), (vx1, vy1, vx2, vy2), RIDER_Y_MARGIN):
                        rider_ids.add(p_id)
                        break

        zone_polys = {m: [(z, zone_polygon(z, w, h)) for z in zs] for m, zs in plan["zones"].items()}
        for m, polys in zone_polys.items():
            for z, poly in polys:
                if len(poly) >= 3:
                    cv2.polylines(frame, [poly], True, hex_to_bgr(z.get("color", "#3478FF")), 2)

        detections, new_confirmations = [], []
        for track_id, cls_id, x1, y1, x2, y2, conf in all_boxes:
            if track_id in rider_ids:
                label = "Rider"
            elif track_id in self.locked_label:
                label = self.locked_label[track_id]
            else:
                label = None
            frame_label = ALL_CLASS_MAP.get(cls_id, ("Unknown", None))[0]
            shown = label or frame_label
            if shown not in plan["draw_labels"]:
                continue   # e.g. 2-wheeler boxes fetched only for the rider rule
            module = label_module(shown)
            cx, cy = box_center(x1, y1, x2, y2)
            polys = zone_polys.get(module) or []
            in_zone = (not polys) or any(len(p) >= 3 and cv2.pointPolygonTest(p, (float(cx), float(cy)), False) >= 0 for _, p in polys)
            zone_name = next((z.get("name") for z, p in polys if len(p) >= 3 and cv2.pointPolygonTest(p, (float(cx), float(cy)), False) >= 0), None)

            smod = sampled.get(module) if sampled else None
            if smod is not None:
                # sampled mode: confirm on this frame (see process docstring); the tracker still runs and gives IDs
                label = "Rider" if track_id in rider_ids else frame_label
                box_n = (x1 / w, y1 / h, x2 / w, y2 / h)
                confirmed = conf >= thresholds.get(label, plan["predict_conf"]) + float(smod.get("margin", 0.10))
                if confirmed and in_zone and self._sample_is_new(module, label, box_n, float(smod.get("dedupeSec", 60))):
                    new_confirmations.append({"trackId": track_id, "label": label, "module": module,
                                              "confidence": round(conf, 3), "zone": zone_name,
                                              "box": [round(v, 4) for v in box_n], "hits": 1, "firstSeenToConfirmMs": 0,
                                              "confirmMode": "sampled"})
                confirmed_branch = False
            else:
                confirmed = label is not None and self.hit_counts[track_id] >= MIN_HITS_TO_CONFIRM
                confirmed_branch = True
            if confirmed_branch and confirmed and track_id not in self.confirmed_ids.setdefault(label, set()):
                self.confirmed_ids[label].add(track_id)
                if in_zone:
                    new_confirmations.append({"trackId": track_id, "label": label, "module": label_module(label),
                                              "confidence": round(conf, 3), "zone": zone_name,
                                              "box": [round(x1 / w, 4), round(y1 / h, 4), round(x2 / w, 4), round(y2 / h, 4)],
                                              # frames / time from the track's first box to this confirming frame
                                              "hits": self.hit_counts[track_id],
                                              "firstSeenToConfirmMs": round((time.time() - self.first_seen.get(track_id, time.time())) * 1000.0)})
            elif confirmed_branch and confirmed:
                self.confirmed_ids[label].add(track_id)

            display_label = label if label is not None else "(deciding)"
            color = RIDER_COLOR if display_label == "Rider" else ALL_CLASS_MAP.get(cls_id, ("", (255, 255, 255)))[1]
            if not in_zone:
                color = (140, 140, 140)
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            cv2.putText(frame, f"{display_label} #{track_id} {conf:.2f}", (x1, max(y1 - 8, 14)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 2)
            detections.append({"trackId": track_id, "label": display_label, "module": module, "confidence": round(conf, 3),
                               "box": [round(x1 / w, 4), round(y1 / h, 4), round(x2 / w, 4), round(y2 / h, 4)],
                               "confirmed": confirmed, "inZone": in_zone})

        return frame, detections, new_confirmations, inference_ms

    # ------------------------------------------------------------ shared person pass
    def _install_share_callback(self):
        """Runs after NMS and BEFORE the tracker callback that model.track() registers."""
        if self._share_cb_installed:
            return
        cbs = self.model.callbacks["on_predict_postprocess_end"]
        cbs.insert(cbs.index(self._pre_track) + 1 if self._pre_track in cbs else 0, self._split_shared)
        self._share_cb_installed = True

    def _split_shared(self, predictor):
        req = self._share
        if req is None:
            return
        res = predictor.results[0]
        boxes = res.boxes
        if boxes is None or not len(boxes):
            self.shared_persons = np.zeros((0, 5), np.float32)
            return
        data = boxes.data.cpu().numpy()                 # x1 y1 x2 y2 conf cls (no ids yet)
        conf, cls = data[:, 4], data[:, 5].astype(int)
        # Ultralytics keeps boxes with conf > threshold (strict); greedy NMS is monotonic in the threshold
        # and class-aware, so these subsets equal the boxes of separate calls at each threshold / class set.
        pm = (cls == PERSON_CLASS) & (conf > req["person_conf"])
        self.shared_persons = np.concatenate([data[pm, :4], data[pm, 4:5]], 1).astype(np.float32)
        keep = (conf > req["conf"]) & np.isin(cls, list(req["classes"]))
        if not keep.all():
            predictor.results[0] = res[np.flatnonzero(keep)]

    # ------------------------------------------------------------ far zoom + animal filter
    def _far_tiles(self, src, ai_shape, plan, imgsz):
        """Tiles over the top FAR_ZOOM_REGION of `src` (full resolution) -> boxes (N x 6: x1 y1 x2 y2 conf cls) in
        the coordinates of the analysed frame (ai_shape), merged across tiles (class-aware NMS)."""
        from ultralytics import YOLO
        if self.far_model is None:          # own model instance: model.track() keeps its tracker on self.model
            self.far_model = YOLO(self.model_path, task="detect")
        sh, sw = src.shape[:2]
        ah, aw = ai_shape[:2]
        rh = int(sh * FAR_ZOOM_REGION)
        n = max(1, FAR_ZOOM_TILES)
        tw = int(sw / n * (1 + FAR_ZOOM_OVERLAP)) if n > 1 else sw
        step = (sw - tw) / (n - 1) if n > 1 else 0
        out = []
        kw = dict(imgsz=int(imgsz or self.img_size), conf=plan["predict_conf"], classes=plan["farCoco"], verbose=False)
        if self.device is not None:
            kw["device"] = self.device
        for i in range(n):
            x0 = int(round(i * step))
            tile = src[0:rh, x0:x0 + tw]
            with gated():
                r = self.far_model.predict(tile, **kw)[0]
            if r.boxes is None or not len(r.boxes):
                continue
            d = r.boxes.data.cpu().numpy().astype(np.float32)[:, :6]
            d[:, [0, 2]] = (d[:, [0, 2]] + x0) * (aw / sw)
            d[:, [1, 3]] = d[:, [1, 3]] * (ah / sh)
            out.append(d)
        if not out:
            return np.zeros((0, 6), np.float32)
        d = np.concatenate(out)
        keep = []
        for c in np.unique(d[:, 5]):
            idx = np.flatnonzero(d[:, 5] == c)
            boxes = [[float(b[0]), float(b[1]), float(b[2] - b[0]), float(b[3] - b[1])] for b in d[idx]]
            k = cv2.dnn.NMSBoxes(boxes, d[idx, 4].astype(float).tolist(), 0.0, 0.5)
            keep += [int(idx[j]) for j in np.array(k).reshape(-1)]
        return d[sorted(keep)]

    def _pre_track(self, predictor):
        """After NMS, before the tracker: add the far-zoom boxes the normal pass did not find, then drop "person"
        boxes that are really animals (and the animal boxes themselves - animals are never reported)."""
        req = self._pre
        if req is None or (req["far"] is None and not req["animals"]):
            return
        import torch
        res = predictor.results[0]
        data = res.boxes.data.cpu().numpy().astype(np.float32) if res.boxes is not None and len(res.boxes) else np.zeros((0, 6), np.float32)
        data = data[:, :6]
        changed = False
        far = req["far"]
        if far is not None and len(far):
            add = []
            for b in far:
                cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
                dup = False
                for m in data:
                    same = int(m[5]) == int(b[5]) or (int(m[5]) in VEHICLE_CLASS_MAP and int(b[5]) in VEHICLE_CLASS_MAP)
                    if same and (iou(tuple(b[:4]), tuple(m[:4])) > 0.3 or (m[0] <= cx <= m[2] and m[1] <= cy <= m[3])):
                        dup = True
                        break
                if not dup:
                    add.append(b)
            if add:
                data = np.concatenate([data, np.array(add, np.float32)])
                changed = True
        if req["animals"] and len(data):
            cls = data[:, 5].astype(int)
            animal = np.isin(cls, list(ANIMAL_COCO_IDS))
            if animal.any():
                drop = animal.copy()
                for i in np.flatnonzero(cls == PERSON_CLASS):
                    p = data[i]
                    pa = max(1e-6, (p[2] - p[0]) * (p[3] - p[1]))
                    for a in data[animal]:
                        ix = max(0.0, min(p[2], a[2]) - max(p[0], a[0])) * max(0.0, min(p[3], a[3]) - max(p[1], a[1]))
                        if (iou(tuple(p[:4]), tuple(a[:4])) >= ANIMAL_OVERLAP or ix / pa >= 0.7) and a[4] >= ANIMAL_CONF_RATIO * p[4]:
                            drop[i] = True
                            self.animal_suppressed += 1
                            break
                data = data[~drop]
                changed = True
        if changed:
            res.update(boxes=torch.from_numpy(np.ascontiguousarray(data)))

    def unique_counts(self):
        return {label: len(ids) for label, ids in self.confirmed_ids.items()}
