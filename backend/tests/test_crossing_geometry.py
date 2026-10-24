"""Line-crossing geometry and per-track rules (no model). Tracks here are SYNTHETIC points fed to the rules to test
the rules themselves - they are not detections and say nothing about detector accuracy.
Run: python3 -m unittest backend.tests.test_crossing_geometry"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import backend  # noqa: E402,F401
from backend.detection.crossing.geometry import CrossingTracker, Line, cuts_segment, IN, OUT  # noqa: E402

W, H = 1280, 720
HLINE = Line(0.2, 0.5, 0.8, 0.5, W, H)          # left -> right: arrow (IN) points DOWN the picture


def person(tid, x, foot_y, h=200, w=80, cls="person"):
    return {"tid": tid, "box": [x - w / 2, foot_y - h, x + w / 2, foot_y], "cls": cls, "conf": 0.9}


def walk(tr, tid, xs, ys, t0=0.0, dt=0.1, **kw):
    ev = []
    for i, (x, y) in enumerate(zip(xs, ys)):
        ev += tr.update([person(tid, x, y, **kw)], t0 + i * dt, fid=i)
    return ev


def tracker(**kw):
    return CrossingTracker(HLINE, W, H, **kw)


class GeometryTests(unittest.TestCase):
    def test_signed_side_and_arrow(self):
        self.assertGreater(HLINE.signed((600, 400), W, H), 0 - 1e9)
        self.assertLess(HLINE.signed((600, 300), W, H), 0)      # above the line = negative
        self.assertGreater(HLINE.signed((600, 400), W, H), 0)   # below = positive = the arrow side
        nx, ny = HLINE.arrow_normal(W, H)
        self.assertAlmostEqual((nx, ny), (0.0, 1.0), places=6)

    def test_finite_segment(self):
        self.assertTrue(cuts_segment(HLINE, (600, 300), (600, 420), W, H))
        self.assertFalse(cuts_segment(HLINE, (100, 300), (100, 420), W, H))        # beyond the left end
        self.assertFalse(cuts_segment(HLINE, (600, 300), (600, 340), W, H))        # does not reach the line

    def test_diagonal_line_direction(self):
        diag = Line(0.2, 0.8, 0.8, 0.2, W, H)
        tr = CrossingTracker(diag, W, H)
        xs = [300 + 40 * i for i in range(11)]                          # y=400: above the diagonal at x=300, below it at x=700
        ev = walk(tr, 1, xs, [400] * 11)
        ev2 = walk(CrossingTracker(diag, W, H), 1, xs[::-1], [400] * 11)
        self.assertEqual((len(ev), len(ev2)), (1, 1))
        self.assertNotEqual(ev[0]["direction"], ev2[0]["direction"])
        self.assertEqual(ev[0]["direction"], IN)                         # arrow normal of A->B points down-right here

    def test_aspect_mismatch(self):
        self.assertFalse(HLINE.aspect_mismatch(640, 360))
        self.assertTrue(HLINE.aspect_mismatch(640, 480))


class CrossingRuleTests(unittest.TestCase):
    def test_in_and_out_once_each(self):
        ys = [300, 330, 360, 390, 420, 450, 480, 520, 560, 600]       # top -> bottom = IN
        ev = walk(tracker(), 1, [600] * 10, ys)
        self.assertEqual([e["direction"] for e in ev], [IN])
        ev = walk(tracker(), 2, [600] * 10, ys[::-1])
        self.assertEqual([e["direction"] for e in ev], [OUT])

    def test_event_frame_is_the_crossing_frame(self):
        ev = walk(tracker(), 1, [600] * 10, [300, 330, 360, 390, 420, 450, 480, 520, 560, 600], t0=100.0)
        self.assertEqual(len(ev), 1)
        e = ev[0]
        self.assertEqual((e["frameId"], round(e["t"], 3)), (3, 100.3))   # first frame firmly past the line (foot y=390 vs line y=360)

    def test_jitter_on_the_line_counts_nothing(self):
        ys = [330, 345, 352, 349, 355, 347, 353, 350, 349, 351, 348, 352]   # hovering at the line (y=360 foot -> line y=360)
        ys = [355, 362, 358, 364, 357, 361, 359, 363, 358, 362, 360, 357]
        self.assertEqual(walk(tracker(), 1, [600] * 12, ys), [])

    def test_jitter_then_real_cross_counts_once(self):
        ys = [300, 330, 352, 366, 356, 368, 354, 366, 358, 372, 400, 440, 480, 520]
        ev = walk(tracker(), 1, [600] * len(ys), ys)
        self.assertEqual(len(ev), 1)

    def test_walk_around_the_end_does_not_count(self):
        ev = walk(tracker(), 1, [100] * 10, [300, 330, 360, 390, 420, 450, 480, 520, 560, 600])
        self.assertEqual(ev, [])

    def test_cross_back_inside_cooldown_is_suppressed_then_counts_after(self):
        down = [300, 340, 380, 420, 460, 500]
        tr = tracker(recross_sec=2.0)
        ev = walk(tr, 1, [600] * 6, down)
        ev += walk(tr, 1, [600] * 6, down[::-1], t0=0.6)       # back within 1.1 s
        self.assertEqual([e["direction"] for e in ev], [IN])
        ev2 = walk(tr, 1, [600] * 6, down[::-1], t0=10.0)       # new crossing much later (track returns to top first)
        # the track is at the top after the walk back; going down again after the cooldown must count
        ev3 = walk(tr, 1, [600] * 6, down, t0=12.0)
        self.assertEqual([e["direction"] for e in ev3], [IN])

    def test_two_people_opposite_directions_and_side_by_side(self):
        tr = tracker()
        ev = []
        for i in range(10):
            ev += tr.update([person(1, 400, 300 + 30 * i), person(2, 800, 570 - 30 * i), person(3, 500, 300 + 30 * i)], i * 0.1, i)
        by = sorted((e["tid"], e["direction"]) for e in ev)
        self.assertEqual(by, [(1, IN), (2, OUT), (3, IN)])          # 1 and 3 side by side: both counted

    def test_id_switch_at_the_line_is_one_person(self):
        tr = tracker()
        ev = walk(tr, 1, [600] * 5, [300, 330, 360, 390, 420])        # id 1 lost mid-way ...
        ev += walk(tr, 7, [600] * 5, [450, 480, 520, 560, 600], t0=0.6)    # ... id 7 continues right there
        self.assertEqual(len(ev), 1)
        self.assertEqual(tr.stats["handovers"], 1)

    def test_occlusion_gap_same_id_counts_once_with_gap(self):
        tr = tracker()
        ev = walk(tr, 1, [600] * 4, [300, 320, 340, 350])
        ev += walk(tr, 1, [600] * 3, [520, 560, 600], t0=1.5)          # hidden for 1.1 s, reappears past the line
        self.assertEqual(len(ev), 1)
        self.assertGreater(ev[0]["gapSec"], 1.0)

    def test_duplicate_track_same_spot_same_time_suppressed(self):
        tr = tracker(handover_sec=0.0)                                 # no handover: duplicate box with a second id
        ev = []
        for i in range(10):
            y = 300 + 30 * i
            ev += tr.update([person(1, 600, y), person(2, 604, y)], i * 0.1, i)
        self.assertEqual(len(ev), 1)
        self.assertEqual(tr.stats["duplicatesSuppressed"], 1)

    def test_short_ghost_track_ignored(self):
        tr = tracker(min_frames=3)
        ev = walk(tr, 1, [600] * 2, [300, 520])
        self.assertEqual(ev, [])
        self.assertEqual(tr.stats["shortSuppressed"], 1)

    def test_stationary_person_on_far_side_never_counts(self):
        self.assertEqual(walk(tracker(), 1, [600] * 30, [540] * 30), [])

    def test_untracked_detections_are_not_counted(self):
        tr = tracker()
        ev = []
        for i, y in enumerate([300, 340, 380, 420, 460, 500]):
            ev += tr.update([dict(person(1, 600, y), tid=None)], i * 0.1, i)
        self.assertEqual(ev, [])
        self.assertEqual(tr.stats["untracked"], 6)

    def test_reliability_numbers(self):
        tr = tracker(forget_sec=0.5, handover_sec=0.2)
        walk(tr, 1, [600] * 6, [300, 330, 360, 390, 420, 450])
        tr.update([], 10.0, 99)                                        # long silence: track lost
        r = tr.reliability()
        self.assertEqual((r["tracksStarted"], r["tracksLost"], r["tracksShort"]), (1, 1, 0))


if __name__ == "__main__":
    unittest.main()
