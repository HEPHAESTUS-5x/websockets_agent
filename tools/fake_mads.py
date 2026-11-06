#!/usr/bin/env python3
"""Test the web dashboard without the machine. Python 3 standard library only.

Two modes:

  serve    (default) A fake mads-websockets bridge: serves ../web on HTTP, a
           bootstrap.json and the WebSockets /mads/<topic>, and generates the
           data of the agents (eekf_plugin, spi_agent, energy_kpi). No MADS,
           no broker, nothing to build.

             python3 tools/fake_mads.py
             -> open http://localhost:8080/mads

  publish  Generates the same data and publishes it through a REAL bridge, as
           a WebSocket client (one connection per topic). Tests the whole chain
           broker -> mads-websockets -> browser.

             mads broker                                  (terminal 1)
             ./build/src/mads-websockets -w tcp://localhost:9092   (terminal 2)
             python3 tools/fake_mads.py publish --url ws://localhost:<ws_port>/mads
             -> open the HTTP UI address printed by mads-websockets

The motion: a 120 x 80 mm square and a 50 mm circle in XY, Z slowly up and
down, A tilting to -25 deg, C turning; it "cuts" for 12 s, then 12 s in air.
Ctrl+C to stop.
"""
import argparse
import base64
import hashlib
import json
import math
import os
import random
import socket
import socketserver
import struct
import sys
import threading
import time
import urllib.parse
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PROTOCOL = "mads-websockets"


# ─────────────────────────────── data ───────────────────────────────
class Generator:
    """The messages of the agents, as the dashboard expects them."""

    def __init__(self):
        self.t0 = time.time()
        self.t_us = 0
        self.energy_kwh = 0.0

    @staticmethod
    def pose(t):
        u = (t % 24.0) / 24.0
        if u < 0.5:                                   # square 120 x 80 mm
            q = u / 0.5 * 4
            side = min(3, int(q))
            f = q - side
            sx = (f, 1, 1 - f, 0)[side]
            sy = (0, f, 1, 1 - f)[side]
            x, y = -60 + 120 * sx, -40 + 80 * sy
        else:                                         # circle r = 50 mm, from the same corner
            th = (u - 0.5) / 0.5 * 2 * math.pi
            x, y = -10 - 50 * math.cos(th), -40 - 50 * math.sin(th)
        z = -110 + 6 * math.sin(2 * math.pi * t / 12)
        a = -25 * (0.5 - 0.5 * math.cos(2 * math.pi * t / 48))
        c = (t * 6) % 360
        return [x, y, z, a, c]

    def eekf(self, t):
        cut = (t % 24.0) < 12.0
        if cut:
            F = [140 + 25 * math.sin(t * 9.1) + 12 * random.random(),
                 -70 + 18 * math.sin(t * 5.3) + 8 * random.random(),
                 55 + 10 * math.sin(t * 3.7) + 5 * random.random()]
        else:
            F = [random.uniform(-1, 1) for _ in range(3)]
        self.t_us = (self.t_us + 1024) % 2**32
        return {
            "fmu_state_correction": [random.gauss(0, 1e-6) for _ in range(43)],
            "fmu_input": {"torque_in": [0.4, -0.2, 1.1, 0.0, 0.0]},
            "fmu_dt": 0.001024,
            "eekf": {
                "t_us": self.t_us, "force_n": F, "force_norm_n": math.hypot(*F),
                "force_std_n": [2.1, 2.1, 19.3], "position": self.pose(t),
                "bias_nm": [0.01, -0.02, 0.0, 0.0, 0.0], "cutting": cut,
                "measurements": 3 if cut else 6, "nis": 2.5 + 2 * random.random(),
                "rejected_updates": 0, "air_rejections": 9, "overruns": 0, "gaps": 0, "fmu_rejections": 0,
            },
        }

    def spi(self, t):
        cut = (t % 24.0) < 12.0
        return {
            "t_us": int(t * 1e6) % 2**32,
            "torque_nm": [0.8 + 0.1 * math.sin(t * 7), 0.5, 2.1, 0.2, 0.1, 3.2 if cut else 0.2],
            "fe_hz": [12.5, -8.2, 3.1, 0.0, 0.0, 200.0],
            "imu": {"clamp": [0.01, -0.02, 1.0], "spindle": [0.02 * math.sin(t * 40), 0.01, 1.0]},
        }

    def kpi(self, t):
        cut = (t % 24.0) < 12.0
        p_in = 3200 + (900 if cut else 0) + 120 * random.random()
        p_cut = 620 + 60 * random.random() if cut else 0.0
        self.energy_kwh += p_in / 3.6e6
        return {"kpi": {"efficiency": p_cut / p_in, "cutting_power_w": p_cut, "mains_power_w": p_in,
                        "specific_energy_j_mm3": 4.2 + 0.4 * random.random() if cut else 0.0,
                        "energy_kwh": self.energy_kwh}}


def run_generator(send, rates, stop):
    """Calls send(topic, dict) at the given rates [Hz] until stop is set."""
    gen = Generator()
    makers = {"eekf_plugin": gen.eekf, "spi_agent": gen.spi, "energy_kpi": gen.kpi}
    next_t = {k: time.time() for k in rates}
    while not stop.is_set():
        now = time.time()
        for topic, hz in rates.items():
            n = 0
            while next_t[topic] <= now and n < 50:        # catch up, but never flood
                t = next_t[topic] - gen.t0
                msg = makers[topic](t)
                msg["hostname"] = socket.gethostname()
                msg["timecode"] = round(t, 4)
                send(topic, json.dumps(msg, separators=(",", ":")))
                next_t[topic] += 1.0 / hz
                n += 1
            if next_t[topic] < now - 1.0:
                next_t[topic] = now
        time.sleep(0.002)


# ──────────────────────────── WebSocket bits ────────────────────────────
def ws_frame(text, mask=False):
    payload = text.encode()
    n = len(payload)
    head = bytearray([0x81])
    mbit = 0x80 if mask else 0
    if n < 126:
        head.append(mbit | n)
    elif n < 65536:
        head.append(mbit | 126)
        head += struct.pack(">H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack(">Q", n)
    if mask:
        key = os.urandom(4)
        head += key
        payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
    return bytes(head) + payload


def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return buf


def ws_read(sock):
    """Reads one frame: (opcode, payload bytes)."""
    b0, b1 = recv_exact(sock, 2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", recv_exact(sock, 8))[0]
    key = recv_exact(sock, 4) if b1 & 0x80 else None
    data = recv_exact(sock, n)
    if key:
        data = bytes(b ^ key[i % 4] for i, b in enumerate(data))
    return b0 & 0x0F, data


# ──────────────────────────────── serve ────────────────────────────────
class Hub:
    def __init__(self):
        self.lock = threading.Lock()
        self.clients = []          # (socket, topic)

    def add(self, sock, topic):
        with self.lock:
            self.clients.append((sock, topic))
        print(f"  + browser on {topic} ({len(self.clients)} open)")

    def remove(self, sock):
        with self.lock:
            self.clients = [c for c in self.clients if c[0] is not sock]

    def publish(self, topic, text):
        frame = None
        with self.lock:
            targets = [s for s, t in self.clients if t == topic or t == "_all"]
        for s in targets:
            frame = frame or ws_frame(text)
            try:
                s.sendall(frame)
            except OSError:
                self.remove(s)


def make_ws_handler(hub, base_path):
    class WsHandler(socketserver.BaseRequestHandler):
        def handle(self):
            sock = self.request
            req = b""
            while b"\r\n\r\n" not in req:
                chunk = sock.recv(4096)
                if not chunk:
                    return
                req += chunk
            lines = req.decode(errors="replace").split("\r\n")
            path = lines[0].split(" ")[1] if len(lines[0].split(" ")) > 1 else "/"
            headers = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
            prefix = base_path + "/"
            if not path.startswith(prefix) or "sec-websocket-key" not in headers:
                sock.sendall(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                return
            topic = path[len(prefix):].split("?")[0].split("#")[0]
            topic = urllib.parse.unquote(topic)
            accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + GUID).encode()).digest()).decode()
            sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: {PROTOCOL}\r\n\r\n").encode())
            hub.add(sock, topic)
            try:
                while True:
                    op, data = ws_read(sock)
                    if op == 8:
                        break
                    if op == 9:                      # ping -> pong
                        sock.sendall(bytes([0x8A, len(data)]) + data)
                    elif op == 1:                    # a client publishes: relay it, like the bridge
                        hub.publish(topic, data.decode(errors="replace"))
            except (ConnectionError, OSError):
                pass
            finally:
                hub.remove(sock)
                print(f"  - browser left {topic}")
    return WsHandler


def serve(args):
    web = os.path.abspath(args.web)
    if not os.path.isfile(os.path.join(web, "index.html")):
        sys.exit(f"index.html not found in {web} (use --web)")
    base = "/mads"

    class HttpHandler(SimpleHTTPRequestHandler):
        def __init__(self, *a, **kw):
            super().__init__(*a, directory=web, **kw)

        def log_message(self, *a):
            pass

        def do_GET(self):
            path = self.path.split("?")[0]
            if path in ("/bootstrap.json", base + "/bootstrap.json"):
                body = json.dumps({"scheme": "ws", "port": args.ws_port, "path": base,
                                   "addresses": ["127.0.0.1"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if path in ("/", base, base + "/"):
                self.path = "/index.html"
            elif path.startswith(base + "/"):
                self.path = path[len(base):]
            return super().do_GET()

    hub = Hub()
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    socketserver.ThreadingTCPServer.daemon_threads = True
    ws_srv = socketserver.ThreadingTCPServer(("0.0.0.0", args.ws_port), make_ws_handler(hub, base))
    http_srv = ThreadingHTTPServer(("0.0.0.0", args.http_port), HttpHandler)
    threading.Thread(target=ws_srv.serve_forever, daemon=True).start()
    threading.Thread(target=http_srv.serve_forever, daemon=True).start()
    print(f"Fake bridge: open http://localhost:{args.http_port}/mads   (WebSockets on port {args.ws_port})")
    print(f"Serving {web}")
    stop = threading.Event()
    rates = {} if args.quiet else {"eekf_plugin": args.ekf_hz, "spi_agent": 100.0, "energy_kpi": 1.0}
    try:
        if rates:
            print(f"Generating eekf_plugin at {args.ekf_hz:g} Hz, spi_agent at 100 Hz, energy_kpi at 1 Hz. Ctrl+C to stop.")
            run_generator(hub.publish, rates, stop)
        else:
            print("No data generated (--quiet): only the messages published by clients are relayed.")
            while True:
                time.sleep(1)
    except KeyboardInterrupt:
        print("\nbye")


# ─────────────────────────────── publish ───────────────────────────────
class WsClient:
    def __init__(self, url, topic):
        from urllib.parse import urlparse, quote
        u = urlparse(url.rstrip("/") + "/" + quote(topic))
        self.topic = topic
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET {u.path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                           f"Sec-WebSocket-Protocol: {PROTOCOL}\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("closed during the handshake")
            resp += chunk
        if b" 101 " not in resp.split(b"\r\n")[0]:
            raise ConnectionError(resp.split(b"\r\n")[0].decode(errors="replace"))
        self.sock.settimeout(None)
        self.lock = threading.Lock()
        # the bridge also sends us what arrives on this topic: drain it
        threading.Thread(target=self._drain, daemon=True).start()

    def _drain(self):
        try:
            while True:
                ws_read(self.sock)
        except (ConnectionError, OSError):
            pass

    def send(self, text):
        with self.lock:
            self.sock.sendall(ws_frame(text, mask=True))


def publish(args):
    rates = {"eekf_plugin": args.ekf_hz, "spi_agent": 100.0, "energy_kpi": 1.0}
    clients = {}
    for topic in rates:
        clients[topic] = WsClient(args.url, topic)
        print(f"connected: {args.url.rstrip('/')}/{topic}")
    print(f"Publishing eekf_plugin at {args.ekf_hz:g} Hz, spi_agent at 100 Hz, energy_kpi at 1 Hz. Ctrl+C to stop.")
    stop = threading.Event()
    try:
        run_generator(lambda topic, text: clients[topic].send(text), rates, stop)
    except KeyboardInterrupt:
        print("\nbye")
    except (ConnectionError, OSError) as e:
        sys.exit(f"bridge connection lost: {e}")


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", nargs="?", choices=["serve", "publish"], default="serve")
    ap.add_argument("--web", default=os.path.join(here, "..", "web"), help="folder with index.html (serve)")
    ap.add_argument("--http-port", type=int, default=8080, help="HTTP port of the page (serve)")
    ap.add_argument("--ws-port", type=int, default=8090, help="WebSocket port (serve)")
    ap.add_argument("--quiet", action="store_true", help="serve without generating data")
    ap.add_argument("--url", default="ws://localhost:9002/mads", help="WebSocket base of the real bridge (publish)")
    ap.add_argument("--ekf-hz", type=float, default=500.0, help="rate of the eekf_plugin messages")
    args = ap.parse_args()
    serve(args) if args.mode == "serve" else publish(args)


if __name__ == "__main__":
    main()
