"""Dynamic AI frame rate and the two-camera limit. Pure arithmetic + the session manager with stub sessions
(no camera, no model). Run: python3 -m unittest backend.tests.test_ai_rate"""
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import backend  # noqa: E402,F401
from backend import config  # noqa: E402
from backend.services import ai_rate  # noqa: E402
from backend.services.sessions import SessionError, SessionManager  # noqa: E402


class TargetTests(unittest.TestCase):
    def test_one_camera_gets_the_maximum(self):
        self.assertEqual(ai_rate.target(1, ceiling=30, budget=30, floor=2), 30)
        self.assertEqual(ai_rate.target(0, ceiling=30, budget=30, floor=2), 30)

    def test_rate_shrinks_as_cameras_are_added(self):
        r = [ai_rate.target(n, ceiling=30, budget=30, floor=2) for n in (1, 2, 3, 4)]
        self.assertEqual(r, [30, 15, 10, 7.5])
        self.assertTrue(all(a > b for a, b in zip(r, r[1:])))

    def test_ceiling_and_floor(self):
        self.assertEqual(ai_rate.target(2, ceiling=10, budget=100, floor=2), 10)      # budget is large: ceiling wins
        self.assertEqual(ai_rate.target(8, ceiling=30, budget=10, floor=2), 2)        # floor wins


class RateEasingTests(unittest.TestCase):
    def test_moves_gradually_not_in_one_jump(self):
        r = ai_rate.Rate(30)
        r.step(100.0, ramp=6)                      # first call only sets the clock
        r.set_goal(15)
        seen = [round(r.step(100.0 + k, ramp=6), 1) for k in range(1, 5)]
        self.assertEqual(seen, [24.0, 18.0, 15.0, 15.0])                               # 6 fps per second, then holds

    def test_rises_gradually_when_a_camera_leaves(self):
        r = ai_rate.Rate(15)
        r.step(0.0, ramp=6)
        r.set_goal(30)
        self.assertEqual([round(r.step(float(k), ramp=6), 1) for k in (1, 2, 3)], [21.0, 27.0, 30.0])

    def test_new_camera_starts_at_its_goal(self):
        r = ai_rate.Rate(30)
        r.set_goal(15, immediate=True)
        self.assertEqual(r.value, 15)


class _Sess:
    """Stand-in for a CameraSession: only what the manager's rate / limit code touches."""

    def __init__(self, modules):
        self.modules = {m: {"paused": False} for m in modules}
        self.state = "processing"
        self.rate = ai_rate.Rate(config.AI_FPS_MAX)
        self.rate_fresh = True
        self.metrics = {}


class ManagerLimitTests(unittest.TestCase):
    def mgr(self, **sessions):
        m = SessionManager.__new__(SessionManager)
        m.sessions = dict(sessions)
        m.lock = threading.RLock()
        return m

    def test_two_cameras_share_the_budget_and_a_third_is_not_counted_as_running(self):
        a, b = _Sess(["ppe"]), _Sess(["pcross"])
        m = self.mgr(A=a, B=b)
        m._rebalance()
        self.assertEqual(sorted(m._running()), ["A", "B"])
        self.assertEqual(a.rate.goal, ai_rate.target(2))
        self.assertEqual(b.rate.value, ai_rate.target(2))                              # new cameras start at their share
        self.assertEqual(a.metrics["aiCameras"], 2)

    def test_leaving_camera_raises_the_other_back_to_the_maximum(self):
        a, b = _Sess(["ppe"]), _Sess(["pcross"])
        m = self.mgr(A=a, B=b)
        m._rebalance()
        b.modules.clear()
        m._rebalance()
        self.assertEqual(a.rate.goal, ai_rate.target(1))

    def test_viewer_only_session_does_not_hold_a_slot(self):
        m = self.mgr(A=_Sess(["ppe"]), V=_Sess([]))
        self.assertEqual(list(m._running()), ["A"])

    def test_third_camera_is_refused_with_a_clear_error(self):
        m = self.mgr(A=_Sess(["ppe"]), B=_Sess(["fire"]))
        m._check_camera = lambda cid: {}
        try:
            m.start_module("C", "ppe")
        except SessionError as e:
            self.assertEqual((e.code, e.status), ("CAMERA_LIMIT", 409))
            self.assertIn("2 cameras", e.message)
        else:
            self.fail("a third camera must be refused")

    def test_second_module_on_a_running_camera_is_not_a_new_camera(self):
        m = self.mgr(A=_Sess(["ppe"]), B=_Sess(["fire"]))
        m._check_camera = lambda cid: {}
        try:
            m.start_module("A", "pcross")                       # passes the limit; fails later (no model / camera), not with CAMERA_LIMIT
        except SessionError as e:
            self.assertNotEqual(e.code, "CAMERA_LIMIT")
        except Exception:
            pass


if __name__ == "__main__":
    unittest.main()


class NpuCoreTests(unittest.TestCase):
    """Dynamic NPU cores with a recording stand-in for librknnrt (no NPU here: this checks the policy, not the speed)."""

    def setUp(self):
        import os
        from backend.detection import rknn_runtime as rr
        self.rr, self.env = rr, os.environ.pop("AIV_NPU_CORE", None)
        self.soc, rr.soc = rr.soc, lambda: "rk3588"
        self.calls = []

        class Lib:
            def rknn_set_core_mask(_s, ctx, mask):
                self.calls.append(mask)
                return 0
        self.Lib = Lib

    def tearDown(self):
        import os
        self.rr.soc = self.soc
        self.rr.set_solo(False)
        if self.env is not None:
            os.environ["AIV_NPU_CORE"] = self.env

    def inst(self, home):
        m = self.rr.RKNNLite.__new__(self.rr.RKNNLite)
        m.lib, m.ctx, m.core_mask, m.home_mask, m._mask_failed = self.Lib(), None, home, home, False
        return m

    def test_default_is_rr_and_never_switches(self):
        self.assertEqual(self.rr.core_mode(), "rr")
        a = self.inst(self.rr.CORE_1)
        self.rr.set_solo(True)
        a._sync_mask()
        self.assertEqual((a.core_mask, self.calls), (self.rr.CORE_1, []))

    def test_one_camera_uses_all_cores_two_cameras_one_each_when_dynamic(self):
        import os
        os.environ["AIV_NPU_CORE"] = "dynamic"
        a = self.inst(self.rr.CORE_1)
        self.rr.set_solo(True)
        a._sync_mask()
        self.assertEqual(a.core_mask, self.rr.CORE_0_1_2)
        self.rr.set_solo(False)
        a._sync_mask()
        self.assertEqual(a.core_mask, self.rr.CORE_1)                  # back on its own core
        self.assertEqual(self.calls, [self.rr.CORE_0_1_2, self.rr.CORE_1])

    def test_no_driver_call_when_nothing_changes(self):
        import os
        os.environ["AIV_NPU_CORE"] = "dynamic"
        a = self.inst(self.rr.CORE_0)
        a._sync_mask()
        a._sync_mask()
        self.assertEqual(self.calls, [])

    def test_rr_mode_never_switches(self):
        import os
        os.environ["AIV_NPU_CORE"] = "rr"
        try:
            a = self.inst(self.rr.CORE_2)
            self.rr.set_solo(True)
            a._sync_mask()
            self.assertEqual((a.core_mask, self.calls), (self.rr.CORE_2, []))
        finally:
            del os.environ["AIV_NPU_CORE"]

    def test_refused_mask_is_kept_and_not_retried(self):
        import os
        os.environ["AIV_NPU_CORE"] = "dynamic"
        a = self.inst(self.rr.CORE_1)
        a.lib = type("L", (), {"rknn_set_core_mask": lambda s, c, m: -1})()
        self.rr.set_solo(True)
        a._sync_mask()
        a._sync_mask()
        self.assertEqual(a.core_mask, self.rr.CORE_1)
        self.assertTrue(a._mask_failed)
