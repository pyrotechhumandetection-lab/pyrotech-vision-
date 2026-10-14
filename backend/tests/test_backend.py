"""Unit tests for the PYROTECH VISION backend wrapper (no camera needed).

    python -m unittest discover -s backend/tests -t . -v      (from the repository root)

The pipeline test runs the real YOLO model on Ultralytics' bundled bus.jpg when the
weights (backend/models/yolov8n.pt) are present; it is skipped otherwise.
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # backend/
sys.path.insert(0, str(ROOT.parent))                    # repository root -> "backend" package
_tmp = tempfile.mkdtemp(prefix="aiv-test-")
os.environ["AIV_DATA_DIR"] = _tmp           # never touch the real data folder
os.environ["AIV_SNAPSHOT_DIR"] = os.path.join(_tmp, "snapshots")

import backend  # noqa: E402,F401  (sets OpenCV env first)
from backend import config  # noqa: E402
from backend.services.cameras import CameraRegistry, apply_stream_profile, host_port, split_rtsp  # noqa: E402
from backend.detection.modules import DEFAULTS, module_config, validate  # noqa: E402
from backend.services.util import redact  # noqa: E402

config.setup_logging()


class UrlTests(unittest.TestCase):
    def test_password_with_at_sign(self):
        # the existing camera's password contains '@'
        self.assertEqual(split_rtsp("rtsp://admin:pa@ss@10.0.0.1/h264/ch1/main/av_stream"),
                         ("rtsp", "admin", "pa@ss", "10.0.0.1", "/h264/ch1/main/av_stream"))

    def test_host_port(self):
        self.assertEqual(host_port("10.0.0.1"), ("10.0.0.1", 554))
        self.assertEqual(host_port("cam.local:8554"), ("cam.local", 8554))
        with self.assertRaises(ValueError):
            host_port("10.0.0.1:99999")

    def test_substream(self):
        self.assertEqual(apply_stream_profile("/h264/ch1/main/av_stream", "sub", ""), "/h264/ch1/sub/av_stream")
        self.assertEqual(apply_stream_profile("/Streaming/Channels/101", "sub", ""), "/Streaming/Channels/102")
        self.assertEqual(apply_stream_profile("/x", "main", ""), "/x")
        with self.assertRaises(ValueError):
            apply_stream_profile("/unknown", "sub", "")

    def test_redact(self):
        self.assertEqual(redact("failed rtsp://admin:pa@ss@10.0.0.1/x"), "failed rtsp://***:***@10.0.0.1/x")


class RegistryTests(unittest.TestCase):
    def test_credentials_never_public(self):
        reg = CameraRegistry()
        body = {"id": "T1", "name": "Test", "type": "rtsp", "sourceUrl": "rtsp://u:secret@10.0.0.9/s", "password": None}
        self.assertEqual(reg.validate(body), [])
        pub = reg.save(body)
        self.assertNotIn("secret", str(pub))
        self.assertEqual(pub["sourceUrl"], "rtsp://10.0.0.9/s")
        self.assertTrue(pub["hasCredentials"])
        self.assertEqual(reg.resolve_source(reg.get("T1")), "rtsp://u:secret@10.0.0.9/s")
        self.assertNotIn("secret", (Path(_tmp) / "cameras.json").read_text())
        # update without password keeps the stored one
        reg.save({**reg.get("T1"), "name": "Renamed"}, existing_id="T1")
        self.assertEqual(reg.resolve_source(reg.get("T1")), "rtsp://u:secret@10.0.0.9/s")

    def test_validation(self):
        reg = CameraRegistry()
        self.assertTrue(reg.validate({"id": "x", "name": "", "type": "rtsp", "sourceUrl": "http://a"}))

    def test_password_from_other_computer_is_reported(self):
        # a DPAPI blob that cannot be decrypted here (copied from another PC / Windows user)
        reg = CameraRegistry()
        reg.save({"id": "T2", "name": "Copied", "type": "rtsp", "sourceUrl": "rtsp://10.0.0.8/s", "username": "admin"})
        reg.creds.data["T2"] = {"username": "admin", "password": {"scheme": "dpapi", "value": "AAAA"}}
        self.assertFalse(reg.public(reg.get("T2"))["hasCredentials"])
        with self.assertRaises(ValueError) as ctx:
            reg.resolve_source(reg.get("T2"))
        self.assertIn("enter the password again", str(ctx.exception))
        # re-entering the password fixes it
        reg.save({**reg.get("T2"), "password": "newpass"}, existing_id="T2")
        self.assertEqual(reg.resolve_source(reg.get("T2")), "rtsp://admin:newpass@10.0.0.8/s")


class ModuleTests(unittest.TestCase):
    def test_defaults_are_original_values(self):
        self.assertEqual(DEFAULTS["vehicle"]["classConf"], {"2-Wheeler": 0.35, "4-Wheeler": 0.35, "Bus": 0.55, "Truck": 0.55})
        self.assertEqual(DEFAULTS["human"]["confidence"], 0.35)

    def test_validation(self):
        cfg = module_config({}, "vehicle")
        self.assertEqual(validate("vehicle", cfg, []), [])
        cfg["classes"]["plane"] = True
        self.assertTrue(validate("vehicle", cfg, []))

    def test_plan_classes(self):
        from backend.detection.pipeline import PersonVehiclePipeline
        plan = PersonVehiclePipeline.build_plan(None, module_config({}, "vehicle"), [])
        self.assertEqual(plan["coco"], [1, 2, 3, 5, 7])
        plan = PersonVehiclePipeline.build_plan(module_config({}, "human"), None, [])
        self.assertEqual(plan["coco"], [0, 1, 3])          # 2-wheelers needed for the rider rule
        self.assertNotIn("2-Wheeler", plan["draw_labels"])  # but not drawn/reported without the vehicle module


@unittest.skipUnless((ROOT / "models" / "yolov8n.pt").is_file(), "model weights not present")
class PipelineTests(unittest.TestCase):
    def test_real_detection_on_sample_image(self):
        import cv2
        import ultralytics
        from backend.detection.pipeline import PersonVehiclePipeline
        img = cv2.imread(os.path.join(os.path.dirname(ultralytics.__file__), "assets", "bus.jpg"))
        p = PersonVehiclePipeline(ROOT / "models" / "yolov8n.pt", ROOT / "models" / "bytetrack_stable.yaml")
        plan = p.build_plan(module_config({}, "human"), module_config({}, "vehicle"), [])
        new = []
        for _ in range(6):   # MIN_HITS_TO_CONFIRM + label voting need several frames
            _, dets, n, _ = p.process(img.copy(), plan)
            new += n
        labels = {n["label"] for n in new}
        self.assertIn("Bus", labels)
        self.assertIn("Person", labels)


class IndependenceTests(unittest.TestCase):
    def test_no_reference_to_old_project_folder(self):
        """No runtime file of the repository may point at the original detection folder."""
        repo = ROOT.parent
        bad = []
        for p in list(repo.rglob("*.py")) + list(repo.rglob("*.cmd")) + list(repo.rglob("*.js")) + list(repo.glob(".env.example")):
            if any(part in (".venv", "wheels", "node_modules", "tests") for part in p.parts):
                continue
            text = p.read_text(encoding="utf-8", errors="ignore").lower()
            # path-like references only ("Human Detection" is also the module's display name)
            if re.search(r"human[\\/]+human detection|program files[\\/]+human|object detection[\\/]|fire\^?&smoke|aivision_backend|run_backend\.py", text):
                bad.append(str(p.relative_to(repo)))
        self.assertEqual(bad, [])

    def test_ultralytics_offline_defaults(self):
        self.assertEqual(os.environ.get("YOLO_OFFLINE"), "1")
        self.assertEqual(os.environ.get("YOLO_AUTOINSTALL"), "0")
        self.assertTrue(os.environ.get("YOLO_CONFIG_DIR", "").startswith(str(ROOT)))


class LayoutTests(unittest.TestCase):
    def test_default_paths_point_into_backend(self):
        self.assertEqual(config.MODEL_PATH, ROOT / "models" / "yolov8n.pt")
        self.assertEqual(config.TRACKER_CONFIG, ROOT / "models" / "bytetrack_stable.yaml")
        self.assertTrue(config.MODEL_PATH.is_file(), "model weights missing in backend/models")
        self.assertTrue(config.TRACKER_CONFIG.is_file(), "tracker config missing in backend/models")
        self.assertTrue((ROOT / "models" / "yolov8s.pt").is_file(), "optional yolov8s.pt missing in backend/models")
        self.assertTrue((ROOT / "requirements-lock.txt").is_file())


if __name__ == "__main__":
    unittest.main()


class FpsAndVehicleTests(unittest.TestCase):
    """Camera FPS monitoring, exact-moment snapshots, fast-vehicle tracking, vehicle filters."""

    def test_camera_host(self):
        from backend.services.util import camera_host
        self.assertEqual(camera_host({"sourceUrl": "rtsp://admin:p@ss@192.168.192.87:554/ch01.264"}), "192.168.192.87")
        self.assertEqual(camera_host({"sourceUrl": "rtsp://192.168.192.3/h264"}), "192.168.192.3")
        self.assertIsNone(camera_host({"sourceUrl": "0"}))

    def test_engine_falls_back_to_pytorch_without_openvino(self):
        import builtins
        real_import = builtins.__import__

        def no_openvino(name, *a, **k):
            if name == "openvino":
                raise ImportError("not installed")
            return real_import(name, *a, **k)
        builtins.__import__ = no_openvino
        try:
            self.assertEqual(config.resolve_engine(), ("pytorch", config.MODEL_PATH))
        finally:
            builtins.__import__ = real_import

    def test_openvino_model_is_shipped(self):
        self.assertTrue((config.OPENVINO_MODEL_PATH / "yolov8n.xml").is_file())
        self.assertTrue((config.OPENVINO_MODEL_PATH / "yolov8n.bin").is_file())
        self.assertIn("dynamic: true", (config.OPENVINO_MODEL_PATH / "metadata.yaml").read_text())

    def test_buffered_iou_only_for_vehicles(self):
        import numpy as np
        from types import SimpleNamespace
        from backend.detection.tracker import BufferedByteTracker

        def obj(x1, cls):
            return SimpleNamespace(xyxy=np.array([x1, 100, x1 + 100, 180], dtype=np.float32), cls=cls, angle=None, score=0.9)
        tr = BufferedByteTracker.__new__(BufferedByteTracker)
        tr.args = SimpleNamespace(vehicle_iou_buffer=0.3, fuse_score=False)
        # a car that moved 120 px (more than its 100 px width): no plain overlap, buffered overlap
        self.assertLess(tr.get_dists([obj(0, 2.0)], [obj(120, 2.0)])[0, 0], 1.0)
        # persons keep the original ByteTrack cost (no overlap -> cost 1)
        self.assertEqual(tr.get_dists([obj(0, 0.0)], [obj(120, 0.0)])[0, 0], 1.0)
        tr.args.vehicle_iou_buffer = 0
        self.assertEqual(tr.get_dists([obj(0, 2.0)], [obj(120, 2.0)])[0, 0], 1.0)

    def test_vehicle_snapshot_default_has_no_class_cooldown(self):
        # code default (a .env may still override it with AIV_SNAPSHOT_COOLDOWN)
        self.assertIn('_env("AIV_SNAPSHOT_COOLDOWN", "0")', (ROOT / "config.py").read_text())

    def test_exact_moment_snapshot_and_dedupe(self):
        import time
        import numpy as np
        from backend.services.events import EventStore
        ev = EventStore()
        cam = {"id": "CAM-T", "name": "road", "sourceUrl": "rtsp://192.168.192.87:554/ch01.264"}
        cfg = module_config({}, "vehicle")
        captured = time.time() - 2.5                       # frame received 2.5 s before the event is written
        frame = np.zeros((90, 160, 3), np.uint8)
        frame[:, :80] = 200                                 # the triggering frame's content
        item = {"trackId": 7, "label": "Truck", "module": "vehicle", "confidence": 0.81, "zone": None, "box": [0.1, 0.2, 0.5, 0.6]}
        e1 = ev.on_confirmation(cam, "vehicle", cfg, item, frame, captured_at=captured)
        frame[:] = 0                                        # the loop moves on / draws on the next frame
        self.assertIsNone(ev.on_confirmation(cam, "vehicle", cfg, item, frame, captured_at=captured), "same track reported twice")
        item2 = dict(item, trackId=8, label="Truck")       # another truck right after: its own snapshot (no class cooldown)
        ev.on_confirmation(cam, "vehicle", cfg, item2, frame, captured_at=captured + 0.2)
        self.assertTrue(ev.flush(10))
        snaps = [s for s in ev.snapshots.data if s["cameraId"] == "CAM-T"]
        self.assertEqual(len(snaps), 2)
        s1 = next(s for s in snaps if s["eventId"] == e1["id"])
        self.assertEqual((s1["label"], s1["vehicleClass"], s1["confidence"], s1["cameraIp"], s1["trackId"]),
                         ("Truck", "truck", 0.81, "192.168.192.87", 7))
        from backend.services.util import iso_from_ts
        self.assertEqual(s1["timestamp"], iso_from_ts(captured))
        import cv2
        img = cv2.imread(str(ev.snapshot_root() / s1["path"]))
        self.assertGreater(img[:, :60].mean(), 150, "snapshot must be the frame that triggered the detection")
        d1 = next(d for d in ev.detections.data if d["id"] == e1["id"])
        self.assertEqual((d1["snapshotId"], d1["vehicleClass"], d1["cameraIp"]), (s1["id"], "truck", "192.168.192.87"))

    def test_vehicle_class_filter(self):
        from backend.api.server import _filter
        recs = [{"module": "vehicle", "label": "4-Wheeler", "cameraId": "A"}, {"module": "vehicle", "label": "Truck", "cameraId": "A"},
                {"module": "vehicle", "label": "2-Wheeler", "cameraId": "A", "vehicleClass": "two_wheeler"}, {"module": "human", "label": "Person", "cameraId": "A"}]
        self.assertEqual([r["label"] for r in _filter(recs, {"vehicleClass": ["car"]})], ["4-Wheeler"])
        self.assertEqual([r["label"] for r in _filter(recs, {"vehicleClass": ["two_wheeler"]})], ["2-Wheeler"])
        self.assertEqual(len(_filter(recs, {})), 4)


class HumanCountTests(unittest.TestCase):
    """Human Count counting rules, with scripted detections (no model needed)."""

    @staticmethod
    def _counter(min_frames=3, keep=2.0, zones=None, cam_zones=None):
        import numpy as np
        from backend.detection import human_count as H
        hc = H.HumanCounter.__new__(H.HumanCounter)
        hc.engine = "test"
        hc.tracker = H.PeopleTracker()
        hc.reset()
        hc.configure({"confidence": 0.4, "counting": {"minFrames": min_frames, "keepLostSec": keep}, "zones": zones or []}, cam_zones or [])
        script = {"dets": np.zeros((0, 5), np.float32)}
        hc.detector = type("D", (), {"detect": lambda self, frame, conf: script["dets"]})()
        return hc, script

    def _run(self, hc, script, frames, fps=10.0):
        import numpy as np
        img = np.zeros((480, 640, 3), np.uint8)
        for i, boxes in enumerate(frames):
            script["dets"] = np.array([b + [0.8] for b in boxes], np.float32).reshape(-1, 5)
            hc.process(img, img.copy(), i / fps)
        return hc

    def test_walking_person_counted_once(self):
        hc, sc = self._counter()
        self._run(hc, sc, [[[20 + 8 * i, 200, 60 + 8 * i, 300]] for i in range(60)])
        self.assertEqual((hc.unique, hc.current, hc.peak), (1, 1, 1))

    def test_short_occlusion_keeps_identity(self):
        hc, sc = self._counter(keep=2.0)
        frames = [[[20 + 8 * i, 200, 60 + 8 * i, 300]] if not 20 <= i < 30 else [] for i in range(50)]   # hidden 1.0 s
        self._run(hc, sc, frames)
        self.assertEqual(hc.unique, 1, "the same person must not be counted again after a 1 s occlusion")

    def test_long_absence_is_a_new_visit(self):
        hc, sc = self._counter(keep=2.0)
        frames = [[[100, 200, 140, 300]] if not 10 <= i < 40 else [] for i in range(50)]                   # gone 3 s
        self._run(hc, sc, frames)
        self.assertEqual(hc.unique, 2)

    def test_flicker_is_not_counted(self):
        hc, sc = self._counter(min_frames=3)
        frames = [[[300, 100, 340, 200]] if i in (5, 15, 25) else [] for i in range(40)]                   # single-frame blips
        self._run(hc, sc, frames)
        self.assertEqual(hc.unique, 0)

    def test_two_people_crossing(self):
        hc, sc = self._counter()
        frames = [[[20 + 10 * i, 200, 60 + 10 * i, 300], [580 - 10 * i, 205, 620 - 10 * i, 305]] for i in range(56)]
        self._run(hc, sc, frames)
        self.assertEqual(hc.unique, 2)
        self.assertEqual(hc.peak, 2)

    def test_partial_occlusion_keeps_identity(self):
        hc, sc = self._counter()
        frames = []
        for i in range(40):
            x = 100 + 6 * i
            h = 100 if not 15 <= i < 22 else 45          # legs hidden behind an object, then fully visible again
            frames.append([[x, 200, x + 40, 200 + h]])
        self._run(hc, sc, frames)
        self.assertEqual(hc.unique, 1)

    def test_zone_counts_only_people_standing_inside(self):
        zone = {"id": "z1", "name": "Door", "type": "rectangle", "x": 0.5, "y": 0.0, "width": 0.5, "height": 1.0, "enabled": True}
        hc, sc = self._counter(zones=["z1"], cam_zones=[zone])
        self._run(hc, sc, [[[50, 200, 90, 300], [400, 200, 440, 300]] for _ in range(20)])
        self.assertEqual((hc.unique, hc.current), (1, 1))

    def test_static_person_counted_once_and_history(self):
        hc, sc = self._counter()
        self._run(hc, sc, [[[300, 100, 340, 200]] for _ in range(1300)])     # 130 s
        self.assertEqual(hc.unique, 1)
        mins = hc.flush()
        self.assertGreaterEqual(len(mins), 2)
        self.assertEqual(max(m["max"] for m in mins), 1)
        self.assertEqual(sum(m["new"] for m in mins), 1)

    def test_count_module_registered_and_validated(self):
        from backend.detection.modules import MODULES
        self.assertIn("count", MODULES)
        cfg = module_config({}, "count")
        self.assertEqual(validate("count", cfg, []), [])
        bad = dict(cfg, counting={"minFrames": 0, "keepLostSec": 99}, alert=dict(cfg["alert"], threshold=-1))
        self.assertEqual(len(validate("count", bad, [])), 3)


class FramePipelineTests(unittest.TestCase):
    """Per-camera frame queue (explicit overload policy), camera-side gaps, AI stream selection, fire engine."""

    def _reader(self, cap):
        from backend.detection.frame_reader import LatestFrameReader
        r = LatestFrameReader.__new__(LatestFrameReader)       # no camera: drive the queue logic directly
        import threading
        r.lock = threading.Lock(); r.qcond = threading.Condition(r.lock)
        r.queue = None; r.queue_cap = 0; r.queue_dropped = 0; r.queue_peak = 0
        r.camera_gaps = 0; r._last_pts = None; r.nominal_fps = 20.0
        r.enable_queue(cap)
        return r

    def _push(self, r, seq):
        import numpy as np
        with r.lock:     # same steps as the reader thread
            if len(r.queue) >= r.queue_cap:
                r.queue.popleft(); r.queue_dropped += 1
            r.queue.append((seq, np.zeros((2, 2, 3), np.uint8), float(seq)))
            r.queue_peak = max(r.queue_peak, len(r.queue))

    def test_queue_keeps_order_and_counts_overload(self):
        r = self._reader(5)
        for i in range(1, 9):
            self._push(r, i)
        self.assertEqual(r.queue_dropped, 3)                    # 8 frames into 5 places: 3 oldest dropped, counted
        got = [r.next_frame(0)[0] for _ in range(5)]
        self.assertEqual(got, [4, 5, 6, 7, 8])                  # FIFO, newest kept, no frame repeated
        self.assertIsNone(r.next_frame(0))
        self.assertEqual(r.queue_peak, 5)

    def test_camera_gaps_from_stream_timestamps(self):
        r = self._reader(4)
        for pts in (50, 100, 150, 300, 350, 9000, 9050):       # 150 -> 300 = 2 frames missing; 350 -> 9000 = reconnect
            r._count_gap(pts)
        self.assertEqual(r.camera_gaps, 2)

    def test_ai_stream_resolution(self):
        reg = CameraRegistry()
        base = {"id": "AI-1", "name": "AI one", "type": "rtsp", "requiresAuth": False, "enabled": True,
                "sourceUrl": "rtsp://10.0.0.5:554/Streaming/Channels/101", "streamProfile": "main"}
        self.assertEqual(reg.validate(dict(base)), [])
        src, kind = reg.resolve_ai_source(dict(base))
        self.assertEqual((src, kind), ("rtsp://10.0.0.5:554/Streaming/Channels/101", "same"))
        src, kind = reg.resolve_ai_source(dict(base, aiStream="sub"))
        self.assertEqual((src, kind), ("rtsp://10.0.0.5:554/Streaming/Channels/102", "sub"))
        src, kind = reg.resolve_ai_source(dict(base, aiStream="custom", aiCustomStream="/Streaming/Channels/103"))
        self.assertEqual((src, kind), ("rtsp://10.0.0.5:554/Streaming/Channels/103", "custom"))
        # no substream is ever guessed for an unknown URL pattern
        odd = dict(base, id="AI-2", name="AI two", sourceUrl="rtsp://10.0.0.6/live", aiStream="sub")
        self.assertTrue(any("cannot derive the substream" in e for e in reg.validate(odd)))
        self.assertTrue(any("AI stream path" in e for e in reg.validate(dict(base, aiStream="custom"))))
        self.assertTrue(any("aiStream" in e for e in reg.validate(dict(base, aiStream="tiny"))))
        self.assertTrue(any("snapshotSource" in e for e in reg.validate(dict(base, snapshotSource="x"))))

    def test_fire_openvino_model_present_and_selected(self):
        from backend.detection import fire_smoke as fs
        self.assertTrue((fs.OV_DIR / "firedetect-11s.xml").is_file())
        self.assertTrue((fs.OV_DIR / "firedetect-11s.bin").is_file())
        old = os.environ.get("AIV_FIRE_ENGINE")
        try:
            os.environ["AIV_FIRE_ENGINE"] = "pytorch"
            self.assertEqual(fs.resolve_engine(), ("pytorch", fs.WEIGHTS))
            os.environ["AIV_FIRE_ENGINE"] = "auto"
            want = "openvino" if config.openvino_available() else "pytorch"
            self.assertEqual(fs.resolve_engine()[0], want)
        finally:
            if old is None:
                os.environ.pop("AIV_FIRE_ENGINE", None)
            else:
                os.environ["AIV_FIRE_ENGINE"] = old


class AutoStartTests(unittest.TestCase):
    """Detections that were running are resumed after a backend restart (AI box reboot)."""

    def setUp(self):
        from backend.services import autostart
        self.mod = autostart
        self.path = config.DATA_DIR / "running_modules.json"
        if self.path.exists():
            self.path.unlink()
        self.old = (autostart.FIRST_DELAY_S, autostart.RETRY_MIN_S)
        autostart.FIRST_DELAY_S, autostart.RETRY_MIN_S = 0, 0.2

    def tearDown(self):
        self.mod.FIRST_DELAY_S, self.mod.RETRY_MIN_S = self.old
        if self.path.exists():
            self.path.unlink()

    def _fake(self, fail_first=0, code="CAMERA_UNAVAILABLE"):
        from backend.services.sessions import SessionError
        calls = []

        class Sessions:
            def start_module(self, cam_id, mod):
                calls.append((cam_id, mod))
                if len([c for c in calls if c == (cam_id, mod)]) <= fail_first:
                    raise SessionError(code, "Cannot connect to rtsp://admin:secret@10.0.0.9/stream", 503)
        return Sessions(), calls

    def _wait(self, cond, sec=5):
        import time as _t
        end = _t.time() + sec
        while _t.time() < end and not cond():
            _t.sleep(0.05)
        return cond()

    def test_record_forget_persist(self):
        a = self.mod.AutoStart(None, None)
        a.record("CAM-01", "human"); a.record("CAM-01", "fire"); a.record("CAM-01", "human"); a.record("CAM-02", "count")
        b = self.mod.AutoStart(None, None)            # a new backend process reads the same file
        self.assertEqual(sorted(b.remembered()), [("CAM-01", "fire"), ("CAM-01", "human"), ("CAM-02", "count")])
        b.forget("CAM-01", "human"); b.forget("CAM-02")
        self.assertEqual(self.mod.AutoStart(None, None).remembered(), [("CAM-01", "fire")])

    def test_resume_with_retry_until_camera_answers(self):
        a = self.mod.AutoStart(None, None)
        a.record("CAM-01", "fire")
        sessions, calls = self._fake(fail_first=2)
        b = self.mod.AutoStart(sessions, None)
        b.start()
        self.assertTrue(self._wait(lambda: b.state.get("CAM-01|fire", {}).get("status") == "running"), b.status())
        b.stop()
        self.assertEqual(calls, [("CAM-01", "fire")] * 3)
        self.assertNotIn("secret", str(b.status()))       # credentials never kept in the status

    def test_permanent_error_and_user_stop(self):
        a = self.mod.AutoStart(None, None)
        a.record("CAM-09", "human")
        sessions, calls = self._fake(fail_first=99, code="CAMERA_NOT_FOUND")
        b = self.mod.AutoStart(sessions, None)
        b.start()
        self.assertTrue(self._wait(lambda: b.status()["remembered"][0].get("status") == "failed"))
        b.stop()
        self.assertEqual(len(calls), 1)                  # not retried
        # a module the user stopped while retrying is not started again
        a2 = self.mod.AutoStart(None, None); a2.forget("CAM-09"); a2.record("CAM-03", "count")
        sessions2, calls2 = self._fake(fail_first=99)
        c = self.mod.AutoStart(sessions2, None)
        c.start()
        self.assertTrue(self._wait(lambda: len(calls2) >= 1))
        c.forget("CAM-03", "count")
        n = len(calls2)
        import time as _t
        _t.sleep(1.0)
        c.stop()
        self.assertEqual(len(calls2), n)

    def test_can_be_switched_off(self):
        os.environ["AIV_AUTOSTART"] = "0"
        try:
            a = self.mod.AutoStart(None, None)
            a.record("CAM-01", "human")
            a.start()
            self.assertIsNone(a.thread)
        finally:
            del os.environ["AIV_AUTOSTART"]


class ClientDisconnectTests(unittest.TestCase):
    def test_client_closed_connection_is_not_an_error(self):
        """WinError 10053 (ConnectionAbortedError) while answering must not be logged as an unhandled error."""
        import logging
        from backend.api.server import Handler
        for exc in (ConnectionAbortedError(10053, "aborted"), ConnectionResetError(), BrokenPipeError()):
            h = Handler.__new__(Handler)
            h.path = "/api/system/health"
            h.app = None

            def boom(*a, **k):
                raise exc
            h._send_json = boom
            with self.assertLogs("aivision.api", level="DEBUG") as cm:
                h._dispatch("GET")
            self.assertFalse([r for r in cm.records if r.levelno >= logging.ERROR], f"{type(exc).__name__} logged as error")

    def test_events_version_changes_with_records(self):
        from backend.api.server import events_version
        from backend.services.events import EventStore
        app = type("A", (), {})()
        app.events = EventStore()
        v1 = events_version(app, {}, None)["version"]
        app.events.raise_alert({"id": "CAM-V", "name": "v"}, "camera_disconnected", "medium", "test")
        v2 = events_version(app, {}, None)["version"]
        self.assertNotEqual(v1, v2)
        self.assertEqual(v2, events_version(app, {}, None)["version"])


class HeadCountTests(unittest.TestCase):
    """Human Head Count (still frame): geometry, duplicate removal, area filter, saving - scripted detector,
    plus the shipped model files."""

    class FakeDet:
        engine = "test"

        def __init__(self, boxes):
            import numpy as np
            self.boxes = np.array(boxes, np.float32).reshape(-1, 5)
            self.calls = []

        def detect(self, img, conf, imgsz):
            # boxes are given in FULL-frame coordinates; return them relative to the crop received
            self.calls.append(img.shape[:2])
            ox, oy = self.offset
            d = self.boxes.copy()
            d[:, [0, 2]] -= ox
            d[:, [1, 3]] -= oy
            h, w = img.shape[:2]
            keep = (d[:, 0] >= 0) & (d[:, 1] >= 0) & (d[:, 2] <= w) & (d[:, 3] <= h) & (d[:, 4] >= conf)
            return d[keep]

    def _count(self, boxes, zones, settings=None, frame_wh=(1000, 600)):
        import numpy as np
        from backend.detection import head_count as hc
        frame = np.zeros((frame_wh[1], frame_wh[0], 3), np.uint8)
        det = self.FakeDet(boxes)
        orig = hc.count_heads

        # learn the crop offset the same way count_heads computes it
        polys = [p for p in (hc.zone_polygon_px(z, frame_wh[0], frame_wh[1]) for z in zones) if p is not None]
        if polys:
            allp = np.concatenate(polys)
            pad = max(16, int(0.03 * max(frame_wh)))
            det.offset = (max(int(allp[:, 0].min()) - pad, 0), max(int(allp[:, 1].min()) - pad, 0))
        else:
            det.offset = (0, 0)
        return orig(frame, zones, dict({"tiling": "off"}, **(settings or {})), det), det

    def test_settings_validation(self):
        from backend.detection.head_count import validate_settings, DEFAULT_SETTINGS
        s, e = validate_settings({})
        self.assertEqual((s, e), (DEFAULT_SETTINGS, []))
        s, e = validate_settings({"confidence": 2, "iou": "x", "tiling": "maybe", "imgSize": 1000})
        self.assertEqual(len(e), 3)
        self.assertEqual(s["imgSize"], 992)          # multiple of 32

    def test_nms_merges_duplicates_keeps_neighbours(self):
        import numpy as np
        from backend.detection.head_count import nms_merge
        d = np.array([[10, 10, 30, 30, .9], [11, 11, 31, 31, .8],          # same head twice
                      [40, 10, 60, 30, .7],                                 # neighbour (touching, separate)
                      [12, 12, 22, 22, .6]], np.float32)                    # partial box inside the first head
        out = nms_merge(d, 0.45)
        self.assertEqual(len(out), 2)
        self.assertEqual(sorted(round(float(v), 2) for v in out[:, 4]), [0.7, 0.9])

    def test_only_heads_inside_the_area_counted_once_and_numbered(self):
        zone = {"type": "polygon", "points": [{"x": 0.1, "y": 0.1}, {"x": 0.6, "y": 0.1}, {"x": 0.6, "y": 0.9}, {"x": 0.1, "y": 0.9}]}
        boxes = [[200, 300, 230, 330, .9], [201, 301, 231, 331, .85],      # one head, two boxes
                 [400, 100, 430, 130, .8],                                  # inside, upper row
                 [150, 305, 180, 335, .7],                                  # inside, same row, left
                 [700, 300, 730, 330, .95],                                 # outside the area
                 [300, 300, 304, 304, .9],                                  # too small (4 px)
                 [500, 400, 530, 430, .2]]                                  # below confidence
        r, det = self._count(boxes, [zone])
        self.assertEqual(len(r["heads"]), 3)
        self.assertEqual([h["id"] for h in r["heads"]], [1, 2, 3])
        self.assertEqual([h["box"][0] for h in r["heads"]], [400, 150, 200])   # reading order: rows, then left->right
        # the model only saw the area's bounding box (+ margin), not the whole frame
        self.assertLess(det.calls[0][1], 1000)

    def test_whole_frame_and_rectangle(self):
        r, _ = self._count([[10, 10, 40, 40, .9], [900, 500, 930, 530, .9]], [])
        self.assertEqual(len(r["heads"]), 2)
        rect = {"type": "rectangle", "x": 0.5, "y": 0.5, "width": 0.5, "height": 0.5}
        r, _ = self._count([[10, 10, 40, 40, .9], [900, 500, 930, 530, .9]], [rect])
        self.assertEqual(len(r["heads"]), 1)

    def test_no_heads(self):
        r, _ = self._count([], [])
        self.assertEqual(r["heads"], [])

    def test_tiles_cover_the_crop(self):
        from backend.detection.head_count import _tiles
        for w, h in ((3000, 2000), (2600, 1000), (4000, 3000)):
            t = _tiles(w, h, 1280)
            self.assertGreaterEqual(len(t), 4)
            self.assertEqual(max(x + tw for x, y, tw, th in t), w)
            self.assertEqual(max(y + th for x, y, tw, th in t), h)
            self.assertEqual(min(x for x, *_ in t), 0)

    def test_annotation_does_not_touch_the_original(self):
        import numpy as np
        from backend.detection import head_count as hc
        frame = np.full((480, 640, 3), 120, np.uint8)
        res = {"heads": [{"id": 1, "box": [100, 100, 130, 130], "confidence": .9}],
               "polygons": [np.array([(0, 0), (320, 0), (320, 480), (0, 480)], np.float32)]}
        out = hc.annotate(frame, res, [{"color": "#13D99A"}], {"cameraIp": "192.168.1.5", "capturedAt": "2026-01-01 10:00:00"})
        self.assertTrue((frame == 120).all())
        self.assertEqual(out.shape, frame.shape)
        self.assertLess(out[400, 600].mean(), 120)       # outside the area is dimmed
        self.assertFalse((out == frame).all())

    def test_roi_validation(self):
        from backend.services.headcount import validate_roi
        self.assertEqual(validate_roi([{"type": "rectangle", "x": 0, "y": 0, "width": 1, "height": 1}]), [])
        self.assertTrue(validate_roi([]))
        self.assertTrue(validate_roi([{"type": "polygon", "points": [{"x": 0, "y": 0}, {"x": 1, "y": 0}]}]))
        self.assertTrue(validate_roi([{"type": "rectangle", "x": 0.8, "y": 0, "width": 0.5, "height": 1}]))

    def test_capture_and_count_saves_original_and_annotated(self):
        import numpy as np
        import cv2
        from backend.detection import head_count as hc
        from backend.services.events import EventStore
        from backend.services.headcount import HeadCountError, HeadCountService
        yy, xx = np.mgrid[0:360, 0:640]
        frame = np.dstack([(xx * 255 // 640), (yy * 255 // 360), ((xx + yy) % 256)]).astype(np.uint8)
        cam = {"id": "CAM-H", "name": "hall", "sourceUrl": "rtsp://admin:secret@192.168.9.9:554/s", "zones": [
            {"id": "a", "name": "Area", "type": "rectangle", "x": 0, "y": 0, "width": 0.5, "height": 1, "enabled": True, "modules": ["count"]},
            {"id": "b", "name": "Other", "type": "rectangle", "x": 0.5, "y": 0, "width": 0.5, "height": 1, "enabled": True, "modules": ["human"]}]}
        reader = type("R", (), {"read_latest": lambda self: (True, 1, frame.copy(), __import__("time").time())})()
        sessions = type("S", (), {"get": lambda self, cid: type("X", (), {"reader": reader})()})()
        svc = HeadCountService(None, sessions, EventStore())
        fake = self.FakeDet([[50, 50, 80, 80, .9], [500, 50, 530, 80, .9]])
        fake.offset = (0, 0)
        old = hc._detector
        hc._detector = fake
        try:
            r = svc.capture_and_count(cam, {"cameraId": "CAM-H"})
            with self.assertRaises(HeadCountError):
                svc.capture_and_count(dict(cam, zones=[]), {"cameraId": "CAM-H"})
        finally:
            hc._detector = old
        self.assertEqual(r["totalHeads"], 1)                 # the head in "Other" (human-only zone) is not counted
        self.assertEqual(r["frameSource"], "session")
        self.assertEqual(r["cameraIp"], "192.168.9.9")
        self.assertNotIn("secret", str(r))
        rec, p = svc.events.snapshot_file(r["snapshot"]["id"])
        _r, po = svc.events.snapshot_original_file(r["snapshot"]["id"])
        self.assertTrue(p.is_file() and po.is_file())
        orig = cv2.imread(str(po))
        self.assertEqual(orig.shape, frame.shape)
        self.assertLess(np.abs(orig.astype(int) - frame.astype(int)).mean(), 3)   # same frame (JPEG noise only)
        self.assertEqual(rec["type"], "head_count")
        self.assertTrue(svc.events.delete_snapshot(rec["id"]))
        self.assertFalse(po.exists())

    def test_model_parts_join_to_the_published_weights(self):
        import hashlib
        from backend.detection import head_count as hc
        for target in (hc.PT_FILE, hc.OV_BIN):
            parts = sorted(target.parent.glob(target.name + ".part*"))
            self.assertTrue(parts, f"{target.name}.part* missing")
            self.assertTrue(all(p.stat().st_size < 30_000_000 for p in parts))
            h = hashlib.sha256()
            for p in parts:
                h.update(p.read_bytes())
            self.assertEqual(h.hexdigest(), hc.SHA256[target.name])
        self.assertTrue((hc.OV_DIR / "head_yolov8m.xml").is_file())

    def test_assemble_rejects_incomplete_parts(self):
        from backend.detection import head_count as hc
        d = Path(tempfile.mkdtemp())
        (d / "m.bin.part0").write_bytes(b"abc")
        (d / "m.bin.part1").write_bytes(b"def")
        hc.SHA256["m.bin"] = "0" * 64
        with self.assertRaises(hc.HeadModelError):
            hc.assemble(d / "m.bin")
        self.assertFalse((d / "m.bin").exists())
        import hashlib
        hc.SHA256["m.bin"] = hashlib.sha256(b"abcdef").hexdigest()
        self.assertEqual(hc.assemble(d / "m.bin").read_bytes(), b"abcdef")
        del hc.SHA256["m.bin"]


class FireSmokeTests(unittest.TestCase):
    """Fire & Smoke module: alert window, smoke rules, settings (no camera; scripted detections)."""

    def test_defaults_and_legacy_camera_form_settings(self):
        from backend.detection.modules import DEFAULTS
        cfg = module_config({"detection": {"modules": {"fire": {"enabled": False, "confidence": 0.45, "fire": True, "smoke": False,
                                                                "severity": "high", "zones": []}}}}, "fire")
        self.assertEqual(cfg["classConf"], {"Fire": 0.30, "Smoke": 0.65})
        self.assertEqual(cfg["confidence"], 0.50)                 # placeholder 0.45 ignored
        self.assertEqual(cfg["classes"], {"fire": True, "smoke": False})
        self.assertEqual(cfg["alert"]["severity"], "high")
        self.assertEqual(validate("fire", cfg, []), [])
        self.assertEqual(validate("fire", DEFAULTS["fire"], []), [])
        bad = dict(cfg, imgSize=500, everyNFrames=0, alertRule={"consecutiveFrames": 3, "windowFrames": 6, "minHits": {"fire": 0, "smoke": 3}})
        self.assertEqual(len(validate("fire", bad, [])), 3)

    def test_alert_window_m_of_k_and_cooldown(self):
        from backend.detection.fire_smoke import AlertWindow, Detection
        w = AlertWindow({"consecutiveFrames": 3, "windowFrames": 6, "minHits": {"fire": 2, "smoke": 3}}, 10, None)
        fire = [Detection("fire", 0.6, (10, 10, 50, 50))]
        self.assertEqual(w.update(fire, (480, 640, 3), 0.0), [])          # 1 of 6
        self.assertEqual(w.update([], (480, 640, 3), 0.5), [])
        due = w.update(fire, (480, 640, 3), 1.0)                           # 2 of 6 -> alert
        self.assertEqual([d[0] for d in due], ["fire"])
        self.assertEqual(w.update(fire, (480, 640, 3), 2.0), [])          # cooldown 10 s
        self.assertEqual([d[0] for d in w.update(fire, (480, 640, 3), 11.5)], ["fire"])
        weak = [Detection("smoke", 0.5, (10, 10, 50, 50), weak=True)]
        for t in range(6):
            self.assertEqual([d for d in w.update(weak, (480, 640, 3), 20 + t) if d[0] == "smoke"], [])   # weak smoke alone never alerts

    def test_weak_smoke_counts_with_fire_nearby(self):
        from backend.detection.fire_smoke import AlertWindow, Detection
        rules = {"enabled": True, "lowConf": 0.4, "fireNearFrac": 0.15, "persistS": 20, "minRiseFrac": 0.03, "minGrowth": 1.3,
                 "longPersistS": 90, "maxGapS": 4, "driftFrac": 0.08}
        w = AlertWindow({"consecutiveFrames": 3, "windowFrames": 6, "minHits": {"fire": 2, "smoke": 3}}, 10, rules)
        frame = [Detection("fire", 0.7, (100, 300, 160, 380)), Detection("smoke", 0.5, (90, 200, 200, 300), weak=True)]
        labels = [lab for t in range(4) for lab, *_ in w.update(frame, (480, 640, 3), float(t))]
        self.assertIn("smoke", labels)

    def test_green_overlay_is_erased(self):
        import numpy as np
        from backend.detection.fire_smoke import remove_green_overlay
        img = np.full((200, 300, 3), 90, np.uint8)
        img[50:52, 40:200] = (0, 255, 0)
        out, n = remove_green_overlay(img)
        self.assertGreater(n, 50)
        self.assertTrue((out[50:52, 40:200, 1] < 150).all())


class ObjectMonitorTests(unittest.TestCase):
    """Object Monitoring: the ported monitor on a synthetic scene (moved / missing / restored) and the API config."""

    def _scene(self):
        import numpy as np
        rng = np.random.default_rng(3)
        yy, xx = np.mgrid[0:480, 0:640]      # smooth scene (a random-noise scene trips the whole-picture-change guard)
        bg = np.dstack([60 + xx // 8, 80 + yy // 8, 100 + (xx + yy) // 16]).astype(np.uint8)
        obj = (rng.integers(0, 255, (60, 80, 3))).astype(np.uint8)
        return bg, obj

    def test_moved_missing_restored(self):
        import copy
        from backend.detection.modules import DEFAULTS
        from backend.detection.object_monitor.monitor import ObjectMonitor
        bg, obj = self._scene()
        home = bg.copy(); home[200:260, 100:180] = obj
        moved = bg.copy(); moved[200:260, 400:480] = obj
        cfg = dict(copy.deepcopy(DEFAULTS["object"]["monitor"]), check_every_n_frames=1, min_moved_s=0, min_missing_s=0,
                   confirm_checks=2, restore_checks=2, cooldown_s=0, tracker="mil")
        clock = [0.0]
        alerts, infos = [], []
        mon = ObjectMonitor(cfg, on_alert=lambda v, k, ev, f: alerts.append(k), on_info=infos.append, clock=lambda: clock[0])
        mon.request_add(home, (100, 200, 80, 60), "box")
        for frame in [home] * 3 + [moved] * 3 + [home] * 3 + [bg] * 3:
            clock[0] += 1
            mon.process(frame, [])
        self.assertEqual(alerts, ["moved", "missing"])
        self.assertTrue(any(i.startswith("RESTORED") for i in infos))
        mon.request("remove", 1)
        mon.process(bg, [])
        self.assertEqual(mon.view(), [])

    def test_object_config_and_legacy(self):
        cfg = module_config({"detection": {"modules": {"object": {"alert": "alert", "target": "x", "missingAfterSec": 30,
                                                                  "monitor": {"move_threshold_px": 55}}}}}, "object")
        self.assertIsInstance(cfg["alert"], dict)
        self.assertEqual(cfg["monitor"]["move_threshold_px"], 55)
        self.assertEqual(validate("object", cfg, []), [])
        self.assertTrue(validate("object", dict(cfg, monitor=dict(cfg["monitor"], lost_score=0.9)), []))

    def test_models_present(self):
        from backend.detection import fire_smoke
        from backend.detection.object_monitor import engine
        from backend.detection.modules import DEFAULTS
        self.assertTrue(fire_smoke.WEIGHTS.is_file() and fire_smoke.VERIFIER_FILE.is_file())
        self.assertTrue((engine.MODELS_DIR / DEFAULTS["object"]["detector"]["model"]).is_file())
        caches = list(engine.MODELS_DIR.glob("yoloe-26n-seg_vocab_*.npz"))
        self.assertEqual(len(caches), 1)
        from backend.detection.object_monitor.open_vocab import prompt_table
        import hashlib
        prompts, _ = prompt_table(DEFAULTS["object"]["detector"]["vocabulary"])
        self.assertEqual(caches[0].stem.split("_")[-1], hashlib.sha1("\n".join(prompts).encode()).hexdigest()[:10],
                         "the shipped vocabulary cache must match the default vocabulary")


class PerfRuntimeTests(unittest.TestCase):
    """CPU settings (backend/runtime.py), shared person pass, and the perf tool's safety logic."""

    def test_cpu_list(self):
        from backend.runtime import parse_cpu_list, _cpu_list_text
        self.assertEqual(parse_cpu_list("0,2,4-7"), [0, 2, 4, 5, 6, 7])
        self.assertEqual(_cpu_list_text([0, 2, 4, 5, 6, 7]), "0,2,4-7")
        for bad in ("", "5-3", "-1", "a"):
            with self.assertRaises(ValueError):
                parse_cpu_list(bad)

    def test_gate_limits_and_is_fifo(self):
        import threading
        import time as _t
        from backend.runtime import InferenceGate
        g = InferenceGate(1)
        running, peak, order = [0], [0], []
        lock = threading.Lock()

        def work(i):
            with g.slot():
                with lock:
                    running[0] += 1
                    peak[0] = max(peak[0], running[0])
                    order.append(i)
                _t.sleep(0.03)
                with lock:
                    running[0] -= 1
        ts = []
        for i in range(5):
            t = threading.Thread(target=work, args=(i,))
            t.start()
            ts.append(t)
            _t.sleep(0.005)          # arrive in order 0..4
        for t in ts:
            t.join()
        self.assertEqual(peak[0], 1)
        self.assertEqual(order, [0, 1, 2, 3, 4])
        self.assertGreaterEqual(g.stats()["callsThatWaited"], 3)
        g.configure(0)                # unlimited = no-op
        with g.slot() as s:
            self.assertEqual(s.wait_ms, 0.0)

    def test_settings_survive_ultralytics_model_load(self):
        """Ultralytics sets torch threads to min(8, CPUs-1) on every model load - AIV_TORCH_THREADS must win."""
        import subprocess
        code = ("import sys,os; sys.path.insert(0, %r); os.chdir(%r)\n"
                "from backend import config, runtime; runtime.apply()\n"
                "import torch, cv2\nfrom ultralytics import YOLO\n"
                "m = YOLO(%r); m.predict(cv2.imread(os.path.join(os.path.dirname(__import__('ultralytics').__file__), 'assets', 'bus.jpg')), verbose=False)\n"
                "print('RESULT', torch.get_num_threads(), torch.get_num_interop_threads(), runtime.GATE.limit, cv2.getNumThreads())"
                % (str(ROOT.parent), str(ROOT), str(ROOT / "models" / "yolov8n.pt")))
        env = dict(os.environ, AIV_TORCH_THREADS="3", AIV_TORCH_INTEROP_THREADS="1", AIV_INFER_CONCURRENCY="2",
                   AIV_CV_THREADS="2", AIV_ENGINE="pytorch")
        env.pop("OMP_NUM_THREADS", None)
        r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=300)
        line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT")), None)
        self.assertIsNotNone(line, r.stderr[-800:])
        self.assertEqual(line.split()[1:], ["3", "1", "2", "2"])

    def test_shared_person_pass_is_identical(self):
        import cv2
        import ultralytics
        from backend.detection.pipeline import PersonVehiclePipeline
        from backend.detection.human_count import HumanCounter
        img = cv2.imread(os.path.join(os.path.dirname(ultralytics.__file__), "assets", "bus.jpg"))
        for mods in (("human",), ("human", "vehicle"), ("vehicle",)):
            outs = []
            for shared in (False, True):
                p = PersonVehiclePipeline(ROOT / "models" / "yolov8n.pt", ROOT / "models" / "bytetrack_stable.yaml")
                c = HumanCounter(ROOT / "models" / "yolov8n.pt")
                c.configure(module_config({}, "count"), [])
                plan = p.build_plan(module_config({}, "human") if "human" in mods else None,
                                    module_config({}, "vehicle") if "vehicle" in mods else None, [])
                res = []
                for k in range(4):
                    _, dets, new, _ = p.process(img.copy(), plan, share_person_conf=c.detect_conf if shared else None)
                    _, _, counted = c.process(img, img.copy(), 100.0 + k, dets=p.shared_persons if shared else None)
                    res.append(([(d["trackId"], d["label"], d["confidence"], d["box"]) for d in dets],
                                [n["label"] for n in new], c.current, c.unique, len(counted)))
                    if shared:
                        own = c.detector.detect(img, c.detect_conf)
                        self.assertEqual(own.shape, p.shared_persons.shape)
                        self.assertLess(float(abs(own - p.shared_persons).max()) if len(own) else 0.0, 1e-3)
                outs.append(res)
            self.assertEqual(outs[0], outs[1], f"modules {mods}")

    def _tool(self, tmp):
        import importlib.util
        spec = importlib.util.spec_from_file_location("pyrotech_perf", ROOT.parent / "scripts" / "perf" / "pyrotech_perf.py")
        t = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(t)
        t.ENV_FILE = os.path.join(tmp, ".env")
        t.BACKEND_ENV_FILE = os.path.join(tmp, "backend.env")
        t.PERF_DIR = os.path.join(tmp, "perf")
        t.REPO = tmp
        return t

    def test_tool_env_backup_and_rollback(self):
        tmp = tempfile.mkdtemp(prefix="aiv-perf-")
        t = self._tool(tmp)
        original = "PORT=5173\nAIV_TORCH_THREADS=8\n# comment\nAIV_LOG_LEVEL=INFO\n"
        with open(t.ENV_FILE, "w") as f:
            f.write(original)
        b = t.write_env({"AIV_TORCH_THREADS": "4", "AIV_INFER_CONCURRENCY": "2"}, "test")
        env = t.read_env(t.ENV_FILE)
        self.assertEqual((env["AIV_TORCH_THREADS"], env["AIV_INFER_CONCURRENCY"], env["PORT"]), ("4", "2", "5173"))
        self.assertEqual(Path(b).read_text(), original)                 # backup = the file before the change
        self.assertEqual(Path(t.ENV_FILE).read_text().count("AIV_TORCH_THREADS="), 1)   # replaced in place, not duplicated
        with self.assertRaises(ValueError):
            t.write_env({"AIV_PORT": "1"}, "not a CPU key")
        t.restore_env(b)
        self.assertEqual(Path(t.ENV_FILE).read_text(), original)
        os.remove(t.ENV_FILE)                                      # .env that did not exist: rollback removes it again
        b2 = t.write_env({"AIV_TORCH_THREADS": "2"}, "test")
        self.assertTrue(os.path.exists(t.ENV_FILE))
        t.restore_env(b2)
        self.assertFalse(os.path.exists(t.ENV_FILE))

    def test_tool_candidates_never_oversubscribe(self):
        t = self._tool(tempfile.mkdtemp(prefix="aiv-perf-"))
        cur = {k: "" for k in t.KEYS}
        for ncpu, streams in ((8, 3), (8, 1), (4, 2), (2, 3)):
            cs = t.candidates({"cpus": ncpu, "cpusUsable": ncpu}, streams, cur)
            self.assertEqual(cs[0]["name"], "current")
            self.assertGreater(len(cs), 1)
            for x in cs[1:]:
                th, g = int(x["AIV_TORCH_THREADS"]), int(x["AIV_INFER_CONCURRENCY"] or 0)
                if th > 1:
                    self.assertLessEqual(th * (g or streams), ncpu, x)
            if streams >= ncpu // 2:
                self.assertTrue(any(x.get("AIV_TORCH_THREADS") == "1" and not x.get("AIV_INFER_CONCURRENCY") for x in cs),
                                "one thread per call, all cameras at once, is measured when there are many cameras")

    def test_tool_accuracy_compare_and_judge(self):
        t = self._tool(tempfile.mkdtemp(prefix="aiv-perf-"))
        ref = [[[10, 10, 50, 90, 0.80, 0], [100, 10, 150, 90, 0.36, 0]]]
        same = [[[10.01, 10, 50, 90, 0.8001, 0], [100, 10, 150, 90, 0.3599, 0]]]
        self.assertTrue(t.compare_detections(ref, same, 0.35)["ok"])
        missing = [[[100, 10, 150, 90, 0.36, 0]]]
        r = t.compare_detections(ref, missing, 0.35)
        self.assertFalse(r["ok"])
        self.assertEqual(r["mismatches"], 1)
        border = [[[10, 10, 50, 90, 0.80, 0]]]                     # the 0.36 box (threshold 0.35) flipped: borderline
        self.assertTrue(t.compare_detections(ref, border, 0.35)["ok"])
        cam = lambda fps, rec=25.0: {"processedFps": fps, "receivedFps": rec, "state": "processing", "modules": ["human"]}
        before = {"cameras": {"C1": cam(1.9)}, "processedFpsTotal": 1.9, "receivedFpsTotal": 25.0, "processRssMb": 3000, "processCpu": 400}
        better = {"cameras": {"C1": cam(3.0)}, "processedFpsTotal": 3.0, "receivedFpsTotal": 25.0, "processRssMb": 3100, "processCpu": 600}
        same_ = {"cameras": {"C1": cam(1.95)}, "processedFpsTotal": 1.95, "receivedFpsTotal": 25.0, "processRssMb": 3000, "processCpu": 400}
        lost = {"cameras": {}, "processedFpsTotal": 0, "receivedFpsTotal": 0, "processRssMb": 3000, "processCpu": 100}
        other_src = {"cameras": {"C1": cam(3.0, 12.0)}, "processedFpsTotal": 3.0, "receivedFpsTotal": 12.0, "processRssMb": 3000}
        self.assertTrue(t.judge(before, better, 0.05)[0])
        self.assertFalse(t.judge(before, same_, 0.05)[0])
        self.assertFalse(t.judge(before, lost, 0.05)[0])
        self.assertFalse(t.judge(before, other_src, 0.05)[0])
