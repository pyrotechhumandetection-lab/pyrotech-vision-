"""PPE Detection module: rules (no model), config validation, and the real models on a staged picture.
Run: python3 -m unittest backend.tests.test_ppe"""
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
from backend.detection.modules import DEFAULTS, validate  # noqa: E402
from backend.detection.ppe import rules  # noqa: E402
from backend.detection.ppe import engine as ppe_engine  # noqa: E402

FRAME = np.full((720, 1280, 3), 90, np.uint8)
PERSON = {"cls": "Person", "box": [500, 100, 700, 650], "conf": 0.9, "tid": 1}


def cfg(**kw):
    c = copy.deepcopy(DEFAULTS["ppe"])
    c.update(kw)
    return c


def det(cls, box, conf):
    return {"cls": cls, "box": box, "conf": conf, "tid": None}


HELMET_BOX = [560, 95, 640, 160]
VEST_BOX = [530, 220, 670, 420]


class PPERuleTests(unittest.TestCase):
    def test_construction_helmet_present(self):
        ev = rules.assess_person(PERSON, [det("helmet", HELMET_BOX, 0.9)], FRAME, cfg(), lambda f, b: ("construction", 0.95))
        self.assertEqual(ev["helmet"]["obs"], rules.PRESENT)

    def test_classifier_road_alone_is_review_not_ppe(self):
        ev = rules.assess_person(PERSON, [det("helmet", HELMET_BOX, 0.9)], FRAME, cfg(), lambda f, b: ("road", 0.9))
        self.assertEqual(ev["helmet"]["obs"], rules.UNCERTAIN)       # never PRESENT, never an automatic MISSING
        self.assertIn("road", ev["helmet"]["reason"])

    def test_rider_helmet_is_missing(self):
        ev = rules.assess_person(PERSON, [det("helmet", HELMET_BOX, 0.9)], FRAME, cfg(), lambda f, b: ("construction", 0.99), rider=True)
        self.assertEqual((ev["helmet"]["obs"], ev["helmet"]["kind"]), (rules.MISSING, "rider"))
        ev = rules.assess_person(PERSON, [det("helmet", HELMET_BOX, 0.9)], FRAME, cfg(riderCheck=False), lambda f, b: ("construction", 0.99), rider=True)
        self.assertEqual(ev["helmet"]["obs"], rules.PRESENT)

    def test_black_helmet_is_missing(self):
        img = FRAME.copy()
        img[HELMET_BOX[1]:HELMET_BOX[3], HELMET_BOX[0]:HELMET_BOX[2]] = (25, 25, 25)
        img[HELMET_BOX[1] + 5:HELMET_BOX[1] + 12, HELMET_BOX[0] + 20:HELMET_BOX[0] + 40] = (230, 230, 230)   # gloss
        construction = lambda f, b: ("construction", 0.99)
        ev = rules.assess_person(PERSON, [det("helmet", HELMET_BOX, 0.9)], img, cfg(), construction)
        self.assertEqual((ev["helmet"]["obs"], ev["helmet"]["color"]), (rules.MISSING, "black"))
        ev = rules.assess_person(PERSON, [det("helmet", HELMET_BOX, 0.4)], img, cfg(), construction)    # weak + black
        self.assertEqual(ev["helmet"]["obs"], rules.MISSING)
        ev = rules.assess_person(PERSON, [det("helmet", HELMET_BOX, 0.9)], img, cfg(), construction, grey=True)
        self.assertEqual(ev["helmet"]["obs"], rules.UNCERTAIN)                                           # IR picture
        ev = rules.assess_person(PERSON, [det("helmet", HELMET_BOX, 0.9)], img, cfg(rejectBlackHelmet=False), construction)
        self.assertEqual(ev["helmet"]["obs"], rules.PRESENT)

    def test_coloured_helmets_are_not_black(self):
        for bgr in ((240, 240, 240), (0, 200, 240), (0, 120, 250), (200, 110, 30), (40, 40, 200)):
            self.assertFalse(rules.is_black_helmet(np.full((40, 60, 3), bgr, np.uint8)), bgr)

    def test_on_two_wheeler(self):
        rider = [500, 100, 640, 450]
        self.assertTrue(ppe_engine.on_two_wheeler(rider, [[470, 250, 680, 560]]))     # motorcycle under the person
        self.assertFalse(ppe_engine.on_two_wheeler(rider, [[700, 250, 900, 560]]))    # parked beside
        self.assertFalse(ppe_engine.on_two_wheeler(rider, [[470, 430, 680, 700]]))    # in front, below the feet

    def test_unverified_helmet_type_is_uncertain(self):
        ev = rules.assess_person(PERSON, [det("helmet", HELMET_BOX, 0.9)], FRAME, cfg(), lambda f, b: ("other", 0.5))
        self.assertEqual(ev["helmet"]["obs"], rules.UNCERTAIN)

    def test_weak_helmet_blocks_missing(self):
        ev = rules.assess_person(PERSON, [det("helmet", HELMET_BOX, 0.3)], FRAME, cfg(), None)
        self.assertEqual(ev["helmet"]["obs"], rules.UNCERTAIN)

    def test_no_evidence_visible_head_counts_toward_missing(self):
        ev = rules.assess_person(PERSON, [], FRAME, cfg(), None)
        self.assertEqual(ev["helmet"]["obs"], "NOHIT")
        self.assertEqual(ev["vest"]["obs"], "NOHIT")

    def test_gloves_absence_is_not_assessable(self):
        ev = rules.assess_person(PERSON, [], FRAME, cfg(), None)
        self.assertEqual(ev["gloves"]["obs"], rules.NOT_ASSESSABLE)

    def test_small_person_not_assessable(self):
        small = dict(PERSON, box=[600, 300, 630, 360])
        ev = rules.assess_person(small, [], FRAME, cfg(), None)
        self.assertEqual(ev["helmet"]["obs"], rules.NOT_ASSESSABLE)
        self.assertEqual(ev["vest"]["obs"], rules.NOT_ASSESSABLE)

    def test_item_outside_every_person_is_unassigned(self):
        self.assertIsNone(rules.assign_person([10, 10, 50, 50], [PERSON]))

    def test_crowd_helmet_goes_to_matching_head(self):
        front = {"cls": "Person", "box": [480, 80, 760, 700], "conf": 0.9}      # bigger box behind
        back = {"cls": "Person", "box": [560, 60, 700, 400], "conf": 0.9}
        helmet = [600, 55, 660, 110]                                          # on top of `back`
        self.assertEqual(rules.assign_person(helmet, [front, back], "helmet"), 1)

    def test_miss_after_streak(self):
        ps = rules.PersonState(1)
        c = cfg(missAfter=3)
        for n in range(3):
            ps.update(rules.assess_person(PERSON, [], FRAME, c, None), c, float(n))
            st = ps.status["helmet"]["status"]
            self.assertEqual(st, rules.MISSING if n == 2 else rules.UNCERTAIN)

    def test_alert_dedupe_repeat_and_reset(self):
        c = cfg(missAfter=1, repeatSec=600)
        req = {"helmet": True, "vest": False, "gloves": False}
        ps = rules.PersonState(1)
        ps.update(rules.assess_person(PERSON, [], FRAME, c, None), c, 0.0)
        self.assertEqual([a[1] for a in rules.alerts_due(ps, req, c, 0.0)], ["Helmet missing"])
        self.assertEqual(rules.alerts_due(ps, req, c, 10.0), [])                 # cooldown: no repeat
        self.assertEqual(len(rules.alerts_due(ps, req, c, 700.0)), 1)            # after repeatSec
        ok = rules.assess_person(PERSON, [det("helmet", HELMET_BOX, 0.9)], FRAME, c, lambda f, b: ("construction", 0.9))
        ps.update(ok, c, 701.0)
        self.assertEqual(rules.alerts_due(ps, req, c, 701.0), [])
        ps.update(rules.assess_person(PERSON, [], FRAME, c, None), c, 702.0)
        self.assertEqual(len(rules.alerts_due(ps, req, c, 702.0)), 1)            # state change -> new alert

    def test_vest_colour_only_after_detector_vest(self):
        img = FRAME.copy()
        img[VEST_BOX[1]:VEST_BOX[3], VEST_BOX[0]:VEST_BOX[2]] = (0, 110, 255)    # orange (BGR)
        ev = rules.assess_person(PERSON, [], img, cfg(), None)                   # no vest box: colour never makes one
        self.assertEqual(ev["vest"]["obs"], "NOHIT")
        ev = rules.assess_person(PERSON, [det("vest", VEST_BOX, 0.8)], img, cfg(), None)
        self.assertEqual((ev["vest"]["obs"], ev["vest"]["color"]), (rules.PRESENT, "orange"))
        blue = FRAME.copy()
        blue[VEST_BOX[1]:VEST_BOX[3], VEST_BOX[0]:VEST_BOX[2]] = (220, 80, 20)
        c = cfg(missAfter=1)
        ps = rules.PersonState(1)
        ps.update(rules.assess_person(PERSON, [det("vest", VEST_BOX, 0.8)], blue, c, None), c, 0.0)
        self.assertEqual(ps.status["vest"]["colorCompliance"], "non-compliant")
        kinds = [a[0] for a in rules.alerts_due(ps, {"helmet": False, "vest": True, "gloves": False}, c, 0.0)]
        self.assertIn("ppe_vest_color", kinds)

    def test_helmet_colour_unknown_when_dark(self):
        self.assertEqual(rules.helmet_color(np.zeros((40, 40, 3), np.uint8)), "unknown")
        white = np.full((40, 40, 3), 235, np.uint8)
        self.assertEqual(rules.helmet_color(white), "white")


class PPEConfigTests(unittest.TestCase):
    def test_defaults_valid(self):
        self.assertEqual(validate("ppe", copy.deepcopy(DEFAULTS["ppe"]), []), [])

    def test_bad_values_rejected(self):
        bad = copy.deepcopy(DEFAULTS["ppe"])
        bad["intervalSec"] = -1
        self.assertTrue(validate("ppe", bad, []))


STAGED = Path(os.environ.get("AIV_PPE_TEST_IMAGE", "/mnt/user-data/uploads/ppe detection/Realtime-Edge-PPE-Detection/cons1.jpg"))


@unittest.skipUnless(ppe_engine.models_present() and STAGED.is_file(), "PPE model or staged test picture not available")
class PPEEngineRealModelTests(unittest.TestCase):
    """Real models: alert carries the exact frame that was assessed (never a later frame)."""

    def test_alert_uses_assessed_frame(self):
        import cv2
        frame = cv2.imread(str(STAGED))
        got, done = [], threading.Event()

        def on_alert(kind, label, item, reason, view, snap, fr, captured_at):
            got.append((kind, label, fr, snap, captured_at))
            done.set()

        eng = ppe_engine.PPEEngine(on_alert)
        try:
            eng.configure(cfg(missAfter=1, intervalSec=0), [])
            self.assertTrue(eng.submit(frame, 1234.5))
            self.assertTrue(done.wait(120), "no PPE alert (the picture has two people without vests)")
            kind, label, fr, snap, ts = got[0]
            self.assertEqual(ts, 1234.5)
            self.assertTrue(np.array_equal(fr, frame))
            self.assertEqual(snap.shape, frame.shape)
            self.assertIn("Vest missing", [g[1] for g in got])
            self.assertGreaterEqual(eng.view()["summary"]["peopleAssessed"], 2)
        finally:
            eng.close()


if __name__ == "__main__":
    unittest.main()
