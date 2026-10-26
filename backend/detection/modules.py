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
        # personFar / animalFilter are options, not classes (far zoom, animals not reported as people)
        "classKeys": {"person": "Person", "rider": "Rider", "personFar": "Person (far)", "animalFilter": None},
        "eventType": {"Person": "person", "Rider": "rider", "Person (far)": "person"},
    },
    "vehicle": {
        "id": "vehicle", "name": "Vehicle Detection",
        "labels": list(VEHICLE_LABELS),
        "classKeys": {"twoWheeler": "2-Wheeler", "fourWheeler": "4-Wheeler", "bus": "Bus", "truck": "Truck", "vehicleFar": None},
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
    # PPE Detection: construction PPE per person (backend/detection/ppe/), same camera session, own worker
    "ppe": {
        "id": "ppe", "name": "PPE Detection",
        "labels": ["Helmet missing", "Vest missing", "Gloves missing", "Road helmet (not construction)",
                   "Vest wrong colour", "PPE needs review"],
        "classKeys": {},
        "eventType": {"Helmet missing": "ppe_missing", "Vest missing": "ppe_missing", "Gloves missing": "ppe_missing",
                      "Road helmet (not construction)": "ppe_missing", "Vest wrong colour": "ppe_vest_color",
                      "PPE needs review": "ppe_review"},
    },
    # Two-wheeler helmet check when a motorcycle / scooter crosses a virtual line, and person line crossing &
    # counting (backend/detection/crossing/): one shared tracked pass and worker thread per camera session
    "twhelmet": {
        "id": "twhelmet", "name": "Two-Wheeler Helmet (Line Crossing)",
        "labels": ["Helmet compliant", "Helmet missing", "Helmet unverified"],
        "classKeys": {},
        "eventType": {"Helmet compliant": "twhelmet_compliant", "Helmet missing": "twhelmet_missing",
                      "Helmet unverified": "twhelmet_unverified"},
    },
    "pcross": {
        "id": "pcross", "name": "Person Line Crossing & Counting",
        "labels": ["Person IN", "Person OUT"],
        "classKeys": {},
        "eventType": {"Person IN": "line_cross_in", "Person OUT": "line_cross_out"},
    },
    # Object Monitoring: marked-object monitor (backend/detection/object_monitor/), same camera session
    "object": {
        "id": "object", "name": "Object Monitoring",
        "labels": ["Moved", "Missing"],
        "classKeys": {},
        "eventType": {"Moved": "object_moved", "Missing": "object_missing"},
    },
}

OPTION_KEYS = ("personFar", "vehicleFar", "animalFilter")     # switches stored with the classes, not classes

# Defaults reproduce the original human_detect_yolo-2.py behaviour.
DEFAULTS = {
    "human": {
        "enabled": False,
        "confidence": 0.35,                       # original CONF (predictor base threshold)
        "classConf": {"Person": 0.30},            # original CLASS_CONF_OVERRIDE[0]
        # personFar: zoomed tiles over the far half (pipeline FAR_ZOOM_*); animalFilter: animals never reported as Person
        "classes": {"person": True, "rider": True, "personFar": True, "animalFilter": True},
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
        "classes": {"twoWheeler": True, "fourWheeler": True, "bus": True, "truck": True, "vehicleFar": True},
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

# PPE Detection defaults (gates and colour thresholds: backend/detection/ppe/rules.py, from the PPE project)
DEFAULTS["ppe"] = {
    "enabled": False,
    "confidence": 0.45,                          # minimum PERSON confidence (the project's person filter); PPE items
    #                                              use their own per-class thresholds in detection/ppe/rules.py
    "imgSize": 640,
    "intervalSec": 1.0,                          # one PPE assessment per second per camera (not every frame)
    "required": {"helmet": True, "vest": True, "gloves": True},
    "vestColor": "orange",                       # site policy; "" = any vest colour
    "helmetClassifier": True,                    # construction / road / other helmet-crop classifier
    "rejectBlackHelmet": True,                   # a black helmet is not a construction helmet (site rule)
    "riderCheck": True,                          # helmet of a person on a motorcycle / bicycle = road helmet
    "missAfter": 3,                              # consecutive assessments before an item is MISSING
    "absenceMeansMissing": True,                 # helmet / vest: nothing found on a large visible person counts
    "minPersonFrac": 0.15,                       # person box height (fraction of the frame) to judge head / torso
    "reviewWarnings": False,                     # optional low-severity warning for NEEDS_REVIEW people
    "repeatSec": 600,                            # same person still missing the same item: alert again after this
    "zones": [],
    "snapshot": {"mode": "new_track", "labels": [], "cooldownSec": 0},
    "alert": {"enabled": True, "labels": ["Helmet missing", "Vest missing", "Gloves missing",
                                          "Road helmet (not construction)", "Vest wrong colour"],
              "severity": "high", "cooldownSec": 0},
}
PPE_VEST_COLORS = ("", "orange", "yellow", "green", "red", "blue")

# Line-crossing modules (backend/detection/crossing/). The line is stored per camera in NORMALISED coordinates with the
# resolution it was drawn on; None = no line drawn yet.
_CROSS_COMMON = {
    "enabled": False,
    "line": None,                                # {"x1","y1","x2","y2" (0-1), "refWidth", "refHeight"}
    "direction": "both",                         # which crossings count: "in" = towards the arrow side, "out" = against it
    "confidence": 0.35,                          # minimum detector confidence of a tracked object
    "targetFps": 30,                             # ceiling of the analysed frames per second; the real rate is dynamic (services/ai_rate.py)
    "refPoint": "bottom",                        # bottom = where the object touches the ground, center = box centre
    "minTrackFrames": 3,                         # a track must be seen in this many frames before it can count
    "hysteresis": 0.05,                          # dead band around the line, fraction of the object's box height
    "recrossSec": 2.0,                           # the same track id cannot cross again within this time
    "handoverSec": 1.0,                          # a lost id replaced by a new one nearby within this time = same object
    "dedupeSec": 1.0,                            # two same-direction crossings at the same spot within this time = one
    "minBoxFrac": 0.04,                          # objects lower than this fraction of the picture height are ignored
}
DEFAULTS["pcross"] = dict(copy.deepcopy(_CROSS_COMMON), excludeRiders=True,
                          snapshot={"mode": "crossing"},
                          alert={"enabled": False, "labels": ["Person IN", "Person OUT"], "severity": "low", "cooldownSec": 0})
DEFAULTS["twhelmet"] = dict(copy.deepcopy(_CROSS_COMMON), minBoxFrac=0.05, includeBicycles=False,
                            helmetMinConf=0.5, noHelmetMinConf=0.6, minMissingVotes=3, minHelmetVotes=2,
                            judgeEvery=2, maxJudgePerFrame=2,
                            snapshot={"mode": "crossing"},
                            alert={"enabled": True, "labels": ["Helmet compliant", "Helmet missing"], "severity": "high",
                                   "compliantSeverity": "low", "cooldownSec": 5})

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
for _m in ("vehicle", "human", "count", "object"):
    DEFAULTS[_m]["schedule"] = {"mode": config.SCHEDULE_MODE if config.SCHEDULE_MODE in ("adaptive", "interval", "every") else "adaptive",
                                "idleCheckSec": config.IDLE_CHECK_SEC, "idleAfterSec": config.IDLE_AFTER_SEC,
                                "motionMinAreaPct": config.MOTION_MIN_AREA_PCT, **DEFAULTS[_m]["schedule"]}
SCHEDULE_LIMITS = {"idleCheckSec": (1, 600, False), "idleAfterSec": (1, 600, False), "intervalSec": (0, 600, False), "captureFps": (0, 60, False), "imgSize": (160, 1920, True),
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
        elif k == "mode":
            if v not in ("adaptive", "interval", "every"):
                errs.append("schedule.mode must be adaptive, interval or every")
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
    if module_id in ("fire", "object", "ppe", "twhelmet", "pcross"):
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
    if module_id == "ppe" and any(k in stored for k in ("rule", "alertOnMissing", "classes")):
        out.pop("confidence", None)              # the old placeholder form's 0.5 is not the detector's threshold
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


def _validate_ppe(cfg, camera_zones):
    errs = []
    _num(errs, "confidence", cfg.get("confidence"), 0.05, 0.99)
    _num(errs, "imgSize", cfg.get("imgSize"), 320, 1280, True)
    if not errs and int(cfg["imgSize"]) % 32:
        errs.append("imgSize must be a multiple of 32 (e.g. 480, 640, 960)")
    _num(errs, "intervalSec", cfg.get("intervalSec"), 0.2, 60)
    _num(errs, "missAfter", cfg.get("missAfter"), 1, 30, True)
    _num(errs, "minPersonFrac", cfg.get("minPersonFrac"), 0.02, 0.9)
    _num(errs, "repeatSec", cfg.get("repeatSec"), 0, 86400)
    req = cfg.get("required") or {}
    if [k for k in req if k not in ("helmet", "vest", "gloves")]:
        errs.append("required items are helmet, vest and gloves")
    if cfg.get("enabled") and not any(req.get(k, True) for k in ("helmet", "vest", "gloves")):
        errs.append("require at least one PPE item")
    if (cfg.get("vestColor") or "") not in PPE_VEST_COLORS:
        errs.append("vestColor must be one of: " + ", ".join(c or '"" (any)' for c in PPE_VEST_COLORS))
    alert = cfg.get("alert") or {}
    if alert.get("severity") not in SEVERITIES:
        errs.append("alert.severity must be one of " + ", ".join(SEVERITIES))
    if any(lbl not in MODULES["ppe"]["labels"] for lbl in alert.get("labels") or []):
        errs.append("alert.labels contains unknown PPE alerts")
    zone_ids = {z.get("id") for z in camera_zones or []}
    bad = [z for z in cfg.get("zones") or [] if z not in zone_ids]
    if bad:
        errs.append("zones not defined on this camera: " + ", ".join(map(str, bad)))
    return errs


def _validate_cross(module_id, cfg):
    errs = []
    ln = cfg.get("line")
    if ln is not None:
        try:
            pts = [float(ln[k]) for k in ("x1", "y1", "x2", "y2")]
            if any(not 0.0 <= v <= 1.0 for v in pts):
                errs.append("line coordinates must be between 0 and 1 (fractions of the picture)")
            elif ((pts[2] - pts[0]) ** 2 + (pts[3] - pts[1]) ** 2) ** 0.5 < 0.02:
                errs.append("the line is too short: draw it longer")
            for k in ("refWidth", "refHeight"):
                if ln.get(k) is not None and float(ln[k]) <= 0:
                    errs.append(f"line.{k} must be positive")
        except (KeyError, TypeError, ValueError):
            errs.append("line needs numeric x1, y1, x2, y2")
    if cfg.get("direction") not in ("both", "in", "out"):
        errs.append("direction must be both, in or out")
    if cfg.get("refPoint") not in ("bottom", "center"):
        errs.append("refPoint must be bottom or center")
    _num(errs, "confidence", cfg.get("confidence"), 0.05, 0.99)
    _num(errs, "targetFps", cfg.get("targetFps"), 1, 30)
    _num(errs, "minTrackFrames", cfg.get("minTrackFrames"), 1, 30, True)
    _num(errs, "hysteresis", cfg.get("hysteresis"), 0, 0.3)
    _num(errs, "recrossSec", cfg.get("recrossSec"), 0, 60)
    _num(errs, "handoverSec", cfg.get("handoverSec"), 0, 5)
    _num(errs, "dedupeSec", cfg.get("dedupeSec"), 0, 10)
    _num(errs, "minBoxFrac", cfg.get("minBoxFrac"), 0.005, 0.5)
    if module_id == "twhelmet":
        _num(errs, "helmetMinConf", cfg.get("helmetMinConf"), 0.25, 0.99)
        _num(errs, "noHelmetMinConf", cfg.get("noHelmetMinConf"), 0.25, 0.99)
        _num(errs, "minMissingVotes", cfg.get("minMissingVotes"), 1, 8, True)
        _num(errs, "minHelmetVotes", cfg.get("minHelmetVotes"), 1, 8, True)
        _num(errs, "judgeEvery", cfg.get("judgeEvery"), 1, 10, True)
        _num(errs, "maxJudgePerFrame", cfg.get("maxJudgePerFrame"), 1, 6, True)
    alert = cfg.get("alert") or {}
    if alert.get("severity") not in SEVERITIES:
        errs.append("alert.severity must be one of " + ", ".join(SEVERITIES))
    if alert.get("compliantSeverity", "low") not in SEVERITIES:
        errs.append("alert.compliantSeverity must be one of " + ", ".join(SEVERITIES))
    if any(lbl not in MODULES[module_id]["labels"] for lbl in alert.get("labels") or []):
        errs.append("alert.labels contains unknown alerts")
    _num(errs, "alert.cooldownSec", alert.get("cooldownSec", 0), 0, 3600)
    if (cfg.get("snapshot") or {}).get("mode") not in ("crossing", "off"):
        errs.append("snapshot.mode must be crossing or off")
    return errs


def validate(module_id, cfg, camera_zones):
    """Return a list of human-readable errors (empty = valid)."""
    if module_id in ("twhelmet", "pcross"):
        return _validate_cross(module_id, cfg)
    if module_id == "ppe":
        return _validate_ppe(cfg, camera_zones)
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
    if cfg.get("enabled") and not any(classes.get(k, True) for k in m["classKeys"] if k not in OPTION_KEYS):
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
