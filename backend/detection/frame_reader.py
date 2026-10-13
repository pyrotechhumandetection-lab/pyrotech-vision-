"""LatestFrameReader from the original human_detect_yolo-2.py.

Keeps only the newest frame so live RTSP never lags behind real time (frames the
detector is too slow to reach are dropped - documented trade-off of the original).

Additions for the API (behaviour of the read/reconnect loop is unchanged):
  * open timeout, so a dead camera does not block startup forever
  * counters (frames read, disconnections) and last-frame timestamp for status/metrics
  * a sequence number so consumers can tell a new frame from the same one
  * the capture time of the newest frame (snapshots / events carry the time the frame was received)
  * the stream's own frame rate and resolution (reported next to the measured rate)
"""
import logging
import threading
import time

import cv2

log = logging.getLogger("aivision.reader")


class LatestFrameReader:
    def __init__(self, source, open_timeout_sec=10.0):
        self.source = source
        self.open_timeout_ms = int(open_timeout_sec * 1000)
        self.lock = threading.Lock()
        self.latest_frame = None
        self.ok = False
        self.stopped = False
        self.seq = 0
        self.frames_read = 0
        self.disconnects = 0
        self.last_frame_time = 0.0
        self.latest_time = 0.0       # capture time of latest_frame
        self.nominal_fps = None      # frame rate announced by the stream (may be absent/wrong)
        self.width = self.height = None
        self.opened_once = False
        self.cap = self._open()
        self.thread = threading.Thread(target=self._update, name="frame-reader", daemon=True)
        self.thread.start()

    def _open(self):
        if isinstance(self.source, int):
            cap = cv2.VideoCapture(self.source)
        else:
            params = [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, self.open_timeout_ms, cv2.CAP_PROP_READ_TIMEOUT_MSEC, self.open_timeout_ms]
            try:
                cap = cv2.VideoCapture(self.source, cv2.CAP_FFMPEG, params)
            except Exception:  # older OpenCV without the params overload
                cap = cv2.VideoCapture(self.source)
        if isinstance(self.source, str) and self.source.lower().startswith(("rtsp://", "rtsps://", "http://", "https://")):
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        if cap.isOpened():
            self.opened_once = True
            try:
                fps = float(cap.get(cv2.CAP_PROP_FPS) or 0)
                self.nominal_fps = round(fps, 2) if 0 < fps < 241 else None
                self.width, self.height = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or None, int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or None
            except Exception:
                pass
        return cap

    def _update(self):
        while not self.stopped:
            if not self.cap.isOpened():
                time.sleep(0.5)
                if self.stopped:
                    break
                self.cap = self._open()
                continue
            ok, frame = self.cap.read()
            with self.lock:
                was_ok = self.ok
                self.ok = ok
                if ok:
                    self.latest_frame = frame
                    self.seq += 1
                    self.frames_read += 1
                    self.last_frame_time = self.latest_time = time.time()
            if not ok:
                if was_ok:
                    self.disconnects += 1
                    log.warning("stream read failed - reconnecting")
                self.cap.release()
                # original: sleep 2 s then reopen; sliced so stop() is responsive
                for _ in range(20):
                    if self.stopped:
                        break
                    time.sleep(0.1)
                if not self.stopped:
                    self.cap = self._open()

    def read(self):
        with self.lock:
            return self.ok, (None if self.latest_frame is None else self.latest_frame.copy())

    def read_with_seq(self):
        with self.lock:
            return self.ok, self.seq, (None if self.latest_frame is None else self.latest_frame.copy())

    def read_latest(self):
        """(ok, seq, frame copy, capture time) of the newest decoded frame."""
        with self.lock:
            return self.ok, self.seq, (None if self.latest_frame is None else self.latest_frame.copy()), self.latest_time

    def isOpened(self):
        return self.cap.isOpened()

    def get(self, prop):
        return self.cap.get(prop)

    def wait_first_frame(self, timeout_sec):
        """Block until a frame has been decoded (True) or timeout (False)."""
        end = time.time() + timeout_sec
        while time.time() < end and not self.stopped:
            with self.lock:
                if self.latest_frame is not None:
                    return True
            time.sleep(0.05)
        return False

    def release(self):
        self.stopped = True
        self.thread.join(timeout=4)
        try:
            self.cap.release()
        except Exception:
            pass
