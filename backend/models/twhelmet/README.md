# Two-Wheeler Helmet (line crossing) - models

## Installed: `rider_helmet.pt` = `in_v1` (fine-tuned 2026-10-30)

YOLOv8n-size detector (3.0 M parameters, 640 px input), classes `bike`, `helmet`, `no-helmet`, `number-plate`. Fine-tuned from the
previous weights (`rider_helmet_v0.pt`, below) on 313 curated public images of motorcycle / scooter riders, mostly Indian roads
(Roboflow Universe "Motorcycle Helmet" v1, workspace gajanan-kalbande, **CC BY 4.0 - attribution required**; 629 images, of which
bicycles, kids' scooters and images without a two-wheeler were removed by `backend/tools/twhelmet_train/curate_heads_dataset.py`;
head boxes from the set, `bike` boxes pseudo-labelled by the COCO detector, `number-plate` not labelled). Split 227 / 53 / 33
(train / val / test), 27 epochs on CPU, backbone frozen, strong scale / colour / mosaic augmentation (`in_v1/args.yaml`,
`in_v1/results.csv`, best epoch 19).

Measured against the previous weights (`backend/tools/twhelmet_train/compare_models.py`, numbers in `in_v1/compare_old_vs_new.json`):

| | val mAP50 (53 img) | test mAP50 (33 img) | test no-helmet recall / precision | site snapshots: confirmed wrong (34) |
|---|---|---|---|---|
| `rider_helmet_v0.pt` (previous) | all 0.655 · bike 0.651 · helmet 0.846 · **no-helmet 0.469** | all 0.785 · helmet 0.780 · no-helmet 0.749 | 0.82 / 0.64 | 4 (3 helmeted riders and 1 bare-head rider called "vehicle only") |
| `rider_helmet.pt` (in_v1) | all 0.817 · bike 0.894 · helmet 0.841 · **no-helmet 0.718** | all 0.858 · helmet 0.892 · no-helmet 0.749 | 0.94 / 0.62 | 2 (1 helmeted and 1 bare-head rider called "vehicle only"); no false helmet / no-helmet confirmation; 26 of 27 parked bikes "vehicle only"; 5 of 7 riders left unverified |

Site column = single-frame result of the shipped judge (helmet >= 0.6, head >= 14 px, head no wider than 0.6 of its bike, a head seen
near the bike blocks "vehicle only"); live crossings add the vote rules and the travel limit on top, so they are at least as cautious.

What this does and does not prove: the val / test images are web photos (heads far larger than on a 1080p CCTV frame), not the
client's cameras; the 34 site snapshots are one camera, one afternoon, 27 of them parked bikes, 7 riders - far too few for a
percentage (see "3b" in the training README). The no-helmet precision is unchanged (about 0.63 on 17 test heads): a bare head is
still the hard class. **`validated: false` stays until a labelled set of the client's own crossings exists.** The weights licence
follows the previous weights (not stated by their author) and CC BY 4.0 for the added data - confirm before commercial use.

Previous weights: `rider_helmet_v0.pt` - third-party YOLOv8 from <https://github.com/Jingjing468/motorcycle-helmet-violation-detection>
(`model/helmet_best.pt`, sha256 `45320ba938559d04bf76cd0ea15f94d99df7fadd2998280652f04ccb4b795657`), trained by its author on the
Roboflow Universe set "Helmet and Number Plate Detection for Motorbike Safety" (CC BY 4.0). To go back: `AIV_TWHELMET_MODEL=.../rider_helmet_v0.pt`
or rename the files.

How it is used: the COCO detector still finds and tracks the two-wheelers (NPU). At the line, each rider (person box + small margin)
goes through this model in one pass; a `helmet` / `no-helmet` box in the upper 60 % of that crop and at least 14 px tall gives the
verdict (one clear view is enough, the vote rules still apply: 2 helmet frames, 3 no-helmet frames with no helmet frame). A two-wheeler
with no person linked or nearby is judged by its own area; if no head is found there in 3 or more frames and the bike hardly moved
(at most 1.5 bike-lengths), the crossing is "Vehicle only" (category "Only Vehicle Detected"); a bike that travelled further with no
rider seen stays "unverified". `number-plate` is searched on the two-wheeler's box at the crossing and recorded as
`plate` in the event and alert (`detectPlate`, `plateMinConf` in the module settings; "not visible" is reported only because
this model can see plates). Remove the file to fall back to the stock PPE detector below. The PPE module is not touched.

Previously (stock detector only) the module used:

| Purpose | Model | Note |
|---|---|---|
| Motorcycle / scooter / person detection + tracking | the project's COCO detector (`yolov8n`, RKNN NPU / OpenVINO / PyTorch as configured) with ByteTrack | same weights and engine selection as Human / Vehicle detection |
| Head location | the Head Count detector (`backend/models/head_yolov8m`) | only to cut a tight head crop |
| Helmet evidence | stock PPE detector `backend/models/ppe/ppe_yolo11n.pt` (classes helmet / no_helmet) **or** your own `rider_helmet.pt` here (env `AIV_TWHELMET_MODEL`) | custom model replaces the stock one; classes matched by name: helmet / with_helmet ... and no_helmet / without_helmet / head ... |

## What was measured (staged photos, this repository - not a validation set)

* The stock PPE detector is trained on construction helmets. On a scooter rider with a black full-face helmet it scored
  helmet 0.00 and no_helmet up to 0.62; on a motorcyclist photo helmet 0.0-0.17. Bicycle and construction helmets are found (0.5-0.8).
* The head detector finds every head, helmeted or not, so it cannot tell helmet from bare head.
* `helmet_cls.pt` has no bare-head class (a bare-head crop scored construction 0.98) and cannot be used for this.
* Therefore the stock judge is conservative: HELMET only on strong evidence, MISSING only when two independent views of the head
  both say no helmet and several frames agree, otherwise "Helmet unverified" (event recorded, no compliant / missing alert).
* **Not claimed:** that motorcycle helmets are reliably recognised, that a "genuine" helmet is told from a cap / hood / hard hat,
  or any accuracy figure. Measure it: `python3 backend/tools/eval_twhelmet.py <folder with helmet/ and no_helmet/ photos>`.
* A person pushing a motorcycle on foot is treated as a rider (no motion check).

For real motorcycle-helmet decisions, train or obtain a rider-helmet detector, place it here as `rider_helmet.pt`, and evaluate it with
the script above. Nothing is retrained or replaced automatically.
