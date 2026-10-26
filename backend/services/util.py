"""Small shared helpers: atomic JSON files, credential redaction, IDs."""
import json
import os
import re
import secrets
import tempfile
import threading
from datetime import datetime, timezone

_URL_CRED = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s]*@", re.I)


# credentials written into the path of some cameras' RTSP URLs: .../user=admin_password=secret_channel=1_stream=0.sdp
_PATH_CRED = re.compile(r"(?i)\b(user(?:name)?=)([^_&/?\s]*)([_&])(pass(?:word|wd)?=)([^_&/?\s]*)")


def redact(text):
    """Mask user:password@ and user=..._password=... in any URL inside a string (for logs, errors, API responses)."""
    t = _URL_CRED.sub(r"\1***:***@", str(text or ""))
    def mask(m):
        if m.group(2) == "{user}" and m.group(5) == "{password}":      # the stored form: placeholders, no secret
            return m.group(0)
        return m.group(1) + "***" + m.group(3) + m.group(4) + "***"
    return _PATH_CRED.sub(mask, t)


def camera_host(camera):
    """Host / IP of a network camera from its source URL (credentials never included), else None."""
    src = str((camera or {}).get("sourceUrl") or "").strip()
    m = re.match(r"^[a-z][a-z0-9+.-]*://([^/?#]*)", src, re.I)
    if not m:
        return None
    auth = m.group(1).rsplit("@", 1)[-1]
    host = re.sub(r":\d+$", "", auth).strip("[]")
    return host or None


def iso_from_ts(ts):
    """ISO-8601 local time (ms) of a time.time() value."""
    return datetime.fromtimestamp(ts, timezone.utc).astimezone().isoformat(timespec="milliseconds")


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def new_id(prefix):
    return f"{prefix}-{datetime.now():%Y%m%d%H%M%S}-{secrets.token_hex(3)}"


class JsonFile:
    """Thread-safe JSON document persisted with atomic replace."""

    def __init__(self, path, default):
        self.path = path
        self.lock = threading.RLock()
        self.default = default
        self.data = self._load()

    def _load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return json.loads(json.dumps(self.default))
        except (OSError, ValueError):
            # keep the unreadable file for inspection instead of overwriting it
            bad = str(self.path) + ".corrupt"
            try:
                os.replace(self.path, bad)
            except OSError:
                pass
            return json.loads(json.dumps(self.default))

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=1, ensure_ascii=False)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
