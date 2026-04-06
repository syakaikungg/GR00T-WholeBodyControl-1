"""HTTP MJPEG streaming server for MuJoCo camera images.

Receives images from the ZMQ camera publisher and re-serves them as an
MJPEG stream over HTTP.  Any device with a web browser (including PICO VR)
can view the stream at:

    http://<host_ip>:8888/stream/head_camera
    http://<host_ip>:8888/stream/third_person
    http://<host_ip>:8888/             (index page with links)

Usage:
    # Terminal 1 - sim loop with image publishing:
    python gear_sonic/scripts/run_sim_loop.py --enable-offscreen --enable-image-publish

    # Terminal 2 - streaming server:
    python gear_sonic/scripts/camera_stream_server.py

    # Terminal 3 (optional) - open in browser:
    xdg-open http://localhost:8888/
"""

import argparse
import base64
import socket
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

import cv2
import msgpack
import numpy as np
import zmq


class FrameStore:
    """Thread-safe store for the latest JPEG frame per camera."""

    def __init__(self):
        self._lock = threading.Lock()
        self._frames: dict[str, bytes] = {}
        self._cameras: set[str] = set()

    def update(self, camera_name: str, jpeg_bytes: bytes):
        with self._lock:
            self._frames[camera_name] = jpeg_bytes
            self._cameras.add(camera_name)

    def get(self, camera_name: str) -> bytes | None:
        with self._lock:
            return self._frames.get(camera_name)

    def cameras(self) -> list[str]:
        with self._lock:
            return sorted(self._cameras)


frame_store = FrameStore()


def decode_and_reencode(encoded: str | bytes, quality: int = 80) -> bytes | None:
    if isinstance(encoded, bytes):
        encoded = encoded.decode()
    raw = base64.b64decode(encoded)
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    _, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes()


def zmq_receiver(host: str, port: int):
    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVHWM, 5)
    sub.connect(f"tcp://{host}:{port}")
    print(f"[ZMQ] Connected to tcp://{host}:{port}")

    while True:
        try:
            packed = sub.recv()
            data = msgpack.unpackb(packed, raw=False)
            for key, value in data.items():
                if key in ("timestamps", "images"):
                    continue
                if isinstance(value, (str, bytes)):
                    jpeg = decode_and_reencode(value)
                    if jpeg:
                        frame_store.update(key, jpeg)
        except Exception as e:
            print(f"[ZMQ] Error: {e}")
            time.sleep(0.1)


BOUNDARY = b"--mjpegboundary"


VR_HTML = """<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VR Camera View</title>
<style>
  *{margin:0;padding:0;overflow:hidden;background:#000}
  .container{display:flex;width:100vw;height:100vh}
  .eye{width:50vw;height:100vh;object-fit:contain}
</style>
</head><body>
<div class="container">
  <img class="eye" id="left" src="/stream/CAMERA" alt="L"/>
  <img class="eye" id="right" src="/stream/CAMERA" alt="R"/>
</div>
</body></html>"""


class MJPEGHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._serve_index()
        elif self.path.startswith("/vr/"):
            camera = self.path.split("/vr/", 1)[1].rstrip("/") or "head_camera"
            self._serve_vr(camera)
        elif self.path.startswith("/stream/"):
            camera = self.path.split("/stream/", 1)[1].rstrip("/")
            self._serve_mjpeg(camera)
        elif self.path.startswith("/snapshot/"):
            camera = self.path.split("/snapshot/", 1)[1].rstrip("/")
            self._serve_snapshot(camera)
        else:
            try:
                self.send_error(404)
            except BrokenPipeError:
                pass

    def _serve_index(self):
        cameras = frame_store.cameras()
        body_parts = [
            "<html><head><title>MuJoCo Camera Streams</title>"
            "<style>body{font-family:sans-serif;margin:2em}"
            "img{max-width:640px;border:1px solid #ccc;margin:1em 0}"
            "</style></head><body>"
            "<h1>MuJoCo Camera Streams</h1>"
            "<h2>VR Stereo View (for PICO browser)</h2>"
            '<p><a href="/vr/head_camera">Head Camera - VR Side-by-Side</a></p>'
        ]
        if not cameras:
            body_parts.append("<p>No cameras available yet. Is the sim loop running with --enable-image-publish?</p>")
        for cam in cameras:
            body_parts.append(
                f'<h2>{cam}</h2>'
                f'<img src="/stream/{cam}" alt="{cam}"/><br/>'
                f'<a href="/snapshot/{cam}">Snapshot</a> | '
                f'<a href="/stream/{cam}">Raw stream</a> | '
                f'<a href="/vr/{cam}">VR stereo</a>'
            )
        body_parts.append("</body></html>")
        html = "".join(body_parts).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _serve_vr(self, camera: str):
        html = VR_HTML.replace("CAMERA", camera).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)

    def _serve_snapshot(self, camera: str):
        frame = frame_store.get(camera)
        if frame is None:
            self.send_error(404, f"Camera '{camera}' not found")
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.end_headers()
        self.wfile.write(frame)

    def _serve_mjpeg(self, camera: str):
        self.send_response(200)
        self.send_header("Content-Type", f"multipart/x-mixed-replace; boundary={BOUNDARY.decode()}")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        try:
            while True:
                frame = frame_store.get(camera)
                if frame:
                    self.wfile.write(BOUNDARY + b"\r\n")
                    self.wfile.write(b"Content-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(frame)}\r\n".encode())
                    self.wfile.write(b"\r\n")
                    self.wfile.write(frame)
                    self.wfile.write(b"\r\n")
                    self.wfile.flush()
                time.sleep(0.033)
        except (BrokenPipeError, ConnectionResetError):
            pass


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    parser = argparse.ArgumentParser(description="HTTP MJPEG camera stream server")
    parser.add_argument("--zmq-host", default="localhost", help="ZMQ source host")
    parser.add_argument("--zmq-port", type=int, default=5555, help="ZMQ source port")
    parser.add_argument("--http-port", type=int, default=8888, help="HTTP server port")
    args = parser.parse_args()

    zmq_thread = threading.Thread(target=zmq_receiver, args=(args.zmq_host, args.zmq_port), daemon=True)
    zmq_thread.start()

    local_ip = get_local_ip()
    server = HTTPServer(("0.0.0.0", args.http_port), MJPEGHandler)
    print(f"[HTTP] MJPEG server at:")
    print(f"       http://localhost:{args.http_port}/")
    print(f"       http://{local_ip}:{args.http_port}/")
    print(f"       (PICO VR browser can access the second URL)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nServer stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
