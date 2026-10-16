"""HTTP API (Python standard library only - no new web framework dependency).

All routes live under /api. Responses are JSON; errors are
    {"success": false, "error_code": "...", "message": "..."}  with a 4xx/5xx status.
Live video is MJPEG (multipart/x-mixed-replace) - plays in a plain <img> in every browser.
See README.md (section "Backend API") for the full endpoint list.
"""
import json
import logging
import os
import re
import shutil
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from backend import __version__, config          # first: reads .env and the CPU settings before torch / cv2 load
from backend import runtime

import cv2

BOOT_ID = str(int(time.time()))
from backend.services.autostart import AutoStart
from backend.services.cameras import CameraRegistry
from backend.services.events import VEHICLE_CATEGORY, EventStore
from backend.detection.modules import MODULES, describe, module_config, validate as validate_module, deep_merge, DEFAULTS
from backend.detection.rtsp_probe import grab_frame, test_source
from backend.services.headcount import HeadCountError, HeadCountService
from backend.services.sessions import SessionError, SessionManager
from backend.services.util import camera_host, redact

log = logging.getLogger("aivision.api")
MAX_BODY = 1_000_000


class ApiError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, redact(message)


class App:
    def __init__(self):
        self.registry = CameraRegistry()
        self.events = EventStore()
        self.sessions = SessionManager(self.registry, self.events)
        self.headcount = HeadCountService(self.registry, self.sessions, self.events)
        self.autostart = AutoStart(self.sessions, self.registry)
        self._storage_cache = (0, None)
        self._gpu = None
        try:
            import pynvml  # installed with ultralytics on this machine (nvidia-ml-py)
            pynvml.nvmlInit()
            if pynvml.nvmlDeviceGetCount() > 0:
                self._gpu = pynvml
        except Exception:
            self._gpu = None
        try:
            import psutil
            psutil.cpu_percent(interval=None)   # prime the counter
            self._psutil = psutil
        except Exception:
            self._psutil = None
        self._proc = None
        if self._psutil:
            try:    # prime the per-process CPU counter so the first reading is a real measurement
                self._proc = self._psutil.Process()
                self._proc.cpu_percent(interval=None)
            except Exception:
                self._proc = None

    # ------------------------------------------------------------------ helpers
    def camera_or_404(self, cam_id):
        cam = self.registry.get(cam_id)
        if not cam:
            raise ApiError(404, "CAMERA_NOT_FOUND", f"Camera {cam_id} does not exist")
        return cam

    def public_camera(self, cam):
        pub = self.registry.public(cam)
        rt = self.sessions.camera_runtime_status(cam["id"])
        if rt:
            pub["status"] = rt
        if cam.get("enabled") is False:
            pub["status"] = pub.get("status") or "unverified"
        s = self.sessions.get(cam["id"])
        pub["runningModules"] = sorted(s.modules) if s else []
        return pub

    def module_status(self, module_id, cam_id):
        m = MODULES[module_id]
        model_file, model_ok = config.model_label(), config.MODEL_PATH.is_file()
        tracking = "ByteTrack (track IDs can be re-assigned after occlusion)"
        if module_id == "count":
            tracking = "People tracker of the Human Count module (motion + overlap; a lost person keeps the ID for the configured time)"
        elif module_id == "fire":
            from backend.detection import fire_smoke
            model_file, model_ok = fire_smoke.WEIGHTS.name, fire_smoke.WEIGHTS.is_file()
            tracking = "Alert after M of the last K analysed frames per class; weak smoke needs fire nearby or rising/growing smoke"
        elif module_id == "object":
            from backend.detection.object_monitor import engine as obj_engine
            model_file = DEFAULTS["object"]["detector"]["model"]
            model_ok = (obj_engine.MODELS_DIR / model_file).is_file()
            tracking = "Marked objects: template + ORB/SIFT + colour histogram, CSRT tracker while displaced"
        base = {"success": True, "module": module_id, "cameraId": cam_id, "backend": "connected",
                "modelFile": model_file, "modelAvailable": model_ok,
                "supportedClasses": m["labels"], "tracking": tracking}
        if not cam_id:
            base.update(state="no_camera")
            return base
        cam = self.camera_or_404(cam_id)
        s = self.sessions.get(cam_id)
        base["camera"] = {"id": cam["id"], "name": cam.get("name"), "status": self.public_camera(cam).get("status"), "enabled": cam.get("enabled", True)}
        if not s:
            base.update(state="stopped", cameraState="not_connected", metrics=None, currentCounts={}, uniqueCounts={}, lastDetectionAt=None)
            last = next((e for e in self.events.detections.data if e["cameraId"] == cam_id and e["module"] == module_id), None)
            base["lastDetectionAt"] = last["timestamp"] if last else None
            return base
        base.update(s.module_status(module_id))
        if not base.get("lastDetectionAt"):
            last = next((e for e in self.events.detections.data if e["cameraId"] == cam_id and e["module"] == module_id), None)
            base["lastDetectionAt"] = last["timestamp"] if last else None
        return base

    def _gpu_info(self, h):
        """GPU 0 (NVML): utilisation, memory of the whole GPU and of THIS process, PyTorch allocator."""
        nv = self._gpu
        mem = nv.nvmlDeviceGetMemoryInfo(h)
        util = nv.nvmlDeviceGetUtilizationRates(h)
        info = {"name": None, "utilPercent": float(util.gpu), "memUtilPercent": float(util.memory),
                "memUsedMb": round(mem.used / 1048576), "memTotalMb": round(mem.total / 1048576),
                "processMemMb": None, "torchAllocatedMb": None, "torchReservedMb": None, "temperatureC": None}
        try:
            n = nv.nvmlDeviceGetName(h)
            info["name"] = n.decode() if isinstance(n, bytes) else n
            info["temperatureC"] = nv.nvmlDeviceGetTemperature(h, nv.NVML_TEMPERATURE_GPU)
        except Exception:
            pass
        try:
            me = os.getpid()
            for p in nv.nvmlDeviceGetComputeRunningProcesses(h):
                if p.pid == me and p.usedGpuMemory is not None:
                    info["processMemMb"] = round(p.usedGpuMemory / 1048576)
        except Exception:
            pass
        torch = sys.modules.get("torch")          # only if a model already loaded it - never import here
        try:
            if torch is not None and torch.cuda.is_initialized():
                info["torchAllocatedMb"] = round(torch.cuda.memory_allocated() / 1048576)
                info["torchReservedMb"] = round(torch.cuda.memory_reserved() / 1048576)
        except Exception:
            pass
        return info

    def metrics(self):
        out = {"cpu": None, "ram": None, "gpu": None, "fps": None, "latencyMs": None, "cameraFps": None, "framesDropped": 0,
               "disconnections": 0, "sessions": []}
        if self._psutil:
            out["cpu"] = self._psutil.cpu_percent(interval=None)
            vm = self._psutil.virtual_memory()
            out["ram"] = vm.percent
            out["ramAvailableMb"] = round(vm.available / 1048576)
            out["cpuCores"] = self._psutil.cpu_count(logical=False)
            out["cpuThreads"] = self._psutil.cpu_count(logical=True)
            try:      # this backend process: CPU in % of ONE core (200 = two cores busy), resident memory
                if self._proc is None:
                    self._proc = self._psutil.Process()
                    self._proc.cpu_percent(interval=None)
                out["processCpu"] = round(self._proc.cpu_percent(interval=None), 1)
                out["processRssMb"] = round(self._proc.memory_info().rss / 1048576)
            except Exception:
                pass
        if self._gpu:
            try:
                h = self._gpu.nvmlDeviceGetHandleByIndex(0)
                out["gpu"] = float(self._gpu.nvmlDeviceGetUtilizationRates(h).gpu)
                out["gpuInfo"] = self._gpu_info(h)
            except Exception:
                out["gpu"] = None
        fps, lat, cam_fps = [], [], []
        for cid, s in list(self.sessions.sessions.items()):
            mt = s.metrics
            cam = self.registry.get(cid) or {}
            out["sessions"].append({"cameraId": cid, "cameraIp": camera_host(cam), "state": s.state, "modules": sorted(s.modules),
                                    "viewers": s.viewers, **mt})
            out["framesDropped"] += mt["framesDropped"]
            out["disconnections"] += mt["disconnections"]
            if mt["processingFps"] is not None:
                fps.append(mt["processingFps"])
            if mt["inferenceMs"] is not None:
                lat.append(mt["inferenceMs"])
            if mt["cameraFps"] is not None:
                cam_fps.append(mt["cameraFps"])
        out["runtime"] = {**runtime.snapshot(), "gate": runtime.GATE.stats(), "errors": runtime.STATE["errors"]}
        ws = self.events.writer_stats
        out["eventWriter"] = {"jobs": ws["jobs"], "msAvg": round(ws["msTotal"] / ws["jobs"], 1) if ws["jobs"] else None,
                              "msMax": round(ws["msMax"], 1), "queued": self.events._jobs.qsize(),
                              # detection snapshots: capture of the triggering frame -> files written
                              "snapshotCaptureToSavedMsAvg": round(ws["snapMsTotal"] / ws["snapN"], 1) if ws.get("snapN") else None,
                              "snapshotCaptureToSavedMsMax": round(ws["snapMsMax"], 1) if ws.get("snapN") else None}
        out["fps"] = round(sum(fps), 1) if fps else None
        out["latencyMs"] = round(sum(lat) / len(lat), 1) if lat else None
        out["cameraFps"] = round(sum(cam_fps), 1) if cam_fps else None
        cams = self.registry.list()
        out["camerasTotal"] = len(cams)
        out["camerasOnline"] = sum(1 for c in cams if self.sessions.camera_runtime_status(c["id"]) == "online")
        return out

    def storage(self):
        t, cached = self._storage_cache
        if cached and time.time() - t < 10:
            return cached
        root = config.SNAPSHOT_DIR
        used = 0
        for dp, _dn, fns in os.walk(root):
            for fn in fns:
                try:
                    used += os.path.getsize(os.path.join(dp, fn))
                except OSError:
                    pass
        du = shutil.disk_usage(root)
        res = {"snapshotBytes": used, "snapshotCount": len(self.events.snapshots.data), "usedBytes": du.used,
               "capacityBytes": du.total, "freeBytes": du.free, "snapshotDir": "configured (AIV_SNAPSHOT_DIR)"}
        self._storage_cache = (time.time(), res)
        return res

    def save_manual_snapshot(self, cam, module_id=None):
        s = self.sessions.get(cam["id"])
        frame = None
        if s is not None and s.latest_annotated is not None:
            frame = s.latest_annotated.copy()
        elif module_id:
            raise ApiError(409, "NOT_RUNNING", "Start detection first - snapshots are taken from the running detection stream")
        else:
            try:
                source = self.registry.resolve_source(cam)
            except ValueError as e:
                raise ApiError(400, "INVALID_CONFIG", str(e))
            box = grab_frame(source, float(cam.get("timeoutSec") or config.CONNECT_TIMEOUT_SEC))
            frame = box.get("frame")
        if frame is None:
            raise ApiError(502, "NO_FRAME", "No valid frame is available from the camera")
        try:
            return self.events.save_snapshot(frame, cam, module_id or "manual", "manual", source="manual")
        except OSError as e:
            raise ApiError(500, "SNAPSHOT_WRITE_FAILED", f"Snapshot could not be written: {e}")


# ====================================================================== HTTP layer
ROUTES = []


def route(method, pattern):
    rx = re.compile("^" + config.API_PREFIX + pattern + "$")

    def deco(fn):
        ROUTES.append((method, rx, fn))
        return fn
    return deco


def _cam_id_param(q, body):
    cid = (body or {}).get("cameraId") or (q.get("cameraId") or [None])[0]
    if not cid:
        raise ApiError(400, "CAMERA_REQUIRED", "cameraId is required")
    return cid


def _module(m):
    if m not in MODULES:
        raise ApiError(404, "UNKNOWN_MODULE", f"Unknown module '{m}' (available: {', '.join(MODULES)})")
    return m


@route("GET", r"/system/health")
def health(app, q, body):
    engine, path = config.resolve_engine()
    return {"status": "ok", "version": __version__, "modules": list(MODULES), "modelAvailable": config.MODEL_PATH.is_file(),
            "modelFile": config.MODEL_PATH.name, "engine": {"openvino": "OpenVINO", "pytorch": "PyTorch"}[engine],
            "autostart": app.autostart.status() if getattr(app, "autostart", None) else None}


@route("GET", r"/system/metrics")
def metrics(app, q, body):
    return app.metrics()


@route("GET", r"/system/runtime")
def runtime_info(app, q, body):
    """CPU settings in effect (thread pools, inference concurrency, affinity), as read back from the libraries."""
    engine, _ = config.resolve_engine()
    return {"requested": runtime.STATE["requested"], "applied": runtime.snapshot(), "errors": runtime.STATE["errors"],
            "gate": runtime.GATE.stats(), "engine": {"openvino": "OpenVINO", "pytorch": "PyTorch"}[engine],
            "sharedPersonInference": config.SHARED_PERSON_INFERENCE, "imgSize": config.IMG_SIZE,
            "pid": os.getpid()}


@route("GET", r"/system/storage")
def storage(app, q, body):
    return app.storage()


@route("GET", r"/detection-modules")
def detection_modules(app, q, body):
    out = []
    for d in describe():
        running = any(d["id"] in s.modules and not s.modules[d["id"]]["paused"] for s in app.sessions.sessions.values())
        loaded = any(s.pipeline is not None for s in app.sessions.sessions.values())
        out.append({**d, "loaded": loaded or config.MODEL_PATH.is_file(), "running": running, "modelFile": config.model_label()})
    return out


# ---- cameras
@route("GET", r"/cameras")
def cameras_list(app, q, body):
    return [app.public_camera(c) for c in app.registry.cams.data["cameras"]]


@route("POST", r"/cameras")
def cameras_create(app, q, body):
    errs = app.registry.validate(body or {})
    if errs:
        raise ApiError(422, "INVALID_CONFIG", "; ".join(errs))
    return app.registry.save(body)


@route("GET", r"/cameras/(?P<cid>[^/]+)")
def camera_get(app, q, body, cid):
    return app.public_camera(app.camera_or_404(cid))


@route("PUT", r"/cameras/(?P<cid>[^/]+)")
def camera_put(app, q, body, cid):
    old = app.camera_or_404(cid)
    errs = app.registry.validate(body or {}, existing_id=cid)
    if errs:
        raise ApiError(422, "INVALID_CONFIG", "; ".join(errs))
    pub = app.registry.save(body, existing_id=cid)
    new = app.registry.get(cid)
    changed = any(old.get(k) != new.get(k) for k in ("type", "sourceUrl", "username", "streamProfile", "customStream",
                                                           "aiStream", "aiCustomStream", "snapshotSource")) or bool((body or {}).get("password"))
    app.sessions.camera_changed(cid, changed)
    return app.public_camera(new) if new else pub


@route("DELETE", r"/cameras/(?P<cid>[^/]+)")
def camera_delete(app, q, body, cid):
    app.camera_or_404(cid)
    s = app.sessions.get(cid)
    if s:
        s.modules.clear()
        s.viewers = 0
    app.registry.delete(cid)
    app.autostart.forget(cid)
    app.sessions.camera_changed(cid, True)
    return {"success": True, "cameraId": cid}


@route("GET", r"/cameras/(?P<cid>[^/]+)/status")
def camera_status(app, q, body, cid):
    cam = app.camera_or_404(cid)
    s = app.sessions.get(cid)
    pub = app.public_camera(cam)
    return {"success": True, "cameraId": cid, "status": pub.get("status"), "lastConnectedAt": cam.get("lastConnectedAt"),
            "lastError": cam.get("lastError"), "session": None if not s else {"state": s.state, "modules": sorted(s.modules),
                                                                                "viewers": s.viewers, "metrics": s.metrics}}


@route("POST", r"/cameras/test-connection")
def test_new(app, q, body):
    body = body or {}
    cam = dict(body)
    errs = app.registry.validate({**cam, "id": cam.get("id") or "TEST-ID"}, existing_id=cam.get("id") if app.registry.get(cam.get("id") or "") else None)
    errs = [e for e in errs if "already" not in e and "name" not in e]
    if errs:
        return {"ok": False, "success": False, "code": "INVALID_CONFIG", "message": "; ".join(errs)}
    pw = body.get("password") or None
    try:
        source = app.registry.resolve_source(cam, password_override=pw)
    except ValueError as e:
        return {"ok": False, "success": False, "code": "INVALID_CONFIG", "message": redact(e)}
    return test_source(source, float(cam.get("timeoutSec") or config.CONNECT_TIMEOUT_SEC))


@route("POST", r"/cameras/(?P<cid>[^/]+)/test-connection")
def test_saved(app, q, body, cid):
    cam = app.camera_or_404(cid)
    s = app.sessions.get(cid)
    if s and s.latest_raw is not None and s.state in ("connected", "processing"):
        from backend.detection.rtsp_probe import frame_to_data_url
        h, w = s.latest_raw.shape[:2]
        res = {"ok": True, "success": True, "code": "OK", "message": "Camera is streaming (frame taken from the running session)",
               "frame": frame_to_data_url(s.latest_raw), "width": w, "height": h, "fps": s.metrics.get("cameraFps")}
    else:
        try:
            source = app.registry.resolve_source(cam)
        except ValueError as e:
            return {"ok": False, "success": False, "code": "INVALID_CONFIG", "message": redact(e)}
        res = test_source(source, float(cam.get("timeoutSec") or config.CONNECT_TIMEOUT_SEC))
    from backend.services.util import now_iso
    if res["ok"]:
        app.registry.update_status(cid, status="online", lastConnectedAt=now_iso(), lastError=None)
    elif res["code"] != "INVALID_CONFIG":
        app.registry.update_status(cid, status="error", lastError=res["message"])
    return res


@route("GET", r"/cameras/(?P<cid>[^/]+)/stream")
def camera_stream_info(app, q, body, cid):
    app.camera_or_404(cid)
    return {"transport": "mjpeg", "url": f"cameras/{cid}/stream.mjpeg", "audio": False,
            "note": "Annotated frames from the shared detection session (raw frames when no module is running)."}


@route("POST", r"/cameras/(?P<cid>[^/]+)/snapshot")
def camera_snapshot(app, q, body, cid):
    return app.save_manual_snapshot(app.camera_or_404(cid))


@route("PUT", r"/cameras/(?P<cid>[^/]+)/detection-config")
def camera_detection_config(app, q, body, cid):
    cam = app.camera_or_404(cid)
    body = body or {}
    if "zones" in body:
        cam["zones"] = body["zones"]
    if "detection" in body:
        cam["detection"] = body["detection"]
    for m in MODULES:
        errs = validate_module(m, module_config(cam, m), cam.get("zones"))
        if errs:
            raise ApiError(422, "INVALID_CONFIG", f"{m}: " + "; ".join(errs))
    app.registry.save(cam, existing_id=cid)
    app.sessions.config_changed(cid)
    return {"success": True, "cameraId": cid}


# ---- events
def _limit(q, default=200):
    try:
        return max(1, min(5000, int((q.get("limit") or [default])[0])))
    except ValueError:
        return default


def _filter(records, q):
    cid = (q.get("cameraId") or [None])[0]
    mod = (q.get("module") or [None])[0]
    vcls = (q.get("vehicleClass") or [None])[0]     # car | truck | two_wheeler | bus (from the detection label)
    out = [r for r in records if (not cid or r.get("cameraId") == cid) and (not mod or r.get("module") == mod)
           and (not vcls or vehicle_class(r) == vcls)]
    return out[:_limit(q)]


def vehicle_class(rec):
    """Vehicle category of an event / snapshot record (older records: derived from their label)."""
    if rec.get("module") != "vehicle":
        return None
    return rec.get("vehicleClass") or VEHICLE_CATEGORY.get(rec.get("label"))


@route("GET", r"/detections")
def detections(app, q, body):
    return _filter(app.events.detections.data, q)


@route("GET", r"/alerts")
def alerts(app, q, body):
    return _filter(app.events.alerts.data, q)


@route("GET", r"/alerts/(?P<aid>[^/]+)")
def alert_get(app, q, body, aid):
    a = next((x for x in app.events.alerts.data if x["id"] == aid), None)
    if not a:
        raise ApiError(404, "ALERT_NOT_FOUND", f"Alert {aid} does not exist")
    return a


@route("POST", r"/alerts/(?P<aid>[^/]+)/acknowledge")
def alert_ack(app, q, body, aid):
    body = body or {}
    a = app.events.acknowledge(aid, str(body.get("note") or ""), str(body.get("by") or "operator")[:40])
    if not a:
        raise ApiError(404, "ALERT_NOT_FOUND", f"Alert {aid} does not exist")
    return {"success": True, **a}


@route("GET", r"/snapshots")
def snapshots(app, q, body):
    # every vehicle record carries its category (records saved before it was stored get it from their label)
    return [dict(r, vehicleClass=vehicle_class(r)) if r.get("module") == "vehicle" and not r.get("vehicleClass") else r
            for r in _filter(app.events.snapshots.data, q)]


@route("GET", r"/snapshots/(?P<sid>[^/]+)")
def snapshot_get(app, q, body, sid):
    rec, _p = app.events.snapshot_file(sid)
    if not rec:
        raise ApiError(404, "SNAPSHOT_NOT_FOUND", f"Snapshot {sid} does not exist")
    return rec


@route("DELETE", r"/snapshots/(?P<sid>[^/]+)")
def snapshot_delete(app, q, body, sid):
    if not app.events.delete_snapshot(sid):
        raise ApiError(404, "SNAPSHOT_NOT_FOUND", f"Snapshot {sid} does not exist")
    return {"success": True, "id": sid}


# ---- Human / Vehicle detection modules
@route("GET", r"/detections/(?P<mod>[a-z]+)/status")
def mod_status(app, q, body, mod):
    return app.module_status(_module(mod), (q.get("cameraId") or [None])[0])


@route("GET", r"/detections/(?P<mod>[a-z]+)/config")
def mod_config_get(app, q, body, mod):
    cam = app.camera_or_404(_cam_id_param(q, None))
    return {"success": True, "module": mod, "cameraId": cam["id"], "config": module_config(cam, _module(mod)),
            "defaults": DEFAULTS[mod], "zones": [{"id": z.get("id"), "name": z.get("name"), "color": z.get("color"), "enabled": z.get("enabled", True)} for z in cam.get("zones") or []],
            "liveApply": ["confidence", "classConf", "classes", "zones", "snapshot", "alert"] + (["counting"] if mod == "count" else [])
            + (["iou", "imgSize", "everyNFrames", "removeGreenOverlay", "verifier", "alertRule", "smokeRules"] if mod == "fire" else [])
            + (["monitor", "detector.conf", "detector.nameConf", "detector.nameConfFull", "detector.intervalS", "detector.showLabels"] if mod == "object" else []),
            "restartRequired": ["detector.model", "detector.imgsz", "detector.iou", "detector.tiles", "detector.enabled", "verifier"] if mod == "object" else []}


@route("PUT", r"/detections/(?P<mod>[a-z]+)/config")
def mod_config_put(app, q, body, mod):
    mod = _module(mod)
    cam = app.camera_or_404(_cam_id_param(q, body))
    new_cfg = deep_merge(module_config(cam, mod), (body or {}).get("config") or {})
    errs = validate_module(mod, new_cfg, cam.get("zones"))
    if errs:
        raise ApiError(422, "INVALID_CONFIG", "; ".join(errs))
    saved = app.registry.update_module_config(cam["id"], mod, new_cfg)
    app.sessions.config_changed(cam["id"])
    return {"success": True, "module": mod, "cameraId": cam["id"], "config": saved, "applied": "live"}


@route("POST", r"/detections/(?P<mod>[a-z]+)/start")
def mod_start(app, q, body, mod):
    mod = _module(mod)
    cid = _cam_id_param(q, body)
    try:
        s = app.sessions.start_module(cid, mod)
    except SessionError as e:
        raise ApiError(e.status, e.code, e.message)
    # Confirm that frame processing has actually started (first inference includes model warm-up).
    before = s.metrics["framesProcessed"]
    end = time.time() + 45
    while time.time() < end and s.metrics["framesProcessed"] <= before and s.state not in ("error", "stopped"):
        time.sleep(0.1)
    if s.state == "error":
        err = s.error or {"code": "ENGINE_FAILURE", "message": "Detection failed to start"}
        raise ApiError(500, err["code"], err["message"])
    if s.metrics["framesProcessed"] <= before:
        raise ApiError(504, "START_TIMEOUT", "The camera is connected but no frame was processed within 45 s (check CPU/GPU load). Detection stays enabled and will continue when frames are processed.")
    cam = app.registry.get(cid)
    cfg = module_config(cam, mod)
    if not cfg.get("enabled"):
        cfg["enabled"] = True
        app.registry.update_module_config(cid, mod, cfg)
    app.autostart.record(cid, mod)          # resumed automatically after a restart / reboot
    return {"success": True, "camera_id": cid, "cameraId": cid, "module": mod, "status": "running",
            "sharedWith": [m for m in s.modules if m != mod], "message": f"{MODULES[mod]['name']} started"}


@route("POST", r"/detections/(?P<mod>[a-z]+)/stop")
def mod_stop(app, q, body, mod):
    mod = _module(mod)
    cid = _cam_id_param(q, body)
    stopped = app.sessions.stop_module(cid, mod)
    app.autostart.forget(cid, mod)
    s = app.sessions.get(cid)
    return {"success": True, "camera_id": cid, "cameraId": cid, "module": mod, "status": "stopped", "wasRunning": stopped,
            "cameraStillOpen": bool(s), "message": f"{MODULES[mod]['name']} stopped" + (" (camera kept open for other users)" if s else "")}


@route("POST", r"/detections/(?P<mod>[a-z]+)/(?P<action>pause|resume)")
def mod_pause(app, q, body, mod, action):
    mod = _module(mod)
    cid = _cam_id_param(q, body)
    try:
        app.sessions.pause_module(cid, mod, action == "pause")
    except SessionError as e:
        raise ApiError(e.status, e.code, e.message)
    return {"success": True, "cameraId": cid, "module": mod, "status": "paused" if action == "pause" else "running"}


@route("POST", r"/detections/(?P<mod>[a-z]+)/snapshot")
def mod_snapshot(app, q, body, mod):
    mod = _module(mod)
    return app.save_manual_snapshot(app.camera_or_404(_cam_id_param(q, body)), module_id=mod)


@route("GET", r"/events/version")
def events_version(app, q, body):
    """Cheap change token for pollers: changes whenever an alert, detection or snapshot record is
    added, acknowledged or deleted (and on every backend start)."""
    return {"version": f"{BOOT_ID}-{app.events.version}"}


@route("GET", r"/detections/count/history")
def count_history(app, q, body):
    """Human Count per-minute history (max / average people, newly counted) for one camera.
    Saved minutes plus the running minute of an active session."""
    cam = app.camera_or_404(_cam_id_param(q, None))
    try:
        minutes = int((q.get("minutes") or ["60"])[0])
    except ValueError:
        raise ApiError(422, "INVALID_CONFIG", "minutes must be a whole number")
    rows = app.sessions.counts.history(cam["id"], minutes)
    s = app.sessions.get(cam["id"])
    if s and s.counter is not None and "count" in s.modules:
        live = s.counter.stats()["minutes"]
        cur = live[-1] if live and live[-1].get("partial") else None
        if cur and (not rows or rows[-1]["t"] != cur["t"]):
            rows = rows + [cur]
    return {"success": True, "cameraId": cam["id"], "minutes": rows[-max(1, minutes):]}


# ---- Object Monitoring: marked objects (the original desktop window's mouse box + keys)
def _objmon(app, q, body):
    cam = app.camera_or_404(_cam_id_param(q, body))
    s = app.sessions.get(cam["id"])
    if not s or s.objmon is None or "object" not in s.modules:
        raise ApiError(409, "NOT_RUNNING", "Start Object Monitoring on this camera first - objects are marked on its live picture")
    return cam, s


@route("GET", r"/detections/object/objects")
def object_list(app, q, body):
    cam = app.camera_or_404(_cam_id_param(q, None))
    s = app.sessions.get(cam["id"])
    running = bool(s and s.objmon is not None and "object" in s.modules)
    view = s.objmon.view() if running else {"objects": [], "messages": []}
    view["saved"] = app.sessions.marks.get(cam["id"])
    return {"success": True, "cameraId": cam["id"], "running": running, **view}


@route("POST", r"/detections/object/objects")
def object_mark(app, q, body):
    """Mark an object: body {cameraId, box: {x, y, width, height} (0-1 of the frame), name?}."""
    cam, s = _objmon(app, q, body)
    b = (body or {}).get("box") or {}
    try:
        box = tuple(float(b[k]) for k in ("x", "y", "width", "height"))
    except (KeyError, TypeError, ValueError):
        raise ApiError(422, "INVALID_BOX", "box needs x, y, width, height as fractions (0-1) of the frame")
    if not (0 <= box[0] < 1 and 0 <= box[1] < 1 and 0 < box[2] <= 1 and 0 < box[3] <= 1 and box[0] + box[2] <= 1.001 and box[1] + box[3] <= 1.001):
        raise ApiError(422, "INVALID_BOX", "the box must lie inside the frame (fractions 0-1)")
    name = str((body or {}).get("name") or "").strip()[:60] or None
    try:
        s.objmon.mark(box, name)
    except (RuntimeError, ValueError) as e:
        raise ApiError(422, "INVALID_BOX", str(e))
    return {"success": True, "cameraId": cam["id"], "message": "Object marked - its reference is taken from the current frame"}


@route("PUT", r"/detections/object/objects/(?P<oid>\d+)")
def object_rename(app, q, body, oid):
    cam, s = _objmon(app, q, body)
    name = str((body or {}).get("name") or "").strip()[:60]
    if not name:
        raise ApiError(422, "INVALID_NAME", "name is required")
    if not s.objmon.rename(oid, name):
        raise ApiError(404, "OBJECT_NOT_FOUND", f"No marked object #{oid}")
    return {"success": True, "cameraId": cam["id"], "id": int(oid), "name": name}


@route("DELETE", r"/detections/object/objects/(?P<oid>\d+)")
def object_remove(app, q, body, oid):
    cam, s = _objmon(app, q, body)
    s.objmon.remove(oid)
    return {"success": True, "cameraId": cam["id"], "id": int(oid)}


@route("POST", r"/detections/object/(?P<action>clear|accept)")
def object_action(app, q, body, action):
    """clear = remove all marked objects; accept = current positions become the new home (original key R)."""
    cam, s = _objmon(app, q, body)
    (s.objmon.clear if action == "clear" else s.objmon.accept_positions)()
    return {"success": True, "cameraId": cam["id"], "action": action}


# ---- Human Head Count (still frame: capture & count heads inside the detection area)
def _hc(fn, *a):
    try:
        return fn(*a)
    except HeadCountError as e:
        raise ApiError(e.status, e.code, e.message)


@route("GET", r"/detections/count/head-settings")
def head_settings_get(app, q, body):
    return app.headcount.get_settings()


@route("PUT", r"/detections/count/head-settings")
def head_settings_put(app, q, body):
    return _hc(app.headcount.put_settings, body)


@route("POST", r"/detections/count/head-count")
def head_count_capture(app, q, body):
    """Capture one frame of cameraId and count the heads inside its Human Count detection area(s).
    Body: {cameraId, zoneIds?: [...], roi?: [normalised zones], fullFrame?: bool, settings?: {...}}"""
    cam = app.camera_or_404(_cam_id_param(q, body))
    return _hc(app.headcount.capture_and_count, cam, body)


@route("GET", r"/detections/count/head-counts")
def head_count_history(app, q, body):
    cid = (q.get("cameraId") or [None])[0]
    return {"success": True, "results": app.headcount.history(cid, _limit(q, 20))}


@route("GET", r"/detections/(?P<mod>[a-z]+)/events")
def mod_events(app, q, body, mod):
    mod = _module(mod)
    q = dict(q)
    q["module"] = [mod]
    return _filter(app.events.detections.data, q)


class Handler(BaseHTTPRequestHandler):
    server_version = "AIVISION-backend/" + __version__
    protocol_version = "HTTP/1.1"
    app = None

    def log_message(self, fmt, *args):
        log.debug("%s %s", self.address_string(), redact(fmt % args))

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin and origin in config.CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send_json(self, status, obj):
        data = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(413, "BODY_TOO_LARGE", "Request body too large")
        if not n:
            return None
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            raise ApiError(400, "INVALID_JSON", "Request body is not valid JSON")

    def _dispatch(self, method):
        url = urlparse(self.path)
        path, q = url.path.rstrip("/") or "/", parse_qs(url.query)
        try:
            m = re.match(r"^/api/cameras/([^/]+)/stream\.mjpeg$", path)
            if method == "GET" and m:
                if (q.get("source") or [""])[0] == "main":
                    return self._mjpeg_main(m.group(1))
                return self._mjpeg(m.group(1))
            m = re.match(r"^/api/cameras/([^/]+)/frame\.jpg$", path)
            if method == "GET" and m:
                return self._clean_frame(m.group(1))
            m = re.match(r"^/api/snapshots/([^/]+)/(image|original)$", path)
            if method == "GET" and m:
                return self._snapshot_image(m.group(1), "download" in q, original=m.group(2) == "original")
            body = self._body() if method in ("POST", "PUT") else None
            for rm, rx, fn in ROUTES:
                mm = rx.match(path)
                if mm and rm == method:
                    return self._send_json(200, fn(self.app, q, body, **mm.groupdict()))
            if any(rx.match(path) for _, rx, _ in ROUTES):
                raise ApiError(405, "METHOD_NOT_ALLOWED", f"{method} is not supported on {path}")
            raise ApiError(404, "NOT_FOUND", f"No endpoint {method} {path}")
        except (ApiError, SessionError) as e:
            try:
                self._send_json(e.status, {"success": False, "error_code": e.code, "code": e.code, "message": e.message})
            except ConnectionError:
                pass   # client (e.g. a closed live-stream <img>) already went away
        except ConnectionError:
            # The browser / proxy closed the request before the reply was written (page left, request
            # timed out). ConnectionError covers BrokenPipe, ConnectionReset and Windows'
            # ConnectionAbortedError (WinError 10053) - nothing to answer any more.
            log.debug("client closed %s %s before the reply was sent", method, path)
        except Exception as e:
            log.exception("unhandled error on %s %s", method, path)
            try:
                self._send_json(500, {"success": False, "error_code": "INTERNAL_ERROR", "code": "INTERNAL_ERROR", "message": redact(e)})
            except Exception:
                pass

    def _clean_frame(self, cid):
        """Newest decoded frame of a running session, WITHOUT boxes, from the stream the AI analyses (JPEG,
        quality 95). Used by scripts/perf/pyrotech_perf.py to benchmark on representative frames at the
        real input resolution. No camera connection is opened for it."""
        self.app.camera_or_404(cid)
        s = self.app.sessions.get(cid)
        raw = s.latest_raw if s is not None else None
        if raw is None:
            raise ApiError(409, "NOT_RUNNING", "No frame yet - the camera has no running detection session")
        ok, buf = cv2.imencode(".jpg", raw, [cv2.IMWRITE_JPEG_QUALITY, 95])
        if not ok:
            raise ApiError(500, "ENCODE_FAILED", "Frame could not be encoded")
        data = buf.tobytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _snapshot_image(self, sid, download, original=False):
        rec, p = (self.app.events.snapshot_original_file if original else self.app.events.snapshot_file)(sid)
        if not rec or not p:
            raise ApiError(404, "SNAPSHOT_NOT_FOUND", "Original frame not found" if original else "Snapshot image not found")
        data = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "private, max-age=3600")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{p.name}"')
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _mjpeg(self, cid):
        try:
            s = self.app.sessions.add_viewer(cid)
        except SessionError as e:
            raise ApiError(e.status, e.code, e.message)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self._cors()
            self.end_headers()
            last = -1
            while s.state != "stopped":
                with s.cond:
                    if s.jpeg_seq == last:
                        s.cond.wait(timeout=2.0)
                    jpg, seq = s.jpeg, s.jpeg_seq
                if jpg is None or seq == last:
                    continue
                last = seq
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                self.wfile.flush()
        except (ConnectionError, OSError):
            pass
        finally:
            self.app.sessions.remove_viewer(cid)


    def _mjpeg_main(self, cid):
        """Full-resolution MAIN stream without detection boxes (?source=main). Only differs from the normal
        stream when the camera's AI uses a separate (sub)stream; otherwise the normal stream is sent."""
        try:
            s = self.app.sessions.add_viewer(cid, main=True)
        except SessionError as e:
            raise ApiError(e.status, e.code, e.message)
        if not s._separate_ai_stream():
            self.app.sessions.remove_viewer(cid, main=True)
            return self._mjpeg(cid)
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self._cors()
            self.end_headers()
            last, last_t = -1, 0.0
            while s.state != "stopped":
                vr = s.view_reader
                if vr is None:                 # opening (or retrying) the main stream
                    time.sleep(0.2)
                    continue
                ok, seq, frame, _t = vr.read_latest()
                now = time.time()
                if frame is None or seq == last or now - last_t < 1.0 / max(1.0, config.STREAM_MAX_FPS):
                    time.sleep(0.01)
                    continue
                last, last_t = seq, now
                h, w = frame.shape[:2]
                if w > config.STREAM_MAX_WIDTH:
                    frame = cv2.resize(frame, (config.STREAM_MAX_WIDTH, int(h * config.STREAM_MAX_WIDTH / w)))
                ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, config.STREAM_JPEG_QUALITY])
                if not ok:
                    continue
                jpg = buf.tobytes()
                self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpg)).encode() + b"\r\n\r\n" + jpg + b"\r\n")
                self.wfile.flush()
        except (ConnectionError, OSError):
            pass
        finally:
            self.app.sessions.remove_viewer(cid, main=True)


def serve():
    config.setup_logging()
    runtime.name_thread("api")        # HTTP threads inherit this OS thread name
    runtime.apply(log)           # CPU thread settings - once, before any model is loaded
    app = App()
    Handler.app = app
    srv = ThreadingHTTPServer((config.HOST, config.PORT), Handler)
    srv.daemon_threads = True
    log.info("PYROTECH VISION backend %s on http://%s:%d%s", __version__, config.HOST, config.PORT, config.API_PREFIX)
    log.info("model=%s (%s) data=%s snapshots=%s", config.MODEL_PATH.name, "found" if config.MODEL_PATH.is_file() else "MISSING",
             config.DATA_DIR, config.SNAPSHOT_DIR)
    def _graceful(signum, frame):   # systemctl stop / kill: same clean shutdown as Ctrl+C
        raise KeyboardInterrupt
    try:
        signal.signal(signal.SIGTERM, _graceful)
    except ValueError:                 # not in the main thread (tests)
        pass
    app.autostart.start()        # background: resume detections that were running before the restart
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        log.info("shutting down")
        app.autostart.stop()
        app.sessions.shutdown()
        srv.server_close()
