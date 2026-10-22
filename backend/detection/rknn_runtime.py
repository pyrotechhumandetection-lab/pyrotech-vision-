"""Rockchip NPU runtime (RK3588 / RK3568 / RK3566 ...) for the shipped yolov8n weights in RKNN format.

The official Python runtime (rknn-toolkit-lite2) only exists for Python 3.7-3.12; the backend runs on Python 3.13.
This module therefore talks to Rockchip's C runtime librknnrt.so directly (ctypes, rknn_api.h of rknn-toolkit2
v2.3.2) and offers the same small interface as rknnlite.api.RKNNLite (load_rknn / init_runtime / inference /
release), so the RKNN backend built into Ultralytics (preprocessing, NMS, ByteTrack) is used unchanged.

Where librknnrt.so is looked for (first found):
    AIV_RKNN_LIB, backend/npu/librknnrt.so (downloaded by ./aivision.sh npu setup), /usr/lib/librknnrt.so,
    /usr/lib/aarch64-linux-gnu/librknnrt.so, /usr/local/lib/librknnrt.so
The real rknn-toolkit-lite2 package is used instead when it is importable (Python <= 3.12).

NPU cores (RK3588 has 3): every new model instance (= camera session / module) gets the next core in turn
(AIV_NPU_CORE=rr, default), so 3 cameras run in parallel on 3 cores. auto = the driver picks, 0 / 1 / 2 / 0_1_2
= fixed mask for every instance.
"""
import ctypes
import hashlib
import itertools
import logging
import os
import sys
import threading
import time
import types
from pathlib import Path

import numpy as np

log = logging.getLogger("aivision.npu")

BACKEND_DIR = Path(__file__).resolve().parents[1]
LIB_CANDIDATES = [BACKEND_DIR / "npu" / "librknnrt.so", Path("/usr/lib/librknnrt.so"),
                  Path("/usr/lib/aarch64-linux-gnu/librknnrt.so"), Path("/usr/local/lib/librknnrt.so")]
RKNN_CHIPS = ("rk3588", "rk3576", "rk3568", "rk3566", "rk3562", "rv1126b", "rk2118")

# ---------------------------------------------------------------- rknn_api.h (v2.3.2)
RKNN_SUCC = 0
RKNN_MAX_DIMS = 16
RKNN_MAX_NAME_LEN = 256
Q_IN_OUT_NUM, Q_INPUT_ATTR, Q_OUTPUT_ATTR, Q_PERF_RUN, Q_SDK_VERSION, Q_MEM_SIZE, Q_CUSTOM_STRING = 0, 1, 2, 4, 5, 6, 7
T_FLOAT32, T_FLOAT16, T_INT8, T_UINT8 = 0, 1, 2, 3
F_NCHW, F_NHWC = 0, 1
CORE_AUTO, CORE_0, CORE_1, CORE_2, CORE_0_1_2 = 0, 1, 2, 4, 7
TYPE_NAMES = {0: "FP32", 1: "FP16", 2: "INT8", 3: "UINT8", 4: "INT16", 5: "UINT16", 6: "INT32", 7: "UINT32",
              8: "INT64", 9: "BOOL", 10: "INT4", 11: "BF16"}
FMT_NAMES = {0: "NCHW", 1: "NHWC", 2: "NC1HWC2", 3: "UNDEFINED"}
QNT_NAMES = {0: "NONE", 1: "DFP", 2: "AFFINE"}
ERRORS = {-1: "execute failed", -2: "timeout", -3: "device unavailable", -4: "out of memory", -5: "invalid parameter",
          -6: "invalid model", -7: "invalid context", -8: "invalid input", -9: "invalid output",
          -10: "model version mismatch (model built with a newer rknn-toolkit2 than this librknnrt)",
          -11: "device version mismatch", -12: "model uses an op this driver does not support",
          -13: "target platform mismatch (model built for another chip)"}

rknn_context = ctypes.c_uint64


class InOutNum(ctypes.Structure):
    _fields_ = [("n_input", ctypes.c_uint32), ("n_output", ctypes.c_uint32)]


class TensorAttr(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32), ("n_dims", ctypes.c_uint32), ("dims", ctypes.c_uint32 * RKNN_MAX_DIMS),
                ("name", ctypes.c_char * RKNN_MAX_NAME_LEN), ("n_elems", ctypes.c_uint32), ("size", ctypes.c_uint32),
                ("fmt", ctypes.c_int), ("type", ctypes.c_int), ("qnt_type", ctypes.c_int), ("fl", ctypes.c_int8),
                ("zp", ctypes.c_int32), ("scale", ctypes.c_float), ("w_stride", ctypes.c_uint32),
                ("size_with_stride", ctypes.c_uint32), ("pass_through", ctypes.c_uint8), ("h_stride", ctypes.c_uint32)]


class SdkVersion(ctypes.Structure):
    _fields_ = [("api_version", ctypes.c_char * 256), ("drv_version", ctypes.c_char * 256)]


class MemSize(ctypes.Structure):
    _fields_ = [("total_weight_size", ctypes.c_uint32), ("total_internal_size", ctypes.c_uint32),
                ("total_dma_allocated_size", ctypes.c_uint64), ("total_sram_size", ctypes.c_uint32),
                ("free_sram_size", ctypes.c_uint32), ("reserved", ctypes.c_uint32 * 10)]


class PerfRun(ctypes.Structure):
    _fields_ = [("run_duration", ctypes.c_int64)]


class CustomString(ctypes.Structure):
    _fields_ = [("string", ctypes.c_char * 1024)]


class Input(ctypes.Structure):
    _fields_ = [("index", ctypes.c_uint32), ("buf", ctypes.c_void_p), ("size", ctypes.c_uint32),
                ("pass_through", ctypes.c_uint8), ("type", ctypes.c_int), ("fmt", ctypes.c_int)]


class Output(ctypes.Structure):
    _fields_ = [("want_float", ctypes.c_uint8), ("is_prealloc", ctypes.c_uint8), ("index", ctypes.c_uint32),
                ("buf", ctypes.c_void_p), ("size", ctypes.c_uint32)]


# ---------------------------------------------------------------- platform / library
def soc():
    """Rockchip chip name from the device tree (e.g. 'rk3588'), or None. AIV_RKNN_SOC overrides (tests only)."""
    forced = os.environ.get("AIV_RKNN_SOC", "").strip().lower()
    if forced:
        return forced if forced in RKNN_CHIPS else None
    try:
        txt = Path("/proc/device-tree/compatible").read_bytes().decode("ascii", "ignore").lower()
    except OSError:
        return None
    for part in txt.replace("\x00", ",").split(","):
        p = part.strip().split("-", 1)[0]
        for chip in RKNN_CHIPS:           # rk3588s = rk3588 NPU
            if p == chip or p.startswith(chip) and p[len(chip):] in ("s", "j", "m"):
                return chip
    return None


def lib_path():
    env = os.environ.get("AIV_RKNN_LIB", "").strip()
    for p in ([Path(env)] if env else []) + LIB_CANDIDATES:
        if p.is_file():
            return p
    return None


_LIB = None
_LIB_LOCK = threading.Lock()


def load_lib():
    global _LIB
    with _LIB_LOCK:
        if _LIB is not None:
            return _LIB
        p = lib_path()
        if p is None:
            raise OSError("librknnrt.so not found - run ./aivision.sh npu setup")
        lib = ctypes.CDLL(str(p))
        u32, ctx, vp = ctypes.c_uint32, rknn_context, ctypes.c_void_p
        sig = {"rknn_init": ([ctypes.POINTER(rknn_context), vp, u32, u32, vp]),
               "rknn_destroy": ([ctx]), "rknn_query": ([ctx, ctypes.c_int, vp, u32]),
               "rknn_inputs_set": ([ctx, u32, ctypes.POINTER(Input)]),
               "rknn_set_core_mask": ([ctx, ctypes.c_int]), "rknn_run": ([ctx, vp]),
               "rknn_outputs_get": ([ctx, u32, ctypes.POINTER(Output), vp]),
               "rknn_outputs_release": ([ctx, u32, ctypes.POINTER(Output)])}
        for name, args in sig.items():
            f = getattr(lib, name)
            f.argtypes, f.restype = args, ctypes.c_int
        lib._path = str(p)
        _LIB = lib
        return lib


def available():
    """(ok, reason): the NPU can be used (Rockchip SoC with an NPU device and a runtime library)."""
    if not sys.platform.startswith("linux"):
        return False, "not Linux"
    if soc() is None:
        return False, "not a Rockchip SoC with an NPU"
    if _real_rknnlite() is None and lib_path() is None:
        return False, "librknnrt.so not found (./aivision.sh npu setup)"
    return True, "ok"


def _real_rknnlite():
    if os.environ.get("AIV_RKNN_CTYPES", "").strip() in ("1", "true", "yes"):
        return None
    try:
        from rknnlite.api import RKNNLite as R   # noqa: N814
        if getattr(R, "_aivision_shim", False):
            return None
        return R
    except Exception:
        return None


def _check(ret, what):
    if ret != RKNN_SUCC:
        raise RuntimeError(f"{what} failed: {ret} ({ERRORS.get(ret, 'error')})")


def attr_dict(a):
    dims = [int(a.dims[i]) for i in range(a.n_dims)]
    return {"index": int(a.index), "name": a.name.decode(errors="ignore"), "dims": dims, "nElems": int(a.n_elems),
            "size": int(a.size), "fmt": FMT_NAMES.get(a.fmt, a.fmt), "type": TYPE_NAMES.get(a.type, a.type),
            "qnt": QNT_NAMES.get(a.qnt_type, a.qnt_type), "zp": int(a.zp), "scale": float(a.scale)}


# ---------------------------------------------------------------- core assignment
_CORES = itertools.cycle((CORE_0, CORE_1, CORE_2))
_CORE_LOCK = threading.Lock()
CORE_MASKS = {"auto": CORE_AUTO, "0": CORE_0, "1": CORE_1, "2": CORE_2, "0_1_2": CORE_0_1_2, "all": CORE_0_1_2}


def next_core_mask():
    want = os.environ.get("AIV_NPU_CORE", "rr").strip().lower()
    if want in CORE_MASKS:
        return CORE_MASKS[want]
    if soc() != "rk3588":            # only RK3588 has 3 cores; other chips: the driver decides
        return CORE_AUTO
    with _CORE_LOCK:
        return next(_CORES)


def core_name(mask):
    return {CORE_AUTO: "auto", CORE_0: "core 0", CORE_1: "core 1", CORE_2: "core 2", CORE_0_1_2: "cores 0+1+2"}.get(mask, str(mask))


def _test_image(shape):
    """1 x H x W x 3 uint8 RGB test image: Ultralytics' bus.jpg letterboxed, else smooth gradients."""
    h, w = int(shape[1]), int(shape[2])
    try:
        import cv2
        import ultralytics
        im = cv2.imread(str(Path(ultralytics.__file__).parent / "assets" / "bus.jpg"))
        if im is not None:
            r = min(h / im.shape[0], w / im.shape[1])
            nh, nw = round(im.shape[0] * r), round(im.shape[1] * r)
            out = np.full((h, w, 3), 114, np.uint8)
            top, left = (h - nh) // 2, (w - nw) // 2
            out[top:top + nh, left:left + nw] = cv2.resize(im, (nw, nh))[:, :, ::-1]
            return out[None]
    except Exception:
        pass
    yy, xx = np.mgrid[0:h, 0:w]
    return np.stack([xx * 255 // max(1, w - 1), yy * 255 // max(1, h - 1), (xx + yy) % 256], -1).astype(np.uint8)[None]


# ---------------------------------------------------------------- RKNNLite-compatible runtime
_IO_CHOICE = {}            # model sha1 -> (io variant, autotune table)
_IO_LOCK = threading.Lock()

class RKNNLite:
    """Same calls as rknnlite.api.RKNNLite (the subset Ultralytics uses), backed by librknnrt.so via ctypes."""
    _aivision_shim = True
    NPU_CORE_AUTO, NPU_CORE_0, NPU_CORE_1, NPU_CORE_2, NPU_CORE_0_1_2 = CORE_AUTO, CORE_0, CORE_1, CORE_2, CORE_0_1_2

    def __init__(self, verbose=False):
        self.lib = None
        self.ctx = rknn_context(0)
        self.model = None
        self.inputs, self.outputs = [], []
        self.core_mask = None
        self._lock = threading.Lock()

    def load_rknn(self, path):
        try:
            self.model = Path(path).read_bytes()
            return 0
        except OSError as e:
            log.error("cannot read %s: %s", path, e)
            return -1

    def init_runtime(self, core_mask=None, **_kw):
        self.lib = load_lib()
        buf = ctypes.create_string_buffer(self.model, len(self.model))
        _check(self.lib.rknn_init(ctypes.byref(self.ctx), ctypes.cast(buf, ctypes.c_void_p), len(self.model), 0, None), "rknn_init")
        self._buf = buf
        n = InOutNum()
        _check(self.lib.rknn_query(self.ctx, Q_IN_OUT_NUM, ctypes.byref(n), ctypes.sizeof(n)), "query in/out")
        self.inputs = [self._attr(Q_INPUT_ATTR, i) for i in range(n.n_input)]
        self.outputs = [self._attr(Q_OUTPUT_ATTR, i) for i in range(n.n_output)]
        mask = next_core_mask() if core_mask is None else core_mask
        if mask != CORE_AUTO:
            ret = self.lib.rknn_set_core_mask(self.ctx, mask)
            if ret != RKNN_SUCC:          # single-core chips refuse a mask: keep auto
                log.warning("NPU core mask %s refused (%s) - driver chooses", core_name(mask), ret)
                mask = CORE_AUTO
        self.core_mask = mask
        self._choose_io()
        return 0

    def _choose_io(self):
        """AIV_RKNN_IO: auto (default: measured once per model file and process), or e.g. uint8/float, fp16/raw."""
        want = os.environ.get("AIV_RKNN_IO", "auto").strip().lower()
        if want != "auto":
            self.io = tuple(want.split("/")) if "/" in want else ("uint8", "float")
            return
        key = hashlib.sha1(self.model).hexdigest()
        with _IO_LOCK:
            if key not in _IO_CHOICE:
                try:
                    table = self.autotune()
                    _IO_CHOICE[key] = (getattr(self, "io", ("uint8", "float")), table)
                    log.info("NPU I/O for this model: %s  (%s)", "/".join(_IO_CHOICE[key][0]),
                             ", ".join(f"{r['io']} {r['ms']} ms{'' if r['same'] else ' (differs - not used)'}" for r in table))
                except Exception as e:      # never block loading: keep the default
                    log.warning("NPU I/O autotune failed (%s) - using uint8/float", e)
                    _IO_CHOICE[key] = (("uint8", "float"), [])
            self.io = _IO_CHOICE[key][0]
            self.io_table = _IO_CHOICE[key][1]

    def _attr(self, cmd, i):
        a = TensorAttr()
        a.index = i
        _check(self.lib.rknn_query(self.ctx, cmd, ctypes.byref(a), ctypes.sizeof(a)), "query tensor attr")
        return attr_dict(a)

    # How data crosses the CPU <-> NPU boundary. The FP16 model's NPU time is about half of the call on the RK3588:
    # the rest is librknnrt converting the input (uint8 -> FP16) and the output (FP16 -> FP32, layout) on one CPU
    # core. Variants (same model, same result - checked by autotune before one is used):
    #   in:  uint8 (runtime converts)   fp16 (numpy converts, 0..255)   fp16n (numpy converts, 0..1)
    #   out: float (runtime converts)   raw  (native type, numpy converts / dequantizes)
    IO_VARIANTS = (("uint8", "float"), ("uint8", "raw"), ("fp16", "raw"), ("fp16n", "raw"))

    def _prep(self, x, mode):
        if mode == "fp16":
            return np.ascontiguousarray(x, np.float16), T_FLOAT16
        if mode == "fp16n":
            return np.ascontiguousarray(x.astype(np.float32) * (1.0 / 255.0), np.float16), T_FLOAT16
        x = np.ascontiguousarray(x)
        return x, T_UINT8 if x.dtype == np.uint8 else T_FLOAT32

    def inference(self, inputs, data_format=None, io=None, **_kw):
        """inputs: list of uint8 NHWC arrays (one per model input). Returns float32 arrays in the model's shapes."""
        in_mode, out_mode = io or getattr(self, "io", None) or ("uint8", "float")
        with self._lock:
            n_in = len(inputs)
            ins = (Input * n_in)()
            keep = []
            for i, x in enumerate(inputs):
                x, t = self._prep(x, in_mode)
                keep.append(x)
                ins[i].index, ins[i].buf, ins[i].size = i, x.ctypes.data, x.nbytes
                ins[i].pass_through = 0
                ins[i].type = t
                ins[i].fmt = F_NCHW if data_format == "nchw" else F_NHWC
            _check(self.lib.rknn_inputs_set(self.ctx, n_in, ins), "rknn_inputs_set")
            ret = self.lib.rknn_run(self.ctx, None)
            if ret != RKNN_SUCC:          # one retry: a busy / timed-out NPU should not stop the camera
                log.warning("rknn_run failed (%s %s) - retrying once", ret, ERRORS.get(ret, ""))
                _check(self.lib.rknn_inputs_set(self.ctx, n_in, ins), "rknn_inputs_set")
                ret = self.lib.rknn_run(self.ctx, None)
            _check(ret, "rknn_run")
            n_out = len(self.outputs)
            outs = (Output * n_out)()
            want_float = 1 if out_mode == "float" else 0
            for i in range(n_out):
                outs[i].want_float, outs[i].is_prealloc, outs[i].index = want_float, 0, i
            _check(self.lib.rknn_outputs_get(self.ctx, n_out, outs, None), "rknn_outputs_get")
            try:
                res = []
                for i, meta in enumerate(self.outputs):
                    raw = np.frombuffer(ctypes.string_at(outs[i].buf, outs[i].size), np.uint8)
                    t = meta["type"] if not want_float else "FP32"
                    if t == "FP32":
                        arr = raw.view(np.float32).copy()
                    elif t == "FP16":
                        arr = raw.view(np.float16).astype(np.float32)
                    elif t in ("INT8", "UINT8"):
                        q = raw.view(np.int8 if t == "INT8" else np.uint8).astype(np.float32)
                        arr = (q - meta["zp"]) * meta["scale"]
                    else:
                        raise RuntimeError(f"raw output type {t} not supported")
                    dims = meta["dims"]
                    res.append(arr.reshape(dims) if int(np.prod(dims)) == arr.size else arr)
                return res
            finally:
                self.lib.rknn_outputs_release(self.ctx, n_out, outs)

    def autotune(self, runs=4):
        """Measure every I/O variant on a test image, keep the fastest one whose output equals the default
        (uint8 / float) output: scores within 0.01, boxes within 0.5 px. Returns the table (for the report)."""
        dims = self.inputs[0]["dims"]
        shape = dims if self.inputs[0]["fmt"] == "NHWC" else [dims[0], dims[2], dims[3], dims[1]]
        x = _test_image(shape)             # a real photo: random noise drives the FP16 model into inf / NaN
        table, ref = [], None
        for io in self.IO_VARIANTS:
            try:
                y = self.inference([x], io=io)[0]
                ts = []
                for _ in range(runs):
                    t0 = time.perf_counter()
                    self.inference([x], io=io)
                    ts.append((time.perf_counter() - t0) * 1000.0)
                ms = float(np.median(ts))
            except Exception as e:
                table.append({"io": "/".join(io), "ms": None, "same": False, "error": str(e)[:80]})
                continue
            if ref is None:
                ref = y
            d = None
            if y.shape == ref.shape:
                a, b = y.astype(np.float32), ref.astype(np.float32)
                bad = ~np.isfinite(a) | ~np.isfinite(b)
                with np.errstate(invalid="ignore", over="ignore"):
                    d = np.where(bad, np.where(np.isnan(a) & np.isnan(b) | (a == b), 0.0, np.inf), np.abs(a - b))
            if d is None:
                same = False
            elif d.ndim == 3:            # detection head 1 x (4 box + classes) x anchors
                same = float(d[:, :4].max()) <= 0.5 and float(d[:, 4:].max()) <= 0.01
            else:
                same = float(d.max()) <= 0.01
            table.append({"io": "/".join(io), "ms": round(ms, 1), "same": bool(same),
                          "maxDiff": round(float(d.max()), 4) if d is not None else None})
        ok = [r for r in table if r["same"] and r["ms"] is not None]
        if ok:
            best = min(ok, key=lambda r: r["ms"])
            self.io = tuple(best["io"].split("/"))
        return table

    # extras for the analysis script
    def sdk_version(self):
        v = SdkVersion()
        _check(self.lib.rknn_query(self.ctx, Q_SDK_VERSION, ctypes.byref(v), ctypes.sizeof(v)), "query sdk version")
        return v.api_version.decode(errors="ignore").strip(), v.drv_version.decode(errors="ignore").strip()

    def mem_size(self):
        m = MemSize()
        if self.lib.rknn_query(self.ctx, Q_MEM_SIZE, ctypes.byref(m), ctypes.sizeof(m)) != RKNN_SUCC:
            return None
        return {"weightBytes": int(m.total_weight_size), "internalBytes": int(m.total_internal_size),
                "dmaBytes": int(m.total_dma_allocated_size)}

    def custom_string(self):
        c = CustomString()
        if self.lib.rknn_query(self.ctx, Q_CUSTOM_STRING, ctypes.byref(c), ctypes.sizeof(c)) != RKNN_SUCC:
            return ""
        return c.string.decode(errors="ignore")

    def last_run_us(self):
        p = PerfRun()
        if self.lib.rknn_query(self.ctx, Q_PERF_RUN, ctypes.byref(p), ctypes.sizeof(p)) != RKNN_SUCC:
            return None
        return int(p.run_duration)

    def release(self):
        if self.lib is not None and self.ctx.value:
            self.lib.rknn_destroy(self.ctx)
            self.ctx = rknn_context(0)

    def __del__(self):
        try:
            self.release()
        except Exception:
            pass


# ---------------------------------------------------------------- Ultralytics integration
_INSTALLED = False


def install_for_ultralytics():
    """Make Ultralytics' RKNN backend usable on Python 3.13: provide rknnlite.api (ctypes runtime) unless the
    real package is importable, and use this module's Rockchip / requirement checks."""
    global _INSTALLED
    if _INSTALLED:
        return
    real = _real_rknnlite()
    if real is None:
        pkg = types.ModuleType("rknnlite")
        api = types.ModuleType("rknnlite.api")
        api.RKNNLite = RKNNLite
        pkg.api = api
        sys.modules["rknnlite"], sys.modules["rknnlite.api"] = pkg, api
    else:
        _wrap_real_core(real)
    try:
        import ultralytics.nn.backends.rknn as ub
        ub.is_rockchip = lambda: soc() is not None
        orig = ub.check_requirements

        def _req(req, *a, **k):
            if "rknn-toolkit-lite2" in str(req):
                return True
            return orig(req, *a, **k)
        ub.check_requirements = _req
    except Exception as e:          # older Ultralytics without the RKNN backend
        raise RuntimeError(f"this Ultralytics version has no RKNN backend: {e}")
    _INSTALLED = True


def _wrap_real_core(cls):
    """Real rknn-toolkit-lite2: give every instance the next NPU core as well."""
    if getattr(cls, "_aivision_wrapped", False):
        return
    orig = cls.init_runtime

    def init_runtime(self, core_mask=None, *a, **k):
        mask = next_core_mask() if core_mask is None else core_mask
        self._aivision_core = mask
        return orig(self, core_mask=mask, *a, **k)
    cls.init_runtime = init_runtime
    cls._aivision_wrapped = True


def model_info(model_dir):
    """Static info of an exported *_rknn_model folder: .rknn file, input size, precision (metadata.yaml)."""
    d = Path(model_dir)
    f = d if d.is_file() else next(iter(sorted(d.glob("*.rknn"))), None)
    info = {"file": str(f) if f else None, "imgsz": 640, "quantize": None, "names": None}
    meta = (f.parent if f else d) / "metadata.yaml"
    if meta.is_file():
        try:
            import yaml
            m = yaml.safe_load(meta.read_text()) or {}
            imgsz = m.get("imgsz") or [640, 640]
            info["imgsz"] = int(imgsz[0] if isinstance(imgsz, (list, tuple)) else imgsz)
            info["quantize"] = (m.get("args") or {}).get("quantize")
            info["names"] = len(m.get("names") or {})
        except Exception:
            pass
    return info
