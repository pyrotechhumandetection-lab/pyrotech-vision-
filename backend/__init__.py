"""PYROTECH VISION backend - Human + Vehicle detection API and processing.

Layout:
    main.py                 entry point (python backend/main.py)
    config.py               settings (.env / environment variables, paths)
    detection/              the existing detection logic
        pipeline.py         person + vehicle algorithm from human_detect_yolo-2.py (YOLOv8 + ByteTrack)
        frame_reader.py     LatestFrameReader from the original script (RTSP capture)
        modules.py          Human Detection / Vehicle Detection module definitions
        rtsp_probe.py       camera connection test
    services/               cameras + credentials, shared per-camera sessions, events/snapshots/alerts
    api/server.py           HTTP API + MJPEG live stream (Python standard library)
    models/                 yolov8n.pt + bytetrack_stable.yaml
    snapshots/              saved detection images
    data/                   cameras, credentials (DPAPI), alerts, detections, snapshot index
"""
import os

# From the original script: force RTSP over TCP (UDP drops/reorders packets and causes
# stalls). MUST be set before cv2 is imported anywhere, which is why it lives here.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp|stimeout;5000000|max_delay;500000")

os.environ.setdefault("OPENCV_LOG_LEVEL", "ERROR")   # keep the console readable (reconnect warnings)


__version__ = "1.0.0"
