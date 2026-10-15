#!/usr/bin/env python3
"""Local TCP port -> target host:port through a SOCKS5 proxy (tailscaled --socks5-server).

    python3 socks_bridge.py LISTEN_PORT TARGET_HOST TARGET_PORT [SOCKS_PORT]

Used by colab_launch.py bridge: 127.0.0.1:18554 -> relay on your PC over Tailscale (userspace mode).
Listens on 127.0.0.1 only. One thread pair per connection; logs each connection's result.
"""
import socket
import struct
import sys
import threading
import time


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def socks5_connect(host, port, socks_port, timeout=15):
    s = socket.create_connection(("127.0.0.1", socks_port), timeout=timeout)
    s.sendall(b"\x05\x01\x00")                                  # version 5, 1 method: no auth
    if s.recv(2) != b"\x05\x00":
        raise OSError("SOCKS5 proxy refused (method)")
    h = host.encode()
    s.sendall(b"\x05\x01\x00\x03" + bytes([len(h)]) + h + struct.pack(">H", port))
    rep = s.recv(4)
    if len(rep) < 4 or rep[1] != 0:
        raise OSError(f"SOCKS5 connect to {host}:{port} failed (code {rep[1] if len(rep) > 1 else '?'})")
    alen = {1: 4, 4: 16}.get(rep[3])
    if alen is None:
        alen = s.recv(1)[0]
    s.recv(alen + 2)                                             # bound address + port
    s.settimeout(None)
    return s


def pipe(src, dst):
    try:
        while True:
            data = src.recv(65536)
            if not data:
                break
            dst.sendall(data)
    except OSError:
        pass
    finally:
        for s in (dst, src):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def handle(client, host, port, socks_port):
    try:
        up = socks5_connect(host, port, socks_port)
    except OSError as e:
        log(f"connect {host}:{port} FAILED: {e}")
        client.close()
        return
    t = threading.Thread(target=pipe, args=(up, client), daemon=True)
    t.start()
    pipe(client, up)
    t.join(timeout=5)
    client.close()
    up.close()


def main():
    listen, host, port = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
    socks_port = int(sys.argv[4]) if len(sys.argv) > 4 else 1055
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", listen))
    srv.listen(64)
    log(f"bridge 127.0.0.1:{listen} -> {host}:{port} via SOCKS5 127.0.0.1:{socks_port}")
    while True:
        c, _ = srv.accept()
        threading.Thread(target=handle, args=(c, host, port, socks_port), daemon=True).start()


if __name__ == "__main__":
    main()
