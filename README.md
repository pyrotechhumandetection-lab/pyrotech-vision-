# PYROTECH VISION — Camera Detection System

Operator console + detection backend in one repository.

* **Frontend** (repository root): dashboard, camera management, Add/Edit Camera with zone drawing,
  Live View, Human Detection, Vehicle Detection, Human Count, Fire & Smoke Detection, Object
  Monitoring, Alerts, Snapshots, Recordings, Reports, AI Models, Settings. Plain HTML/CSS/JavaScript —
  no build step, no npm dependencies.
* **Backend** (`backend/`): ONE common backend with five detection modules sharing the camera
  connections, configuration, API, snapshot storage and alert handling:

  | Module | Origin | Engine |
  |---|---|---|
  | Human Detection, Vehicle Detection | `human_detect_yolo-2.py` | YOLOv8n + ByteTrack, one pass for both |
  | Human Count (+ Head count) | written for this console | person tracker / dedicated head detector |
  | Fire & Smoke Detection | the standalone fire & smoke detector (`main.py`) | fire/smoke YOLO11 model + smoke verifier + smoke rules |
  | Object Monitoring | the standalone marked-object monitor (`app.py` + `src/`) | template/ORB matcher + CSRT tracker, YOLOE names, optional Grounding DINO |

  RTSP capture (one connection per camera for all modules), live annotated MJPEG stream, snapshot
  saving and alert generation. Python standard-library server — no web framework.

## Project structure

The frontend already lived at the repository root, so it stays there (it is not duplicated
into a `frontend/` folder).

```
D:\ui\                         (repository root = frontend; copy this ONE folder to another PC)
├── index.html  css\  js\  assets\     frontend (unchanged apart from API integration)
├── server.js                   frontend server; proxies /api to the backend; serves only frontend files
├── package.json                npm start / npm run backend / npm run test:backend
├── start_backend.cmd           START HERE: prepares backend\.venv if needed, starts the web UI + the common backend
├── setup-backend.cmd           creates/updates backend\.venv with the pinned packages (start_backend.cmd runs it)
├── start.cmd                   same as start_backend.cmd (old name)
├── start-backend.cmd           backend only, no checks (advanced)
├── start-frontend.cmd          starts server.js
├── .env.example                all settings for both parts (copy to .env)
├── README.md
├── scripts\debian\              Debian AI box: systemd services, status, perf-tune.sh (performance monitor / CPU optimizer)
├── scripts\perf\                pyrotech_perf.py (monitor, benchmark, apply, rollback) + bench_worker.py
├── docs\DEBIAN_AI_BOX_DEPLOYMENT.md   AI box: installation, services, camera streams, performance tuning
└── backend\
    ├── main.py                 entry point  (main.py --check = self-check)
    ├── config.py  selfcheck.py
    ├── runtime.py              CPU settings: PyTorch / OpenCV threads, inference concurrency, CPU affinity (read at start-up)
    ├── requirements.txt        direct dependencies (pinned)
    ├── requirements-lock.txt   exact versions of the working detector environment (installed by setup)
    ├── detection\              existing detection logic
    │   ├── pipeline.py         person + vehicle algorithm (classes, thresholds, tracking, rider rule)
    │   ├── frame_reader.py     LatestFrameReader (RTSP capture, reconnect)
    │   ├── modules.py          the five modules: defaults + validation
    │   ├── human_count.py  head_count.py   Human Count, Head count
    │   ├── fire_smoke.py       Fire & Smoke Detection (from the fire & smoke detector's main.py)
    │   ├── object_monitor\     Object Monitoring (from the marked-object monitor's src\ + app.py)
    │   └── rtsp_probe.py       connection test (TCP -> RTSP DESCRIBE -> decoded frame)
    ├── services\               cameras + DPAPI credentials, shared camera sessions, events/snapshots/alerts
    ├── api\server.py           REST API + MJPEG live stream
    ├── models\                 yolov8n.pt (+ OpenVINO), yolov8s.pt (optional), bytetrack_stable.yaml,
    │                           head_yolov8m\, fire_smoke\, object_monitor\  (see models\README.md)
    ├── snapshots\              saved detection images
    ├── data\                   cameras, credentials, alerts, detections, snapshot index (JSON)
    ├── tests\                  unit tests
    ├── tools\download-wheels.cmd   prepares an offline install (backend\wheels)
    ├── tools\install-grounding-dino.cmd   optional: Object Monitoring Layer 3 model
    ├── tools\migrate_data.ps1      optional one-time import of data saved by an older installation
    ├── .venv\                  created by setup-backend.cmd (not copied between computers)
    └── .ultralytics\           Ultralytics settings (created at runtime)
```

**Self-contained:** the backend needs nothing outside this folder at runtime: code, model weights,
tracker config, data, snapshots and its Python environment are all inside `backend\`. Ultralytics
runs offline (`YOLO_OFFLINE=1`, `YOLO_AUTOINSTALL=0`): no internet access and no automatic package
installs while running. The only thing a new computer needs is **Python 3.14** and **Node.js 16+**.

## Setup on a computer (Windows) - once

1. Install **Python 3.14** (python.org; tick "Add python.exe to PATH") and **Node.js 16+**.
2. Copy `.env.example` to `.env`. The defaults work as they are.
3. Double-click **`start_backend.cmd`**. On the first start (and whenever the environment is older than
   the modules need) it runs **`setup-backend.cmd`**, which:
   * creates `backend\.venv` with Python 3.14 (an existing one is updated in place)
   * first lists any program that still has `backend\.venv` files open (usually an old backend window)
     and offers to stop it - Windows cannot replace a loaded file, pip would otherwise fail with
     `[WinError 5] Access is denied: ...\cv2\cv2.pyd` (`backend\tools\venv-in-use.ps1`)
   * installs the exact package versions (`backend\requirements-lock.txt`, about 1 GB from PyPI, CPU
     PyTorch; ultralytics 8.4.155 for all five modules)
   * makes the **contrib** OpenCV build the active one (Object Monitoring's CSRT tracker; ultralytics
     itself pulls in plain `opencv-python`)
   * runs the self-check: imports, model files, paths inside the folder, and detection on a sample
     image. It must end with `Result: OK`.

**Offline computer:** on any Windows PC with internet, run `backend\tools\download-wheels.cmd`.
It fills `backend\wheels\`. Copy the whole folder, then run `setup-backend.cmd` on the offline PC; it
installs from `backend\wheels` without internet.

**NVIDIA GPU (optional):** after setup, install the CUDA build of the same PyTorch version into
`backend\.venv` (see pytorch.org) and set `AIV_DEVICE=0`.

**Existing data (optional):** to bring over the cameras, alerts and snapshots that the earlier backend
saved in the original detection folder, stop that backend and run:
```powershell
powershell -ExecutionPolicy Bypass -File backend\tools\migrate_data.ps1 -From "C:\Program Files\human\human detection"
```
It only copies (the source folder is not modified), and the backend never reads the source folder
afterwards. Camera passwords are encrypted for one Windows account on one PC (DPAPI). On a different
computer or account the Cameras page shows "Saved password cannot be decrypted". Edit the camera and
enter the password again.

**Check at any time:** `start-backend.cmd --check` (or `backend\.venv\Scripts\python backend\main.py --check`).

## Setup and start on Debian / Ubuntu Linux

ONE script, `aivision.sh`, does everything on Debian / Ubuntu (the AI box):

```
git clone https://github.com/pyrotechhumandetection-lab/pyrotech-vision-.git
cd pyrotech-vision-
./aivision.sh install          # downloads + installs every requirement, installs the backend and the web UI as
                               # services (start at every boot, restart after a crash), starts and checks them
```

Console: `http://<box-ip>:5173`. The first run needs internet and takes a few minutes (about 1 GB).

| Command | What it does |
|---|---|
| `./aivision.sh install` | first installation (and after moving the folder / changing ports): setup + services + start + check |
| `./aivision.sh update [--data-dir DIR]` | backup, `git pull`, setup only if requirements changed, reinstall, check |
| `./aivision.sh start` / `stop` / `restart [backend\|frontend]` | control the services |
| `./aivision.sh status` | step-by-step status (services, ports, API, cameras, detections, recent errors) |
| `./aivision.sh setup [--reinstall]` | only download / install the requirements (apt, Node.js, `backend/.venv`, pinned packages, self-check) |
| `./aivision.sh run` | run in this terminal without services (Ctrl+C stops); refused while the services run |
| `./aivision.sh check` | read-only environment check |
| `./aivision.sh ai-stream list` / `sub --all` | let the AI analyse the cameras' substreams |
| `./aivision.sh perf status` | performance monitor / CPU tuning |
| `./aivision.sh uninstall` | remove the services (data, models, `.env` are kept) |
| `./aivision.sh <command> --help` | details |

Optional tools: `backend/tools/install-grounding-dino.sh <folder>` (Object Monitoring Layer 3),
`backend/tools/download-wheels.sh` (all packages to `backend/wheels/linux` for an offline install).
Windows keeps its own `.cmd` scripts (table above). Details: [docs/DEBIAN_AI_BOX_DEPLOYMENT.md](docs/DEBIAN_AI_BOX_DEPLOYMENT.md).

`backend/tools/migrate_data.ps1` and `venv-in-use.ps1` are Windows-only and have no Linux version (not needed there).

* Python 3.13 or 3.14 is used; on Debian 12 (Python 3.11) setup fetches Python 3.13 with `uv` into
  `backend/.uv` without changing the system Python. CPU PyTorch unless an NVIDIA GPU is found.
* Settings: edit `.env` (ports, `HOST=0.0.0.0` to open the UI from other PCs, model, engine), then `./aivision.sh restart` (services) or run `./aivision.sh run` again. `AIV_TORCH=cpu|cuda` forces the PyTorch build.
* `./aivision.sh run` options: `--setup-only`, `--check`, `--reinstall` (rebuild `backend/.venv`), `--no-browser`.
* OpenVINO (faster CPU inference) is optional: where no OpenVINO package exists for the PC (for example
  64-bit ARM with an older system library) setup warns and every module runs on PyTorch.
  `AIV_OPENVINO=off` skips it. 32-bit systems are not supported (no PyTorch).
* Cameras are not in the repository: add them in the UI. Camera passwords from the Windows PC cannot
  be moved (they are encrypted for that Windows user); on Linux they are stored with owner-only file
  permissions.

## Start

**Everything at once:** double-click **`start_backend.cmd`**. It first checks whether an older backend still
holds the API port (and offers to end it), then checks the Python environment, starts the web UI in a second window,
opens the browser and runs the common backend with all five detection modules in its own window.
Or run each part in its own terminal:

```powershell
# terminal 1 - backend  (API on http://127.0.0.1:8000/api)
cd D:\ui
start-backend.cmd
#   or: backend\.venv\Scripts\python backend\main.py

# terminal 2 - frontend (http://127.0.0.1:5173, /api proxied to the backend)
cd D:\ui
npm start
#   or: start-frontend.cmd   /   node server.js
```

Open http://127.0.0.1:5173. The top bar shows **Backend connected** when both are running.

Then go to **Cameras → Add Camera**. Enter the RTSP URL without credentials, and the username and
password in their own fields. Click **Test Connection**, then **Save**. Open **Detections → Vehicle
Detection** (or Human Detection), select the camera and press **Start Detection**.

UI only, without a backend: set `BACKEND_URL=off` in `.env` (or the environment) and run `npm start`.
The UI contains no demonstration or sample data: everything shown comes from the backend.

The console always opens in **light mode**; dark mode can be switched on from the top bar or Settings.
Each camera card, the camera details, the dashboard tiles, the Detection Area panel of the camera form
and the Human/Vehicle Detection status show the camera's **IP address** (from its RTSP URL, never with
credentials) and its live state from the backend (`Active · detecting`, `Active · streaming`,
`Connecting…`, `Disconnected · reconnecting`, `Not streaming`, ...). The UI polls `/cameras` and
`/system/metrics` every 4 s (`cameraPollMs` in `js/config.js`) and updates these in place.

## Configuration (`.env`)

Real environment variables override `.env`. Relative backend paths resolve against `backend\`.

| Variable | Default | Used by | Meaning |
|---|---|---|---|
| `PORT` / `HOST` | `5173` / `127.0.0.1` | frontend | UI address (`HOST=0.0.0.0` for the LAN) |
| `BACKEND_URL` | `http://127.0.0.1:8000/api` | frontend | where `/api` is proxied; `off` = UI only |
| `AIV_HOST` / `AIV_PORT` | `127.0.0.1` / `8000` | backend | API address |
| `AIV_MODEL` | `models/yolov8n.pt` | backend | weights (e.g. `models/yolov8s.pt` after copying it there) |
| `AIV_TRACKER_CONFIG` | `models/bytetrack_stable.yaml` | backend | ByteTrack settings |
| `AIV_SNAPSHOT_DIR` | `snapshots` | backend | snapshot image root |
| `AIV_DATA_DIR` | `data` | backend | JSON data (falls back to `%LOCALAPPDATA%\AIVISION\data` if not writable) |
| `AIV_DEVICE` | auto | backend | `cpu`, `0` = first CUDA GPU |
| `AIV_IMG_SIZE` | `640` | backend | inference size (original value) |
| `AIV_CONNECT_TIMEOUT` | `10` | backend | camera open / first frame (s) |
| `AIV_EVENT_COOLDOWN` / `AIV_SNAPSHOT_COOLDOWN` | `30` / `0` | backend | default cooldowns per class (s); snapshots `0` = one per confirmed vehicle |
| `AIV_ENGINE` | `auto` | backend | `auto` (OpenVINO if installed, else PyTorch), `openvino`, `pytorch` — same yolov8n weights |
| `AIV_FIRE_ENGINE` | `auto` | backend | Fire & Smoke model: `auto` (OpenVINO if installed, ~5x faster on CPU), `openvino`, `pytorch` |
| `AIV_FRAME_POLICY` | `queue` | backend | `queue` = every decoded frame is analysed in order (FIFO per camera); `latest` = original newest-frame behaviour |
| `AIV_FRAME_QUEUE_SECONDS` / `_MAX_MB` | `0.5` / `96` | backend | per-camera queue size; when full the oldest frames are dropped and counted ("dropped (queue full)") |
| `AIV_TORCH_THREADS` / `AIV_TORCH_INTEROP_THREADS` | *(empty)* | backend | PyTorch threads per model call / inter-op pool. Empty: Ultralytics sets min(8, CPUs−1) per call, for every camera at once |
| `AIV_INFER_CONCURRENCY` | *(empty)* = unlimited | backend | model calls (all cameras, fire and object workers) running at the same time; the others wait in order |
| `AIV_CV_THREADS` / `AIV_CPU_AFFINITY` / `AIV_OMP_WAIT_POLICY` | *(empty)* | backend | OpenCV threads / CPUs the backend may use (e.g. `4-7`) / `passive` or `active` OpenMP waiting |
| `AIV_SHARED_PERSON_INFERENCE` | `1` | backend | Human Detection + Human Count on one camera use one model pass (identical detections); `0` = two passes |
| `AIV_HEAD_ENGINE` | `auto` | backend | Head count model engine: `auto` (OpenVINO FP16 if installed), `openvino`, `pytorch` |
| `AIV_HEAD_MAX_FRAME_AGE` | `2` | backend | Head count uses the running stream's newest frame if it is at most this many seconds old, else reads a new frame |
| `AIV_STREAM_MAX_FPS` / `_MAX_WIDTH` / `_JPEG_QUALITY` | `15` / `1280` / `75` | backend | MJPEG output |
| `AIV_CORS_ORIGINS` | *(empty)* | backend | only if a browser calls the backend directly |
| `AIV_LOG_LEVEL` | `INFO` | backend | `DEBUG` for request logs |
| `AIV_PYTHON` | *(empty)* | start-backend.cmd | another Python to use instead of `backend\.venv` (normally empty) |
| `YOLO_OFFLINE` / `YOLO_AUTOINSTALL` | `1` / `0` | backend | Ultralytics offline mode, no automatic pip installs |

**Per camera (camera form):** *AI detection stream* = same as viewing stream (default) / camera substream
(Hikvision, Dahua URL patterns only) / custom AI stream path - the AI then analyses the low-resolution
substream while the main stream stays the viewing stream; *Snapshot image* = boxes only, or boxes + a
full-resolution original from the main stream. Measurements, costs and setup:
[docs/DEBIAN_AI_BOX_DEPLOYMENT.md §11](docs/DEBIAN_AI_BOX_DEPLOYMENT.md).

**CPU thread settings** are read once at start-up. Do not guess them - `./aivision.sh perf` monitors the
running application, benchmarks thread configurations on frames from your cameras, applies the best one to `.env`
(backup first), measures before/after and rolls back automatically when there is no measured gain:
[docs/DEBIAN_AI_BOX_DEPLOYMENT.md §13](docs/DEBIAN_AI_BOX_DEPLOYMENT.md).

## How it works

```
Camera (RTSP) ─► LatestFrameReader (1 per camera) ─► PersonVehiclePipeline (1 YOLO+ByteTrack per camera)
                                                       │                       │
                                   annotated frames ◄──┘                       └─► new confirmed tracks
                                          │                                         │
                       MJPEG /api/cameras/{id}/stream.mjpeg          event → snapshot (policy + cooldown)
                                          │                                → alert (policy + cooldown)
            Vehicle page · Human page · Live View · Dashboard          Snapshots page · Alerts page · Dashboard
```

* **Human Detection** and **Vehicle Detection** are separate modules. Each has its own page,
  start/stop/pause, settings, snapshots and alerts. On a given camera they share one RTSP
  connection and one inference pass, as the original script did.
* **Event** = the tracker confirms a new track (2 hits + label voting, the original values),
  never one per frame. ByteTrack can re-assign an ID after occlusion.
* **Snapshots** (on by default): `backend\snapshots\{human|vehicle}_detection\YYYY\MM\DD\CAM01_vehicle_bus_20261009_114500_123_ab12.jpg`.
  Manual captures from the camera pages go to `backend\snapshots\manual\…`. A record is only created
  after the image is written and read back. Images are served only through
  `/api/snapshots/{id}/image`.
* **Alerts** (on by default, severity `low` = informational, per-class cooldown): also
  `camera_disconnected`, `engine_failure` and `snapshot_failed`. Stored in `backend\data\alerts.json`.
* **Credentials** go only to `backend\data\credentials.json`, encrypted with Windows DPAPI. They are
  never returned by the API, logged, or shown in errors.
* The frontend polls alerts, detections and snapshots every 5 s (`eventPollMs` in `js/config.js`).

### Frame rate, exact-moment snapshots and fast vehicles

* **Measured, never estimated** (Vehicle/Human Detection → Detection Status, `/api/system/metrics`):
  *Camera FPS (received)* = frames actually decoded from the camera per second (plus the rate the
  stream announces), *AI processing FPS* = frames analysed per second, frames received / analysed /
  skipped (skipped = decoded while the AI was still busy), AI inference time, frame age at analysis,
  and which stage is the bottleneck. Before this change the "Camera FPS" figure was really the AI rate.
* **Inference engine:** with the `openvino` package installed (`setup-backend.cmd` installs it) the
  same yolov8n weights run through OpenVINO (`backend\models\yolov8n_openvino_model`, FP32,
  dynamic shape) - identical boxes on CAM-02 frames and about 3-4x faster on CPU. Without it, or if it
  fails to load, the backend uses PyTorch automatically. `AIV_ENGINE=pytorch` forces the original engine.
* **Exact-moment snapshots:** the snapshot is the frame on which the vehicle was confirmed, copied in
  the detection loop and stamped with that frame's capture time. Snapshot and event records carry
  `timestamp`/`detectedAt`, `label`, `vehicleClass` (`car`, `truck`, `two_wheeler`, `bus`),
  `confidence`, `cameraIp`, `trackId` and `box`. One snapshot per tracked vehicle (a track is never
  reported twice); JPEG writing and JSON saving run on a writer thread, so they never stall detection.
* **Fast vehicles:** (1) vehicle tracks use buffered IoU (`vehicle_iou_buffer: 0.3`,
  `backend\detection\tracker.py`), so a vehicle that moves further than its own width between analysed
  frames keeps one track - measured on CAM-02 vehicles, the model detected them in almost every frame but
  plain ByteTrack dropped the track; (2) a vehicle track whose first 3 votes agree on the class with a mean
  confidence of class threshold + 0.10 is confirmed after 3 frames instead of 5 (mixed or borderline
  votes still need the original 5-vote majority); (3) a higher AI frame rate gives every track more frames.
* **Snapshots page:** selecting a road camera (vehicle detection enabled, or vehicle snapshots on the
  backend) shows *All vehicles / Cars / Trucks / Two-wheelers* filters with counts, from each snapshot's
  `vehicleClass` (older records: derived from their class label by the backend). `GET /api/snapshots?vehicleClass=truck` filters on the backend.

### Human Count

A separate people-counting module (sidebar **Detections → Human Count**) in the same backend:
`backend/detection/human_count.py` (engine), `backend/services/counts.py` (history), module `count`
in `backend/detection/modules.py`, page `js/human-count.js`. It uses the camera's shared session
(one RTSP connection, one MJPEG stream) and the same `/api/detections/{module}/*` routes; it can run
alone or together with Human / Vehicle Detection on the same camera (each module has its own counts).

* **Detector:** the shipped COCO yolov8n weights (OpenVINO when installed, else PyTorch), person class only,
  as a separate model instance (detection only - its own tracker, not Ultralytics' ByteTrack).
* **Tracker (no double counting):** constant-velocity motion of each person's box centre plus a smoothed
  box size, optimal assignment on overlap with a centre-distance gate. A new person needs *Frames to
  confirm* (default 3) detections before being counted; a counted person keeps the same ID while visible
  and for *Keep ID when hidden* (default 2 s) after a missed detection or occlusion, so they are not
  counted again. A box truncated by partial occlusion does not break the track.
* **Numbers:** *People now* (confirmed people in the current frame), *People counted* (each person once,
  since Start), *Peak*, *Average (last 60 s)*, per-minute history (saved in `backend\data\human_count.json`,
  last 24 h per camera) and the list of recently counted people. Optional zones (people are counted where
  they stand) and an occupancy alert (*people now ≥ N*, with a snapshot of that frame).
* **Measured** (OpenCV `vtest.avi`, 768×576, 10 fps, 795 frames of people walking): per-frame counts
  matched a manual count on sampled frames (one person mostly hidden behind a sign post was missed once);
  tracks fragmented mid-view 0-1 times (12 before the size/occlusion handling); the same counts at 10, 5
  and 3.3 analysed frames per second.
* **Limits:** there is no face / appearance re-identification - a person who leaves the view and comes
  back, or is hidden longer than *Keep ID when hidden*, is counted again (a new visit). Very dense crowds
  with heavy overlap are under-counted by any box detector.

### Human Count - Head count (capture & count)

**Detections → Human Count → Head count** (the page's first tab; *Live people counting* is the second tab).

1. **Select a camera** - its live feed is shown.
2. **Draw / select the detection area** on the feed: Rectangle, Polygon or Whole frame. Areas are saved in the
   camera's Detection Area zones with **Human Count** ticked, so they also appear (and can be edited) in
   Camera Management → Live Preview & Detection Area. Ticking Human Count on a zone there, or switching the
   Human Count module on without an area, asks for the area too. Zones of other modules are not changed.
3. **Capture & Count** - the backend takes ONE frame (from the running stream when one is open, otherwise
   it reads a fresh frame from the camera), detects heads inside the area and saves the **original frame and
   the annotated image** (numbered box per head, area boundary, camera IP, detection type, total, capture
   time). The total, the annotated image and the original always come from that same frame. Nothing updates
   until the next Capture & Count. Results are listed under *Recent head counts* and in **Snapshots**
   (type *Head count*, with a link to the original frame). Images: `backend\snapshots\head_count\YYYY\MM\DD\`.

* **Model:** `backend/models/head_yolov8m` - a dedicated single-class **head** detector (YOLOv8m, trained at
  1280 px on a crowd head dataset; heads from the front, side and back), not the person model. OpenVINO FP16
  when installed (identical boxes to the PyTorch weights on the test images, ~6x faster), else PyTorch.
  Source and licence note: `backend/models/README.md`.
* **Processing:** the frame is cropped to the area (+ margin) so heads keep the most pixels; large areas are
  also scanned as overlapping tiles; duplicate boxes are removed by NMS plus a containment check; a head is
  counted when its box centre is inside the area; boxes smaller than *Smallest head* are ignored.
* **Settings** (Head count → Detection settings, `backend\data\head_count_settings.json`): head confidence
  (0.35), NMS IoU (0.45), detection resolution (1280), tiled detection (auto), smallest head (7 px).
* **Measured** (this repository's test frames, manual check of every box): OpenCV `vtest.avi` CCTV frames
  (people walking toward / away / sideways, 768×576) 22 of 22 heads, no false boxes; dense street crowd
  (2592×1944, all orientations, partly hidden people) 45 boxes, 1 duplicate (a ponytail under its head),
  about 8-10 very small distant pedestrians missed; close-up frames (front / side / back) all heads found,
  one basketball counted as a head; empty areas 0. **Road camera CAM-02 (steep top-down view, sunlight):
  only about 3 of 10 people found - riders wearing helmets, a head covered with a cloth and people in deep
  shade are not detected** at any threshold or resolution.
* **Limits:** helmets / caps covering the head, steep top-down views, heads smaller than about 8 px,
  heavy blur, full occlusion and round objects (balls) at small sizes. For a camera like CAM-02 the model has
  to be fine-tuned on labelled frames from that camera (same file names, single class `head`).

### Fire & Smoke Detection

Sidebar **Detections → Fire & Smoke Detection**. The standalone fire & smoke detector's logic, unchanged,
on the camera's shared session (`backend/detection/fire_smoke.py`):

* **Model:** `models/fire_smoke/firedetect-11s.pt` (YOLO11s, classes Fire / Smoke, from the Hugging Face
  repository `leeyunjai/yolo11-firedetect` that the original downloaded) at 480 px, every 2nd frame,
  in its own worker thread (frames are skipped while it is busy, as in the original).
* **Smoke verifier:** `models/fire_smoke/smoke_verifier.npz` checks every smoke box (smoke / cloud /
  dust) on the model's own features; it is tied to these exact weights (SHA-256 checked).
* **Rules (original values):** fire ≥ 0.30, smoke ≥ 0.65 alert directly; weaker smoke (≥ 0.40) only with
  a fire box nearby, or when it rises / grows for 20 s, or persists 90 s; an alert needs fire in 2 / smoke
  in 3 of the last 6 analysed frames, then 10 s cooldown per class. The camera's green overlay boxes are
  erased before detection. All of this is editable per camera on the page and applies live.
* **Output:** red / blue boxes and a FIRE DETECTED banner on the live stream; every confirmed alert is
  an event + annotated snapshot (Snapshots) + alert (fire *critical*, smoke *high*).
* **Measured:** on the original project's `fire.mp4` the integrated module and the original `main.py`
  both raised fire 3× and smoke 1× ("fire nearby"); on `video1.mp4` both raised nothing.
* **Fixed:** in the original, a class that reached the M-of-K rule on earlier frames but was missing in
  the current frame raised `KeyError` and the alert was lost; the module reports it with the class's last
  confidence.
* Not used: the original's optional "nano" fallback model (`model.variant: small` was configured) and its
  training / dataset tools.

### Object Monitoring

Sidebar **Detections → Object Monitoring**. The standalone marked-object monitor's three layers on the
camera's shared session (`backend/detection/object_monitor/`, code unchanged apart from imports,
logging and a "remove one object" command):

1. Start monitoring on a camera, press **Mark object** and drag a box around an object on the live
   picture (optional name). The picture inside the box becomes its reference — mark objects while they
   are in their normal place.
2. **Layer 1** checks every object (template + ORB/SIFT + colour histogram, CSRT tracker while displaced):
   in place / checking / covered / **MOVED** / **MISSING**, with the original hysteresis (4 bad checks,
   2 s moved / 5 s missing, 3 good checks to clear, 30 s cooldown). A person in front of an object makes
   it *covered*, not missing; a change of the whole picture pauses the checks.
3. **Layer 2** (`models/object_monitor/yoloe-26n-seg.pt` at 960 px, every 3 s in its own thread) names
   unnamed objects from the original 23-name office vocabulary and spots people. Its text embeddings are
   the original cache (`yoloe-26n-seg_vocab_87c9a6699f.npz`), so no text encoder is needed — the
   vocabulary is therefore fixed.
4. **Layer 3** (Grounding DINO name suggestions) is optional and not shipped (690 MB): install it with
   `backend\tools\install-grounding-dino.cmd "<folder with models--IDEA-Research--grounding-dino-tiny>"`.
   Until then the page shows "Layer 3: Not installed" and monitoring works without it.

MOVED / MISSING create an event + annotated snapshot (where it was, where it is now) + alert (*high*);
"back in place" is logged as an event. Marked objects are remembered per camera
(`backend\data\object_marks.json`) and re-referenced from the first frame after the next start.
Measured on a real 1920×1080 office frame (lunch box moved 300 px, printer removed): the integrated module
and the original `app.py` gave the same alerts at the same times. Layer 2 naming is weak on this
camera (e.g. lunch box → "mug" 0.15, printer → "laptop" 0.17 — the original gives the same names).

## Backend API (all under `/api`)

Errors: `{"success": false, "error_code": "...", "message": "..."}` with a 4xx/5xx status.

| Method | Path | Notes |
|---|---|---|
| GET | `/system/health` · `/system/metrics` · `/system/storage` | health; CPU/RAM/GPU, FPS, latency, per-camera stage times (`stageMs`), CPU settings in effect; disk + snapshots |
| GET | `/system/runtime` | CPU settings requested (`.env`) and in effect (read back from PyTorch / OpenCV), inference-slot statistics, engine |
| GET | `/cameras/{id}/frame.jpg` | newest frame of a running session without boxes (AI stream) - used by the performance tool for representative frames |
| GET | `/detection-modules` | human / vehicle, classes, running state |
| GET, POST | `/cameras` | camera registry (password write-only) |
| GET, PUT, DELETE | `/cameras/{id}` | editing a source restarts its session |
| GET | `/cameras/{id}/status` | stored status + live session |
| POST | `/cameras/test-connection` · `/cameras/{id}/test-connection` | `{ok, code, message, frame, width, height, codec, fps}` |
| GET | `/cameras/{id}/stream` · `/cameras/{id}/stream.mjpeg` | stream info · annotated MJPEG |
| POST | `/cameras/{id}/snapshot` | manual snapshot |
| PUT | `/cameras/{id}/detection-config` | detection + zones from the camera form |
| GET | `/detections/{human\|vehicle\|count}/status?cameraId=` | state, counts, FPS, latency, last detection (`count`: people now, counted, peak, average, recent people) |
| GET | `/detections/count/history?cameraId=&minutes=60` | Human Count per-minute history (most at once, average, newly counted) |
| POST | `/detections/count/head-count` | Head count: capture one frame and count heads. Body `{cameraId, zoneIds?, roi?, fullFrame?, settings?}`; without zoneIds/roi the camera's zones with Human Count ticked are used (422 `ROI_REQUIRED` if none). Returns total, heads (id, box, confidence), snapshot record |
| GET | `/detections/count/head-counts?cameraId=&limit=20` | Saved head count results (newest first) |
| GET / PUT | `/detections/count/head-settings` | Head count settings (confidence, iou, imgSize, tiling, minHeadPx) |
| GET | `/snapshots/{id}/original` | Unannotated frame of a head count snapshot |
| GET | `/detections/object/objects?cameraId=` | Object Monitoring: marked objects with state, score, activity log |
| POST | `/detections/object/objects` | Mark an object `{cameraId, box: {x, y, width, height} (0-1), name?}` (module must be running) |
| PUT / DELETE | `/detections/object/objects/{id}` | Rename `{cameraId, name}` / stop monitoring one object |
| POST | `/detections/object/clear` · `/detections/object/accept` | Remove all objects · current positions become the new home |
| GET, PUT | `/detections/{module}/config?cameraId=` | per-camera settings, applied live |
| POST | `/detections/{module}/start` · `stop` · `pause` · `resume` · `snapshot` | body `{cameraId}` |
| GET | `/detections/{module}/events?cameraId=&limit=` | that module's events |
| GET | `/detections` · `/alerts` · `/snapshots` | `?cameraId=&module=&limit=` |
| GET / POST | `/alerts/{id}` · `/alerts/{id}/acknowledge` | `{note, by}` |
| GET / DELETE | `/snapshots/{id}` · `/snapshots/{id}/image[?download]` | |

Error codes: `AUTH_FAILED`, `CAMERA_UNAVAILABLE`, `UNREACHABLE`, `STREAM_UNAVAILABLE`, `TIMEOUT`,
`UNSUPPORTED_FORMAT`, `INVALID_CONFIG`, `CAMERA_NOT_FOUND`, `CAMERA_DISABLED`, `CAMERA_INCOMPATIBLE`,
`MODEL_NOT_FOUND`, `ENGINE_FAILURE`, `START_TIMEOUT`, `NOT_RUNNING`, `SNAPSHOT_WRITE_FAILED`.

## Tests

```powershell
cd D:\ui
backend\.venv\Scripts\python -m unittest discover -s backend/tests -t . -v
backend\.venv\Scripts\python backend\main.py --check
```

On Debian / Ubuntu: `backend/.venv/bin/python -m unittest discover -s backend/tests -t .` and `./aivision.sh run --check`.

## Frontend without a backend

The following work without a backend: navigation, search (Ctrl+K), theme, profile, camera forms with
validation, module settings and CSV reports. Detection areas are drawn on a real camera frame, so they
need the backend (Test Connection or a saved camera's live stream).

Connection tests, live video, detections, snapshots, alerts from cameras and system metrics need the
backend. Until it is reachable the UI shows "Unavailable" and never shows invented values.

## Security notes

* `server.js` serves only `index.html`, `css/`, `js/` and `assets/`. It never serves `backend/`,
  `.env`, data or snapshot files directly.
* Both servers listen on `127.0.0.1` by default. Before using `HOST=0.0.0.0` / `AIV_HOST=0.0.0.0`,
  note that the console has no login.
* Passwords are never stored in the browser. URLs are shown masked (`rtsp://***:***@host`).
