"""Find the camera picture inside a screen recording of the operator page (used by run_recording.py --screen).
The picture keeps its width at one browser zoom; only its vertical position changes with scrolling. Adjust X1, X2, H for
another recording (read them off one frame)."""
import cv2, numpy as np, sys
X1, X2, H = 386, 1378, 558          # the picture keeps its width at this browser zoom; only its vertical position moves (scroll)
def locate(frame):
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    rows = g[:, X1:X2].mean(axis=1) < 205
    # longest run of dark rows
    best, cur, start = (0, 0), 0, 0
    for i, v in enumerate(np.append(rows, False)):
        if v: 
            if cur == 0: start = i
            cur += 1
        else:
            if cur > best[1]: best = (start, cur)
            cur = 0
    y, n = best
    if not 540 <= n <= 580: return None
    cols = g[y:y + n, :].mean(axis=0) < 205
    if cols[X1 + 20:X2 - 20].mean() < 0.95 or cols[X1 - 12] or cols[X2 + 12]: return None   # white margins outside = real picture edges
    return X1, y, X2, y + H
if __name__ == "__main__":
    cap = cv2.VideoCapture(sys.argv[1]); n = int(cap.get(7))
    for fi in range(0, n, 250):
        cap.set(1, fi); ok, f = cap.read()
        if ok: print(fi, locate(f))
