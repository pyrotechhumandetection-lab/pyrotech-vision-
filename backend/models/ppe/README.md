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
| Helmet type classifier | val: construction 200/203, road 50/51; field set: road alerted 4/6, construction kept 51/56. **On the original grids a.png / b.png it called 47 / 47 white construction helmets "road"** | construction ≥ 0.70 → PRESENT; "road" alone → UNCERTAIN (not accepted, review) — never an automatic MISSING |
| Helmet colour (HSV) | blue 97 %, white 93 %, yellow 48 % (original evaluation) | "unknown" when evidence is thin; yellow is weak |

## Non-PPE helmet rules (site rules, added on request)

| Rule | Result | Evidence / limits |
|---|---|---|
| Person sits on a motorcycle / bicycle (COCO yolov8n of the project, same engine as Human / Vehicle detection) and wears a helmet | MISSING, "Rider helmet (2-wheeler)" | detector found the 2-wheeler under the rider on road.jpg, bike.jpg, cycle.jpg; not on the crowded c.jpg. A person standing next to a parked bike is not a rider (geometry test). Off-site or walking people with a motorcycle helmet in hand are not caught. |
| Helmet is mostly black (top 60 % of the box: V < 60, or grey with V < 80, ≥ 50 % of pixels) | MISSING, "Black helmet (not PPE)" | 0 of 76 real non-black helmets reached the threshold (highest 0.44). Not checked on night / IR (grey) pictures → UNCERTAIN. **No real black-helmet footage was available**: a black construction helmet is also rejected (site rule), a dark navy helmet in deep shade may be called black. |
| Classifier says "road", no 2-wheeler | UNCERTAIN (needs review), never PRESENT | classifier not reliable alone (47 / 47 white construction helmets called road) |

All three use the existing alert switch "Road helmet (not construction)", shown in the UI as "Non-PPE helmet (rider / black)".

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
