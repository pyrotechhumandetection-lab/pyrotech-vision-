"""CPU runtime settings for inference: thread pools, CPU affinity, inference concurrency.

Why: on a CPU-only box every model call (one per camera loop, plus the fire / object workers) runs its own
pool of PyTorch intra-op threads. By default each pool has one thread per CPU core, so three cameras plus
a fire worker try to run 4 x 8 = 32 busy threads on 8 cores. The threads then spend their time waiting for
each other (and, on ARM boards with fast and slow cores, for the slow cores) instead of computing.

All settings are read ONCE at start-up from the environment / .env (see config.py) and applied before the
first model is loaded. They are never changed while inference is running: PyTorch only allows the
inter-op pool size to be set before it is used, and threads that already ran a model keep the intra-op
size they started with. To change a setting, edit .env (or run scripts/perf/pyrotech_perf.py apply) and
restart the backend.

    AIV_TORCH_THREADS          PyTorch intra-op threads per model call     0 = PyTorch default (one per core)
    AIV_TORCH_INTEROP_THREADS  PyTorch inter-op threads                    0 = PyTorch default
    AIV_INFER_CONCURRENCY      model calls allowed to run at the same time 0 = unlimited (previous behaviour)
                               (all cameras + fire + object workers); the others wait their turn (FIFO)
    AIV_CV_THREADS             OpenCV worker threads (resize / colour conversion)  0 = OpenCV default
    AIV_CPU_AFFINITY           CPUs the backend may use, e.g. "4-7" or "0,2,4-7"   empty = all
    AIV_OMP_WAIT_POLICY        passive | active  OpenMP idle threads sleep (passive) or spin (active)
                               empty = library default

Note: without AIV_TORCH_THREADS, Ultralytics itself sets PyTorch to min(8, CPUs - 1) intra-op threads
whenever it loads a model (7 on an 8-CPU box, per model call, for every camera at once) and switches
OpenCV threading off. With AIV_TORCH_THREADS set, that value is used everywhere instead.

Defaults leave every setting exactly as before; scripts/perf/pyrotech_perf.py measures which values are
faster on the actual hardware and writes them to .env (with a backup and a rollback command).
"""
import collections
import ctypes
import logging
import os
import sys
import threading
import time

log = logging.getLogger("aivision.runtime")

# what was requested and what is actually in effect (shown in /api/system/runtime and by the perf tool)
STATE = {"requested": {}, "applied": {}, "errors": [], "appliedAt": None}


def _int(name, default=0):
    try:
        return int(str(os.environ.get(name, "") or default).strip())
    except ValueError:
        STATE["errors"].append(f"{name}: not a number ({os.environ.get(name)!r}) - ignored")
        return default


def parse_cpu_list(text):
    """'0,2,4-7' -> [0, 2, 4, 5, 6, 7]. Raises ValueError on bad input."""
    cpus = set()
    for part in str(text).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            a, b = int(a), int(b)
            if b < a:
                raise ValueError(f"bad CPU range {part!r}")
            cpus.update(range(a, b + 1))
        else:
            cpus.add(int(part))
    if not cpus or min(cpus) < 0:
        raise ValueError(f"bad CPU list {text!r}")
    return sorted(cpus)


def early_env():
    """Called by config.py right after .env is read, i.e. before torch (and its OpenMP runtime) is imported.
    OpenMP reads OMP_NUM_THREADS / OMP_WAIT_POLICY only once, when it starts. An OMP_* value set in the
    real environment is kept (it is more specific)."""
    t = _int("AIV_TORCH_THREADS") or _int("AIV_FIRE_THREADS")      # AIV_FIRE_THREADS: older name, see apply()
    if t > 0:
        os.environ.setdefault("OMP_NUM_THREADS", str(t))
    wait = str(os.environ.get("AIV_OMP_WAIT_POLICY", "") or "").strip().lower()
    if wait in ("passive", "active"):
        os.environ.setdefault("OMP_WAIT_POLICY", wait.upper())
    elif wait:
        STATE["errors"].append(f"AIV_OMP_WAIT_POLICY: use passive or active (got {wait!r}) - ignored")


def apply(logger=None):
    """Apply the thread / affinity settings once, before any model is loaded. Safe to call again
    (later calls only report). Returns STATE."""
    lg = logger or log
    if STATE["appliedAt"]:
        return STATE
    req = {
        "torchThreads": _int("AIV_TORCH_THREADS"),
        "torchInteropThreads": _int("AIV_TORCH_INTEROP_THREADS"),
        "inferConcurrency": _int("AIV_INFER_CONCURRENCY"),
        "cvThreads": _int("AIV_CV_THREADS"),
        "cpuAffinity": str(os.environ.get("AIV_CPU_AFFINITY", "") or "").strip(),
        "ompWaitPolicy": str(os.environ.get("AIV_OMP_WAIT_POLICY", "") or "").strip().lower(),
    }
    fire_threads = _int("AIV_FIRE_THREADS")
    if fire_threads > 0 and req["torchThreads"] <= 0:
        # The fire worker used to call torch.set_num_threads() itself while other cameras were running.
        # That setting is process-wide, so it is now applied once here instead.
        req["torchThreads"] = fire_threads
        lg.info("AIV_FIRE_THREADS=%d is applied as AIV_TORCH_THREADS (one setting for all models)", fire_threads)
    STATE["requested"] = req

    if req["cpuAffinity"]:
        try:
            cpus = parse_cpu_list(req["cpuAffinity"])
            allowed = set(range(os.cpu_count() or 1))
            bad = [c for c in cpus if c not in allowed]
            if bad:
                raise ValueError(f"CPU(s) {bad} do not exist on this machine (0-{max(allowed)})")
            for tid in os.listdir("/proc/self/task"):      # threads already running (e.g. OpenCV / logging)
                try:
                    os.sched_setaffinity(int(tid), cpus)
                except OSError:
                    pass
            os.sched_setaffinity(0, cpus)                    # this thread; new threads inherit it
        except (ValueError, OSError, AttributeError) as e:
            STATE["errors"].append(f"AIV_CPU_AFFINITY: {e} - ignored")
            lg.warning("AIV_CPU_AFFINITY ignored: %s", e)

    if req["torchThreads"] > 0 or req["cvThreads"] > 0:
        # Ultralytics changes both settings itself: importing it calls cv2.setNumThreads(0), and every model
        # load calls torch.set_num_threads(min(8, CPUs - 1)) - process-wide, also while other cameras are
        # running inference. Import it first, then set our values, and make its model loader use our value.
        _pin_ultralytics(req["torchThreads"], lg)
    try:
        import cv2
        if req["cvThreads"] > 0:
            cv2.setNumThreads(req["cvThreads"])
    except Exception as e:  # noqa: BLE001
        STATE["errors"].append(f"AIV_CV_THREADS: {e}")

    try:
        if req["torchThreads"] <= 0 and req["torchInteropThreads"] <= 0:
            raise ImportError       # nothing to set: do not load torch before a model needs it
        import torch
        if req["torchThreads"] > 0:
            torch.set_num_threads(req["torchThreads"])
        if req["torchInteropThreads"] > 0:
            try:
                torch.set_num_interop_threads(req["torchInteropThreads"])
            except RuntimeError as e:          # inter-op pool already started - only possible at start-up
                STATE["errors"].append(f"AIV_TORCH_INTEROP_THREADS: {e}")
                lg.warning("AIV_TORCH_INTEROP_THREADS not applied: %s", e)
    except ImportError:
        pass

    GATE.configure(req["inferConcurrency"])
    STATE["applied"] = snapshot()
    STATE["appliedAt"] = time.time()
    a = STATE["applied"]
    lg.info("CPU settings: torch intra-op %s, inter-op %s, inference concurrency %s, OpenCV threads %s, "
            "CPUs %s of %s, OpenMP wait %s", a.get("torchThreads"), a.get("torchInteropThreads"),
            a.get("inferConcurrency") or "unlimited", a.get("cvThreads"), a.get("cpuAffinity"), os.cpu_count(),
            a.get("ompWaitPolicy") or "default")
    for e in STATE["errors"]:
        lg.warning("CPU settings: %s", e)
    return STATE


def _pin_ultralytics(torch_threads, lg):
    try:
        from ultralytics.utils import torch_utils
    except Exception as e:  # noqa: BLE001  (Ultralytics missing: the API still starts and reports it)
        lg.debug("Ultralytics not importable yet: %s", e)
        return
    if torch_threads > 0 and hasattr(torch_utils, "NUM_THREADS"):
        torch_utils.NUM_THREADS = torch_threads      # select_device() re-applies exactly our value


def ultralytics_default_threads():
    """The intra-op thread count Ultralytics sets when it loads a model (if AIV_TORCH_THREADS is not set)."""
    return min(8, max(1, (os.cpu_count() or 1) - 1))


def snapshot():
    """Settings in effect right now (read back from the libraries, not from the configuration)."""
    out = {"cpuCount": os.cpu_count()}
    try:
        aff = sorted(os.sched_getaffinity(0))
        out["cpuAffinity"] = _cpu_list_text(aff)
        out["cpusUsable"] = len(aff)
    except (AttributeError, OSError):
        out["cpuAffinity"], out["cpusUsable"] = None, os.cpu_count()
    torch = sys.modules.get("torch")          # never import it just to report (loads ~200 MB)
    try:
        if torch is None:
            raise ImportError
        out["torchThreads"] = torch.get_num_threads()
        out["torchInteropThreads"] = torch.get_num_interop_threads()
        out["torchVersion"] = torch.__version__
    except Exception:  # noqa: BLE001
        out["torchThreads"] = out["torchInteropThreads"] = None
    try:
        import cv2
        out["cvThreads"] = cv2.getNumThreads()
    except Exception:  # noqa: BLE001
        out["cvThreads"] = None
    out["inferConcurrency"] = GATE.limit
    out["ompNumThreads"] = os.environ.get("OMP_NUM_THREADS")
    out["ompWaitPolicy"] = (os.environ.get("OMP_WAIT_POLICY") or "").lower() or None
    return out


def _cpu_list_text(cpus):
    out, start, prev = [], None, None
    for c in cpus:
        if start is None:
            start = prev = c
        elif c == prev + 1:
            prev = c
        else:
            out.append(f"{start}-{prev}" if prev > start else str(start))
            start = prev = c
    if start is not None:
        out.append(f"{start}-{prev}" if prev > start else str(start))
    return ",".join(out)


class InferenceGate:
    """Limits how many model calls run at the same time (FIFO, so no camera is starved).
    limit 0 = unlimited: the gate is a no-op (previous behaviour)."""

    def __init__(self, limit=0):
        self._cond = threading.Condition()
        self._queue = collections.deque()
        self.limit = 0
        self.running = 0
        self.calls = 0
        self.waited = 0
        self.wait_ms_total = 0.0
        self.configure(limit)

    def configure(self, limit):
        with self._cond:
            self.limit = max(0, int(limit or 0))
            self._cond.notify_all()

    def slot(self):
        return _Slot(self)

    def _enter(self):
        if self.limit <= 0:
            with self._cond:
                self.calls += 1
            return 0.0
        t0 = time.perf_counter()
        me = object()
        with self._cond:
            self._queue.append(me)
            while self.limit > 0 and (self._queue[0] is not me or self.running >= self.limit):
                self._cond.wait()
            self._queue.popleft()
            self.running += 1
            self.calls += 1
            self._cond.notify_all()          # the next one in line may also fit
        w = (time.perf_counter() - t0) * 1000.0
        if w > 1.0:
            self.waited += 1
        self.wait_ms_total += w
        return w

    def _exit(self):
        if self.limit <= 0:
            return
        with self._cond:
            self.running = max(0, self.running - 1)
            self._cond.notify_all()

    def stats(self):
        return {"limit": self.limit, "running": self.running, "waiting": len(self._queue), "calls": self.calls,
                "callsThatWaited": self.waited, "waitMsTotal": round(self.wait_ms_total, 1)}


class _Slot:
    __slots__ = ("gate", "wait_ms")

    def __init__(self, gate):
        self.gate = gate
        self.wait_ms = 0.0

    def __enter__(self):
        self.wait_ms = self.gate._enter()
        return self

    def __exit__(self, *exc):
        self.gate._exit()
        return False


GATE = InferenceGate(0)

# per-thread sum of gate waits, so the camera loop can report it without seeing the slots
_tls = threading.local()


def take_wait_ms():
    """Gate waiting time (ms) accumulated by this thread since the last call."""
    s = getattr(_tls, "acc", 0.0)
    _tls.acc = 0.0
    return s


def _accumulate(slot):
    _tls.acc = getattr(_tls, "acc", 0.0) + slot.wait_ms


class gated:
    """with gated(): model call  - waits for a free inference slot when AIV_INFER_CONCURRENCY > 0 and
    records the wait for this thread (take_wait_ms)."""
    __slots__ = ("_s",)

    def __enter__(self):
        self._s = GATE.slot()
        self._s.__enter__()
        _accumulate(self._s)
        return self._s

    def __exit__(self, *exc):
        return self._s.__exit__(*exc)


# ---------------------------------------------------------------------------- thread names
_PR_SET_NAME = 15
try:
    _libc = ctypes.CDLL(None, use_errno=True)
    _prctl = _libc.prctl
except (OSError, AttributeError):
    _prctl = None


def name_thread(name):
    """Give the calling thread an OS-visible name (top -H, /proc/<pid>/task/*/comm, the perf monitor).
    Threads it creates later (e.g. the OpenMP threads of PyTorch) inherit the name, so the CPU time of a
    camera's model threads is reported under that camera. Linux only, max 15 characters; no-op elsewhere."""
    if _prctl is None:
        return
    try:
        _prctl(_PR_SET_NAME, ctypes.c_char_p(str(name).encode("ascii", "replace")[:15]), 0, 0, 0)
    except Exception:  # noqa: BLE001
        pass
