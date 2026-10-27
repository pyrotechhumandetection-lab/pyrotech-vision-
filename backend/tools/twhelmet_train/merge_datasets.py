"""Merge several YOLO-format datasets into one training set with this project's classes.

    python3 merge_datasets.py --out data/twhelmet_in  ds1=/path/to/roboflow_export1  ds2=/path/to/iith_yolo ...

Each source is a folder with data.yaml (names) and images/ + labels/ (train|valid|val|test sub-folders, or flat).
* class names are mapped with classes.py; an unknown name stops the run (nothing is guessed);
* the split is made PER SOURCE AND PER SEQUENCE (file-name prefix before the last '_' / frame number), never per image,
  so frames of the same video cannot be in both train and validation (that would inflate the scores);
* exact duplicate images (same bytes) are dropped;
* 'val-site' : put photos from YOUR cameras in a source called site=... ; they all go to the validation set, so the reported
  numbers are about your cameras and the training never sees them. (Use --site-to-train for the final model.)"""
import argparse
import hashlib
import random
import re
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from classes import CLASSES, map_name  # noqa: E402

IMG = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}


def sequence_of(stem):
    m = re.match(r"^(.*?)[_\-\.]?(\d+)$", stem)
    return m.group(1) if m else stem


def find_pairs(root):
    for img in sorted(root.rglob("*")):
        if img.suffix.lower() not in IMG:
            continue
        lab = None
        for cand in (img.with_suffix(".txt"), Path(str(img).replace("/images/", "/labels/")).with_suffix(".txt")):
            if cand.is_file():
                lab = cand
                break
        if lab is not None:
            yield img, lab


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--site-to-train", action="store_true", help="also train on the 'site' source (final model); default: validation only")
    ap.add_argument("sources", nargs="+", help="name=folder")
    a = ap.parse_args()
    rnd = random.Random(a.seed)
    out = Path(a.out)
    for s in ("train", "val"):
        (out / "images" / s).mkdir(parents=True, exist_ok=True)
        (out / "labels" / s).mkdir(parents=True, exist_ok=True)
    seen, stats = set(), {}
    for spec in a.sources:
        name, folder = spec.split("=", 1)
        root = Path(folder)
        names = yaml.safe_load((root / "data.yaml").read_text())["names"]
        names = list(names.values()) if isinstance(names, dict) else list(names)
        remap = {i: map_name(n) for i, n in enumerate(names)}
        items = {}
        for img, lab in find_pairs(root):
            items.setdefault(sequence_of(img.stem), []).append((img, lab))
        seqs = sorted(items)
        rnd.shuffle(seqs)
        n_val = max(1, int(len(seqs) * a.val_frac)) if len(seqs) > 1 else 0
        val_seqs = set(seqs) if (name == "site" and not a.site_to_train) else set(seqs[:n_val])
        kept = {"train": 0, "val": 0, "dup": 0, "empty": 0}
        for seq, pairs in items.items():
            split = "val" if seq in val_seqs else "train"
            for img, lab in pairs:
                h = hashlib.md5(img.read_bytes()).hexdigest()
                if h in seen:
                    kept["dup"] += 1
                    continue
                seen.add(h)
                rows = []
                for line in lab.read_text().split("\n"):
                    p = line.split()
                    if len(p) < 5:
                        continue
                    cls = remap.get(int(float(p[0])))
                    if cls is None:
                        continue
                    rows.append(f"{CLASSES.index(cls)} " + " ".join(p[1:5]))
                if not rows:
                    kept["empty"] += 1                     # background image: kept as a hard negative only if it has an (empty) label file
                stem = f"{name}_{h[:10]}"
                shutil.copy2(img, out / "images" / split / f"{stem}{img.suffix.lower()}")
                (out / "labels" / split / f"{stem}.txt").write_text("\n".join(rows) + ("\n" if rows else ""))
                kept[split] += 1
        stats[name] = kept
    (out / "data.yaml").write_text(yaml.safe_dump({"path": str(out.resolve()), "train": "images/train", "val": "images/val",
                                                   "names": {i: n for i, n in enumerate(CLASSES)}}, sort_keys=False))
    for k, v in stats.items():
        print(f"{k:<14} train {v['train']:>6}  val {v['val']:>6}  duplicates dropped {v['dup']}  images without boxes {v['empty']}")
    print("wrote", out / "data.yaml")


if __name__ == "__main__":
    main()
