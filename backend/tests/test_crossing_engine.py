"""Line-crossing engine: queue / freshness, rider association, helmet verdict rules, event content.
SCRIPTED detections are used where a test is about plumbing or rules (clearly named `Scripted`); the final class runs the
REAL detector on a real photo moved across a canvas (simulated motion, real pixels). Nothing here measures accuracy.
Run: python3 -m unittest backend.tests.test_crossing_engine"""
import copy
import os
import sys
import threading
import time
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import backend  # noqa: E402,F401
from backend import config  # noqa: E402
from backend.detection.crossing import engine as ce  # noqa: E402
from backend.detection.crossing import judge as jd  # noqa: E402
from backend.detection.modules import DEFAULTS, validate  # noqa: E402

W, H = 1280, 720
LINE = {"x1": 0.1, "y1": 0.5, "x2": 0.9, "y2": 0.5, "refWidth": W, "refHeight": H}


def cfg(mod, **kw):
    c = copy.deepcopy(DEFAULTS[mod])
    c["line"] = dict(LINE)
    c.update(kw)
    return c


class ScriptedEngine(ce.CrossingEngine):
    """Engine without a model: `script(frame_id)` returns the detections of that frame."""

    def __init__(self, on_event, script, delay=0.0, depth_max=1, delays=None):
        self._script, self._delay, self._delays = script, delay, delays
        ce.load_model, self._orig = (lambda: (object(), "pytorch", 640, Path("scripted.pt"))), ce.load_model
        try:
            super().__init__(on_event, camera_id="t", depth_max=depth_max)
        finally:
            ce.load_model = self._orig
        self.assoc_order = []

    def _infer(self, frame, mods, model=None):
        i = int(frame[0, 0, 0])                                # frame id is stored in pixel [0,0,0] by `frame(i)`
        d = self._delays(i) if self._delays else self._delay
        if d:
            time.sleep(d)
        return (i, self._script(i)), 1.0

    def _assoc(self, raw, frame):
        self.assoc_order.append(raw[0])
        return raw[1]


def frame(i):
    f = np.full((H, W, 3), 100, np.uint8)
    f[0, 0, 0] = i % 250
    return f


def person(tid, x, foot, h=300, w=110):
    return {"tid": tid, "cls": "person", "box": [x - w / 2, foot - h, x + w / 2, foot], "conf": 0.9}


def bike(tid, x, foot, h=220, w=240, cls="motorcycle"):
    return {"tid": tid, "cls": cls, "box": [x - w / 2, foot - h, x + w / 2, foot], "conf": 0.9}


def rider_on(bk, tid):
    x = (bk["box"][0] + bk["box"][2]) / 2
    return person(tid, x, bk["box"][3] - 70, h=330, w=120)          # person box reaching down over the vehicle


class Collect:
    def __init__(self):
        self.ev, self.lock = [], threading.Lock()

    def __call__(self, mod, ev, snap, fr):
        with self.lock:
            self.ev.append((mod, ev, snap, fr))

    def wait(self, n, t=5.0):
        end = time.time() + t
        while time.time() < end and len(self.ev) < n:
            time.sleep(0.02)
        return list(self.ev)


def run(eng, frames, t0=1000.0, dt=0.1, sleep=0.0):
    for i in range(frames):
        eng._last_submit = 0.0
        eng.process(frame(i), t0 + i * dt, i)
        if sleep:
            time.sleep(sleep)


class ConfigTests(unittest.TestCase):
    def test_defaults_valid_and_line_checked(self):
        for m in ("twhelmet", "pcross"):
            self.assertEqual(validate(m, copy.deepcopy(DEFAULTS[m]), []), [])
            self.assertEqual(validate(m, cfg(m), []), [])
        bad = cfg("pcross")
        bad["line"]["x2"] = 1.5
        self.assertTrue(validate("pcross", bad, []))
        bad = cfg("pcross", direction="sideways")
        self.assertTrue(validate("pcross", bad, []))
        bad = cfg("pcross")
        bad["line"].update(x1=0.5, x2=0.5, y1=0.5, y2=0.505)
        self.assertTrue(validate("pcross", bad, []))        # too short

    def test_line_is_per_camera_normalised(self):
        from backend.detection.crossing.geometry import Line
        ln = Line.from_cfg(cfg("pcross"))
        self.assertEqual(ln.px(1280, 720), (128.0, 360.0, 1152.0, 360.0))
        self.assertEqual(ln.px(640, 360), (64.0, 180.0, 576.0, 180.0))      # same line on a substream
        self.assertTrue(ln.aspect_mismatch(640, 480))


class AssociationTests(unittest.TestCase):
    def test_rider_pillion_and_pedestrian(self):
        b1 = bike(1, 400, 500)
        persons = [rider_on(b1, 10), person(11, 405, 330, h=120, w=60), person(12, 900, 500)]     # driver, pillion (higher), pedestrian far away
        r = ce.associate_riders(persons, [b1])
        self.assertEqual(sorted(r[0]), [0, 1])              # driver and pillion belong to the bike
        self.assertNotIn(2, r[0])                           # the pedestrian far away never does

    def test_person_next_to_parked_bike_is_not_a_rider(self):
        b1 = bike(1, 400, 500)
        self.assertEqual(ce.associate_riders([person(5, 700, 500)], [b1]), {})

    def test_person_belongs_to_one_bike(self):
        b1, b2 = bike(1, 400, 500), bike(2, 470, 520)
        r = ce.associate_riders([rider_on(b1, 10)], [b1, b2])
        self.assertEqual(sum(len(v) for v in r.values()), 1)


class JudgeRuleTests(unittest.TestCase):
    def test_votes(self):
        self.assertEqual(jd.decide([jd.MISSING]), jd.UNCERTAIN)
        self.assertEqual(jd.decide([jd.MISSING] * 3), jd.MISSING)
        self.assertEqual(jd.decide([jd.MISSING] * 5 + [jd.HELMET]), jd.UNCERTAIN)      # one helmet vote blocks MISSING
        self.assertEqual(jd.decide([jd.HELMET] * 2), jd.HELMET)
        self.assertEqual(jd.decide([jd.HELMET, jd.MISSING, jd.MISSING]), jd.UNCERTAIN)

    def test_two_views_needed_for_missing(self):
        # the stock PPE detector rules; a rider_helmet.pt in backend/models/twhelmet must not change this test
        saved = jd._STATE["custom"]
        jd._STATE["custom"] = False
        self.addCleanup(lambda: jd._STATE.__setitem__("custom", saved))
        top = np.zeros((200, 120, 3), np.uint8)
        no_helm = lambda crop: (0.0, 0.8)
        two_views = lambda crop: [[10, 10, 90, 100, 0.9]]
        self.assertEqual(jd.judge_crop(top, classify=no_helm, heads=two_views)["verdict"], jd.MISSING)
        self.assertEqual(jd.judge_crop(top, classify=no_helm, heads=lambda c: [])["verdict"], jd.UNCERTAIN)    # one view only
        self.assertEqual(jd.judge_crop(top, classify=lambda c: (0.8, 0.0), heads=two_views)["verdict"], jd.HELMET)
        self.assertEqual(jd.judge_crop(top, classify=lambda c: (0.3, 0.3), heads=two_views)["verdict"], jd.UNCERTAIN)
        mixed = iter([(0.0, 0.8), (0.7, 0.0)])
        self.assertEqual(jd.judge_crop(top, classify=lambda c: next(mixed), heads=two_views)["verdict"], jd.UNCERTAIN)  # views disagree

    def test_custom_model_one_clear_view_is_enough(self):
        saved = jd._STATE["custom"]
        jd._STATE["custom"] = True
        self.addCleanup(lambda: jd._STATE.__setitem__("custom", saved))
        top = np.zeros((200, 120, 3), np.uint8)
        self.assertEqual(jd.judge_crop(top, classify=lambda c: (0.0, 0.8), heads=lambda c: [])["verdict"], jd.MISSING)
        self.assertEqual(jd.judge_crop(top, classify=lambda c: (0.8, 0.0), heads=lambda c: [])["verdict"], jd.HELMET)

    def test_plate_only_reported_when_the_model_can_see_plates(self):
        frame = np.zeros((300, 400, 3), np.uint8)
        st = dict(jd._STATE)
        self.addCleanup(lambda: (jd._STATE.clear(), jd._STATE.update(st)))
        jd._STATE.update(tried=True, model=None, custom=False, names={})
        self.assertIsNone(jd.find_plate(frame, [50, 50, 200, 250]))            # stock detector: unknown, not "no plate"

        class Box:
            def __init__(s, c, conf, xyxy): s.cls, s.conf, s.xyxy = c, conf, [np.array(xyxy)]

        class Res:
            def __init__(s, boxes): s.boxes = boxes

        class Model:
            names = {0: "bike", 1: "helmet", 2: "no-helmet", 3: "number-plate"}
            out = []
            def predict(s, crop, **kw): return [Res(s.out)]

        m = Model()
        jd._STATE.update(model=m, custom=True, names=dict(m.names))
        r = jd.find_plate(frame, [50, 50, 200, 250])
        self.assertEqual(r, {"detected": False, "conf": None, "box": None})
        m.out = [Box(3, 0.6, [10, 20, 40, 35]), Box(1, 0.9, [0, 0, 5, 5])]
        r = jd.find_plate(frame, [50, 50, 200, 250])
        self.assertTrue(r["detected"])
        self.assertEqual(r["box"], [round(35 + 10, 1), round(30 + 20, 1), round(35 + 40, 1), round(30 + 35, 1)])

    def test_stock_judge_is_declared_unvalidated(self):
        st = jd.status()
        self.assertFalse(st["validated"])


class ZoneTests(unittest.TestCase):
    """Detect only around the line: the crop must be the right rectangle and the boxes must come back in full-picture pixels."""

    def _engine(self, **kw):
        eng = ScriptedEngine(Collect(), lambda i: [])
        eng.set_module("pcross", cfg("pcross", **kw))
        return eng

    def test_zone_rectangle(self):
        eng = self._engine()
        mods = list(eng.mods.values())
        try:
            self.assertIsNone(eng._zone(mods, W, H))                          # off by default
            mods[0].cfg.update(zoneAroundLine=True, zoneMargin=0.2)
            ln = mods[0].cfg["line"]
            x1, y1, x2, y2 = eng._zone(mods, W, H)
            self.assertLessEqual(x1, min(ln["x1"], ln["x2"]) * W)
            self.assertGreaterEqual(x2, max(ln["x1"], ln["x2"]) * W)
            self.assertTrue(0 <= x1 < x2 <= W and 0 <= y1 < y2 <= H)
            mods[0].cfg.update(zoneMargin=1.0)
            self.assertIsNone(eng._zone(mods, W, H))                          # (nearly) the whole picture anyway
            mods[0].cfg.update(zoneMargin=0.2, line=None)
            self.assertIsNone(eng._zone(mods, W, H))
        finally:
            eng.close()

    def test_boxes_come_back_in_full_picture_pixels(self):
        from ultralytics.engine.results import Boxes
        seen = {}

        class Res:
            def __init__(s, boxes): s.boxes = boxes

        class Model:
            names = {0: "person", 3: "motorcycle"}
            def predict(s, img, **kw):
                seen["shape"] = img.shape[:2]
                return [Res(Boxes(np.array([[10, 20, 50, 80, 0.9, 3]], np.float32), img.shape[:2]))]

        eng = self._engine(zoneAroundLine=True, zoneMargin=0.2)
        try:
            mods = list(eng.mods.values())
            zone = eng._zone(mods, W, H)
            raw, _ms = ce.CrossingEngine._infer(eng, np.zeros((H, W, 3), np.uint8), mods, model=Model())
            self.assertEqual(seen["shape"], (zone[3] - zone[1], zone[2] - zone[0]))     # the detector got the crop only
            self.assertEqual([round(float(v)) for v in raw.xyxy[0]], [10 + zone[0], 20 + zone[1], 50 + zone[0], 80 + zone[1]])
            self.assertEqual(int(raw.cls[0]), 3)
        finally:
            eng.close()


class BikeAreaFallbackTests(unittest.TestCase):
    def test_box_stretches_upward_and_stays_in_frame(self):
        b = jd.bike_rider_box((100, 50, 200, 150), 1080, 1920)
        self.assertEqual((b[0], b[2], b[3]), (100, 200, 150))
        self.assertEqual(b[1], 0.0)                                   # 50 - 0.7*100 clipped at the frame top
        self.assertEqual(jd.bike_rider_box((100, 400, 200, 500), 1080, 1920)[1], 330.0)

    def test_unlinked_bike_is_judged_by_its_own_area_with_the_rider_model(self):
        bike = {"box": [100, 400, 200, 500], "tid": 1}
        old = dict(jd._STATE)
        try:
            jd._STATE.update(custom=True, tried=True)
            items = ce.CrossingEngine._rider_items([], [], bike, 1080, 1920)
            self.assertEqual([(k, t) for k, t, _b in items], [("bike-area", "bike-area")])
            jd._STATE.update(custom=False)
            Path_ok = jd._custom_expected()
            self.assertFalse(Path_ok)
            self.assertEqual(ce.CrossingEngine._rider_items([], [], bike, 1080, 1920), [])
        finally:
            jd._STATE.clear()
            jd._STATE.update(old)


class ConfirmedCategoryTests(unittest.TestCase):
    """Three classes when the evidence is sufficient, unverified otherwise - the gates that stop false confirmations."""

    def setUp(self):
        self.saved = dict(jd._STATE)
        jd._STATE["custom"] = True
        self.top = np.zeros((200, 120, 3), np.uint8)

    def tearDown(self):
        jd._STATE.clear()
        jd._STATE.update(self.saved)

    def test_no_rider_vote_only_from_bike_area_with_no_head_at_all(self):
        none = lambda c: (0.0, 0.0, {"heads": 0, "tiny": 0, "headPx": 0})
        r = jd.judge_crop(self.top, classify=none, heads=lambda c: [], bike_area=True)
        self.assertEqual((r["verdict"], r["reason"]), (jd.NO_RIDER, "no rider head found on the two-wheeler"))
        r = jd.judge_crop(self.top, classify=none, heads=lambda c: [], bike_area=False)       # a person WAS linked: head hidden
        self.assertEqual((r["verdict"], r["reason"]), (jd.UNCERTAIN, "head not visible"))

    def test_head_near_but_not_on_the_bike_blocks_vehicle_only(self):
        near = lambda c: (0.0, 0.0, {"heads": 0, "tiny": 0, "headPx": 0, "foreign": 1})
        r = jd.judge_crop(self.top, classify=near, heads=lambda c: [], bike_area=True)
        self.assertEqual((r["verdict"], r["reason"]), (jd.UNCERTAIN, "head seen near, not on, the two-wheeler"))

    def test_tiny_head_never_confirms_anything(self):
        tiny = lambda c: (0.0, 0.0, {"heads": 0, "tiny": 1, "headPx": 0})          # _classify dropped a 9 px head
        for ba in (True, False):
            r = jd.judge_crop(self.top, classify=tiny, heads=lambda c: [], bike_area=ba)
            self.assertEqual((r["verdict"], r["reason"]), (jd.UNCERTAIN, "head too small to judge"), ba)

    def test_classify_drops_small_heads_and_lower_boxes(self):
        class Box:
            def __init__(s, c, conf, xyxy): s.cls, s.conf, s.xyxy = c, conf, [np.array(xyxy, float)]

        class Res:
            def __init__(s, boxes): s.boxes = boxes

        class Model:
            out = []
            def predict(s, crop, **kw): return [Res(s.out)]

        m = Model()
        names = {0: "bike", 1: "helmet", 2: "no-helmet", 3: "number-plate"}
        crop = np.zeros((200, 120, 3), np.uint8)
        m.out = [Box(1, 0.9, [10, 10, 20, 20])]                                  # 10 px helmet: too small
        self.assertEqual(jd._classify(m, names, crop, 640, True), (0.0, 0.0, {"heads": 0, "tiny": 1, "headPx": 0, "foreign": 0}))
        m.out = [Box(1, 0.9, [10, 150, 40, 180])]                                # helmet in the lower part (on the lap / mirror)
        self.assertEqual(jd._classify(m, names, crop, 640, True)[2]["heads"], 0)
        m.out = [Box(2, 0.7, [10, 10, 40, 40]), Box(0, 0.9, [0, 0, 120, 200]), Box(3, 0.8, [5, 170, 50, 190])]
        pos, neg, info = jd._classify(m, names, crop, 640, True)
        self.assertEqual((pos, neg, info), (0.0, 0.7, {"heads": 1, "tiny": 0, "headPx": 30, "foreign": 0}))

    def test_head_wider_than_its_bike_is_foreign(self):
        class Box:
            def __init__(s, c, conf, xyxy): s.cls, s.conf, s.xyxy = c, conf, [np.array(xyxy, float)]

        class Res:
            def __init__(s, boxes): s.boxes = boxes

        class Model:
            out = []
            def predict(s, crop, **kw): return [Res(s.out)]

        m = Model()
        names = {0: "bike", 1: "helmet", 2: "no-helmet", 3: "number-plate"}
        crop = np.zeros((400, 450, 3), np.uint8)                                # bike-area crop: the bike is the middle third (150 px)
        m.out = [Box(1, 0.9, [170, 0, 290, 80]), Box(1, 0.6, [190, 80, 230, 130])]   # 120 px "helmet" = a parked bike behind; 40 px head
        pos, neg, info = jd._classify(m, names, crop, 640, True, ref_w=150)
        self.assertEqual((pos, info["heads"], info["foreign"]), (0.6, 1, 1))
        pos, neg, info = jd._classify(m, names, crop, 640, True)                  # no reference width: no size rule
        self.assertEqual((pos, info["heads"]), (0.9, 2))

    def test_no_rider_needs_every_vote_to_agree(self):
        self.assertEqual(jd.decide([jd.NO_RIDER] * 3), jd.NO_RIDER)
        self.assertEqual(jd.decide([jd.NO_RIDER] * 2), jd.UNCERTAIN)
        self.assertEqual(jd.decide([jd.NO_RIDER] * 5 + [jd.UNCERTAIN]), jd.UNCERTAIN)   # one frame showed a head
        self.assertEqual(jd.decide([jd.NO_RIDER] * 2 + [jd.HELMET] * 2), jd.HELMET)
        self.assertEqual(jd.decide([jd.NO_RIDER] * 2, min_no_rider=2), jd.NO_RIDER)

    def test_event_carries_category_and_vehicle_only_count(self):
        orig = jd.judge_crop

        def fake(top, *a, **k):
            return {"verdict": jd.NO_RIDER, "helmetConf": 0.0, "noHelmetConf": 0.0, "views": 1, "reason": "scripted", "source": "scripted"}
        jd.judge_crop = fake
        got = Collect()
        eng = ScriptedEngine(got, lambda i: [bike(1, 640, 330 + 18 * i)])          # two-wheeler, nobody on it
        try:
            jd._STATE["tried"] = True
            eng.set_module("twhelmet", cfg("twhelmet", minTrackFrames=1, minNoRiderVotes=1))
            run(eng, 20)
            ev = got.wait(1, 6)
            self.assertEqual(len(ev), 1)
            e = ev[0][1]
            self.assertEqual((e["label"], e["category"], e["confirmed"]), ("Vehicle only", "Only Vehicle Detected", True))
            self.assertEqual(eng.view("twhelmet")["counts"]["vehicleOnly"], 1)
        finally:
            eng.close()
            jd.judge_crop = orig

    def test_vehicle_only_refused_for_a_two_wheeler_that_travelled(self):
        orig = jd.judge_crop
        jd.judge_crop = lambda top, *a, **k: {"verdict": jd.NO_RIDER, "helmetConf": 0.0, "noHelmetConf": 0.0, "views": 1,
                                              "reason": "no rider head found on the two-wheeler", "source": "scripted"}
        got = Collect()
        eng = ScriptedEngine(got, lambda i: [bike(1, 640, 60 + 40 * i, h=110, w=120)])   # 40 px a frame: travels several bike lengths before the line
        try:
            jd._STATE["tried"] = True
            eng.set_module("twhelmet", cfg("twhelmet", minTrackFrames=1, minNoRiderVotes=1))
            run(eng, 24)
            e = got.wait(1, 6)[0][1]
            self.assertGreater(e["travel"], 1.5)
            self.assertEqual((e["label"], e["category"], e["why"]), ("Helmet unverified", None, "moving two-wheeler, rider not seen"))
        finally:
            eng.close()
            jd.judge_crop = orig

    def test_person_near_the_bike_blocks_vehicle_only(self):
        bike_ = {"box": [100, 400, 200, 500], "tid": 1}
        jd._STATE["tried"] = True
        items = ce.CrossingEngine._rider_items([], [{"box": [90, 300, 150, 480], "tid": 7}], bike_, 1080, 1920)   # standing beside it, not linked
        self.assertEqual([k for k, _t, _b in items], ["bike-area+person"])
        items = ce.CrossingEngine._rider_items([], [{"box": [900, 300, 960, 480], "tid": 7}], bike_, 1080, 1920)  # far away
        self.assertEqual([k for k, _t, _b in items], ["bike-area"])
        none = lambda c: (0.0, 0.0, {"heads": 0, "tiny": 0, "headPx": 0})
        self.assertEqual(jd.judge_crop(self.top, classify=none, heads=lambda c: [], bike_area=False)["verdict"], jd.UNCERTAIN)

    def test_pending_crops_are_judged_at_the_crossing_when_the_judge_thread_lags(self):
        """Field finding 2026-10-30: every crossing had 1-2 votes because the judge thread (CPU) was behind, so a helmet at
        0.75 stayed unverified. Approach crops are kept per rider and judged at the crossing if still waiting."""
        rh = jd.RiderHistory()
        crop = np.zeros((60, 40, 3), np.uint8)
        calls = []
        orig = jd.judge_crop
        jd.judge_crop = lambda top, *a, **k: (calls.append(k.get("bike_area")) or {"verdict": jd.HELMET, "helmetConf": 0.75, "noHelmetConf": 0.0,
                                                                                   "views": 1, "reason": "scripted", "source": "scripted"})
        try:
            for _ in range(6):                                   # six approach frames, the judge thread never ran
                rh.queue(crop, True)
            self.assertEqual(len(rh.pending), jd.RiderHistory.KEEP)   # only the last 4 are kept
            self.assertEqual(rh.judge_pending(limit=1), 1)         # what the judge thread does when it has time
            self.assertEqual(rh.judge_pending(), 3)                # what the crossing does with the rest
            self.assertEqual((list(rh.votes), calls), ([jd.HELMET] * 4, [True] * 4))
            self.assertEqual(rh.verdict(), jd.HELMET)
        finally:
            jd.judge_crop = orig

    def test_slow_judge_thread_still_yields_a_confirmed_crossing(self):
        orig = jd.judge_crop

        def slow(top, *a, **k):
            time.sleep(0.15)                                       # ~ the rider model on the box CPU
            return {"verdict": jd.HELMET, "helmetConf": 0.8, "noHelmetConf": 0.0, "views": 1, "reason": "scripted", "source": "scripted"}
        jd.judge_crop = slow
        got = Collect()
        eng = ScriptedEngine(got, lambda i: [bike(1, 640, 330 + 18 * i), rider_on(bike(1, 640, 330 + 18 * i), 10)])
        try:
            eng.set_module("twhelmet", cfg("twhelmet", minTrackFrames=1))          # default: 2 helmet votes needed
            run(eng, 20)
            e = got.wait(1, 10)[0][1]
            self.assertGreaterEqual(len(e["riders"][0]["votes"]), 2, e["riders"][0])
            self.assertEqual((e["label"], e["category"]), ("Helmet compliant", "Helmet Detected"))
        finally:
            eng.close()
            jd.judge_crop = orig

    def test_unverified_event_is_not_confirmed(self):
        orig = jd.judge_crop
        jd.judge_crop = lambda top, *a, **k: {"verdict": jd.UNCERTAIN, "helmetConf": 0.2, "noHelmetConf": 0.1, "views": 1,
                                              "reason": "head too small to judge", "source": "scripted"}
        got = Collect()
        eng = ScriptedEngine(got, lambda i: [bike(1, 640, 330 + 18 * i), rider_on(bike(1, 640, 330 + 18 * i), 10)])
        try:
            eng.set_module("twhelmet", cfg("twhelmet", minTrackFrames=1))
            run(eng, 20)
            e = got.wait(1, 6)[0][1]
            self.assertEqual((e["label"], e["category"], e["confirmed"]), ("Helmet unverified", None, False))
            self.assertIn("head too small to judge", e["why"])
        finally:
            eng.close()
            jd.judge_crop = orig


class EveryFrameTests(unittest.TestCase):
    def test_every_frame_lifts_the_shared_rate_cap_for_the_helmet_module_only(self):
        eng = ScriptedEngine(lambda *a: None, lambda i: [])
        try:
            cfg_h = copy.deepcopy(DEFAULTS["twhelmet"])
            cfg_p = copy.deepcopy(DEFAULTS["pcross"])
            self.assertTrue(cfg_h["everyFrame"])
            self.assertNotIn("everyFrame", cfg_p)
            eng.set_module("pcross", cfg_p)
            eng.set_rate_cap(5)
            self.assertFalse(eng.every_frame)
            self.assertEqual(eng.target_fps, 5.0)
            eng.set_module("twhelmet", cfg_h)
            self.assertTrue(eng.every_frame)
            self.assertEqual(eng.target_fps, float(config.AI_FPS_MAX))
            cfg_h["everyFrame"] = False
            eng.set_module("twhelmet", cfg_h)
            self.assertFalse(eng.every_frame)
            self.assertEqual(eng.target_fps, 5.0)
        finally:
            eng.close()


class QueueTests(unittest.TestCase):
    def test_newest_frame_wins_and_stale_dropped(self):
        got = Collect()
        eng = ScriptedEngine(got, lambda i: [], delay=0.15)
        try:
            eng.set_module("pcross", cfg("pcross", targetFps=30))
            now = time.time()
            for i in range(10):
                eng._last_submit = 0.0
                eng.submit(frame(i), time.time(), i)
            time.sleep(1.0)
            m = eng.metrics()
            self.assertEqual(m["queueSize"], 1)
            self.assertEqual(m["lastFrameId"], 9)                                  # the freshest frame was analysed
            self.assertLessEqual(m["framesProcessed"], 3)
            self.assertGreaterEqual(m["droppedReplaced"], 7)
            eng._last_submit = 0.0
            eng.submit(frame(50), time.time() - 5.0, 50)                           # captured 5 s ago
            time.sleep(0.6)
            self.assertEqual(eng.metrics()["droppedStale"], 1)
            self.assertNotEqual(eng.metrics()["lastFrameId"], 50)
        finally:
            eng.close()

    def test_target_fps_interval(self):
        eng = ScriptedEngine(Collect(), lambda i: [])
        try:
            eng.set_module("pcross", cfg("pcross", targetFps=3))     # a saved value must NOT pin the dynamic rate
            eng.set_rate_cap(10)                                      # what the session hands over with 10 fps for this camera
            self.assertEqual(eng.target_fps, 10)
            taken = 0
            t_end = time.time() + 1.0
            i = 0
            while time.time() < t_end:
                taken += bool(eng.submit(frame(i), time.time(), i))
                i += 1
                time.sleep(0.01)                                                   # a 100 fps camera
            self.assertTrue(8 <= taken <= 11, taken)
            self.assertGreater(eng.metrics()["skippedInterval"], 50)
        finally:
            eng.close()


    def test_dynamic_rate_follows_the_session(self):
        eng = ScriptedEngine(Collect(), lambda i: [])
        try:
            eng.set_module("pcross", cfg("pcross"))
            eng.set_rate_cap(15)
            self.assertEqual(eng.target_fps, 15)                                   # two cameras share the budget
            eng.set_rate_cap(config.AI_FPS_MAX)
            self.assertEqual(eng.target_fps, config.AI_FPS_MAX)                    # one camera: the maximum
        finally:
            eng.close()


class ScriptedEventTests(unittest.TestCase):
    def _walk_person(self, i):
        return [person(1, 640, 330 + 18 * i)]                                      # foot 330 -> crosses y=360 at frame 2, then goes on

    def test_person_event_has_frame_id_time_and_exact_snapshot(self):
        got = Collect()
        eng = ScriptedEngine(got, self._walk_person)
        try:
            eng.set_module("pcross", cfg("pcross", minTrackFrames=1))
            run(eng, 20, t0=5000.0)
            ev = got.wait(1)
            self.assertEqual(len(ev), 1)
            mod, e, snap, fr = ev[0]
            self.assertEqual(e["direction"], "in")
            self.assertEqual(int(fr[0, 0, 0]), e["frameId"] % 250)               # the saved clean frame IS the crossing frame
            self.assertAlmostEqual(e["capturedAt"], 5000.0 + e["frameId"] * 0.1, places=6)
            self.assertEqual(snap.shape, fr.shape)
            self.assertFalse(np.array_equal(snap, fr))                              # annotated copy
            self.assertEqual(eng.view("pcross")["counts"], {"in": 1, "out": 0})
        finally:
            eng.close()

    def test_direction_filter_and_both_ways(self):
        got = Collect()
        eng = ScriptedEngine(got, lambda i: [person(1, 640, 330 + 18 * i if i < 12 else 330 + 18 * (24 - i))])
        try:
            eng.set_module("pcross", cfg("pcross", minTrackFrames=1, recrossSec=0.0))
            run(eng, 25)
            self.assertEqual(eng.view("pcross")["counts"], {"in": 1, "out": 1})
            eng.reset("pcross")
            eng.set_module("pcross", cfg("pcross", minTrackFrames=1, recrossSec=0.0, direction="out"))
            run(eng, 25, t0=2000.0)
            self.assertEqual(eng.view("pcross")["counts"], {"in": 0, "out": 1})
            self.assertEqual(eng.view("pcross")["ignoredDirection"], 1)
        finally:
            eng.close()

    def test_riders_are_not_counted_as_pedestrians_and_pedestrians_not_helmet_checked(self):
        got = Collect()

        def script(i):
            b = bike(1, 300, 330 + 18 * i)
            return [b, rider_on(b, 10), person(2, 900, 330 + 18 * i)]               # a rider on a bike and a pedestrian, both crossing
        eng = ScriptedEngine(got, script)
        try:
            eng.set_module("pcross", cfg("pcross", minTrackFrames=1))
            eng.set_module("twhelmet", cfg("twhelmet", minTrackFrames=1))
            run(eng, 20)
            ev = got.wait(2, 6)
            pc = [e for m, e, *_ in ev if m == "pcross"]
            tw = [e for m, e, *_ in ev if m == "twhelmet"]
            self.assertEqual([e["tid"] for e in pc], [2])                           # only the pedestrian
            self.assertEqual([e["tid"] for e in tw], [1])                           # only the motorcycle
        finally:
            eng.close()

    def test_car_and_pedestrian_do_not_trigger_helmet_rule(self):
        got = Collect()
        eng = ScriptedEngine(got, lambda i: [person(2, 640, 330 + 18 * i), dict(bike(3, 300, 330 + 18 * i), cls="car")])
        try:
            eng.set_module("twhelmet", cfg("twhelmet", minTrackFrames=1))
            run(eng, 20)
            time.sleep(0.5)
            self.assertEqual(got.ev, [])
        finally:
            eng.close()

    def test_helmet_verdicts_compliant_missing_unverified(self):
        for scripted, label, key in (((jd.HELMET,), "Helmet compliant", "compliant"), ((jd.MISSING,), "Helmet missing", "missing"),
                                     ((jd.UNCERTAIN,), "Helmet unverified", "unverified"),
                                     ((jd.HELMET, jd.MISSING), "Helmet missing", "missing")):          # pillion without helmet
            verdicts = list(scripted)
            orig = jd.judge_crop
            seq = {"n": 0}

            def fake(top, *a, **k):
                v = verdicts[seq["n"] % len(verdicts)]
                seq["n"] += 1
                return {"verdict": v, "helmetConf": 0.9 if v == jd.HELMET else 0.0, "noHelmetConf": 0.9 if v == jd.MISSING else 0.0,
                        "views": 2, "reason": "scripted", "source": "scripted"}
            jd.judge_crop = fake
            got = Collect()

            def script(i):
                b = bike(1, 640, 330 + 18 * i)
                out = [b, rider_on(b, 10)]
                if len(verdicts) == 2:
                    out.append(dict(rider_on(b, 11), box=[640 + 30, b["box"][1] - 40, 640 + 130, b["box"][3] - 40]))
                return out
            eng = ScriptedEngine(got, script)
            try:
                eng.set_module("twhelmet", cfg("twhelmet", minTrackFrames=1, minMissingVotes=1, minHelmetVotes=1))
                run(eng, 20)
                ev = got.wait(1, 6)
                self.assertEqual(len(ev), 1, scripted)
                self.assertEqual(ev[0][1]["label"], label, scripted)
                self.assertEqual(eng.view("twhelmet")["counts"][key], 1)
            finally:
                eng.close()
                jd.judge_crop = orig

    def test_crossing_event_not_repeated_by_jitter_or_second_track_id(self):
        got = Collect()

        def script(i):
            y = 330 + 18 * i
            jitter = (-3, 4, -2, 5)[i % 4]
            return [person(1, 640, y + jitter), person(9, 644, y)]                   # second id on the same person
        eng = ScriptedEngine(got, script)
        try:
            eng.set_module("pcross", cfg("pcross", minTrackFrames=1))
            run(eng, 20)
            time.sleep(0.3)
            self.assertEqual(len(got.ev), 1)
            self.assertEqual(eng.view("pcross")["tracking"]["suppressed"]["duplicate"], 1)
        finally:
            eng.close()


class AlertWiringTests(unittest.TestCase):
    """The session turns events into alerts: separate compliant / missing alerts, none for unverified, cooldown by track id."""

    def test_event_to_alert_mapping(self):
        from backend.services.sessions import CameraSession
        calls = []

        class Ev:
            def on_module_alert(self, *a, **k):
                calls.append((a, k))

        class Reg:
            def get(self, cid):
                return {"id": cid, "name": "c"}
        s = CameraSession.__new__(CameraSession)
        s.camera_id, s.mgr = "C1", type("M", (), {"registry": Reg(), "events": Ev()})()
        s.modules = {"twhelmet": {"paused": False}}
        s.last_detection_at, s.cross_cfg = {}, {"twhelmet": cfg("twhelmet")}
        base = {"tid": 4, "frameId": 77, "direction": "in", "point": [1, 2], "box": [0, 0, 1, 1], "capturedAt": 1000.0, "cls": "motorcycle",
                "frameShape": (H, W), "counts": {}, "riders": [{"helmetConf": 0.8, "noHelmetConf": 0.0}], "why": "x"}
        snap = np.zeros((10, 10, 3), np.uint8)
        for label, etype, alert in (("Helmet compliant", "twhelmet_compliant", True), ("Helmet missing", "twhelmet_missing", True),
                                    ("Helmet unverified", "twhelmet_unverified", False)):
            calls.clear()
            s._cross_event("twhelmet", dict(base, label=label, verdict="X"), snap, snap)
            (a, k), = calls
            self.assertEqual(a[2], etype)
            self.assertEqual(k["alert"], alert)
            self.assertEqual(k["extra"]["frameId"], 77)
            self.assertIn(etype, str(k["cooldown_key"]))
        self.assertEqual(calls[0][1]["severity"], "low")


class PipelineTests(unittest.TestCase):
    """Frame pipeline plumbing with SCRIPTED detections and a sleep standing in for the network (no model, no speed claim):
    workers analyse different frames at the same time, the tracker still sees them in frame order."""

    def _feed(self, eng, n):
        for i in range(n):
            while True:                                         # never drop: wait until a worker has taken the waiting frame
                eng._last_submit = 0.0
                if eng.job is None and eng.submit(frame(i), time.time(), i):
                    break
                time.sleep(0.002)
        end = time.time() + 10
        while time.time() < end and eng.metrics()["framesProcessed"] < n:
            time.sleep(0.02)

    def test_results_reach_the_tracker_in_frame_order_even_when_later_frames_finish_first(self):
        eng = ScriptedEngine(Collect(), lambda i: [], depth_max=3, delays=lambda i: 0.16 if i % 3 == 0 else 0.02)
        try:
            eng.set_module("pcross", cfg("pcross"))
            eng.set_depth(3)
            self._feed(eng, 30)
            self.assertEqual(eng.assoc_order, sorted(eng.assoc_order))
            self.assertEqual(eng.assoc_order, list(range(30)))
        finally:
            eng.close()

    def test_three_workers_overlap_frames(self):
        out = {}
        for depth in (1, 3):
            eng = ScriptedEngine(Collect(), lambda i: [], depth_max=3, delay=0.05)
            try:
                eng.set_module("pcross", cfg("pcross"))
                eng.set_depth(depth)
                t0 = time.time()
                self._feed(eng, 30)
                out[depth] = time.time() - t0
                self.assertEqual(eng.metrics()["pipeline"]["depth"], depth)
            finally:
                eng.close()
        self.assertLess(out[3], out[1] * 0.6, out)                                # simulated 50 ms "network": 30 frames, 1 vs 3 workers

    def test_depth_one_uses_only_the_first_worker(self):
        eng = ScriptedEngine(Collect(), lambda i: [], depth_max=3, delay=0.02)
        try:
            eng.set_module("pcross", cfg("pcross"))
            seen = set()
            orig = eng._infer

            def spy(frame_, mods, model=None):
                seen.add(threading.current_thread().name)
                return orig(frame_, mods, model)
            eng._infer = spy
            self._feed(eng, 10)
            self.assertEqual(len(seen), 1)
        finally:
            eng.close()

    def test_a_failed_frame_does_not_stall_the_frames_after_it(self):
        eng = ScriptedEngine(Collect(), lambda i: [], depth_max=3, delay=0.01)
        try:
            eng.set_module("pcross", cfg("pcross"))
            eng.set_depth(3)

            def script(i):
                if i == 4:
                    raise RuntimeError("scripted network failure")
                return []
            eng._script = script
            self._feed(eng, 12)
            time.sleep(0.3)
            m = eng.metrics()
            self.assertEqual((m["framesProcessed"], m["errors"]), (11, 1))
            self.assertEqual(eng.assoc_order, [i for i in range(12) if i != 4])
        finally:
            eng.close()


SRC = Path(os.environ.get("AIV_PPE_TEST_IMAGE_DIR", "/mnt/user-data/uploads/ppe detection/Realtime-Edge-PPE-Detection"))


@unittest.skipUnless((SRC / "road.jpg").is_file() and (SRC / "cons1.jpg").is_file() and Path(ce.config.MODEL_PATH).is_file(), "staged photos / model not available")
class RealModelSimulatedMotionTests(unittest.TestCase):
    """REAL detector + tracker on real photo pixels; only the MOTION is simulated (the photo is slid down the canvas)."""

    def _slide(self, name, mod, n=40, extra=None):
        import cv2
        img = cv2.imread(str(SRC / name))
        sprite = cv2.resize(img, None, fx=420 / img.shape[0], fy=420 / img.shape[0])
        sh, sw = sprite.shape[:2]
        got = Collect()
        eng = ce.CrossingEngine(got, camera_id="sim")
        try:
            c = cfg(mod, confidence=0.3, **(extra or {}))
            c["line"]["y1"] = c["line"]["y2"] = 0.8
            eng.set_module(mod, c)
            for i in range(n):
                canvas = np.full((H, W, 3), 110, np.uint8)
                y0 = int(20 + i * (H - sh - 40) / (n - 1))
                canvas[y0:y0 + sh, (W - sw) // 2:(W - sw) // 2 + sw] = sprite
                eng.process(canvas, 100.0 + i * 0.1, i)
            while eng.judger.pending():
                time.sleep(0.1)
            time.sleep(1.0)
            return got.ev, eng.view(mod)
        finally:
            eng.close()

    def test_two_people_counted_once_each_in(self):
        ev, v = self._slide("cons1.jpg", "pcross")
        self.assertEqual(v["counts"], {"in": 2, "out": 0})
        self.assertEqual(len({e["tid"] for _m, e, *_ in ev}), 2)

    def test_scooter_rider_checked_and_not_counted_as_pedestrian(self):
        ev, v = self._slide("road.jpg", "twhelmet")
        self.assertEqual(v["crossings"], 1)
        e = ev[0][1]
        self.assertEqual(len(e["riders"]), 1)                                       # the rider was associated with the scooter
        # the stock judge is not validated on motorcycle helmets: it must not claim MISSING for a rider who wears one
        self.assertNotEqual(e["verdict"], jd.MISSING)
        ev, v = self._slide("road.jpg", "pcross")
        self.assertEqual(v["counts"], {"in": 0, "out": 0})



@unittest.skipUnless((SRC / "cons1.jpg").is_file() and Path(ce.config.MODEL_PATH).is_file(), "staged photos / model not available")
class RealModelPipelineTests(unittest.TestCase):
    """REAL detector + tracker. The split network / tracker path must give what model.track gave, and 3 workers the same counts."""

    def _frames(self, n=30):
        import cv2
        img = cv2.imread(str(SRC / "cons1.jpg"))
        sprite = cv2.resize(img, None, fx=420 / img.shape[0], fy=420 / img.shape[0])
        sh, sw = sprite.shape[:2]
        out = []
        for i in range(n):
            canvas = np.full((H, W, 3), 110, np.uint8)
            y0 = int(20 + i * (H - sh - 40) / (n - 1))
            canvas[y0:y0 + sh, (W - sw) // 2:(W - sw) // 2 + sw] = sprite
            out.append(canvas)
        return out

    def test_split_path_matches_model_track(self):
        eng = ce.CrossingEngine(Collect(), camera_id="eq", depth_max=1)
        try:
            eng.set_module("pcross", cfg("pcross", confidence=0.3))
            mods = list(eng.mods.values())
            ref_model = ce.load_model()[0]                                      # a second, untouched model: model.track as before
            for f in self._frames(24):
                dets, _ms = eng._detect(f, mods)
                r = ref_model.track(f, persist=True, tracker=str(ce.config.TRACKER_CONFIG), imgsz=eng.imgsz, conf=0.3,
                                    classes=[0, 1, 3], verbose=False)[0]
                ref = [(int(b.id), [round(float(v), 1) for v in b.xyxy[0].tolist()]) for b in r.boxes if b.id is not None]
                got = [(d["tid"], [round(v, 1) for v in d["box"]]) for d in dets]
                self.assertEqual(sorted(got), sorted(ref))
        finally:
            eng.close()

    def test_three_workers_count_like_one(self):
        res = {}
        for depth in (1, 3):
            got = Collect()
            eng = ce.CrossingEngine(got, camera_id="pl", depth_max=3)
            try:
                c = cfg("pcross", confidence=0.3)
                c["line"]["y1"] = c["line"]["y2"] = 0.8
                eng.set_module("pcross", c)
                eng.set_depth(depth)
                for i, f in enumerate(self._frames(40)):
                    while True:
                        eng._last_submit = 0.0
                        if eng.job is None and eng.submit(f, time.time(), i):
                            break
                        time.sleep(0.002)
                end = time.time() + 60
                while time.time() < end and eng.metrics()["framesProcessed"] < 40:
                    time.sleep(0.05)
                time.sleep(0.5)
                res[depth] = (eng.view("pcross")["counts"], eng.metrics()["framesProcessed"], eng.metrics()["pipeline"]["orderSkipped"])
            finally:
                eng.close()
        self.assertEqual(res[1][0], res[3][0], res)
        self.assertEqual((res[1][1], res[3][1], res[3][2]), (40, 40, 0), res)


if __name__ == "__main__":
    unittest.main()
