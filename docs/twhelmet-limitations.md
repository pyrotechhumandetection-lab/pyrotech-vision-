# Two-Wheeler Helmet Detection (line crossing): limitations and edge cases

*For clients and for the delivery team. Plain statements of when the system is reliable, when it is not, and why.*

**Read this first.** The system assists a human supervisor. It is not a certified enforcement device. It has **no accuracy figure yet
that was measured on a labelled validation set from the client's own cameras**. Until that measurement exists (section 7), nothing in
this document is a promise of a percentage. Statements marked **[measured]** come from a test on one recorded video (Appendix A); all
others are **[expected]**: the reasoning is sound, the size of the effect on a given site is not yet measured.

---

## 1. What the system does, step by step, and where each step can fail

| # | Step | Technology | Typical failure |
|---|---|---|---|
| 1 | **Find** every motorcycle / scooter and person in each analysed frame | COCO-trained YOLOv8n detector (on the NPU, 640 px network input) | Bike not found (far, small, blurred, cut by the picture edge, hidden, unusual shape). A bike that is never found is never checked: **no event at all**. |
| 2 | **Follow** each two-wheeler from frame to frame (ByteTrack) | tracker | The number (ID) changes when the bike is hidden for a moment; a short or fragmented track is ignored or counted twice. |
| 3 | **Link riders to the bike** (driver, pillion) | geometry: person boxes that sit on the bike box | A rider missed, a bystander linked to the bike, 3 or 4 riders merged, a child hidden behind the driver. |
| 4 | **Wait for the bike to cross the line** you drew | geometry | The bike crosses outside the line ends, along the line instead of across it, or is not seen on both sides of the line. |
| 5 | **Judge each rider's head** from the frames around the crossing | rider-helmet model (YOLOv8, classes helmet / no-helmet) + a vote over several frames | Head too small, blurred, hidden, or unlike anything the model was trained on: answer is **"Helmet unverified"**. A wrong answer is also possible (sections 4D, 4E). |
| 6 | **Report**: counts, snapshot with the crossing frame, alert | software | Alerts are only as good as steps 1-5; a number plate is only *detected* (visible / not visible), never read. |

A crossing ends in one of three results: **Helmet**, **No helmet**, or **Unverified** ("the evidence was not strong enough"). "Unverified" is a normal, safe answer: the
system is built to say "unsure" rather than guess. A high share of "Unverified" means the picture quality or the model is the problem, not that the system is idle.

## 2. What we can and cannot promise today

* **Cannot promise:** a detection rate, a helmet / no-helmet accuracy, or a false-alarm rate for any site, until measured (section 7).
* **Model provenance.** The helmet model is a third-party YOLOv8 model trained on a Southeast-Asian public dataset (Roboflow "Helmet and Number Plate Detection for Motorbike Safety", licence terms of the weights not stated by the author). The two-wheeler detector is the standard COCO model. Neither was trained on Indian roads. Both are being fine-tuned (section 8); until then **expect lower accuracy on Indian helmet types, Indian traffic density and Indian camera views**.
* **A model's published score is not a field score.** The helmet model's author reports mAP50 0.94 on their own test split. That figure says little about another country, camera angle, distance or light.
* **Cannot certify helmet quality.** The system sees "something helmet-shaped on the head". It cannot tell an ISI/BIS-certified helmet from a toy or construction helmet, and cannot see whether the chin strap is fastened.

## 3. Installation conditions that decide the result (rules of thumb)

These are engineering rules of thumb **[expected]**, to be confirmed by the site acceptance test. Below them the system will mostly answer "Unverified" or miss the bike.

| Condition | Good | Degraded | Poor / do not rely |
|---|---|---|---|
| Rider's head height in the picture (pixels, at the analysis resolution) | 40 px or more | 20-40 px | under 20 px |
| Two-wheeler height in the picture | 150 px or more | 80-150 px | under 80 px |
| Camera view of the rider | front view, slightly from above | side view | rear view only (no face; helmet vs no-helmet is harder; plate may be the only cue) |
| Camera height / angle | 3-6 m, looking down 15-35 degrees at approaching traffic | higher, steeper (head seen from above: a helmet and a bare head both look like "a round dark top") | very high, straight down |
| Frame rate of the stream | 20 fps or more | 10-20 fps | under 10 fps (a fast bike moves 2-4 m between frames) |
| Shutter / exposure | fast shutter (1/500 s or faster) in daylight | 1/100-1/250 s | slow shutter in dim light (motion blur: head becomes a smear) |
| Light | even daylight | harsh sun and shadow, light haze | night without a good white light, strong backlight, headlights facing the camera |
| Picture width at the analysed stream | 1920 px | 1280 px | 640 px |
| Traffic | free-flowing, bikes separated | moderate density | queues, bikes touching, overtaking in the line area |
| Speed at the line | under about 40 km/h | 40-60 km/h | above 60 km/h without a high frame rate and fast shutter |

**Where the line is drawn matters as much as the camera.** The best place is where bikes slow down, travel straight towards or away from the camera, and the head is large and unobstructed. The worst place is a fast, wide, curved or crowded stretch.

## 4. Edge-case catalogue

*Result column: **Missed** = no event; **Unverified** = event recorded, no helmet decision; **False no-helmet** = helmet present, called missing; **False helmet** = bare head called helmet; **Miscount** = counted twice or in the wrong direction.*

### A. Camera, picture quality and placement

| # | Scenario | Result | Why | Reduce by |
|---|---|---|---|---|
| A1 | Distant bike, small in the picture | Missed / Unverified | The network input is 640 px; a 1920 px picture is shrunk 3 times, so a 40 px head becomes about 13 px, too small for a helmet decision. **[measured]** the current setting found a moving bike in only about 11 % of frames on the test video (Appendix A). | Detect only around the line (zoom), lower the confidence threshold, move the camera closer, narrower lens, higher-resolution model |
| A2 | Bike cut by the picture edge or leaving the frame at the line | Missed / Miscount | A partly visible bike scores low; a track that disappears at the edge never shows both sides of the line | Place the line away from the edge; keep the line ends inside the picture |
| A3 | Very high mounting, steep downward angle | Unverified / False helmet | From above the head is a disc: helmet or bare hair both look like a dark circle; legs and plate are hidden | Mount lower; use 15-35 degree look-down |
| A4 | Rear view only | Unverified; plate only in some cases | No face or visor; helmets and hair can look alike from behind; strap and chin invisible | Add a front-facing camera |
| A5 | Side view at a wide angle | Unverified | Pillion and driver overlap; helmet shape differs from front views | Narrow the angle; look at the approaching traffic |
| A6 | Camera shake, pole sway, wind, vibration | Missed / Miscount | Tracks break; the picture moves relative to the drawn line | Rigid mount; the line must be redrawn if the camera is moved |
| A7 | Out of focus, dirty lens, water drops, spider web | Unverified / Missed | Edges and colours of the helmet disappear | Clean lens, hydrophobic coating, scheduled cleaning |
| A8 | Heavy video compression, low bitrate, sub-stream (640x360) | Unverified / Missed | Compression smears small heads and removes texture | Use the main stream at 1080p; check the bitrate |
| A9 | Low frame rate (10 fps or less) | Missed / Unverified / Miscount | A fast bike crosses the line in 1-3 frames; fewer judged frames; tracks break | 25 fps stream, 25 fps analysis |
| A10 | Rolling shutter / slow shutter motion blur | Unverified | Head is smeared; shape is lost | Fast shutter; good light |
| A11 | The recorded picture is already a screen capture or a re-encoded copy | All of the above, worse | A copy has less detail than the live stream **[measured: the video used for the first test was a screen recording at about half the camera's resolution]** | Test on the original stream or a camera-side recording |
| A12 | Camera moved or zoomed after the line was drawn | Missed / Miscount | The line is stored on the picture, not on the road | Redraw the line after any camera movement; do not use auto-zoom / auto-patrol |
| A13 | Wrong aspect ratio or resolution change of the stream | Miscount | The line is stored as a fraction of the picture | The software warns on a different aspect; re-check the line |

### B. Light, weather and time of day

| # | Scenario | Result | Why | Reduce by |
|---|---|---|---|---|
| B1 | Hard sun and deep shadow on the road (tree shadow, building shadow) | Unverified / False no-helmet | A rider passing through shadow has a dark head: a black helmet and black hair look the same. **[seen in the test video]** strong shadow patterns on the road | Lower angle of view; wide dynamic range (WDR) camera; fixed-exposure tuning |
| B2 | Backlight, low sun in front of the camera | Missed / Unverified | The rider is a silhouette | Reposition the camera to look away from the sun's path; WDR/HLC |
| B3 | Night with street lighting only | Missed / Unverified | Low light = noise and blur; IR makes helmets grey and hides colour | Add white light; do not rely on the system at night without a night test |
| B4 | Headlights of oncoming vehicles | Missed / Unverified | Glare blots out the rider | Angle the camera; mask; test at night |
| B5 | Rain, wet road reflections, raincoats, hoods, umbrellas | Unverified / False no-helmet | Hood over a helmet or an umbrella over the head hides the helmet; reflections create false shapes | Accept lower accuracy in rain; test separately |
| B6 | Fog, haze, dust (kicked up by traffic, construction, dry roads) | Missed / Unverified | Contrast drops with distance | Shorter distance; test in the worst season |
| B7 | Sudden change in light (cloud passes, tunnel exit) | Unverified | The camera exposure lags for 1-2 s | WDR; fixed exposure |
| B8 | Lens flare, sun glint on a helmet or windscreen | Unverified | Bright spot hides the head | Camera shade |

### C. Traffic, scene and vehicles

| # | Scenario | Result | Why | Reduce by |
|---|---|---|---|---|
| C1 | Dense traffic, bikes touching, filtering between cars | Missed / Miscount / Unverified | Overlapping boxes; the tracker swaps IDs; riders are linked to the wrong bike | Choose a line where traffic is spread out; judge only at the line |
| C2 | Bike hidden behind a bus, truck, car, auto-rickshaw or pole | Missed / Unverified | Occlusion: the model sees part of a bike or none | Camera higher and further to the side; line where lanes are clear |
| C3 | 3 or 4 people on one bike (family, children) | Unverified / False helmet / False no-helmet | The system links up to 3 riders per bike; children are small and hidden; passengers overlap | Expect a lower accuracy for pillions and children; policy decision on children (helmet rules differ) |
| C4 | Child in front of the driver / on the lap | Unverified / Missed | Not visible or too small | None practical; document as a known gap |
| C5 | Parked bikes, bikes on a stand, a person starting a bike in place, a bike pushed by hand | Miscount / False alert | A person next to a bike, or a person pushing a bike, is treated as a rider; the software has no "is it moving" check **[seen in the test video: people starting a parked bike]** | Place the line away from parking; set a minimum movement (planned) |
| C6 | Bicycles, e-bikes, mobility scooters, e-rickshaws, three-wheelers, tractors, hand carts | Miscount / False alert | The detector class "motorcycle" can fire on similar shapes; bicycles are off by default | Keep "also check bicycles" off; add these classes in the fine-tuning set |
| C7 | Loads: gas cylinders, milk cans, sacks, ladders, a person carrying something large | Unverified / Missed | Odd silhouettes confuse the detector and the rider linking | Fine-tune with such images |
| C8 | Very fast bikes (over 60 km/h) | Missed / Unverified | Few frames at the line; blur | Higher frame rate; faster shutter; line at a speed-reducing point (speed bump) |
| C9 | Wrong-way riders and riders crossing at an angle | Miscount | Direction is defined by the line arrow; a path parallel to the line never crosses it | Choose the line direction to match the road; set the direction filter |
| C10 | U-turns and stop-and-go at the line | Miscount | A bike can cross the line more than once; the system waits 2 s before it counts the same track again | Line away from junctions |
| C11 | Crossing the line near its end points | Missed | The path must cut the line; the software now allows a margin of half a person's width beyond each end | Draw the line wall-to-wall |
| C12 | Pedestrians, animals (cattle, dogs) and street vendors near the line | False alert | A person standing on the line is not a rider; a cow can be mistaken in rare cases | Keep the line off pavements |
| C13 | Reflections or posters of bikes on vehicles, hoardings, shop windows | False alert | The detector responds to the picture of a bike | Mask the area (planned); review alerts |
| C14 | Dirt roads and dust clouds | Missed / Unverified | A bike disappears inside a dust cloud for several frames | Short test season |
| C15 | Festival, procession, crowd, group riding | Missed / Miscount | Very dense groups; many occlusions | Do not use the system in these conditions |

### D. The rider and the helmet (Indian conditions)

| # | Scenario | Result | Why | Reduce by |
|---|---|---|---|---|
| D1 | **Open-face and half helmets** (very common in India) | False no-helmet / Unverified | Models trained mostly on full-face helmets learn "round cap with a visor"; an open-face helmet shows the face, which can look like "head without helmet" | Fine-tune on Indian images with these helmets |
| D2 | **Non-ISI, cheap, toy or construction-style helmets** | False helmet / False no-helmet | The model sees a hard shell; it cannot judge quality or standard; thin cap-like helmets may be rejected | Policy decision: the system checks "helmet present", not "helmet certified" |
| D3 | Helmet **carried** in the hand, on the arm, on the mirror, on the handlebar, hung on the back, or on the lap | False helmet / Unverified | The model may still see a helmet shape in the rider box; the software only looks at the upper part of the rider box, but a helmet on the arm or back can sit there | Labelling rule: a helmet is only a helmet when on the head (fine-tuning) |
| D4 | **Helmet not fastened** or loosely worn | False helmet | The strap is not visible | State to clients: strap is not checked |
| D5 | **Turban (Sikh riders), cloth head cover, scarf, dupatta, hijab, cap, towel** | No-helmet | These are not helmets; in some jurisdictions a turban-wearing Sikh rider is exempt by law | Policy: define how such riders are handled; the system will flag them as "no helmet" |
| D6 | Dark or black helmet in shadow, or on dark hair | Unverified / False no-helmet | Helmet and hair have the same colour and texture in a small, dark region | Light, camera, fine-tuning |
| D7 | White or light helmet against a bright sky or wall | Unverified | Little contrast | Camera placement |
| D8 | Helmet with a visor, mirrored or tinted | Unverified / False helmet | Reflections hide the shape | Fine-tune |
| D9 | Hood, balaclava, face mask, scarf wrapped around the head, sunglasses | False no-helmet / False helmet | Head covers look like helmets from a distance | Fine-tune; accept lower accuracy |
| D10 | Hair bun, long hair, bald head under bright light | False helmet | A bright round head looks like a white helmet at low resolution | Higher resolution |
| D11 | Pillion rider (back seat) hidden by the driver | Unverified / Missed | Only part of the head is visible | Front camera; document |
| D12 | Children with child helmets or without | Unverified | Smaller, usually hidden | Policy decision |
| D13 | Rider leaning forward or head turned away | Unverified | The helmet shape changes | Judge over several frames (the software votes) |
| D14 | Delivery riders with a thermal box on the back, tall backpack | Unverified / False helmet | The back box can be mistaken for part of the head region in rear views | Fine-tune |
| D15 | Police, military, construction or fire helmets worn on two-wheelers | False helmet | They are helmets but may not be road-legal | Policy |
| D16 | Helmet colour and branding variety (stickers, covers) | Unverified | Unusual appearance not in the training set | Fine-tune on Indian images |

### E. The model

| # | Scenario | Result | Why | Reduce by |
|---|---|---|---|---|
| E1 | The helmet model was trained on a non-Indian dataset | Lower accuracy for all of D | Domain shift: different helmets, bikes, roads, camera angles | Fine-tune on Indian roads and on the client's own cameras (section 8) |
| E2 | The two-wheeler detector (COCO) has few small, distant or crowded Indian bikes | Missed | COCO has far fewer Indian-style scooters and bikes carrying 2-4 people; it was trained at larger object sizes | Fine-tune a two-wheeler detector (section 8) |
| E3 | NPU (INT8) conversion of the detector | Slightly lower accuracy | 8-bit numbers lose precision for small objects; the NPU model has a fixed 640 px input | Higher-resolution export; measure FP16 vs INT8 |
| E4 | Confidence is not a probability | Wrong thresholds | A score of 0.6 does not mean "60 % right" | The thresholds are tuned on the client's validation set |
| E5 | The "unverified" threshold trades missed violations against false alarms | Either more unverified or more false alerts | Strict thresholds give few false "no helmet" alerts but many "unverified"; loose thresholds do the opposite | Decide the preferred error on site |
| E6 | The rider model is also used for number-plate detection | Plate often "not visible" | Plates are small and visible only from the front or rear; Indian plate formats and sizes differ; the plate is not read | A dedicated plate model and OCR if needed (not included) |
| E7 | Model files replaced or removed | Falls back to the older PPE model | The old PPE detector was built for construction helmets and is not validated for motorcycle helmets | Keep the model file; the Detection Status panel names the model in use |

### F. System, timing and software

| # | Scenario | Result | Why | Reduce by |
|---|---|---|---|---|
| F1 | AI frame rate lower than the camera's | Missed / Unverified | The AI keeps only the newest frame; with 16 fps AI on a 25 fps camera about a third of the frames are skipped | Check "Inference rate" on the panel; one camera per module on the NPU |
| F2 | Two cameras at once | Lower frame rate each | The NPU is shared (maximum 2 AI cameras) | Plan the number of cameras |
| F3 | Track ID switches (the same bike gets a new number) | Miscount / Missed vote history | The system keeps the history of votes per rider position, so a switch loses less, but a new track needs 2-3 frames | Tracker tuning |
| F4 | Backend restart, power cut, network loss | No events during the gap | The stream is reconnected automatically; the line is saved | UPS; the software shows the connection state |
| F5 | Clock wrong | Wrong time stamps | The time comes from the box's clock | NTP |
| F6 | Alerts flood or are rate-limited | Missing alerts | A cooldown stops the same alert repeating | Set the cooldown to the site's needs |
| F7 | Storage full | No snapshots | Snapshots need disk | Retention policy |
| F8 | Judging happens off the main thread | Delay of a few hundred milliseconds | Helmet checks run on the CPU and are queued; under heavy load some intermediate frames are skipped | Use the main CPU headroom; limit other modules |
| F9 | Zoomed detection area (when switched on) | Objects outside the dashed area are not seen | The detector looks only at the area around the line | Set the area margin |

### G. Legal and operational boundaries

* **Assistive, not enforcement.** Every alert needs human review before action. The system must not issue a penalty by itself.
* **No identification.** Number plates are not read; faces are not recognised.
* **No certificate.** The system does not certify helmet standard (ISI/BIS), fit or chin strap.
* **Privacy.** Snapshots show riders; store and share them under the client's data policy.
* **Policy items the client must decide:** turban-wearing Sikh riders, children and pillions, bicycles and e-bikes, half-face helmets, delivery riders, what to do with "Unverified" crossings (ignore, count, or review).

## 5. How errors show up on screen

* **Many "Unverified"** — picture too small, blurred, dark, or model not suited. Check head size (section 3).
* **Nothing at all although bikes pass** — the bike was not detected (A1, A2, C2) or never crossed the line (C9, C11). The Detection Status panel shows "passed beyond a line end" and the number of tracks.
* **Alert for a person pushing or starting a bike** — C5.
* **Compliant rider flagged "no helmet"** — usually open-face or non-standard helmet, shadow, or low resolution (D1, D6, B1).
* **Bare head called "helmet"** — usually a held helmet, a cap or hood, or a very small head (D3, D9, A3).

## 6. What it takes to quote an accuracy to a client

1. Fix the camera, angle, line and light as they will be in service.
2. Record at least **300 two-wheeler crossings per condition** (day, night, rain, crowded) with the main stream.
3. A person watches the recordings and notes for each crossing: was it detected, were riders helmeted. Save the notes.
4. Run the system on the same recordings and compare crossing by crossing.
5. Report for each condition: crossings missed; helmeted riders called "no helmet"; bare-head riders called "helmet"; share left "Unverified"; with the number of crossings behind every figure. **Fewer than about 300 crossings give figures too uncertain to quote** (30 crossings can hide a 10-point error).
6. Repeat after each model change and after any camera move.

## 7. Improvement plan (status)

| Item | Status |
|---|---|
| Lower the detection confidence for two-wheelers; check every frame near the line; 2 frames to start a track | in the software (see Appendix A for the measured effect) |
| Detect only around the line (zoom onto the road near the line) | in the software, switch in the module settings, with a dashed box on the picture |
| Margin at the line ends | in the software |
| Number-plate visible / not visible at the crossing | in the software |
| Training toolkit: harvest from the client's cameras, merge Indian datasets, fine-tune | in the repository (`backend/tools/twhelmet_train/`) |
| Fine-tune on Indian road data (public sets and the client's own labelled images) | planned: needs data and a GPU session (about 1-3 hours per run) |
| Higher-resolution NPU model (960 / 1280 px) for small, far bikes | planned: needs a model conversion on a PC |
| Minimum-movement check (ignore parked / hand-pushed bikes) | planned |
| Masks for areas to ignore (parking, hoardings) | planned |
| Night test and rain test with labelled recordings | planned |

## Appendix A. First measurement on one recorded video (2026-10-27)

*(filled in from the analysis; see below)*
