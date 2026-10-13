"""Resume detections after a backend restart (power cut, reboot, service restart).

The backend remembers which detection modules the user STARTED on which camera
(data/running_modules.json). Clicking Stop, deleting the camera or turning
AIV_AUTOSTART off removes the entry. When the backend starts, a background thread
starts the remembered modules again; cameras that are not reachable yet (still
booting, network down) are retried until they answer. The web UI and API are
available immediately - restoring never blocks them.

The camera module setting "enabled" is not used for this: it is also set by the
camera form and is not cleared on Stop.
"""
import logging
import threading
import time

from backend import config
from backend.services.util import JsonFile, redact

log = logging.getLogger("aivision.autostart")

FIRST_DELAY_S = 3          # let the HTTP server come up first
RETRY_MIN_S = 10           # unreachable camera: retry after 10 s, doubling up to 2 min
RETRY_MAX_S = 120
# errors that a retry cannot fix -> give up for this module until the user starts it again
PERMANENT = {"CAMERA_NOT_FOUND", "CAMERA_DISABLED", "UNKNOWN_MODULE", "MODEL_NOT_FOUND", "INVALID_CONFIG"}


def enabled():
    return str(config._env("AIV_AUTOSTART", "1")).strip().lower() not in ("0", "off", "false", "no")


class AutoStart:
    def __init__(self, sessions, registry):
        self.sessions = sessions
        self.registry = registry
        self.store = JsonFile(config.DATA_DIR / "running_modules.json", {})
        self.stop_event = threading.Event()
        self.thread = None
        self.state = {}        # "CAM|module" -> {"status": ..., "message": ..., "attempts": n}

    # ------------------------------------------------------------- bookkeeping
    def record(self, cam_id, module_id):
        with self.store.lock:
            mods = self.store.data.setdefault(cam_id, [])
            if module_id not in mods:
                mods.append(module_id)
                self.store.save()

    def forget(self, cam_id, module_id=None):
        with self.store.lock:
            mods = self.store.data.get(cam_id)
            if mods is None:
                return
            if module_id is None or module_id in mods:
                if module_id is None:
                    self.store.data.pop(cam_id, None)
                else:
                    mods.remove(module_id)
                    if not mods:
                        self.store.data.pop(cam_id, None)
                self.store.save()
        for key in [k for k in self.state if k == f"{cam_id}|{module_id}" or (module_id is None and k.startswith(cam_id + "|"))]:
            self.state.pop(key, None)

    def remembered(self):
        with self.store.lock:
            return [(c, m) for c, mods in self.store.data.items() for m in list(mods)]

    # ------------------------------------------------------------- restore
    def start(self):
        if not enabled():
            log.info("detection auto-resume is off (AIV_AUTOSTART=0)")
            return
        items = self.remembered()
        if not items:
            return
        log.info("resuming %d detection(s) that were running before the restart", len(items))
        self.thread = threading.Thread(target=self._run, name="autostart", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def _run(self):
        if self.stop_event.wait(FIRST_DELAY_S):
            return
        pending = {f"{c}|{m}": (c, m, 0.0, RETRY_MIN_S) for c, m in self.remembered()}
        while pending and not self.stop_event.is_set():
            now = time.time()
            for key, (cam_id, mod, due, wait) in list(pending.items()):
                if self.stop_event.is_set():
                    return
                if (cam_id, mod) not in self.remembered():      # user stopped it meanwhile
                    pending.pop(key, None)
                    continue
                if now < due:
                    continue
                st = self.state.setdefault(key, {"attempts": 0})
                st["attempts"] += 1
                try:
                    self.sessions.start_module(cam_id, mod)
                    st.update(status="running", message="resumed")
                    log.info("resumed %s on %s", mod, cam_id)
                    pending.pop(key, None)
                except Exception as e:  # noqa: BLE001 - SessionError and anything unexpected
                    code = getattr(e, "code", "ERROR")
                    msg = redact(getattr(e, "message", e))
                    if code in PERMANENT:
                        st.update(status="failed", message=f"{code}: {msg}")
                        log.warning("cannot resume %s on %s: %s (%s) - start it again in the UI", mod, cam_id, msg, code)
                        pending.pop(key, None)
                        continue
                    st.update(status="retrying", message=f"{code}: {msg}")
                    log.warning("resume %s on %s failed (%s: %s) - retrying in %d s", mod, cam_id, code, msg, wait)
                    pending[key] = (cam_id, mod, time.time() + wait, min(wait * 2, RETRY_MAX_S))
            if self.stop_event.wait(1):
                return

    def status(self):
        live = getattr(self.sessions, "sessions", None) or {}
        out = []
        for c, m in self.remembered():
            st = dict(self.state.get(f"{c}|{m}") or {"status": "waiting"})
            s = live.get(c)
            if s is not None and m in getattr(s, "modules", {}):
                st.update(status="running", message="")
            out.append({"cameraId": c, "module": m, **st})
        return {"enabled": enabled(), "remembered": out}
