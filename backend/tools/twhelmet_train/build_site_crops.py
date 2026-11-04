"""Paths are the constants below (OUT, public roots, site roots, metas): edit them for a new round.
in_v3b: own-camera data as the judge sees it. The judge never looks at a full 1080p frame: it crops one rider (person box +
50 % side margin) or one two-wheeler area (bike box stretched up, 100 % margin) and runs the model on that crop at 640 px. A full
frame at 640 px shrinks a 40 px head to 13 px, so in_v3 (full frames) learnt the wrong scale. Here every own-camera training
image is such a crop, with the head / bike labels re-expressed in crop coordinates. Public images stay as they are.
Split as in_v3: 11-02 + 11-03 -> train, 11-04 even tracks -> val, 11-04 odd tracks -> test_site."""
import os, re, json, glob, shutil, collections, sys
import cv2
sys.path.insert(0, '/home/claude/pyrotech-vision-')
import backend  # noqa
from backend.detection.crossing import judge as jd
OUT = '/tmp/claude-0/pub2/ds_v3b'; shutil.rmtree(OUT, ignore_errors=True)
for s in ('train', 'val', 'test_rf', 'test_kag', 'test_site'):
    os.makedirs(f'{OUT}/{s}/images'); os.makedirs(f'{OUT}/{s}/labels')
cnt = collections.Counter()
# public, unchanged
for tag, root, splits in (('rf', '/tmp/claude-0/pub2/rf_curated', {'train': 'train', 'val': 'val', 'test': 'test_rf'}),
                          ('kag', '/tmp/claude-0/pub2/kag_yolo', {'train': 'train', 'val': 'val', 'test': 'test_kag'})):
    for s, dst in splits.items():
        for img in sorted(glob.glob(f'{root}/{s}/images/*')):
            base = os.path.splitext(os.path.basename(img))[0]; lab = f'{root}/{s}/labels/{base}.txt'
            if not os.path.exists(lab): continue
            os.link(img, f'{OUT}/{dst}/images/{tag}_{base}.jpg'); shutil.copy(lab, f'{OUT}/{dst}/labels/{tag}_{base}.txt'); cnt[(tag, dst)] += 1
metas = {}
for jf in glob.glob('/tmp/claude-0/site/raw/**/*.json', recursive=True) + glob.glob('/tmp/claude-0/site2/raw/**/*.json', recursive=True):
    metas[os.path.basename(jf)[:-5]] = json.load(open(jf))
def crop_region(pbox, fw, fh, mx):
    x1, y1, x2, y2 = pbox; bw, bh = x2 - x1, y2 - y1
    return [max(0, int(x1 - mx * bw)), max(0, int(y1 - 0.10 * bh)), min(fw, int(x2 + mx * bw)), min(fh, int(y2 + 0.05 * bh))]
parked_budget = collections.Counter()
for root in ('/tmp/claude-0/site/dataset_cam02_2026-11-02', '/tmp/claude-0/site2/dataset_cam02_2026-11-03_04'):
    for lf in sorted(glob.glob(f'{root}/labels/*.txt')):
        base = os.path.basename(lf)[:-4]; day = base.split('_')[1]; tid = int(re.search(r'_t(\d+)$', base).group(1))
        dst = 'train' if day != '20261104' else ('val' if tid % 2 == 0 else 'test_site')
        img = cv2.imread(f'{root}/images/{base}.jpg'); fh, fw = img.shape[:2]
        boxes = []
        for l in open(lf):
            p = l.split()
            if len(p) != 5: continue
            c, cx, cy, w, h = int(p[0]), *map(float, p[1:])
            boxes.append((c, (cx - w / 2) * fw, (cy - h / 2) * fh, (cx + w / 2) * fw, (cy + h / 2) * fh))
        m = metas.get(base, {'twoWheelers': []})
        regions = []
        for t in m['twoWheelers']:
            if t.get('riders'):
                for p in t['riders']: regions.append(('person', crop_region(p['box'], fw, fh, 0.5)))
            else:
                regions.append(('bike-area', crop_region(jd.bike_rider_box(t['box'], fh, fw), fw, fh, 1.0)))
        k = 0
        for kind, (rx1, ry1, rx2, ry2) in regions:
            if rx2 - rx1 < 48 or ry2 - ry1 < 48: continue
            cw, ch = rx2 - rx1, ry2 - ry1
            lines = []; heads = 0
            for c, x1, y1, x2, y2 in boxes:
                ix1, iy1, ix2, iy2 = max(x1, rx1), max(y1, ry1), min(x2, rx2), min(y2, ry2)
                if ix2 <= ix1 or iy2 <= iy1: continue
                inside = (ix2 - ix1) * (iy2 - iy1) / ((x2 - x1) * (y2 - y1))
                if c in (1, 2):
                    if inside < 0.6: continue
                    heads += 1
                elif inside < 0.4: continue
                lines.append(f"{c} {(ix1 + ix2) / 2 / cw - rx1 / cw:.6f} {(iy1 + iy2) / 2 / ch - ry1 / ch:.6f} {(ix2 - ix1) / cw:.6f} {(iy2 - iy1) / ch:.6f}")
            if heads == 0:
                if kind == 'person': continue                      # rider crop without a usable head label: ambiguous, skip
                if parked_budget[dst] >= {'train': 160, 'val': 40, 'test_site': 40}[dst]: continue
                parked_budget[dst] += 1
            name = f'site_{base}_c{k}'; k += 1
            cv2.imwrite(f'{OUT}/{dst}/images/{name}.jpg', img[ry1:ry2, rx1:rx2], [cv2.IMWRITE_JPEG_QUALITY, 92])
            open(f'{OUT}/{dst}/labels/{name}.txt', 'w').write('\n'.join(lines) + '\n')
            cnt[('site-' + kind, dst)] += 1; cnt[('site-heads', dst)] += heads
names = "nc: 4\nnames: ['bike', 'helmet', 'no-helmet', 'number-plate']\n"
open(f'{OUT}/data.yaml', 'w').write(f"path: {OUT}\ntrain: train/images\nval: val/images\ntest: test_site/images\n{names}")
for t in ('rf', 'kag', 'site'):
    open(f'{OUT}/test_{t}.yaml', 'w').write(f"path: {OUT}\ntrain: train/images\nval: val/images\ntest: test_{t}/images\n{names}")
for k, v in sorted(cnt.items()): print(k, v)
for s in ('train', 'val', 'test_site'):
    c = collections.Counter()
    for lf in glob.glob(f'{OUT}/{s}/labels/*.txt'):
        for l in open(lf):
            if l.strip(): c[l.split()[0]] += 1
    print(s, len(glob.glob(f'{OUT}/{s}/images/*')), 'imgs', dict(c))
