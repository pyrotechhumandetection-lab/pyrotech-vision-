"""Alert snapshot annotation and the on-video overlay of marked objects (Object Monitoring).

annotate_alert / draw_dashed_rect: unchanged from the standalone monitor's src/alerts/alert_manager.py.
draw_objects: the marked-object part of its src/ui/display.py render(), drawn on the shared annotated
camera stream (the desktop window, mouse box-selector and key help are replaced by the web UI).
"""
from __future__ import annotations

from datetime import datetime

import cv2
import numpy as np

from .common import box_center

FONT = cv2.FONT_HERSHEY_SIMPLEX
STATE_COLORS = {
    "STABLE": (0, 200, 0), "CHECKING": (0, 215, 255), "OCCLUDED": (0, 165, 255),
    "MOVED": (0, 0, 255), "MISSING": (0, 0, 255), "PAUSED": (170, 170, 170),
}
FONT = cv2.FONT_HERSHEY_SIMPLEX

def draw_dashed_rect(img, box, color, thickness=2, dash=10) -> None:
    x, y, w, h = [int(v) for v in box]
    for x1, y1, x2, y2 in ((x, y, x + w, y), (x + w, y, x + w, y + h), (x + w, y + h, x, y + h), (x, y + h, x, y)):
        length = int(np.hypot(x2 - x1, y2 - y1))
        for s in range(0, length, dash * 2):
            a, b = s / max(1, length), min(1.0, (s + dash) / max(1, length))
            cv2.line(img, (int(x1 + (x2 - x1) * a), int(y1 + (y2 - y1) * a)),
                     (int(x1 + (x2 - x1) * b), int(y1 + (y2 - y1) * b)), color, thickness, cv2.LINE_AA)


def annotate_alert(frame: np.ndarray, view: dict, kind: str, when: datetime, distance: float) -> np.ndarray:
    """Full-resolution copy of the frame showing where the object was and where it is now,
    with a red text strip ADDED above the picture (the camera's own on-screen clock stays visible)."""
    img = frame.copy()
    h_img = img.shape[0]
    t = max(2, h_img // 360)
    scale = max(0.6, h_img / 1100)
    red, cyan = (0, 0, 255), (255, 255, 0)
    hx, hy, hw, hh = view["home"]
    draw_dashed_rect(img, view["home"], cyan, t)
    cv2.putText(img, "was here", (hx, max(15, hy - 6)), cv2.FONT_HERSHEY_SIMPLEX, scale * 0.8, cyan, t, cv2.LINE_AA)
    if kind == "moved":
        cx, cy, cw, ch = [int(v) for v in view["current"]]
        cv2.rectangle(img, (cx, cy), (cx + cw, cy + ch), red, t + 1)
        cv2.arrowedLine(img, tuple(int(v) for v in box_center(view["home"])),
                        tuple(int(v) for v in box_center(view["current"])), red, t + 1, cv2.LINE_AA, tipLength=0.08)
        cv2.putText(img, f"now here ({distance:.0f} px)", (cx, max(15, cy - 6)), cv2.FONT_HERSHEY_SIMPLEX,
                    scale * 0.8, red, t, cv2.LINE_AA)
    else:
        cv2.line(img, (hx, hy), (hx + hw, hy + hh), red, t)
        cv2.line(img, (hx + hw, hy), (hx, hy + hh), red, t)
    banner = f"{when:%Y-%m-%d %H:%M:%S}  {kind.upper()}: #{view['id']} {view['name']}"
    if kind == "moved":
        banner += f"  moved {distance:.0f} px"
    (tw, th), base = cv2.getTextSize(banner, cv2.FONT_HERSHEY_SIMPLEX, scale, t)
    strip = np.zeros((th + base + 16, img.shape[1], 3), np.uint8)
    strip[:] = (0, 0, 170)
    cv2.putText(strip, banner, (10, th + 8), cv2.FONT_HERSHEY_SIMPLEX, scale, (255, 255, 255), t, cv2.LINE_AA)
    return np.vstack([strip, img])


def _label(img, text, x, y, color, scale=0.5) -> None:
    (tw, th), base = cv2.getTextSize(text, FONT, scale, 1)
    y = max(th + 4, y)
    cv2.rectangle(img, (x, y - th - base - 3), (x + tw + 6, y + 1), color, -1)
    cv2.putText(img, text, (x + 3, y - base), FONT, scale, (255, 255, 255), 1, cv2.LINE_AA)


def _sbox(box, s):
    return tuple(int(v * s) for v in box)



def draw_objects(img: np.ndarray, objects: list[dict], detections: list, show_detections: bool) -> np.ndarray:
    """Marked objects (state colours as in the original window) and, optionally, Layer 2 labels.
    Boxes are full-resolution pixels; drawn in place on the full-resolution annotated frame."""
    s = 1.0
    t = max(2, img.shape[0] // 540)
    fs = max(0.5, img.shape[0] / 1080 * 0.7)
    if show_detections:  # Layer 2 names (grey = objects, light = people)
        for d in detections:
            x, y, bw, bh = _sbox(d.box, s)
            color = (200, 200, 200) if d.name == "person" else (140, 140, 140)
            cv2.rectangle(img, (x, y), (x + bw, y + bh), color, 1)
            cv2.putText(img, f"{d.name} {d.conf:.2f}", (x + 2, max(10, y - 3)), FONT, fs * 0.75, color, 1, cv2.LINE_AA)
    for o in objects:  # Layer 1 marked objects
        color = STATE_COLORS.get(o["state"], (255, 255, 255))
        home = _sbox(o["home"], s)
        state = o["state"]
        if state == "MOVED":
            draw_dashed_rect(img, home, (255, 255, 0), max(1, t - 1), 8)
            cur = _sbox(o["current"], s)
            cv2.rectangle(img, cur[:2], (cur[0] + cur[2], cur[1] + cur[3]), color, t)
            cv2.arrowedLine(img, tuple(int(v) for v in box_center(home)), tuple(int(v) for v in box_center(cur)),
                            color, t, cv2.LINE_AA, tipLength=0.1)
            _label(img, f"#{o['id']} {o['name']}  MOVED {o['distance']:.0f}px", cur[0], cur[1] - 2, color, fs)
        else:
            box = _sbox(o["current"] if state == "STABLE" else o["home"], s)
            if state in ("MISSING", "OCCLUDED", "PAUSED"):
                draw_dashed_rect(img, box, color, t, 10)
            else:
                cv2.rectangle(img, box[:2], (box[0] + box[2], box[1] + box[3]), color, t)
            _label(img, f"#{o['id']} {o['name']}  {state} {o['score']:.2f}", box[0], box[1] - 2, color, fs)
        if o.get("tracker_box") and state != "STABLE":
            tb = _sbox(o["tracker_box"], s)
            cv2.rectangle(img, tb[:2], (tb[0] + tb[2], tb[1] + tb[3]), (0, 255, 255), 1)
    return img
