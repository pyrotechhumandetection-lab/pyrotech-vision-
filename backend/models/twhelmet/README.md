# Two-Wheeler Helmet (line crossing) - models

## Installed: `rider_helmet.pt`

Third-party YOLOv8 weights from <https://github.com/Jingjing468/motorcycle-helmet-violation-detection> (`model/helmet_best.pt`,
sha256 `45320ba938559d04bf76cd0ea15f94d99df7fadd2998280652f04ccb4b795657`). Classes: `bike`, `helmet`, `no-helmet`, `number-plate`.
Trained by its author on the Roboflow Universe set "Helmet and Number Plate Detection for Motorbike Safety" (CC BY 4.0, attribution required).
**Licence of the weights: not stated by the author - confirm before commercial use.** The author's mAP50 0.94 comes from their own test
split; it has not been measured on this system's cameras (`validated: false`).

How it is used: the COCO detector still finds and tracks the two-wheelers (NPU). At the line, each rider (person box + small margin)
goes through this model in one pass; a `helmet` / `no-helmet` box in the upper 60 % of that crop gives the verdict (one clear view is
enough, the 3-vote rule still applies). `number-plate` is searched on the two-wheeler's box at the crossing and recorded as
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
