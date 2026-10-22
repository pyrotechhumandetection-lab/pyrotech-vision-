#!/usr/bin/env python3
"""PYROTECH VISION - RKNN / Rockchip NPU analysis (read-only: changes no setting, model or camera).

    ./aivision.sh npu check                 everything below with quick settings
    backend/.venv/bin/python scripts/npu/rknn_analyze.py [options]

Sections
  1. system     SoC, kernel, NPU device nodes + permissions, NPU driver version, NPU frequency / load,
                other programs using the NPU (e.g. the box vendor's own AI engine)
  2. runtime    librknnrt.so in use (path, version, sha256), rknn-toolkit-lite2 present or not
  3. models     every RKNN model: file, target chip, precision, input / output tensors, NPU memory
  4. bench      NPU latency per core (raw model run + NPU-only time), then N parallel streams (= cameras) on
                the 3 cores: total and per-stream fps, CPU used by this process
  5. pipeline   full Ultralytics pass on a real image (resize + NPU + NMS): NPU engine vs the CPU engine
  6. accuracy   NPU vs CPU detections on the same images (snapshot originals, --images DIR or --camera ID):
                person / vehicle boxes matched at IoU >= 0.5, missed, extra, confidence difference

Options
  --model PATH        RKNN model folder or .rknn file (default: every backend/models/*_rknn_model)
  --only LIST         sections to run, comma separated (system,runtime,models,bench,pipeline,accuracy)
  --runs N            timed runs per core in bench (default 50)
  --streams N         parallel streams in bench (default 3; 8 = your 8 cameras)
  --seconds S         duration of the parallel test (default 8)
  --images DIR        images for pipeline / accuracy (default: newest snapshot originals)
  --camera ID         also grab frames from this camera's AI stream (password never printed)
  --frames N          images / frames used for accuracy (default 40)
  --out DIR           report folder (default logs/)

Reports: <out>/npu_report_<date>.txt and .json. Passwords in camera URLs are never printed or saved.
Run with sudo for the NPU load and the list of other programs using the NPU (read-only).
"""
import argparse
import glob
import hashlib
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.chdir(REPO)

import numpy as np  # noqa: E402

from backend import config as _config  # noqa: E402,F401  (loads .env: AIV_RKNN_LIB, AIV_NPU_CORE ...)
from backend.detection import rknn_runtime as rt  # noqa: E402

PERSON_VEHICLE = {0: "person", 1: "bicycle", 2: "car", 3: "motorcycle", 5: "bus", 7: "truck"}
MASK_RE = re.compile(r"(rtsps?://)[^@/\s]*@", re.I)

OUT = []          # text report lines
REPORT = {}       # json report
PROBLEMS = []     # failed checks only


def mask(s):
    return MASK_RE.sub(r"\1***@", str(s))


def say(line=""):
    line = mask(line)
    print(line, flush=True)
    OUT.append(line)


def head(title):
    say()
    say("=" * 78)
    say(title)
    say("=" * 78)


def problem(text):
    PROBLEMS.append(mask(text))
    say("  !! " + text)


def read(path, default=None):
    try:
        return Path(path).read_text(errors="ignore").strip()
    except OSError:
        return default


def pct(v, n):
    return f"{100.0 * v / n:.1f}%" if n else "n/a"


def stats_ms(xs):
    a = np.array(xs, float)
    return {"mean": round(a.mean(), 2), "p50": round(np.percentile(a, 50), 2), "p95": round(np.percentile(a, 95), 2),
            "max": round(a.max(), 2)} if len(a) else {}


# ---------------------------------------------------------------- 1. system
def npu_nodes():
    nodes = []
    for p in sorted(glob.glob("/dev/dri/renderD*")) + ["/dev/rknpu"]:
        if not os.path.exists(p):
            continue
        drv = ""
        m = re.search(r"renderD(\d+)", p)
        if m:
            drv = read(f"/sys/class/drm/renderD{m.group(1)}/device/uevent", "") or ""
            drv = (re.search(r"DRIVER=(\S+)", drv) or [None, ""])[1]
        nodes.append({"path": p, "driver": drv, "rw": os.access(p, os.R_OK | os.W_OK)})
    return nodes


def npu_users(paths):
    """Processes with an NPU device open (all processes only with sudo)."""
    users, denied = {}, 0
    for pd in glob.glob("/proc/[0-9]*"):
        try:
            for fd in os.listdir(pd + "/fd"):
                try:
                    tgt = os.readlink(f"{pd}/fd/{fd}")
                except OSError:
                    continue
                if tgt in paths:
                    pid = int(pd.split("/")[-1])
                    cmd = (read(pd + "/cmdline", "") or "").replace("\x00", " ").strip()[:100]
                    users[pid] = cmd or read(pd + "/comm", "?")
                    break
        except PermissionError:
            denied += 1
        except OSError:
            pass
    return users, denied


def section_system():
    head("1. SYSTEM / NPU")
    chip = rt.soc()
    model = (read("/proc/device-tree/model", "") or "").replace("\x00", "")
    say(f"  board         : {model or 'unknown'}")
    say(f"  SoC           : {chip or 'not a Rockchip NPU chip'}")
    say(f"  kernel        : {os.uname().release}  ({os.uname().machine})")
    osr = read("/etc/os-release", "") or ""
    m_os = re.search(r'PRETTY_NAME="?([^"\n]+)', osr)
    say(f"  OS            : {m_os.group(1) if m_os else '?'}")
    say(f"  CPU cores     : {os.cpu_count()}")
    mem = read("/proc/meminfo", "") or ""
    mt = re.search(r"MemTotal:\s+(\d+)", mem)
    ma = re.search(r"MemAvailable:\s+(\d+)", mem)
    if mt:
        say(f"  RAM           : {int(mt.group(1)) // 1024} MB total, {int(ma.group(1)) // 1024 if ma else '?'} MB available")
    st = os.statvfs(str(REPO))
    free_mb = st.f_bavail * st.f_frsize // 2**20
    say(f"  free disk     : {free_mb} MB (project folder)")
    if free_mb < 300:
        problem(f"only {free_mb} MB free disk - logs and snapshots may fill it")
    REPORT["system"] = {"board": model, "soc": chip, "kernel": os.uname().release, "freeDiskMb": free_mb}
    if chip is None:
        problem("this computer has no Rockchip NPU - the RKNN engine cannot be used here")
        return

    nodes = npu_nodes()
    REPORT["system"]["npuNodes"] = nodes
    if not nodes:
        problem("no NPU device node (/dev/dri/renderD* of rknpu, or /dev/rknpu) - NPU driver not loaded")
    for n in nodes:
        say(f"  device        : {n['path']}  driver={n['driver'] or '?'}  {'read/write OK' if n['rw'] else 'NO ACCESS for this user'}")
    npu_paths = [n["path"] for n in nodes if n["driver"] in ("", "RKNPU", "rknpu") or n["path"] == "/dev/rknpu"]
    if npu_paths and not any(n["rw"] for n in nodes if n["path"] in npu_paths):
        problem(f"user '{os.environ.get('USER', '?')}' cannot open the NPU device - add it to the 'render' and 'video' groups "
                "(sudo usermod -aG render,video $USER, then log in again)")

    drv = read("/sys/kernel/debug/rknpu/version") or read("/sys/module/rknpu/version")
    if not drv:
        try:
            import subprocess
            dm = subprocess.run(["dmesg"], capture_output=True, text=True, timeout=5).stdout
            m = re.findall(r"RKNPU.*?driver.*?v?(\d+\.\d+\.\d+)", dm, re.I)
            drv = m[-1] if m else None
        except Exception:
            drv = None
    say(f"  NPU driver    : {drv or 'not readable here (shown again in section 3 from the runtime)'}")
    REPORT["system"]["npuDriver"] = drv

    for d in glob.glob("/sys/class/devfreq/*npu*"):
        cur = read(d + "/cur_freq")
        mx = read(d + "/max_freq")
        gov = read(d + "/governor")
        avail = read(d + "/available_frequencies", "")
        say(f"  NPU clock     : {int(cur) // 10**6 if cur and cur.isdigit() else '?'} MHz now, max "
            f"{int(mx) // 10**6 if mx and mx.isdigit() else '?'} MHz, governor {gov}  ({os.path.basename(d)})")
        REPORT["system"]["npuFreq"] = {"cur": cur, "max": mx, "governor": gov, "available": avail}
    load = read("/sys/kernel/debug/rknpu/load")
    say(f"  NPU load      : {load if load else 'needs sudo (debugfs)'}")
    REPORT["system"]["npuLoad"] = load

    users, denied = npu_users(set(npu_paths))
    REPORT["system"]["npuUsers"] = users
    me = os.getpid()
    others = {p: c for p, c in users.items() if p != me}
    if others:
        say("  NPU in use by :")
        for p, c in others.items():
            say(f"      pid {p}: {c}")
        if any("pyrotech" not in c and "backend.main" not in c and "aivision" not in c for c in others.values()):
            say("      (another program shares the NPU - its load lowers the fps measured below)")
    else:
        say(f"  NPU in use by : nobody else{' (some processes not readable - run with sudo to see all)' if denied else ''}")


# ---------------------------------------------------------------- 2. runtime
def lib_version(p):
    try:
        data = Path(p).read_bytes()
    except OSError:
        return None
    m = re.search(rb"librknnrt version: ([0-9][^\x00\n]{0,60})", data)
    return m.group(1).decode(errors="ignore") if m else None


def section_runtime():
    head("2. NPU RUNTIME")
    real = rt._real_rknnlite()
    say(f"  rknn-toolkit-lite2 : {'installed - used instead of the built-in runtime' if real else 'not installed (not needed: the backend calls librknnrt.so directly)'}")
    p = rt.lib_path()
    if p is None:
        problem("librknnrt.so not found - run ./aivision.sh npu setup")
        REPORT["runtime"] = {"lib": None}
        return
    ver = lib_version(p)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    say(f"  librknnrt.so       : {p}")
    say(f"  version            : {ver or '?'}")
    say(f"  sha256             : {sha[:16]}...")
    others = [str(c) for c in rt.LIB_CANDIDATES if c.is_file() and c != p]
    for o in others:
        say(f"  also present       : {o}  (version {lib_version(o) or '?'}; not used)")
    REPORT["runtime"] = {"lib": str(p), "version": ver, "sha256": sha, "others": others,
                         "toolkitLite2": bool(real)}


# ---------------------------------------------------------------- 3. models
def model_dirs(arg):
    if arg:
        return [Path(arg)]
    return sorted(p for p in (REPO / "backend" / "models").glob("*_rknn_model") if p.is_dir())


def open_ctx(model, core=None):
    r = rt.RKNNLite()
    if r.load_rknn(str(model)) != 0:
        raise RuntimeError(f"cannot read {model}")
    r.init_runtime(core_mask=core)
    return r


def rknn_file(d):
    d = Path(d)
    return d if d.is_file() else next(iter(sorted(d.glob("*.rknn"))), None)


def section_models(dirs):
    head("3. RKNN MODELS")
    REPORT["models"] = []
    if not dirs:
        problem("no RKNN model in backend/models (*_rknn_model) - git pull, or convert with scripts/npu/convert_rknn.py")
        return
    for d in dirs:
        f = rknn_file(d)
        info = rt.model_info(d)
        say(f"  {d.name}")
        if f is None:
            problem(f"{d}: no .rknn file")
            continue
        raw = f.read_bytes()
        plat = sorted({m.decode() for m in re.findall(rb"rk3588|rk3576|rk3568|rk3566|rk3562|rv1126b|rk2118", raw[:4096 * 64])})
        tk = re.search(rb'"?(?:toolkit|compiler)[_ ]?version"?\s*[:=]\s*"?([0-9][0-9.a-z+_-]{2,30})', raw[:4096 * 256], re.I)
        rec = {"dir": str(d), "file": f.name, "sizeMb": round(len(raw) / 2**20, 2), "imgsz": info["imgsz"],
               "precision": "INT8" if info["quantize"] == 8 else "FP16", "classes": info["names"],
               "targets": plat, "toolkit": tk.group(1).decode(errors="ignore") if tk else None}
        say(f"      file       : {f.name}  ({rec['sizeMb']} MB)")
        say(f"      built for  : {', '.join(plat) or '?'}   toolkit {rec['toolkit'] or '?'}")
        say(f"      precision  : {rec['precision']}   input {info['imgsz']}x{info['imgsz']}   classes {info['names']}")
        chip = rt.soc()
        if chip and plat and chip not in plat:
            problem(f"{f.name} was built for {', '.join(plat)}, this board is {chip}")
        if chip and rt.lib_path() is not None or chip and rt._real_rknnlite():
            try:
                r = open_ctx(f)
                api, drv = r.sdk_version()
                rec.update(api=api, driver=drv, inputs=r.inputs, outputs=r.outputs, mem=r.mem_size(),
                           custom=r.custom_string())
                say(f"      runtime    : API {api}   NPU driver {drv}")
                for t in r.inputs:
                    say(f"      input  {t['index']}   : {t['name']} {t['dims']} {t['type']} {t['fmt']} qnt={t['qnt']}")
                for t in r.outputs:
                    say(f"      output {t['index']}   : {t['name']} {t['dims']} {t['type']} {t['fmt']} qnt={t['qnt']}")
                if rec["mem"]:
                    say(f"      NPU memory : weights {rec['mem']['weightBytes'] / 2**20:.1f} MB, internal "
                        f"{rec['mem']['internalBytes'] / 2**20:.1f} MB per camera")
                dv = re.match(r"(\d+)\.(\d+)", drv or "")
                if dv and (int(dv.group(1)), int(dv.group(2))) < (0, 9):
                    problem(f"NPU driver {drv} is older than 0.9 - runtime 2.x may be slow or unstable; a newer board image is advised")
                r.release()
            except Exception as e:
                problem(f"{f.name} does not load on the NPU: {e}")
        REPORT["models"].append(rec)


# ---------------------------------------------------------------- 4. bench
def section_bench(dirs, runs, streams, seconds):
    head("4. NPU BENCHMARK (raw model run, no camera)")
    REPORT["bench"] = []
    if rt.soc() is None or (rt.lib_path() is None and not rt._real_rknnlite()):
        say("  skipped - no NPU runtime on this computer")
        return
    try:
        import urllib.request
        urllib.request.urlopen("http://127.0.0.1:8000/api/system/health", timeout=1)
        say("  note: the PYROTECH backend is running and may use the NPU at the same time -")
        say("        for clean numbers run ./aivision.sh stop first (./aivision.sh start afterwards)")
    except Exception:
        pass
    chip = rt.soc()
    cores = [("core 0", rt.CORE_0), ("core 1", rt.CORE_1), ("core 2", rt.CORE_2), ("cores 0+1+2", rt.CORE_0_1_2)] \
        if chip == "rk3588" else [("auto", rt.CORE_AUTO)]
    for d in dirs:
        f = rknn_file(d)
        if f is None:
            continue
        rec = {"model": d.name, "cores": {}, "parallel": None}
        say(f"  {d.name}")
        try:
            probe = open_ctx(f, rt.CORE_AUTO)
        except Exception as e:
            problem(f"{f.name}: cannot open on the NPU: {e}")
            continue
        shape = probe.inputs[0]["dims"]
        tab = getattr(probe, "io_table", None) or []
        if tab:
            say(f"      CPU<->NPU data path (fastest one with identical output is used: {'/'.join(probe.io)}):")
            for t in tab:
                say(f"        {t['io']:12s} {('%.1f ms' % t['ms']) if t['ms'] is not None else 'error':>9s}   "
                    f"{'same output' if t['same'] else 'DIFFERENT output - not used'}"
                    f"{'' if t.get('maxDiff') is None else '  (max diff %s)' % t['maxDiff']}{('  ' + t['error']) if t.get('error') else ''}")
        rec["io"] = {"chosen": "/".join(getattr(probe, "io", ("uint8", "float"))), "variants": tab}
        probe.release()
        if probe.inputs[0]["fmt"] == "NCHW":
            shape = [shape[0], shape[2], shape[3], shape[1]]
        rng = np.random.default_rng(0)
        x = rng.integers(0, 255, size=shape, dtype=np.uint8)
        say(f"      {'NPU core':12s} {'run ms mean':>11s} {'p95':>7s} {'NPU-only ms':>12s} {'fps':>7s}")
        for name, mask_ in cores:
            try:
                r = open_ctx(f, mask_)
                for _ in range(5):
                    r.inference([x])
                ts, npu = [], []
                for _ in range(runs):
                    t0 = time.perf_counter()
                    r.inference([x])
                    ts.append((time.perf_counter() - t0) * 1000.0)
                    us = r.last_run_us()
                    if us:
                        npu.append(us / 1000.0)
                r.release()
                s = stats_ms(ts)
                n = stats_ms(npu)
                rec["cores"][name] = {"runMs": s, "npuMs": n, "fps": round(1000.0 / s["mean"], 1)}
                say(f"      {name:12s} {s['mean']:11.1f} {s['p95']:7.1f} {n.get('mean', float('nan')):12.1f} {1000.0 / s['mean']:7.1f}")
            except Exception as e:
                problem(f"{f.name} on {name}: {e}")
        # parallel streams, round-robin cores like the backend
        say(f"      parallel: {streams} streams (one per camera) on the NPU cores for {seconds:.0f} s ...")
        ctxs = []
        try:
            for i in range(streams):
                ctxs.append(open_ctx(f, cores[i % min(3, len(cores))][1] if chip == "rk3588" else rt.CORE_AUTO))
        except Exception as e:
            problem(f"only {len(ctxs)} of {streams} NPU contexts could be opened: {e}")
        if ctxs:
            counts = [0] * len(ctxs)
            lat = [[] for _ in ctxs]
            stop = time.time() + seconds
            cpu0 = os.times()

            def work(i):
                while time.time() < stop:
                    t0 = time.perf_counter()
                    ctxs[i].inference([x])
                    lat[i].append((time.perf_counter() - t0) * 1000.0)
                    counts[i] += 1
            th = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(len(ctxs))]
            t_start = time.time()
            for t in th:
                t.start()
            for t in th:
                t.join()
            el = time.time() - t_start
            cpu1 = os.times()
            cpu_pct = 100.0 * ((cpu1.user - cpu0.user) + (cpu1.system - cpu0.system)) / el
            total = sum(counts) / el
            per = [c / el for c in counts]
            for c in ctxs:
                c.release()
            rec["parallel"] = {"streams": len(ctxs), "totalFps": round(total, 1), "perStreamFps": [round(p, 1) for p in per],
                               "minStreamFps": round(min(per), 1), "cpuPct": round(cpu_pct, 1),
                               "latencyMs": stats_ms(sum(lat, []))}
            say(f"      total {total:.1f} fps   per stream {min(per):.1f}-{max(per):.1f} fps   latency p95 "
                f"{rec['parallel']['latencyMs'].get('p95', 0):.1f} ms   CPU of this process {cpu_pct:.0f}% (100% = 1 core)")
        REPORT["bench"].append(rec)
    say("  (raw model only: the backend adds resize + NMS + tracking on the CPU, see section 5)")


# ---------------------------------------------------------------- images
def snapshot_images(limit):
    roots = [REPO / "backend" / "snapshots", REPO / "backend" / "data" / "snapshots"]
    env = os.environ.get("AIV_SNAPSHOT_DIR")
    if env:
        roots.insert(0, Path(env))
    files = []
    for r in roots:
        if r.is_dir():
            files += [p for p in r.rglob("*.jpg") if "thumb" not in p.name.lower()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    orig = [p for p in files if "orig" in p.name.lower()]
    return (orig or files)[:limit]


def camera_frames(cam_id, n):
    import cv2
    from backend.services.cameras import CameraRegistry
    reg = CameraRegistry()
    cams = reg.list()
    cam = reg.get(cam_id) or next((c for c in cams if str(c.get("name", "")).strip().lower() == cam_id.strip().lower()
                                   or str(c.get("id", "")).lower() == cam_id.strip().lower()), None)
    if not cam:
        problem(f"camera {cam_id} not found - cameras: "
                f"{', '.join(c['id'] + ' (' + str(c.get('name') or '') + ')' for c in cams) or 'none'}")
        return []
    cam_id = f"{cam['id']} ({cam.get('name') or ''})"
    try:
        src, kind = reg.resolve_ai_source(cam)
    except ValueError as e:
        problem(f"camera {cam_id}: {e}")
        return []
    say(f"  grabbing {n} frames from camera {cam_id} ({kind} stream) ...")
    cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
    frames, t_end = [], time.time() + max(20, n * 1.5)
    k = 0
    while len(frames) < n and time.time() < t_end:
        ok, fr = cap.read()
        if not ok:
            time.sleep(0.2)
            continue
        k += 1
        if k % 10 == 0:           # spread over time: every 10th frame
            frames.append(fr)
    cap.release()
    if len(frames) < n:
        problem(f"camera {cam_id}: only {len(frames)} of {n} frames could be read")
    return frames


def load_images(args):
    import cv2
    imgs, names = [], []
    if args.images:
        files = sorted(p for p in Path(args.images).rglob("*") if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".bmp"))[:args.frames]
    else:
        files = snapshot_images(args.frames)
    for p in files:
        im = cv2.imread(str(p))
        if im is not None:
            imgs.append(im)
            names.append(p.name)
    if args.camera:
        fr = camera_frames(args.camera, args.frames)
        imgs += fr
        names += [f"cam{args.camera}-{i}" for i in range(len(fr))]
    return imgs, names


# ---------------------------------------------------------------- 5/6. pipeline + accuracy
def cpu_model():
    from backend import config
    os.environ["AIV_ENGINE"] = "auto"
    if (config.OPENVINO_MODEL_PATH / "yolov8n.xml").is_file() and config.openvino_available():
        return "OpenVINO", str(config.OPENVINO_MODEL_PATH)
    return "PyTorch", str(config.MODEL_PATH)


def predict(model, im, imgsz):
    r = model.predict(im, imgsz=imgsz, conf=0.25, classes=list(PERSON_VEHICLE), verbose=False)[0]
    b = r.boxes
    if b is None or not len(b):
        return np.zeros((0, 6), np.float32), r.speed
    return np.concatenate([b.xyxy.cpu().numpy(), b.conf.cpu().numpy()[:, None], b.cls.cpu().numpy()[:, None]], 1), r.speed


def box_iou(a, b):
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / u if u > 0 else 0.0


def match(ref, test):
    """Greedy same-class matching at IoU >= 0.5: (pairs, unmatched ref idx, unmatched test idx)."""
    pairs, used = [], set()
    for i in np.argsort(-ref[:, 4]) if len(ref) else []:
        best, bj = 0.5, None
        for j in range(len(test)):
            if j in used or int(test[j, 5]) != int(ref[i, 5]):
                continue
            v = box_iou(ref[i], test[j])
            if v >= best:
                best, bj = v, j
        if bj is not None:
            used.add(bj)
            pairs.append((i, bj, best))
    mi = set(p[0] for p in pairs)
    return pairs, [i for i in range(len(ref)) if i not in mi], [j for j in range(len(test)) if j not in used]


def section_pipeline_accuracy(dirs, args, do_pipe, do_acc):
    if rt.soc() is None or (rt.lib_path() is None and not rt._real_rknnlite()):
        if do_pipe:
            head("5. FULL PIPELINE")
            say("  skipped - no NPU runtime on this computer")
        if do_acc:
            head("6. ACCURACY NPU vs CPU")
            say("  skipped - no NPU runtime on this computer")
        return
    from ultralytics import YOLO
    rt.install_for_ultralytics()
    imgs, names = load_images(args)
    cpu_name, cpu_path = cpu_model()
    ref_model = YOLO(cpu_path, task="detect")
    if not imgs:
        say("  (no snapshot images found and no --images / --camera given: using a blank test frame for timing only)")
        imgs, names = [np.full((720, 1280, 3), 114, np.uint8)], ["blank"]
    for d in dirs:
        f = rknn_file(d)
        if f is None:
            continue
        info = rt.model_info(d)
        imgsz = info["imgsz"]
        npu_model = YOLO(str(d), task="detect")
        prec = "INT8" if info["quantize"] == 8 else "FP16"
        if do_pipe:
            head(f"5. FULL PIPELINE - {d.name} ({prec}) vs {cpu_name} on the CPU, image {imgs[0].shape[1]}x{imgs[0].shape[0]}")
            res = {}
            for label, m in (("NPU " + prec, npu_model), (cpu_name + " CPU", ref_model)):
                for _ in range(3):
                    predict(m, imgs[0], imgsz)
                sp = {"preprocess": [], "inference": [], "postprocess": []}
                tot = []
                for i in range(min(20, max(5, len(imgs)))):
                    t0 = time.perf_counter()
                    _, s = predict(m, imgs[i % len(imgs)], imgsz)
                    tot.append((time.perf_counter() - t0) * 1000.0)
                    for k in sp:
                        sp[k].append(s.get(k, 0.0))
                t = stats_ms(tot)
                res[label] = {"totalMs": t, **{k: round(float(np.mean(v)), 1) for k, v in sp.items()}}
                say(f"  {label:16s} total {t['mean']:6.1f} ms ({1000.0 / t['mean']:5.1f} fps)   resize {res[label]['preprocess']:5.1f}"
                    f"   model {res[label]['inference']:6.1f}   NMS {res[label]['postprocess']:5.1f} ms")
            REPORT.setdefault("pipeline", {})[d.name] = res
        if do_acc:
            head(f"6. ACCURACY - {d.name} ({prec}) vs {cpu_name} (reference), {len(imgs)} images, conf >= 0.25")
            if names == ["blank"]:
                say("  skipped - needs real images: --images DIR, --camera ID, or snapshots from detections")
                continue
            tot = {"ref": 0, "matched": 0, "missed": 0, "extra": 0, "strongRef": 0, "strongMissed": 0}
            per_cls, cdiff, ious, worst = {}, [], [], []
            for im, nm in zip(imgs, names):
                ref, _ = predict(ref_model, im, imgsz)
                tst, _ = predict(npu_model, im, imgsz)
                pairs, miss, extra = match(ref, tst)
                tot["ref"] += len(ref)
                tot["matched"] += len(pairs)
                tot["missed"] += len(miss)
                tot["extra"] += len(extra)
                strong = ref[:, 4] >= 0.5 if len(ref) else np.zeros(0, bool)
                tot["strongRef"] += int(strong.sum())
                tot["strongMissed"] += sum(1 for i in miss if strong[i])
                for i, j, v in pairs:
                    cdiff.append(abs(float(ref[i, 4]) - float(tst[j, 4])))
                    ious.append(v)
                for i in range(len(ref)):
                    c = PERSON_VEHICLE.get(int(ref[i, 5]), str(int(ref[i, 5])))
                    pc = per_cls.setdefault(c, [0, 0])
                    pc[0] += 1
                    pc[1] += 0 if i in miss else 1
                if miss or extra:
                    worst.append((len(miss) + len(extra), nm, len(miss), len(extra)))
            n = tot["ref"]
            say(f"  reference boxes      : {n}   (strong, conf >= 0.5: {tot['strongRef']})")
            say(f"  found by the NPU     : {tot['matched']} ({pct(tot['matched'], n)})   missed {tot['missed']}   extra {tot['extra']}")
            say(f"  strong boxes missed  : {tot['strongMissed']} of {tot['strongRef']} ({pct(tot['strongMissed'], tot['strongRef'])})")
            if cdiff:
                say(f"  confidence difference: mean {np.mean(cdiff):.3f}, max {np.max(cdiff):.3f}    box overlap (IoU) mean {np.mean(ious):.3f}")
            for c, (a, b) in sorted(per_cls.items()):
                say(f"      {c:11s} {b}/{a} found ({pct(b, a)})")
            for _, nm, m_, e_ in sorted(worst, reverse=True)[:5]:
                say(f"      most different: {nm}  missed {m_}, extra {e_}")
            # agreement rules (each one a problem of its own): strong boxes missed <= 3 %, boxes found >= 95 %,
            # extra boxes <= 10 %, mean confidence difference <= 0.05, every class with >= 5 boxes found >= 80 %
            fails = []
            if tot["strongRef"] and tot["strongMissed"] / tot["strongRef"] > 0.03:
                fails.append(f"misses {pct(tot['strongMissed'], tot['strongRef'])} of the strong detections")
            if n and tot["matched"] / n < 0.95:
                fails.append(f"finds only {pct(tot['matched'], n)} of the boxes")
            if n and tot["extra"] / n > 0.10:
                fails.append(f"adds {tot['extra']} extra boxes ({pct(tot['extra'], n)})")
            if cdiff and float(np.mean(cdiff)) > 0.05:
                fails.append(f"confidence differs by {np.mean(cdiff):.3f} on average (thresholds would act differently)")
            for c, (a, b) in sorted(per_cls.items()):
                if a >= 5 and b / a < 0.8:
                    fails.append(f"finds only {b}/{a} {c} boxes")
            for f_ in fails:
                problem(f"{d.name} ({prec}) vs CPU: {f_}")
            if n and not fails:
                say(f"  verdict              : NPU detections agree with the CPU ({prec})")
            REPORT.setdefault("accuracy", {})[d.name] = {**tot, "perClass": per_cls,
                                                         "confDiffMean": round(float(np.mean(cdiff)), 4) if cdiff else None,
                                                         "iouMean": round(float(np.mean(ious)), 4) if ious else None}


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="RKNN / Rockchip NPU analysis (read-only)")
    ap.add_argument("--model")
    ap.add_argument("--only", default="system,runtime,models,bench,pipeline,accuracy")
    ap.add_argument("--runs", type=int, default=50)
    ap.add_argument("--streams", type=int, default=3)
    ap.add_argument("--seconds", type=float, default=8.0)
    ap.add_argument("--images")
    ap.add_argument("--camera")
    ap.add_argument("--frames", type=int, default=40)
    ap.add_argument("--out", default=str(REPO / "logs"))
    args = ap.parse_args()
    only = {s.strip() for s in args.only.split(",") if s.strip()}

    say(f"PYROTECH VISION - NPU / RKNN analysis   {time.strftime('%Y-%m-%d %H:%M:%S')}   user {os.environ.get('USER', '?')}")
    dirs = model_dirs(args.model)
    if "system" in only:
        section_system()
    if "runtime" in only:
        section_runtime()
    if "models" in only:
        section_models(dirs)
    if "bench" in only:
        section_bench(dirs, args.runs, max(1, args.streams), args.seconds)
    if only & {"pipeline", "accuracy"}:
        try:
            section_pipeline_accuracy(dirs, args, "pipeline" in only, "accuracy" in only)
        except Exception as e:
            problem(f"pipeline / accuracy test failed: {e}")

    head("PROBLEMS FOUND")
    if PROBLEMS:
        for p in PROBLEMS:
            OUT.append("  - " + p)
            print("  - " + p)
    else:
        say("  none")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    (out / f"npu_report_{stamp}.txt").write_text("\n".join(OUT) + "\n")
    REPORT["problems"] = PROBLEMS
    (out / f"npu_report_{stamp}.json").write_text(mask(json.dumps(REPORT, indent=1, default=str)))
    print(f"\nReport saved: {out / f'npu_report_{stamp}.txt'} (+ .json)")
    return 1 if PROBLEMS else 0


if __name__ == "__main__":
    sys.exit(main())
