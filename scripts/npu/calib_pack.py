#!/usr/bin/env python3
"""Collect calibration images for an INT8 RKNN model of YOUR cameras (read-only: changes nothing).

    ./aivision.sh npu calib-pack                 -> logs/npu_calib_<date>.zip

Takes up to --snapshots snapshot originals (spread over all dates and cameras: people, vehicles) plus --frames
current frames from every camera (spread over time: also empty scenes, which INT8 must handle too), shrinks them
to 1280 px wide (the model works at 640) and zips them. Camera passwords are never written.
Run it once by day and once at night for the best result, then send the zip(s) for the INT8 conversion
(scripts/npu/convert_rknn.py build --int8 --calib DIR).
"""
import argparse
import os
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
os.chdir(REPO)

import cv2  # noqa: E402

from backend import config  # noqa: E402


def shrink(im, width=1280):
    h, w = im.shape[:2]
    return im if w <= width else cv2.resize(im, (width, round(h * width / w)), interpolation=cv2.INTER_AREA)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--snapshots", type=int, default=150)
    ap.add_argument("--frames", type=int, default=15, help="frames per camera (one every --every seconds)")
    ap.add_argument("--every", type=float, default=4.0)
    ap.add_argument("--out", default=str(REPO / "logs"))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    zpath = out / f"npu_calib_{time.strftime('%Y%m%d_%H%M%S')}.zip"
    n = 0
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_STORED) as z:
        # 1. snapshot originals, evenly spread over the whole history
        roots = [config.SNAPSHOT_DIR, REPO / "backend" / "snapshots"]
        files = []
        for r in dict.fromkeys(roots):
            if Path(r).is_dir():
                files += [p for p in Path(r).rglob("*original*.jpg")]
        files = sorted(set(files), key=lambda p: p.stat().st_mtime)
        if len(files) > args.snapshots:
            step = len(files) / args.snapshots
            files = [files[int(i * step)] for i in range(args.snapshots)]
        for p in files:
            im = cv2.imread(str(p))
            if im is None:
                continue
            ok, buf = cv2.imencode(".jpg", shrink(im), [cv2.IMWRITE_JPEG_QUALITY, 90])
            if ok:
                z.writestr(f"snap/{n:04d}_{p.name}", buf.tobytes())
                n += 1
        print(f"snapshot images: {n}")
        # 2. live frames from every camera (AI stream), spread over time
        if args.frames > 0:
            from backend.services.cameras import CameraRegistry
            reg = CameraRegistry()
            for cam in reg.list():
                if cam.get("enabled") is False:
                    continue
                try:
                    src, kind = reg.resolve_ai_source(cam)
                except ValueError as e:
                    print(f"{cam['id']}: skipped ({e})")
                    continue
                cap = cv2.VideoCapture(src, cv2.CAP_FFMPEG)
                got, t_next, t_end = 0, 0.0, time.time() + args.frames * args.every + 20
                while got < args.frames and time.time() < t_end:
                    ok, fr = cap.read()
                    if not ok:
                        time.sleep(0.2)
                        continue
                    if time.time() >= t_next:
                        ok2, buf = cv2.imencode(".jpg", shrink(fr), [cv2.IMWRITE_JPEG_QUALITY, 90])
                        if ok2:
                            z.writestr(f"live/{cam['id']}_{got:03d}.jpg", buf.tobytes())
                            got += 1
                            n += 1
                        t_next = time.time() + args.every
                cap.release()
                print(f"{cam['id']} ({kind} stream): {got} frames")
    print(f"\n{n} images -> {zpath} ({zpath.stat().st_size / 2**20:.1f} MB)")
    return 0 if n >= 50 else 1


if __name__ == "__main__":
    sys.exit(main())
