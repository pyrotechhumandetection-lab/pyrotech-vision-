"""LatestFrameReader from the original human_detect_yolo-2.py.

Keeps only the newest frame so live RTSP never lags behind real time (frames the
detector is too slow to reach are dropped - documented trade-off of the original).

Additions for the API (behaviour of the read/reconnect loop is unchanged):
  * open timeout, so a dead camera does not block startup forever
  * counters (frames read, disconnections) and last-frame timestamp for status/metrics
  * a sequence number so consumers can tell a new frame from the same one
  * the capture time of the newest frame (snapshots / events carry the time the frame was received)
  * the stream's own frame rate and resolution (reported next to the measured rate)
  * optional FIFO queue (enable_queue): every decoded frame is queued for the detection loop instead of
    only the newest one. The queue is bounded (frames and memory); when it is full the OLDEST queued
    frame is discarded and counted (queue_dropped) - an explicit, measured overload policy.
  * gaps in the stream's own timestamps (frames the camera / network never delivered) are counted
    separately (camera_gaps), so application-side and camera-side frame loss can be told apart.
"""
import logging
import threading
import time
from collections import deque

import cv2

from backend.runtime import name_thread

log = logging.getLogger("aivision.reader")


class LatestFrameReader:
    def __init__(self, source, open_timeout_sec=10.0, tag=None):
        self.source = source
        self.tag = tag               # OS thread name of the reader (perf monitor), e.g. "rd:CAM-01"
        self.read_cpu_ms = None      # CPU time of this reader thread per frame (demux + conversion; EMA)
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
        self.queue = None            # deque of (seq, frame, capture time) when enable_queue() was called
        self.queue_cap = 0
        self.queue_dropped = 0       # frames discarded because the queue was full (application-side loss)
        self.queue_peak = 0
        self.qcond = threading.Condition(self.lock)
        self.camera_gaps = 0         # frames missing in the stream's timestamps (camera / network side)
        self._last_pts = None
        self.cap = self._open()
        self.thread = threading.Thread(target=self._update, name=tag or "frame-reader", daemon=True)
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
        if self.tag:
            name_thread(self.tag)
        while not self.stopped:
            if not self.cap.isOpened():
                time.sleep(0.5)
                if self.stopped:
                    break
                self.cap = self._open()
                continue
            c0 = time.thread_time()
            ok, frame = self.cap.read()
            if ok:
                c = (time.thread_time() - c0) * 1000.0
                self.read_cpu_ms = c if self.read_cpu_ms is None else 0.9 * self.read_cpu_ms + 0.1 * c
            pts = None
            if ok:
                try:
                    pts = float(self.cap.get(cv2.CAP_PROP_POS_MSEC))
                except Exception:
                    pts = None
            with self.lock:
                was_ok = self.ok
                self.ok = ok
                if ok:
                    self.latest_frame = frame
                    self.seq += 1
                    self.frames_read += 1
                    self.last_frame_time = self.latest_time = time.time()
                    self._count_gap(pts)
                    if self.queue is not None:
                        if len(self.queue) >= self.queue_cap:
                            self.queue.popleft()          # overload policy: drop the OLDEST, count it
                            self.queue_dropped += 1
                        self.queue.append((self.seq, frame, self.latest_time))
                        self.queue_peak = max(self.queue_peak, len(self.queue))
                        self.qcond.notify()
                else:
                    self._last_pts = None
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

    def _count_gap(self, pts):
        """Frames missing between two consecutive stream timestamps (camera / network side)."""
        if pts is None or pts <= 0 or not self.nominal_fps:
            self._last_pts = pts if pts and pts > 0 else None
            return
        if self._last_pts is not None:
            interval = 1000.0 / self.nominal_fps
            gap = pts - self._last_pts
            if interval * 1.5 < gap < 5000:            # > 5 s or backwards: a reconnect / timestamp reset
                self.camera_gaps += int(round(gap / interval)) - 1
        self._last_pts = pts

    # ---------------------------------------------------------------- FIFO queue (detection loop)
    def enable_queue(self, capacity):
        with self.lock:
            self.queue_cap = max(2, int(capacity))
            if self.queue is None:
                self.queue = deque()

    def next_frame(self, timeout=0.2):
        """Oldest queued (seq, frame, capture time) or None. The frame is shared with the reader - never
        draw on it (copy first)."""
        with self.qcond:
            if not self.queue:
                self.qcond.wait(timeout)
            return self.queue.popleft() if self.queue else None

    def queue_len(self):
        with self.lock:
            return len(self.queue) if self.queue is not None else 0

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
        with self.qcond:
            self.qcond.notify_all()
        self.thread.join(timeout=4)
        try:
            self.cap.release()
        except Exception:
            pass
