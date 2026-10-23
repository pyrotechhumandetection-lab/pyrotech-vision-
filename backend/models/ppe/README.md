# PPE Detection models

Copied unchanged from the original `PPE Detection/Realtime-Edge-PPE-Detection` folder. The original folder is untouched; nothing was retrained.

| File | Source | Task | Classes |
|---|---|---|---|
| `ppe_yolo11n.pt` | `best.pt` / `ppe_yolo11n.pt` (md5 3bac42d1…) | detect | helmet, gloves, vest, boots, goggles, none, Person, no_helmet, no_goggle, no_gloves, no_boots |
| `helmet_cls.pt` | `helmet_cls.pt` | classify (helmet crop) | construction, other, road |

Used by `backend/detection/ppe/` (rules.py: decisions; engine.py: worker thread, drawing, snapshots).

## Evidence available (from the original project's own reports)

| Item | Evidence | Use in AI VISION |
|---|---|---|
| Helmet | test mAP50 0.91 | PRESENT only at conf ≥ 0.65; 0.25–0.65 → UNCERTAIN (never MISSING) |
| Vest | test mAP50 0.91 | PRESENT at conf ≥ 0.50; colour checked only on a detector "vest" box |
| Person | test mAP50 0.83 | person-first; minimum person confidence = module setting (default 0.45) |
| Gloves | test mAP50 0.76 | per person only; absence = NOT_ASSESSABLE |
| no_helmet / no_gloves | mAP50 0.20 / 0.08 | weak: MISSING only at high confidence, else UNCERTAIN |
| Helmet type classifier | val: construction 200/203, road 50/51, other 1 sample; field set: road alerted 4/6, construction kept 51/56 | road ≥ 0.70 → MISSING ("road helmet"); construction ≥ 0.70 → PRESENT; else UNCERTAIN |
| Helmet colour (HSV) | blue 97 %, white 93 %, yellow 48 % (original evaluation) | "unknown" when evidence is thin; yellow is weak |

## Limits (do not claim otherwise without new validation)

- **Motorcycle vs construction helmet is not reliably separated.** On the staged real photos the classifier scored two clearly construction helmets 0.47 and 0.59 "road", and one scooter rider's helmet crop 0.98 "construction". The 0.70 rule turns these into UNCERTAIN / review, not a confident verdict. There is also no bicycle-helmet class (bicycle and motorcycle are both "road").
- **Orange shirt vs safety vest is not validated.** The detector has no "shirt" class and there is no labelled test set for it. Colour is never used to create a vest; the "Vest wrong colour" check only names the colour of a detector vest box. A false vest on a coloured shirt was observed on one street photo (c.jpg).
- **Gloves are not reliably detected** (mAP50 0.76, no_gloves 0.08, no per-hand class). Gloves are reported per person; hidden hands are "not assessable", never "missing".
- Close-up frames: the original evaluation reported recall 0 on close-ups.
- Fluorescent yellow hi-vis vests measure hue 34–40 on real photos and are named "yellow" (band 22–45).

## Staged-image check (real models, this repo, not a validation set)

| Picture | Result |
|---|---|
| 90.png (4 workers) | orange helmet + orange vest → COMPLIANT; 2 yellow hi-vis vests → NON_COMPLIANT (orange required); blue helmet with type unsure → UNCERTAIN |
| cons1.jpg (2 workers, helmets, no vests) | helmets UNCERTAIN (classifier unsure), vest MISSING → NON_COMPLIANT |
| road.jpg (scooter rider) | helmet not detected → MISSING; vest MISSING |
| bike.jpg (motorcyclist) | no person detected |
| cons.jpg (mannequin) | weak helmet 0.32 → UNCERTAIN (not MISSING) |
