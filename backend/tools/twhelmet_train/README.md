# Two-wheeler helmet model: fine-tuning for Indian roads

Why: the installed `rider_helmet.pt` was trained by a third party on a South-East-Asian (Cambodia) set, and the COCO detector that
finds two-wheelers was trained on COCO. Neither has seen enough Indian traffic: half-face and open helmets, non-ISI helmets, cloth head
covers, three or four people on one bike, dust, harsh shadows, vendors' bikes with loads. Nothing here replaces the installed model: you
train, measure, and copy `best.pt` over `backend/models/twhelmet/rider_helmet.pt` yourself.

## 1. Data (in this order of value)

| Source | What it gives | Check before use |
|---|---|---|
| **Your own cameras** (`harvest.py`) | the exact angle, light, distance, bikes and helmets of your sites | label every box yourself; 500-1,500 labelled images per site is a realistic first target |
| [IITH Helmet dataset](https://sites.google.com/site/dineshsinghindian/iith_helmet_1-dataset) (IIT Hyderabad surveillance videos, India) | Indian campus roads, sparse traffic | licence not stated: ask the authors (page asks for citation); annotation format not stated: convert |
| [Indian Helmet detection dataset (Kaggle)](https://www.kaggle.com/datasets/aryanvaid13/indian-helmet-detection-dataset) | Indian helmet photos | size, classes and licence not visible without logging in: read the page |
| [IDD India Driving Dataset](https://inai.iiit.ac.in/domains/idd.html) (IIIT Hyderabad) | Indian road scenes with vehicle annotations (for the two-wheeler/rider detector, not helmets) | terms not stated on the page; request via the site; class list: check for motorcycle/rider |
| Roboflow Universe sets ("Helmet and Number Plate Detection for Motorbike Safety", MotorcycleHelmets, two-wheeler sets) | many images, easy YOLO export | most are not Indian; licences differ per set (CC BY 4.0 needs attribution); inspect a sample before use |

Do not mix in construction-helmet sets as "helmet": they teach the model the wrong thing for motorcycle helmets.

## 2. Labelling rules (write them down; every labeller uses the same)

* `bike` - the whole two-wheeler (motorcycle, scooter, moped), also when partly cut by the picture edge.
* `helmet` - a helmet **on the head** (any type: full-face, open-face, half, with visor). Box = the helmet only.
* `no-helmet` - a head with no helmet: bare, cap, scarf / cloth, dupatta, turban, hood. Box = the head only.
* A helmet in the hand, on the mirror, on the arm or on the lap is **not** `helmet`. Label the head `no-helmet`.
* Unfastened helmet on the head: `helmet` (the model cannot see the strap; say so to clients).
* `number-plate` - the plate, front or rear.
* Do not label a head smaller than ~12 px or too blurred to decide: leave it out (it becomes an "unverified" case, honestly).
* Label every rider and pillion in the picture, not only the nearest.
* Keep hard pictures: night, rain, backlight, far, blurred, cut by the edge, crowded.

## 3. Steps

```
# 0. labelled folders in YOLO format, one data.yaml each (CVAT / Label Studio / Roboflow can export this)
#    a public HEAD-box set (helmet / no helmet only, mixed with bicycles) first goes through the curator:
python3 curate_heads_dataset.py <roboflow export> data/pub_curated ../../models/yolov8s.pt
# 1. harvest candidates from your cameras (pre-labelled by the current model; you correct them)
python3 harvest.py --source clip.mp4 --out to_label --stride 1 --per-track 6
# 2. merge (split by sequence, never by image; your own cameras = validation only)
python3 merge_datasets.py --out data/twhelmet_in  iith=/data/iith_yolo  rf1=/data/roboflow_a  site=/data/site_labelled
# 3. train on a GPU (Colab T4: ~1-3 h), starting from the shipped weights
python3 train.py --data data/twhelmet_in/data.yaml --base ../../models/twhelmet/rider_helmet.pt --name in_v1
# 4. measure against the installed weights: box metrics on the held-out splits AND the project's own judgement on YOUR site
#    photos sorted into helmet_detected/ helmet_not_detected/ only_vehicle_detected/ (+ optional event_boxes.json)
python3 compare_models.py --data data/twhelmet_in/data.yaml --site site_test --json compare.json old=../../models/twhelmet/rider_helmet.pt new=runs/in_v1/weights/best.pt
python3 ../eval_twhelmet.py <folder with helmet/ and no_helmet/>          # older, rider-photo-only table
# 5. only if it beats the installed model on YOUR photos: copy best.pt to backend/models/twhelmet/rider_helmet.pt (keep the old file)
```

### Done so far: `in_v1` (2026-10-30)

* Data: Roboflow Universe "Motorcycle Helmet" v1 (gajanan-kalbande, CC BY 4.0), 629 web photos -> `curate_heads_dataset.py` kept 313
  (265 were bicycles, 43 had no two-wheeler, 8 had no head box): 340 helmet + 230 no-helmet heads, 597 pseudo-labelled bikes.
  Split 227 / 53 / 33 by the set's own split. The 50 site snapshots (CAM-02, 2026-10-29) were **not** trained on: they carry the
  drawn overlays, only 7 show a rider, and an own-camera set belongs in the measurement, not the training, at this size. 34 of them
  that a person could label (5 helmet, 2 no helmet, 27 vehicle only; 16 ambiguous ones left out) are the site test set.
* Run: `train_cpu` settings in `../../models/twhelmet/in_v1/args.yaml` - from the installed weights, backbone frozen (10 layers),
  640 px, batch 8, scale 0.6 / translate 0.15 / degrees 4 / shear 2, hsv 0.02 / 0.7 / 0.5, mosaic, mixup 0.1, cosine LR, 2 CPU
  cores, about 100 s per epoch; stopped by patience at epoch 27, best epoch 19.
* Result: see `../../models/twhelmet/README.md` (table) and `in_v1/compare_old_vs_new.json`. Short form: val mAP50 0.655 -> 0.817
  (no-helmet 0.47 -> 0.72), test mAP50 0.785 -> 0.858, confirmed-wrong site photos 4 -> 2 (with the new gates; no false
  helmet / no-helmet confirmation, most site riders left unverified). Not enough for an accuracy promise.
* What is missing for a production number: the client's own labelled crossings (step 1, `harvest.py`), a night set, and a GPU run
  at 960 px with the shipped `train.py` settings once there are a few thousand labelled heads.

After the first site model is deployed, repeat steps 1-5 with the "hard" pictures it gets wrong (`manifest.csv` lists them first).
Final model: add `--site-to-train` in step 2 and keep a separate small set of unseen days for the last measurement.

## 3b. What to ask for before you promise a number to a client

Use at least 300 two-wheeler crossings per condition (day / night / rain), counted by a person watching the recording, and report for
each: crossings missed, helmet-present riders called "no helmet", bare-head riders called "helmet", and share left "unverified".
Fewer crossings give figures too uncertain to quote (30 crossings can hide a 10-point error).
