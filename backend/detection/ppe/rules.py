"""PPE compliance rules: person-first assessment of helmet, vest and gloves (pure logic, no model).

Gates, body regions, confidence bands, helmet colour (HSV) and the helmet-crop classifier verdicts come from the
"Realtime-Edge-PPE-Detection" project (src/edgeppe/config.py, color.py, monitor.py), measured there on its own
data. What the models can and cannot tell is in backend/models/ppe/README.md; this module never claims more:

    PRESENT          detected with enough evidence (decide-level confidence, right body zone, helmet classifier says
                     "construction")
    MISSING          the body zone is visible and assessable, and the PPE is absent: the detector reports the absence
                     class (no_helmet / no_gloves) at decide level, OR (helmet, vest only) nothing was found on a large,
                     fully visible person in `missAfter` consecutive assessments; a road (motorcycle / bicycle) helmet
                     counts as a missing construction helmet
    UNCERTAIN        evidence in between (review band, unknown helmet type, too few assessments yet)
    NOT_ASSESSABLE   zone outside the picture / person too small; gloves: no glove evidence either way (the detector
                     has no hand / bare-hand class, so an unseen glove is never called missing)
"""
from collections import Counter, deque

import cv2
import numpy as np

PRESENT, MISSING, UNCERTAIN, NOT_ASSESSABLE = "PRESENT", "MISSING", "UNCERTAIN", "NOT_ASSESSABLE"
COMPLIANT, NON_COMPLIANT, NEEDS_REVIEW, PARTIAL = "COMPLIANT", "NON_COMPLIANT", "NEEDS_REVIEW", "NOT_FULLY_ASSESSED"
ITEMS = ("helmet", "vest", "gloves")

# ---- from edgeppe/config.py (measured on the detector's data) ---------------------------------------------------
CLASS_CONF = {                       # (low, high): below low junk, between review (uncertain), at/above high decides
    "helmet": (0.25, 0.65), "no_helmet": (0.40, 0.60),      # any helmet evidence >= 0.25 blocks a MISSING
    "vest": (0.35, 0.50),
    "gloves": (0.40, 0.60), "no_gloves": (0.45, 0.60),
}
REGIONS = {                          # box centre as (top, bottom) fraction of the person box height
    "helmet": (-0.10, 0.45), "no_helmet": (-0.10, 0.45),   # wide enough for head-and-shoulder (cut) person boxes
    "vest": (0.15, 0.85),
    "gloves": (0.20, 0.90), "no_gloves": (0.15, 0.95),
}
HELMET_SUPPRESS_IOU = 0.5            # no_helmet on the same head as a helmet = false alarm
MIN_BOX_H_FRAC = 0.02                # item boxes lower than 2 % of the frame are too small to decide
PERSON_MIN_CONF = 0.45
PERSON_MIN_ASPECT = 0.9              # person box height / width (bike frames come back as wide "persons")
PERSON_NMS_IOU = 0.5
HELMET_CLS_MIN = 0.70                # classifier P(construction) / P(road) needed for a verdict
HELMET_VOTES = 15                    # helmet colour = majority of a person's last N votes
HELMET_HSV = {
    "top_fraction": 0.6, "min_pixels": 12, "v_dark": 40, "s_gray": 60, "v_gray_max": 170,
    "white_s_max": 55, "white_v_min": 150, "color_s_min": 60,
    "red_hi": 6, "red_lo": 168, "orange_hi": 22, "yellow_hi": 35, "green_hi": 85, "blue_hi": 135,
}
COLOR_MIN_SIDE = 12                  # helmet / vest crops smaller than this (px) -> colour "unknown"
COLOR_MIN_SHARE = 0.55               # the winning hue must cover this share of the usable pixels, else "unknown"
# hi-vis "fluorescent yellow" (yellow-green) vests measure hue 34-40 on real site photos -> yellow up to 45
VEST_HUES = (("red", 0, 6), ("orange", 6, 22), ("yellow", 22, 45), ("green", 45, 85), ("blue", 85, 135))


def iou(a, b):
    x1, y1, x2, y2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


# ---- colour (edgeppe/color.py, plus "unknown" when the evidence is thin) ------------------------------------------
def _usable(crop, top_fraction):
    if crop is None or crop.ndim != 3 or min(crop.shape[:2]) < COLOR_MIN_SIDE:
        return None
    rows = max(1, int(crop.shape[0] * top_fraction))
    hsv = cv2.cvtColor(crop[:rows], cv2.COLOR_BGR2HSV)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    grey = (s < HELMET_HSV["s_gray"]) & (v < HELMET_HSV["v_gray_max"])
    keep = (v >= HELMET_HSV["v_dark"]) & ~grey
    if int(np.count_nonzero(keep)) < HELMET_HSV["min_pixels"]:
        return None
    return h[keep].astype(np.int32), s[keep].astype(np.int32), v[keep].astype(np.int32)


def _hue_name(h):
    c = HELMET_HSV
    if h <= c["red_hi"] or h >= c["red_lo"]:
        return "red"
    if h <= c["orange_hi"]:
        return "orange"
    if h <= c["yellow_hi"]:
        return "yellow"
    if h <= c["green_hi"]:
        return "green"
    if h <= c["blue_hi"]:
        return "blue"
    return "other"


def helmet_color(crop):
    """white / yellow / orange / red / green / blue / other / unknown, from the top 60 % of the helmet box only
    (never the shirt, face or background). unknown = too small, too dark, or no dominant colour."""
    px = _usable(crop, HELMET_HSV["top_fraction"])
    if px is None:
        return "unknown"
    h, s, v = px
    white = (s < HELMET_HSV["white_s_max"]) & (v >= HELMET_HSV["white_v_min"])
    names = np.where(white, "white", np.where(s < HELMET_HSV["color_s_min"], "low", np.vectorize(_hue_name)(h)))
    counts = Counter(names.tolist())
    counts.pop("low", None)
    if not counts:
        return "unknown"
    name, n = counts.most_common(1)[0]
    if n / max(1, len(names)) < COLOR_MIN_SHARE:
        return "unknown"
    return name


def vest_color(crop):
    """Dominant colour of the vest box (central part, saturated pixels): orange / yellow / green / red / blue /
    other / unknown. Only ever asked AFTER the detector called the garment a vest - colour never makes a vest."""
    if crop is None or crop.ndim != 3 or min(crop.shape[:2]) < COLOR_MIN_SIDE:
        return "unknown"
    h0, w0 = crop.shape[:2]
    core = crop[int(h0 * 0.15):int(h0 * 0.85), int(w0 * 0.2):int(w0 * 0.8)]
    px = _usable(core, 1.0)
    if px is None:
        return "unknown"
    h, s, _v = px
    sat = s >= HELMET_HSV["color_s_min"]
    if int(np.count_nonzero(sat)) < HELMET_HSV["min_pixels"]:
        return "unknown"
    counts = Counter(_vest_hue_name(int(x)) for x in h[sat])
    name, n = counts.most_common(1)[0]
    return name if n / max(1, int(np.count_nonzero(sat))) >= COLOR_MIN_SHARE else "unknown"


def _vest_hue_name(h):
    if h >= HELMET_HSV["red_lo"]:
        return "red"
    for name, lo, hi in VEST_HUES:
        if lo <= h < hi:
            return name
    return "other"


# ---- detection gates (edgeppe/monitor.py) -------------------------------------------------------------------------
def filter_persons(dets, min_conf=PERSON_MIN_CONF):
    """dets: list of dicts {cls, box, conf, tid}. Weak (< min_conf), too-wide and duplicate person boxes are dropped."""
    out, persons = [], []
    for d in dets:
        if d["cls"] != "Person":
            out.append(d)
            continue
        x1, y1, x2, y2 = d["box"]
        if d["conf"] < min_conf or (y2 - y1) < PERSON_MIN_ASPECT * max(x2 - x1, 1.0):
            continue
        persons.append(d)
    kept = []
    for d in sorted(persons, key=lambda d: -d["conf"]):
        if all(iou(d["box"], k["box"]) < PERSON_NMS_IOU for k in kept):
            kept.append(d)
    return out + kept


def suppress_helmet_conflicts(dets):
    helmets = [d["box"] for d in dets if d["cls"] == "helmet"]
    return [d for d in dets if d["cls"] != "no_helmet" or all(iou(d["box"], h) < HELMET_SUPPRESS_IOU for h in helmets)]


def assign_person(box, persons, cls=None):
    """Index of the person an item belongs to, or None (an item that belongs to nobody is never given to the nearest
    person). Candidates: person boxes containing the item centre (and, for a class with a body region, the item inside
    that region). In a crowd the best fit wins: helmets by head position (top-centre of the person box), other
    items by the most specific (smallest) box."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    hits = []
    for i, p in enumerate(persons):
        x1, y1, x2, y2 = p["box"]
        if not (x1 <= cx <= x2 and y1 <= cy <= y2):
            continue
        if cls is not None and not region_ok(cls, box, p["box"]):
            continue
        pw, ph = max(x2 - x1, 1.0), max(y2 - y1, 1.0)
        if cls in ("helmet", "no_helmet"):
            score = abs(cx - (x1 + x2) / 2) / pw + abs(box[1] - y1) / ph
        else:
            score = pw * ph
        hits.append((score, i))
    return min(hits)[1] if hits else None


def region_ok(cls, box, pbox):
    band = REGIONS.get(cls)
    if band is None:
        return True
    frac = ((box[1] + box[3]) / 2 - pbox[1]) / max(pbox[3] - pbox[1], 1.0)
    return band[0] <= frac <= band[1]


# ---- one assessment of one person ---------------------------------------------------------------------------------
def visibility(pbox, frame_hw, min_person_frac):
    """Which body zones can be judged at all: head (helmet), torso (vest). A person too small or cut by the picture
    edge is not assessable in that zone."""
    fh, fw = frame_hw
    x1, y1, x2, y2 = pbox
    ph = y2 - y1
    big = ph >= min_person_frac * fh
    edge = 0.01 * fh
    head = big and y1 > edge                                  # top of the person inside the picture
    torso = big and (y2 < fh - edge or ph >= 0.35 * fh)       # bottom cut is fine when the upper body is large
    return {"head": head, "torso": torso}


def assess_person(person, items, frame, cfg, helmet_kind=None):
    """Per-frame evidence for one person. items: detections assigned to this person (dicts with cls/box/conf).
    helmet_kind(frame, box) -> ("construction" | "road" | "other", prob) or None (no classifier).
    Returns {item: {"obs": PRESENT|MISSING|UNCERTAIN|NOT_ASSESSABLE|NOHIT, "conf", "box", "reason", "color"}}."""
    fh, fw = frame.shape[:2]
    vis = visibility(person["box"], (fh, fw), cfg["minPersonFrac"])
    by = {}
    for d in items:
        if not region_ok(d["cls"], d["box"], person["box"]):
            continue
        if (d["box"][3] - d["box"][1]) < MIN_BOX_H_FRAC * fh:
            d = dict(d, small=True)
        if d["cls"] not in by or d["conf"] > by[d["cls"]]["conf"]:
            by[d["cls"]] = d
    out = {}

    # helmet
    h, nh = by.get("helmet"), by.get("no_helmet")
    lo, hi = CLASS_CONF["helmet"]
    if h and h["conf"] >= hi and not h.get("small"):
        kind, prob = helmet_kind(frame, h["box"]) if helmet_kind else ("unverified", None)
        crop = _crop(frame, h["box"])
        color = helmet_color(crop)
        if kind == "construction":
            out["helmet"] = _ev(PRESENT, h, "construction helmet", color, kind, prob)
        elif kind == "road":
            out["helmet"] = _ev(MISSING, h, "road (motorcycle / bicycle) helmet - not construction PPE", color, kind, prob)
        elif kind == "unverified":
            out["helmet"] = _ev(PRESENT, h, "helmet (type not verified: classifier off)", color, kind, prob)
        else:
            out["helmet"] = _ev(UNCERTAIN, h, "helmet type could not be verified", color, kind, prob)
    elif h and h["conf"] >= lo:
        out["helmet"] = _ev(UNCERTAIN, h, "weak helmet detection", "unknown")
    elif nh and nh["conf"] >= CLASS_CONF["no_helmet"][1] and vis["head"] and not nh.get("small"):
        out["helmet"] = _ev(MISSING, nh, "no helmet (detector)")
    elif nh and nh["conf"] >= CLASS_CONF["no_helmet"][0]:
        out["helmet"] = _ev(UNCERTAIN, nh, "possible missing helmet (weak)")
    elif vis["head"]:
        out["helmet"] = _ev("NOHIT", None, "no helmet found on the visible head")
    else:
        out["helmet"] = _ev(NOT_ASSESSABLE, None, "head not visible / person too small")

    # vest
    v = by.get("vest")
    lo, hi = CLASS_CONF["vest"]
    if v and v["conf"] >= hi and not v.get("small"):
        color = vest_color(_crop(frame, v["box"]))
        out["vest"] = _ev(PRESENT, v, "safety vest", color)
    elif v and v["conf"] >= lo:
        out["vest"] = _ev(UNCERTAIN, v, "weak vest detection", "unknown")
    elif vis["torso"]:
        out["vest"] = _ev("NOHIT", None, "no vest found on the visible torso")
    else:
        out["vest"] = _ev(NOT_ASSESSABLE, None, "torso not visible / person too small")

    # gloves: per person (the detector has no hand / bare-hand class: per-hand left / right is not possible)
    g, ng = by.get("gloves"), by.get("no_gloves")
    if g and g["conf"] >= CLASS_CONF["gloves"][1] and not g.get("small"):
        n = sum(1 for d in items if d["cls"] == "gloves" and d["conf"] >= CLASS_CONF["gloves"][1]
                and region_ok("gloves", d["box"], person["box"]))
        out["gloves"] = _ev(PRESENT, g, f"{n} glove{'s' if n != 1 else ''} detected (per-hand check not supported)")
        out["gloves"]["count"] = n
    elif ng and ng["conf"] >= CLASS_CONF["no_gloves"][1] and not ng.get("small"):
        out["gloves"] = _ev(MISSING, ng, "bare hand (detector no_gloves)")
    elif (g and g["conf"] >= CLASS_CONF["gloves"][0]) or (ng and ng["conf"] >= CLASS_CONF["no_gloves"][0]):
        out["gloves"] = _ev(UNCERTAIN, g or ng, "weak glove / bare-hand detection")
    else:
        out["gloves"] = _ev(NOT_ASSESSABLE, None, "hands not visible or not recognisable")
    return out


def _ev(obs, det, reason, color=None, kind=None, prob=None):
    return {"obs": obs, "conf": round(det["conf"], 3) if det else None, "box": list(det["box"]) if det else None,
            "reason": reason, "color": color, "kind": kind, "kindProb": round(prob, 3) if prob is not None else None}


def _crop(frame, box, pad=0.0):
    fh, fw = frame.shape[:2]
    bw, bh = box[2] - box[0], box[3] - box[1]
    x1, y1 = max(0, int(box[0] - bw * pad)), max(0, int(box[1] - bh * pad))
    x2, y2 = min(fw, int(box[2] + bw * pad)), min(fh, int(box[3] + bh * pad))
    return frame[y1:y2, x1:x2] if x2 > x1 and y2 > y1 else None


# ---- state over time for one tracked person -----------------------------------------------------------------------
class PersonState:
    """Turns per-frame evidence into a stable status per item. A missing item needs `missAfter` assessments in a row
    (MISSING / NOHIT evidence); PRESENT resets it. Helmet colour is the majority of the last HELMET_VOTES votes."""

    def __init__(self, tid):
        self.tid = tid
        self.miss = Counter()
        self.status = {i: {"status": NOT_ASSESSABLE, "reason": "not assessed yet"} for i in ITEMS}
        self.helmet_votes = deque(maxlen=HELMET_VOTES)
        self.vest_votes = deque(maxlen=HELMET_VOTES)
        self.assessments = 0
        self.last_seen = 0.0
        self.box = None
        self.alerted = {}            # item/kind -> time of the last alert (dedupe + repeat)
        self.review_alerted = False

    def update(self, ev, cfg, now):
        self.assessments += 1
        self.last_seen = now
        need = max(1, int(cfg["missAfter"]))
        for item in ITEMS:
            e = ev[item]
            obs = e["obs"]
            if obs in (MISSING, "NOHIT"):
                self.miss[item] += 1
            elif obs == PRESENT:
                self.miss[item] = 0
            # NOHIT only becomes MISSING for helmet / vest when absence counting is on
            countable = obs == MISSING or (obs == "NOHIT" and cfg.get("absenceMeansMissing", True) and item in ("helmet", "vest"))
            if obs in (MISSING, "NOHIT") and countable and self.miss[item] >= need:
                st = MISSING
                reason = e["reason"] + (f" ({self.miss[item]} checks in a row)" if obs == "NOHIT" else "")
            elif obs in (MISSING, "NOHIT"):
                st = UNCERTAIN if countable else NOT_ASSESSABLE
                reason = e["reason"] + (f" - checking {self.miss[item]}/{need}" if countable else "")
            else:
                st, reason = obs, e["reason"]
            rec = {"status": st, "reason": reason, "confidence": e["conf"], "box": e["box"]}
            if item == "helmet":
                if e.get("color") and e["obs"] in (PRESENT, MISSING, UNCERTAIN) and e["color"] != "unknown":
                    self.helmet_votes.append(e["color"])
                rec["color"] = Counter(self.helmet_votes).most_common(1)[0][0] if self.helmet_votes else "unknown"
                rec["type"] = e.get("kind")
            if item == "vest":
                if e.get("color") and e["color"] != "unknown" and e["obs"] == PRESENT:
                    self.vest_votes.append(e["color"])
                rec["color"] = Counter(self.vest_votes).most_common(1)[0][0] if self.vest_votes else (e.get("color") or "unknown")
                want = (cfg.get("vestColor") or "").strip().lower()
                if st == PRESENT and want:
                    rec["expectedColor"] = want
                    rec["colorCompliance"] = ("uncertain" if rec["color"] == "unknown" else
                                              "compliant" if rec["color"] == want else "non-compliant")
            if item == "gloves" and "count" in e:
                rec["count"] = e["count"]
            self.status[item] = rec

    def overall(self, required):
        st = [self.status[i]["status"] for i in ITEMS if required.get(i, True)]
        vest = self.status["vest"]
        if MISSING in st or (required.get("vest", True) and vest.get("colorCompliance") == "non-compliant"):
            return NON_COMPLIANT
        if UNCERTAIN in st or (required.get("vest", True) and vest.get("colorCompliance") == "uncertain"):
            return NEEDS_REVIEW
        core = [self.status[i]["status"] for i in ("helmet", "vest") if required.get(i, True)]
        if core and all(s == PRESENT for s in core):
            return COMPLIANT          # gloves NOT_ASSESSABLE does not block (cannot be judged, not missing)
        return PARTIAL

    def view(self, required):
        return {"personId": self.tid, "items": {i: dict(self.status[i]) for i in ITEMS},
                "overall": self.overall(required), "assessments": self.assessments, "box": self.box}


def alerts_due(ps, required, cfg, now):
    """(kind, label, item, reason) alerts to raise for this person now: each confirmed missing item (and a non-orange
    vest) once, again only after it was resolved or after repeatSec; optional review warning once per person."""
    out = []
    repeat = float(cfg.get("repeatSec", 600) or 0)
    for item in ITEMS:
        if not required.get(item, True):
            continue
        rec = ps.status[item]
        key = item
        if rec["status"] == MISSING:
            label = {"helmet": "Helmet missing", "vest": "Vest missing", "gloves": "Gloves missing"}[item]
            if item == "helmet" and rec.get("type") == "road":
                label = "Road helmet (not construction)"
            last = ps.alerted.get(key)
            if last is None or (repeat > 0 and now - last >= repeat):
                ps.alerted[key] = now
                out.append(("ppe_missing", label, item, rec["reason"]))
        elif rec["status"] == PRESENT:
            ps.alerted.pop(key, None)          # resolved: a new MISSING later is a new event
    vest = ps.status["vest"]
    if required.get("vest", True) and vest.get("colorCompliance") == "non-compliant":
        last = ps.alerted.get("vestColor")
        if last is None or (repeat > 0 and now - last >= repeat):
            ps.alerted["vestColor"] = now
            out.append(("ppe_vest_color", f"Vest {vest['color']} (required {vest['expectedColor']})", "vest",
                        f"safety vest is {vest['color']}, site policy requires {vest['expectedColor']}"))
    elif vest.get("colorCompliance") == "compliant":
        ps.alerted.pop("vestColor", None)
    if cfg.get("reviewWarnings") and not ps.review_alerted and ps.overall(required) == NEEDS_REVIEW and ps.assessments >= int(cfg["missAfter"]):
        ps.review_alerted = True
        items = [i for i in ITEMS if required.get(i, True) and ps.status[i]["status"] == UNCERTAIN]
        out.append(("ppe_review", "PPE needs review", ",".join(items), "; ".join(f"{i}: {ps.status[i]['reason']}" for i in items)))
    return out
