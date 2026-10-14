"""Backend self-check:  python backend/main.py --check

Verifies on THIS computer, without a camera, that the backend can run from the repository alone:
  * Python packages import, with their versions
  * model weights + tracker config exist inside backend/models
  * every runtime path (model, tracker, data, snapshots, Python itself) lies inside the repository
  * the existing pipeline loads and detects objects in Ultralytics' bundled sample image (bus.jpg)
  * the other modules' models load from backend/models: Human Count, Head count, Fire & Smoke (with its
    smoke verifier), Object Monitoring (YOLOE + its vocabulary cache, CSRT tracker of the contrib OpenCV)
  * data and snapshot folders are writable
Exit code 0 = OK, 1 = a required check failed. Warnings do not fail the check.
"""
import os
import sys
import time
from pathlib import Path


def run():
    from backend import config
    repo = config.REPO_ROOT.resolve()
    fails, warns = [], []

    def inside(p):
        try:
            Path(p).resolve().relative_to(repo)
            return True
        except ValueError:
            return False

    def line(status, text):
        print(f"  [{status:4}] {text}")
        if status == "FAIL":
            fails.append(text)
        elif status == "WARN":
            warns.append(text)

    print(f"PYROTECH VISION backend self-check\n  repository : {repo}\n  python     : {sys.executable} ({sys.version.split()[0]})")

    # 1. packages
    versions = {}
    for mod in ("ultralytics", "torch", "torchvision", "cv2", "numpy", "lap", "psutil", "openvino"):
        try:
            m = __import__(mod)
            versions[mod] = getattr(m, "__version__", "ok")
            line("OK", f"import {mod} {versions[mod]}")
        except Exception as e:  # noqa: BLE001
            if mod == "openvino":   # optional: faster CPU inference of the same model
                line("WARN", f"import openvino: {e}  -> optional speed-up not installed, PyTorch is used "
                     f"({'setup-backend.cmd' if os.name == 'nt' else './start.sh --reinstall'} installs it where available)")
            else:
                line("FAIL", f"import {mod}: {e}  -> run setup-backend.cmd")
    if fails:
        return _done(fails, warns)

    # 2. paths stay inside the repository
    for name, p in (("model", config.MODEL_PATH), ("openvino model", config.OPENVINO_MODEL_PATH / "yolov8n.xml"), ("tracker", config.TRACKER_CONFIG),
                    ("data", config.DATA_DIR), ("snapshots", config.SNAPSHOT_DIR)):
        if name == "openvino model" and not Path(p).is_file():
            line("WARN", f"{name} missing: {p} (PyTorch is used)")
        elif name in ("model", "tracker") and not Path(p).is_file():
            line("FAIL", f"{name} file missing: {p}")
        elif inside(p):
            line("OK", f"{name:9}: {Path(p).resolve().relative_to(repo)}")
        else:
            line("WARN", f"{name:9}: {p} is OUTSIDE the repository (set in .env / environment)")
    if inside(sys.prefix):
        line("OK", f"python env: {Path(sys.prefix).resolve().relative_to(repo)}")
    else:
        line("WARN", f"python env {sys.prefix} is outside the repository - run setup-backend.cmd to create backend\\.venv")
    for d in (config.DATA_DIR, config.SNAPSHOT_DIR):
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
            probe = Path(d) / ".selfcheck"
            probe.write_text("ok")
            probe.unlink()
        except OSError as e:
            line("FAIL", f"not writable: {d} ({e})")
    if fails:
        return _done(fails, warns)

    # CPU thread settings from .env (AIV_TORCH_THREADS, ...), applied the same way as the backend does
    from backend import runtime
    st = runtime.apply()
    for e in st["errors"]:
        line("WARN", f"CPU settings: {e}")
    req = {k: v for k, v in st["requested"].items() if v}
    line("OK", "CPU settings: " + (", ".join(f"{k}={v}" for k, v in req.items()) if req else "application defaults")
         + f" ({os.cpu_count()} CPUs)")

    # 3. the existing pipeline on a real image (no camera needed)
    import cv2
    import ultralytics
    from backend.detection.modules import module_config
    from backend.detection.pipeline import PersonVehiclePipeline
    sample = Path(ultralytics.__file__).parent / "assets" / "bus.jpg"
    img = cv2.imread(str(sample))
    if img is None:
        line("WARN", "Ultralytics sample image not found - detection test skipped")
        return _done(fails, warns)
    t0 = time.time()
    engine, model_path = config.resolve_engine()
    line("OK", f"inference engine: {engine} ({Path(model_path).name})")
    p = PersonVehiclePipeline(model_path, config.TRACKER_CONFIG, config.IMG_SIZE, config.DEVICE, engine=engine)
    plan = p.build_plan(module_config({}, "human"), module_config({}, "vehicle"), [])
    labels, ms = set(), 0.0
    for _ in range(6):   # MIN_HITS_TO_CONFIRM + label voting need several frames
        _, _, new, ms = p.process(img.copy(), plan)
        labels |= {n["label"] for n in new}
    if {"Bus", "Person"} <= labels:
        line("OK", f"detection on sample image: {', '.join(sorted(labels))} (model load + 6 frames {time.time() - t0:.1f}s, last inference {ms:.0f} ms)")
    else:
        line("FAIL", f"detection on sample image found {sorted(labels) or 'nothing'} (expected Bus and Person)")
    # 4. Human Count engine (own detector + tracker) on the same image
    from backend.detection.human_count import HumanCounter
    hc = HumanCounter(model_path, config.IMG_SIZE, config.DEVICE, engine=engine)
    hc.configure(module_config({}, "count"), [])
    t_now = time.time()      # real clock values: Windows cannot convert times close to 1970-01-01
    for i in range(4):
        hc.process(img.copy(), img.copy(), t_now + i * 0.1)
    if hc.current >= 3 and hc.unique == hc.current:
        line("OK", f"Human Count on sample image: {hc.current} people now, {hc.unique} counted once each")
    else:
        line("FAIL", f"Human Count on sample image: {hc.current} people now, {hc.unique} counted (expected >= 3, each counted once)")
    # 5. Head count model (dedicated head detector, joined from its .part files on first use)
    try:
        from backend.detection import head_count as headc
        t0 = time.time()
        r = headc.count_heads(img, [], {})
        n = len(r["heads"])
        msg = f"Head count model ({r['engine']}) on sample image: {n} heads (load + count {time.time() - t0:.1f}s, detection {r['inferenceMs']:.0f} ms)"
        line("OK" if n >= 3 else "FAIL", msg + ("" if n >= 3 else " - expected >= 3"))
    except Exception as e:
        line("FAIL", f"Head count model could not run: {e}")
    # 6. Fire & Smoke (own model + smoke verifier tied to its weights)
    try:
        from backend.detection import fire_smoke as fs
        t0 = time.time()
        engine, mpath = fs.resolve_engine()
        det = fs.Detector(fs.WEIGHTS, 480, 0.45, engine=engine, model_path=mpath)
        det.configure({"fire": 0.30, "smoke": 0.65}, {"smoke": 0.40}, 0.45, 480, True)
        det.enable_verifier(fs.VERIFIER_FILE, 0.40, 4)
        det.detect(img)
        line("OK" if det.verifier else "WARN", f"Fire & Smoke model {fs.WEIGHTS.name} ({'OpenVINO' if det.engine == 'openvino' else 'PyTorch'}) loaded, classes {sorted(det.names.values())}, "
             f"smoke verifier {'on' if det.verifier else 'NOT available: ' + str(det.verifier_error)} ({time.time() - t0:.1f}s)")
    except Exception as e:
        line("FAIL", f"Fire & Smoke model could not run: {e}")
    # 7. Object Monitoring (Layer 2 YOLOE with the cached vocabulary, Layer 1 tracker, optional Layer 3)
    try:
        import cv2
        from backend.detection.modules import DEFAULTS
        from backend.detection.object_monitor import engine as oe
        from backend.detection.object_monitor.open_vocab import OpenVocabDetector
        from backend.detection.object_monitor.tracker import create_tracker
        d = DEFAULTS["object"]["detector"]
        t0 = time.time()
        ov = OpenVocabDetector(d["model"], d["vocabulary"], oe.MODELS_DIR, d["imgsz"], d["conf"], d["iou"], d["tiles"])
        people = [x for x in ov.detect(img) if x.name == "person"]
        line("OK" if people else "FAIL", f"Object Monitoring Layer 2 {d['model']} with the cached vocabulary: {len(people)} people in the sample image ({time.time() - t0:.1f}s)")
        trk = create_tracker("csrt")
        csrt = hasattr(cv2, "TrackerCSRT_create") or hasattr(getattr(cv2, "legacy", None), "TrackerCSRT_create")
        line("OK" if csrt else "WARN", f"Object Monitoring tracker: {type(trk).__name__ if trk else 'none'}" +
             ("" if csrt else " - CSRT needs the contrib OpenCV build (run setup-backend.cmd); MIL is used meanwhile"))
        ok3, why = oe.dino_available()
        line("OK" if ok3 else "INFO", "Object Monitoring Layer 3 (Grounding DINO): " + ("installed" if ok3 else f"not installed (optional) - {why}"))
    except Exception as e:
        line("FAIL", f"Object Monitoring could not start: {e}")
    return _done(fails, warns)


def _done(fails, warns):
    print(f"\nResult: {'FAILED' if fails else 'OK'} - {len(fails)} failure(s), {len(warns)} warning(s)")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
