"""Detection events, snapshot files and alerts (JSON persistence under AIV_DATA_DIR).

Rules (per camera, per module, configurable from the UI):
  * a detection EVENT is recorded when the existing pipeline CONFIRMS a new track
    (MIN_HITS_TO_CONFIRM + label voting) - never per frame
  * a SNAPSHOT is written when the module's snapshot policy matches and its cooldown has passed;
    the record is only created after the JPEG is written and read back from disk
  * an ALERT is raised when the module's alert policy matches and its cooldown has passed
  * ByteTrack IDs can be re-assigned after occlusion, so "new track" means new to the tracker,
    not a guaranteed unique physical vehicle/person
  * exact moment: the snapshot is the frame on which the detection was confirmed (a copy taken in
    the detection loop), and event + snapshot carry that frame's capture time, the class, the
    confidence, the box and the camera IP
  * one event / snapshot per track: a track that was already reported is never reported again
  * JPEG encoding, disk writes and JSON persistence run on one writer thread (in order), so saving
    a snapshot never stalls the detection loop
"""
import queue
from collections import OrderedDict
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path

import cv2

from backend import config, runtime
from backend.services.util import JsonFile, camera_host, iso_from_ts, now_iso

log = logging.getLogger("aivision.events")

TITLES = {
    "vehicle": "Vehicle detected", "person": "Person detected", "rider": "Rider detected",
    "camera_disconnected": "Camera disconnected", "engine_failure": "Detection engine failure",
    "snapshot_failed": "Snapshot saving failed", "manual": "Manual capture",
    "occupancy": "People count threshold reached", "head_count": "Head count",
    "fire": "Fire detected", "smoke": "Smoke detected",
    "object_moved": "Object moved", "object_missing": "Object missing", "object_restored": "Object back in place",
    "ppe_missing": "PPE missing", "ppe_vest_color": "Safety vest wrong colour", "ppe_review": "PPE needs review",
    "twhelmet_compliant": "Two-wheeler helmet compliant", "twhelmet_missing": "Two-wheeler helmet missing",
    "twhelmet_unverified": "Two-wheeler helmet not verified", "twhelmet_vehicle_only": "Two-wheeler without rider",
    "line_cross_in": "Person crossed the line (IN)", "line_cross_out": "Person crossed the line (OUT)",
}


# Vehicle category used by the Snapshots page filters (from the existing class mapping).
VEHICLE_CATEGORY = {"4-Wheeler": "car", "Truck": "truck", "2-Wheeler": "two_wheeler", "Bus": "bus"}


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_") or "event"


FAR_BOX_HEIGHT = 0.15     # a confirmed box lower than 15 % of the picture height = far / small object


def far_snapshot(frame, original, box, label, conf):
    """Snapshot of a far (small) detection that can be read: the full-resolution picture (main stream) with the box,
    plus an enlarged view of the object in the top-right corner. Other detections: the frame as it is."""
    if frame is None or not box or len(box) != 4:
        return frame
    x1, y1, x2, y2 = (float(v) for v in box)
    if y2 - y1 >= FAR_BOX_HEIGHT:
        return frame
    try:
        base = original if original is not None and original.shape[0] > frame.shape[0] else frame
        img = base.copy() if base is original else frame.copy()
        h, w = img.shape[:2]
        bx1, by1, bx2, by2 = int(x1 * w), int(y1 * h), int(x2 * w), int(y2 * h)
        col = (0, 255, 255)
        if base is original:                       # the HD original has no drawings yet
            cv2.rectangle(img, (bx1, by1), (bx2, by2), col, max(2, w // 640))
            cv2.putText(img, f"{label} {conf:.2f}" if conf is not None else label, (bx1, max(by1 - 8, 18)),
                        cv2.FONT_HERSHEY_SIMPLEX, max(0.5, w / 1600), col, max(1, w // 960))
        # enlarged view: the object with some context (2.5x its size), in the picture corner farthest from it
        bw, bh = max(1, bx2 - bx1), max(1, by2 - by1)
        side = int(max(2.5 * max(bw, bh), 64))
        cx, cy = (bx1 + bx2) // 2, (by1 + by2) // 2
        side = min(side, w, h)
        cx1, cy1 = max(0, min(w - side, cx - side // 2)), max(0, min(h - side, cy - side // 2))
        crop = base[cy1:cy1 + side, cx1:cx1 + side].copy()
        if crop.size == 0:
            return img
        cv2.rectangle(crop, (bx1 - cx1, by1 - cy1), (bx2 - cx1, by2 - cy1), col, 2)
        inset = int(h * 0.45)
        zoom = cv2.resize(crop, (inset, inset), interpolation=cv2.INTER_CUBIC)
        pad = max(4, h // 150)
        x0 = pad * 2 if cx > w / 2 else w - inset - 2 * pad          # opposite side of the object
        y0 = h - inset - 2 * pad if cy < h / 2 else pad * 2
        cv2.rectangle(img, (x0 - pad, y0 - pad), (x0 + inset + pad, y0 + inset + pad), (255, 255, 255), -1)
        img[y0:y0 + inset, x0:x0 + inset] = zoom
        txt = f"ZOOM x{inset / max(1, crop.shape[0]):.1f}"
        fs = max(0.6, h / 800)
        cv2.putText(img, txt, (x0 + 10, y0 + inset - 14), cv2.FONT_HERSHEY_SIMPLEX, fs, (0, 0, 0), 5)
        cv2.putText(img, txt, (x0 + 10, y0 + inset - 14), cv2.FONT_HERSHEY_SIMPLEX, fs, (255, 255, 255), 2)
        # line from the object to the zoom view
        cv2.line(img, (cx, cy), (x0 + inset // 2 if x0 > cx else x0 + inset, y0 if y0 > cy else y0 + inset), col, max(1, w // 1280))
        return img
    except Exception:          # never lose the snapshot because of the zoom view
        log.exception("far snapshot zoom failed")
        return frame


class EventStore:
    def __init__(self):
        d = config.DATA_DIR
        self.detections = JsonFile(str(d / "detections.json"), [])
        self.alerts = JsonFile(str(d / "alerts.json"), [])
        self.snapshots = JsonFile(str(d / "snapshots.json"), [])
        self.lock = threading.RLock()
        self._cool = {}           # cooldown key -> last time
        self.version = 0          # bumps on every change (cheap change detection for pollers)
        self.edits = 0            # bumps only when an EXISTING record changes or is removed (acknowledge, delete):
                                  # pollers then reload the lists; otherwise they fetch only the newest records
        self._reported = OrderedDict()   # (camera, module, trackId) already reported -> True (bounded)
        self._jobs = queue.Queue(maxsize=64)
        self.writer_stats = {"jobs": 0, "msTotal": 0.0, "msMax": 0.0}   # snapshot / alert writing (perf monitor)
        self._writer = threading.Thread(target=self._write_loop, name="event-writer", daemon=True)
        self._writer.start()

    # ------------------------------------------------------------------ helpers
    def _cooldown_ok(self, key, seconds):
        now = time.time()
        last = self._cool.get(key, 0)
        if seconds and now - last < seconds:
            return False
        self._cool[key] = now
        return True

    def _push(self, store, rec):
        with self.lock:
            store.data.insert(0, rec)
            del store.data[config.MAX_EVENT_RECORDS:]
            store.save()
            self.version += 1

    # ------------------------------------------------------------------ snapshots
    def snapshot_root(self):
        return Path(config.SNAPSHOT_DIR).resolve()

    def save_snapshot(self, frame, camera, module, label, source="detection", confidence=None, event_id=None, severity=None,
                      captured_at=None, extra=None, original=None, folder=None, etype=None):
        """Write the JPEG, verify it, then persist the record. Raises OSError on failure.

        original: optional unannotated frame saved next to it as <name>_original.jpg (Head Count);
        folder / etype: override the snapshot sub-folder and the record type.

        captured_at: time.time() at which the frame was received from the camera (default: now)."""
        if frame is None or getattr(frame, "size", 0) == 0:
            raise OSError("no valid frame available")
        now = datetime.fromtimestamp(captured_at) if captured_at else datetime.now()
        folder = folder or ("manual" if module == "manual" else f"{module}_detection")
        rel_dir = Path(folder) / f"{now:%Y}" / f"{now:%m}" / f"{now:%d}"
        name = f"{_slug(camera['id']).upper()}_{module}_{_slug(label)}_{now:%Y%m%d_%H%M%S}_{now.microsecond // 1000:03d}_{secrets.token_hex(2)}.jpg"
        root = self.snapshot_root()
        target = (root / rel_dir / name).resolve()
        if root not in target.parents:
            raise OSError("snapshot path escapes the configured snapshot directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 90])
        if not ok:
            raise OSError("JPEG encoding failed")
        with open(target, "wb") as f:
            f.write(buf.tobytes())
        size = target.stat().st_size
        if size <= 0 or cv2.imread(str(target)) is None:
            raise OSError("snapshot file could not be read back")
        orig_target = None
        if original is not None:
            orig_target = target.with_name(target.stem + "_original.jpg")
            ok, obuf = cv2.imencode(".jpg", original, [cv2.IMWRITE_JPEG_QUALITY, 95])
            if not ok:
                target.unlink(missing_ok=True)
                raise OSError("JPEG encoding of the original frame failed")
            with open(orig_target, "wb") as f:
                f.write(obuf.tobytes())
            if orig_target.stat().st_size <= 0:
                target.unlink(missing_ok=True)
                raise OSError("original frame could not be written")
        h, w = frame.shape[:2]
        sid = f"SN-{now:%Y%m%d%H%M%S}-{secrets.token_hex(3)}"
        etype = etype or ("manual" if source == "manual" else _etype(module, label))
        rec = {
            "id": sid, "cameraId": camera["id"], "cameraName": camera.get("name"), "module": module,
            "type": etype, "label": label, "title": f"{TITLES.get(etype, 'Snapshot')}" + (f": {label}" if label and source != "manual" else ""),
            "timestamp": iso_from_ts(captured_at) if captured_at else now_iso(), "confidence": confidence, "eventId": event_id,
            "alertId": None, "severity": severity, "cameraIp": camera_host(camera),
            "path": str(target.relative_to(root)).replace("\\", "/"), "url": f"snapshots/{sid}/image", "image": f"snapshots/{sid}/image",
            "sizeBytes": size, "width": w, "height": h, "source": source,
        }
        if orig_target is not None:
            rec["originalPath"] = str(orig_target.relative_to(root)).replace("\\", "/")
            rec["originalUrl"] = f"snapshots/{sid}/original"
        rec.update(extra or {})
        self._push(self.snapshots, rec)
        log.info("snapshot saved %s (%d bytes)", rec["path"], size)
        return rec

    def snapshot_file(self, sid):
        rec = next((s for s in self.snapshots.data if s["id"] == sid), None)
        if not rec:
            return None, None
        root = self.snapshot_root()
        p = (root / rec["path"]).resolve()
        if root not in p.parents or not p.is_file():
            return rec, None
        return rec, p

    def snapshot_original_file(self, sid):
        """(record, path) of the unannotated frame saved with a Head Count snapshot."""
        rec = next((s for s in self.snapshots.data if s["id"] == sid), None)
        if not rec or not rec.get("originalPath"):
            return rec, None
        root = self.snapshot_root()
        p = (root / rec["originalPath"]).resolve()
        if root not in p.parents or not p.is_file():
            return rec, None
        return rec, p

    def delete_snapshot(self, sid):
        with self.lock:
            rec, p = self.snapshot_file(sid)
            if not rec:
                return False
            if p:
                os.remove(p)
            _r, op = self.snapshot_original_file(sid)
            if op:
                os.remove(op)
            self.snapshots.data = [s for s in self.snapshots.data if s["id"] != sid]
            self.snapshots.save()
            self.version += 1
            self.edits += 1
            return True

    def _link_snapshot(self, sid, **fields):
        with self.lock:
            for s in self.snapshots.data:
                if s["id"] == sid:
                    s.update(fields)
            self.snapshots.save()

    # ------------------------------------------------------------------ alerts
    def raise_alert(self, camera, atype, severity, description, module=None, label=None, confidence=None,
                    snapshot=None, event_id=None, cooldown_key=None, cooldown_sec=0):
        if cooldown_key and not self._cooldown_ok(("alert",) + tuple(cooldown_key), cooldown_sec):
            return None
        title = TITLES.get(atype, atype) + (f": {label}" if label else "")
        rec = {
            "id": f"AL-{datetime.now():%Y%m%d%H%M%S}-{secrets.token_hex(3)}", "type": atype, "module": module, "label": label,
            "title": title, "description": description, "cameraId": camera["id"], "cameraName": camera.get("name"),
            "timestamp": now_iso(), "confidence": confidence, "severity": severity,
            "snapshotId": snapshot["id"] if snapshot else None, "image": snapshot["image"] if snapshot else None,
            "eventId": event_id, "status": "new", "acknowledgedAt": None, "acknowledgedBy": None, "notes": [], "source": "backend",
        }
        self._push(self.alerts, rec)
        if snapshot:
            self._link_snapshot(snapshot["id"], alertId=rec["id"], severity=severity)
        log.info("alert %s %s on %s", rec["id"], title, camera["id"])
        return rec

    def acknowledge(self, aid, note="", by="operator"):
        with self.lock:
            a = next((x for x in self.alerts.data if x["id"] == aid), None)
            if not a:
                return None
            if a["status"] != "acknowledged":
                a["status"] = "acknowledged"
                a["acknowledgedAt"] = now_iso()
                a["acknowledgedBy"] = by
            if note:
                a.setdefault("notes", []).append({"text": note[:500], "at": now_iso(), "by": by})
            self.alerts.save()
            self.version += 1
            self.edits += 1
            return a

    # ------------------------------------------------------------------ detection pipeline hook
    def on_confirmation(self, camera, module, cfg, item, frame, captured_at=None, original=None):
        """Called by the session for each NEW confirmed track, with the frame that confirmed it.

        Decides synchronously (dedupe, cooldowns), copies the frame, and queues the disk work.
        Returns the event record (completed by the writer thread), or None for an already reported track."""
        key = (camera["id"], module, item.get("trackId"))
        with self.lock:
            if item.get("trackId") is not None:
                if key in self._reported:
                    return None
                self._reported[key] = True
                while len(self._reported) > 20000:
                    self._reported.popitem(last=False)
        label = item["label"]
        etype = _etype(module, label)
        detected = iso_from_ts(captured_at) if captured_at else now_iso()
        event = {
            "id": f"EV-{datetime.now():%Y%m%d%H%M%S}-{secrets.token_hex(3)}", "type": etype, "module": module, "label": label,
            "title": f"{TITLES[etype]}: {label}", "cameraId": camera["id"], "cameraName": camera.get("name"),
            "cameraIp": camera_host(camera), "timestamp": detected, "confidence": item.get("confidence"),
            "trackId": item.get("trackId"), "zone": item.get("zone"), "box": item.get("box"),
            "snapshotId": None, "alertId": None, "image": None, "source": "backend",
        }
        if module == "vehicle":
            event["vehicleClass"] = VEHICLE_CATEGORY.get(label)
        scfg, acfg = cfg.get("snapshot", {}), cfg.get("alert", {})
        want_alert = bool(acfg.get("enabled") and label in (acfg.get("labels") or []) and
                          self._cooldown_ok(("alert", camera["id"], module, label), float(acfg.get("cooldownSec", 0))))
        want_snap = scfg.get("mode") == "new_track" and (label in (scfg.get("labels") or []) or want_alert) and \
            (want_alert or self._cooldown_ok(("snap", camera["id"], module, label), float(scfg.get("cooldownSec", 0))))
        job = (dict(camera), module, label, etype, dict(item), frame.copy() if want_snap and frame is not None else None,
               captured_at, event, want_snap, want_alert, dict(acfg), original if want_snap else None)
        self._jobs.put(job)      # blocks only if 64 events are already waiting (back-pressure, nothing is lost)
        return event

    def _write_loop(self):
        runtime.name_thread("evt-writer")
        while True:
            job = self._jobs.get()
            t0 = time.perf_counter()
            try:
                if callable(job):
                    job()
                else:
                    self._complete(*job)
            except Exception:
                log.exception("event writer failed")
            finally:
                ms = (time.perf_counter() - t0) * 1000.0
                ws = self.writer_stats
                ws["jobs"] += 1
                ws["msTotal"] += ms
                ws["msMax"] = max(ws["msMax"], ms)
                self._jobs.task_done()

    def _complete(self, camera, module, label, etype, item, frame, captured_at, event, want_snap, want_alert, acfg, original=None):
        snap = None
        if want_snap:
            try:
                extra = {"trackId": item.get("trackId"), "box": item.get("box"), "detectedAt": event["timestamp"]}
                if item.get("hits") is not None:
                    extra["trackHits"] = item["hits"]            # frames of this track up to the confirming frame
                if item.get("firstSeenToConfirmMs") is not None:
                    extra["firstSeenToConfirmMs"] = item["firstSeenToConfirmMs"]   # tracker confirmation delay
                if module == "vehicle":
                    extra["vehicleClass"] = VEHICLE_CATEGORY.get(label)
                frame = far_snapshot(frame, original, item.get("box"), label, item.get("confidence"))
                snap = self.save_snapshot(frame, camera, module, label, confidence=item.get("confidence"), event_id=event["id"],
                                          severity=acfg.get("severity") if want_alert else None, captured_at=captured_at, extra=extra,
                                          original=original)
                event["snapshotId"], event["image"] = snap["id"], snap["image"]
                if captured_at:      # capture of the triggering frame -> both JPEGs on disk
                    d = (time.time() - captured_at) * 1000.0
                    ws = self.writer_stats
                    ws["snapN"] = ws.get("snapN", 0) + 1
                    ws["snapMsTotal"] = ws.get("snapMsTotal", 0.0) + d
                    ws["snapMsMax"] = max(ws.get("snapMsMax", 0.0), d)
            except OSError as e:
                log.error("snapshot failed for %s: %s", camera["id"], e)
                self.raise_alert(camera, "snapshot_failed", "medium", f"Could not save snapshot: {e}",
                                 module=module, cooldown_key=(camera["id"], "snapshot_failed"), cooldown_sec=300)
        if want_alert:
            zone = f" in zone '{item['zone']}'" if item.get("zone") else ""
            desc = f"{label} confirmed by the tracker (track #{item.get('trackId')}){zone}."
            a = self.raise_alert(camera, etype, acfg.get("severity", "low"), desc, module=module, label=label,
                                 confidence=item.get("confidence"), snapshot=snap, event_id=event["id"])
            if a:
                event["alertId"] = a["id"]
        self._push(self.detections, event)

    def on_count_alert(self, camera, acfg, count, frame, captured_at=None):
        """Human Count occupancy alert (people now >= threshold): alert + snapshot of the frame that
        raised it, written by the event writer thread. Cooldown per camera."""
        if not self._cooldown_ok(("alert", camera["id"], "count", "occupancy"), float(acfg.get("cooldownSec", 60))):
            return False
        cam, img = dict(camera), frame.copy()
        threshold = int(acfg.get("threshold") or 0)

        def job():
            snap = None
            try:
                snap = self.save_snapshot(img, cam, "count", "people", confidence=None, severity=acfg.get("severity", "medium"),
                                          captured_at=captured_at, extra={"peopleCount": count, "threshold": threshold})
            except OSError as e:
                log.error("occupancy snapshot failed for %s: %s", cam["id"], e)
            self.raise_alert(cam, "occupancy", acfg.get("severity", "medium"),
                             f"{count} people in view (threshold {threshold}).", module="count", label=f"{count} people",
                             snapshot=snap)
        self._jobs.put(job)
        return True

    def on_module_alert(self, camera, module, etype, label, frame, captured_at=None, confidence=None, severity="high",
                        description="", alert=True, snapshot=True, extra=None, cooldown_key=None, cooldown_sec=0,
                        original=None):
        """Fire & Smoke / Object Monitoring: one confirmed event -> detection record + snapshot of the frame
        that confirmed it + (optional) alert, written in order by the event writer thread.
        The module decides WHEN (its own confirmation rules); this only stores the result."""
        if cooldown_key and not self._cooldown_ok(("modalert",) + tuple(cooldown_key), cooldown_sec):
            return None
        cam, img = dict(camera), (frame.copy() if (snapshot and frame is not None) else None)
        detected = iso_from_ts(captured_at) if captured_at else now_iso()
        event = {
            "id": f"EV-{datetime.now():%Y%m%d%H%M%S}-{secrets.token_hex(3)}", "type": etype, "module": module, "label": label,
            "title": f"{TITLES.get(etype, etype)}: {label}" if label else TITLES.get(etype, etype), "cameraId": cam["id"],
            "cameraName": cam.get("name"), "cameraIp": camera_host(cam), "timestamp": detected, "confidence": confidence,
            "trackId": None, "zone": None, "box": None, "snapshotId": None, "alertId": None, "image": None,
            "source": "backend", "description": description,
        }
        event.update(extra or {})

        def job():
            snap = None
            if img is not None:
                try:
                    snap = self.save_snapshot(img, cam, module, label or etype, confidence=confidence, event_id=event["id"],
                                              severity=severity if alert else None, captured_at=captured_at,
                                              etype=etype, extra=dict(extra or {}, detectedAt=detected, description=description),
                                              original=original if snapshot else None)
                    event["snapshotId"], event["image"] = snap["id"], snap["image"]
                except OSError as e:
                    log.error("snapshot failed for %s: %s", cam["id"], e)
                    self.raise_alert(cam, "snapshot_failed", "medium", f"Could not save snapshot: {e}", module=module,
                                     cooldown_key=(cam["id"], "snapshot_failed"), cooldown_sec=300)
            if alert:
                a = self.raise_alert(cam, etype, severity, description, module=module, label=label, confidence=confidence,
                                     snapshot=snap, event_id=event["id"])
                if a:
                    event["alertId"] = a["id"]
            self._push(self.detections, event)
        self._jobs.put(job)
        return event

    def flush(self, timeout=10.0):
        """Wait until every queued event has been written (tests, shutdown)."""
        end = time.time() + timeout
        while self._jobs.unfinished_tasks and time.time() < end:
            time.sleep(0.01)
        return not self._jobs.unfinished_tasks


def _etype(module, label):
    if module == "count":
        return "occupancy"
    if module == "human":
        return "rider" if label == "Rider" else "person"
    return "vehicle"
