"""Real camera connection test.

1. TCP connect to the RTSP host/port                    -> UNREACHABLE / TIMEOUT
2. RTSP DESCRIBE (Basic or Digest auth, stdlib only)     -> AUTH_FAILED / STREAM_UNAVAILABLE
3. Open with OpenCV (same reader settings as the pipeline) and decode one frame
                                                         -> UNSUPPORTED_FORMAT / STREAM_UNAVAILABLE / OK
A TCP connection alone is never reported as success; OK requires a decoded frame.
"""
import base64
import hashlib
import logging
import re
import socket
import threading
import time

import cv2

from backend.services.cameras import host_port, split_rtsp
from backend.services.util import redact

log = logging.getLogger("aivision.probe")


def _result(ok, code, message, **extra):
    d = {"ok": ok, "success": ok, "code": code, "message": redact(message)}
    d.update(extra)
    return d


def _recv_response(sock, timeout):
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > 65536:
            break
    head, _, body = data.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    m = re.match(r"RTSP/\d\.\d\s+(\d{3})", lines[0] if lines else "")
    status = int(m.group(1)) if m else 0
    headers = {}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        headers.setdefault(k.strip().lower(), []).append(v.strip())
    length = int((headers.get("content-length") or ["0"])[0] or 0)
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        body += chunk
    return status, headers, body.decode("latin-1", "replace")


def _auth_header(www, method, uri, user, pw):
    """Build Authorization for the first supported challenge (Digest preferred)."""
    for ch in www:
        if ch.lower().startswith("digest"):
            params = dict(re.findall(r'(\w+)="?([^",]*)"?', ch))
            realm, nonce = params.get("realm", ""), params.get("nonce", "")
            ha1 = hashlib.md5(f"{user}:{realm}:{pw}".encode()).hexdigest()
            ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
            if "auth" in params.get("qop", ""):
                nc, cnonce = "00000001", hashlib.md5(str(time.time()).encode()).hexdigest()[:16]
                resp = hashlib.md5(f"{ha1}:{nonce}:{nc}:{cnonce}:auth:{ha2}".encode()).hexdigest()
                return (f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", '
                        f'response="{resp}", qop=auth, nc={nc}, cnonce="{cnonce}"')
            resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
            return f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}", response="{resp}"'
    for ch in www:
        if ch.lower().startswith("basic"):
            return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()
    return None


def rtsp_describe(source_url, timeout):
    """Returns (code or None if OK, message, codec)."""
    scheme, user, pw, hp, path = split_rtsp(source_url)
    host, port = host_port(hp)
    uri = f"rtsp://{hp}{path or '/'}"
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except socket.timeout:
        return "TIMEOUT", f"No response from {host}:{port} within {timeout:.0f} s", None
    except OSError as e:
        return "UNREACHABLE", f"Cannot connect to {host}:{port} ({e.strerror or e})", None
    if scheme == "rtsps":
        sock.close()
        return None, "TLS RTSP - DESCRIBE skipped", None
    try:
        def describe(cseq, auth=None):
            req = f"DESCRIBE {uri} RTSP/1.0\r\nCSeq: {cseq}\r\nAccept: application/sdp\r\nUser-Agent: AIVISION-probe\r\n"
            if auth:
                req += f"Authorization: {auth}\r\n"
            sock.sendall((req + "\r\n").encode("latin-1"))
            return _recv_response(sock, timeout)

        status, headers, body = describe(1)
        if status == 401:
            if not user:
                return "AUTH_FAILED", "The camera requires authentication but no username/password is configured", None
            auth = _auth_header(headers.get("www-authenticate", []), "DESCRIBE", uri, user, pw or "")
            if not auth:
                return None, "unknown auth scheme - continuing with decode test", None
            status, headers, body = describe(2, auth)
            if status == 401:
                return "AUTH_FAILED", "The camera rejected the username or password (401)", None
        if status in (404, 454):
            return "STREAM_UNAVAILABLE", f"The camera has no stream at path '{path or '/'}' ({status})", None
        if status == 0:
            return None, "non-RTSP reply - continuing with decode test", None
        if status != 200:
            return "STREAM_UNAVAILABLE", f"The camera answered DESCRIBE with status {status}", None
        codec = None
        m = re.search(r"a=rtpmap:\d+\s+([A-Za-z0-9-]+)/", body)
        if m:
            codec = m.group(1).upper()
        return None, "DESCRIBE OK", codec
    except socket.timeout:
        return "TIMEOUT", "The camera accepted the connection but did not answer RTSP DESCRIBE", None
    except OSError as e:
        return "UNREACHABLE", f"Connection dropped during RTSP handshake ({e})", None
    finally:
        sock.close()


def grab_frame(source, timeout):
    """Open with OpenCV and decode one frame, bounded by timeout. Returns frame or None."""
    box = {}

    def work():
        try:
            if isinstance(source, int):
                cap = cv2.VideoCapture(source)
            else:
                ms = int(timeout * 1000)
                try:
                    cap = cv2.VideoCapture(source, cv2.CAP_FFMPEG, [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, ms])
                except Exception:
                    cap = cv2.VideoCapture(source)
            box["opened"] = cap.isOpened()
            end = time.time() + timeout
            while cap.isOpened() and time.time() < end:
                ok, frame = cap.read()
                if ok and frame is not None:
                    box["frame"] = frame
                    box["fps"] = cap.get(cv2.CAP_PROP_FPS) or None
                    break
            cap.release()
        except Exception as e:   # never let OpenCV errors escape with credentials
            box["error"] = redact(e)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout + 3)
    return box


def frame_to_data_url(frame, max_width=1280, quality=80):
    h, w = frame.shape[:2]
    if w > max_width:
        frame = cv2.resize(frame, (max_width, int(h * max_width / w)))
    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode() if ok else None


def test_source(source, timeout):
    codec = None
    t0 = time.time()
    if isinstance(source, str) and source.lower().startswith(("rtsp://", "rtsps://")):
        code, msg, codec = rtsp_describe(source, timeout)
        if code:
            return _result(False, code, msg)
    box = grab_frame(source, timeout)
    if "frame" not in box:
        if box.get("error"):
            return _result(False, "UNKNOWN", box["error"])
        if not box.get("opened"):
            if isinstance(source, str) and not source.lower().startswith(("rtsp", "http")):
                return _result(False, "STREAM_UNAVAILABLE", "The file or device could not be opened")
            return _result(False, "STREAM_UNAVAILABLE", "OpenCV/FFmpeg could not open the stream")
        if codec in ("H265", "HEVC"):
            return _result(False, "UNSUPPORTED_FORMAT", "Stream opened but no H.265 frame could be decoded - set the camera to H.264")
        return _result(False, "UNSUPPORTED_FORMAT", "Stream opened but no frame could be decoded within the timeout")
    frame = box["frame"]
    h, w = frame.shape[:2]
    return _result(True, "OK", "Frame decoded successfully", frame=frame_to_data_url(frame), width=w, height=h,
                   codec=codec, fps=round(box["fps"], 1) if box.get("fps") else None, latencyMs=int((time.time() - t0) * 1000))
