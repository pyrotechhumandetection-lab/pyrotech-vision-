# Line-crossing modules

Two modules on the common backend (one per-camera session, one shared tracked COCO pass, one worker thread):

* **Two-Wheeler Helmet (Line)** `twhelmet` - motorcycles / scooters (bicycles optional) are tracked; riders (driver and pillion) are associated by geometry; when the two-wheeler crosses the line its riders are judged. Events: `twhelmet_compliant`, `twhelmet_missing`, `twhelmet_unverified` (no alert by default). Pedestrians, cars and unrelated people never trigger it.
* **Person Line Crossing & Counting** `pcross` - tracked people counted once per crossing, IN and OUT; riders of two-wheelers are excluded by default. Events `line_cross_in` / `line_cross_out`; live counts, last 1 / 5 / 60 minute windows, recent crossings.

## Line
Drawn on the live picture (Draw line, drag A to B; the arrow marks IN; Flip IN / OUT swaps sides). Stored per camera and module in normalised coordinates with the resolution it was drawn on (`line.refWidth/refHeight`); a stream of a different aspect ratio raises a warning.

## Crossing rules (`backend/detection/crossing/geometry.py`)
Reference point bottom-centre; a dead band (5 % of the box height) around the line; a crossing needs a firm observation on each side, a path that cuts the finite segment, at least `minTrackFrames` observations, no crossing by the same id within `recrossSec`, no same-direction crossing at the same spot within `dedupeSec`; a lost id replaced by a new one nearby within `handoverSec` inherits the old state. Occlusion: the crossing is reported on the first frame seen past the line (`gapSec` says how long the object was unseen).

## Frames, timestamps, performance
Newest frame wins (one slot): at most the camera's dynamic AI rate (see *Cameras and frame rate* below) is taken, a waiting frame is replaced by a newer one, a frame older than 1 s is dropped; all counted. Each event stores the frame id and capture time of the frame that showed the crossing; the snapshot is that frame (annotated), the clean frame is its original. The helmet judge runs on its own thread, so counting keeps its rate. The status shows measured inference fps, inference / latency (avg, p95), frames processed / dropped, and tracker statistics (tracks started / ended / too short, id hand-overs, duplicates suppressed).

## Limits
* `backend/models/twhelmet/rider_helmet.pt` (YOLOv8n fine-tuned on Indian-road rider photos, `in_v1`; classes bike, helmet, no-helmet, number-plate; previous third-party weights kept as `rider_helmet_v0.pt`) is the helmet model of this module; it also reports whether a number plate is visible at the crossing. Its accuracy on your cameras is unmeasured. Without that file the stock PPE detector is used, which is not validated on motorcycle helmets: see `backend/models/twhelmet/README.md`. Unsure = "Helmet unverified", no compliant / missing alert. Use `backend/tools/eval_twhelmet.py` on your own labelled photos, or supply `rider_helmet.pt`.
* Crossing tests use scripted tracks and real photos moved across a canvas (simulated motion); no real traffic or crowd footage was available, so counting accuracy in the field is not claimed.
* Measured on a 2-CPU container with PyTorch (not the RK3588 NPU): about 7.6 analysed fps with both modules, ~5 % frames replaced. The dynamic rate was checked with two simulated RTSP cameras on that container (rate 30 -> 15 -> 30 as a camera joined and left; analysed fps stayed compute-bound at about 7-10); how much a second camera really costs on the RK3588 is not measured.


## Cameras and frame rate (all modules)

* At most `AIV_MAX_AI_CAMERAS` (default **2**) cameras can have a module running at the same time. A Start on a third camera is refused with `CAMERA_LIMIT` (HTTP 409) and a message naming the cameras that are running; a second module on a running camera is always allowed. A camera that only has its live view open does not hold a slot; a paused module does.
* The analysed rate is dynamic: **one camera gets the maximum** (`AIV_AI_FPS_MAX`, default 30, or its own frame rate if lower); with `n` cameras each gets `AIV_AI_FPS_BUDGET / n` (default budget 60: 2 cameras -> 30 each, i.e. not limiting; a budget of 30 would give 15 each), never below `AIV_AI_FPS_MIN` (2).
* The change is gradual: a camera that is already running moves by at most `AIV_AI_FPS_RAMP` (6) fps per second; a camera that has just started begins at its share. Stopping a camera lifts the other one back up the same way.
* The rate is a ceiling on the frames the camera session accepts (it also drives the line-crossing interval gate), on top of any per-module capture cap (the lower value wins). A saved `targetFps` in a line-crossing module's settings no longer pins the rate.
* `AIV_AI_FPS_BUDGET` is a tuning value, not a measurement: set it from the analysed fps one camera really reaches on the box (`./aivision.sh npu status`).
* Live values: `metrics.aiFpsTarget` (current rate), `aiFpsGoal`, `aiCameras`; the crossing status `targetFps` follows the same rate. Tests: `python3 -m unittest backend.tests.test_ai_rate`.

## Frame pipeline (one camera on several NPU cores)

The line-crossing engine splits one camera's work so that frames are analysed in parallel:

* `depth` **detection workers**, each with its own model (on the RKNN engine each model is on a different NPU core), take the newest waiting frames and run the network at the same time (resize, NPU model, NMS).
* **One tracking thread** takes the finished detections strictly in frame order (ByteTrack needs the order) and runs the crossing rules, events and snapshots. A frame that fails never blocks the ones after it; a missing frame is skipped after 2 s (`pipeline.orderSkipped`).
* Depth follows the camera count: one camera uses `AIV_NPU_CORES` (3) workers, two cameras one worker each. `AIV_PIPELINE_DEPTH` = `auto` (3 on RKNN, 1 on every other engine) or a number (`1` = off).
* Checked: with the real detector the split network / tracker path gives the same track ids and boxes as `model.track` (24 frames), and 3 workers count the same as 1 (`RealModelPipelineTests`); ordering, overlap and a failing frame are tested with scripted detections (`PipelineTests`).
* Measured on a 2-CPU PyTorch container (forced depth 3, not the RK3588): one camera analysed 14.9 fps of a 15 fps stream where one worker gave about 7.5 fps; two cameras (depth 1 each) stay at about 7.5 fps each. The RK3588 gain is **not measured yet**: compare `inferenceFps` in the detection status with `AIV_PIPELINE_DEPTH=1`.
* Live values: `metrics.pipeline` (`depth`, `workers`, `inFlight`, `orderSkipped`) in the crossing status.


## Line ends (A / B)

The line is a finite segment. A person whose foot point passes right at A or B used to be missed when the path fell a few
pixels outside the segment. The segment now counts `endExtend` (default 0.5) x the person's box width beyond A and B
(setting "Line ends: count this far beyond A / B"; 0 = exact segment). A path clearly around an end is still not counted;
the Tracking row shows how many such passes happened ("passed beyond a line end"), so a line that is too short is visible.
