#!/usr/bin/env python3
"""PYROTECH VISION - camera relay on YOUR computer (the one on the camera LAN) for the Google Colab backend.

The IP camera keeps its private address and its own password. This script runs MediaMTX on your computer:
MediaMTX pulls the camera's RTSP stream on the LAN and re-serves it ONLY on this computer's Tailscale
(VPN) address, to ONE read-only relay user with a random password. Colab joins the same Tailscale
network and reads the relay - never the camera.

    python scripts/colab/local_relay.py setup     once: camera URL + password (asked, not stored in the project),
                                                  downloads MediaMTX v1.9.3, writes the relay config
    python scripts/colab/local_relay.py add       add cameras later (keeps the relay password)
    python scripts/colab/local_relay.py start     run the relay (keep this window open; Ctrl+C stops it)
    python scripts/colab/local_relay.py test      check the relay -> camera chain (RTSP DESCRIBE, like Colab will)
    python scripts/colab/local_relay.py setup --userspace   the same when Tailscale runs WITHOUT a TUN device
    python scripts/colab/local_relay.py show      print what to enter in Colab (relay address, path, user, password)

Needs: Python 3.8+, Tailscale installed and logged in on this computer (https://tailscale.com/download).
Files: %USERPROFILE%\\.pyrotech-relay (Windows) or ~/.pyrotech-relay (Linux) - outside the project, owner-only.
"""
import argparse
import base64
import getpass
import io
import json
import os
import platform
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request
import zipfile

MTX_VERSION = "v1.9.3"          # config keys below are for this version
HOME = os.environ.get("PYROTECH_RELAY_DIR") or os.path.join(os.path.expanduser("~"), ".pyrotech-relay")
CONF = os.path.join(HOME, "mediamtx.yml")
INFO = os.path.join(HOME, "relay.json")
RELAY_PORT = 8554
RELAY_USER = "aivision"


def die(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def private(path):
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def tailscale_ip():
    exe = shutil.which("tailscale") or (r"C:\Program Files\Tailscale\tailscale.exe"
                                        if os.path.exists(r"C:\Program Files\Tailscale\tailscale.exe") else None)
    if not exe:
        die("Tailscale is not installed. Install it from https://tailscale.com/download and log in, then run again.")
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as e:
        die(f"could not run tailscale: {e}")
    ip = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    if not ip.startswith("100."):
        die("Tailscale is not connected on this computer ('tailscale ip -4' gave no 100.x address). "
            "Open Tailscale and log in, then run again.")
    return ip


def mtx_binary():
    return os.path.join(HOME, "mediamtx.exe" if os.name == "nt" else "mediamtx")


def download_mediamtx():
    exe = mtx_binary()
    if os.path.exists(exe):
        return exe
    m = platform.machine().lower()
    if os.name == "nt":
        asset = f"mediamtx_{MTX_VERSION}_windows_amd64.zip"
    elif m in ("x86_64", "amd64"):
        asset = f"mediamtx_{MTX_VERSION}_linux_amd64.tar.gz"
    elif m in ("aarch64", "arm64"):
        asset = f"mediamtx_{MTX_VERSION}_linux_arm64v8.tar.gz"
    else:
        die(f"no MediaMTX download for {platform.system()} {m} - put the binary at {exe} yourself")
    url = f"https://github.com/bluenviron/mediamtx/releases/download/{MTX_VERSION}/{asset}"
    print(f"downloading {url}")
    try:
        data = urllib.request.urlopen(url, timeout=120).read()
    except Exception as e:  # noqa: BLE001
        die(f"download failed ({e}). Download {asset} by hand and put the mediamtx binary at {exe}")
    name = "mediamtx.exe" if os.name == "nt" else "mediamtx"
    if asset.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            with open(exe, "wb") as f:
                f.write(z.read(name))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as t:
            member = t.getmember(name)
            with open(exe, "wb") as f:
                f.write(t.extractfile(member).read())
        os.chmod(exe, 0o700)
    return exe


def write_config(listen_ip, cams, read_pass, userspace=False):
    """cams: [(path, source_url_with_credentials)]. Only RTSP over TCP on the Tailscale address; everything else off."""
    q = lambda s: json.dumps(s)          # YAML accepts JSON strings: safe quoting of passwords / URLs
    lines = [
        "# PYROTECH VISION camera relay - written by scripts/colab/local_relay.py. Contains the camera password: keep private.",
        "logLevel: info",
        "logDestinations: [stdout]",
        "readTimeout: 10s",
        "writeTimeout: 10s",
        "authMethod: internal",
        "authInternalUsers:",
        f"- user: {q(RELAY_USER)}",
        f"  pass: {q(read_pass)}",
        ("  ips: ['127.0.0.1/32']   # userspace Tailscale delivers tailnet connections from 127.0.0.1"
         if userspace else "  ips: ['100.64.0.0/10', 'fd7a:115c:a1e0::/48']   # Tailscale addresses only"),
        "  permissions:",
        "  - action: read",
        "    path:",
        "api: no", "metrics: no", "pprof: no", "playback: no",
        "rtsp: yes",
        "protocols: [tcp]",
        "encryption: \"no\"",
        f"rtspAddress: {'127.0.0.1' if userspace else listen_ip}:{RELAY_PORT}",
        "rtspAuthMethods: [basic]",
        "rtmp: no", "hls: no", "webrtc: no", "srt: no",
        "pathDefaults:",
        "  sourceOnDemand: yes",
        "  sourceOnDemandStartTimeout: 15s",
        "  sourceOnDemandCloseAfter: 15s",
        "  rtspTransport: tcp",
        "paths:",
    ]
    for path, src in cams:
        lines += [f"  {path}:", f"    source: {q(src)}"]
    with open(CONF, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    private(CONF)


def ask_cameras(existing_paths):
    """Ask for cameras until an empty URL. Returns [(path, source URL with credentials)]."""
    cams = []
    taken = set(existing_paths)
    while True:
        n = len(taken) + len(cams) + 1
        url = input(f"Camera {n} RTSP URL WITHOUT password, e.g. rtsp://192.168.0.64:554/Streaming/Channels/102 "
                    f"(empty = done): ").strip()
        if not url:
            break
        p = urllib.parse.urlsplit(url)
        if p.scheme not in ("rtsp", "rtsps") or not p.hostname:
            print("  not an rtsp:// URL - try again")
            continue
        url_user = urllib.parse.unquote(p.username) if p.username else ""
        url_pw = urllib.parse.unquote(p.password) if p.password else ""
        user = input(f"  camera user name{' [' + url_user + ' from the URL]' if url_user else ''}: ").strip() or url_user
        pw = getpass.getpass("  camera password (not shown)" + (" [Enter = the one in the URL]" if url_pw else "") + ": ") or url_pw
        if not user or not pw:
            print("  the camera needs a user name AND a password (an empty one gives 401 Unauthorized) - try again")
            continue
        default = next(f"cam{i}" for i in range(1, 999) if f"cam{i}" not in taken and f"cam{i}" not in [c[0] for c in cams])
        path = input(f"  relay path name [{default}]: ").strip() or default
        if not path.replace("_", "").replace("-", "").isalnum():
            print("  path: letters, digits, - and _ only - try again")
            continue
        if path in taken or path in [c[0] for c in cams]:
            print(f"  path {path} already exists - choose another name")
            continue
        netloc = f"{urllib.parse.quote(user, safe='')}:{urllib.parse.quote(pw, safe='')}@{p.hostname}:{p.port or 554}"
        cams.append((path, urllib.parse.urlunsplit((p.scheme, netloc, p.path, p.query, ""))))
    return cams


def cmd_add(a):
    """Add cameras to an existing relay: same relay password, same address - only new paths."""
    i = load_info()
    if not os.path.exists(CONF):
        die("relay config missing - run setup")
    print(f"existing cameras: {', '.join(i['paths'])}")
    cams = ask_cameras(i["paths"])
    if not cams:
        die("no camera entered")
    q = lambda v: json.dumps(v)
    with open(CONF, "a", encoding="utf-8") as f:          # 'paths:' is the last section of the config
        for path, src in cams:
            f.write(f"  {path}:\n    source: {q(src)}\n")
    private(CONF)
    i["paths"] += [c[0] for c in cams]
    with open(INFO, "w", encoding="utf-8") as f:
        json.dump(i, f, indent=1)
    private(INFO)
    print(f"added: {', '.join(c[0] for c in cams)}. Restart the relay (Ctrl+C in its window, then start), then test.")
    print(f"In Colab: RELAY_PATHS = \"{','.join(i['paths'])}\"  (relay password unchanged)")


def cmd_setup(a):
    global RELAY_PORT
    RELAY_PORT = a.port
    os.makedirs(HOME, exist_ok=True)
    private(HOME)
    ip = tailscale_ip()
    if a.userspace:
        print(f"Tailscale address of this computer: {ip} (userspace mode, no TUN): the relay listens on 127.0.0.1 only;"
              f" Tailscale hands tailnet connections to {ip}:{RELAY_PORT} to it. Nothing is opened on the LAN.")
    else:
        print(f"Tailscale address of this computer: {ip} (the relay listens ONLY there)")
    cams = ask_cameras([])
    if not cams:
        die("no camera entered")
    read_pass = secrets.token_urlsafe(18)
    download_mediamtx()
    write_config(ip, cams, read_pass, a.userspace)
    info = {"tailscaleIp": ip, "port": RELAY_PORT, "user": RELAY_USER, "password": read_pass,
            "paths": [c[0] for c in cams], "userspace": bool(a.userspace)}
    with open(INFO, "w", encoding="utf-8") as f:
        json.dump(info, f, indent=1)
    private(INFO)
    print(f"\nrelay config written: {CONF}")
    cmd_show(a)
    print("Next: python scripts/colab/local_relay.py start   (then in another window: ... test)")


def load_info():
    try:
        with open(INFO, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        import getpass as _g
        die(f"relay not set up for user '{_g.getuser()}': {INFO} not found.\n"
            f"  setup / start / test / show must run as the SAME user (all with sudo or all without).\n"
            f"  find it: sudo ls -l /root/.pyrotech-relay ~/.pyrotech-relay   - or run: python3 scripts/colab/local_relay.py setup")


def cmd_show(a):
    i = load_info()
    print("\nEnter these in the Colab notebook (cell 'Relay settings'):")
    print(f"  RELAY_HOST     = {i['tailscaleIp']}")
    print(f"  RELAY_PORT     = {i['port']}")
    print(f"  RELAY_USER     = {i['user']}")
    print(f"  RELAY_PASSWORD = {i['password']}     <- store it as a Colab secret named RELAY_PASSWORD")
    print(f"  camera paths   = {', '.join(i['paths'])}")
    print("The camera's own address and password never leave this computer.\n")


def cmd_start(a):
    load_info()
    exe = mtx_binary()
    if not os.path.exists(exe):
        die("MediaMTX missing - run setup again")
    cur = tailscale_ip()
    if cur != load_info()["tailscaleIp"]:
        die(f"this computer's Tailscale address changed ({load_info()['tailscaleIp']} -> {cur}): run setup again")
    info = load_info()
    bind_ip = "127.0.0.1" if info.get("userspace") else cur
    port = info.get("port", RELAY_PORT)
    with socket.socket() as t:                       # can the relay bind its address? (fails in userspace mode)
        try:
            t.bind((bind_ip, 0))
        except OSError:
            die(f"{bind_ip} is not an address of this computer: Tailscale runs in userspace mode (no TUN).\n"
                f"  fix: python3 scripts/colab/local_relay.py setup --userspace   (then start again)")
    for host in (bind_ip, "0.0.0.0"):                # is the port already taken (an old relay still running?)
        with socket.socket() as t:
            try:
                t.bind((host, port))
            except OSError:
                die(f"port {port} is already in use on this computer - an earlier relay is probably still running.\n"
                    f"  Windows: taskkill /IM mediamtx.exe /F      Linux: pkill -x mediamtx      then start again.\n"
                    f"  If another program needs {port}: setup --port 8654 (and RELAY_PORT=8654 in Colab).")
    with open(CONF, encoding="utf-8") as f:
        if f"rtspAddress: {bind_ip}:" not in f.read():
            die("the relay config does not match the setup mode - run setup again"
                + (" with --userspace" if info.get("userspace") else ""))
    print(f"relay running on {bind_ip}:{port} -> reachable as {cur}:{port} on Tailscale only. Ctrl+C stops it.")
    try:
        sys.exit(subprocess.call([exe, CONF]))
    except KeyboardInterrupt:
        print("relay stopped")


def rtsp_describe(host, port, path, user, pw, timeout=20):
    """Minimal RTSP DESCRIBE with basic auth. Returns (status code, first SDP lines)."""
    url = f"rtsp://{host}:{port}/{path}"
    auth = base64.b64encode(f"{user}:{pw}".encode()).decode()
    req = (f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 2\r\nAccept: application/sdp\r\n"
           f"Authorization: Basic {auth}\r\nUser-Agent: pyrotech-relay-test\r\n\r\n")
    with socket.create_connection((host, port), timeout=timeout) as s:
        s.settimeout(timeout)
        s.sendall(req.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
        head = data.split(b"\r\n\r\n", 1)[0].decode(errors="replace")
        code = int(head.split()[1]) if head.startswith("RTSP/") else 0
        body = data.split(b"\r\n\r\n", 1)[1] if b"\r\n\r\n" in data else b""
        return code, body.decode(errors="replace")


def cmd_test(a):
    i = load_info()
    bad = 0
    for path in i["paths"]:
        try:
            host = "127.0.0.1" if i.get("userspace") else i["tailscaleIp"]
            code, body = rtsp_describe(host, i["port"], path, i["user"], i["password"])
        except OSError as e:
            print(f"FAIL {path}: relay not reachable ({e}) - is 'start' running?")
            bad += 1
            continue
        if code == 200:
            media = [ln for ln in body.splitlines() if ln.startswith(("m=", "a=rtpmap"))][:3]
            print(f"OK   {path}: relay reached the camera ({'; '.join(media) or 'SDP received'})")
        elif code == 401:
            print(f"FAIL {path}: relay refused the relay user (401) - run setup again")
            bad += 1
        elif code in (400, 404):
            print(f"FAIL {path}: relay could not open the camera ({code}) - check the camera URL / password, see the 'start' window")
            bad += 1
        else:
            print(f"FAIL {path}: unexpected answer {code}")
            bad += 1
    sys.exit(1 if bad else 0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=("setup", "add", "start", "test", "show"))
    ap.add_argument("--port", type=int, default=8554, help="setup: relay port (default 8554; use another one if it is taken)")
    ap.add_argument("--userspace", action="store_true",
                    help="setup: Tailscale runs without a TUN device (tailscaled --tun=userspace-networking)")
    a = ap.parse_args()
    {"setup": cmd_setup, "add": cmd_add, "start": cmd_start, "test": cmd_test, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    try:
        main()
    except PermissionError as e:
        where = e.filename or "?"
        die(f"permission denied on {where} ({e.strerror}).\n"
            f"  - files in {HOME} created earlier with sudo? fix: sudo chown -R $USER: {HOME}\n"
            f"  - running from a folder you cannot write? run as your normal user, WITHOUT sudo\n"
            f"  - mediamtx not executable? fix: chmod +x {mtx_binary()}")
