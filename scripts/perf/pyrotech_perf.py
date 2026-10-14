#!/usr/bin/env python3
"""PYROTECH VISION - performance monitor and CPU optimizer for the AI box.

Runs next to the application (it is NOT a second backend): it reads the backend's own metrics API and
/proc, benchmarks CPU thread configurations in separate low-priority processes, and applies a chosen
configuration only through .env + a controlled backend restart, with a backup, a live before/after
measurement and an automatic rollback when the measurement does not confirm the gain.

    scripts/debian/perf-tune.sh <command> [options]       (wrapper: uses backend/.venv, low priority)

Commands
    status                 one-shot: hardware, CPU settings in effect, per-camera performance, findings
    monitor                live view every --interval s (default 2) with timestamps; --log FILE.jsonl
    start | stop | restart application control through its normal start-up procedure (systemd services
                           when installed, otherwise ./start.sh started and stopped by this tool)
    frames                 save representative frames from the running cameras (backend/data/perf/frames)
    bench                  benchmark CPU configurations on those frames (separate processes, idle priority)
    apply                  write CPU settings to .env (backup first), restart, measure, keep or roll back
    validate               measure now and compare with the measurement taken before the last apply
    rollback               restore the .env from before the last apply (or --to ID) and restart
    history                applied changes and their measured results
    auto                   start if needed -> measure -> frames -> bench -> apply best (validated) -> report

Everything this tool writes stays in the project: backend/data/perf/ (logs, frames, backups, reports) and
.env (only the AIV_* CPU keys listed in KEYS, always after a timestamped backup).
"""
import argparse
import datetime as _dt
import glob
import json
import os
import platform
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PERF_DIR = os.path.join(REPO, "backend", "data", "perf")
ENV_FILE = os.path.join(REPO, ".env")
BACKEND_ENV_FILE = os.path.join(REPO, "backend", ".env")
CHANGES = os.path.join(PERF_DIR, "changes.jsonl")
STATE_FILE = os.path.join(PERF_DIR, "state.json")
APP_PIDFILE = os.path.join(PERF_DIR, "app.pid")
VENV_PY = os.path.join(REPO, "backend", ".venv", "bin", "python")
WORKER = os.path.join(REPO, "scripts", "perf", "bench_worker.py")
BACKEND_UNIT = "pyrotech-vision-backend.service"
FRONTEND_UNIT = "pyrotech-vision-frontend.service"
UNIT_DIR = "/etc/systemd/system"

# the only .env keys this tool ever writes
KEYS = ("AIV_TORCH_THREADS", "AIV_TORCH_INTEROP_THREADS", "AIV_INFER_CONCURRENCY", "AIV_CV_THREADS",
        "AIV_CPU_AFFINITY", "AIV_OMP_WAIT_POLICY")
SHORT = {"AIV_TORCH_THREADS": "threads", "AIV_TORCH_INTEROP_THREADS": "interop", "AIV_INFER_CONCURRENCY": "concurrency",
         "AIV_CV_THREADS": "cv", "AIV_CPU_AFFINITY": "cpus", "AIV_OMP_WAIT_POLICY": "omp"}

COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(text, code):
    return f"\033[{code}m{text}\033[0m" if COLOR else str(text)


def ts():
    return _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def stamp():
    return _dt.datetime.now().strftime("%Y%m%d-%H%M%S")


def say(msg):
    print(f"[{ts()}] {msg}", flush=True)


def warn(msg):
    print(f"[{ts()}] {c('WARN', '1;33')} {msg}", flush=True)


def fail(msg, code=1):
    print(f"[{ts()}] {c('ERROR', '1;31')} {msg}", file=sys.stderr, flush=True)
    sys.exit(code)


def ensure_dirs():
    for d in (PERF_DIR, os.path.join(PERF_DIR, "backups"), os.path.join(PERF_DIR, "reports"),
              os.path.join(PERF_DIR, "frames"), os.path.join(PERF_DIR, "bench")):
        os.makedirs(d, exist_ok=True)


def append_jsonl(path, obj):
    ensure_dirs()
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj) + "\n")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    ensure_dirs()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_FILE)


# ============================================================================ .env
def read_env(path):
    """KEY -> value like backend/config.py reads it (first occurrence wins, quotes stripped)."""
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                    v = v[1:-1]
                out.setdefault(k, v)
    except FileNotFoundError:
        pass
    return out


def current_settings():
    """The CPU settings the backend will read at its next start (.env + backend/.env + environment)."""
    env = read_env(ENV_FILE)
    benv = read_env(BACKEND_ENV_FILE)
    out = {}
    for k in KEYS:
        out[k] = os.environ.get(k) or benv.get(k) or env.get(k) or ""
    return out


def unit_env(unit, key):
    try:
        r = subprocess.run(["systemctl", "show", "-p", "Environment", "--value", unit], capture_output=True, text=True, timeout=10)
        for tok in r.stdout.split():
            if tok.startswith(key + "="):
                return tok.split("=", 1)[1]
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def blockers(keys):
    """Places that would override a value written to .env (so it would silently not take effect)."""
    found = []
    benv = read_env(BACKEND_ENV_FILE)
    for k in keys:
        if k in benv:
            found.append(f"{k} is also set in backend/.env (that file wins over .env)")
        if systemd_mode() and unit_env(BACKEND_UNIT, k) is not None:
            found.append(f"{k} is set in the systemd unit {BACKEND_UNIT} (Environment= wins over .env)")
    return found


def write_env(settings, reason):
    """Back up .env, then set the given KEYS (in place where they already exist, else in a marked block).
    settings: {KEY: value}; value "" removes the key (back to the application default). Returns backup path."""
    ensure_dirs()
    for k in settings:
        if k not in KEYS:
            raise ValueError(f"{k} is not a CPU setting this tool manages")
    backup = os.path.join(PERF_DIR, "backups", f"env-{stamp()}.bak")
    if os.path.exists(ENV_FILE):
        shutil.copy2(ENV_FILE, backup)
        with open(ENV_FILE, encoding="utf-8") as f:
            lines = f.read().splitlines()
    else:
        with open(backup, "w", encoding="utf-8") as f:
            f.write("# .env did not exist before this change\n")
        lines = []
    todo = dict(settings)
    out = []
    for line in lines:
        m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if m and m.group(1) in todo:
            k = m.group(1)
            if todo[k] == "":
                out.append(f"# {line.strip()}   (removed by pyrotech_perf {ts()}: {reason})")
            else:
                out.append(f"{k}={todo[k]}")
            continue
        out.append(line)
    seen = {m.group(1) for m in (re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", l) for l in lines) if m}
    new = {k: v for k, v in todo.items() if k not in seen and v != ""}
    if new:
        if "# >>> pyrotech-perf" not in "\n".join(out):
            out += ["", "# >>> pyrotech-perf: CPU settings written by scripts/perf/pyrotech_perf.py",
                    "#     (undo: scripts/debian/perf-tune.sh rollback; backups in backend/data/perf/backups)"]
            out += [f"{k}={v}" for k, v in new.items()]
            out.append("# <<< pyrotech-perf")
        else:
            i = out.index("# <<< pyrotech-perf") if "# <<< pyrotech-perf" in out else len(out)
            out[i:i] = [f"{k}={v}" for k, v in new.items()]
    tmp = ENV_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")
    os.replace(tmp, ENV_FILE)
    return backup


def restore_env(backup):
    ensure_dirs()
    safety = os.path.join(PERF_DIR, "backups", f"env-{stamp()}-before-rollback.bak")
    if os.path.exists(ENV_FILE):
        shutil.copy2(ENV_FILE, safety)
    with open(backup, encoding="utf-8") as f:
        first = f.readline()
    if first.startswith("# .env did not exist before this change"):
        if os.path.exists(ENV_FILE):
            os.remove(ENV_FILE)       # it did not exist before the change (a copy is kept in safety)
    else:
        shutil.copy2(backup, ENV_FILE)
    return safety


# ============================================================================ backend API
def api_port():
    return (os.environ.get("AIV_PORT") or (unit_env(BACKEND_UNIT, "AIV_PORT") if systemd_mode() else None)
            or read_env(ENV_FILE).get("AIV_PORT") or "8000")


def api(path, timeout=5.0, method="GET", body=None):
    url = f"http://127.0.0.1:{api_port()}/api{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Type", "").startswith("application/json"):
                return r.status, json.loads(raw or b"null")
            return r.status, raw
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def backend_pid():
    st, r = api("/system/runtime", 3)
    if st == 200 and isinstance(r, dict) and r.get("pid"):
        return int(r["pid"])
    for d in os.listdir("/proc"):          # backend without the runtime endpoint (older version) / API busy
        if not d.isdigit():
            continue
        try:
            cmd = open(f"/proc/{d}/cmdline", "rb").read().split(b"\0")
            cwd = os.readlink(f"/proc/{d}/cwd")
        except OSError:
            continue
        if any(x.endswith(b"backend/main.py") for x in cmd) and cwd.startswith(REPO):
            return int(d)
    return None


def systemd_mode():
    return os.path.isfile(os.path.join(UNIT_DIR, BACKEND_UNIT))


# ============================================================================ /proc sampling
TICK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def _cpu_times():
    out = {}
    with open("/proc/stat") as f:
        for line in f:
            if not line.startswith("cpu"):
                break
            p = line.split()
            vals = list(map(int, p[1:]))
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            out[p[0]] = (sum(vals[:8]), idle)
    return out


def _meminfo():
    m = {}
    with open("/proc/meminfo") as f:
        for line in f:
            k, v = line.split(":", 1)
            m[k] = int(v.split()[0])
    return m


def thread_group(comm):
    if comm.startswith("av:") or comm.startswith("ffmpeg"):
        return "decode (FFmpeg)"
    return comm


class Sampler:
    """Cheap /proc sampler: deltas between calls (system, per core, backend process, backend threads)."""

    def __init__(self, pid=None):
        self.pid = pid
        self.prev = None
        self.self_prev = None

    def _proc(self):
        if not self.pid:
            return None
        try:
            with open(f"/proc/{self.pid}/stat") as f:
                p = f.read().rsplit(")", 1)[1].split()
            cpu = (int(p[11]) + int(p[12])) / TICK
            rss = thr = None
            with open(f"/proc/{self.pid}/status") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        rss = int(line.split()[1]) / 1024
                    elif line.startswith("Threads:"):
                        thr = int(line.split()[1])
            tasks = {}
            for tid in os.listdir(f"/proc/{self.pid}/task"):
                try:
                    with open(f"/proc/{self.pid}/task/{tid}/stat") as f:
                        s = f.read()
                    comm = s[s.index("(") + 1:s.rindex(")")]
                    q = s.rsplit(")", 1)[1].split()
                    tasks[tid] = (comm, q[0], (int(q[11]) + int(q[12])) / TICK)
                except (OSError, ValueError, IndexError):
                    pass
            return {"cpu": cpu, "rss": rss, "threads": thr, "tasks": tasks}
        except OSError:
            return None

    def sample(self):
        now = time.monotonic()
        cur = {"t": now, "cpu": _cpu_times(), "proc": self._proc(), "mem": _meminfo(),
               "load": open("/proc/loadavg").read().split()[:3], "self": os.times()}
        out = None
        if self.prev:
            el = now - self.prev["t"]
            out = {"seconds": el}
            cores = []
            for k, (tot, idle) in sorted(cur["cpu"].items(), key=lambda kv: (len(kv[0]), kv[0])):
                pt, pi = self.prev["cpu"].get(k, (tot, idle))
                dt = tot - pt
                use = 100.0 * (1 - (idle - pi) / dt) if dt > 0 else 0.0
                if k == "cpu":
                    out["cpuTotal"] = round(use, 1)
                else:
                    cores.append(round(use))
            out["cores"] = cores
            m = cur["mem"]
            out["memTotalMb"] = round(m["MemTotal"] / 1024)
            out["memAvailMb"] = round(m.get("MemAvailable", m["MemFree"]) / 1024)
            out["memUsedPct"] = round(100 * (1 - m.get("MemAvailable", m["MemFree"]) / m["MemTotal"]), 1)
            out["load"] = [float(x) for x in cur["load"]]
            pp, cp = self.prev["proc"], cur["proc"]
            if pp and cp:
                out["procCpu"] = round((cp["cpu"] - pp["cpu"]) / el * 100, 1)
                out["procRssMb"] = round(cp["rss"] or 0)
                out["procThreads"] = cp["threads"]
                groups = {}
                for tid, (comm, state, t) in cp["tasks"].items():
                    g = thread_group(comm)
                    e = groups.setdefault(g, {"cpu": 0.0, "threads": 0, "running": 0})
                    e["threads"] += 1
                    e["running"] += 1 if state == "R" else 0
                    prevt = pp["tasks"].get(tid)
                    if prevt:
                        e["cpu"] += (t - prevt[2]) / el * 100
                out["threadGroups"] = {k: {**v, "cpu": round(v["cpu"], 1)} for k, v in groups.items()}
            s0, s1 = self.first["self"], cur["self"]          # since start: os.times() has 10 ms resolution
            out["monitorCpu"] = round(((s1.user - s0.user) + (s1.system - s0.system)) / max(0.1, now - self.first["t"]) * 100, 2)
        else:
            self.first = cur
        self.prev = cur
        return out


# ============================================================================ hardware
def hardware():
    hw = {"arch": platform.machine(), "cpus": os.cpu_count(), "python": platform.python_version()}
    try:
        hw["cpusUsable"] = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        hw["cpusUsable"] = hw["cpus"]
    try:
        hw["glibc"] = os.confstr("CS_GNU_LIBC_VERSION")
    except (ValueError, OSError, AttributeError):
        hw["glibc"] = None
    model = None
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.lower().startswith(("model name", "hardware", "cpu part")) and not model:
                    model = line.split(":", 1)[1].strip()
    except OSError:
        pass
    hw["cpuModel"] = model
    # big.LITTLE: cpu_capacity (ARM) or the maximum clock per CPU
    caps = {}
    for n in range(hw["cpus"] or 0):
        for f in (f"/sys/devices/system/cpu/cpu{n}/cpu_capacity", f"/sys/devices/system/cpu/cpu{n}/cpufreq/cpuinfo_max_freq"):
            try:
                caps[n] = int(open(f).read().strip())
                break
            except (OSError, ValueError):
                continue
    hw["capacity"] = caps
    if caps and len(set(caps.values())) > 1:
        top = max(caps.values())
        hw["bigCores"] = sorted(n for n, v in caps.items() if v == top)
        hw["littleCores"] = sorted(n for n, v in caps.items() if v != top)
    try:
        import importlib.util
        hw["openvinoInstalled"] = importlib.util.find_spec("openvino") is not None
    except Exception:  # noqa: BLE001
        hw["openvinoInstalled"] = None
    return hw


def cpu_list_text(cpus):
    cpus = sorted(cpus)
    out, i = [], 0
    while i < len(cpus):
        j = i
        while j + 1 < len(cpus) and cpus[j + 1] == cpus[j] + 1:
            j += 1
        out.append(f"{cpus[i]}-{cpus[j]}" if j > i else str(cpus[i]))
        i = j + 1
    return ",".join(out)


# ============================================================================ live view
def fmt(v, unit="", nd=1):
    if v is None:
        return "-"
    if isinstance(v, float):
        v = round(v, nd)
    return f"{v}{unit}"


def render(sample, metrics, rt, prev_metrics=None):
    lines = []
    s = sample or {}
    head = f"[{ts()}] CPU {fmt(s.get('cpuTotal'), '%')}"
    if s.get("cores"):
        head += " | cores " + " ".join(f"{x:>3}" for x in s["cores"])
    if s.get("load"):
        head += f" | load {s['load'][0]:.2f} {s['load'][1]:.2f} {s['load'][2]:.2f}"
    if s.get("memTotalMb"):
        head += f" | RAM {s['memUsedPct']}% used, {s['memAvailMb']} MB free"
    lines.append(c(head, "1"))
    if s.get("procCpu") is not None:
        a = (rt or {}).get("applied") or (metrics or {}).get("runtime") or {}
        gate = (rt or {}).get("gate") or (a.get("gate") if isinstance(a.get("gate"), dict) else {}) or {}
        lim = a.get("inferConcurrency")
        lines.append(f"    backend: CPU {s['procCpu']}% ({s['procCpu'] / 100:.1f} cores) RSS {s['procRssMb']} MB threads {s['procThreads']}"
                     f" | torch intra-op {fmt(a.get('torchThreads'))} inter-op {fmt(a.get('torchInteropThreads'))}"
                     f" | inference slots {lim or 'unlimited'}"
                     + (f" (waiting {gate.get('waiting', 0)})" if lim else "")
                     + f" | OpenCV {fmt(a.get('cvThreads'))} | CPUs {a.get('cpuAffinity') or '-'}"
                     + f" | monitor {s.get('monitorCpu', 0)}%")
        tg = sorted(((k, v) for k, v in (s.get("threadGroups") or {}).items() if v["cpu"] >= 0.5), key=lambda kv: -kv[1]["cpu"])
        if tg:
            lines.append("    threads: " + "  ".join(f"{k} {v['cpu']:.0f}%[{v['threads']}t,{v['running']}R]" for k, v in tg[:10]))
    if metrics is None:
        lines.append(c("    backend API not answering", "1;31"))
        return lines
    pm = {x["cameraId"]: x for x in (prev_metrics or {}).get("sessions", [])}
    for x in metrics.get("sessions", []):
        st = x.get("stageMs") or {}
        drops = x.get("framesDropped", 0)
        dps = None
        p = pm.get(x["cameraId"])
        if p is not None and sample:
            dps = (drops - p.get("framesDropped", 0)) / max(0.5, sample.get("seconds", 1))
        stages = " ".join(f"{k[:4]} {st[k]:.0f}" for k in ("preprocess", "inference", "postprocess", "tracking") if st.get(k) is not None)
        extra = []
        if st.get("humanCount"):
            extra.append(f"count {st['humanCount']:.0f}")
        if st.get("objectLayer1"):
            extra.append(f"obj {st['objectLayer1']:.0f}")
        if st.get("slotWait"):
            extra.append(f"slot-wait {st['slotWait']:.0f}")
        if st.get("readerCpu") is not None:
            extra.append(f"reader {st['readerCpu']:.0f}")
        lines.append(f"    {x['cameraId']:<10} {','.join(x.get('modules', [])):<18} {x.get('state', ''):<10}"
                     f" recv {fmt(x.get('receivedFps'))} fps  AI {fmt(x.get('processedFps'))} fps"
                     f" | infer {fmt(x.get('inferenceMs'))} ms ({stages}{'; ' + ', '.join(extra) if extra else ''})"
                     f" | queue {x.get('queueLen', 0)}/{x.get('queueCapacity') or '-'}"
                     f" dropped {fmt(dps, '/s') if dps is not None else drops}"
                     f" | latency {fmt(x.get('latencyMs'), ' ms', 0)}"
                     + (f" | fire {fmt(x.get('fireAnalysedFps'))} fps" if x.get("fireAnalysedFps") is not None else "")
                     + (f" | AI stream {x.get('aiResolution')}" if x.get("aiResolution") else ""))
    if not metrics.get("sessions"):
        lines.append("    no camera session running")
    return lines


def cmd_monitor(a):
    pid = backend_pid()
    if not pid:
        warn("backend not running - showing system values only (start it with: perf-tune.sh start)")
    smp = Sampler(pid)
    smp.sample()
    prev = None
    end = time.time() + a.duration if a.duration else None
    logf = open(a.log, "a", encoding="utf-8") if a.log else None
    try:
        while end is None or time.time() < end:
            time.sleep(max(1.0, a.interval))
            if not smp.pid or not os.path.exists(f"/proc/{smp.pid}"):
                smp.pid = backend_pid()
            s = smp.sample()
            st, m = api("/system/metrics", 4)
            m = m if st == 200 else None
            st2, rt = api("/system/runtime", 4)
            rt = rt if st2 == 200 else None
            print("\n".join(render(s, m, rt, prev)), flush=True)
            if logf:
                logf.write(json.dumps({"time": ts(), "system": s, "metrics": m, "runtime": rt}) + "\n")
                logf.flush()
            prev = m
    except KeyboardInterrupt:
        pass
    finally:
        if logf:
            logf.close()


# ============================================================================ live measurement
def measure_live(seconds, interval=2.0, quiet=False):
    """Average the application's own metrics and /proc over `seconds`. Returns a summary dict."""
    pid = backend_pid()
    smp = Sampler(pid)
    smp.sample()
    st, m0 = api("/system/metrics", 5)
    if st != 200:
        return None
    t0 = time.time()
    rows, sys_rows = [], []
    while time.time() - t0 < seconds:
        time.sleep(interval)
        s = smp.sample()
        st, m = api("/system/metrics", 5)
        if st == 200:
            rows.append(m)
            sys_rows.append(s)
            if not quiet:
                print("\n".join(render(s, m, None, rows[-2] if len(rows) > 1 else m0)), flush=True)
    if not rows:
        return None
    el = time.time() - t0
    m1 = rows[-1]
    s0 = {x["cameraId"]: x for x in m0.get("sessions", [])}
    cams = {}
    for x in m1.get("sessions", []):
        cid = x["cameraId"]
        a0 = s0.get(cid, {})
        ser = [next((y for y in r.get("sessions", []) if y["cameraId"] == cid), {}) for r in rows]

        def avg(key, sub=None, series=ser):
            vals = [(y.get(sub) or {}).get(key) if sub else y.get(key) for y in series]
            vals = [v for v in vals if isinstance(v, (int, float))]
            return round(statistics.mean(vals), 1) if vals else None
        rec = x.get("framesReceived", 0) - a0.get("framesReceived", 0)
        proc = x.get("framesProcessed", 0) - a0.get("framesProcessed", 0)
        cams[cid] = {"modules": x.get("modules", []), "state": x.get("state"),
                     "receivedFps": round(rec / el, 2), "processedFps": round(proc / el, 2),
                     "inferenceMs": avg("inferenceMs"), "modelMs": avg("inference", "stageMs"),
                     "preprocessMs": avg("preprocess", "stageMs"), "trackingMs": avg("tracking", "stageMs"),
                     "slotWaitMs": avg("slotWait", "stageMs"), "latencyMs": avg("latencyMs"),
                     "droppedPerS": round((x.get("framesDropped", 0) - a0.get("framesDropped", 0)) / el, 2),
                     "fireFps": avg("fireAnalysedFps"), "aiResolution": x.get("aiResolution"),
                     "sharedPersonPass": x.get("sharedPersonPass")}
    sv = lambda k: round(statistics.mean([s[k] for s in sys_rows if s and s.get(k) is not None]), 1) if any(s and s.get(k) is not None for s in sys_rows) else None
    groups = {}
    for s in sys_rows:
        for k, v in ((s or {}).get("threadGroups") or {}).items():
            groups.setdefault(k, []).append(v["cpu"])
    return {"time": ts(), "seconds": round(el), "cameras": cams,
            "processedFpsTotal": round(sum(v["processedFps"] for v in cams.values()), 2),
            "receivedFpsTotal": round(sum(v["receivedFps"] for v in cams.values()), 2),
            "systemCpu": sv("cpuTotal"), "processCpu": sv("procCpu"), "processRssMb": sv("procRssMb"),
            "memUsedPct": sv("memUsedPct"),
            "load1": round(statistics.mean([s["load"][0] for s in sys_rows if s] or [0]), 2),
            "threadGroups": {k: round(statistics.mean(v), 1) for k, v in groups.items()},
            "monitorCpu": sv("monitorCpu"), "settings": current_settings(),
            "runtime": (api("/system/runtime", 4)[1] or {}).get("applied")}


def running_modules():
    st, m = api("/system/metrics", 5)
    if st != 200:
        return {}
    return {x["cameraId"]: list(x.get("modules", [])) for x in m.get("sessions", []) if x.get("modules")}


# ============================================================================ findings
def analyse(meas, hw, rt):
    out = []
    if not meas:
        return [("info", "no live measurement (backend not running or no camera session)")]
    ncpu = hw.get("cpusUsable") or hw.get("cpus") or 1
    syscpu = meas.get("systemCpu") or 0
    a = (rt or {}).get("applied") or meas.get("runtime") or {}
    cams = meas.get("cameras", {})
    for cid, x in cams.items():
        if x["receivedFps"] and x["processedFps"] < 0.95 * x["receivedFps"] and x.get("inferenceMs"):
            need = 1000.0 / x["receivedFps"]
            out.append(("limit", f"{cid}: {x['receivedFps']} fps arrive, {x['processedFps']} fps analysed. Analysing every "
                                 f"frame needs <= {need:.0f} ms per frame; now {x['inferenceMs']:.0f} ms. The rest are dropped "
                                 f"(counted) - expected while one frame takes longer than the gap between frames."))
    total_threads = None
    if a.get("torchThreads"):
        callers = sum(1 for x in cams.values() if any(m in x["modules"] for m in ("human", "vehicle", "count"))) + \
            sum(1 for x in cams.values() if "fire" in x["modules"])
        lim = a.get("inferConcurrency") or 0
        active = min(callers, lim) if lim else callers
        total_threads = active * a["torchThreads"]
        if active and total_threads > 1.5 * ncpu:
            out.append(("cpu", f"{active} model calls can run at once with {a['torchThreads']} PyTorch threads each = "
                               f"{total_threads} threads on {ncpu} CPUs: oversubscribed. Threads wait for each other; "
                               f"run 'bench' to measure a better thread count / inference concurrency."))
    if syscpu and syscpu < 75 and any(x["processedFps"] < 0.95 * x["receivedFps"] for x in cams.values()):
        out.append(("cpu", f"system CPU only {syscpu}% while frames are dropped: the CPUs are not the limit, the threads "
                           f"are waiting (thread contention, slow cores, or OpenMP spin-waiting). 'bench' measures this."))
    if syscpu and syscpu >= 90:
        out.append(("cpu", f"system CPU {syscpu}%: the CPUs are saturated - fewer pixels to decode (AI substream), lower camera "
                           f"frame rate, or fewer modules per camera are the remaining levers; thread settings cannot add CPU."))
    tg = meas.get("threadGroups") or {}
    dec = sum(v for k, v in tg.items() if k.startswith(("decode", "rd:", "hd:")))
    if dec and meas.get("processCpu") and dec > 0.25 * meas["processCpu"]:
        out.append(("decode", f"video decoding uses {dec:.0f}% CPU ({dec / max(1, meas['processCpu']) * 100:.0f}% of the backend). "
                              f"An AI substream (camera form > AI detection stream) decodes ~6-9x fewer pixels."))
    hd = sum(v for k, v in tg.items() if k.startswith("hd:"))
    if hd > 10:
        out.append(("decode", f"the main stream is decoded next to the AI substream for HD snapshot originals ({hd:.0f}% CPU). "
                              f"'Boxes only' snapshots in the camera form save this."))
    for cid, x in cams.items():
        if "count" in x["modules"] and ("human" in x["modules"] or "vehicle" in x["modules"]) and x.get("sharedPersonPass") is False:
            out.append(("model", f"{cid}: Human Count and Human/Vehicle detection run two model passes - set AIV_SHARED_PERSON_INFERENCE=1."))
        if (x.get("aiResolution") or "").startswith(("1920", "2560", "2688", "3840")) and x["processedFps"] < x["receivedFps"]:
            out.append(("decode", f"{cid}: AI analyses the {x['aiResolution']} main stream; the model input is 640 px anyway."))
        if x.get("slotWaitMs") and x.get("inferenceMs") and x["slotWaitMs"] > x["inferenceMs"]:
            out.append(("cpu", f"{cid}: waits {x['slotWaitMs']:.0f} ms per frame for an inference slot - AIV_INFER_CONCURRENCY may be too low."))
    if (meas.get("memUsedPct") or 0) > 85:
        out.append(("memory", f"RAM {meas['memUsedPct']}% used - no headroom for more threads / models."))
    eng = (rt or {}).get("engine")
    if eng == "OpenVINO":
        out.append(("engine", "the person/vehicle model runs on OpenVINO, which manages its own threads: the PyTorch thread "
                              "settings only affect the models still on PyTorch; AIV_INFER_CONCURRENCY affects all."))
    if eng == "PyTorch" and hw.get("arch") in ("aarch64", "arm64"):
        g = hw.get("glibc") or ""
        mver = re.search(r"(\d+)\.(\d+)", g)
        if mver and (int(mver.group(1)), int(mver.group(2))) >= (2, 35) and not hw.get("openvinoInstalled"):
            out.append(("engine", f"models run on PyTorch; this box has {g}, so the OpenVINO aarch64 wheel can be installed "
                                  f"(backend/.venv/bin/pip install openvino==<version in backend/requirements-lock.txt>, then restart) - "
                                  f"it was several times faster than PyTorch on x86 in our tests; measure it here with 'status' / 'monitor'."))
        elif mver:
            out.append(("engine", f"models run on PyTorch ({g}: OpenVINO aarch64 wheels need glibc 2.35+, e.g. Debian 12)."))
    if hw.get("bigCores"):
        out.append(("cpu", f"CPU has fast cores {cpu_list_text(hw['bigCores'])} and slow cores {cpu_list_text(hw['littleCores'])}: "
                           f"a model call split over both waits for the slow ones - 'bench' also tests the fast cores only."))
    return out or [("ok", "no bottleneck found in the measured values")]


def print_findings(fs):
    tag = {"limit": c("LIMIT ", "1;36"), "cpu": c("CPU   ", "1;33"), "decode": c("DECODE", "1;33"), "model": c("MODEL ", "1;33"),
           "memory": c("MEMORY", "1;31"), "engine": c("ENGINE", "1;35"), "info": "INFO  ", "ok": c("OK    ", "1;32")}
    for k, t in fs:
        print(f"  {tag.get(k, k)} {t}")


# ============================================================================ app control
def wait_api(timeout):
    end = time.time() + timeout
    while time.time() < end:
        st, _ = api("/system/health", 3)
        if st == 200:
            return True
        time.sleep(1)
    return False


def sudo_cmd(args):
    return args if os.geteuid() == 0 else ["sudo"] + args


def alive(pid):
    """True while the process runs (a finished child of this tool is reaped, not counted as running)."""
    try:
        if os.waitpid(pid, os.WNOHANG)[0] == pid:
            return False
    except ChildProcessError:
        pass
    except OSError:
        return False
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:"):
                    return " Z " not in line and "zombie" not in line
    except OSError:
        return False
    return True


def managed_pid():
    try:
        with open(APP_PIDFILE) as f:
            pid = int(f.read().strip())
        return pid if alive(pid) else None
    except (OSError, ValueError):
        return None


def start_app(wait=90):
    if wait_api(1):
        say("application already running")
        return True
    ensure_dirs()
    if systemd_mode():
        say(f"starting the systemd services ({BACKEND_UNIT}, {FRONTEND_UNIT})")
        subprocess.run(sudo_cmd(["systemctl", "start", BACKEND_UNIT, FRONTEND_UNIT]), check=False)
    else:
        logp = os.path.join(PERF_DIR, "app.log")
        say(f"starting ./start.sh --no-browser (log: {os.path.relpath(logp, REPO)})")
        lf = open(logp, "a")
        p = subprocess.Popen([os.path.join(REPO, "start.sh"), "--no-browser"], cwd=REPO, stdout=lf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        with open(APP_PIDFILE, "w") as f:
            f.write(str(p.pid))
    ok = wait_api(wait)
    say("backend answering" if ok else "backend did not answer in time - see the log")
    return ok


def stop_app(timeout=40):
    if systemd_mode():
        say("stopping the systemd services (clean shutdown: SIGINT to the backend)")
        subprocess.run(sudo_cmd(["systemctl", "stop", BACKEND_UNIT, FRONTEND_UNIT]), check=False)
        return True
    pid = managed_pid()
    if not pid:
        bp = backend_pid()
        if bp:
            warn(f"the backend (PID {bp}) was not started by this tool - stop it in its own terminal with Ctrl+C "
                 f"(or install the services: scripts/debian/install-services.sh)")
            return False
        say("application not running")
        return True
    say(f"stopping ./start.sh (process group {pid}) with Ctrl+C semantics")
    try:
        os.killpg(pid, signal.SIGINT)
    except OSError:
        pass
    end = time.time() + timeout
    while time.time() < end:
        if not alive(pid):
            break
        time.sleep(0.5)
    else:
        warn("still running - sending SIGTERM")
        try:
            os.killpg(pid, signal.SIGTERM)
        except OSError:
            pass
        time.sleep(5)
    try:
        os.remove(APP_PIDFILE)
    except OSError:
        pass
    return True


def restart_backend(wait=120):
    """Controlled restart so the backend reads .env again. Detections that were running are resumed by the
    backend itself (data/running_modules.json); the caller re-checks that."""
    if systemd_mode():
        say(f"restarting {BACKEND_UNIT}")
        r = subprocess.run(sudo_cmd(["systemctl", "restart", BACKEND_UNIT]), check=False)
        if r.returncode != 0:
            return False
    elif managed_pid():
        stop_app()
        start_app(wait)
    else:
        return False
    time.sleep(2)
    return wait_api(wait)


def resume_modules(expected, timeout=90):
    """Wait until the detections that ran before the restart run again; start the missing ones via the API."""
    end = time.time() + timeout
    kicked = False
    while time.time() < end:
        now = running_modules()
        missing = {cid: [m for m in mods if m not in now.get(cid, [])] for cid, mods in expected.items()}
        missing = {k: v for k, v in missing.items() if v}
        if not missing:
            return True
        if not kicked and time.time() > end - timeout + 40:      # backend auto-resume did not do it (AIV_AUTOSTART=0?)
            for cid, mods in missing.items():
                for m in mods:
                    api(f"/detections/{m}/start", 60, "POST", {"cameraId": cid})
            kicked = True
        time.sleep(3)
    return False


# ============================================================================ frames
def cmd_frames(a, quiet=False):
    ensure_dirs()
    st, m = api("/system/metrics", 5)
    if st != 200:
        fail("backend not running - start it first (perf-tune.sh start), or pass --frames-dir / --video to bench")
    cams = [x["cameraId"] for x in m.get("sessions", []) if x.get("modules")][: a.max_cameras]
    if not cams:
        fail("no camera has a running detection - start a detection in the UI first")
    out = os.path.join(PERF_DIR, "frames", stamp())
    os.makedirs(out)
    n = 0
    for i in range(a.per_camera):
        for cid in cams:
            st, data = api(f"/cameras/{cid}/frame.jpg", 10)
            if st == 200 and isinstance(data, (bytes, bytearray)):
                safe = re.sub(r"[^A-Za-z0-9_.-]", "_", cid)
                with open(os.path.join(out, f"{safe}-{i:03d}.jpg"), "wb") as f:
                    f.write(data)
                n += 1
        time.sleep(a.every)
    if not quiet:
        say(f"saved {n} frames from {len(cams)} camera(s) to {os.path.relpath(out, REPO)}")
    return out if n else None


def latest_frames_dir():
    dirs = sorted(d for d in glob.glob(os.path.join(PERF_DIR, "frames", "*")) if os.path.isdir(d))
    return dirs[-1] if dirs else None


def frames_from_video(video, n=16):
    import cv2  # noqa: PLC0415  (venv python)
    out = os.path.join(PERF_DIR, "frames", f"video-{stamp()}")
    os.makedirs(out)
    cap = cv2.VideoCapture(video)
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    step = max(1, total // n) if total else 15
    i = k = 0
    while k < n:
        ok, f = cap.read()
        if not ok:
            break
        if i % step == 0:
            cv2.imwrite(os.path.join(out, f"video-{k:03d}.jpg"), f, [cv2.IMWRITE_JPEG_QUALITY, 95])
            k += 1
        i += 1
    return out if k else None


# ============================================================================ benchmark
def settings_label(s):
    parts = []
    for k in KEYS:
        v = s.get(k, "")
        if v not in ("", None):
            parts.append(f"{SHORT[k]}={v}")
    return " ".join(parts) or "application defaults"


def candidates(hw, streams, current):
    """Safe CPU configurations to measure. Each keeps (threads per call) x (calls at once) <= usable CPUs,
    except the current configuration, which is always measured as the reference."""
    ncpu = hw.get("cpusUsable") or hw.get("cpus") or 1
    S = max(1, streams)
    cands = [{"name": "current", **{k: current.get(k, "") for k in KEYS}}]
    tvals = sorted({t for t in (1, 2, 3, 4, 6, 8, 12, 16) if t <= ncpu} | {ncpu})
    gvals = [0] + [g for g in (1, 2, 3, 4) if g < S]
    seen = set()
    for g in gvals:
        active = S if g == 0 else g
        for t in tvals:
            used = t * active
            # never fewer busy threads than half the CPUs, never more than the CPUs - except one thread per
            # call, the smallest setting there is (more cameras than CPUs)
            if used < max(1, ncpu // 2) or (t > 1 and used > ncpu):
                continue
            key = (t, g)
            if key in seen:
                continue
            seen.add(key)
            cands.append({"name": f"t{t}-c{g or 'all'}", "AIV_TORCH_THREADS": str(t), "AIV_TORCH_INTEROP_THREADS": "1",
                          "AIV_INFER_CONCURRENCY": str(g) if g else "", "AIV_CV_THREADS": "", "AIV_CPU_AFFINITY": "",
                          "AIV_OMP_WAIT_POLICY": ""})
    return cands


def variants(best, hw, streams):
    """Second round on top of the best configuration: OpenMP passive waiting, fast cores only."""
    out = []
    v = dict(best)
    v["name"] = best["name"] + "+passive"
    v["AIV_OMP_WAIT_POLICY"] = "passive"
    out.append(v)
    if hw.get("bigCores"):
        big = hw["bigCores"]
        g = int(best.get("AIV_INFER_CONCURRENCY") or 0) or max(1, streams)
        t = max(1, len(big) // g)
        b = dict(best)
        b.update({"name": f"t{t}-c{g}-bigcores", "AIV_TORCH_THREADS": str(t), "AIV_INFER_CONCURRENCY": str(g) if g < streams else "",
                  "AIV_CPU_AFFINITY": cpu_list_text(big), "AIV_TORCH_INTEROP_THREADS": "1"})
        out.append(b)
    return out


def run_worker(cand, job, timeout):
    env = dict(os.environ)
    for k in KEYS:
        env.pop(k, None)
    env.pop("OMP_NUM_THREADS", None)
    env.pop("OMP_WAIT_POLICY", None)
    env.pop("AIV_FIRE_THREADS", None)
    for k in KEYS:
        if cand.get(k) not in (None, ""):
            env[k] = str(cand[k])
    # an empty value in the environment wins over .env and means "application default"
    for k in KEYS:
        env.setdefault(k, "")
    if job.get("engine"):
        env["AIV_ENGINE"] = env["AIV_FIRE_ENGINE"] = job["engine"]
    if cand["name"] != "current":
        env["AIV_FIRE_THREADS"] = ""        # older alias of AIV_TORCH_THREADS: only the reference keeps it
    ensure_dirs()
    jp = os.path.join(PERF_DIR, "bench", f"job-{os.getpid()}.json")
    with open(jp, "w") as f:
        json.dump({**job, "label": cand["name"]}, f)
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    try:
        r = subprocess.run([py, WORKER, jp], cwd=REPO, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"error": f"timed out after {timeout} s"}
    for line in reversed(r.stdout.splitlines()):
        if line.startswith("RESULT "):
            return json.loads(line[7:])
    tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
    return {"error": f"worker exit {r.returncode}: {' | '.join(tail)}"}


def iou(a, b):
    ix1, iy1, ix2, iy2 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix2 - ix1) * max(0.0, iy2 - iy1)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def compare_detections(ref, other, thr):
    """Same detections? Boxes of the same class matched by IoU >= 0.95 and |conf diff| <= 0.02. A box that exists
    in only one result counts as a mismatch unless its confidence is within 0.02 of the threshold (a borderline
    box that float rounding moved across the threshold)."""
    if ref is None or other is None or len(ref) != len(other):
        return {"ok": False, "reason": "missing detections"}
    total = mism = border = 0
    max_dconf = 0.0
    for fa, fb in zip(ref, other):
        used = set()
        total += len(fa)
        for d in fa:
            best, bi = 0.0, None
            for j, e in enumerate(fb):
                if j in used or e[5] != d[5]:
                    continue
                v = iou(d, e)
                if v > best:
                    best, bi = v, j
            if bi is not None and best >= 0.95 and abs(fb[bi][4] - d[4]) <= 0.02:
                used.add(bi)
                max_dconf = max(max_dconf, abs(fb[bi][4] - d[4]))
            elif d[4] - thr <= 0.02:
                border += 1
            else:
                mism += 1
        for j, e in enumerate(fb):
            if j not in used:
                if e[4] - thr <= 0.02:
                    border += 1
                else:
                    mism += 1
    return {"ok": mism == 0, "boxes": total, "mismatches": mism, "borderline": border, "maxConfDiff": round(max_dconf, 4)}


def cmd_bench(a, live_ctx=None):
    hw = hardware()
    ensure_dirs()
    frames_dir = a.frames_dir
    if a.video:
        frames_dir = frames_from_video(a.video)
    if not frames_dir:
        frames_dir = latest_frames_dir()
    if not frames_dir:
        say("no saved frames - taking representative frames from the running cameras")
        frames_dir = cmd_frames(argparse.Namespace(per_camera=6, every=1.0, max_cameras=4))
    frames_dir = os.path.abspath(frames_dir)
    frames = sorted(glob.glob(os.path.join(frames_dir, "*.jpg")) + glob.glob(os.path.join(frames_dir, "*.png")))
    if not frames:
        fail(f"no frames in {frames_dir}")
    mods = running_modules()
    streams = a.streams or sum(1 for v in mods.values() if any(m in v for m in ("human", "vehicle", "count"))) or 1
    use_count = a.count if a.count is not None else any("count" in v for v in mods.values())
    use_fire = a.fire if a.fire is not None else any("fire" in v for v in mods.values())
    running = backend_pid() is not None and bool(mods)
    paused = []
    if running and a.maintenance:
        say("maintenance mode: pausing the running detections for the benchmark (they are resumed afterwards)")
        for cid, ms in mods.items():
            for m in ms:
                st, _ = api(f"/detections/{m}/pause", 20, "POST", {"cameraId": cid})
                if st == 200:
                    paused.append((cid, m))
    idle = running and not a.maintenance
    current = current_settings()
    cands = candidates(hw, streams + (1 if use_fire else 0), current)
    if a.threads:
        keep = {int(x) for x in a.threads.split(",")}
        cands = [x for x in cands if x["name"] == "current" or int(x.get("AIV_TORCH_THREADS") or 0) in keep]
    say(f"benchmark: {len(frames)} frames ({os.path.relpath(frames_dir, REPO)}), {streams} camera stream(s)"
        f"{' + Human Count' if use_count else ''}{' + fire model' if use_fire else ''}, {len(cands)} configurations x "
        f"{a.rounds} round(s) x {a.seconds} s, {hw.get('cpusUsable')} CPUs")
    if idle:
        warn("the application is running: benchmark processes run at idle priority (never take CPU from it). "
             "Numbers are then relative (measured on the CPU the application leaves free); rounds are interleaved so "
             "every configuration sees the same load. For absolute numbers use --maintenance (pauses detection).")
    job = {"frames": frames, "streams": streams, "count": use_count, "fire": use_fire, "seconds": a.seconds,
           "warmup": 3, "idle": idle, "accuracy": True}
    # same inference engine as the running backend (its environment may differ from this shell's)
    st_, rt_ = api("/system/runtime", 5)
    eng = (rt_ or {}).get("engine") if st_ == 200 else None
    if eng:
        job["engine"] = {"OpenVINO": "openvino", "PyTorch": "pytorch"}.get(eng)
        say(f"inference engine of the running backend: {eng} (the benchmark uses the same)")
    results = {x["name"]: [] for x in cands}
    meta = {}
    try:
        def one_round(cs, r):
            for cand in cs:
                avail = _meminfo().get("MemAvailable", 0) / 1024
                need = max([600] + [x.get("rssMaxMb", 0) for v in results.values() for x in v]) + 400
                if avail < need:
                    warn(f"only {avail:.0f} MB RAM free (need ~{need:.0f} MB) - skipping {cand['name']}")
                    continue
                res = run_worker(cand, job, timeout=a.seconds * 4 + 300)
                res["round"] = r
                results.setdefault(cand["name"], []).append(res)
                meta[cand["name"]] = cand
                if res.get("error"):
                    warn(f"{cand['name']}: {res['error']}")
                else:
                    say(f"  round {r} {cand['name']:<22} {settings_label(cand):<44} {res['fpsTotal']:6.2f} fps total "
                        f"({' / '.join(str(v) for v in res['fpsPerStream'])}) p50 {res['latencyMsP50']} ms p95 {res['latencyMsP95']} ms "
                        f"CPU {res['cpuPct']}% RSS {res['rssMaxMb']} MB"
                        + (f" | fire {res['fireFps']} fps" if res.get('fireFps') is not None else ""))

        for r in range(1, a.rounds + 1):
            order = cands if r % 2 else list(reversed(cands))      # interleave: cancel slow drifts in load
            one_round(order, r)
        table = summarise(results, meta)
        ok_rows = [t for t in table if t["ok"]]
        if ok_rows and not a.no_variants:
            best = max(ok_rows, key=lambda t: t["fps"])
            vs = variants(meta[best["name"]], hw, streams + (1 if use_fire else 0))
            vs = [v for v in vs if v["name"] not in meta]
            if vs:
                say(f"second stage on top of {best['name']}: " + ", ".join(v["name"] for v in vs))
                for r in range(1, a.rounds + 1):        # the best one runs again next to its variants (same load)
                    one_round(vs + [meta[best["name"]]], 100 + r)
        table = summarise(results, meta)
    finally:
        for cid, m in paused:
            api(f"/detections/{m}/resume", 20, "POST", {"cameraId": cid})
        if paused:
            say(f"resumed {len(paused)} paused detection(s)")
    ref = next((t for t in table if t["name"] == "current"), None)
    choice, why = choose(table, ref, a.min_gain)
    rep = {"time": ts(), "hardware": hw, "frames": frames_dir, "streams": streams, "count": use_count, "fire": use_fire,
           "idlePriority": idle, "maintenance": bool(a.maintenance), "rounds": a.rounds, "seconds": a.seconds,
           "table": table, "choice": choice, "why": why, "raw": results}
    path = os.path.join(PERF_DIR, "bench", f"bench-{stamp()}.json")
    with open(path, "w") as f:
        json.dump(rep, f, indent=1)
    print_bench(table, choice, why)
    spread = max([t.get("spread") or 0 for t in table if not t.get("error")] + [0])
    if spread > 0.10:
        warn(f"measurements varied up to {spread * 100:.0f}% between rounds"
             + (" (the running application changes the free CPU)" if idle else "")
             + " - for firmer numbers: more --rounds / --seconds" + ("" if a.maintenance else ", or --maintenance"))
    say(f"benchmark saved: {os.path.relpath(path, REPO)}")
    st = load_state()
    st["lastBench"] = path
    save_state(st)
    return rep


def summarise(results, meta):
    """Median over rounds per configuration; accuracy vs the reference ('current') run."""
    ref_det = None
    for r in results.get("current", []):
        if r.get("detections") is not None:
            ref_det, thr = r["detections"], r.get("predictConf", 0.35)
            break
    table = []
    for name, rs in results.items():
        good = [r for r in rs if not r.get("error")]
        if not good:
            table.append({"name": name, "ok": False, "fps": 0, "error": (rs[-1].get("error") if rs else "not run")})
            continue
        fps = [r["fpsTotal"] for r in good]
        acc = {"ok": True, "note": "reference"} if name == "current" else (
            compare_detections(ref_det, good[0].get("detections"), thr) if ref_det is not None else {"ok": True, "note": "no reference"})
        spread = (max(fps) - min(fps)) / statistics.median(fps) if len(fps) > 1 and statistics.median(fps) > 0 else None
        table.append({
            "name": name, "settings": {k: meta.get(name, {}).get(k, "") for k in KEYS}, "label": settings_label(meta.get(name, {})),
            "rounds": len(good), "fps": round(statistics.median(fps), 2), "fpsAll": fps, "spread": round(spread, 3) if spread is not None else None,
            "p50": round(statistics.median([r["latencyMsP50"] for r in good if r.get("latencyMsP50") is not None] or [0]), 1),
            "p95": round(statistics.median([r["latencyMsP95"] for r in good if r.get("latencyMsP95") is not None] or [0]), 1),
            "cpu": round(statistics.median([r["cpuPct"] for r in good]), 1), "rss": max(r["rssMaxMb"] for r in good),
            "cpuMsPerFrame": round(statistics.median([r["cpuMsPerFrame"] for r in good]), 1),
            "fireFps": statistics.median([r["fireFps"] for r in good]) if good[0].get("fireFps") is not None else None,
            "stageMs": good[-1].get("stageMs"), "applied": good[-1].get("settings"),
            "accuracy": acc, "ok": acc.get("ok", False) and not any(r.get("errors") for r in good),
            "warnings": sorted({e for r in good for e in r.get("errors", [])}),
        })
    table.sort(key=lambda t: -t["fps"])
    return table


def choose(table, ref, min_gain):
    if not ref or not ref.get("ok"):
        return None, "the current configuration could not be measured - nothing to compare with"
    best = None
    for t in table:
        if t["name"] == "current" or not t["ok"]:
            continue
        if t["rss"] > ref["rss"] * 1.25 + 150:
            continue
        if ref["p95"] and t["p95"] > ref["p95"] * 1.5:
            continue
        if ref.get("fireFps") and t.get("fireFps") is not None and t["fireFps"] < ref["fireFps"] * 0.8:
            continue
        if best is None or t["fps"] > best["fps"]:
            best = t
    if best is None:
        return None, "no other configuration passed the accuracy / memory / latency checks"
    gain = best["fps"] / ref["fps"] - 1 if ref["fps"] else 0
    # the gain must also exceed the round-to-round spread measured for these two configurations
    noise = max(ref.get("spread") or 0.0, best.get("spread") or 0.0)
    need = max(min_gain, noise)
    if gain < need:
        return None, (f"best other configuration {best['name']} ({best['fps']} fps) is only {gain * 100:+.1f}% vs current "
                      f"{ref['fps']} fps - below the required {need * 100:.0f}% (min gain / measurement spread): keep current")
    return best, f"{best['name']}: {best['fps']} fps vs {ref['fps']} fps current ({gain * 100:+.1f}%), detections identical " \
                 f"({best['accuracy'].get('boxes', '?')} boxes, max confidence difference {best['accuracy'].get('maxConfDiff', 0)})"


def print_bench(table, choice, why):
    print()
    print(f"  {'configuration':<26} {'settings':<46} {'fps':>7} {'p50 ms':>7} {'p95 ms':>7} {'CPU%':>6} {'RSS MB':>7} {'spread':>7}  accuracy")
    for t in table:
        if t.get("error"):
            print(f"  {t['name']:<26} {'':<46} {'error':>7}  {t['error']}")
            continue
        acc = t["accuracy"]
        at = acc.get("note") or (f"identical ({acc.get('boxes')} boxes)" if acc.get("ok") and not acc.get("borderline")
                                 else f"{acc.get('mismatches', '?')} mismatches, {acc.get('borderline', 0)} borderline")
        mark = c("*", "1;32") if choice and t["name"] == choice["name"] else " "
        sp = f"{t['spread'] * 100:.0f}%" if t.get("spread") is not None else "-"
        print(f" {mark}{t['name']:<26} {t['label'][:46]:<46} {t['fps']:>7} {t['p50']:>7} {t['p95']:>7} {t['cpu']:>6} {t['rss']:>7} {sp:>7}  {at}")
    print(f"\n  decision: {why}\n")


# ============================================================================ apply / validate / rollback
def compare_live(before, after):
    rows = []
    for cid, b in before["cameras"].items():
        x = after["cameras"].get(cid)
        rows.append((cid, b, x))
    return rows


def print_live_compare(before, after):
    print(f"\n  {'':<12} {'before':>24}   {'after':>24}")
    print(f"  {'total AI fps':<12} {before['processedFpsTotal']:>24}   {after['processedFpsTotal']:>24}")
    for cid, b, x in compare_live(before, after):
        bt = f"{b['processedFps']} fps / {fmt(b.get('modelMs'))} ms"
        at = f"{x['processedFps']} fps / {fmt(x.get('modelMs'))} ms" if x else "missing"
        print(f"  {cid:<12} {bt:>24}   {at:>24}   (received {b['receivedFps']} -> {x['receivedFps'] if x else '-'} fps)")
    print(f"  {'system CPU':<12} {fmt(before.get('systemCpu'), '%'):>24}   {fmt(after.get('systemCpu'), '%'):>24}")
    print(f"  {'backend RSS':<12} {fmt(before.get('processRssMb'), ' MB'):>24}   {fmt(after.get('processRssMb'), ' MB'):>24}\n")


def judge(before, after, min_gain):
    """Keep only a measured improvement. Returns (keep, reason)."""
    if not after or not after.get("cameras"):
        return False, "no measurement after the change"
    lost = [cid for cid in before["cameras"] if cid not in after["cameras"]]
    if lost:
        return False, f"camera(s) {', '.join(lost)} not running after the change"
    bad_state = [cid for cid, x in after["cameras"].items() if x.get("state") not in ("processing", "connected")]
    if bad_state:
        return False, f"camera(s) {', '.join(bad_state)} not processing after the change"
    rb, ra = before["receivedFpsTotal"], after["receivedFpsTotal"]
    if rb and abs(ra - rb) / rb > 0.15:
        return False, f"the cameras delivered a different frame rate ({rb} -> {ra} fps) - comparison not valid"
    pb, pa = before["processedFpsTotal"], after["processedFpsTotal"]
    if (after.get("processRssMb") or 0) > (before.get("processRssMb") or 0) * 1.25 + 200:
        return False, f"memory grew {before.get('processRssMb')} -> {after.get('processRssMb')} MB"
    all_frames = pb >= 0.97 * rb
    if all_frames:          # every frame was analysed already: lower CPU is the only possible gain
        cb, ca = before.get("processCpu") or 0, after.get("processCpu") or 0
        if pa >= 0.97 * ra and ca <= cb * (1 - min_gain):
            return True, f"all frames analysed before and after; backend CPU {cb}% -> {ca}%"
        return False, f"all frames were analysed already; CPU {cb}% -> {ca}% is not a {min_gain * 100:.0f}% saving"
    if pb and pa >= pb * (1 + min_gain):
        return True, f"AI processing {pb} -> {pa} fps ({(pa / pb - 1) * 100:+.1f}%)"
    return False, f"AI processing {pb} -> {pa} fps ({((pa / pb - 1) * 100) if pb else 0:+.1f}%) - the required gain is +{min_gain * 100:.0f}%"


def settings_from_args(a, base):
    s = {}
    if a.threads is not None:
        s["AIV_TORCH_THREADS"] = str(a.threads) if a.threads > 0 else ""
    if a.interop is not None:
        s["AIV_TORCH_INTEROP_THREADS"] = str(a.interop) if a.interop > 0 else ""
    if a.concurrency is not None:
        s["AIV_INFER_CONCURRENCY"] = str(a.concurrency) if a.concurrency > 0 else ""
    if a.cv_threads is not None:
        s["AIV_CV_THREADS"] = str(a.cv_threads) if a.cv_threads > 0 else ""
    if a.cpus is not None:
        s["AIV_CPU_AFFINITY"] = a.cpus
    if a.omp_wait is not None:
        s["AIV_OMP_WAIT_POLICY"] = "" if a.omp_wait == "default" else a.omp_wait
    if a.from_bench:
        st = load_state()
        path = st.get("lastBench")
        if not path or not os.path.exists(path):
            fail("no benchmark result - run 'bench' first")
        rep = json.load(open(path))
        if not rep.get("choice"):
            fail(f"the last benchmark recommends keeping the current settings: {rep.get('why')}")
        s.update({k: v for k, v in rep["choice"]["settings"].items()})
    return s


def cmd_apply(a, settings=None, bench_ref=None):
    settings = settings if settings is not None else settings_from_args(a, current_settings())
    if not settings:
        fail("nothing to apply - give --threads/--concurrency/... or --from-bench")
    before_cfg = current_settings()
    changed = {k: v for k, v in settings.items() if before_cfg.get(k, "") != v}
    if not changed:
        say(f"settings already in effect: {settings_label(settings)}")
        return True
    for b in blockers(changed):
        fail(f"cannot apply: {b}. Change it there (or remove it) and run again.")
    mods = running_modules()
    can_restart = systemd_mode() or managed_pid() is not None
    before = None
    if mods and not a.no_validate:
        say(f"measuring the current performance for {a.measure} s (before)")
        before = measure_live(a.measure, quiet=not a.verbose)
        if before:
            say(f"before: {before['processedFpsTotal']} fps analysed in total, system CPU {before['systemCpu']}%")
    elif not mods:
        warn("no detection running - the change cannot be validated by measurement now (use 'validate' later)")
    backup = write_env(changed, a.reason or "apply")
    entry = {"id": stamp(), "time": ts(), "action": "apply", "settingsBefore": {k: before_cfg.get(k, "") for k in changed},
             "settingsAfter": changed, "backup": backup, "before": before, "bench": bench_ref, "status": "written"}
    say(f".env updated ({settings_label(changed)}); backup {os.path.relpath(backup, REPO)}")
    st = load_state()
    st["pending"] = entry
    save_state(st)
    if not can_restart:
        entry["status"] = "pending-restart"
        append_jsonl(CHANGES, entry)
        warn("this tool did not start the backend and no systemd service is installed: restart the backend yourself "
             "(Ctrl+C in its terminal, then ./start.sh), then run:  perf-tune.sh validate")
        return True
    if not restart_backend():
        entry["status"] = "restart-failed"
        append_jsonl(CHANGES, entry)
        warn("backend did not come back after the restart - rolling back")
        restore_env(backup)
        restart_backend()
        return False
    rt = api("/system/runtime", 5)[1] or {}
    entry["runtimeAfter"] = rt.get("applied")
    mismatch = check_applied(changed, rt)
    if mismatch:
        entry["status"] = "not-in-effect"
        warn("the backend does not report the new values: " + "; ".join(mismatch) + " - rolling back")
        append_jsonl(CHANGES, entry)
        restore_env(backup)
        restart_backend()
        return False
    if before and mods:
        if not resume_modules(mods):
            warn("not all detections are running again after the restart")
        say(f"warming up {a.warmup} s, then measuring {a.measure} s (after)")
        time.sleep(a.warmup)
        after = measure_live(a.measure, quiet=not a.verbose)
        entry["after"] = after
        keep, why = judge(before, after, a.min_gain)
        print_live_compare(before, after) if after else None
        entry["decision"] = why
        if keep or a.keep:
            entry["status"] = "kept" if keep else "kept-by-user"
            say(c(f"kept: {why}", "1;32") if keep else f"kept (--keep) although: {why}")
        else:
            entry["status"] = "rolled-back"
            say(c(f"rolled back: {why}", "1;33"))
            restore_env(backup)
            restart_backend()
            resume_modules(mods)
    else:
        entry["status"] = "applied-unvalidated"
    append_jsonl(CHANGES, entry)
    st = load_state()
    st.pop("pending", None)
    if entry["status"] in ("kept", "kept-by-user", "applied-unvalidated"):
        st.setdefault("applied", []).append({"id": entry["id"], "backup": backup, "settings": changed})
    save_state(st)
    write_report(entry)
    return entry["status"] in ("kept", "kept-by-user", "applied-unvalidated")


def check_applied(changed, rt):
    a = (rt or {}).get("applied") or {}
    req = (rt or {}).get("requested") or {}
    out = []
    pairs = {"AIV_TORCH_THREADS": ("torchThreads", a), "AIV_TORCH_INTEROP_THREADS": ("torchInteropThreads", a),
             "AIV_INFER_CONCURRENCY": ("inferConcurrency", a), "AIV_CV_THREADS": ("cvThreads", a)}
    for k, v in changed.items():
        if k in pairs and v:
            key, src = pairs[k]
            got = src.get(key)
            if got is None:          # torch not loaded yet (no model started): check what the backend read instead
                got = req.get(key)
            if got is not None and str(got) != str(v):
                out.append(f"{k}={v} but the backend reports {got}")
        if k == "AIV_CPU_AFFINITY" and v and a.get("cpuAffinity") != v:
            out.append(f"{k}={v} but the backend runs on CPUs {a.get('cpuAffinity')}")
    return out + [e for e in (rt or {}).get("errors", [])]


def cmd_validate(a):
    st = load_state()
    p = st.get("pending")
    if not p:
        fail("no change waiting for validation")
    before = p.get("before")
    if not before:
        fail("no measurement from before the change was stored")
    mods = {cid: v["modules"] for cid, v in before["cameras"].items()}
    resume_modules(mods, timeout=30)
    after = measure_live(a.measure, quiet=not a.verbose)
    keep, why = judge(before, after, a.min_gain)
    if after:
        print_live_compare(before, after)
    p.update(after=after, decision=why, status="kept" if keep else "measured-worse")
    append_jsonl(CHANGES, p)
    st.pop("pending", None)
    if keep:
        st.setdefault("applied", []).append({"id": p["id"], "backup": p["backup"], "settings": p["settingsAfter"]})
        say(c(f"kept: {why}", "1;32"))
    else:
        say(c(f"NOT confirmed: {why}", "1;33") + " - undo with: perf-tune.sh rollback (then restart the backend)")
        st.setdefault("applied", []).append({"id": p["id"], "backup": p["backup"], "settings": p["settingsAfter"]})
    save_state(st)
    write_report(p)


def cmd_rollback(a):
    st = load_state()
    applied = st.get("applied") or []
    if a.list:
        for x in applied:
            print(f"  {x['id']}  {settings_label(x['settings'])}  backup {os.path.relpath(x['backup'], REPO)}")
        if not applied:
            print("  nothing applied by this tool")
        return
    if a.to:
        target = next((x for x in applied if x["id"] == a.to), None)
        if not target:
            fail(f"no applied change {a.to} (see: rollback --list)")
    elif applied:
        target = applied[-1]
    else:
        fail("nothing to roll back (no change applied by this tool)")
    safety = restore_env(target["backup"])
    say(f".env restored from {os.path.relpath(target['backup'], REPO)} (the replaced .env is kept: {os.path.relpath(safety, REPO)})")
    idx = applied.index(target)
    st["applied"] = applied[:idx]
    save_state(st)
    append_jsonl(CHANGES, {"id": stamp(), "time": ts(), "action": "rollback", "to": target["id"], "backup": target["backup"],
                           "status": "rolled-back"})
    if a.no_restart:
        say("restart the backend to use the restored settings")
        return
    mods = running_modules()
    if systemd_mode() or managed_pid():
        restart_backend()
        if mods:
            resume_modules(mods)
        rt = api("/system/runtime", 5)[1] or {}
        say("in effect now: " + json.dumps(rt.get("applied", {})))
    else:
        say("restart the backend yourself (Ctrl+C in its terminal, then ./start.sh) to use the restored settings")


def cmd_history(a):
    try:
        lines = open(CHANGES, encoding="utf-8").read().splitlines()
    except OSError:
        print("  no changes recorded")
        return
    for line in lines[-a.last:]:
        e = json.loads(line)
        if e["action"] == "rollback":
            print(f"  {e['time']}  ROLLBACK to before {e['to']}")
            continue
        b, af = e.get("before") or {}, e.get("after") or {}
        meas = f"{b.get('processedFpsTotal', '-')} -> {af.get('processedFpsTotal', '-')} fps" if b else "not measured"
        print(f"  {e['time']}  {e['status']:<20} {settings_label(e['settingsBefore'])} -> {settings_label(e['settingsAfter'])}"
              f"  [{meas}]  {e.get('decision', '')}")


def write_report(entry):
    ensure_dirs()
    hw = hardware()
    p = os.path.join(PERF_DIR, "reports", f"report-{entry['id']}.md")
    b, af = entry.get("before"), entry.get("after")
    L = [f"# PYROTECH VISION performance change {entry['id']}", "",
         f"- time: {entry['time']}", f"- result: **{entry['status']}** - {entry.get('decision', 'no live measurement')}",
         f"- hardware: {hw.get('cpuModel') or '?'} ({hw.get('arch')}, {hw.get('cpus')} CPUs"
         + (f", fast cores {cpu_list_text(hw['bigCores'])}" if hw.get("bigCores") else "") + f"), {hw.get('glibc')}",
         f"- settings: {settings_label(entry['settingsBefore'])} -> {settings_label(entry['settingsAfter'])}",
         f"- .env backup: {os.path.relpath(entry['backup'], REPO)}"
         + ("" if entry["status"] == "rolled-back" else " (undo: `scripts/debian/perf-tune.sh rollback`)"), ""]
    if b and af:
        L += ["## Measured on this machine (live application, same cameras)", "",
              "| | before | after |", "|---|---|---|",
              f"| AI frames analysed, all cameras | {b['processedFpsTotal']} fps | {af['processedFpsTotal']} fps |",
              f"| frames received, all cameras | {b['receivedFpsTotal']} fps | {af['receivedFpsTotal']} fps |",
              f"| system CPU | {b['systemCpu']}% | {af['systemCpu']}% |",
              f"| backend CPU (100 = one core) | {b['processCpu']}% | {af['processCpu']}% |",
              f"| backend RSS | {b['processRssMb']} MB | {af['processRssMb']} MB |"]
        for cid, x in b["cameras"].items():
            y = af["cameras"].get(cid) or {}
            L.append(f"| {cid}: analysed / model ms / dropped | {x['processedFps']} fps / {fmt(x.get('modelMs'))} ms / {x['droppedPerS']}/s "
                     f"| {y.get('processedFps', '-')} fps / {fmt(y.get('modelMs'))} ms / {y.get('droppedPerS', '-')}/s |")
        L.append("")
    elif b:
        L += ["Before (measured):", f"- {b['processedFpsTotal']} fps analysed, system CPU {b['systemCpu']}%", "",
              "After: not measured yet (`scripts/debian/perf-tune.sh validate`).", ""]
    bench = entry.get("bench")
    if bench:
        L += ["## Benchmark (separate processes, saved frames)", "", f"{bench}", ""]
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    say(f"report: {os.path.relpath(p, REPO)}")
    return p


# ============================================================================ status / auto
def cmd_status(a):
    hw = hardware()
    print(c(f"PYROTECH VISION performance status  {ts()}", "1"))
    print(f"  hardware: {hw.get('cpuModel') or '?'} | {hw.get('arch')} | {hw.get('cpus')} CPUs ({hw.get('cpusUsable')} usable)"
          + (f" | fast cores {cpu_list_text(hw['bigCores'])}, slow {cpu_list_text(hw['littleCores'])}" if hw.get("bigCores") else "")
          + f" | {hw.get('glibc')} | OpenVINO {'installed' if hw.get('openvinoInstalled') else 'not installed'}")
    print(f"  start-up: {'systemd services' if systemd_mode() else ('./start.sh started by this tool' if managed_pid() else './start.sh / manual')}")
    print(f"  .env CPU settings: {settings_label(current_settings())}")
    st, rt = api("/system/runtime", 5)
    if st != 200:
        print(c("  backend: not answering", "1;31") + ("  (an older backend without /system/runtime?)" if backend_pid() else ""))
        return
    ap = rt.get("applied", {})
    print(f"  in effect: engine {rt.get('engine')}, input {rt.get('imgSize')} px, torch intra-op {ap.get('torchThreads')} "
          f"inter-op {ap.get('torchInteropThreads')}, inference slots {ap.get('inferConcurrency') or 'unlimited'}, "
          f"OpenCV {ap.get('cvThreads')}, CPUs {ap.get('cpuAffinity')}, OpenMP wait {ap.get('ompWaitPolicy') or 'default'}, "
          f"shared person pass {'on' if rt.get('sharedPersonInference') else 'off'}")
    for e in rt.get("errors") or []:
        warn(e)
    say(f"measuring {a.measure} s ...")
    meas = measure_live(a.measure, quiet=True)
    if meas:
        print("\n".join(render(None, api('/system/metrics', 4)[1], rt)[1:]))
        print(f"  over {meas['seconds']} s: {meas['processedFpsTotal']} fps analysed of {meas['receivedFpsTotal']} fps received, "
              f"system CPU {meas['systemCpu']}%, backend {meas['processCpu']}% / {meas['processRssMb']} MB, load {meas['load1']}")
        tg = sorted(meas["threadGroups"].items(), key=lambda kv: -kv[1])[:8]
        print("  CPU by thread: " + ", ".join(f"{k} {v:.0f}%" for k, v in tg))
    print("  findings:")
    print_findings(analyse(meas, hw, rt))


def cmd_auto(a):
    ensure_dirs()
    hw = hardware()
    say(c("auto: start -> measure -> frames -> benchmark -> apply best (validated) -> report", "1"))
    if not wait_api(2):
        if not start_app():
            fail("the application did not start")
    if not running_modules():
        say("waiting up to 60 s for detections to resume ...")
        end = time.time() + 60
        while time.time() < end and not running_modules():
            time.sleep(3)
    if not running_modules():
        fail("no detection is running - start the detections you use in the UI, then run 'auto' again")
    time.sleep(a.warmup)
    say(f"baseline: measuring {a.measure} s")
    base = measure_live(a.measure, quiet=not a.verbose)
    rt = api("/system/runtime", 5)[1]
    print_findings(analyse(base, hw, rt))
    a.frames_dir = None
    a.video = None
    a.streams = a.streams if getattr(a, "streams", None) else None
    frames = cmd_frames(argparse.Namespace(per_camera=a.per_camera, every=1.0, max_cameras=4), quiet=False)
    a.frames_dir = frames
    rep = cmd_bench(a)
    if not rep.get("choice"):
        say(c(f"no change applied: {rep['why']}", "1;33"))
        append_jsonl(CHANGES, {"id": stamp(), "time": ts(), "action": "auto", "status": "no-change", "decision": rep["why"],
                               "settingsBefore": current_settings(), "settingsAfter": {}, "before": base, "backup": ""})
        return
    if a.dry_run:
        say(f"--dry-run: would apply {rep['choice']['label']}")
        return
    ch = rep["choice"]
    bench_ref = (f"{ch['label']}: {ch['fps']} fps vs current "
                 f"{next(t['fps'] for t in rep['table'] if t['name'] == 'current')} fps in {rep['streams']} simulated stream(s), "
                 f"{'idle priority next to the running app' if rep['idlePriority'] else 'exclusive'}; {rep['why']}")
    cmd_apply(a, settings={k: v for k, v in ch["settings"].items()}, bench_ref=bench_ref)


# ============================================================================ CLI
def main():
    ap = argparse.ArgumentParser(prog="perf-tune.sh", description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common_measure(p):
        p.add_argument("--measure", type=int, default=60, help="seconds of live measurement (default 60)")
        p.add_argument("--warmup", type=int, default=20, help="seconds to wait after a restart before measuring")
        p.add_argument("--min-gain", type=float, default=0.05, help="required improvement (default 0.05 = 5%%)")
        p.add_argument("--verbose", action="store_true", help="print the live view while measuring")

    def common_bench(p):
        p.add_argument("--frames-dir", help="folder with representative frames (default: newest saved, else captured)")
        p.add_argument("--video", help="take frames from this video file instead")
        p.add_argument("--streams", type=int, help="camera streams to simulate (default: cameras running detection)")
        p.add_argument("--count", action=argparse.BooleanOptionalAction, default=None, help="include Human Count (default: as running)")
        p.add_argument("--fire", action=argparse.BooleanOptionalAction, default=None, help="include the fire model (default: as running)")
        p.add_argument("--seconds", type=int, default=20, help="measuring time per configuration and round (default 20)")
        p.add_argument("--rounds", type=int, default=2, help="interleaved rounds (default 2)")
        p.add_argument("--threads", help="only these intra-op thread counts, e.g. 2,4,8")
        p.add_argument("--no-variants", action="store_true", help="skip the second stage (OpenMP passive, fast cores)")
        p.add_argument("--maintenance", action="store_true",
                       help="pause the running detections during the benchmark (exact numbers; detection stops for minutes)")

    p = sub.add_parser("status")
    p.add_argument("--measure", type=int, default=10)
    p = sub.add_parser("monitor")
    p.add_argument("--interval", type=float, default=2.0)
    p.add_argument("--duration", type=float, default=0, help="stop after N seconds (default: until Ctrl+C)")
    p.add_argument("--log", help="also append every sample to this JSON-lines file")
    sub.add_parser("start")
    sub.add_parser("stop")
    sub.add_parser("restart")
    p = sub.add_parser("frames")
    p.add_argument("--per-camera", type=int, default=6)
    p.add_argument("--every", type=float, default=1.0)
    p.add_argument("--max-cameras", type=int, default=4)
    p = sub.add_parser("bench")
    common_bench(p)
    p.add_argument("--min-gain", type=float, default=0.05, help="required improvement to recommend a change (default 5%%)")
    p = sub.add_parser("apply")
    common_measure(p)
    p.add_argument("--from-bench", action="store_true", help="apply the configuration chosen by the last benchmark")
    p.add_argument("--threads", type=int, help="AIV_TORCH_THREADS (0 = PyTorch default)")
    p.add_argument("--interop", type=int, help="AIV_TORCH_INTEROP_THREADS (0 = default)")
    p.add_argument("--concurrency", type=int, help="AIV_INFER_CONCURRENCY (0 = unlimited)")
    p.add_argument("--cv-threads", type=int, help="AIV_CV_THREADS (0 = default)")
    p.add_argument("--cpus", help="AIV_CPU_AFFINITY, e.g. 4-7 ('' = all)")
    p.add_argument("--omp-wait", choices=("passive", "active", "default"))
    p.add_argument("--no-validate", action="store_true", help="restart without before/after measurement")
    p.add_argument("--keep", action="store_true", help="keep the change even if the measurement does not confirm it")
    p.add_argument("--reason", default="")
    p = sub.add_parser("validate")
    common_measure(p)
    p = sub.add_parser("rollback")
    p.add_argument("--to", help="change id (see rollback --list)")
    p.add_argument("--list", action="store_true")
    p.add_argument("--no-restart", action="store_true")
    p = sub.add_parser("history")
    p.add_argument("--last", type=int, default=20)
    p = sub.add_parser("auto")
    common_measure(p)
    common_bench(p)
    p.add_argument("--per-camera", type=int, default=6, help="frames saved per camera")
    p.add_argument("--dry-run", action="store_true", help="benchmark and recommend, change nothing")
    p.add_argument("--keep", action="store_true")
    p.add_argument("--no-validate", action="store_true")
    p.add_argument("--reason", default="auto")
    p.set_defaults(min_gain=0.05)
    a = ap.parse_args()
    if a.cmd == "auto":
        a.from_bench = False
    if not os.path.isdir("/proc"):
        fail("this tool needs Linux (/proc)")
    {"status": cmd_status, "monitor": cmd_monitor, "start": lambda a: start_app(), "stop": lambda a: stop_app(),
     "restart": lambda a: (stop_app(), start_app()), "frames": cmd_frames, "bench": cmd_bench, "apply": cmd_apply,
     "validate": cmd_validate, "rollback": cmd_rollback, "history": cmd_history, "auto": cmd_auto}[a.cmd](a)


if __name__ == "__main__":
    main()
