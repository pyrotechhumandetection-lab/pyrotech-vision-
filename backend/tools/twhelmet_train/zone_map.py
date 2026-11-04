"""Camera-view map of where the helmet judge is reliable, from judge_site.py output.

    python3 zone_map.py --judge judge_v3.json --frame <clean camera frame.jpg> --out zone_cam02.jpg [--title "..."] [--cell 160]

For every labelled head (position = centre of the head in the frame) judge_site.py recorded whether the shipped judge confirmed
the right class, the wrong class, or left it unverified. This script smooths those outcomes over the picture (Gaussian kernel,
bandwidth = one cell) and paints:
  * green   - reliable: >= 60 % of heads there are confirmed and >= 95 % of the confirmations are right (needs >= 4 heads of support)
  * yellow  - usable: confirmations are right but fewer than 60 % of heads get a verdict (most riders stay "unverified")
  * red     - do not trust: a wrong confirmation occurred here, or heads are too small (median head < 28 px)
  * grey    - no data: no labelled rider has crossed that part of the picture
Every head is drawn as a dot (green = confirmed right, red = confirmed wrong, white = unverified). The highest-accuracy region
is outlined, with the numbers behind it printed in the legend. Also writes <out>.json with the per-cell table."""
import argparse
import json
import os

import cv2
import numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge", required=True)
    ap.add_argument("--frame", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="")
    ap.add_argument("--cell", type=int, default=160)
    ap.add_argument("--at", choices=("head", "ground"), default="ground",
                    help="where each head's outcome is placed: at the head, or at the point where its two-wheeler touches the ground "
                         "(default) - the line is crossed by that point, so this is the map to draw the line with")
    a = ap.parse_args()
    J = json.load(open(a.judge))
    recs = J["records"]
    bg = cv2.imread(a.frame)
    H, W = bg.shape[:2]
    pts = np.array([[r.get("gx", r["cx"]), r.get("gy", r["cy"])] if a.at == "ground" else [r["cx"], r["cy"]] for r in recs], float)
    right = np.array([1.0 if (r["label"] == "helmet" and r["verdict"] == "HELMET") or (r["label"] == "no-helmet" and r["verdict"] == "MISSING") else 0.0 for r in recs])
    wrong = np.array([1.0 if r["verdict"] in ("HELMET", "MISSING") and not rr else 0.0 for r, rr in zip(recs, right)])
    size = np.array([min(r["w"], r["h"]) for r in recs], float)
    # kernel-smoothed fields on a coarse grid
    step = 20
    gx, gy = np.meshgrid(np.arange(0, W, step) + step / 2, np.arange(0, H, step) + step / 2)
    bw = a.cell
    d2 = ((gx[..., None] - pts[:, 0]) ** 2 + (gy[..., None] - pts[:, 1]) ** 2) / (2 * bw * bw)
    k = np.exp(-d2)
    n_eff = k.sum(-1)
    frac_right = (k * right).sum(-1) / np.maximum(n_eff, 1e-9)
    frac_wrong = (k * wrong).sum(-1) / np.maximum(n_eff, 1e-9)
    med_size = (k * size).sum(-1) / np.maximum(n_eff, 1e-9)
    conf_rate = frac_right + frac_wrong
    precision = frac_right / np.maximum(conf_rate, 1e-9)
    cls = np.zeros(gx.shape, np.uint8)                    # 0 none, 1 red, 2 yellow, 3 green
    has = n_eff >= 4
    cls[has] = 2
    cls[has & ((frac_wrong * n_eff >= 0.5) | (med_size < 28))] = 1
    cls[has & (conf_rate >= 0.6) & (precision >= 0.95) & (med_size >= 28)] = 3
    col = {1: (40, 40, 230), 2: (40, 210, 240), 3: (60, 200, 60)}
    overlay = bg.copy()
    full = cv2.resize(cls, (W, H), interpolation=cv2.INTER_NEAREST)
    for c, bgr in col.items():
        overlay[full == c] = (0.45 * np.array(bgr) + 0.55 * overlay[full == c]).astype(np.uint8)
    overlay[full == 0] = (0.6 * overlay[full == 0]).astype(np.uint8)
    out = overlay
    # outline of the reliable region
    mask = (full == 3).astype(np.uint8) * 255
    cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    small = [c for c in cnts if cv2.contourArea(c) < 4 * a.cell * a.cell]          # specks smaller than 4 cells are not a zone
    cv2.drawContours(mask, small, -1, 0, -1); full[(mask == 0) & (full == 3)] = 2
    out = overlay.copy()
    for c, bgr in col.items():
        out[full == c] = (0.45 * np.array(bgr) + 0.55 * bg[full == c]).astype(np.uint8)
    out[full == 0] = (0.6 * bg[full == 0]).astype(np.uint8)
    cv2.drawContours(out, [c for c in cnts if cv2.contourArea(c) >= 4 * a.cell * a.cell], -1, (0, 255, 0), 3)
    # heads
    for (x, y), rr, ww in zip(pts, right, wrong):
        c = (0, 255, 0) if rr else (0, 0, 255) if ww else (255, 255, 255)
        cv2.circle(out, (int(x), int(y)), 5, c, -1); cv2.circle(out, (int(x), int(y)), 5, (0, 0, 0), 1)
    # per-cell table (coarse, for the json and the printed numbers)
    cells = []
    for cy0 in range(0, H, a.cell):
        for cx0 in range(0, W, a.cell):
            sel = (pts[:, 0] >= cx0) & (pts[:, 0] < cx0 + a.cell) & (pts[:, 1] >= cy0) & (pts[:, 1] < cy0 + a.cell)
            n = int(sel.sum())
            if n:
                cells.append({"x": cx0, "y": cy0, "heads": n, "confirmedRight": int(right[sel].sum()), "confirmedWrong": int(wrong[sel].sum()),
                              "medianHeadPx": float(np.median(size[sel]))})
    # numbers behind the green region
    gsel = np.array([full[int(min(H - 1, y)), int(min(W - 1, x))] == 3 for x, y in pts])
    g_n, g_r, g_w = int(gsel.sum()), int(right[gsel].sum()), int(wrong[gsel].sum())
    r_n, r_r, r_w = len(recs), int(right.sum()), int(wrong.sum())
    # legend
    pad = np.zeros((200, W, 3), np.uint8)
    f = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(pad, a.title or f"Helmet judge reliability map - {os.path.basename(J.get('weights', ''))}", (16, 34), f, 0.9, (255, 255, 255), 2)
    cv2.putText(pad, f"GREEN = highest accuracy: {g_n} labelled heads here, {g_r} confirmed right, {g_w} confirmed wrong, "
                     f"{g_n - g_r - g_w} left unverified  (precision {100 * g_r / max(1, g_r + g_w):.0f} %, verdict rate {100 * (g_r + g_w) / max(1, g_n):.0f} %)",
                (16, 72), f, 0.6, (120, 255, 120), 2)
    cv2.putText(pad, f"YELLOW = verdicts right but most riders unverified   RED = a wrong verdict occurred or heads < 28 px   GREY = no rider data yet",
                (16, 102), f, 0.6, (200, 200, 200), 1)
    cv2.putText(pad, f"Whole picture: {r_n} heads, {r_r} confirmed right, {r_w} confirmed wrong, {r_n - r_r - r_w} unverified  "
                     f"(precision {100 * r_r / max(1, r_r + r_w):.0f} %, verdict rate {100 * (r_r + r_w) / max(1, r_n):.0f} %)   "
                     f"dots: green right / red wrong / white unverified", (16, 132), f, 0.6, (200, 200, 200), 1)
    cv2.putText(pad, f"Data: labelled CAM-02 captures 2026-11-02..04, judged one frame at a time with the shipped rules (helmet >= {J['hmin']}, "
                     f"no-helmet >= {J['nmin']}, head >= {J['minHeadPx']} px). Live crossings add the vote rules on top.",
                (16, 162), f, 0.6, (200, 200, 200), 1)
    cv2.imwrite(a.out, np.vstack([out, pad]), [cv2.IMWRITE_JPEG_QUALITY, 92])
    json.dump({"weights": J.get("weights"), "cell": a.cell, "green": {"heads": g_n, "right": g_r, "wrong": g_w},
               "all": {"heads": r_n, "right": r_r, "wrong": r_w}, "cells": cells}, open(a.out + ".json", "w"), indent=1)
    print("green region:", g_n, g_r, g_w, "| all:", r_n, r_r, r_w)


if __name__ == "__main__":
    main()
