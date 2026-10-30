# Two-Wheeler Helmet (line crossing) - models

## Installed: `rider_helmet.pt` = `in_v2` (fine-tuned 2026-10-30, second round)

YOLOv8n-size detector (3.0 M parameters, 640 px input), classes `bike`, `helmet`, `no-helmet`, `number-plate`. Fine-tuned from
`in_v1` on two curated public sets, both filtered to images with a motorcycle / scooter (cyclists removed), `bike` boxes
pseudo-labelled by the COCO detector, `number-plate` not labelled:

* Roboflow Universe "Motorcycle Helmet" v1 (gajanan-kalbande, **CC BY 4.0**): 629 -> 313 images (`curate_heads_dataset.py`)
* Kaggle "Helmet Detection" (andrewmvd, Pascal VOC, BikesHelmets###): 764 -> 432 images (`convert_kaggle_helmet.py`;
  check the licence on the Kaggle page before commercial use)

Train 515 / val 153 / test 33 + 44 images; 24 epochs on CPU, backbone frozen, best epoch 18 (`in_v2/args.yaml`, `in_v2/results.csv`).
Measured with `compare_models.py` (`in_v2/compare_v1_vs_v2.json`):

| | public test (33 img) mAP50 | Kaggle test (44 img) mAP50 | public test no-helmet P / R | site snapshots (34): confirmed wrong |
|---|---|---|---|---|
| `rider_helmet_v0.pt` (third-party) | all 0.785 · helmet 0.780 · no-helmet 0.749 | - | 0.64 / 0.82 | 4 |
| `rider_helmet_v1.pt` (in_v1) | all 0.858 · helmet 0.892 · no-helmet 0.749 | all 0.845 · helmet 0.922 · no-helmet 0.676 | 0.62 / 0.94 | 2 |
| `rider_helmet.pt` (in_v2) | **all 0.922 · helmet 0.942 · no-helmet 0.878** | all 0.844 · helmet 0.950 · no-helmet 0.652 | **0.83** / 0.87 | 2 (two helmeted riders, head not found by either detector, called "vehicle only"); 27 / 27 parked bikes right; no false helmet / no-helmet; the 2 bare-head riders left unverified |

What this does and does not prove: all test images are web photos with heads far larger than on a 1080p CCTV frame; the 34 site
snapshots are one camera, one afternoon, 7 riders. The gain that matters for a client is no-helmet **precision** (0.62 -> 0.83:
fewer false "no helmet" alerts on web-sized heads). Nothing here changes what happens below ~28 px of head (setting minHeadPx):
such heads are never confirmed. **`validated: false` stays until a labelled set of the client's own crossings exists.**
Licences: the base weights' author stated none; added data is CC BY 4.0 and the Kaggle set's own licence - confirm before commercial use.

Site column = single-frame result of the shipped judge (helmet >= 0.6, head >= 28 px, head no wider than 0.6 of its bike, a head seen
near the bike blocks "vehicle only"); live crossings add the vote rules and the travel limit on top, so they are at least as cautious.

Previous weights: `rider_helmet_v1.pt` (in_v1, 2026-10-30, Roboflow set only) and `rider_helmet_v0.pt` (third-party, below).
To go back: `AIV_TWHELMET_MODEL=.../rider_helmet_v1.pt` or rename the files.

`rider_helmet_v0.pt` - third-party YOLOv8 from <https://github.com/Jingjing468/motorcycle-helmet-violation-detection>
(`model/helmet_best.pt`, sha256 `45320ba938559d04bf76cd0ea15f94d99df7fadd2998280652f04ccb4b795657`), trained by its author on the
Roboflow Universe set "Helmet and Number Plate Detection for Motorbike Safety" (CC BY 4.0). To go back: `AIV_TWHELMET_MODEL=.../rider_helmet_v0.pt`
or rename the files.

How it is used: the COCO detector still finds and tracks the two-wheelers (NPU). At the line, each rider (person box + small margin)
goes through this model in one pass; a `helmet` / `no-helmet` box in the upper 60 % of that crop and at least 28 px (setting minHeadPx) gives the
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
