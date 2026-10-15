# PYROTECH VISION on Google Colab - live RTSP detection from a camera on your LAN

## Architecture

```
 Camera LAN (private)                     Tailscale VPN (WireGuard, encrypted)          Google Colab VM
 ┌──────────────┐  RTSP/TCP  ┌───────────────────────────┐        ┌───────────────────────────────────────────┐
 │ IP camera    │◄───────────│ YOUR PC: local_relay.py   │◄───────│ tailscaled (userspace)                    │
 │ 192.168.x.y  │ camera     │ MediaMTX relay            │ RTSP   │ socat 127.0.0.1:18554 ─► tailscale nc     │
 │ (own login)  │ password   │ listens ONLY on 100.x.y.z │ relay  │ backend 127.0.0.1:8000 (GPU / CPU)        │
 └──────────────┘ stays here │ read-only user + random pw│ login  │ web UI  127.0.0.1:5173  (server.js → /api)│
                             └───────────────────────────┘        └──────────────────┬────────────────────────┘
                                                                                     │ Colab's own proxy
                                                                     your browser ◄──┘ (your Google login only)
```

* **The camera is never exposed.** Its address and password stay on your PC (MediaMTX config in your home folder,
  owner-only). No router port is opened.
* **The relay is reachable only inside your Tailscale network:** it listens on the PC's 100.x Tailscale address
  only, accepts only Tailscale source addresses, RTSP over TCP only, one read-only user with a random password
  (all other MediaMTX servers - RTMP, HLS, WebRTC, SRT, API, metrics - are switched off).
* **Colab joins your tailnet** with an ephemeral auth key (userspace mode: Colab has no TUN device), removed again
  on `stop`. A local bridge port `127.0.0.1:18554` forwards to the relay through `tailscale nc`; the backend reads
  `rtsp://127.0.0.1:18554/cam1` with the relay user - unchanged backend code, its normal RTSP pipeline.
* **Backend and UI listen on 127.0.0.1 only.** The UI calls the relative `/api`, which `server.js` forwards to
  `127.0.0.1:8000/api`. You open the UI through Colab's authenticated proxy (`serve_kernel_port_as_window`):
  only the Google account running the notebook can open it. No public tunnel (ngrok etc.) is used.
* **GPU:** on a GPU runtime the setup installs CUDA PyTorch and the backend starts with `AIV_DEVICE=0`,
  `AIV_ENGINE=pytorch` (process environment, no file is changed) - Human, Vehicle and Human Count run on the GPU.
  Fire & Smoke and Object Monitoring call their models with `device="cpu"` in the existing code, so they stay on
  the CPU (OpenVINO); this was left unchanged. Without a GPU everything runs on the CPU as usual.

New files only: `scripts/colab/local_relay.py`, `scripts/colab/colab_launch.py`, `notebooks/AIVISION_Colab.ipynb`,
this document. No existing file was changed.

## 1. Once: Tailscale

1. Create a free Tailscale account, install Tailscale on **your PC on the camera LAN** (Windows: tailscale.com/download;
   Debian: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`) and log in.
2. Tailscale admin console → Settings → Keys → **Generate auth key**: *Ephemeral* on, *Reusable* off (one per Colab
   session), short expiry.
3. Colab → key icon (Secrets) → add `TS_AUTHKEY` = that key, notebook access on.

## 2. Your PC (local terminal)

Windows (PowerShell, in `D:\ui`) - Debian: same commands with `python3`:

```
python scripts\colab\local_relay.py setup    # camera RTSP URL (prefer the substream, e.g. .../Streaming/Channels/102),
                                             # camera user + password (hidden), downloads MediaMTX v1.9.3,
                                             # prints RELAY_HOST / RELAY_PASSWORD for Colab
python scripts\colab\local_relay.py start    # keep this window open while Colab runs (Ctrl+C stops the relay)
python scripts\colab\local_relay.py test     # in a 2nd window: relay -> camera check (must say OK)
python scripts\colab\local_relay.py show     # print the Colab values again
```

Add `RELAY_PASSWORD` (from `setup`/`show`) as a second Colab secret.

## 3. Colab notebook

Upload `notebooks/AIVISION_Colab.ipynb` (or open it from GitHub in Colab), choose *Runtime → Change runtime type →
GPU* if you want the GPU, then run the cells in order:

| Cell | Command | What it checks / does |
|---|---|---|
| 1 | `colab_launch.py check` | project files, Python, GPU (`nvidia-smi`), Node.js, disk, ports already used (no duplicates) |
| 2 | relay settings | `RELAY_HOST` (your PC's 100.x address), paths; secrets read from Colab Secrets |
| 3 | `install` | `./setup-backend.sh` (CUDA PyTorch on GPU runtimes, self-check), Tailscale, socat |
| 4 | `vpn` | starts `tailscaled`, joins with `TS_AUTHKEY`, `tailscale ping` to your PC |
| 5 | `bridge` + `test-camera` | bridge port up; RTSP DESCRIBE with the relay login; one frame decoded by the backend's OpenCV |
| 6 | `start` + `add-camera` | backend (GPU env when available) + web UI, health checks through the UI's `/api`; camera(s) saved with the relay login (stored by the backend's credential store, never in the URL) and connection-tested |
| 7 | open UI | Colab proxy window for port 5173 |
| 8 | (optional) | starts Human Detection on the first camera |
| 9 | `status` | services, **GPU verification** (backend PID in `nvidia-smi` with its memory), per-camera fps / model ms |
| 10 | `stop` | UI, backend (Ctrl+C semantics - sessions closed, data saved), bridge, `tailscale logout`, `tailscaled` |

Logs: `/content/aivision-run/logs/{setup,tailscaled,bridge,backend,frontend}.log`. Secrets are removed from the
environment of the backend and UI processes and masked in error messages.

## Verified / not verified

Verified here (x86 Linux test machine, no GPU, no Tailscale account): relay config with MediaMTX v1.9.3 - only the
Tailscale-bound RTSP port open, wrong password → 401, client outside the Tailscale range → 401 even with the right password, right password
(IP restriction lifted for the test) → camera stream; `check`, `test-camera` (frame 1920x1080 through bridge + relay), `start` (backend + UI, `/api`
through the UI), duplicate `start` refused, `add-camera` (connection test OK), Human Detection running on the relayed
stream (19.9 of 19.9 fps), `status`, `stop` (ports free). The bridge was emulated with socat → relay directly.

**Not verified:** Tailscale itself (`vpn`, `tailscale nc` bridge), Google Colab, the GPU path (CUDA PyTorch,
`AIV_DEVICE=0`), your camera and your PC. `status` reports whether the backend really holds GPU memory - trust that
line, not the runtime type.

## Troubleshooting

| Message | Cause / fix |
|---|---|
| `Tailscale is not connected on this computer` | open Tailscale on the PC and log in |
| relay `test`: `could not open the camera (400/404)` | camera URL / password wrong, or camera not reachable from the PC - see the `start` window |
| `tailscale up failed` | auth key expired / already used - create a new ephemeral key |
| `test-camera`: `no answer through the bridge` | relay `start` not running, PC asleep, or `RELAY_HOST` wrong (`local_relay.py show`) |
| `test-camera`: `RTSP 401` | `RELAY_PASSWORD` secret differs from `local_relay.py show` |
| `RTSP answered but no frame` | H.265 camera stream or very long keyframe interval - use an H.264 substream, keyframe 1-2 s |
| `port ... used by ANOTHER process` | a backend/UI started by hand in the notebook - stop it (`!pkill -INT -f backend/main.py`) |
| `status`: GPU present but no GPU memory | no Human/Vehicle/Count detection running yet, or `install` ran on a CPU runtime (after switching to GPU: `!AIV_TORCH=cuda bash setup-backend.sh --reinstall`) |

Limits: the stream crosses your PC's upload bandwidth (a 640x360 substream needs ~0.5-1 Mbit/s, a 1080p main stream
2-6 Mbit/s); detection latency adds the internet round trip; Colab sessions end after idle time / ~12 h and the VM is
wiped (cameras, snapshots, `.venv` are not kept unless you work from a mounted Google Drive folder).
