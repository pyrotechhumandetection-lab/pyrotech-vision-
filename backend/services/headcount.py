"""Human Head Count service: capture ONE still frame from a camera, count the heads inside its detection
area(s) and save the original + annotated images with a snapshot record.

Detection area (ROI)
    The camera's Detection Area zones that have "Human Count" (module id "count") ticked and are enabled -
    drawn in the camera form or on the Human Count page. A request may also name zones (zoneIds), send its
    own normalised zones (roi) or ask for the whole frame (fullFrame).

Frame
    From the camera's running session when its newest decoded frame is at most AIV_HEAD_MAX_FRAME_AGE
    seconds old (no second RTSP connection), otherwise a fresh frame is read from the camera.
The count, the annotated image and the original image always come from the same frame array.
"""
import logging
import threading
import time
from datetime import datetime

from backend import config
from backend.detection import head_count as hc
from backend.detection.rtsp_probe import grab_frame
from backend.services.util import JsonFile, camera_host

log = logging.getLogger("aivision.headcount")
MODULE_ID = "count"


class HeadCountError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def validate_roi(zones):
    """Validate request-supplied normalised zones. Returns [errors]."""
    errs = []
    if not isinstance(zones, list) or not zones:
        return ["roi must be a non-empty list of zones"]
    for i, z in enumerate(zones):
        if not isinstance(z, dict):
            errs.append(f"roi[{i}] must be an object")
            continue
        try:
            if z.get("type") == "rectangle":
                x, y, w, h = (float(z[k]) for k in ("x", "y", "width", "height"))
                if not (0 <= x <= 1 and 0 <= y <= 1 and w > 0.005 and h > 0.005 and x + w <= 1.0001 and y + h <= 1.0001):
                    errs.append(f"roi[{i}]: rectangle must lie inside the frame (normalised 0-1)")
            elif z.get("type") == "polygon":
                pts = z.get("points") or []
                if len(pts) < 3 or not all(0 <= float(p["x"]) <= 1 and 0 <= float(p["y"]) <= 1 for p in pts):
                    errs.append(f"roi[{i}]: polygon needs at least 3 points inside the frame (normalised 0-1)")
            else:
                errs.append(f"roi[{i}]: type must be rectangle or polygon")
        except (KeyError, TypeError, ValueError):
            errs.append(f"roi[{i}]: invalid coordinates")
    return errs


class HeadCountService:
    def __init__(self, registry, sessions, events):
        self.registry, self.sessions, self.events = registry, sessions, events
        self.settings = JsonFile(str(config.DATA_DIR / "head_count_settings.json"), dict(hc.DEFAULT_SETTINGS))
        self.busy = threading.Lock()
        self.last_engine = None

    # ------------------------------------------------------------------ settings
    def get_settings(self):
        s, _ = hc.validate_settings(self.settings.data)
        engine, _p = hc.resolve_engine()
        return {"success": True, "settings": s, "defaults": dict(hc.DEFAULT_SETTINGS),
                "limits": {k: list(v) for k, v in hc.LIMITS.items()}, "model": hc.MODEL_NAME,
                "engine": self.last_engine or engine, "modelLoaded": hc._detector is not None}

    def put_settings(self, body):
        s, errs = hc.validate_settings(body or {})
        if errs:
            raise HeadCountError(422, "INVALID_CONFIG", "; ".join(errs))
        self.settings.data = s
        self.settings.save()
        return self.get_settings()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def count_zones(cam):
        """Enabled Detection Area zones of the camera with Human Count ticked."""
        if cam.get("zonesEnabled") is False:
            return []
        return [z for z in cam.get("zones") or [] if z.get("enabled", True) and MODULE_ID in (z.get("modules") or [])]

    def capture_frame(self, cam):
        """(frame, captured_at, source) - newest session frame if fresh, else a new frame from the camera."""
        s = self.sessions.get(cam["id"])
        reader = getattr(s, "reader", None) if s else None
        if reader is not None:
            try:
                ok, _seq, frame, captured = reader.read_latest()
            except Exception:
                frame, captured = None, None
            if frame is not None and captured and time.time() - captured <= config.HEAD_MAX_FRAME_AGE_SEC:
                return frame, captured, "session"
        try:
            source = self.registry.resolve_source(cam)
        except ValueError as e:
            raise HeadCountError(400, "INVALID_CONFIG", str(e))
        t0 = time.time()
        box = grab_frame(source, float(cam.get("timeoutSec") or config.CONNECT_TIMEOUT_SEC))
        frame = box.get("frame")
        if frame is None:
            msg = box.get("error") or ("camera did not deliver a frame" if box.get("opened") else "camera could not be opened")
            raise HeadCountError(502, "NO_FRAME", f"No frame could be captured from the camera: {msg}")
        return frame, max(t0, time.time() - 0.05), "camera"

    # ------------------------------------------------------------------ capture & count
    def capture_and_count(self, cam, body):
        body = body or {}
        all_zones = {z["id"]: z for z in cam.get("zones") or [] if z.get("id")}
        if body.get("fullFrame"):
            zones = []
        elif body.get("roi") is not None:
            errs = validate_roi(body["roi"])
            if errs:
                raise HeadCountError(422, "INVALID_ROI", "; ".join(errs))
            zones = [dict(z, name=z.get("name") or f"Area {i + 1}") for i, z in enumerate(body["roi"])]
        elif body.get("zoneIds"):
            missing = [i for i in body["zoneIds"] if i not in all_zones]
            if missing:
                raise HeadCountError(422, "INVALID_ROI", "Unknown detection area(s): " + ", ".join(map(str, missing)))
            zones = [all_zones[i] for i in body["zoneIds"]]
        else:
            zones = self.count_zones(cam)
            if not zones:
                raise HeadCountError(422, "ROI_REQUIRED", "Draw or select a detection area for Human Count first "
                                                          "(or count the whole frame).")
        settings = dict(self.settings.data)
        if isinstance(body.get("settings"), dict):
            settings.update(body["settings"])
        settings, errs = hc.validate_settings(settings)
        if errs:
            raise HeadCountError(422, "INVALID_CONFIG", "; ".join(errs))

        if not self.busy.acquire(blocking=False):
            raise HeadCountError(409, "BUSY", "A head count is already running - wait for it to finish")
        try:
            try:
                detector = hc.get_detector()
            except Exception as e:
                log.exception("head detector could not be loaded")
                raise HeadCountError(500, "MODEL_UNAVAILABLE", f"Head detection model could not be loaded: {e}")
            self.last_engine = detector.engine
            frame, captured_at, frame_source = self.capture_frame(cam)
            t0 = time.perf_counter()
            result = hc.count_heads(frame, zones, settings, detector)
            ip = camera_host(cam)
            when = datetime.fromtimestamp(captured_at)
            info = {"cameraIp": ip or cam.get("name") or cam["id"], "capturedAt": when.strftime("%Y-%m-%d %H:%M:%S")}
            annotated = hc.annotate(frame, result, zones, info)
            total_ms = (time.perf_counter() - t0) * 1000.0
            n = len(result["heads"])
            roi = [{k: z.get(k) for k in ("id", "name", "type", "x", "y", "width", "height", "points", "color") if z.get(k) is not None}
                   for z in zones]
            extra = {"headCount": n, "heads": result["heads"], "roi": roi, "fullFrame": not zones,
                     "headSettings": result["settings"], "inferenceMs": result["inferenceMs"], "passes": result["passes"],
                     "engine": result["engine"], "model": hc.MODEL_NAME, "frameSource": frame_source,
                     "capturedAtLocal": info["capturedAt"],
                     "title": f"Head count: {n} head{'s' if n != 1 else ''}"}
            try:
                rec = self.events.save_snapshot(annotated, cam, MODULE_ID, "Head count", source="headcount",
                                                confidence=None, captured_at=captured_at, extra=extra,
                                                original=frame, folder="head_count", etype="head_count")
            except OSError as e:
                raise HeadCountError(500, "SNAPSHOT_WRITE_FAILED", f"Images could not be saved: {e}")
            log.info("head count %s: %d heads (%s, %.0f ms inference, %d pass(es))", cam["id"], n, result["engine"],
                     result["inferenceMs"], result["passes"])
            return {"success": True, "cameraId": cam["id"], "cameraName": cam.get("name"), "cameraIp": ip,
                    "totalHeads": n, "heads": result["heads"], "capturedAt": rec["timestamp"],
                    "capturedAtLocal": info["capturedAt"], "frameSource": frame_source,
                    "width": int(frame.shape[1]), "height": int(frame.shape[0]), "roi": roi, "fullFrame": not zones,
                    "settings": result["settings"], "inferenceMs": result["inferenceMs"], "totalMs": round(total_ms, 1),
                    "engine": result["engine"], "model": hc.MODEL_NAME, "snapshot": rec,
                    "message": None if n else "No heads were detected in the selected area."}
        finally:
            self.busy.release()

    def history(self, cam_id=None, limit=20):
        rows = [r for r in self.events.snapshots.data if r.get("type") == "head_count" and (not cam_id or r.get("cameraId") == cam_id)]
        return rows[:limit]
