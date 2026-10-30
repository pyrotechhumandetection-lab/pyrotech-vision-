"""Kaggle "Helmet Detection" (andrewmvd, 764 Pascal VOC images, BikesHelmets###.png) -> our 4-class YOLO layout.

    python3 convert_kaggle_helmet.py ../../models/yolov8s.pt <unzipped dir with images/ and annotations/> data/kaggle_helmet

Heads come from the XML (With Helmet / Without Helmet -> helmet / no-helmet); `bike` boxes are pseudo-labelled by the COCO
detector (same recipe as curate_heads_dataset.py); number-plate is left unlabelled. The 17 boxes with coordinates scaled by
1000 (known corruption in this set) are divided by 1000. PNG is re-saved as JPEG q92. Split 70 / 20 / 10 by a hash of the
file name, so it is stable across runs. Images in which the COCO detector finds no motorcycle are dropped (--keep-all to
keep them): about 40 % of this set are cyclists with bicycle helmets, which are not what the rider model should learn
(in_v2: 761 -> 432 images kept, 452 helmet + 315 no-helmet heads)."""
import glob
import hashlib
import os
import sys
import xml.etree.ElementTree as ET

from PIL import Image
from ultralytics import YOLO

det = YOLO(sys.argv[1])
SRC, OUT = sys.argv[2], sys.argv[3]
KEEP_ALL = "--keep-all" in sys.argv
for s in ('train','val','test'):
    os.makedirs(f'{OUT}/{s}/images', exist_ok=True); os.makedirs(f'{OUT}/{s}/labels', exist_ok=True)
stats = {'img':0,'helmet':0,'no':0,'fixed':0,'bikes':0,'nobike':0,'bicycle':0}
for xf in sorted(glob.glob(f'{SRC}/annotations/*.xml')):
    r = ET.parse(xf).getroot(); fn = r.find('filename').text
    ip = f'{SRC}/images/{fn}'
    if not os.path.exists(ip): continue
    im = Image.open(ip).convert('RGB'); W, H = im.size
    lines = []
    for o in r.findall('object'):
        n = o.find('name').text.strip().lower(); b = o.find('bndbox')
        x1,y1,x2,y2 = (float(b.find(k).text) for k in ('xmin','ymin','xmax','ymax'))
        if x2 > W*1.5 or y2 > H*1.5:
            x1,y1,x2,y2 = x1/1000,y1/1000,x2/1000,y2/1000; stats['fixed'] += 1
        x1,x2 = max(0,min(x1,x2)), min(W,max(x1,x2)); y1,y2 = max(0,min(y1,y2)), min(H,max(y1,y2))
        if x2-x1 < 2 or y2-y1 < 2: continue
        c = 1 if n == 'with helmet' else 2 if n == 'without helmet' else None
        if c is None: continue
        stats['helmet' if c == 1 else 'no'] += 1
        lines.append(f'{c} {(x1+x2)/2/W:.6f} {(y1+y2)/2/H:.6f} {(x2-x1)/W:.6f} {(y2-y1)/H:.6f}')
    if not lines: continue
    res = det.predict(im, imgsz=640, conf=0.4, verbose=False)[0]
    cls = res.boxes.cls.tolist(); xyxyn = res.boxes.xyxyn.tolist()
    if any(int(c)==1 for c in cls): stats['bicycle'] += 1
    bikes = [b for c,b in zip(cls,xyxyn) if int(c)==3]
    if not bikes:
        stats['nobike'] += 1
        if not KEEP_ALL:
            continue
    for bx1,by1,bx2,by2 in bikes:
        lines.append(f'0 {(bx1+bx2)/2:.6f} {(by1+by2)/2:.6f} {bx2-bx1:.6f} {by2-by1:.6f}'); stats['bikes'] += 1
    h = int(hashlib.md5(fn.encode()).hexdigest(), 16) % 10
    s = 'test' if h == 0 else 'val' if h in (1,2) else 'train'
    base = 'kag_' + os.path.splitext(fn)[0]
    im.save(f'{OUT}/{s}/images/{base}.jpg', quality=92)
    open(f'{OUT}/{s}/labels/{base}.txt','w').write('\n'.join(lines)+'\n'); stats['img'] += 1
open(f'{OUT}/data.yaml','w').write(f"path: {os.path.abspath(OUT)}\ntrain: train/images\nval: val/images\ntest: test/images\nnc: 4\nnames: ['bike', 'helmet', 'no-helmet', 'number-plate']\n")
print(stats, {s: len(os.listdir(f'{OUT}/{s}/images')) for s in ('train','val','test')})
