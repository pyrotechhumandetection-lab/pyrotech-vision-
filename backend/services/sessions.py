"""One shared detection session per camera (all five modules: Human, Vehicle, Human Count,
Fire & Smoke, Object Monitoring).

* ONE RTSP connection (LatestFrameReader) per camera, however many pages watch it.
* ONE pipeline (YOLO + ByteTrack) per camera; Human and Vehicle modules are served by the
  same inference pass, exactly like the original script (it detected both in one pass).
* Human Count, Fire & Smoke and Object Monitoring are separate engines on the SAME session: they get the
  same decoded frames (no second RTSP connection) and draw on the same annotated MJPEG stream.
  Fire & Smoke and Object Monitoring Layer 2 run in their own worker threads (as in their original
  programs), so a slow model never stalls the camera loop.
* Live View / dashboard viewers attach to the session's MJPEG output; they never start
  their own inference loop.
* Starting the same camera twice is serialised by a lock; stopping a module only tears the
  session down when no other module or viewer still needs it.
"""
import logging
import threading
import time

import cv2

from backend import config, runtime
from backend.detection.frame_reader import LatestFrameReader
from backend.detection.motion import MotionGate
from backend.detection.modules import MODULES, module_config
from backend.detection.rtsp_probe import rtsp_describe
from backend.services.counts import CountStore
from backend.services.scheduler import ModuleSchedule
from backend.services.util import now_iso, redact

log = logging.getLogger("aivision.sessions")


class SessionError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code, self.message, self.status = code, redact(message), status


class CameraSession:
    def __init__(self, manager, camera):
        self.mgr = manager
        self.camera_id = camera["id"]
        self.lock = threading.RLock()
        self.cond = threading.Condition()
        self.modules = {}            # module_id -> {"paused": bool, "startedAt": iso}
        self.viewers = 0
        self.state = "connecting"
        self.error = None
        self.reader = None           # stream the detection loop analyses (AI stream)
        self.view_reader = None      # main stream, only while needed (HD snapshot originals / HD viewing)
        self.view_opening = False
        self.view_retry_at = 0.0
        self.main_viewers = 0        # browsers watching the raw main stream (stream.mjpeg?source=main)
        self.main_source = None
        self.snapshot_source = "main"
        self.ai_stream = {"requested": "same", "active": "same", "reason": None}
        self.pipeline = None         # existing Person/Vehicle pipeline (human, vehicle modules)
        self.counter = None          # Human Count engine (count module) - its own detector + tracker
        self.count_cfg = None
        self.fire = None             # Fire & Smoke engine (fire module)
        self.fire_cfg = None
        self.objmon = None           # Object Monitoring engine (object module)
        self.obj_cfg = None
        self.thread = None
        self.stopping = False
        self.idle_since = None
        self.jpeg = None
        self.jpeg_seq = 0
        # live view: the AI loop publishes its newest annotated frame, the encoder thread turns it into JPEG
        self.enc_cond = threading.Condition()
        self.enc_frame = None
        self.enc_seq = 0
        self.enc_thread = None
        self.enc_stats = {"encoded": 0, "skipped": 0, "ms": 0.0}    # cumulative; rates per second in _update_rates
        self.latest_annotated = None
        self.latest_raw = None
        self.plan = None
        self.cfg_dirty = True
        # AI frame scheduling (services/scheduler.py): one schedule per running module
        self.sched = {}
        self._sched0 = {}
        self.motion = MotionGate()
        self.gate_on = False                # a motion-gated module is running
        self.motion_min_area = 0.5
        self.capture_cap = 0.0              # frames per second processed by this session (0 = every frame)
        self.pv_sampled = {}                # human / vehicle modules confirmed per sample (pipeline.process)
        self.pv_imgsz = None
        self.held_pv = None                 # (detections, time) of the last Person/Vehicle analysis
        self.held_count_at = None
        # Measured, never estimated:
        #   cameraFps      frames actually received + decoded from the camera per second (reader thread)
        #   processingFps  frames analysed by the AI model per second
        #   framesReceived / framesProcessed / framesDropped (received but not analysed - the AI was busy)
        #   inferenceMs    model + tracker time per analysed frame; loopMs = whole per-frame cost
        #   frameAgeMs     age of the frame when analysis starts (buffer latency)
        #   queue mode (AIV_FRAME_POLICY=queue): framesDropped = frames discarded because the per-camera
        #   queue was full (application-side), cameraGaps = frames missing in the stream's own timestamps
        #   (camera / network side), latencyMs = time from receiving a frame to the end of its analysis
        self.metrics = {"cameraFps": None, "streamFps": None, "processingFps": None, "inferenceMs": None, "loopMs": None,
                        "frameAgeMs": None, "framesReceived": 0, "framesProcessed": 0, "framesDropped": 0,
                        "disconnections": 0, "lastFrameAt": None, "engine": None, "resolution": None,
                        "receivedFps": None, "processedFps": None, "inferenceMsRecent": None, "latencyMs": None,
                        "latencyMsMax": None, "framePolicy": config.FRAME_POLICY, "queueLen": 0, "queueCapacity": None,
                        "queuePeak": 0, "framesDroppedOverload": 0, "cameraGaps": 0, "aiStream": None,
                        "aiResolution": None, "viewResolution": None, "fireAnalysedFps": None, "fireSkipped": None,
                        "stageMs": None, "sharedPersonPass": None, "displayFps": None, "displayEncodeMs": None,
                        "displaySkipped": 0, "device": None, "precision": None, "imgSize": None,
                        "lastDisconnectReason": None, "framesSkippedCapture": 0, "captureFpsTarget": None,
                        "framesRecorded": None, "schedule": {}}
        self.current = {"human": {}, "vehicle": {}}             # label -> count visible in the latest frame
        self.last_detection_at = {"human": None, "vehicle": None, "count": None, "fire": None, "object": None}
        self._disconnect_alerted = False

    # ------------------------------------------------------------------ lifecycle
    def _open_reader(self, source, timeout, report=True, tag=None):
        """Open one stream and wait for its first frame. report=False: failures are not shown as a camera error
        (used for the optional AI substream, which falls back to the main stream)."""
        def fail(code, msg):
            if report:
                self.state, self.error = "error", {"code": code, "message": redact(msg)}
                self.mgr.registry.update_status(self.camera_id, status="error", lastError=redact(msg))
            raise SessionError(code, msg, 502)
        if isinstance(source, str) and source.lower().startswith("rtsp://"):
            # fast, specific failure (auth / wrong path / unreachable) before OpenCV starts retrying
            c, m, _ = rtsp_describe(source, min(timeout, 5))
            if c:
                fail("AUTH_FAILED" if c == "AUTH_FAILED" else "CAMERA_UNAVAILABLE", m)
        reader = LatestFrameReader(source, open_timeout_sec=timeout, tag=tag or f"rd:{self.camera_id}")
        if not reader.wait_first_frame(timeout):
            reader.release()
            code, msg = "CAMERA_UNAVAILABLE", "No frame could be read from the camera within %.0f s" % timeout
            if isinstance(source, str) and source.lower().startswith("rtsp"):
                c, m, _ = rtsp_describe(source, min(timeout, 5))
                if c:
                    code, msg = ("AUTH_FAILED" if c == "AUTH_FAILED" else "CAMERA_UNAVAILABLE"), m
            fail(code, msg)
        return reader

    def open(self, timeout):
        cam = self.mgr.registry.get(self.camera_id)
        try:
            source = self.mgr.registry.resolve_source(cam)
            ai_source, kind = self.mgr.registry.resolve_ai_source(cam)
        except ValueError as e:
            raise SessionError("INVALID_CONFIG", str(e))
        self.main_source = source
        self.snapshot_source = cam.get("snapshotSource") or "main"
        self.ai_stream = {"requested": cam.get("aiStream") or "same", "active": "same", "reason": None}
        if kind != "same":
            try:
                self.reader = self._open_reader(ai_source, timeout, report=False)
                self.ai_stream["active"] = kind
            except SessionError as e:
                log.warning("camera %s: AI stream (%s) not available (%s) - detection uses the main stream",
                            self.camera_id, kind, e.message)
                self.ai_stream.update(active="main", reason=e.message)
        if self.reader is None:
            self.reader = self._open_reader(source, timeout)
        self.metrics["aiStream"] = dict(self.ai_stream)
        if config.FRAME_POLICY == "queue":
            r = self.reader
            frame_mb = max(0.05, (r.width or 1920) * (r.height or 1080) * 3 / 1e6)
            fps = r.nominal_fps or 25.0
            cap = max(2, min(int(round(fps * config.FRAME_QUEUE_SECONDS)), int(config.FRAME_QUEUE_MAX_MB / frame_mb)))
            r.enable_queue(cap)
            self.metrics["queueCapacity"] = cap
            log.info("camera %s: frame queue %d frames (%.1f s at %.0f fps, %.0f MB max); AI stream %s %sx%s",
                     self.camera_id, cap, cap / fps, fps, cap * frame_mb, self.ai_stream["active"], r.width, r.height)
        self.state = "connected"
        self.mgr.registry.update_status(self.camera_id, status="online", lastConnectedAt=now_iso(), lastError=None)
        self.thread = threading.Thread(target=self._loop, name=f"session-{self.camera_id}", daemon=True)
        self.thread.start()
        if config.STREAM_ENCODER_THREAD:
            self.enc_thread = threading.Thread(target=self._encode_loop, name=f"enc-{self.camera_id}", daemon=True)
            self.enc_thread.start()

    # ---- main stream next to a separate AI stream (HD snapshot originals, HD viewing)
    def _separate_ai_stream(self):
        return self.ai_stream.get("active") not in ("same", "main")

    def _manage_view_reader(self, now):
        """Open the main stream while it is needed, close it when it is not (it costs decoding CPU)."""
        active = any(not v["paused"] for v in self.modules.values())
        need = self._separate_ai_stream() and not self.stopping and (
            self.main_viewers > 0 or (self.snapshot_source == "main" and active))
        vr = self.view_reader
        if need and vr is None and not self.view_opening and now >= self.view_retry_at:
            self.view_opening = True
            threading.Thread(target=self._open_view_reader, name=f"view-{self.camera_id}", daemon=True).start()
        elif not need and vr is not None:
            self.view_reader = None
            threading.Thread(target=vr.release, daemon=True).start()
        if self.view_reader is not None and self.view_reader.width and not self.metrics.get("viewResolution"):
            self.metrics["viewResolution"] = f"{self.view_reader.width}x{self.view_reader.height}"

    def _open_view_reader(self):
        try:
            r = self._open_reader(self.main_source, config.CONNECT_TIMEOUT_SEC, report=False, tag=f"hd:{self.camera_id}")
            if self.stopping:
                r.release()
            else:
                self.view_reader = r
                log.info("camera %s: main stream open for HD snapshots / viewing (%sx%s)", self.camera_id, r.width, r.height)
        except SessionError as e:
            self.view_retry_at = time.time() + 60
            log.warning("camera %s: main stream not available for HD snapshots (%s) - retrying in 60 s", self.camera_id, e.message)
        finally:
            self.view_opening = False

    def hd_original(self):
        """Newest main-stream frame (copy) for snapshot originals, or None (same stream / not open / stale)."""
        if not self._separate_ai_stream() or self.snapshot_source != "main":
            return None
        vr = self.view_reader
        if vr is None:
            return None
        _ok, _seq, frame, t = vr.read_latest()
        return frame if frame is not None and time.time() - t <= 2.0 else None

    def ensure_pipeline(self):
        if self.pipeline is not None:
            return
        if not config.MODEL_PATH.is_file():
            raise SessionError("MODEL_NOT_FOUND", f"Model file not found: {config.MODEL_PATH.name}", 500)
        if not config.TRACKER_CONFIG.is_file():
            raise SessionError("MODEL_NOT_FOUND", f"Tracker config not found: {config.TRACKER_CONFIG.name}", 500)
        from backend.detection.pipeline import PersonVehiclePipeline
        engine, path = config.resolve_engine()
        try:
            self.pipeline = PersonVehiclePipeline(path, config.TRACKER_CONFIG, config.IMG_SIZE, config.DEVICE, engine=engine)
        except Exception as e:
            if engine == "pytorch":
                raise SessionError("ENGINE_FAILURE", f"Could not load the detection model: {e}", 500)
            log.warning("OpenVINO model could not be loaded (%s) - using PyTorch", e)
            try:
                engine = "pytorch"
                self.pipeline = PersonVehiclePipeline(config.MODEL_PATH, config.TRACKER_CONFIG, config.IMG_SIZE, config.DEVICE, engine=engine)
            except Exception as e2:
                raise SessionError("ENGINE_FAILURE", f"Could not load the detection model: {e2}", 500)
        self.metrics["engine"] = {"openvino": "OpenVINO", "pytorch": "PyTorch"}[engine]
        gpu = engine == "pytorch" and config.DEVICE not in (None, "cpu")
        self.metrics["device"] = f"GPU cuda:{config.DEVICE}" if gpu else "CPU"
        self.metrics["precision"] = "FP16" if self.pipeline.half else "FP32"
        self.metrics["imgSize"] = config.IMG_SIZE
        log.info("camera %s: inference engine %s on %s, %s, imgsz %d (%s)", self.camera_id, self.metrics["engine"],
                 self.metrics["device"], self.metrics["precision"], config.IMG_SIZE, path if engine != "pytorch" else config.MODEL_PATH)

    def ensure_counter(self):
        """Load the Human Count engine (same shipped yolov8n weights, separate model instance)."""
        if self.counter is not None:
            return
        if not config.MODEL_PATH.is_file():
            raise SessionError("MODEL_NOT_FOUND", f"Model file not found: {config.MODEL_PATH.name}", 500)
        from backend.detection.human_count import HumanCounter
        engine, path = config.resolve_engine()
        try:
            self.counter = HumanCounter(path, config.IMG_SIZE, config.DEVICE, engine=engine)
        except Exception as e:
            if engine == "pytorch":
                raise SessionError("ENGINE_FAILURE", f"Could not load the person detection model: {e}", 500)
            log.warning("OpenVINO model could not be loaded for Human Count (%s) - using PyTorch", e)
            try:
                engine = "pytorch"
                self.counter = HumanCounter(config.MODEL_PATH, config.IMG_SIZE, config.DEVICE, engine=engine)
            except Exception as e2:
                raise SessionError("ENGINE_FAILURE", f"Could not load the person detection model: {e2}", 500)
        if not self.metrics.get("engine"):
            self.metrics["engine"] = {"openvino": "OpenVINO", "pytorch": "PyTorch"}[engine]
        log.info("camera %s: Human Count engine ready (%s)", self.camera_id, engine)

    def ensure_fire(self):
        """Load the Fire & Smoke engine (own model in backend/models/fire_smoke, own worker thread)."""
        if self.fire is not None:
            return
        from backend.detection import fire_smoke
        if not fire_smoke.WEIGHTS.is_file():
            raise SessionError("MODEL_NOT_FOUND", "Fire & smoke model file not found: backend/models/fire_smoke/firedetect-11s.pt", 500)
        try:
            self.fire = fire_smoke.FireEngine(self._fire_alert, thread_name=f"fire:{self.camera_id}")
        except Exception as e:
            raise SessionError("ENGINE_FAILURE", f"Could not load the fire & smoke model: {e}", 500)
        log.info("camera %s: Fire & Smoke engine ready (%s)", self.camera_id, fire_smoke.WEIGHTS.name)

    def ensure_object(self):
        """Load the Object Monitoring engine (Layer 1 matcher, Layer 2 YOLOE worker, optional Layer 3)."""
        if self.objmon is not None:
            return
        from backend.detection.object_monitor.engine import ObjectEngine
        cam = self.mgr.registry.get(self.camera_id) or {}
        self.obj_cfg = module_config(cam, "object")
        try:
            self.objmon = ObjectEngine(self.camera_id, self.obj_cfg, self.mgr.marks, self._object_alert, self._object_info)
        except Exception as e:
            raise SessionError("ENGINE_FAILURE", f"Could not start Object Monitoring: {e}", 500)
        log.info("camera %s: Object Monitoring engine ready (Layer 2: %s, Layer 3: %s)", self.camera_id,
                 self.objmon.detector_label or self.objmon.detector_error or "off", self.objmon.verifier_state)

    def close_engine(self, module_id):
        if module_id == "fire" and self.fire is not None:
            self.fire.close()
            self.fire = None
        if module_id == "object" and self.objmon is not None:
            self.objmon._save_marks()
            self.objmon.close()
            self.objmon = None

    # ---- Fire & Smoke / Object Monitoring results -> event store (Alerts, Snapshots, Detections)
    def _fire_alert(self, label, conf, reason, snapshot, captured_at, dets):
        cam = self.mgr.registry.get(self.camera_id)
        cfg = self.fire_cfg or {}
        if not cam or "fire" not in self.modules or self.modules["fire"]["paused"]:
            return
        name = {"fire": "Fire", "smoke": "Smoke"}[label]
        if not (cfg.get("classes") or {}).get(label, True):
            return
        self.last_detection_at["fire"] = now_iso()
        acfg = cfg.get("alert") or {}
        sev = acfg.get("smokeSeverity", "high") if label == "smoke" else acfg.get("severity", "critical")
        boxes = [{"label": {"fire": "Fire", "smoke": "Smoke"}[d.label], "confidence": round(d.conf, 3), "box": list(d.box), "weak": d.weak}
                 for d in dets if d.label == label]
        w = self.fire.window if self.fire is not None else None
        desc = (f"{name} seen in {w.min_hits[label]} of the last {w.window} analysed frames" if w else f"{name} confirmed") + \
               (f" ({reason})" if reason else "") + f", confidence {conf:.2f}."
        self.mgr.events.on_module_alert(cam, "fire", label, name, snapshot, captured_at=captured_at, confidence=round(conf, 3),
                                        severity=sev, description=desc, alert=bool(acfg.get("enabled", True)),
                                        extra={"boxes": boxes, "reason": reason or None}, original=self.hd_original())

    def _object_alert(self, view, kind, distance, image):
        cam = self.mgr.registry.get(self.camera_id)
        cfg = self.obj_cfg or {}
        if not cam or "object" not in self.modules or self.modules["object"]["paused"]:
            return
        self.last_detection_at["object"] = now_iso()
        acfg = cfg.get("alert") or {}
        if kind == "moved":
            detail = f"moved {distance:.0f} px ({distance / max(1.0, (view['home'][2] ** 2 + view['home'][3] ** 2) ** 0.5):.1f}x its size)"
        else:
            detail = f"not found for {view.get('since_s', 0):.1f} s"
        self.mgr.events.on_module_alert(cam, "object", f"object_{kind}", f"#{view['id']} {view['name']}", image,
                                        severity=acfg.get("severity", "high"), alert=bool(acfg.get("enabled", True)),
                                        description=f"{kind.upper()}: #{view['id']} '{view['name']}' {detail}.",
                                        extra={"objectId": view["id"], "objectName": view["name"], "kind": kind,
                                               "distancePx": round(distance, 1)}, original=self.hd_original())

    def _object_info(self, text):
        if text.startswith("RESTORED"):
            cam = self.mgr.registry.get(self.camera_id)
            if cam and "object" in self.modules:
                self.mgr.events.on_module_alert(cam, "object", "object_restored", text.split("|", 1)[-1].strip(), None,
                                                alert=False, snapshot=False, severity="low", description=text)

    def flush_counts(self):
        """Persist the current, unfinished minute of Human Count history (on stop)."""
        if self.counter is not None:
            self.mgr.counts.add_minutes(self.camera_id, self.counter.flush())

    def close(self):
        self.stopping = True
        with self.enc_cond:
            self.enc_cond.notify_all()
        if self.thread:
            self.thread.join(timeout=5)
        if self.enc_thread:
            self.enc_thread.join(timeout=2)
        for m in ("fire", "object"):
            self.close_engine(m)
        if self.reader:
            self.reader.release()
        if self.view_reader:
            self.view_reader.release()
            self.view_reader = None
        self.state = "stopped"
        with self.cond:
            self.cond.notify_all()
        log.info("session %s closed", self.camera_id)

    def in_use(self):
        return bool(self.modules) or self.viewers > 0 or self.main_viewers > 0

    # ------------------------------------------------------------------ main loop
    def _loop(self):
        # OS thread name; the PyTorch / OpenMP threads this loop starts inherit it, so the perf monitor
        # shows the CPU of this camera's AI as "ai:<camera id>"
        runtime.name_thread(f"ai:{self.camera_id}")
        last_seq = -1
        win = self._new_window()
        win_read0 = self.reader.frames_read
        last_enc = 0.0
        next_accept = 0.0
        last_seen = time.time()
        queue_mode = self.reader.queue is not None
        while not self.stopping:
            if queue_mode:
                # every decoded frame, oldest first (FIFO); the shared frame is never drawn on
                item = self.reader.next_frame(0.2, config.FRAME_MAX_LAG_MS / 1000.0 or None)
                now = time.time()
                if now - win["t0"] >= 1.0:
                    self._update_rates(now - win["t0"], win_read0, win)
                    win, win_read0 = self._new_window(now), self.reader.frames_read
                    self._manage_view_reader(now)
                if item is None:
                    if now - last_seen > max(5.0, config.CONNECT_TIMEOUT_SEC) and self.state != "disconnected":
                        self._on_disconnect()
                    continue
                seq, raw, captured = item
                frame = None                        # copied below only when the frame is used
            else:
                ok, seq, frame, captured = self.reader.read_latest()
                now = time.time()
                if now - win["t0"] >= 1.0:
                    self._update_rates(now - win["t0"], win_read0, win)
                    win, win_read0 = self._new_window(now), self.reader.frames_read
                    self._manage_view_reader(now)
                if frame is None or seq == last_seq:
                    if now - last_seen > max(5.0, config.CONNECT_TIMEOUT_SEC) and self.state != "disconnected":
                        self._on_disconnect()
                    time.sleep(0.003)
                    continue
                raw = None
            active = {m: v for m, v in self.modules.items() if not v["paused"]}
            pv_on = any(m in ("human", "vehicle") for m in active) and self.pipeline is not None   # Person/Vehicle pipeline
            count_on = "count" in active and self.counter is not None                 # Human Count engine
            fire_eng, obj_eng = self.fire, self.objmon     # local refs: a Stop may free an engine meanwhile
            fire_on = "fire" in active and fire_eng is not None                       # Fire & Smoke engine
            obj_on = "object" in active and obj_eng is not None                       # Object Monitoring engine
            any_on = pv_on or count_on or fire_on or obj_on
            if not queue_mode and (pv_on or count_on or obj_on) and seq - last_seq > 1 and last_seq >= 0:
                self.metrics["framesDropped"] += seq - last_seq - 1     # decoded, but the AI was still busy
            last_seq, last_seen = seq, now
            if self.state == "disconnected":
                self._on_reconnect()
            self.metrics["framesReceived"] = self.reader.frames_read
            self.metrics["lastFrameAt"] = now_iso()
            if self.reader.width and not self.metrics["resolution"]:
                self.metrics["resolution"] = f"{self.reader.width}x{self.reader.height}"
            if any_on and self.cfg_dirty:
                self._rebuild_plan(active)
            # capture-rate cap of the running modules (e.g. 16 fps vehicle, 6 fps human): frames above it are
            # skipped right after decoding - no copy, no AI, no live-view encoding (counted, never silent)
            cap = self.capture_cap if any_on else 0.0
            if cap:
                # frame-rate decimation with a running schedule: 20 fps in, 16 fps target -> 16 of 20 frames
                # (a plain "time since last frame" test would keep only every 2nd frame = 10 fps)
                if now < next_accept:
                    self.metrics["framesSkippedCapture"] += 1
                    continue
                next_accept = max(next_accept + 1.0 / cap, now - 1.0 / cap)
            if frame is None:                       # queue mode: copy only frames that are used
                frame = raw.copy() if any_on else raw
            self.latest_raw = raw if raw is not None else frame.copy()     # clean frame (never drawn on)

            annotated = frame
            if any_on:
                t_loop = time.perf_counter()
                runtime.take_wait_ms()                       # reset this thread's slot-wait counter
                # ---- which modules analyse this frame (scheduler: interval / motion gate / every frame)
                if self.gate_on:
                    self.motion.update(self.latest_raw, now, self.motion_min_area)
                due = {}
                for m in ("human", "vehicle", "count", "object"):
                    sc = self.sched.get(m)
                    if m in active and sc is not None:
                        run, why = sc.decide(now, lambda t, sc=sc: self.motion.active(t, sc.hold))
                        if run:
                            due[m] = why
                if due and queue_mode:                       # analyse the NEWEST frame, not the oldest waiting
                    newest, n_old = self.reader.take_newest()
                    if newest is not None:
                        seq, raw, captured = newest
                        last_seq = seq
                        frame = annotated = raw.copy()
                        self.latest_raw = raw
                        for m in due:
                            self.sched[m].replaced(n_old)
                if due and captured and now - captured > config.STALE_FRAME_SEC:
                    for m in due:                            # too old to report as current: wait for the next frame
                        self.sched[m].stale()
                    due = {}
                for m, why in due.items():
                    self.sched[m].selected(now, seq, captured, why)
                pv_due = pv_on and ("human" in due or "vehicle" in due)
                count_due = count_on and "count" in due
                obj_due = obj_on and "object" in due
                ran = pv_due or count_due or obj_due
                inf_ms, new = 0.0, []
                shared = pv_due and count_due and config.SHARED_PERSON_INFERENCE
                if pv_due:
                    try:
                        annotated, dets, new, inf_ms = self.pipeline.process(
                            frame, self.plan, share_person_conf=self.counter.detect_conf if shared else None,
                            sampled=self.pv_sampled or None, imgsz=self.pv_imgsz)
                    except Exception as e:     # engine failure: stop processing, keep the stream, tell the UI
                        log.exception("pipeline failure on %s", self.camera_id)
                        for m in ("human", "vehicle"):
                            if m in due:
                                self.sched[m].done(False)
                        self._engine_failure(e)
                        continue
                    for m in ("human", "vehicle"):
                        if m in due:
                            self.sched[m].done(True, inf_ms)
                    self.held_pv = (dets, now)
                    st = self.pipeline.last_stages
                    for k in ("preprocess", "inference", "postprocess", "tracking"):
                        win["st_" + k] += st.get(k, 0.0)
                    win["st_detect_n"] += 1 if st else 0
                    win["st_draw"] += max(0.0, inf_ms - sum(st.get(k, 0.0) for k in ("preprocess", "inference", "postprocess", "tracking")))
                    counts = {"human": {}, "vehicle": {}}
                    for d in dets:
                        if d["inZone"] and d["module"] in active:
                            counts[d["module"]][d["label"]] = counts[d["module"]].get(d["label"], 0) + 1
                    self.current = counts
                elif pv_on and self.held_pv is not None and self.pv_sampled:
                    # between two analyses: show the last result, labelled with its age
                    annotated = self.pipeline.draw_held(frame, self.plan, self.held_pv[0], now - self.held_pv[1])
                elif not pv_on:
                    self.current = {"human": {}, "vehicle": {}}
                if count_on and count_due:
                    try:   # inference on the clean frame, boxes drawn on the shared annotated frame
                        pre_wait = runtime.take_wait_ms()
                        persons = self.pipeline.shared_persons if shared else None
                        c_t = captured or now
                        if self.sched["count"].sampled:
                            annotated, c_ms, counted = self.counter.occupancy(self.latest_raw, annotated, c_t, dets=persons)
                        else:
                            annotated, c_ms, counted = self.counter.process(self.latest_raw, annotated, c_t, dets=persons)
                        c_wait = runtime.take_wait_ms()
                        c_ms -= c_wait
                        win["gate"] += pre_wait + c_wait
                        win["st_count"] += c_ms
                        win["shared"] += 1 if persons is not None else 0
                        self.sched["count"].done(True, c_ms)
                        self.held_count_at = now
                    except Exception as e:
                        log.exception("Human Count failure on %s", self.camera_id)
                        self.sched["count"].done(False)
                        self._engine_failure(e)
                        continue
                    inf_ms += c_ms
                    self._after_count(counted, annotated, captured)
                elif count_on and self.counter.occupancy_mode:
                    self.counter.draw_occupancy(annotated, now - (self.held_count_at or now))
                if obj_on:
                    try:   # Layer 1 when due (motion gate / every frame), Layer 2 in its own thread
                        t_o = time.perf_counter()
                        if obj_due:
                            annotated = obj_eng.process(self.latest_raw, annotated)
                            self.sched["object"].done(True)
                        else:
                            annotated = obj_eng.skip(self.latest_raw, annotated)
                        o_ms = (time.perf_counter() - t_o) * 1000.0
                        inf_ms += o_ms if obj_due else 0.0
                        win["st_object"] += o_ms
                    except Exception as e:
                        log.exception("Object Monitoring failure on %s", self.camera_id)
                        if obj_due:
                            self.sched["object"].done(False)
                        self._engine_failure(e)
                        continue
                if fire_on:      # hand the newest frame to the fire worker (skipped while it is busy), draw its boxes
                    t_f = time.perf_counter()
                    fire_eng.submit(self.latest_raw, captured or now)
                    annotated = fire_eng.draw(annotated)
                    win["st_fire"] += (time.perf_counter() - t_f) * 1000.0
                    ran = True
                win["gate"] += runtime.take_wait_ms()
                if ran:
                    win["proc"] += 1
                    win["inf"] += inf_ms
                    win["inf_last"] = inf_ms
                    win["age"] += (now - captured) * 1000.0 if captured else 0.0
                    self.metrics["framesProcessed"] += 1
                self.state = "processing"
                t_ev = time.perf_counter()
                if new:
                    cam = self.mgr.registry.get(self.camera_id)
                    for item in new:
                        if item["module"] in active and cam:
                            self.last_detection_at[item["module"]] = now_iso()
                            item["frameId"] = seq                 # the exact analysed frame (camera session sequence)
                            # the frame that triggered the confirmation, stamped with its capture time;
                            # encoding, disk writes and JSON persistence run on the event writer thread
                            # original = HD main-stream frame when the AI uses a substream, else the clean
                            # (unannotated) copy of this very frame
                            orig = self.hd_original()
                            if orig is None:
                                orig = self.latest_raw
                            self.mgr.events.on_confirmation(cam, item["module"], module_config(cam, item["module"]), item,
                                                            annotated, captured_at=captured, original=orig)
                win["st_events"] += (time.perf_counter() - t_ev) * 1000.0
                if ran:
                    win["loop"] += (time.perf_counter() - t_loop) * 1000.0
                    win["n"] += 1
                    if captured:          # receive -> analysis finished (includes the time spent in the queue)
                        lat = (time.time() - captured) * 1000.0
                        win["lat"] += lat
                        win["lat_max"] = max(win["lat_max"], lat)
            else:
                self.state = "connected"
                self.current = {"human": {}, "vehicle": {}}

            self.latest_annotated = annotated
            if self.enc_thread is not None:
                if self.viewers > 0:          # hand over the newest frame; never waits for the encoder
                    with self.enc_cond:
                        if self.enc_frame is not None:
                            self.enc_stats["skipped"] += 1      # previous frame replaced before it was encoded
                        self.enc_frame = annotated
                        self.enc_seq += 1
                        self.enc_cond.notify()
            elif self.viewers > 0 and now - last_enc >= 1.0 / max(1.0, config.STREAM_MAX_FPS):
                last_enc = now
                t_e = time.perf_counter()
                self._encode(annotated)
                win["st_encode"] += (time.perf_counter() - t_e) * 1000.0

    @staticmethod
    def _new_window(t0=None):
        return {"t0": t0 or time.time(), "proc": 0, "inf": 0.0, "inf_last": None, "loop": 0.0, "age": 0.0, "n": 0,
                "lat": 0.0, "lat_max": 0.0, "gate": 0.0, "shared": 0, "st_detect_n": 0, "st_preprocess": 0.0,
                "st_inference": 0.0, "st_postprocess": 0.0, "st_tracking": 0.0, "st_draw": 0.0, "st_count": 0.0,
                "st_object": 0.0, "st_fire": 0.0, "st_events": 0.0, "st_encode": 0.0}

    def _update_rates(self, el, read0, w):
        m = self.metrics
        active = any(not v["paused"] for v in self.modules.values())
        n = w["n"]
        m["cameraFps"] = m["receivedFps"] = round((self.reader.frames_read - read0) / el, 1)
        m["streamFps"] = self.reader.nominal_fps
        m["processingFps"] = m["processedFps"] = round(w["proc"] / el, 1) if active else None
        m["inferenceMs"] = round(w["inf"] / n, 1) if n else None
        m["inferenceMsRecent"] = round(w["inf_last"], 1) if w["inf_last"] is not None else None
        m["loopMs"] = round(w["loop"] / n, 1) if n else None
        m["frameAgeMs"] = round(w["age"] / n, 1) if n else None
        m["latencyMs"] = round(w["lat"] / n, 1) if n else None
        m["latencyMsMax"] = round(w["lat_max"], 1) if n else None
        r = self.reader
        if r.queue is not None:
            m["queueLen"] = r.queue_len()
            m["queuePeak"] = r.queue_peak
            m["framesDropped"] = r.queue_dropped + r.queue_stale
            m["framesDroppedOverload"] = r.queue_dropped        # queue full
            m["framesStale"] = r.queue_stale                     # older than AIV_FRAME_MAX_LAG_MS
            m["maxLagMs"] = config.FRAME_MAX_LAG_MS or None
        m["cameraGaps"] = r.camera_gaps
        # where the time of one analysed frame goes (ms per analysed frame, averaged over the last second)
        dn = w["st_detect_n"]
        stg = {"readerCpu": round(r.read_cpu_ms, 1) if r.read_cpu_ms is not None else None}
        if n:
            if dn:
                for k in ("preprocess", "inference", "postprocess", "tracking"):
                    stg[k] = round(w["st_" + k] / dn, 1)
                stg["drawAndRules"] = round(w["st_draw"] / dn, 1)
            for k, key in (("count", "humanCount"), ("object", "objectLayer1"), ("fire", "fireHandOver"),
                           ("events", "events")):
                if w["st_" + k]:
                    stg[key] = round(w["st_" + k] / n, 1)
            stg["slotWait"] = round(w["gate"] / n, 1)
            stg["liveEncode"] = round(w["st_encode"] / max(1, n), 1) if w["st_encode"] else 0.0
        m["stageMs"] = stg
        sched_out = {}
        for mid, sc in list(self.sched.items()):
            snap, self._sched0[mid] = sc.snapshot(self._sched0.get(mid, {}), el)
            sched_out[mid] = snap
        m["schedule"] = sched_out
        es = self.enc_stats
        e0 = getattr(self, "_enc0", None) or {"encoded": 0, "skipped": 0, "ms": 0.0}
        n_enc = es["encoded"] - e0["encoded"]
        m["displayFps"] = round(n_enc / el, 1) if self.viewers > 0 else None
        m["displayEncodeMs"] = round((es["ms"] - e0["ms"]) / n_enc, 1) if n_enc else None
        m["displaySkipped"] = es["skipped"]
        self._enc0 = dict(es)
        both = self.counter is not None and self.pipeline is not None and "count" in self.modules and \
            any(k in self.modules for k in ("human", "vehicle"))
        m["sharedPersonPass"] = ((w["shared"] > 0) if n else m.get("sharedPersonPass")) if both else None
        if r.width:
            m["aiResolution"] = f"{r.width}x{r.height}"
        fe = self.fire
        if fe is not None:
            runs0 = getattr(self, "_fire_runs0", None)
            m["fireAnalysedFps"] = round((fe.runs - runs0) / el, 1) if runs0 is not None and runs0 <= fe.runs else None
            m["fireSkipped"] = fe.skipped
            self._fire_runs0 = fe.runs
        else:
            m["fireAnalysedFps"] = m["fireSkipped"] = None
            self._fire_runs0 = None

    def _rebuild_plan(self, active):
        cam = self.mgr.registry.get(self.camera_id) or {}
        if self.pipeline is not None:
            human = module_config(cam, "human") if "human" in active else None
            vehicle = module_config(cam, "vehicle") if "vehicle" in active else None
            self.plan = self.pipeline.build_plan(human, vehicle, cam.get("zones"))
        if self.counter is not None:
            self.count_cfg = module_config(cam, "count")
            self.counter.configure(self.count_cfg, cam.get("zones"))
        if self.fire is not None:
            self.fire_cfg = module_config(cam, "fire")
            self.fire.configure(self.fire_cfg, cam.get("zones"))
        if self.objmon is not None:
            self.obj_cfg = module_config(cam, "object")
            self.objmon.configure(self.obj_cfg)
        # ---- AI frame scheduling per module (interval, motion gate, capture cap, model input size)
        caps, gate_area = [], []
        for m in ("human", "vehicle", "count", "object"):
            if m not in active:
                self.sched.pop(m, None)
                continue
            sch = module_config(cam, m).get("schedule") or {}
            sc = self.sched.get(m) or ModuleSchedule(m)
            sc.configure(sch, gate_allowed=m in ("human", "object"))     # never motion-gate vehicle / count
            self.sched[m] = sc
            if sc.motion_gate:
                gate_area.append(float(sch.get("motionMinAreaPct", config.MOTION_MIN_AREA_PCT)))
            caps.append(float(sch.get("captureFps") or 0) if sc.sampled else 0.0)
        if "fire" in active:
            caps.append(0.0)                  # Fire & Smoke keeps every frame (its own everyNFrames setting)
        # the session runs at the HIGHEST rate any running module needs; one module without a cap = no cap
        self.capture_cap = 0.0 if (not caps or 0.0 in caps) else max(caps)
        self.metrics["captureFpsTarget"] = self.capture_cap or None
        self.gate_on = bool(gate_area)
        self.motion_min_area = min(gate_area) if gate_area else config.MOTION_MIN_AREA_PCT
        self.pv_sampled, sizes = {}, []
        for m in ("human", "vehicle"):
            if m in active:
                sch = module_config(cam, m).get("schedule") or {}
                sizes.append(int(sch.get("imgSize") or config.IMG_SIZE))
                if self.sched[m].sampled:
                    self.pv_sampled[m] = {"margin": float(sch.get("confirmMargin", config.SAMPLED_CONFIRM_MARGIN)),
                                          "dedupeSec": float(sch.get("dedupeSec", config.SAMPLED_DEDUPE_SEC))}
        self.pv_imgsz = max(sizes) if sizes else None      # one shared pass: the larger input size of the two
        self.cfg_dirty = False

    def _after_count(self, counted, annotated, captured):
        """Human Count bookkeeping: minute history, last-person time, occupancy alert."""
        if counted:
            self.last_detection_at["count"] = now_iso()
        done = self.counter.take_completed_minutes()
        if done:
            self.mgr.counts.add_minutes(self.camera_id, done)
        alert = (self.count_cfg or {}).get("alert") or {}
        threshold = int(alert.get("threshold") or 0)
        if alert.get("enabled") and threshold > 0 and self.counter.current >= threshold:
            cam = self.mgr.registry.get(self.camera_id)
            if cam:
                self.mgr.events.on_count_alert(cam, alert, self.counter.current, annotated, captured_at=captured)

    def _encode_loop(self):
        """Live-view encoder (own thread): newest annotated frame -> JPEG, at most STREAM_MAX_FPS.
        Frames published faster than that are replaced (counted as displaySkipped), never queued."""
        runtime.name_thread(f"enc:{self.camera_id}")
        period = 1.0 / max(1.0, config.STREAM_MAX_FPS)
        last = 0.0
        while not self.stopping:
            with self.enc_cond:
                while self.enc_frame is None and not self.stopping:
                    self.enc_cond.wait(0.5)
                if self.stopping:
                    break
                wait = last + 0.9 * period - time.time()     # 10% tolerance: 16 fps input is not cut to 14 by timing jitter
                if wait > 0:              # rate limit: wait, then take whatever frame is newest by then
                    self.enc_cond.wait(wait)
                    if self.stopping:
                        break
                frame, self.enc_frame = self.enc_frame, None
            if frame is None:
                continue
            last = time.time()
            t0 = time.perf_counter()
            try:
                self._encode(frame)
            except Exception:
                log.exception("live-view encoding failed on %s", self.camera_id)
                time.sleep(0.5)
                continue
            self.enc_stats["encoded"] += 1
            self.enc_stats["ms"] += (time.perf_counter() - t0) * 1000.0

    def _encode(self, frame):
        h, w = frame.shape[:2]
        if w > config.STREAM_MAX_WIDTH:
            frame = cv2.resize(frame, (config.STREAM_MAX_WIDTH, int(h * config.STREAM_MAX_WIDTH / w)))
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, config.STREAM_JPEG_QUALITY])
        if ok:
            with self.cond:
                self.jpeg = buf.tobytes()
                self.jpeg_seq += 1
                self.cond.notify_all()

    def _on_disconnect(self):
        self.state = "disconnected"
        self.metrics["disconnections"] += 1
        r = self.reader
        reason = f"no frame for >{max(5.0, config.CONNECT_TIMEOUT_SEC):.0f} s" + \
            "".join(f"; {x}" for x in ((r.last_error, r.last_reopen_error) if r else ()) if x)
        self.metrics["lastDisconnectReason"] = reason
        self.mgr.registry.update_status(self.camera_id, status="offline", lastError="Stream disconnected - reconnecting")
        cam = self.mgr.registry.get(self.camera_id)
        if cam and not self._disconnect_alerted:
            self._disconnect_alerted = True
            self.mgr.events.raise_alert(cam, "camera_disconnected", "medium", "No frames received; the reader is reconnecting.",
                                        cooldown_key=(self.camera_id, "disconnected"), cooldown_sec=300)
        log.warning("camera %s disconnected (%s)", self.camera_id, reason)

    def _on_reconnect(self):
        self._disconnect_alerted = False
        self.state = "connected"
        self.mgr.registry.update_status(self.camera_id, status="online", lastConnectedAt=now_iso(), lastError=None)
        log.info("camera %s reconnected", self.camera_id)

    def _engine_failure(self, exc):
        cam = self.mgr.registry.get(self.camera_id)
        self.error = {"code": "ENGINE_FAILURE", "message": redact(exc)}
        with self.lock:
            self.modules.clear()
        self.state = "error"
        if cam:
            self.mgr.events.raise_alert(cam, "engine_failure", "high", f"Detection stopped: {redact(exc)}",
                                        cooldown_key=(self.camera_id, "engine"), cooldown_sec=60)

    # ------------------------------------------------------------------ status
    def module_status(self, module_id):
        m = self.modules.get(module_id)
        if self.state == "error":
            st = "error"
        elif not m:
            st = "stopped"
        elif m["paused"]:
            st = "paused"
        elif self.state == "disconnected":
            st = "disconnected"
        elif self.state == "processing":
            st = "processing"
        else:
            st = "connecting"
        if module_id == "count":
            cs = self.counter.stats() if self.counter else None
            live = bool(m and not m["paused"] and cs)
            return {
                "state": st, "cameraState": self.state, "error": self.error,
                "currentCounts": {"Person": cs["current"]} if live else {},
                "uniqueCounts": {"Person": cs["unique"]} if (m and cs) else {},
                "lastDetectionAt": self.last_detection_at.get("count"),
                "startedAt": m["startedAt"] if m else None,
                "metrics": dict(self.metrics), "viewers": self.viewers,
                "sharedWith": [x for x in self.modules if x != module_id],
                "count": cs if m else None,
            }
        if module_id == "fire":
            fs = self.fire.stats() if self.fire else None
            live = bool(m and not m["paused"] and fs)
            return {
                "state": st, "cameraState": self.state, "error": self.error,
                "currentCounts": dict(self.fire.current) if live else {},
                "uniqueCounts": (fs or {}).get("alerts", {}) if m else {},
                "lastDetectionAt": self.last_detection_at.get("fire"),
                "startedAt": m["startedAt"] if m else None,
                "metrics": dict(self.metrics, inferenceMs=(fs or {}).get("inferenceMs")) if fs else dict(self.metrics),
                "viewers": self.viewers, "sharedWith": [x for x in self.modules if x != module_id],
                "modelFile": "firedetect-11s.pt", "fire": fs if m else None,
            }
        if module_id == "object":
            ov = self.objmon.view() if self.objmon else None
            return {
                "state": st, "cameraState": self.state, "error": self.error,
                "currentCounts": {}, "uniqueCounts": {},
                "lastDetectionAt": self.last_detection_at.get("object"),
                "startedAt": m["startedAt"] if m else None,
                "metrics": dict(self.metrics), "viewers": self.viewers,
                "sharedWith": [x for x in self.modules if x != module_id],
                "modelFile": "yoloe-26n-seg.pt", "object": ov if m else None,
            }
        counts = self.pipeline.unique_counts() if self.pipeline else {}
        labels = MODULES[module_id]["labels"]
        return {
            "state": st, "cameraState": self.state, "error": self.error,
            "currentCounts": self.current.get(module_id, {}) if m and not m["paused"] else {},
            "uniqueCounts": {k: v for k, v in counts.items() if k in labels} if m else {},
            "lastDetectionAt": self.last_detection_at.get(module_id),
            "startedAt": m["startedAt"] if m else None,
            "metrics": dict(self.metrics), "viewers": self.viewers,
            "sharedWith": [x for x in self.modules if x != module_id],
        }


class SessionManager:
    def __init__(self, registry, events):
        self.registry = registry
        self.events = events
        self.counts = CountStore()
        from backend.detection.object_monitor.engine import MarkStore
        self.marks = MarkStore()
        self.sessions = {}
        self.lock = threading.RLock()
        self._janitor = threading.Thread(target=self._reap_idle, name="session-janitor", daemon=True)
        self._janitor.start()

    def _check_camera(self, cam_id):
        cam = self.registry.get(cam_id)
        if not cam:
            raise SessionError("CAMERA_NOT_FOUND", f"Camera {cam_id} does not exist", 404)
        if cam.get("enabled") is False:
            raise SessionError("CAMERA_DISABLED", f"Camera {cam_id} is disabled in Camera Management", 409)
        if cam.get("type") not in ("rtsp", "webcam", "file"):
            raise SessionError("CAMERA_INCOMPATIBLE", "The existing pipeline needs an RTSP URL, webcam or video file (ONVIF is not supported)", 409)
        return cam

    def _get_or_open(self, cam_id):
        s = self.sessions.get(cam_id)
        if s and s.state not in ("stopped", "error"):
            return s
        if s:
            s.close()
        cam = self.registry.get(cam_id)
        s = CameraSession(self, cam)
        s.open(float(cam.get("timeoutSec") or config.CONNECT_TIMEOUT_SEC))
        self.sessions[cam_id] = s
        return s

    def start_module(self, cam_id, module_id):
        with self.lock:   # serialises concurrent Start clicks for the same/different cameras
            self._check_camera(cam_id)
            if not config.MODEL_PATH.is_file():   # fail before touching the camera
                raise SessionError("MODEL_NOT_FOUND", f"Model file not found: {config.MODEL_PATH.name}", 500)
            s = self._get_or_open(cam_id)
            try:
                if module_id == "count":
                    s.ensure_counter()
                elif module_id == "fire":
                    s.ensure_fire()
                elif module_id == "object":
                    s.ensure_object()
                else:
                    s.ensure_pipeline()
            except SessionError:
                if not s.in_use():
                    s.close()
                    self.sessions.pop(cam_id, None)
                raise
            if module_id not in s.modules:
                if module_id == "count":
                    s.counter.reset()          # statistics are "since this start"
                    s.last_detection_at["count"] = None
                if module_id == "fire":
                    s.fire.reset()             # alert window / smoke tracks start from zero
                    s.last_detection_at["fire"] = None
                s.modules[module_id] = {"paused": False, "startedAt": now_iso()}
            s.modules[module_id]["paused"] = False
            s.error = None
            s.cfg_dirty = True
            s.idle_since = None
            return s

    def stop_module(self, cam_id, module_id):
        with self.lock:
            s = self.sessions.get(cam_id)
            if not s or module_id not in s.modules:
                return False
            if module_id == "count":
                s.flush_counts()
            s.modules.pop(module_id, None)
            if module_id in ("fire", "object"):
                s.close_engine(module_id)      # frees the model and its worker thread
            s.cfg_dirty = True
            if not s.in_use():
                s.close()
                self.sessions.pop(cam_id, None)
            elif not s.modules and s.pipeline is not None:
                s.pipeline.reset_tracking()
            return True

    def pause_module(self, cam_id, module_id, paused):
        with self.lock:
            s = self.sessions.get(cam_id)
            if not s or module_id not in s.modules:
                raise SessionError("NOT_RUNNING", f"{MODULES[module_id]['name']} is not running on {cam_id}", 409)
            s.modules[module_id]["paused"] = paused
            s.cfg_dirty = True
            return s

    def config_changed(self, cam_id):
        s = self.sessions.get(cam_id)
        if s:
            s.cfg_dirty = True

    def camera_changed(self, cam_id, source_changed):
        """Camera edited/disabled/deleted in Camera Management."""
        with self.lock:
            s = self.sessions.get(cam_id)
            if not s:
                return
            cam = self.registry.get(cam_id)
            if not cam or cam.get("enabled") is False or source_changed:
                modules = dict(s.modules)
                s.close()
                self.sessions.pop(cam_id, None)
                if cam and cam.get("enabled") is not False and modules:
                    try:   # reconnect with the new source, keep the running modules
                        for m in modules:
                            self.start_module(cam_id, m)
                    except SessionError as e:
                        log.warning("could not restart %s after edit: %s", cam_id, e.message)
            else:
                s.cfg_dirty = True

    def add_viewer(self, cam_id, main=False):
        with self.lock:
            self._check_camera(cam_id)
            s = self._get_or_open(cam_id)
            if main:
                s.main_viewers += 1          # raw main stream: no annotated JPEG encoding needed
            else:
                s.viewers += 1
            s.idle_since = None
            return s

    def remove_viewer(self, cam_id, main=False):
        with self.lock:
            s = self.sessions.get(cam_id)
            if not s:
                return
            if main:
                s.main_viewers = max(0, s.main_viewers - 1)
            else:
                s.viewers = max(0, s.viewers - 1)
            if not s.in_use():
                s.idle_since = time.time()

    def _reap_idle(self):
        while True:
            time.sleep(1.0)
            with self.lock:
                for cam_id, s in list(self.sessions.items()):
                    if s.idle_since and not s.in_use() and time.time() - s.idle_since > config.PREVIEW_IDLE_RELEASE_SEC:
                        s.close()
                        self.sessions.pop(cam_id, None)

    def get(self, cam_id):
        return self.sessions.get(cam_id)

    def camera_runtime_status(self, cam_id):
        s = self.sessions.get(cam_id)
        if not s:
            return None
        if s.state in ("processing", "connected"):
            return "online"
        if s.state == "disconnected":
            return "offline"
        if s.state == "error":
            return "error"
        return None

    def shutdown(self):
        with self.lock:
            for s in list(self.sessions.values()):
                s.close()
            self.sessions.clear()
