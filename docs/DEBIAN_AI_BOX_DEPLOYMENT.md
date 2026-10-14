# PYROTECH VISION on a Debian AI Box - automatic start-up and service management

After installation the AI box behaves like an appliance:

1. Power on → Debian boots → the backend and the web UI start by themselves (systemd).
2. Detections that were running before the power cut / reboot start again by themselves.
3. Nobody has to log in or open a terminal; logging out or closing SSH changes nothing.
4. A crashed service is restarted automatically (and the crash is recorded in the journal).
5. Any PC on the same network opens the console at **`http://<box-ip>:5173`**, for example
   **http://192.168.0.200:5173**. Port 80 of the box is not used.

```
cd ~/ui-system/pyrotech-vision
scripts/debian/check-environment.sh      # read-only check of the box (optional, recommended)
scripts/debian/install-services.sh       # install, enable at boot, start - asks for sudo
scripts/debian/status-services.sh        # is everything running?
```

---

## 1. Architecture selected

```
 PC on the LAN (browser)                         AI box (Debian)
 ───────────────────────                         ─────────────────────────────────────────────────────────
 http://192.168.0.200:5173  ───────────────▶  pyrotech-vision-frontend.service   node server.js
                                                 0.0.0.0:5173  - serves index.html / js / css / assets
                                                 /api/...     ─┐ (also the live MJPEG video)
                                                               ▼
                                              pyrotech-vision-backend.service    backend/.venv/bin/python backend/main.py
                                                 127.0.0.1:8000 - detection API, camera sessions, AI models,
                                                 alerts, snapshots, data (backend/data, backend/snapshots)
                                                               │
                                                               ▼  RTSP / HTTP
                                                         IP cameras on the network
```

**Two systemd services** (option B of the brief), because the repository consists of two programs:

* The **web UI** is plain HTML/CSS/JavaScript with **no build step** (no framework, no bundler, no
  `package.json` dependencies). `server.js` is a ~300-line Node.js web server that serves only the
  approved UI files and forwards `/api` to the backend. It has no watch/reload/dev tooling, so it is
  used as the production web server; nothing has to be built.
* The **backend** is a Python program (standard-library HTTP server + OpenCV/PyTorch/Ultralytics).
  It does not serve the UI files.

Why not one service: the backend would have to be changed to serve the UI, and the UI would then go
down with every backend restart. With two services the console stays reachable while the backend is
restarting (it shows "backend not reachable" and recovers by itself). The services do not depend on
each other.

Why no Nginx / Docker / PM2: not needed. `server.js` already does the only reverse-proxy job
(`/api` → `127.0.0.1:8000`), and systemd covers start at boot, restart and logging.

Security consequence: the **backend listens only on 127.0.0.1**. Only the web UI port (5173) has to
be reachable from the network; port 8000 cannot be reached from other PCs (verified, see §12).

---

## 2. Repository analysis (what the scripts are based on)

| Item | Finding |
|---|---|
| Frontend framework | none - vanilla HTML/CSS/JS (`index.html`, `js/`, `css/`, `assets/`) |
| Frontend start command | `node server.js` (Node.js ≥ 16, no npm packages, no lockfile) |
| Frontend production build | not applicable - files are served as they are |
| Package manager | none for the UI; Python packages via pip from `backend/requirements-lock.txt` |
| Backend framework / entry | Python standard library `ThreadingHTTPServer`; `backend/main.py` (changes into `backend/`) |
| Backend start command | `backend/.venv/bin/python backend/main.py` |
| Python version | 3.13 (Linux) or 3.14 (Windows); `setup-backend.sh` fetches 3.13 with `uv` on Debian 12 |
| Virtual environment | `backend/.venv` (created by `setup-backend.sh`) |
| Ports | UI `PORT` = 5173 (`server.js`), backend `AIV_PORT` = 8000 (`backend/config.py`) |
| Listening addresses | UI `HOST` (installer: `0.0.0.0`), backend `AIV_HOST` (installer: `127.0.0.1`) |
| API URL in the browser | relative `/api` (`js/config.js`) - no `localhost` and no IP in the frontend |
| WebSockets | none; live video is MJPEG over HTTP (`/api/cameras/<id>/stream.mjpeg`), the rest is polling |
| CORS | not needed (browser talks only to the UI server); `AIV_CORS_ORIGINS` stays empty |
| Environment variables | `.env` (from `.env.example`), read by both programs; real environment variables win |
| External services | IP cameras (RTSP/HTTP) on the LAN; **no internet needed at run time** (Ultralytics offline mode) |
| AI models | `backend/models/` (in the repository). Loaded when a detection starts, never downloaded at boot. The head-count model is joined once from its `.part` files into the same folder |
| Data written | `backend/data` (cameras, encrypted credentials, alerts, counts, `running_modules.json`), `backend/snapshots` |
| Health endpoint | `GET /api/system/health` (+ `/api/system/metrics` for camera/inference state) |
| Process management before | none (no unit files, no Docker); manual `start.sh` / `.cmd` scripts |
| Windows-specific parts | `.cmd`/`.ps1` scripts; camera passwords encrypted with Windows DPAPI. On Linux they are stored in `backend/data/credentials.json` with owner-only permissions (0600) - **passwords saved on the Windows PC cannot be carried over; add the cameras again** |

### Changes made to the application for appliance use

1. **Detections resume after a restart** (`backend/services/autostart.py`). Before, every reboot left
   all detections stopped until someone clicked Start. Now the backend remembers which module was
   started on which camera (`backend/data/running_modules.json`; Start adds, Stop / camera delete
   removes) and starts them again in the background when it starts. A camera that is not reachable
   yet is retried after 10 s, 20 s, 40 s … then every 2 minutes, until it answers; every attempt is
   logged. The web UI is never blocked by this. Detection logic is unchanged. `AIV_AUTOSTART=0` in
   `.env` switches it off.
2. **Clean shutdown on `systemctl stop`.** The backend now treats SIGTERM like Ctrl+C (and the unit
   sends SIGINT): cameras are closed and pending counts/alerts are written before it exits.
3. `start.sh` refuses to start a second copy while the services run (no duplicate camera loops).

---

## 3. Requirements on the box

| | Needed | Checked by |
|---|---|---|
| OS | Debian 12 / 13 (or Ubuntu) with systemd | `check-environment.sh` |
| CPU | 64-bit: x86_64 or aarch64 (ARM64). 32-bit is not supported (no PyTorch) | `check-environment.sh` |
| RAM / disk | 4 GB RAM or more; about 4 GB free disk for the Python environment, plus snapshots | `check-environment.sh` |
| Python | 3.13 / 3.14 for `backend/.venv` (Debian 12's 3.11 is handled by `setup-backend.sh`) | `setup-backend.sh` |
| Node.js | 16 or newer (`sudo apt install nodejs`; Debian 12 has 18, Debian 13 has 20) | installer |
| OpenVINO | optional speed-up; on aarch64 it needs glibc 2.35+. Without it PyTorch is used | `setup-backend.sh` |
| Internet | only once, for `setup-backend.sh` (about 1 GB of Python packages) | - |
| User | a normal user who owns the project folder (the services run as this user, **not root**) | installer |

---

## 4. Installation

```
# 1. get the software (once)
mkdir -p ~/ui-system && cd ~/ui-system
git clone https://github.com/pyrotechhumandetection-lab/pyrotech-vision-.git pyrotech-vision
cd pyrotech-vision

# 2. optional: look at the box first (changes nothing)
scripts/debian/check-environment.sh

# 3. install the services (runs ./setup-backend.sh first when something is missing)
scripts/debian/install-services.sh
```

`install-services.sh` (run it as the normal user - it asks for sudo only to write the unit files,
run `systemctl` and change the firewall):

1. checks the project files and the path;
2. picks the service user (owner of the folder; `--user NAME` to choose) and checks that this user
   can read the project and write `backend/data`, `backend/snapshots` and the head-model folder;
   refuses root unless `--allow-root`;
3. creates `.env` from `.env.example` if missing (values are never printed);
4. checks the ports (refuses 80; refuses ports used by another program, e.g. a `./start.sh` still
   running in a terminal);
5. runs `./setup-backend.sh` - installs only what is missing (apt packages, `backend/.venv`, pinned
   packages, self-check). A complete environment is left as it is: nothing is deleted or downgraded;
6. checks Node.js and that the backend Python imports ultralytics / torch / OpenCV contrib;
7. writes the two unit files, validates them with `systemd-analyze verify`, installs them to
   `/etc/systemd/system/` (never overwriting a unit file it did not create), `daemon-reload`, enables
   them for boot;
8. reports the firewall state (`--open-firewall` adds a ufw rule for the UI port only);
9. starts the backend, then the UI, and waits until the UI and the API answer;
10. prints the addresses to open, e.g. `http://192.168.0.200:5173`.

Options: `--user NAME`, `--ui-port N` (not 80), `--api-port N`, `--bind ADDR`, `--open-firewall`,
`--skip-setup`, `--no-start`, `--allow-root`. Run it again at any time (after changing ports, after
an update, after moving the folder) - it replaces only its own two unit files.

---

## 5. Day-to-day commands

| Task | Command |
|---|---|
| Status (all stages, see below) | `scripts/debian/status-services.sh` |
| Start | `scripts/debian/start-services.sh` (backend, then UI; waits until they answer) |
| Stop | `scripts/debian/stop-services.sh` (UI, then backend; clean shutdown, up to 60 s) |
| Restart both | `scripts/debian/restart-services.sh` |
| Restart one | `scripts/debian/restart-services.sh backend` / `... frontend` |
| Check the box | `scripts/debian/check-environment.sh` |
| systemd directly | `systemctl status pyrotech-vision-backend pyrotech-vision-frontend` |
| Do not start at boot any more | `sudo systemctl disable pyrotech-vision-backend pyrotech-vision-frontend` |

`stop-services.sh` stops until the next boot. Running detections are remembered and resume at the
next start.

### What `status-services.sh` reports (and what it does not claim)

It reports each stage separately, so "process started" is never mistaken for "working":

1. **Service process** - active, enabled at boot, PID, number of automatic restarts, since when.
2. **Listening ports** - UI on `0.0.0.0:5173`, backend only on `127.0.0.1:8000`.
3. **Web UI answers**, **backend API answers** (`/api/system/health`), **the UI forwards `/api`**
   (exactly what a browser on another PC uses), **model files present**.
4. Per camera: **camera connected** (a frame arrived in the last 15 s), **AI model loaded** (engine
   OpenVINO / PyTorch), **inference running** (frames analysed, fps).
5. **Resume after restart** - per remembered detection: running / retrying (with the reason) / failed.
6. **Recent warnings and errors** of the backend (last hour).

Exit code 0 = both services active and the API answers.

---

## 6. Logs and diagnostics

Logs go to the systemd journal (camera passwords are masked as `***:***@` in every message).

| Task | Command |
|---|---|
| Backend log, live | `journalctl -u pyrotech-vision-backend -f` |
| Web UI log, live | `journalctl -u pyrotech-vision-frontend -f` |
| Backend log since this boot | `journalctl -u pyrotech-vision-backend -b` |
| Last 100 lines | `journalctl -u pyrotech-vision-backend -n 100 --no-pager` |
| Only warnings/errors | `journalctl -u pyrotech-vision-backend -o cat \| grep -E ' (WARNING\|ERROR) '` |
| Crashes and restarts | `journalctl -u pyrotech-vision-backend \| grep -E 'Main process exited\|Scheduled restart'` |
| Service status | `systemctl status pyrotech-vision-backend pyrotech-vision-frontend` |
| Listening ports | `sudo ss -lntp \| grep -E ':5173\|:8000'` |
| API test on the box | `curl -s http://127.0.0.1:8000/api/system/health` |
| API test as a browser sees it | `curl -s http://127.0.0.1:5173/api/system/health` |
| Camera / inference state | `curl -s http://127.0.0.1:8000/api/system/metrics` |
| CPU / RAM | `top`, `free -h`, `systemctl status pyrotech-vision-backend` (memory line) |
| Disk (snapshots grow) | `df -h ~ ; du -sh ~/ui-system/pyrotech-vision/backend/snapshots` |
| Boot problems | `systemctl --failed`, `journalctl -b -p err`, `systemd-analyze blame \| head` |
| Previous boot | `journalctl -u pyrotech-vision-backend -b -1` |

A normal user may need `sudo` for `journalctl`, or once: `sudo usermod -aG systemd-journal $USER`.

---

## 7. Network access

| Port | Who listens | Reachable from the LAN | Why |
|---|---|---|---|
| **5173/tcp** | web UI (`node server.js`, `0.0.0.0`) | **yes - must be open** | the console, its API calls and the live video |
| 8000/tcp | backend (`127.0.0.1`) | no (by design) | reached only through the UI on the box itself |
| 80/tcp | not used | - | left free for the box's own web page |
| 554 etc. | cameras | outgoing from the box | the box connects to the cameras (RTSP/HTTP) |

* Open the console from another PC: `http://192.168.0.200:5173` (the installer and the status script
  print every address of the box). The frontend uses the relative path `/api`, so nothing has to be
  configured when the IP changes - just use the new address.
* Give the box a fixed address (DHCP reservation in the router, or a static address in Debian) so the
  URL stays the same.
* Firewall: with **ufw** active, `install-services.sh --open-firewall` adds exactly one rule
  (`5173/tcp`). Or by hand: `sudo ufw allow 5173/tcp`. The firewall is never disabled. Other
  firewalls (nftables/iptables rules): allow incoming TCP 5173.
* Another UI port: `scripts/debian/install-services.sh --ui-port 8080` (any port ≥ 1024 except the
  backend port; 80 is refused).
* `network-online.target` only means the network is configured - not that cameras are reachable.
  The backend therefore retries cameras on its own (§2).

---

## 8. Changing settings, updating the software

* **Settings** (`.env`): change the value, then `scripts/debian/restart-services.sh backend`.
  Ports (`PORT`, `AIV_PORT`) are written into the unit files: after changing them run
  `scripts/debian/install-services.sh` again. The services always listen with `HOST=0.0.0.0` (UI) and
  `AIV_HOST=127.0.0.1` (backend) - these override `HOST`/`AIV_HOST` in `.env`.
* **Update**:
  ```
  cd ~/ui-system/pyrotech-vision
  git pull
  scripts/debian/install-services.sh     # installs new packages if needed, restarts the services
  ```
* **Run by hand instead** (development): `scripts/debian/stop-services.sh`, then `./start.sh`.

---

## 9. Troubleshooting

| Symptom | Check / fix |
|---|---|
| Browser on another PC: "cannot connect" | On the box: `scripts/debian/status-services.sh`. If OK there: firewall (`sudo ufw status`; `sudo ufw allow 5173/tcp`), correct IP (`hostname -I`), PC in the same network |
| UI opens but says "backend not reachable" | `systemctl status pyrotech-vision-backend`; `journalctl -u pyrotech-vision-backend -n 100`. It may still be starting (a few seconds) |
| Backend restarts again and again | `journalctl -u pyrotech-vision-backend -b` shows the error before each `Main process exited`. Typical: environment incomplete → `./setup-backend.sh`; port taken → `sudo ss -lntp` |
| "Port … is already in use" during install | a `./start.sh` still runs in a terminal (Ctrl+C there) or another program uses the port (`--ui-port`) |
| Detection not resumed after reboot | status section "Resume after restart": `retrying` (camera unreachable - check the camera / network), `failed` (camera deleted or disabled - start it again in the UI) |
| Camera shows "NOT connected" | camera powered/reachable? (`ping <camera-ip>`); wrong password → edit the camera in the UI |
| No inference although connected | CPU overloaded (`top`), too many detections at once; `journalctl` for `ENGINE_FAILURE` |
| OpenVINO missing (aarch64) | normal on boxes with glibc < 2.35: PyTorch is used, slower but complete |
| `install-services.sh`: "not created by this installer" | an unrelated unit file with the same name exists - it is never overwritten; rename it |
| Disk full | delete old snapshots in the UI (Snapshots page) or under `backend/snapshots` |

---

## 10. Uninstall and rollback

**Uninstall the services** (keeps everything else):

```
scripts/debian/uninstall-services.sh                 # add --close-firewall to remove the ufw rule too
```

It stops and disables the two services and deletes only their two unit files (only if they were
created by `install-services.sh`). The project folder, `backend/.venv`, the models, `.env`, cameras,
alerts and snapshots are **not** touched. `./start.sh` keeps working, and `install-services.sh`
can install the services again at any time.

**Rollback to the previous behaviour** (no services, manual start): run `uninstall-services.sh`,
then start the application by hand with `./start.sh` as before.

**Rollback of a software update**: `git log --oneline` → `git checkout <previous-commit>` →
`scripts/debian/install-services.sh`. Data in `backend/data` stays compatible; `running_modules.json`
is ignored by older versions.

**Switch off only the automatic resume of detections**: `AIV_AUTOSTART=0` in `.env`, then
`scripts/debian/restart-services.sh backend`.

---

## 11. Camera streams and AI performance on the box (CPU only)

### What limits the frame rate (measured, not assumed)

Measured on a 2-core x86 test machine with 8 GB RAM (the AI box has more cores, so its absolute numbers are
higher; the relations are the same). Test cameras: 1080p and 4 MP main streams, 640x360 substreams, 20 fps,
real people in the picture.

| Cause | Measurement |
|---|---|
| **Fire & Smoke ran on PyTorch** | 95-102 ms per analysed frame alone, 200 ms next to two other cameras: only **3.8-7.1 fps** analysed |
| **Every camera decodes and analyses its full-resolution stream** | the model works at 640 px anyway; 3 cameras at 4 MP: decoding alone ~50% of a core, **9 fps** each |
| **All cameras share the CPU** | 1 camera: 22-25 ms per frame (all 20 fps analysed); 3 cameras: 70-100 ms each - no lock between cameras, the cores are simply full |
| **Human + Count on one camera = two person models per frame** | 143-179 ms per frame, **5-7 fps** |
| "~50% CPU" in `top` | OpenVINO / PyTorch use the physical cores; on a CPU with 2 threads per core that shows as ~50% while the AI is fully busy (`check-environment.sh` reports threads per core) |

### What was changed

1. **Fire & Smoke runs on OpenVINO** (`backend/models/fire_smoke/firedetect-11s_openvino_model`, same weights):
   30 ms instead of 102 ms per frame. Detections verified against PyTorch (272 of 275 frames identical, the
   other 3 differ only at confidence 0.300 vs 0.299; identical alerts in a real-time replay). `AIV_FIRE_ENGINE=pytorch`
   switches back.
2. **AI detection stream per camera** (camera form: *AI detection stream*): the AI can analyse the camera's
   substream while the main stream stays the viewing stream. *Substream* is only offered for URL patterns
   that are documented by the vendor (Hikvision `/Streaming/Channels/101` -> `102`, `/h264/ch1/main/av_stream` ->
   `sub`, Dahua `subtype=0` -> `1`); for any other camera enter the path under *Custom AI stream path*. Nothing
   is guessed. If the AI stream cannot be opened, detection uses the main stream and the status shows
   *Fallback* with the reason.
3. **Snapshots with a full-resolution original** (camera form: *Snapshot image*): with a separate AI stream
   the snapshot is the AI frame with boxes **plus** the newest main-stream frame as the original (the
   Snapshots page shows it). This keeps the main stream decoded while detection runs - see the cost below;
   choose *Boxes only* where CPU matters more. Head Count always captures from the main stream.
4. **Live view in full resolution**: `GET /api/cameras/<id>/stream.mjpeg?source=main` sends the main stream
   without boxes (the detection pages keep the annotated AI stream).
5. **Per-camera frame queue** (`AIV_FRAME_POLICY=queue`, default): every decoded frame enters a FIFO queue and is
   analysed in order - no frame is skipped while the AI keeps up, short bursts are absorbed. The queue holds at
   most `AIV_FRAME_QUEUE_SECONDS` (0.5 s) / `AIV_FRAME_QUEUE_MAX_MB` (96 MB) per camera. **Overload policy:** when a
   camera's AI cannot keep up for longer, the queue fills and the **oldest** frames are dropped and counted
   (*dropped (queue full)*); that camera then runs up to 0.5 s behind live. `AIV_FRAME_POLICY=latest` restores
   the original newest-frame behaviour (lowest delay, frames skipped instead). One camera's queue never blocks
   another camera.
6. **Measurements per camera** (Detection pages, `GET /api/system/metrics`, `scripts/debian/status-services.sh`):
   received fps, processed fps, inference time (average + latest), queue length / capacity / peak, latency from
   receiving a frame to the end of its analysis, frames dropped in the application (queue full) and frames
   missing in the camera stream itself (gaps in the stream's timestamps = camera / network side), the fire
   model's own analysed fps and skipped frames, the AI and main stream resolutions, and the backend process'
   CPU and RAM. The frame counters run since the camera was connected: frames dropped while a model loads and
   runs its first (slower) inference right after Start are included.

### Before / after (same test machine, same scenarios, measured back to back)

| Scenario (20 fps cameras) | Before | After, main stream | After, AI on substream |
|---|---|---|---|
| Fire & Smoke alone - fire frames analysed | 7.1 fps (102 ms) | **10.0 fps** (30 ms; = every 2nd frame, the configured maximum) | - |
| Fire next to 2 other cameras | 3.8 fps | 8.5 fps | **9.9 fps** |
| 3 cameras Human, 1080p - processed fps | 10.2 / 10.9 / 12.2 | 11.7 / 12.6 / 14.1 | **13.9 / 15.5 / 18.4** |
| 3 cameras Human, 4 MP | 8.7 / 9.0 / 9.4 | 9.2 / 9.4 / 9.8 | **14.0 / 15.7 / 18.7** |
| Human + Count camera (mixed load) | 5.4 | 6.9 | **9.3** |
| Backend RAM, 3 cameras 4 MP | 1557 MB | 1738 MB | **1029 MB** |
| 1 camera Human 1080p | 20 of 20 fps | 20 of 20 fps, 25 ms receive -> result | - |

Cost of *Boxes + full-resolution original* (main stream decoded as well): 3 cameras Human 1080p 13.7 instead of
15.9 fps on average (-14%); 4 MP 10.2 instead of 16.1 fps (-37%).

**When the cores are full** (3 cameras on 2 cores above) not every frame can be analysed - no setting changes
that; the metrics show it (*dropped (queue full)*, queue at capacity, *Bottleneck: AI inference (overloaded)*).
The AI box's sustainable rate is what `status-services.sh` / the Detection page report there.

### Recommended camera setup on the AI box

1. In the camera's own web page, enable its **substream** (e.g. Hikvision: Configuration -> Video -> Sub Stream):
   H.264, 640x360 or 704x576, the same frame rate as the main stream if detection should see every moment, and
   an **I-frame (keyframe) interval of 1-2 s** (e.g. 25-50 frames at 25 fps). A new connection can only decode
   from the next keyframe: with a 10 s interval, starting a detection or reconnecting can exceed the camera's
   connection timeout (seen with a test video that had a keyframe every 10 s).
2. In PYROTECH VISION edit the camera: *AI detection stream* = *Camera substream* (Hikvision/Dahua URLs) or
   *Custom AI stream path* (e.g. `/Streaming/Channels/102`). Save; running detections restart on the substream.
3. *Snapshot image*: *Boxes + full-resolution original* only where the high-resolution picture is needed.
4. Check the Detection page: *AI stream* = Substream 640x360, *processed fps* close to *received fps*,
   *dropped (queue full)* not growing. If it grows, the box is overloaded: fewer modules per camera (Human and
   Count both run a person model), fewer cameras, or *Boxes only* snapshots.

### Remaining limits

* **Human Detection + Human Count on the same camera** still run two person models per frame (different
  tracking and thresholds); combining them would change the detection logic, so it was not done.
* The CPU is shared: adding cameras lowers each camera's processed fps once the cores are full.
* Object Monitoring's own Layer 1 runs on the AI stream; on a low-resolution substream small objects are matched
  with fewer pixels.
* The optional far-person pass (Human Detection, *Person (far)*) upscales part of the frame; on a substream it
  has less detail to work with - keep the main stream as AI stream for cameras that need it.

## 12. Validation performed

Tested on a test box: a systemd container (systemd 255, Ubuntu 24.04 userland, x86_64), project at
`/home/aibox/ui-system/pyrotech-vision` owned by a normal user `aibox`, installed with
`install-services.sh` run as that user with sudo; "another PC" = the host machine over a virtual
network link (box address 10.77.0.2). Results:

| # | Test | Result |
|---|---|---|
| 1 | bash syntax (`bash -n`) and `shellcheck` of all scripts | passed |
| 2 | generated unit files: `systemd-analyze verify` | passed |
| 3 | referenced files / executables exist (installer checks) | passed |
| 4 | start commands = the repository's own (`backend/main.py`, `server.js`) | yes |
| 5 | frontend build | not applicable (no build step) |
| 6 | backend starts under systemd as the normal user | passed |
| 7 | ports: UI `0.0.0.0:5173`, backend `127.0.0.1:8000`; port 80 refused by the installer | passed |
| 8 | start / stop / restart; stop is clean (`shutting down`, exit status 0) | passed |
| 9 | logs in the journal (`journalctl -u …`) | passed |
| 10 | enabled for boot; **full reboot of the test box**: both services up, the 2 running detections resumed ~8 s after start, UI reachable from the other PC - no manual step | passed |
| 11 | from the other PC: UI 200, `/api` 200, **port 8000 refused**, `.env` and `credentials.json` not served (404) | passed |
| 12 | API requests use the relative `/api` path through the UI | passed |
| 13 | `kill -9` of the backend → restarted after 10 s, counted, logged; `kill -9` of the UI → restarted after 5 s | passed |
| 14 | no duplicate processes: one backend, one UI; `./start.sh` refuses while the services run | passed |
| 15 | uninstall: units removed; `backend/data`, `.venv` (6.1 GB), models unchanged | passed |
| - | camera offline at start: retried 10 s / 20 s / 40 s, resumed by itself when it came back | passed |
| - | ufw active: installer warns; `--open-firewall` opens only 5173/tcp; then reachable | passed |
| - | an unrelated unit file with the same name is not overwritten | passed |
| - | unit tests of the backend incl. the new resume logic (54 tests) | passed |

Stream / performance changes (§11), tested on the 2-core test machine with test RTSP cameras (1080p and 4 MP main
streams, 640x360 substreams, the fire test video): before/after measurements in §11; API test (21 checks: AI on the
substream, fallback to the main stream with reason, no guessed substream URL, HD snapshot original 1920x1080,
annotated live stream 640 px vs `?source=main`, Head Count from the main stream, fire on OpenVINO with alert, per-camera
metrics, process CPU/RAM) and UI test (15 checks: camera form fields save, status rows, fire model fps, snapshot
original link, pages without console errors) passed; 58 backend unit tests passed.

**Not tested** (needs the real box): Debian itself and ARM64 hardware, your cameras' substreams, a physical power cut,
access from a real second PC on your LAN, your real cameras, and the Debian 12 path that fetches
Python 3.13. On the box, run `scripts/debian/check-environment.sh` and `status-services.sh` after
installing, then reboot once (`sudo reboot`) and run `status-services.sh` again - the box is never
rebooted automatically by these scripts.
