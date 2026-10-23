"""Camera registry + credential storage.

The UI's Camera Management page is the single place cameras are configured; it calls
POST/PUT /api/cameras and this module stores them. Passwords:
  * are stored separately from camera metadata (data/credentials.json)
  * are encrypted with Windows DPAPI (current user) on Windows; on other OSes they are
    stored with owner-only file permissions and a warning is logged
  * are NEVER returned by the API, logged, or included in error messages
"""
import base64
import copy
import logging
import os
import re
import sys
import threading

from backend import config
from backend.services.util import JsonFile, now_iso, redact

log = logging.getLogger("aivision.cameras")

SUPPORTED_TYPES = {"rtsp", "webcam", "file"}   # what the existing OpenCV reader can open
ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
STATUS_FIELDS = ("status", "lastConnectedAt", "lastError")


# ------------------------------------------------------------------ DPAPI
def _dpapi(data, protect):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    inp = BLOB(len(data), buf)
    out = BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if protect else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out)):   # CRYPTPROTECT_UI_FORBIDDEN
        raise OSError("DPAPI call failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def _protect(secret):
    if sys.platform == "win32":
        return {"scheme": "dpapi", "value": base64.b64encode(_dpapi(secret.encode("utf-8"), True)).decode()}
    return {"scheme": "plain", "value": secret}


def _unprotect(rec):
    if not rec:
        return ""
    if rec.get("scheme") == "dpapi":
        return _dpapi(base64.b64decode(rec["value"]), False).decode("utf-8")
    return rec.get("value", "")


# ------------------------------------------------------------------ URL helpers
def split_rtsp(url):
    """Return (scheme, user, password, hostport, path) without percent-decoding.

    Passwords may contain '@' (e.g. the existing camera's password) - the host starts
    after the LAST '@' of the authority, which is how FFmpeg parses it too.
    """
    m = re.match(r"^(rtsps?)://(.*)$", url.strip(), re.I)
    if not m:
        raise ValueError("RTSP URL must start with rtsp:// or rtsps://")
    scheme, rest = m.group(1).lower(), m.group(2)
    slash = rest.find("/", rest.rfind("@") + 1 if "@" in rest else 0)
    authority, path = (rest, "") if slash < 0 else (rest[:slash], rest[slash:])
    user = pw = None
    if "@" in authority:
        userinfo, hostport = authority.rsplit("@", 1)
        user, _, pw = userinfo.partition(":")
    else:
        hostport = authority
    if not hostport:
        raise ValueError("RTSP URL has no host")
    return scheme, user, pw, hostport, path


def host_port(hostport, default_port=554):
    if hostport.startswith("["):
        host, _, rest = hostport[1:].partition("]")
        port = rest[1:] if rest.startswith(":") else ""
    else:
        host, _, port = hostport.partition(":")
    if port and not port.isdigit():
        raise ValueError("invalid port")
    p = int(port) if port else default_port
    if not 1 <= p <= 65535:
        raise ValueError("port out of range")
    return host, p


def apply_stream_profile(path, profile, custom):
    """main = URL as configured; sub = derived for known Hikvision/Dahua patterns; custom = given path."""
    if profile == "custom":
        if not custom:
            raise ValueError("custom stream profile selected but no custom stream path given")
        return custom if custom.startswith("/") else "/" + custom
    if profile != "sub":
        return path
    for pat, repl in (
        (r"(?i)/h264/ch(\d+)/main/av_stream", r"/h264/ch\1/sub/av_stream"),        # Hikvision (existing camera)
        (r"(?i)(/Streaming/Channels/\d+)01\b", r"\g<1>02"),                         # Hikvision ISAPI style
        (r"(?i)subtype=0", "subtype=1"),                                            # Dahua
    ):
        new, n = re.subn(pat, repl, path)
        if n:
            return new
    raise ValueError("cannot derive a substream for this URL - enter the substream URL directly or use a custom stream path")


AI_STREAMS = ("same", "sub", "custom")


# ------------------------------------------------------------------ registry
class CameraRegistry:
    def __init__(self):
        self.cams = JsonFile(str(config.DATA_DIR / "cameras.json"), {"cameras": []})
        self.creds = JsonFile(str(config.DATA_DIR / "credentials.json"), {})
        self.lock = threading.RLock()
        # "online" from a previous run is stale until a session or test confirms it again.
        stale = False
        for c in self.cams.data.get("cameras", []):
            if c.get("status") == "online":
                c["status"] = "unverified"
                stale = True
        # one-time upgrade (2026-10-23): far zoom for people and vehicles and the animal filter switched on for
        # cameras saved before these options existed (their stored "personFar": false was the old default).
        # Done once per data folder (data/migrations.json), so a user who switches them off later keeps that.
        mig = JsonFile(str(config.DATA_DIR / "migrations.json"), {})
        if not mig.data.get("farZoomAnimalFilter"):
            for c in self.cams.data.get("cameras", []):
                mods = (c.get("detection") or {}).get("modules") or {}
                h = (mods.get("human") or {}).get("classes")
                if isinstance(h, dict):
                    h["personFar"], h["animalFilter"] = True, True
                v = (mods.get("vehicle") or {}).get("classes")
                if isinstance(v, dict):
                    v["vehicleFar"] = True
                stale = True
            mig.data["farZoomAnimalFilter"] = True
            mig.save()
        if stale:
            self.cams.save()
        if sys.platform != "win32":
            log.warning("Windows DPAPI unavailable on this OS - camera passwords are stored with owner-only file permissions only")

    # -- read
    def list(self):
        with self.lock:
            return [self.public(c) for c in self.cams.data["cameras"]]

    def get(self, cam_id):
        with self.lock:
            c = self._find(cam_id)
            return copy.deepcopy(c) if c else None

    def public(self, cam):
        c = copy.deepcopy(cam)
        c.pop("password", None)
        c["sourceUrl"] = redact(c.get("sourceUrl", ""))
        c["hasCredentials"] = bool(self.creds.data.get(c["id"], {}).get("password"))
        if c["hasCredentials"] and self.credentials_unreadable(c["id"]):
            c["hasCredentials"] = False
            c["lastError"] = "Saved password cannot be decrypted on this computer - edit the camera and re-enter it"
        c["compatible"] = c.get("type") in SUPPORTED_TYPES
        if not c["compatible"]:
            c["incompatibleReason"] = "The existing detection pipeline reads streams with OpenCV; ONVIF cameras must be added with their RTSP URL."
        c["source"] = "backend"
        return c

    def _find(self, cam_id):
        return next((c for c in self.cams.data["cameras"] if c.get("id") == cam_id), None)

    # -- write
    def validate(self, body, existing_id=None):
        errs = []
        cid = body.get("id")
        if not isinstance(cid, str) or not ID_RE.match(cid):
            errs.append("id must be 2-32 characters: letters, digits, '-' or '_'")
        elif existing_id is None and self._find(cid):
            errs.append(f"camera {cid} already exists")
        elif existing_id is not None and cid != existing_id:
            errs.append("camera id cannot be changed")
        name = (body.get("name") or "").strip()
        if not name:
            errs.append("name is required")
        elif any(c["id"] != (existing_id or cid) and c.get("name", "").strip().lower() == name.lower() for c in self.cams.data["cameras"]):
            errs.append("another camera already uses this name")
        ctype = body.get("type")
        if ctype not in ("rtsp", "onvif", "webcam", "file"):
            errs.append("type must be rtsp, onvif, webcam or file")
        src = (body.get("sourceUrl") or "").strip()
        if not src:
            errs.append("sourceUrl is required")
        elif ctype == "rtsp":
            try:
                _, _, _, hp, _ = split_rtsp(src)
                host_port(hp)
            except ValueError as e:
                errs.append(f"sourceUrl: {e}")
        elif ctype == "webcam" and not (src.isdigit() or src.startswith("/dev/video") or src.startswith("video=")):
            errs.append("webcam source must be a device index, /dev/videoN or video=<name>")
        # AI detection stream (optional): "same" = the viewing stream (default), "sub" = the camera's substream
        # derived only for known vendor patterns, "custom" = a path entered by the user. Never guessed.
        ai = body.get("aiStream", "same") or "same"
        if ai not in AI_STREAMS:
            errs.append("aiStream must be same, sub or custom")
        elif ai != "same":
            if ctype != "rtsp":
                errs.append("a separate AI stream is only possible for RTSP cameras")
            elif ai == "custom" and not (body.get("aiCustomStream") or "").strip():
                errs.append("enter the AI stream path (e.g. /Streaming/Channels/102)")
            elif ai == "sub" and src and not errs:
                try:
                    apply_stream_profile(split_rtsp(src)[4], "sub", "")
                except ValueError:
                    errs.append("cannot derive the substream from this URL - choose 'Custom' and enter the substream path")
        if (body.get("snapshotSource") or "main") not in ("main", "ai"):
            errs.append("snapshotSource must be main or ai")
        return errs

    def save(self, body, existing_id=None):
        """Create or update. Returns public camera. Password handled separately."""
        with self.lock:
            body = copy.deepcopy(body)
            password = body.pop("password", None)
            for k in ("hasCredentials", "compatible", "incompatibleReason", "source", "image", "demo"):
                body.pop(k, None)
            src = (body.get("sourceUrl") or "").strip()
            username = (body.get("username") or "").strip()
            if body.get("type") == "rtsp" and src:
                scheme, user, pw, hp, path = split_rtsp(src)
                if user is not None:          # credentials typed into the URL: move them out of the URL
                    username = username or user
                    if password is None and pw:
                        password = pw
                src = f"{scheme}://{hp}{path}"
            body["sourceUrl"] = src
            body["username"] = username
            old = self._find(existing_id or body["id"])
            if old:
                for f in STATUS_FIELDS:
                    body.setdefault(f, old.get(f))
                body["createdAt"] = old.get("createdAt") or now_iso()
                self.cams.data["cameras"] = [body if c["id"] == old["id"] else c for c in self.cams.data["cameras"]]
                if old.get("sourceUrl") != src or old.get("username") != username or old.get("type") != body.get("type"):
                    body["status"] = "unverified"
                    body["lastError"] = None
            else:
                body["createdAt"] = now_iso()
                body["status"] = "unverified"
                body["lastError"] = None
                body["lastConnectedAt"] = None
                self.cams.data["cameras"].append(body)
            body["updatedAt"] = now_iso()
            if password:      # empty / missing = keep the existing password
                self.creds.data[body["id"]] = {"username": username, "password": _protect(password)}
            elif not username:
                self.creds.data.pop(body["id"], None)
            elif body["id"] in self.creds.data:
                self.creds.data[body["id"]]["username"] = username
            self.cams.save()
            self._save_creds()
            return self.public(body)

    def _save_creds(self):
        self.creds.save()
        try:
            os.chmod(self.creds.path, 0o600)
        except OSError:
            pass

    def delete(self, cam_id):
        with self.lock:
            before = len(self.cams.data["cameras"])
            self.cams.data["cameras"] = [c for c in self.cams.data["cameras"] if c["id"] != cam_id]
            if len(self.cams.data["cameras"]) == before:
                return False
            self.creds.data.pop(cam_id, None)
            self.cams.save()
            self._save_creds()
            return True

    def update_status(self, cam_id, **fields):
        with self.lock:
            c = self._find(cam_id)
            if not c:
                return
            changed = any(c.get(k) != v for k, v in fields.items())
            c.update(fields)
            if changed:
                self.cams.save()

    def update_module_config(self, cam_id, module_id, cfg):
        with self.lock:
            c = self._find(cam_id)
            if not c:
                raise KeyError(cam_id)
            c.setdefault("detection", {}).setdefault("modules", {})[module_id] = cfg
            c["updatedAt"] = now_iso()
            self.cams.save()
            return copy.deepcopy(cfg)

    def resolve_ai_source(self, cam):
        """(source for the AI detection stream, kind). kind "same" = identical to the viewing stream."""
        kind = cam.get("aiStream") or "same"
        if kind == "same" or cam.get("type") != "rtsp":
            return self.resolve_source(cam), "same"
        alt = dict(cam, streamProfile=kind, customStream=(cam.get("aiCustomStream") or "").strip())
        src = self.resolve_source(alt)
        return src, ("same" if src == self.resolve_source(cam) else kind)

    # -- source resolution (credentials stay inside the backend)
    def resolve_source(self, cam, password_override=None):
        """Return the value passed to cv2.VideoCapture. Raises ValueError with a safe message."""
        ctype = cam.get("type")
        src = (cam.get("sourceUrl") or "").strip()
        if ctype == "onvif":
            raise ValueError("ONVIF cameras are not supported by the existing pipeline - add the camera with its RTSP URL")
        if ctype == "webcam":
            return int(src) if src.isdigit() else src
        if ctype == "file":
            return src
        if ctype != "rtsp":
            raise ValueError(f"unsupported camera type '{ctype}'")
        scheme, url_user, url_pw, hp, path = split_rtsp(src)
        path = apply_stream_profile(path, cam.get("streamProfile", "main"), (cam.get("customStream") or "").strip())
        username = (cam.get("username") or url_user or "").strip()
        if not password_override and cam.get("requiresAuth") is not False and self.credentials_unreadable(cam.get("id")):
            raise ValueError("The saved password was encrypted on another computer or Windows account and cannot be "
                             "decrypted here - edit the camera and enter the password again")
        password = password_override if password_override else self.password(cam.get("id")) or (url_pw or "")
        if cam.get("requiresAuth") is False:
            username, password = "", ""
        auth = f"{username}:{password}@" if username else ""
        return f"{scheme}://{auth}{hp}{path}"

    def credentials_unreadable(self, cam_id):
        """True when a password is stored but cannot be decrypted (DPAPI blob from another PC/user)."""
        rec = self.creds.data.get(cam_id or "", {}).get("password")
        if not rec:
            return False
        try:
            _unprotect(rec)
            return False
        except (OSError, ValueError, AttributeError):
            return True

    def password(self, cam_id):
        rec = self.creds.data.get(cam_id or "", {}).get("password")
        try:
            return _unprotect(rec) if rec else ""
        except (OSError, ValueError, AttributeError):
            log.error("could not decrypt stored credentials for %s (stored by another Windows user or computer?)", cam_id)
            return ""
