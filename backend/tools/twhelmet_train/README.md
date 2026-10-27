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
# 1. harvest candidates from your cameras (pre-labelled by the current model; you correct them)
python3 harvest.py --source clip.mp4 --out to_label --stride 1 --per-track 6
# 2. merge (split by sequence, never by image; your own cameras = validation only)
python3 merge_datasets.py --out data/twhelmet_in  iith=/data/iith_yolo  rf1=/data/roboflow_a  site=/data/site_labelled
# 3. train on a GPU (Colab T4: ~1-3 h), starting from the shipped weights
python3 train.py --data data/twhelmet_in/data.yaml --base ../../models/twhelmet/rider_helmet.pt --name in_v1
# 4. measure on photos of YOUR riders before using it
python3 ../eval_twhelmet.py <folder with helmet/ and no_helmet/>
# 5. only if it beats the installed model on YOUR validation set: copy best.pt to backend/models/twhelmet/rider_helmet.pt
```

After the first site model is deployed, repeat steps 1-5 with the "hard" pictures it gets wrong (`manifest.csv` lists them first).
Final model: add `--site-to-train` in step 2 and keep a separate small set of unseen days for the last measurement.

## 3b. What to ask for before you promise a number to a client

Use at least 300 two-wheeler crossings per condition (day / night / rain), counted by a person watching the recording, and report for
each: crossings missed, helmet-present riders called "no helmet", bare-head riders called "helmet", and share left "unverified".
Fewer crossings give figures too uncertain to quote (30 crossings can hide a 10-point error).
