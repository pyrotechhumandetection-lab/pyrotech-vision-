# Line-crossing modules

Two modules on the common backend (one per-camera session, one shared tracked COCO pass, one worker thread):

* **Two-Wheeler Helmet (Line)** `twhelmet` - motorcycles / scooters (bicycles optional) are tracked; riders (driver and pillion) are associated by geometry; when the two-wheeler crosses the line its riders are judged. Events: `twhelmet_compliant`, `twhelmet_missing`, `twhelmet_unverified` (no alert by default). Pedestrians, cars and unrelated people never trigger it.
* **Person Line Crossing & Counting** `pcross` - tracked people counted once per crossing, IN and OUT; riders of two-wheelers are excluded by default. Events `line_cross_in` / `line_cross_out`; live counts, last 1 / 5 / 60 minute windows, recent crossings.

## Line
Drawn on the live picture (Draw line, drag A to B; the arrow marks IN; Flip IN / OUT swaps sides). Stored per camera and module in normalised coordinates with the resolution it was drawn on (`line.refWidth/refHeight`); a stream of a different aspect ratio raises a warning.

## Crossing rules (`backend/detection/crossing/geometry.py`)
Reference point bottom-centre; a dead band (5 % of the box height) around the line; a crossing needs a firm observation on each side, a path that cuts the finite segment, at least `minTrackFrames` observations, no crossing by the same id within `recrossSec`, no same-direction crossing at the same spot within `dedupeSec`; a lost id replaced by a new one nearby within `handoverSec` inherits the old state. Occlusion: the crossing is reported on the first frame seen past the line (`gapSec` says how long the object was unseen).

## Frames, timestamps, performance
Newest frame wins (one slot): at most `targetFps` (10) frames per second are taken, a waiting frame is replaced by a newer one, a frame older than 1 s is dropped; all counted. Each event stores the frame id and capture time of the frame that showed the crossing; the snapshot is that frame (annotated), the clean frame is its original. The helmet judge runs on its own thread, so counting keeps its rate. The status shows measured inference fps, inference / latency (avg, p95), frames processed / dropped, and tracker statistics (tracks started / ended / too short, id hand-overs, duplicates suppressed).

## Limits
* The stock PPE detector is not validated on motorcycle helmets: see `backend/models/twhelmet/README.md`. Unsure = "Helmet unverified", no compliant / missing alert. Use `backend/tools/eval_twhelmet.py` on your own labelled photos, or supply `rider_helmet.pt`.
* Crossing tests use scripted tracks and real photos moved across a canvas (simulated motion); no real traffic or crowd footage was available, so counting accuracy in the field is not claimed.
* Measured on a 2-CPU container with PyTorch (not the RK3588 NPU): about 7.6 analysed fps with both modules, ~5 % frames replaced. Target 10 fps on the real box is not verified.
