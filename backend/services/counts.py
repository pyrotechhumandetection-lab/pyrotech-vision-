"""Human Count history: one record per camera per minute (max / average people, newly counted people).

Stored in DATA_DIR/human_count.json, the last 24 hours per camera, so the statistics survive page
reloads and backend restarts. Written by the camera session thread once per completed minute.
"""
import threading

from backend import config
from backend.services.util import JsonFile

MAX_MINUTES_PER_CAMERA = 24 * 60


class CountStore:
    def __init__(self):
        self.file = JsonFile(str(config.DATA_DIR / "human_count.json"), {})
        self.lock = threading.RLock()

    def add_minutes(self, cam_id, records):
        if not records:
            return
        with self.lock:
            rows = self.file.data.setdefault(cam_id, [])
            rows.extend(records)
            del rows[:-MAX_MINUTES_PER_CAMERA]
            self.file.save()

    def history(self, cam_id, minutes=60):
        with self.lock:
            rows = list(self.file.data.get(cam_id, []))
        return rows[-max(1, min(int(minutes), MAX_MINUTES_PER_CAMERA)):]
