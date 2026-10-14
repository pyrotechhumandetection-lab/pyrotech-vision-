"""Backend configuration.

Precedence: real environment variables  >  backend/.env  >  <repo>/.env  >  defaults below.
Relative paths in variables are resolved against the backend/ folder.
"""
import logging
import os
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent          # <repo>/backend
REPO_ROOT = BACKEND_DIR.parent                          # <repo>  (frontend lives here)
ROOT = BACKEND_DIR                                      # base for relative paths


def _load_dotenv(path):
    """Minimal KEY=VALUE reader (no extra dependency). Existing environment values win."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                os.environ.setdefault(k, v)
    except FileNotFoundError:
        pass


_load_dotenv(BACKEND_DIR / ".env")
_load_dotenv(REPO_ROOT / ".env")

# CPU thread settings that the OpenMP runtime reads only when it starts (before torch is imported).
# The other CPU settings (torch threads, inference concurrency, affinity) are applied by
# backend/runtime.apply() at start-up - see that file.
from backend import runtime as _runtime  # noqa: E402

_runtime.early_env()

# Self-contained runtime (applied before Ultralytics is imported anywhere): no connectivity probe,
# no asset downloads, no automatic pip installs, settings file kept inside backend/.
# .env / environment can override (YOLO_OFFLINE=0, YOLO_AUTOINSTALL=1).
os.environ.setdefault("YOLO_OFFLINE", "1")
os.environ.setdefault("YOLO_AUTOINSTALL", "0")
os.environ.setdefault("YOLO_CONFIG_DIR", str(BACKEND_DIR / ".ultralytics"))
try:  # Ultralytics falls back to a temp folder if the directory does not exist yet
    Path(os.environ["YOLO_CONFIG_DIR"]).mkdir(parents=True, exist_ok=True)
except OSError:
    pass


def _env(name, default):
    v = os.environ.get(name, "")
    return v if v != "" else default


def _path(name, default):
    p = Path(_env(name, str(default)))
    return p if p.is_absolute() else (ROOT / p)


# --- API server ---
HOST = _env("AIV_HOST", "127.0.0.1")
PORT = int(_env("AIV_PORT", "8000"))
API_PREFIX = "/api"
# Browser origins allowed to call the API directly. Not needed when the frontend
# (npm start) proxies /api to the backend, which is the default setup.
CORS_ORIGINS = [o.strip() for o in _env("AIV_CORS_ORIGINS", "").split(",") if o.strip()]


# --- storage ---
def _writable(path):
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write-test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def _data_dir():
    """backend/data unless AIV_DATA_DIR is set; per-user folder if backend/ is read-only."""
    if os.environ.get("AIV_DATA_DIR"):
        return _path("AIV_DATA_DIR", "data")
    preferred = BACKEND_DIR / "data"
    if _writable(preferred):
        return preferred
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    return Path(base) / "AIVISION" / "data"


DATA_DIR = _data_dir()
# Snapshot images: backend/snapshots/{human|vehicle}_detection/YYYY/MM/DD/... (and manual/)
SNAPSHOT_DIR = _path("AIV_SNAPSHOT_DIR", "snapshots")
if not os.environ.get("AIV_SNAPSHOT_DIR") and not _writable(SNAPSHOT_DIR):
    SNAPSHOT_DIR = DATA_DIR / "snapshots"
MAX_EVENT_RECORDS = int(_env("AIV_MAX_EVENT_RECORDS", "5000"))

# --- model (same defaults as the original human_detect_yolo-2.py) ---
MODEL_PATH = _path("AIV_MODEL", "models/yolov8n.pt")
TRACKER_CONFIG = _path("AIV_TRACKER_CONFIG", "models/bytetrack_stable.yaml")
IMG_SIZE = int(_env("AIV_IMG_SIZE", "640"))
_dev = _env("AIV_DEVICE", "")
DEVICE = None if _dev == "" else (int(_dev) if _dev.isdigit() else _dev)   # None = auto, "cpu", 0 = first CUDA GPU

# Inference engine for the SAME yolov8n weights:
#   auto     - OpenVINO when the openvino package and models/yolov8n_openvino_model exist, else PyTorch
#   openvino - OpenVINO (falls back to PyTorch with a warning if it cannot be loaded)
#   pytorch  - original PyTorch .pt model
# The OpenVINO folder is an FP32, dynamic-shape export of models/yolov8n.pt (identical detections,
# several times faster on CPU). A custom AIV_MODEL .pt is always run with PyTorch.
ENGINE = _env("AIV_ENGINE", "auto").lower()
OPENVINO_MODEL_PATH = _path("AIV_OPENVINO_MODEL", "models/yolov8n_openvino_model")


def openvino_available():
    try:
        import openvino  # noqa: F401
        return True
    except Exception:
        return False


def resolve_engine():
    """(engine name, model path) actually used for inference."""
    custom_pt = MODEL_PATH.name.lower() != "yolov8n.pt"
    if DEVICE not in (None, "cpu"):        # an explicit CUDA device means the PyTorch GPU build
        return "pytorch", MODEL_PATH
    want_ov = ENGINE == "openvino" or (ENGINE == "auto" and not custom_pt)
    if want_ov and (OPENVINO_MODEL_PATH / "yolov8n.xml").is_file() and openvino_available():
        return "openvino", OPENVINO_MODEL_PATH
    return "pytorch", MODEL_PATH

# Human Head Count (still-frame head detector in models/head_yolov8m, see detection/head_count.py):
#   auto / openvino - OpenVINO FP16 when the openvino package is installed, else PyTorch;  pytorch - always PyTorch
HEAD_ENGINE = _env("AIV_HEAD_ENGINE", "auto").lower()
# Frames from a running session are used for Capture & Count when they are at most this old (seconds);
# otherwise a fresh frame is read from the camera.
HEAD_MAX_FRAME_AGE_SEC = float(_env("AIV_HEAD_MAX_FRAME_AGE", "2"))

# Human Detection and Human Count on the SAME camera: one person-detection pass of the shared yolov8n
# weights serves both (the Human Count module used to run its own second pass on the same frame).
# Detections are identical (same model, input and NMS; each module still applies its own thresholds and
# tracker - see detection/pipeline.py). 0 = two separate passes as before.
SHARED_PERSON_INFERENCE = _env("AIV_SHARED_PERSON_INFERENCE", "1").strip() not in ("0", "false", "no", "off")

# --- camera / stream ---
CONNECT_TIMEOUT_SEC = float(_env("AIV_CONNECT_TIMEOUT", "10"))

# Frames from the camera to the detection loop (per camera):
#   queue  - every decoded frame is queued (FIFO) and analysed in order. The queue holds at most
#            AIV_FRAME_QUEUE_SECONDS of video and AIV_FRAME_QUEUE_MAX_MB of memory; when it is full the
#            OLDEST frame is discarded and counted ("dropped (overload)") - nothing is lost silently.
#   latest - the original behaviour: always the newest frame (frames the AI is too slow for are skipped).
FRAME_POLICY = str(_env("AIV_FRAME_POLICY", "queue")).strip().lower()
FRAME_QUEUE_SECONDS = max(0.1, float(_env("AIV_FRAME_QUEUE_SECONDS", "0.5")))
FRAME_QUEUE_MAX_MB = max(8.0, float(_env("AIV_FRAME_QUEUE_MAX_MB", "96")))
STREAM_JPEG_QUALITY = int(_env("AIV_STREAM_JPEG_QUALITY", "75"))
STREAM_MAX_WIDTH = int(_env("AIV_STREAM_MAX_WIDTH", "1280"))
STREAM_MAX_FPS = float(_env("AIV_STREAM_MAX_FPS", "15"))
PREVIEW_IDLE_RELEASE_SEC = float(_env("AIV_PREVIEW_IDLE_RELEASE", "10"))

# --- events ---
DEFAULT_EVENT_COOLDOWN_SEC = float(_env("AIV_EVENT_COOLDOWN", "30"))
# Vehicle snapshots: one per confirmed vehicle (deduplicated by track), so the default cooldown is 0.
DEFAULT_SNAPSHOT_COOLDOWN_SEC = float(_env("AIV_SNAPSHOT_COOLDOWN", "0"))

LOG_LEVEL = _env("AIV_LOG_LEVEL", "INFO").upper()


def model_label():
    """Model shown in the UI: the yolov8n weights + the engine running them."""
    engine, _ = resolve_engine()
    return f"{MODEL_PATH.name} (OpenVINO)" if engine == "openvino" else MODEL_PATH.name


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
