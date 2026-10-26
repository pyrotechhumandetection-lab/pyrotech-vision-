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


def _flag(name, default):
    v = str(_env(name, default)).strip().lower()
    return None if v in ("", "auto") else v not in ("0", "false", "no", "off")


# GPU tuning (PyTorch on CUDA only; ignored on CPU / OpenVINO):
#   AIV_HALF        FP16 inference. auto (default) = on when AIV_DEVICE is a CUDA device. Measure first:
#                   scripts/colab/bench_vehicle.py compares FP32/FP16 detections on your own footage.
#   AIV_NMS_IOU     NMS IoU threshold passed to the model (empty = Ultralytics default 0.7).
#   AIV_GPU_WARMUP  dummy passes at model load so the first camera frames do not pay CUDA/cuDNN start-up.
HALF = _flag("AIV_HALF", "auto")
_iou = str(_env("AIV_NMS_IOU", "")).strip()
NMS_IOU = float(_iou) if _iou else None
GPU_WARMUP = int(_env("AIV_GPU_WARMUP", "3"))


def use_half():
    """FP16 for the PyTorch model: only on a CUDA device."""
    cuda = DEVICE not in (None, "cpu") and str(DEVICE) != "mps"
    return bool(cuda and (HALF is None or HALF))

# Inference engine for the SAME yolov8n weights:
#   auto     - RKNN on a Rockchip NPU board (see RKNN_MODEL_PATH below), else OpenVINO when the openvino package and models/yolov8n_openvino_model exist, else PyTorch
#   openvino - OpenVINO (falls back to PyTorch with a warning if it cannot be loaded)
#   rknn     - Rockchip NPU (falls back to OpenVINO / PyTorch with a warning if it cannot be loaded)
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


# Rockchip NPU (RK3588 ...): RKNN export of the same yolov8n weights, run on the NPU (detection/rknn_runtime.py).
#   auto picks it first on a Rockchip board once ./aivision.sh npu setup has installed the runtime and its NPU test
#   (same detections as the CPU engine on sample images) has passed; AIV_ENGINE=rknn forces it without that test.
#   The RKNN model has a fixed input size (metadata.yaml imgsz, 640): the per-module imgSize setting is ignored.
RKNN_MODEL_PATH = _path("AIV_RKNN_MODEL", "models/yolov8n_rknn_model")
ENGINE_LABELS = {"rknn": "RKNN NPU", "openvino": "OpenVINO", "pytorch": "PyTorch"}


def engine_label(engine):
    return ENGINE_LABELS.get(engine, str(engine))


NPU_VERIFIED = BACKEND_DIR / "npu" / "verified"      # written by ./aivision.sh npu setup after its NPU test passed


def npu_verified():
    """The NPU test of ./aivision.sh npu setup passed for the RKNN model in use (needed for AIV_ENGINE=auto)."""
    try:
        return NPU_VERIFIED.read_text().strip() == RKNN_MODEL_PATH.name
    except OSError:
        return False


def rknn_available():
    if ENGINE != "rknn" and not npu_verified():      # auto: only after a passed NPU test
        return False
    has_model = any(RKNN_MODEL_PATH.glob("*.rknn")) if RKNN_MODEL_PATH.is_dir() else RKNN_MODEL_PATH.is_file()
    if not has_model:
        return False
    try:
        from backend.detection import rknn_runtime
        return rknn_runtime.available()[0]
    except Exception:
        return False


def rknn_img_size():
    try:
        from backend.detection import rknn_runtime
        return rknn_runtime.model_info(RKNN_MODEL_PATH)["imgsz"]
    except Exception:
        return 640


def fallback_engine(engine):
    """Next engine to try when `engine` cannot be loaded: rknn -> openvino (if present) -> pytorch."""
    if engine == "rknn" and (OPENVINO_MODEL_PATH / "yolov8n.xml").is_file() and openvino_available() \
            and ENGINE in ("auto", "rknn", "openvino"):
        return "openvino", OPENVINO_MODEL_PATH
    return "pytorch", MODEL_PATH


def resolve_engine():
    """(engine name, model path) actually used for inference."""
    custom_pt = MODEL_PATH.name.lower() != "yolov8n.pt"
    if DEVICE not in (None, "cpu"):        # an explicit CUDA device means the PyTorch GPU build
        return "pytorch", MODEL_PATH
    if (ENGINE == "rknn" or (ENGINE == "auto" and not custom_pt)) and rknn_available():
        return "rknn", RKNN_MODEL_PATH
    want_ov = ENGINE in ("openvino", "rknn") or (ENGINE == "auto" and not custom_pt)
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
# Latency budget of the queue (ms, 0 = off): when the AI falls behind, queued frames older than this are
# skipped (counted as framesStale) instead of being analysed late - keeps the live view close to real time.
# Off by default (every frame is analysed in order, as before); scripts/colab/colab_launch.py start sets 300.
FRAME_MAX_LAG_MS = max(0.0, float(_env("AIV_FRAME_MAX_LAG_MS", "0")))
STREAM_JPEG_QUALITY = int(_env("AIV_STREAM_JPEG_QUALITY", "75"))
STREAM_MAX_WIDTH = int(_env("AIV_STREAM_MAX_WIDTH", "1280"))
STREAM_MAX_FPS = float(_env("AIV_STREAM_MAX_FPS", "15"))
# Live-view JPEG encoding runs in its own thread per camera (enc:<camera id>) and always encodes the
# NEWEST annotated frame, so a slow browser / encoder never delays inference. 0 = encode in the AI loop (old).
STREAM_ENCODER_THREAD = _env("AIV_STREAM_ENCODER_THREAD", "1").strip() not in ("0", "false", "no", "off")
PREVIEW_IDLE_RELEASE_SEC = float(_env("AIV_PREVIEW_IDLE_RELEASE", "10"))

# ---- cameras running AI at the same time, and the dynamic AI frame rate (backend/services/ai_rate.py)
#   MAX_AI_CAMERAS   at most this many cameras may have a module running; a Start on a further camera is refused
#   AI_FPS_MAX       highest analysed rate of one camera (a single camera gets this, or its own frame rate if lower)
#   AI_FPS_BUDGET    analysed frames per second shared by all running cameras: each gets BUDGET / cameras,
#                    never more than AI_FPS_MAX and never less than AI_FPS_MIN
#   AI_FPS_RAMP      the rate moves towards its new value by at most this many fps per second (gradual change)
# The budget is a tuning value of the hardware, not a measurement: 60 = two cameras can each reach AI_FPS_MAX (30), so the
# real limits are the camera's frame rate and the cost per frame (RK3588 INT8 pipeline: about 49 ms = 20 fps per stream).
MAX_AI_CAMERAS = max(1, int(_env("AIV_MAX_AI_CAMERAS", "2")))
AI_FPS_MAX = max(1.0, float(_env("AIV_AI_FPS_MAX", "30")))
AI_FPS_BUDGET = max(1.0, float(_env("AIV_AI_FPS_BUDGET", "60")))
AI_FPS_MIN = max(1.0, float(_env("AIV_AI_FPS_MIN", "2")))
AI_FPS_RAMP = max(0.5, float(_env("AIV_AI_FPS_RAMP", "6")))

# Frame pipeline of the line-crossing engine: detection workers (one model each) that analyse different frames at the same
# time. auto = one per RK3588 NPU core (3) on the RKNN engine, 1 on every other engine. AIV_PIPELINE_DEPTH=1 turns it off.
# With 2 cameras each gets NPU_CORES // 2 workers (1), with one camera all of them.
NPU_CORES = max(1, int(_env("AIV_NPU_CORES", "3")))


def pipeline_depth(engine):
    v = str(_env("AIV_PIPELINE_DEPTH", "auto")).strip().lower()
    if v not in ("", "auto"):
        return max(1, int(v))
    return NPU_CORES if engine == "rknn" else 1

# --- AI frame sampling (defaults for every camera; each camera's module settings can override them,
#     camera["detection"]["modules"][<module>]["schedule"]) ---
#   *_INTERVAL   seconds between two AI analyses of a camera (0 = every frame, the previous behaviour).
#                With an interval, Human / Vehicle detections are confirmed on ONE analysed frame
#                (confidence >= class threshold + AIV_SAMPLED_CONFIRM_MARGIN) and repeated sightings at the same
#                place are suppressed for AIV_SAMPLED_DEDUPE_SEC; Human Count reports people in view per
#                sample (occupancy) instead of unique tracked people.
#   *_CAPTURE_FPS  frames per second the camera session processes / shows (frames above it are skipped right
#                after decoding, before any other work; the camera's own fps setting decides what is decoded).
#   motion gate  Human Detection and Object Monitoring only: AI runs when the picture changes (plus a fallback
#                check every AIV_MOTION_FALLBACK_SEC); never used for Vehicle, Human Count or Fire & Smoke.
# Scheduling mode for Human / Vehicle / Human Count / Object Monitoring:
#   adaptive (default) - IDLE while nothing happens (motion check inside the module's zones ~5x/s + one AI check
#              every AIV_IDLE_CHECK_SEC); ACTIVE = every frame (original tracker confirmation, full accuracy) while
#              there is motion in the zones, an unconfirmed object or (Human Count) people present; back to IDLE
#              after AIV_IDLE_AFTER_SEC without any of these
#   interval - fixed sampling (settings below); every - every frame (the original behaviour)
SCHEDULE_MODE = str(_env("AIV_SCHEDULE_MODE", "adaptive")).strip().lower()
IDLE_CHECK_SEC = float(_env("AIV_IDLE_CHECK_SEC", "10"))
IDLE_AFTER_SEC = float(_env("AIV_IDLE_AFTER_SEC", "10"))
VEHICLE_INTERVAL_SEC = float(_env("AIV_VEHICLE_INTERVAL", "2.0"))
VEHICLE_CAPTURE_FPS = float(_env("AIV_VEHICLE_CAPTURE_FPS", "16"))
HUMAN_INTERVAL_SEC = float(_env("AIV_HUMAN_INTERVAL", "4.0"))
HUMAN_CAPTURE_FPS = float(_env("AIV_HUMAN_CAPTURE_FPS", "6"))
COUNT_INTERVAL_SEC = float(_env("AIV_COUNT_INTERVAL", "4.0"))
HUMAN_MOTION_GATE = _env("AIV_HUMAN_MOTION_GATE", "1").strip() not in ("0", "false", "no", "off")
OBJECT_MOTION_GATE = _env("AIV_OBJECT_MOTION_GATE", "1").strip() not in ("0", "false", "no", "off")
MOTION_FALLBACK_SEC = float(_env("AIV_MOTION_FALLBACK_SEC", "30"))
MOTION_HOLD_SEC = float(_env("AIV_MOTION_HOLD_SEC", "10"))
MOTION_MIN_AREA_PCT = float(_env("AIV_MOTION_MIN_AREA_PCT", "0.5"))
SAMPLED_CONFIRM_MARGIN = float(_env("AIV_SAMPLED_CONFIRM_MARGIN", "0.10"))
SAMPLED_DEDUPE_SEC = float(_env("AIV_SAMPLED_DEDUPE_SEC", "60"))
# a frame older than this (receive -> analysis start) is not analysed (counted as stale; the next frame is used)
STALE_FRAME_SEC = float(_env("AIV_STALE_FRAME_SEC", "2.0"))

# --- events ---
DEFAULT_EVENT_COOLDOWN_SEC = float(_env("AIV_EVENT_COOLDOWN", "30"))
# Vehicle snapshots: one per confirmed vehicle (deduplicated by track), so the default cooldown is 0.
DEFAULT_SNAPSHOT_COOLDOWN_SEC = float(_env("AIV_SNAPSHOT_COOLDOWN", "0"))

LOG_LEVEL = _env("AIV_LOG_LEVEL", "INFO").upper()


def model_label():
    """Model shown in the UI: the yolov8n weights + the engine running them."""
    engine, _ = resolve_engine()
    return MODEL_PATH.name if engine == "pytorch" else f"{MODEL_PATH.name} ({engine_label(engine)})"


def setup_logging():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)
