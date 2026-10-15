#!/usr/bin/env python3
"""PYROTECH VISION on Google Colab - checks, install, VPN bridge to the camera relay, start/stop of
backend + frontend, camera registration, GPU verification. Used by notebooks/AIVISION_Colab.ipynb.

    python scripts/colab/colab_launch.py check            Python, GPU, Node.js, disk, ports, project files
    python scripts/colab/colab_launch.py install          backend/.venv via ./setup-backend.sh (CUDA PyTorch when a GPU exists),
                                                          Tailscale, socat
    python scripts/colab/colab_launch.py vpn              join your Tailscale network (TS_AUTHKEY from the environment)
    python scripts/colab/colab_launch.py bridge           127.0.0.1:18554 in Colab -> relay on your PC over Tailscale
    python scripts/colab/colab_launch.py test-camera      RTSP DESCRIBE + one decoded frame through the bridge
    python scripts/colab/colab_launch.py start            backend (GPU if available) + frontend, health checks
    python scripts/colab/colab_launch.py add-camera       register relay path(s) as camera(s) (password from the environment)
    python scripts/colab/colab_launch.py status           services, GPU use by the backend, cameras, detections
    python scripts/colab/colab_launch.py stop             clean shutdown of everything this script started

Settings come from environment variables (set them in the notebook - never write them into files):
    TS_AUTHKEY      Tailscale auth key (use an EPHEMERAL, single-use key)          vpn
    RELAY_HOST      Tailscale address of your PC running local_relay.py (100.x.y.z) bridge / test-camera / add-camera
    RELAY_PORT      8554                                                            bridge
    RELAY_USER      aivision                                                        test-camera / add-camera
    RELAY_PASSWORD  printed by local_relay.py setup (store it as a Colab secret)    test-camera / add-camera
    RELAY_PATHS     cam1[,cam2...]                                                  test-camera / add-camera
    AIV_GPU         auto | on | off   (default auto: use the GPU when nvidia-smi shows one)

Nothing is exposed to the internet: the backend listens on 127.0.0.1:8000, the web UI on 127.0.0.1:5173 and is opened
through Colab's own authenticated proxy (only your Google account), the camera stream arrives over Tailscale.
"""
import base64
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RUN = os.environ.get("AIV_COLAB_RUN", "/content/aivision-run")
LOGS = os.path.join(RUN, "logs")
VENV_PY = os.path.join(REPO, "backend", ".venv", "bin", "python")
TS_SOCK = os.path.join(RUN, "tailscaled.sock")
API_PORT, UI_PORT, BRIDGE_PORT = 8000, 5173, 18554
API = f"http://127.0.0.1:{API_PORT}/api"


def ts():
    return time.strftime("%H:%M:%S")


def log(msg):
    print(f"[{ts()}] {msg}", flush=True)


def ok(msg):
    print(f"[{ts()}] OK    {msg}", flush=True)


def warn(msg):
    print(f"[{ts()}] WARN  {msg}", flush=True)


def fail(msg):
    print(f"[{ts()}] ERROR {msg}", flush=True)
    sys.exit(1)


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, **kw)


def port_open(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def pidfile(name):
    return os.path.join(RUN, f"{name}.pid")


def alive(name):
    try:
        pid = int(open(pidfile(name)).read())
        os.kill(pid, 0)
        with open(f"/proc/{pid}/status") as f:
            if any(l.startswith("State:") and "Z" in l.split()[1] for l in f):
                return None
        return pid
    except (OSError, ValueError):
        return None


def spawn(name, cmd, env=None, cwd=None):
    """Start a background service once (no duplicates); output to logs/<name>.log."""
    os.makedirs(LOGS, exist_ok=True)
    pid = alive(name)
    if pid:
        ok(f"{name} already running (PID {pid}) - not started twice")
        return pid
    lf = open(os.path.join(LOGS, f"{name}.log"), "a")
    p = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, cwd=cwd,
                         start_new_session=True)
    with open(pidfile(name), "w") as f:
        f.write(str(p.pid))
    return p.pid


def stop_proc(name, sig=signal.SIGINT, wait=30):
    pid = alive(name)
    if not pid:
        return
    try:
        os.killpg(pid, sig)
    except OSError:
        pass
    end = time.time() + wait
    while time.time() < end and alive(name):
        time.sleep(0.5)
    if alive(name):
        warn(f"{name} did not stop in {wait} s - SIGKILL")
        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.remove(pidfile(name))
    except OSError:
        pass
    ok(f"{name} stopped")


def tail(name, n=15):
    try:
        with open(os.path.join(LOGS, f"{name}.log"), errors="replace") as f:
            return "".join(f.readlines()[-n:])
    except OSError:
        return "(no log)"


def api(path, method="GET", body=None, timeout=10):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None
    except (urllib.error.URLError, OSError, ValueError):
        return 0, None


def gpu():
    """(present, description)"""
    if not shutil.which("nvidia-smi"):
        return False, "no nvidia-smi (CPU runtime)"
    r = run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version", "--format=csv,noheader"])
    if r.returncode != 0 or not r.stdout.strip():
        return False, "nvidia-smi found but no GPU answered"
    return True, r.stdout.strip().splitlines()[0]


def need_env(*names):
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        fail("missing setting(s): " + ", ".join(missing) + " - set them in the notebook cell 'Relay settings'")


# ============================================================================ commands
def cmd_check():
    bad = 0
    log(f"project: {REPO}")
    for f in ("backend/main.py", "server.js", "setup-backend.sh", "backend/requirements-lock.txt", "backend/models/yolov8n.pt"):
        if os.path.exists(os.path.join(REPO, f)):
            ok(f)
        else:
            warn(f"{f} missing")
            bad += 1
    ok(f"system Python {sys.version.split()[0]} (setup-backend.sh fetches 3.13 itself when needed)")
    has, desc = gpu()
    (ok if has else warn)(f"GPU: {desc}" + ("" if has else " - detection will run on the CPU. Runtime > Change runtime type > GPU"))
    node = shutil.which("node")
    if node:
        v = run([node, "-v"]).stdout.strip()
        (ok if int(v.lstrip("v").split(".")[0]) >= 16 else warn)(f"Node.js {v}")
    else:
        warn("Node.js missing - setup-backend.sh installs it")
    du = shutil.disk_usage("/content" if os.path.isdir("/content") else REPO)
    (ok if du.free > 8e9 else warn)(f"free disk {du.free / 1e9:.0f} GB (the environment needs ~5 GB with CUDA PyTorch)")
    if os.path.exists(VENV_PY):
        ok("backend/.venv present")
    else:
        warn("backend/.venv not installed yet - run: install")
    for port, name in ((API_PORT, "backend"), (UI_PORT, "frontend"), (BRIDGE_PORT, "bridge")):
        if port_open(port):
            (ok if alive(name) else warn)(f"port {port} in use" + (f" by our {name}" if alive(name) else " by ANOTHER process - stop it first"))
    for tool in ("tailscale", "tailscaled", "socat", "ffprobe"):
        (ok if shutil.which(tool) else warn)(f"{tool} {'found' if shutil.which(tool) else 'missing (install installs it)'}")
    return bad


def cmd_install():
    os.makedirs(LOGS, exist_ok=True)
    has, desc = gpu()
    env = dict(os.environ, AIV_TORCH="cuda" if has and os.environ.get("AIV_GPU", "auto") != "off" else "cpu")
    log(f"backend environment ({env['AIV_TORCH']} PyTorch) - first time 5-10 min, log: {LOGS}/setup.log")
    with open(os.path.join(LOGS, "setup.log"), "a") as lf:
        r = subprocess.run(["bash", "setup-backend.sh"], cwd=REPO, env=env, stdout=lf, stderr=subprocess.STDOUT)
    if r.returncode != 0:
        print(tail("setup", 30))
        fail("setup-backend.sh failed - see the lines above / logs/setup.log")
    ok("backend/.venv ready")
    pkgs = [p for p, t in (("socat", "socat"), ("ffmpeg", "ffprobe")) if not shutil.which(t)]
    if pkgs:
        r = run(["bash", "-c", f"apt-get -qq update && apt-get -qq install -y {' '.join(pkgs)}"])
        if r.returncode != 0:
            fail(f"apt-get install {' '.join(pkgs)} failed: {r.stderr[-400:]}")
    ok("socat / ffprobe present")
    if not shutil.which("tailscale"):
        log("installing Tailscale (official install script)")
        r = run(["bash", "-c", "curl -fsSL https://tailscale.com/install.sh | sh"])
        if not shutil.which("tailscale"):
            fail("Tailscale install failed: " + (r.stderr or r.stdout)[-400:])
    ok(f"Tailscale {run(['tailscale', 'version']).stdout.splitlines()[0]}")
    v = run([VENV_PY, "-c", "import torch,sys;print(torch.__version__, torch.cuda.is_available())"])
    ok(f"PyTorch in backend/.venv: {v.stdout.strip()} (version, CUDA usable)")


def ts_cli(*args, timeout=60):
    return run(["tailscale", f"--socket={TS_SOCK}", *args], timeout=timeout)


def cmd_vpn():
    need_env("TS_AUTHKEY")
    if not shutil.which("tailscaled"):
        fail("Tailscale not installed - run: install")
    os.makedirs(RUN, exist_ok=True)
    # Colab has no TUN device: userspace networking; state in memory = the node disappears with the VM
    spawn("tailscaled", ["tailscaled", "--tun=userspace-networking", "--state=mem:", f"--socket={TS_SOCK}",
                         "--socks5-server=127.0.0.1:1055"])
    for _ in range(30):
        if os.path.exists(TS_SOCK):
            break
        time.sleep(0.5)
    r = ts_cli("up", f"--authkey={os.environ['TS_AUTHKEY']}", "--hostname=aivision-colab", "--accept-dns=false", timeout=90)
    if r.returncode != 0:
        print(tail("tailscaled"))
        fail("tailscale up failed: " + (r.stderr or r.stdout).replace(os.environ["TS_AUTHKEY"], "***")[-400:])
    ip = ts_cli("ip", "-4").stdout.strip()
    ok(f"joined the Tailscale network as aivision-colab ({ip})")
    if os.environ.get("RELAY_HOST"):
        r = ts_cli("ping", "--c=3", "--timeout=5s", os.environ["RELAY_HOST"], timeout=30)
        (ok if r.returncode == 0 else warn)(f"tailscale ping {os.environ['RELAY_HOST']}: "
                                            f"{(r.stdout.strip().splitlines() or ['no answer'])[-1]}")


def cmd_bridge():
    need_env("RELAY_HOST")
    port = os.environ.get("RELAY_PORT", "8554")
    if not alive("tailscaled"):
        fail("VPN not running - run: vpn")
    if port_open(BRIDGE_PORT) and not alive("bridge"):
        fail(f"port {BRIDGE_PORT} is used by another process")
    # local TCP port -> tailscaled's SOCKS5 proxy -> relay on your PC (RTSP over TCP; the backend forces TCP)
    stop_proc("bridge", signal.SIGTERM, 5)          # restart with the current RELAY_HOST / RELAY_PORT
    spawn("bridge", [sys.executable, os.path.join(REPO, "scripts", "colab", "socks_bridge.py"),
                     str(BRIDGE_PORT), os.environ["RELAY_HOST"], port, "1055"])
    time.sleep(1.5)
    if not port_open(BRIDGE_PORT):
        print(tail("bridge"))
        fail("bridge did not start")
    ok(f"bridge 127.0.0.1:{BRIDGE_PORT} -> {os.environ['RELAY_HOST']}:{port} (Tailscale)")


def describe(path, user, pw, timeout=25):
    req = (f"DESCRIBE rtsp://127.0.0.1:{BRIDGE_PORT}/{path} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n"
           f"Authorization: Basic {base64.b64encode(f'{user}:{pw}'.encode()).decode()}\r\n\r\n")
    with socket.create_connection(("127.0.0.1", BRIDGE_PORT), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            c = s.recv(4096)
            if not c:
                break
            data += c
    head = data.split(b"\r\n\r\n", 1)[0].decode(errors="replace")
    return int(head.split()[1]) if head.startswith("RTSP/") else 0


def cmd_test_camera():
    need_env("RELAY_PASSWORD", "RELAY_PATHS")
    user, pw = os.environ.get("RELAY_USER", "aivision"), os.environ["RELAY_PASSWORD"]
    if not port_open(BRIDGE_PORT):
        fail("bridge not running - run: bridge")
    bad = 0
    for path in [p.strip() for p in os.environ["RELAY_PATHS"].split(",") if p.strip()]:
        try:
            code = describe(path, user, pw)
        except OSError as e:
            warn(f"{path}: no answer through the bridge ({e}) - is local_relay.py start running on your PC?")
            bad += 1
            continue
        if code != 200:
            hint = {401: "relay password wrong", 404: "path unknown or the relay cannot open the camera",
                    400: "relay cannot open the camera", 0: "no RTSP answer (relay not running?)"}.get(code, "")
            warn(f"{path}: RTSP {code} {hint}")
            bad += 1
            continue
        # one decoded frame with the backend's own OpenCV/FFmpeg (RTSP over TCP), password only in this process
        code_ = ("import cv2,os,sys,time\nos.environ['OPENCV_FFMPEG_CAPTURE_OPTIONS']='rtsp_transport;tcp'\n"
                 "c=cv2.VideoCapture(os.environ['T_URL'],cv2.CAP_FFMPEG);t=time.time();ok=False\n"
                 "while time.time()-t<20 and not ok: ok,f=c.read()\n"
                 "print(f'{f.shape[1]}x{f.shape[0]} {c.get(cv2.CAP_PROP_FPS):.0f}fps' if ok else 'NOFRAME')")
        url = f"rtsp://{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(pw, safe='')}@127.0.0.1:{BRIDGE_PORT}/{path}"
        r = run([VENV_PY, "-c", code_], env=dict(os.environ, T_URL=url), timeout=60)
        out = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "NOFRAME"
        if out == "NOFRAME":
            warn(f"{path}: RTSP answered but no frame was decoded in 20 s (camera keyframe interval? codec H.265?)")
            bad += 1
        else:
            ok(f"{path}: frame decoded through bridge + relay ({out})")
    if bad:
        fail(f"{bad} camera path(s) failed")


def cmd_start():
    if not os.path.exists(VENV_PY):
        fail("backend/.venv missing - run: install")
    for port, name in ((API_PORT, "backend"), (UI_PORT, "frontend")):
        if port_open(port) and not alive(name):
            fail(f"port {port} is already used by another process (a backend/UI started by hand?) - stop it first")
    has, desc = gpu()
    use_gpu = has and os.environ.get("AIV_GPU", "auto") != "off"
    env = dict(os.environ, AIV_HOST="127.0.0.1", AIV_PORT=str(API_PORT), PYTHONUNBUFFERED="1")
    for k in ("TS_AUTHKEY", "RELAY_PASSWORD"):        # secrets never reach the services
        env.pop(k, None)
    if use_gpu:
        # explicit CUDA device -> the person/vehicle/count model runs on PyTorch CUDA (config.resolve_engine)
        env.update(AIV_DEVICE="0", AIV_ENGINE="pytorch")
    spawn("backend", [VENV_PY, "backend/main.py"], env=env, cwd=REPO)
    for _ in range(60):
        st, h = api("/system/health", timeout=3)
        if st == 200:
            break
        if not alive("backend"):
            print(tail("backend", 30))
            fail("backend exited during start-up (log above)")
        time.sleep(1)
    else:
        print(tail("backend", 30))
        fail("backend did not answer within 60 s")
    ok(f"backend http://127.0.0.1:{API_PORT}/api - engine {h.get('engine')}, "
       f"device {'CUDA GPU (' + desc + ')' if use_gpu else 'CPU'} requested")
    fenv = dict(env, HOST="127.0.0.1", PORT=str(UI_PORT), BACKEND_URL=f"http://127.0.0.1:{API_PORT}/api")
    spawn("frontend", ["node", "server.js"], env=fenv, cwd=REPO)
    for _ in range(20):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{UI_PORT}/api/system/health", timeout=3) as r:
                if r.status == 200:
                    break
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    else:
        print(tail("frontend"))
        fail("web UI did not answer (or could not reach the backend through /api)")
    ok(f"web UI http://127.0.0.1:{UI_PORT} (its /api reaches the backend) - open it with the notebook's 'Open UI' cell")


def cmd_add_camera():
    need_env("RELAY_PASSWORD", "RELAY_PATHS")
    user = os.environ.get("RELAY_USER", "aivision")
    for i, path in enumerate([p.strip() for p in os.environ["RELAY_PATHS"].split(",") if p.strip()], 1):
        cid = f"COLAB-{path.upper()}"[:32]
        body = {"id": cid, "name": f"{path} (via relay)", "type": "rtsp", "sourceUrl": f"rtsp://127.0.0.1:{BRIDGE_PORT}/{path}",
                "requiresAuth": True, "username": user, "password": os.environ["RELAY_PASSWORD"], "enabled": True,
                "timeoutSec": 20, "streamProfile": "main", "zones": [], "detection": {"modules": {}}}
        st, r = api("/cameras", "POST", body, timeout=30)
        if st == 409 or (st and st >= 400 and "exists" in str(r).lower()):
            st, r = api(f"/cameras/{cid}", "PUT", body, timeout=30)
        if st not in (200, 201):
            fail(f"{cid}: camera not saved ({st} {(r or {}).get('message')})")
        st, r = api(f"/cameras/{cid}/test-connection", "POST", {}, timeout=60)
        good = st == 200 and (r or {}).get("ok")
        (ok if good else warn)(f"{cid}: saved; connection test {'OK ' + str((r or {}).get('width')) + 'x' + str((r or {}).get('height')) if good else 'FAILED: ' + str((r or {}).get('message'))}")
    log("start Human / Vehicle / ... detection for these cameras in the web UI (or the notebook cell)")


def backend_gpu_use():
    """Backend PID present in nvidia-smi's compute apps = the backend really uses the GPU."""
    pid = alive("backend")
    if not pid or not shutil.which("nvidia-smi"):
        return None
    r = run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"])
    for line in r.stdout.splitlines():
        p, mem = [x.strip() for x in line.split(",")[:2]]
        if p.isdigit() and int(p) == pid:
            return int(mem)
    return 0


def cmd_status():
    for name in ("tailscaled", "bridge", "backend", "frontend"):
        pid = alive(name)
        (ok if pid else warn)(f"{name}: {'running, PID ' + str(pid) if pid else 'not running'}")
    st, m = api("/system/metrics", timeout=5)
    if st != 200:
        warn("backend API not answering")
        return
    st, rt = api("/system/runtime", timeout=5)
    ok(f"engine {rt.get('engine') if rt else '?'}, backend CPU {m.get('processCpu')}%, RSS {m.get('processRssMb')} MB")
    mem = backend_gpu_use()
    if mem is None:
        log("GPU: none in this runtime - detection runs on the CPU")
    elif mem > 0:
        ok(f"GPU VERIFIED: the backend process holds {mem} MiB on the GPU (nvidia-smi)")
    else:
        warn("GPU present but the backend holds no GPU memory - no PyTorch model on the GPU yet (start a Human/Vehicle "
             "detection) or the CUDA build is missing (install log)")
    for s in m.get("sessions", []):
        stg = s.get("stageMs") or {}
        log(f"{s['cameraId']}: {s.get('state')} {','.join(s.get('modules') or [])} - received {s.get('receivedFps')} fps, "
            f"analysed {s.get('processedFps')} fps, model {stg.get('inference')} ms, AI {s.get('aiResolution')}")
    if not m.get("sessions"):
        log("no camera session running")


def cmd_stop():
    st, _ = api("/system/health", timeout=2)
    stop_proc("frontend", signal.SIGTERM, 10)
    stop_proc("backend", signal.SIGINT, 45)        # same clean shutdown as Ctrl+C (sessions closed, data saved)
    stop_proc("bridge", signal.SIGTERM, 5)
    if alive("tailscaled"):
        ts_cli("logout", timeout=20)                # removes this Colab node from your tailnet
        stop_proc("tailscaled", signal.SIGTERM, 10)
    ok("all services of this notebook stopped")


COMMANDS = {"check": cmd_check, "install": cmd_install, "vpn": cmd_vpn, "bridge": cmd_bridge, "test-camera": cmd_test_camera,
            "start": cmd_start, "add-camera": cmd_add_camera, "status": cmd_status, "stop": cmd_stop}

if __name__ == "__main__":
    if len(sys.argv) != 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(2)
    os.makedirs(RUN, exist_ok=True)
    r = COMMANDS[sys.argv[1]]()
    sys.exit(1 if (sys.argv[1] == "check" and r) else 0)
