"""Detection modules of the shared backend: Human Detection, Vehicle Detection, Human Count,
Fire & Smoke Detection and Object Monitoring.

Both modules are served by the SAME per-camera pipeline (one RTSP connection and one
YOLO pass per frame, as in the original script). Each module has its own enable/pause
state, classes, thresholds, zones, snapshot policy and alert policy, stored per camera
inside camera["detection"]["modules"][<module>] - the same object the UI's camera form edits.
"""
import copy

from backend import config
from backend.detection.pipeline import HUMAN_LABELS, VEHICLE_LABELS

MODULES = {
    "human": {
        "id": "human", "name": "Human Detection",
        "labels": list(HUMAN_LABELS),
        "classKeys": {"person": "Person", "rider": "Rider", "personFar": "Person (far)"},
        "eventType": {"Person": "person", "Rider": "rider", "Person (far)": "person"},
    },
    "vehicle": {
        "id": "vehicle", "name": "Vehicle Detection",
        "labels": list(VEHICLE_LABELS),
        "classKeys": {"twoWheeler": "2-Wheeler", "fourWheeler": "4-Wheeler", "bus": "Bus", "truck": "Truck"},
        "eventType": {lbl: "vehicle" for lbl in VEHICLE_LABELS},
    },
    # Human Count: separate people-counting engine (backend/detection/human_count.py), same camera session
    "count": {
        "id": "count", "name": "Human Count",
        "labels": ["Person"],
        "classKeys": {"person": "Person"},
        "eventType": {"Person": "occupancy"},
    },
    # Fire & Smoke: separate fire/smoke model + smoke rules (backend/detection/fire_smoke.py), same camera session
    "fire": {
        "id": "fire", "name": "Fire & Smoke Detection",
        "labels": ["Fire", "Smoke"],
        "classKeys": {"fire": "Fire", "smoke": "Smoke"},
        "eventType": {"Fire": "fire", "Smoke": "smoke"},
    },
    # Object Monitoring: marked-object monitor (backend/detection/object_monitor/), same camera session
    "object": {
        "id": "object", "name": "Object Monitoring",
        "labels": ["Moved", "Missing"],
        "classKeys": {},
        "eventType": {"Moved": "object_moved", "Missing": "object_missing"},
    },
}

# Defaults reproduce the original human_detect_yolo-2.py behaviour.
DEFAULTS = {
    "human": {
        "enabled": False,
        "confidence": 0.35,                       # original CONF (predictor base threshold)
        "classConf": {"Person": 0.30},            # original CLASS_CONF_OVERRIDE[0]
        "classes": {"person": True, "rider": True, "personFar": False},   # personFar = ENABLE_FAR_DETECTION
        "zones": [],
        # original SAVE_SNAPSHOTS=True: one snapshot when each new Person/Rider is confirmed
        "snapshot": {"mode": "new_track", "labels": ["Person", "Rider"], "cooldownSec": 0},
        "alert": {"enabled": True, "labels": ["Person", "Rider", "Person (far)"], "severity": "low",
                  "cooldownSec": config.DEFAULT_EVENT_COOLDOWN_SEC},
    },
    "vehicle": {
        "enabled": False,
        "confidence": 0.35,
        "classConf": {"2-Wheeler": 0.35, "4-Wheeler": 0.35, "Bus": 0.55, "Truck": 0.55},
        "classes": {"twoWheeler": True, "fourWheeler": True, "bus": True, "truck": True},
        "zones": [],
        # The original saved no vehicle snapshots; new_track + cooldown avoids flooding the disk.
        "snapshot": {"mode": "new_track", "labels": list(VEHICLE_LABELS), "cooldownSec": config.DEFAULT_SNAPSHOT_COOLDOWN_SEC},
        "alert": {"enabled": True, "labels": list(VEHICLE_LABELS), "severity": "low",
                  "cooldownSec": config.DEFAULT_EVENT_COOLDOWN_SEC},
    },
}

DEFAULTS["count"] = {
    "enabled": False,
    "confidence": 0.40,                          # person detections >= this start a new track
    "classConf": {"Person": 0.40},
    "classes": {"person": True},
    "zones": [],                                 # empty = whole frame; else people are counted where they stand
    "counting": {"minFrames": 3, "keepLostSec": 2.0},
    # no snapshot per person; an occupancy alert (people now >= threshold) saves the frame that raised it
    "snapshot": {"mode": "off", "labels": [], "cooldownSec": 0},
    "alert": {"enabled": False, "labels": ["Person"], "severity": "medium", "cooldownSec": 60, "threshold": 0},
}

# Fire & Smoke defaults = the standalone detector's config.yaml (model.variant small, imgsz 480, detection,
# alert and smoke_rules sections). Snapshots: one annotated frame with every alert (as the original).
DEFAULTS["fire"] = {
    "enabled": False,
    "confidence": 0.50,                          # detection.conf_threshold (fallback for a class without classConf)
    "classConf": {"Fire": 0.30, "Smoke": 0.65},  # detection.class_conf
    "classes": {"fire": True, "smoke": True},
    "iou": 0.45,
    "imgSize": 480,
    "everyNFrames": 2,                           # analyse every 2nd frame, reuse the boxes in between
    "removeGreenOverlay": True,
    "verifier": {"enabled": True, "minSmokeProb": 0.40, "maxBoxes": 4},
    "alertRule": {"consecutiveFrames": 3, "windowFrames": 6, "minHits": {"fire": 2, "smoke": 3}},
    "smokeRules": {"enabled": True, "lowConf": 0.40, "fireNearFrac": 0.15, "persistS": 20, "minRiseFrac": 0.03,
                   "minGrowth": 1.3, "longPersistS": 90, "maxGapS": 4, "driftFrac": 0.08},
    "zones": [],
    "snapshot": {"mode": "alert", "labels": ["Fire", "Smoke"], "cooldownSec": 0},
    "alert": {"enabled": True, "labels": ["Fire", "Smoke"], "severity": "critical", "smokeSeverity": "high", "cooldownSec": 10},
}

# Object Monitoring defaults = the standalone marked-object monitor's config.yaml (monitor, detector,
# verifier sections; same key names for the monitor so its code is unchanged).
DEFAULTS["object"] = {
    "enabled": False,
    "confidence": 0.15,                          # = detector.conf (Layer 2 labels / spotting people)
    "zones": [],
    "monitor": {
        "check_every_n_frames": 5, "min_side_px": 64, "features": "orb", "tracker": "csrt",
        "present_score": 0.60, "lost_score": 0.40, "found_score": 0.60, "hist_min": 0.35,
        "move_threshold_px": 40, "move_threshold_pct": 30,
        "confirm_checks": 4, "min_moved_s": 2.0, "min_missing_s": 5.0, "restore_checks": 3,
        "occlusion_ring_min": 0.50, "max_occlusion_s": 120, "cooldown_s": 30,
        "reference_refresh_s": 60, "scene_change_min": 0.40,
    },
    "detector": {
        "enabled": True, "model": "yoloe-26n-seg.pt", "imgsz": 960, "conf": 0.15, "nameConf": 0.40,
        "nameConfFull": 0.10, "iou": 0.50, "tiles": 0, "intervalS": 3.0, "showLabels": True,
        "vocabulary": {
            "laptop": ["laptop", "silver laptop", "black laptop"],
            "desktop computer": ["desktop computer tower", "black pc cabinet"],
            "computer monitor": ["computer monitor", "black computer monitor"],
            "keyboard": ["computer keyboard", "black keyboard"],
            "mouse": ["computer mouse"],
            "lunch box": ["lunch box", "tiffin box", "food container"],
            "water bottle": ["water bottle", "steel water bottle", "plastic bottle"],
            "mug": ["coffee mug", "tea cup"],
            "phone": ["mobile phone", "smartphone"],
            "headphones": ["headphones", "headset"],
            "notebook": ["notebook", "diary"],
            "pen": ["pen"],
            "backpack": ["backpack", "laptop bag"],
            "chair": ["office chair", "blue office chair"],
            "stapler": ["stapler"],
            "calculator": ["calculator"],
            "file folder": ["file folder", "box file"],
            "book": ["book"],
            "printer": ["printer"],
            "dustbin": ["dustbin", "waste basket"],
            "table": ["table", "office desk", "table tennis table"],
            "box": ["cardboard box", "carton box"],
            "person": ["person"],
        },
    },
    "verifier": {"enabled": True, "imageSize": 480, "boxThreshold": 0.30, "textThreshold": 0.25, "unloadAfterS": 300},
    "snapshot": {"mode": "alert", "labels": ["Moved", "Missing"], "cooldownSec": 0},
    "alert": {"enabled": True, "labels": ["Moved", "Missing"], "severity": "high", "cooldownSec": 30},
}

# AI frame sampling per module (Method 1 / 4 / 8 of the multi-camera optimisation). intervalSec 0 = analyse
# every frame (previous behaviour). imgSize = model input size for this module (frames are resized only for the
# model; boxes are mapped back to the original frame by Ultralytics). Fire & Smoke keeps its own worker and
# everyNFrames setting and is never motion-gated.
DEFAULTS["vehicle"]["schedule"] = {"intervalSec": config.VEHICLE_INTERVAL_SEC, "captureFps": config.VEHICLE_CAPTURE_FPS,
                                   "imgSize": config.IMG_SIZE, "confirmMargin": config.SAMPLED_CONFIRM_MARGIN,
                                   "dedupeSec": config.SAMPLED_DEDUPE_SEC}
DEFAULTS["human"]["schedule"] = {"intervalSec": config.HUMAN_INTERVAL_SEC, "captureFps": config.HUMAN_CAPTURE_FPS,
                                 "imgSize": config.IMG_SIZE, "confirmMargin": config.SAMPLED_CONFIRM_MARGIN,
                                 "dedupeSec": config.SAMPLED_DEDUPE_SEC, "motionGate": config.HUMAN_MOTION_GATE,
                                 "motionMinAreaPct": config.MOTION_MIN_AREA_PCT, "motionHoldSec": config.MOTION_HOLD_SEC,
                                 "fallbackSec": config.MOTION_FALLBACK_SEC}
DEFAULTS["count"]["schedule"] = {"intervalSec": config.COUNT_INTERVAL_SEC, "captureFps": config.HUMAN_CAPTURE_FPS,
                                 "imgSize": config.IMG_SIZE}
DEFAULTS["object"]["schedule"] = {"intervalSec": 0.0, "motionGate": config.OBJECT_MOTION_GATE,
                                  "motionMinAreaPct": config.MOTION_MIN_AREA_PCT, "motionHoldSec": config.MOTION_HOLD_SEC,
                                  "fallbackSec": config.MOTION_FALLBACK_SEC}
SCHEDULE_LIMITS = {"intervalSec": (0, 600, False), "captureFps": (0, 60, False), "imgSize": (160, 1920, True),
                   "confirmMargin": (0, 0.5, False), "dedupeSec": (0, 3600, False), "motionMinAreaPct": (0.01, 50, False),
                   "motionHoldSec": (0, 600, False), "fallbackSec": (1, 3600, False)}


def _validate_schedule(module_id, cfg):
    sch = cfg.get("schedule")
    if sch is None or module_id not in ("human", "vehicle", "count", "object"):
        return []
    if not isinstance(sch, dict):
        return ["schedule must be an object"]
    errs = []
    allowed = DEFAULTS[module_id]["schedule"]
    for k, v in sch.items():
        if k not in allowed:
            errs.append(f"schedule.{k} is not a setting of {module_id}")
        elif k == "motionGate":
            if not isinstance(v, bool):
                errs.append("schedule.motionGate must be true or false")
        else:
            lo, hi, integer = SCHEDULE_LIMITS[k]
            n = len(errs)
            _num(errs, f"schedule.{k}", v, lo, hi, integer)
            if k == "imgSize" and len(errs) == n and int(v) % 32:
                errs.append("schedule.imgSize must be a multiple of 32 (e.g. 480, 640, 960)")
    return errs


SEVERITIES = ("low", "medium", "high", "critical")
SNAPSHOT_MODES = ("new_track", "off")


def module_config(camera, module_id):
    """Effective config = defaults deep-merged with what is stored on the camera."""
    stored = (((camera or {}).get("detection") or {}).get("modules") or {}).get(module_id) or {}
    if module_id in ("fire", "object"):
        stored = _legacy(module_id, stored)
    cfg = deep_merge(copy.deepcopy(DEFAULTS[module_id]), stored)
    if module_id == "object":
        # the vocabulary is fixed: its text-embedding cache ships with the model (no text encoder at runtime)
        cfg["detector"]["vocabulary"] = copy.deepcopy(DEFAULTS["object"]["detector"]["vocabulary"])
    return cfg


def _legacy(module_id, stored):
    """The camera form stored placeholder settings for Fire & Smoke / Object Monitoring before these
    modules ran on the backend (fire: {fire, smoke, severity}; object: {target, missingAfterSec,
    alert: 'alert'}). Translate what still means something, drop what does not, never let a scalar
    replace a settings section."""
    out = {}
    for k, v in stored.items():
        dflt = DEFAULTS[module_id].get(k)
        if isinstance(dflt, dict) and not isinstance(v, dict):
            continue
        if k in DEFAULTS[module_id]:
            out[k] = v
    if module_id == "fire":
        if "classes" not in stored and any(k in stored for k in ("fire", "smoke")):
            out["classes"] = {"fire": stored.get("fire", True) is not False, "smoke": stored.get("smoke", True) is not False}
        if isinstance(stored.get("severity"), str) and "alert" not in stored:
            out["alert"] = {"severity": stored["severity"]}
        if "classConf" not in stored:
            out.pop("confidence", None)          # the form's placeholder 0.45 is not the detector's threshold
    if module_id == "object":
        out.pop("confidence", None)
    return out


def deep_merge(base, over):
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _num(errs, name, v, lo, hi, integer=False):
    try:
        x = int(v) if integer else float(v)
        if integer and float(v) != int(v):
            raise ValueError
        if not lo <= x <= hi:
            errs.append(f"{name} must be between {lo} and {hi}")
    except (TypeError, ValueError):
        errs.append(f"{name} must be a {'whole number' if integer else 'number'}")


def _validate_fire(cfg, camera_zones):
    errs = []
    _num(errs, "confidence", cfg.get("confidence"), 0.05, 0.99)
    for lbl, v in (cfg.get("classConf") or {}).items():
        if lbl not in ("Fire", "Smoke"):
            errs.append(f"unknown class '{lbl}' for fire")
        else:
            _num(errs, f"{lbl} confidence", v, 0.05, 0.99)
    classes = cfg.get("classes") or {}
    if [k for k in classes if k not in ("fire", "smoke")]:
        errs.append("fire classes are fire and smoke")
    if cfg.get("enabled") and not (classes.get("fire", True) or classes.get("smoke", True)):
        errs.append("select fire, smoke or both")
    _num(errs, "iou", cfg.get("iou"), 0.1, 0.9)
    _num(errs, "imgSize", cfg.get("imgSize"), 320, 1280, True)
    if not errs and int(cfg["imgSize"]) % 32:
        errs.append("imgSize must be a multiple of 32 (e.g. 416, 480, 640)")
    _num(errs, "everyNFrames", cfg.get("everyNFrames"), 1, 10, True)
    v = cfg.get("verifier") or {}
    _num(errs, "verifier.minSmokeProb", v.get("minSmokeProb"), 0.0, 1.0)
    _num(errs, "verifier.maxBoxes", v.get("maxBoxes"), 1, 20, True)
    r = cfg.get("alertRule") or {}
    _num(errs, "alertRule.consecutiveFrames", r.get("consecutiveFrames"), 1, 30, True)
    _num(errs, "alertRule.windowFrames", r.get("windowFrames"), 1, 60, True)
    for k in ("fire", "smoke"):
        _num(errs, f"alertRule.minHits.{k}", (r.get("minHits") or {}).get(k), 1, 60, True)
    sr = cfg.get("smokeRules") or {}
    for k, lo, hi in (("lowConf", 0.05, 0.99), ("fireNearFrac", 0, 1), ("persistS", 0, 3600), ("minRiseFrac", 0, 1),
                      ("minGrowth", 1, 10), ("longPersistS", 0, 86400), ("maxGapS", 0, 600), ("driftFrac", 0, 1)):
        _num(errs, f"smokeRules.{k}", sr.get(k), lo, hi)
    alert = cfg.get("alert") or {}
    for k in ("severity", "smokeSeverity"):
        if alert.get(k) not in SEVERITIES:
            errs.append(f"alert.{k} must be one of " + ", ".join(SEVERITIES))
    _num(errs, "alert.cooldownSec", alert.get("cooldownSec"), 0, 86400)
    zone_ids = {z.get("id") for z in camera_zones or []}
    bad = [z for z in cfg.get("zones") or [] if z not in zone_ids]
    if bad:
        errs.append("zones not defined on this camera: " + ", ".join(map(str, bad)))
    return errs


def _validate_object(cfg):
    errs = []
    m = cfg.get("monitor") or {}
    for k, lo, hi, integer in (("check_every_n_frames", 1, 50, True), ("min_side_px", 16, 512, True),
                               ("present_score", 0.05, 0.99, False), ("lost_score", 0.05, 0.99, False),
                               ("found_score", 0.05, 0.99, False), ("hist_min", 0, 1, False),
                               ("move_threshold_px", 1, 5000, False), ("move_threshold_pct", 1, 1000, False),
                               ("confirm_checks", 1, 50, True), ("min_moved_s", 0, 3600, False),
                               ("min_missing_s", 0, 3600, False), ("restore_checks", 1, 50, True),
                               ("occlusion_ring_min", 0, 1, False), ("max_occlusion_s", 0, 86400, False),
                               ("cooldown_s", 0, 86400, False), ("reference_refresh_s", 1, 86400, False),
                               ("scene_change_min", 0, 1, False)):
        _num(errs, f"monitor.{k}", m.get(k), lo, hi, integer)
    if m.get("features") not in ("orb", "sift"):
        errs.append("monitor.features must be orb or sift")
    if m.get("tracker") not in ("csrt", "kcf", "mil", "none"):
        errs.append("monitor.tracker must be csrt, kcf, mil or none")
    if not errs and float(m["lost_score"]) > float(m["present_score"]):
        errs.append("monitor.lost_score must not be above monitor.present_score")
    d = cfg.get("detector") or {}
    for k, lo, hi in (("conf", 0.01, 0.99), ("nameConf", 0.01, 0.99), ("nameConfFull", 0.01, 0.99), ("iou", 0.1, 0.9),
                      ("intervalS", 0.5, 600)):
        _num(errs, f"detector.{k}", d.get(k), lo, hi)
    _num(errs, "detector.imgsz", d.get("imgsz"), 320, 1920, True)
    if d.get("tiles") not in (0, 2):
        errs.append("detector.tiles must be 0 (whole frame) or 2 (2x2 tiles)")
    v = cfg.get("verifier") or {}
    _num(errs, "verifier.imageSize", v.get("imageSize"), 224, 1333, True)
    for k in ("boxThreshold", "textThreshold"):
        _num(errs, f"verifier.{k}", v.get(k), 0.05, 0.99)
    alert = cfg.get("alert") or {}
    if alert.get("severity") not in SEVERITIES:
        errs.append("alert.severity must be one of " + ", ".join(SEVERITIES))
    return errs


def validate(module_id, cfg, camera_zones):
    """Return a list of human-readable errors (empty = valid)."""
    if module_id == "fire":
        return _validate_fire(cfg, camera_zones)
    if module_id == "object":
        return _validate_object(cfg) + _validate_schedule(module_id, cfg)
    errs = _validate_schedule(module_id, cfg)
    m = MODULES[module_id]
    try:
        c = float(cfg.get("confidence"))
        if not 0.05 <= c <= 0.99:
            errs.append("confidence must be between 0.05 and 0.99")
    except (TypeError, ValueError):
        errs.append("confidence must be a number")
    for lbl, v in (cfg.get("classConf") or {}).items():
        if lbl not in m["labels"]:
            errs.append(f"unknown class '{lbl}' for {module_id}")
            continue
        try:
            if not 0.05 <= float(v) <= 0.99:
                errs.append(f"{lbl} confidence must be between 0.05 and 0.99")
        except (TypeError, ValueError):
            errs.append(f"{lbl} confidence must be a number")
    classes = cfg.get("classes") or {}
    unknown = [k for k in classes if k not in m["classKeys"]]
    if unknown:
        errs.append(f"unsupported classes for {module_id}: {', '.join(unknown)} (supported: {', '.join(m['classKeys'])})")
    if cfg.get("enabled") and not any(classes.get(k, True) for k in m["classKeys"] if k != "personFar"):
        errs.append("select at least one class")
    zone_ids = {z.get("id") for z in camera_zones or []}
    bad = [z for z in cfg.get("zones") or [] if z not in zone_ids]
    if bad:
        errs.append("zones not defined on this camera: " + ", ".join(map(str, bad)))
    snap = cfg.get("snapshot") or {}
    if snap.get("mode") not in SNAPSHOT_MODES:
        errs.append("snapshot.mode must be one of " + ", ".join(SNAPSHOT_MODES))
    if any(lbl not in m["labels"] for lbl in snap.get("labels") or []):
        errs.append("snapshot.labels contains unknown classes")
    alert = cfg.get("alert") or {}
    if alert.get("severity") not in SEVERITIES:
        errs.append("alert.severity must be one of " + ", ".join(SEVERITIES))
    for key in ("cooldownSec",):
        for section in (snap, alert):
            try:
                if not 0 <= float(section.get(key, 0)) <= 86400:
                    errs.append(f"{key} must be 0 - 86400 seconds")
            except (TypeError, ValueError):
                errs.append(f"{key} must be a number")
    if any(lbl not in m["labels"] for lbl in alert.get("labels") or []):
        errs.append("alert.labels contains unknown classes")
    if module_id == "count":
        cnt = cfg.get("counting") or {}
        try:
            if not 1 <= int(cnt.get("minFrames", 3)) <= 10:
                errs.append("counting.minFrames must be 1 - 10")
        except (TypeError, ValueError):
            errs.append("counting.minFrames must be a whole number")
        try:
            if not 0.2 <= float(cnt.get("keepLostSec", 2.0)) <= 30:
                errs.append("counting.keepLostSec must be 0.2 - 30 seconds")
        except (TypeError, ValueError):
            errs.append("counting.keepLostSec must be a number")
        try:
            if not 0 <= int(alert.get("threshold", 0)) <= 1000:
                errs.append("alert.threshold must be 0 - 1000 people (0 = off)")
        except (TypeError, ValueError):
            errs.append("alert.threshold must be a whole number")
        if snap.get("mode") not in ("off",):
            errs.append("Human Count saves snapshots only with occupancy alerts (snapshot.mode must be off)")
    return errs


def describe():
    """Registry entry list for GET /detection-modules (running state filled by caller)."""
    return [{"id": m["id"], "name": m["name"], "classes": m["labels"]} for m in MODULES.values()]
