# AI frame sampling (multi-camera optimisation)

Every camera keeps ONE connection and one reader. Each running module decides per frame whether the AI
analyses it (`backend/services/scheduler.py`); the live view keeps its own frame rate.

| Module | Default interval | Frames processed / shown | Motion gate | Confirmation in sampled mode |
|---|---|---|---|---|
| Vehicle Detection | 2 s | 16 fps | never | one analysed frame, confidence >= class threshold + 0.10; same class at the same place reported once per 60 s |
| Human Detection | 4 s | 6 fps | on (fallback check every 30 s, hold 10 s after motion) | as vehicle |
| Human Count | 4 s | 6 fps | never | people in view per sample (occupancy); unique counting needs interval 0 |
| Object Monitoring | every frame | - | on (fallback 30 s, hold 10 s) | unchanged |
| Fire & Smoke | own worker (everyNFrames) | - | never | unchanged |

Interval 0 = analyse every frame with the original track-based confirmation (previous behaviour).
Human + Vehicle on one camera share one model pass; it runs at the shorter interval and both modules use it.
A camera running several modules processes frames at the HIGHEST rate any of them needs; a module without a
cap (Fire & Smoke, Object Monitoring, interval 0) removes the cap.

Settings: `.env` (`AIV_VEHICLE_INTERVAL`, `AIV_HUMAN_INTERVAL`, `AIV_COUNT_INTERVAL`, `*_CAPTURE_FPS`, motion
settings) and per camera in the module Settings dialog ("AI frame sampling", stored in
`camera.detection.modules.<module>.schedule`: intervalSec, captureFps, imgSize, motionGate, fallbackSec, ...).

Metrics (`/api/system/metrics` -> sessions[].schedule.<module>, and the "AI sampling" row on the detection page):
aiFps (measured over the last 30 s), framesSelected / framesProcessed, skippedSampling, skippedNoMotion (AI calls
avoided), fallbackRuns, rejectedStale, replacedQueue, lastFrameId / lastCapturedAt / lastFrameAgeMs; per session
framesSkippedCapture, captureFpsTarget, receivedFps, displayFps. framesRecorded is null: the project has no
video recorder.

Limits: with seconds between analyses a vehicle or person that crosses the picture between two samples is
missed; motion is a trigger, not a detector (people standing still, small / distant people, slow movement,
lighting changes). Validate the intervals on your own footage before relying on them.
