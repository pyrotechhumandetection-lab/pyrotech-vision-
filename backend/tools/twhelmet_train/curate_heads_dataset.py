"""Turn a public "helmet / no helmet" HEAD-box dataset (Roboflow YOLOv8 export: train/ valid/ test/, class 0 = with
helmet, class 1 = without helmet) into training data for our 4-class rider model.

    python3 curate_heads_dataset.py <roboflow export dir> <out dir> ../../models/yolov8s.pt

Such sets mix motorcycles with bicycles, kids' scooters and stock photos. Per image: keep it only if the COCO detector
finds a motorcycle (conf >= 0.4) and NO bicycle; drop near-duplicates (16x16 thumbnail hash) and images without a head
box; write the COCO motorcycle boxes as `bike` pseudo-labels and the original head boxes as helmet / no-helmet.
`number-plate` is left unlabelled (the fine-tune then learns nothing new about plates - acceptable, they are extra info).
Output: <out>/{train,val,test}/{images,labels}, data.yaml (our 4 class ids), manifest.json. Look at a contact sheet of
the result before training: a few far riders in crowded pictures are unlabelled in these sets (mild label noise).
Used for in_v1 with Roboflow Universe "Motorcycle Helmet" v1 (workspace gajanan-kalbande, CC BY 4.0, 629 images ->
313 kept, 340 helmet + 230 no-helmet heads)."""
import glob
import hashlib
import json
import os
import shutil
import sys

from PIL import Image
from ultralytics import YOLO

SRC, OUT = sys.argv[1], sys.argv[2]
det = YOLO(sys.argv[3])              # COCO detector (yolov8s.pt)
MC, BIC, PERSON = 3, 1, 0
seen, keep, drop = {}, [], {"dup": 0, "no_bike": 0, "bicycle": 0, "bad": 0, "no_head": 0}
for split in ("train", "valid", "test"):
    for f in sorted(glob.glob(f"{SRC}/{split}/images/*")):
        try:
            im = Image.open(f); im.verify(); im = Image.open(f).convert("RGB")
        except Exception:
            drop["bad"] += 1; continue
        h = hashlib.md5(im.resize((16, 16)).tobytes()).hexdigest()
        if h in seen: drop["dup"] += 1; continue
        seen[h] = f
        lp = f.replace("/images/", "/labels/").rsplit(".", 1)[0] + ".txt"
        heads = [l.split() for l in open(lp)] if os.path.exists(lp) else []
        if not heads: drop["no_head"] += 1; continue
        r = det.predict(im, imgsz=640, conf=0.3, verbose=False)[0]
        cls = r.boxes.cls.tolist(); xyxyn = r.boxes.xyxyn.tolist(); confs = r.boxes.conf.tolist()
        bikes = [b for c, b, p in zip(cls, xyxyn, confs) if int(c) == MC and p >= 0.4]
        if any(int(c) == BIC for c in cls): drop["bicycle"] += 1; continue
        if not bikes: drop["no_bike"] += 1; continue
        keep.append((split, f, heads, bikes))
print("kept", len(keep), "dropped", drop)
for split in ("train", "val", "test"):
    os.makedirs(f"{OUT}/{split}/images", exist_ok=True); os.makedirs(f"{OUT}/{split}/labels", exist_ok=True)
man = []
for split, f, heads, bikes in keep:
    s = {"train": "train", "valid": "val", "test": "test"}[split]
    name = "rf_" + os.path.basename(f)
    shutil.copy(f, f"{OUT}/{s}/images/{name}")
    lines = []
    for x1, y1, x2, y2 in bikes:
        lines.append(f"0 {(x1+x2)/2:.6f} {(y1+y2)/2:.6f} {x2-x1:.6f} {y2-y1:.6f}")
    for c, x, y, w, h in heads:
        lines.append(f"{1 if c == '0' else 2} {x} {y} {w} {h}")
    open(f"{OUT}/{s}/labels/{name.rsplit('.',1)[0]}.txt", "w").write("\n".join(lines) + "\n")
    man.append({"split": s, "file": name, "bikes": len(bikes), "helmet": sum(c == "0" for c, *_ in heads), "no_helmet": sum(c == "1" for c, *_ in heads)})
json.dump(man, open(f"{OUT}/manifest.json", "w"), indent=1)
open(f"{OUT}/data.yaml", "w").write(f"path: {os.path.abspath(OUT)}\ntrain: train/images\nval: val/images\ntest: test/images\nnc: 4\nnames: ['bike', 'helmet', 'no-helmet', 'number-plate']\n")
from collections import Counter
print({s: Counter(m["split"] for m in man)[s] for s in ("train", "val", "test")},
      "helmet", sum(m["helmet"] for m in man), "no_helmet", sum(m["no_helmet"] for m in man), "bikes", sum(m["bikes"] for m in man))
