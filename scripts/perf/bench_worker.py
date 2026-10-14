"""PYROTECH VISION - benchmark ONE CPU configuration in its own process (started by pyrotech_perf.py).

Each configuration needs a fresh process: PyTorch's inter-op pool and OpenMP read their sizes only once.
The worker uses the application's own code and settings (backend/config.py, backend/runtime.py, the
Person/Vehicle pipeline with ByteTrack, the Human Count engine, the Fire & Smoke detector) on the saved
representative frames, at the same model, engine and input size as the running application.

    python scripts/perf/bench_worker.py JOB.json      -> one JSON line on stdout ("RESULT {...}")

JOB: {"frames": [paths], "streams": S, "count": bool, "fire": bool, "seconds": D, "warmup": W,
      "idle": bool, "accuracy": bool, "label": str}
The CPU settings under test arrive as environment variables (AIV_TORCH_THREADS, ...), exactly as the
backend reads them from .env.
"""
import json
import os
import resource
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def lower_priority():
    """Never take CPU from the running application: lowest nice value + SCHED_IDLE (Linux)."""
    try:
        os.nice(19)
    except OSError:
        pass
    try:
        os.sched_setscheduler(0, os.SCHED_IDLE, os.sched_param(0))
    except (AttributeError, OSError):
        pass


def pct(xs, p):
    if not xs:
        return None
    xs = sorted(xs)
    k = min(len(xs) - 1, max(0, int(round(p / 100.0 * (len(xs) - 1)))))
    return xs[k]


def main():
    with open(sys.argv[1]) as f:
        job = json.load(f)
    job["frames"] = [os.path.abspath(p) for p in job["frames"]]       # before chdir below
    if job.get("idle"):
        lower_priority()
    sys.path.insert(0, REPO)
    os.chdir(os.path.join(REPO, "backend"))
    t_start = time.time()
    from backend import config, runtime            # reads .env + the CPU settings under test (environment wins)
    runtime.apply()
    import cv2
    from backend.detection.modules import DEFAULTS
    from backend.detection.pipeline import PersonVehiclePipeline

    # frames: decoded once, kept in memory (at most ~240 MB)
    frames = []
    budget = 240e6
    for p in job["frames"]:
        img = cv2.imread(p, cv2.IMREAD_COLOR)
        if img is None:
            continue
        if sum(f.nbytes for f in frames) + img.nbytes > budget:
            break
        frames.append(img)
    if not frames:
        print("RESULT " + json.dumps({"error": "no readable frames"}))
        return 2

    engine, path = config.resolve_engine()
    S = max(1, int(job.get("streams", 1)))
    use_count = bool(job.get("count"))
    t_load = time.time()
    pipes, counters = [], []
    for _ in range(S):
        pipes.append(PersonVehiclePipeline(path, config.TRACKER_CONFIG, config.IMG_SIZE, config.DEVICE, engine=engine))
        if use_count:
            from backend.detection.human_count import HumanCounter
            c = HumanCounter(path, config.IMG_SIZE, config.DEVICE, engine=engine)
            c.configure(DEFAULTS["count"], [])
            counters.append(c)
    plan = pipes[0].build_plan(DEFAULTS["human"], DEFAULTS["vehicle"], [])
    fire = None
    if job.get("fire"):
        from backend.detection import fire_smoke
        fire = fire_smoke.FireEngine(lambda *a: None)
        fire.configure(DEFAULTS["fire"], [])
    load_s = time.time() - t_load

    stop = threading.Event()
    go = threading.Barrier(S + (1 if fire else 0) + 1)
    lat = [[] for _ in range(S + 1)]
    stages = [{"preprocess": 0.0, "inference": 0.0, "postprocess": 0.0, "tracking": 0.0, "slotWait": 0.0, "n": 0}
              for _ in range(S)]
    done = [0] * (S + 1)
    errors = []

    def one(i, f, t_cap):
        frame = f.copy()
        c0 = time.perf_counter()
        share = counters[i].detect_conf if (use_count and config.SHARED_PERSON_INFERENCE) else None
        runtime.take_wait_ms()
        pipes[i].process(frame, plan, share_person_conf=share)
        if use_count:
            counters[i].process(f, frame, t_cap, dets=pipes[i].shared_persons if share is not None else None)
        return (time.perf_counter() - c0) * 1000.0, runtime.take_wait_ms()

    def stream(i):
        runtime.name_thread(f"ai:bench{i}")
        try:
            k = (i * 7) % len(frames)       # each stream starts at another frame (different scenes in flight)
            t_cap = 1000.0
            for _ in range(int(job.get("warmup", 3))):
                one(i, frames[k % len(frames)], t_cap)
                k += 1
                t_cap += 0.2
            go.wait()
            while not stop.is_set():
                ms, w = one(i, frames[k % len(frames)], t_cap)
                st = pipes[i].last_stages
                if not stop.is_set():
                    lat[i].append(ms)
                    done[i] += 1
                    for key in ("preprocess", "inference", "postprocess", "tracking"):
                        stages[i][key] += st.get(key, 0.0)
                    stages[i]["slotWait"] += w
                    stages[i]["n"] += 1
                k += 1
                t_cap += 0.2
        except threading.BrokenBarrierError:
            pass
        except Exception as e:  # noqa: BLE001
            errors.append(f"stream {i}: {type(e).__name__}: {e}")
            stop.set()
            try:
                go.abort()
            except Exception:  # noqa: BLE001
                pass

    def fire_stream():
        runtime.name_thread("fire:bench")
        try:
            k = 3
            fire.detector.detect(frames[0])
            go.wait()
            while not stop.is_set():
                c0 = time.perf_counter()
                with runtime.gated():
                    fire.detector.detect(frames[k % len(frames)])
                if not stop.is_set():
                    lat[S].append((time.perf_counter() - c0) * 1000.0)
                    done[S] += 1
                k += 1
        except threading.BrokenBarrierError:
            pass
        except Exception as e:  # noqa: BLE001
            errors.append(f"fire: {type(e).__name__}: {e}")
            stop.set()

    threads = [threading.Thread(target=stream, args=(i,), daemon=True) for i in range(S)]
    if fire:
        threads.append(threading.Thread(target=fire_stream, daemon=True))
    for t in threads:
        t.start()
    try:
        go.wait(timeout=600)
    except threading.BrokenBarrierError:
        pass
    cpu0, w0 = os.times(), time.perf_counter()
    t_end = w0 + float(job.get("seconds", 20))
    while time.perf_counter() < t_end and not stop.is_set():
        time.sleep(0.1)
    stop.set()
    el = time.perf_counter() - w0
    cpu1 = os.times()
    for t in threads:
        t.join(timeout=30)
    if fire:
        fire.close()
    cpu_pct = ((cpu1.user - cpu0.user) + (cpu1.system - cpu0.system)) / el * 100.0

    all_lat = [x for i in range(S) for x in lat[i]]
    per_stream = [round(done[i] / el, 2) for i in range(S)]
    res = {
        "label": job.get("label"), "engine": engine, "imgSize": config.IMG_SIZE, "frames": len(frames),
        "frameSize": f"{frames[0].shape[1]}x{frames[0].shape[0]}", "streams": S, "count": use_count,
        "fire": bool(fire), "seconds": round(el, 1), "loadSeconds": round(load_s, 1),
        "fpsTotal": round(sum(done[:S]) / el, 2), "fpsPerStream": per_stream,
        "latencyMsP50": round(pct(all_lat, 50), 1) if all_lat else None,
        "latencyMsP95": round(pct(all_lat, 95), 1) if all_lat else None,
        "fireFps": round(done[S] / el, 2) if fire else None,
        "fireLatencyMsP50": round(pct(lat[S], 50), 1) if fire and lat[S] else None,
        "cpuPct": round(cpu_pct, 1), "rssMaxMb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        "cpuMsPerFrame": round(cpu_pct * 10.0 / max(0.01, (sum(done[:S]) + done[S]) / el), 1),
        "settings": runtime.snapshot(), "errors": errors + runtime.STATE["errors"],
    }
    n = sum(s["n"] for s in stages)
    if n:
        res["stageMs"] = {k: round(sum(s[k] for s in stages) / n, 1)
                          for k in ("preprocess", "inference", "postprocess", "tracking", "slotWait")}

    if job.get("accuracy") and not errors:
        # raw model output for every frame, compared by the parent with the reference config. A fresh model
        # instance: the pipeline's instance has the tracker attached, which replaces boxes by track boxes.
        from ultralytics import YOLO
        model = YOLO(str(path), task="detect")
        out = []
        for f in frames:
            r = model.predict(f, imgsz=config.IMG_SIZE, conf=plan["predict_conf"], classes=plan["coco"], verbose=False)[0]
            b = r.boxes
            out.append([] if b is None else [[round(v, 2) for v in xy] + [round(float(c), 4), int(k)]
                                             for xy, c, k in zip(b.xyxy.tolist(), b.conf.tolist(), b.cls.tolist())])
        res["detections"] = out
        res["predictConf"] = plan["predict_conf"]
    res["wallSeconds"] = round(time.time() - t_start, 1)
    sys.stdout.write("RESULT " + json.dumps(res) + "\n")
    sys.stdout.flush()
    os._exit(0)          # do not wait for daemon threads / model teardown


if __name__ == "__main__":
    sys.exit(main())
