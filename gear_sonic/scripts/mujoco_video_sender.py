"""Emulate XRoboToolkit-Orin-Video-Sender for MuJoCo camera images.

Drop-in replacement for the Orin Video Sender, allowing the PICO VR
headset's XRoboToolkit Unity Client to display MuJoCo simulation camera
images using the built-in "ZEDMINI" video source — no Unity Client
modification needed.

Protocol (reverse-engineered from XRoboToolkit-Orin-Video-Sender):
  Control channel (TCP 13579):
    PICO connects → sends OPEN_CAMERA/CLOSE_CAMERA wrapped in
    NetworkDataProtocol with a 4-byte BE outer length header.
  Video channel (TCP, port from OPEN_CAMERA):
    PC connects to PICO's IP:port, sends H.264 frames as
    [4-byte BE size][H.264 access unit data].

On the PICO:
  1. Open XRoboToolkit app
  2. Go to Remote Vision → select "ZEDMINI"
  3. Enter this PC's IP address → Confirm

Usage:
    python gear_sonic/scripts/mujoco_video_sender.py
    python gear_sonic/scripts/mujoco_video_sender.py --camera head_camera
"""

import argparse
import base64
import socket
import struct
import threading
import time
from fractions import Fraction

import cv2
import msgpack
import numpy as np
import zmq

try:
    import av
except ImportError:
    av = None


# ---------------------------------------------------------------------------
# Protocol parsing
# ---------------------------------------------------------------------------

def parse_network_data_protocol(data: bytes) -> tuple:
    """Parse NetworkDataProtocol: [4B LE cmd_len][cmd][4B LE data_len][data]."""
    if len(data) < 8:
        raise ValueError("Too small for NetworkDataProtocol")
    offset = 0
    cmd_len = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    if cmd_len < 0 or offset + cmd_len > len(data):
        raise ValueError(f"Invalid command length: {cmd_len}")
    command = data[offset:offset + cmd_len].decode("utf-8").rstrip("\x00")
    offset += cmd_len
    if offset + 4 > len(data):
        raise ValueError("Missing data length field")
    data_len = struct.unpack_from("<i", data, offset)[0]
    offset += 4
    payload = data[offset:offset + data_len] if data_len > 0 else b""
    return command, payload


def parse_camera_request(data: bytes) -> dict:
    """Parse CameraRequestData from OPEN_CAMERA payload.

    Format: [0xCA 0xFE][ver=1][7x int32 LE][compact_str camera][compact_str ip]
    """
    if len(data) < 10:
        raise ValueError("Too small for CameraRequestData")
    if data[0] != 0xCA or data[1] != 0xFE:
        raise ValueError(f"Bad magic: 0x{data[0]:02X}{data[1]:02X}")
    if data[2] != 1:
        raise ValueError(f"Unsupported version: {data[2]}")
    offset = 3
    fields = struct.unpack_from("<7i", data, offset)
    offset += 28

    def _read_cstr(d, o):
        length = d[o]
        o += 1
        s = d[o:o + length].decode("utf-8") if length > 0 else ""
        return s, o + length

    camera, offset = _read_cstr(data, offset)
    ip, offset = _read_cstr(data, offset)
    return {
        "width": fields[0], "height": fields[1], "fps": fields[2],
        "bitrate": fields[3], "enable_hevc": fields[4],
        "render_mode": fields[5], "port": fields[6],
        "camera": camera, "ip": ip,
    }


# ---------------------------------------------------------------------------
# H.264 Encoder (PyAV / libx264)
# ---------------------------------------------------------------------------

class H264Encoder:
    def __init__(self, width: int, height: int, fps: int, bitrate: int):
        if av is None:
            raise RuntimeError("PyAV is required. Install: pip install 'av<13'")
        self.ctx = av.CodecContext.create("libx264", "w")
        self.ctx.width = width
        self.ctx.height = height
        self.ctx.pix_fmt = "yuv420p"
        self.ctx.time_base = Fraction(1, fps)
        self.ctx.gop_size = 15
        self.ctx.bit_rate = bitrate
        self.ctx.options = {
            "preset": "ultrafast",
            "tune": "zerolatency",
            "x264-params": "repeat-headers=1",
        }
        self.ctx.open()
        self._pts = 0

    def encode(self, bgr_image: np.ndarray) -> list:
        frame = av.VideoFrame.from_ndarray(
            cv2.cvtColor(bgr_image, cv2.COLOR_BGR2RGB), format="rgb24"
        )
        frame.pts = self._pts
        self._pts += 1
        return [bytes(pkt) for pkt in self.ctx.encode(frame)]

    def flush(self) -> list:
        return [bytes(pkt) for pkt in self.ctx.encode(None)]

    def close(self):
        try:
            self.flush()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# TCP Video Sender
# ---------------------------------------------------------------------------

class VideoSender:
    def __init__(self, ip: str, port: int):
        self.ip = ip
        self.port = port
        self.sock = None

    def connect(self, retries: int = 10) -> bool:
        for i in range(retries):
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                s.connect((self.ip, self.port))
                self.sock = s
                print(f"[VIDEO] Connected to {self.ip}:{self.port}")
                return True
            except Exception as e:
                print(f"[VIDEO] Attempt {i+1}/{retries}: {e}")
                time.sleep(1)
        return False

    def send_frame(self, h264_data: bytes):
        if self.sock is None:
            raise ConnectionError("Not connected")
        self.sock.sendall(struct.pack(">I", len(h264_data)) + h264_data)

    def disconnect(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


# ---------------------------------------------------------------------------
# TCP Control Server (port 13579)
# ---------------------------------------------------------------------------

class ControlServer:
    """Handles PICO OPEN_CAMERA / CLOSE_CAMERA commands."""

    def __init__(self, port: int = 13579):
        self.port = port
        self._sock = None
        self.running = False
        self.on_open_camera = None
        self.on_close_camera = None

    def start(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("0.0.0.0", self.port))
        self._sock.listen(1)
        self._sock.settimeout(1.0)
        self.running = True
        print(f"[CTRL] Listening on port {self.port}")

        while self.running:
            try:
                client, addr = self._sock.accept()
                print(f"[CTRL] PICO connected from {addr}")
                self._handle_client(client)
            except socket.timeout:
                continue
            except OSError:
                if self.running:
                    time.sleep(1)

    def _handle_client(self, client: socket.socket):
        client.settimeout(1.0)
        buf = b""
        try:
            while self.running:
                try:
                    chunk = client.recv(4096)
                    if not chunk:
                        print("[CTRL] PICO disconnected")
                        if self.on_close_camera:
                            self.on_close_camera()
                        break
                    buf += chunk
                    buf = self._process(buf)
                except socket.timeout:
                    continue
        except Exception as e:
            print(f"[CTRL] Error: {e}")
        finally:
            client.close()

    def _process(self, buf: bytes) -> bytes:
        while len(buf) >= 4:
            body_len = struct.unpack(">I", buf[:4])[0]
            total = 4 + body_len
            if len(buf) < total:
                break
            body = buf[4:total]
            buf = buf[total:]
            try:
                cmd, payload = parse_network_data_protocol(body)
                print(f"[CTRL] Command: {cmd}")
                if cmd == "OPEN_CAMERA":
                    cfg = parse_camera_request(payload)
                    print(f"[CTRL] Config: {cfg['width']}x{cfg['height']}@{cfg['fps']}fps "
                          f"→ {cfg['ip']}:{cfg['port']}")
                    if self.on_open_camera:
                        self.on_open_camera(cfg)
                elif cmd == "CLOSE_CAMERA":
                    if self.on_close_camera:
                        self.on_close_camera()
                else:
                    print(f"[CTRL] Unknown: {cmd}")
            except Exception as e:
                print(f"[CTRL] Parse error: {e}")
        return buf

    def stop(self):
        self.running = False
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

class MujocoVideoSender:
    def __init__(self, zmq_host="localhost", zmq_port=5555,
                 listen_port=13579, camera_filter=None):
        self.zmq_host = zmq_host
        self.zmq_port = zmq_port
        self.listen_port = listen_port
        self.camera_filter = camera_filter

        self._encoder = None
        self._sender = None
        self._streaming = False
        self._stop = threading.Event()
        self._lock = threading.Lock()

        self._target_w = 0
        self._target_h = 0
        self._frame_interval = 0.033

        self.frames_sent = 0
        self.bytes_sent = 0

    # -- callbacks from ControlServer --

    def _on_open(self, cfg: dict):
        with self._lock:
            if self._streaming:
                self._stop_stream()

            ip = cfg["ip"]
            port = cfg["port"]
            w = cfg.get("width") or 1280
            h = cfg.get("height") or 720
            fps = cfg.get("fps") or 30
            bitrate = cfg.get("bitrate") or 4_000_000

            self._sender = VideoSender(ip, port)
            if not self._sender.connect():
                print("[STREAM] Cannot reach PICO video port")
                self._sender = None
                return
            try:
                self._encoder = H264Encoder(w, h, fps, bitrate)
            except Exception as e:
                print(f"[STREAM] Encoder error: {e}")
                self._sender.disconnect()
                self._sender = None
                return

            self._target_w = w
            self._target_h = h
            self._frame_interval = 1.0 / fps
            self._streaming = True
            self.frames_sent = 0
            self.bytes_sent = 0
            print(f"[STREAM] Encoding {w}x{h}@{fps}fps, bitrate={bitrate}")

    def _on_close(self):
        with self._lock:
            self._stop_stream()

    def _stop_stream(self):
        self._streaming = False
        if self._encoder:
            self._encoder.close()
            self._encoder = None
        if self._sender:
            self._sender.disconnect()
            self._sender = None
        print("[STREAM] Stopped")

    # -- main loop --

    def run(self):
        if av is None:
            print("[FATAL] PyAV is required. Install: pip install 'av<13'")
            return

        ctrl = ControlServer(self.listen_port)
        ctrl.on_open_camera = self._on_open
        ctrl.on_close_camera = self._on_close
        threading.Thread(target=ctrl.start, daemon=True).start()

        ctx = zmq.Context()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.setsockopt(zmq.RCVHWM, 2)
        sub.connect(f"tcp://{self.zmq_host}:{self.zmq_port}")
        print(f"[ZMQ] Subscribed to tcp://{self.zmq_host}:{self.zmq_port}")

        local_ip = _get_local_ip()
        print(f"\n{'='*60}")
        print(f"  MuJoCo Video Sender ready")
        print(f"  Control port : {self.listen_port}")
        print(f"  This PC IP   : {local_ip}")
        print(f"{'='*60}")
        print(f"  PICO側の操作:")
        print(f"    1. XRoboToolkit を開く")
        print(f"    2. Remote Vision → ZEDMINI を選択")
        print(f"    3. IP に {local_ip} を入力 → Confirm")
        print(f"{'='*60}\n")

        last_send = 0.0
        try:
            while not self._stop.is_set():
                try:
                    packed = sub.recv(flags=zmq.NOBLOCK)
                except zmq.Again:
                    time.sleep(0.005)
                    continue

                if not self._streaming:
                    continue

                now = time.time()
                if now - last_send < self._frame_interval:
                    continue

                data = msgpack.unpackb(packed, raw=False)
                for key, value in data.items():
                    if key in ("timestamps", "images"):
                        continue
                    if self.camera_filter and key != self.camera_filter:
                        continue
                    if not isinstance(value, (str, bytes)):
                        continue

                    bgr = _decode_base64_jpeg(value)
                    if bgr is None:
                        continue

                    stereo = _make_stereo(bgr, self._target_w, self._target_h)

                    with self._lock:
                        if not self._streaming:
                            break
                        try:
                            for pkt in self._encoder.encode(stereo):
                                self._sender.send_frame(pkt)
                                self.frames_sent += 1
                                self.bytes_sent += len(pkt)
                            last_send = now
                        except Exception as e:
                            print(f"[STREAM] Send error: {e}")
                            self._stop_stream()
                            break

                    if self.frames_sent > 0 and self.frames_sent % 200 == 0:
                        mb = self.bytes_sent / 1024 / 1024
                        print(f"[STREAM] {self.frames_sent} frames, {mb:.1f} MB sent")
                    break

        except KeyboardInterrupt:
            print("\nStopping...")
        finally:
            ctrl.stop()
            with self._lock:
                self._stop_stream()
            sub.close()
            ctx.term()
            mb = self.bytes_sent / 1024 / 1024
            print(f"[DONE] {self.frames_sent} frames, {mb:.1f} MB total")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_base64_jpeg(encoded) -> np.ndarray:
    if isinstance(encoded, bytes):
        encoded = encoded.decode()
    raw = base64.b64decode(encoded)
    arr = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _make_stereo(bgr: np.ndarray, target_w: int, target_h: int) -> np.ndarray:
    """Resize mono image and duplicate side-by-side for stereo display."""
    half_w = target_w // 2
    mono = cv2.resize(bgr, (half_w, target_h))
    return np.concatenate([mono, mono], axis=1)


def _get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Emulate Orin Video Sender for MuJoCo → PICO VR streaming"
    )
    parser.add_argument("--zmq-host", default="localhost")
    parser.add_argument("--zmq-port", type=int, default=5555)
    parser.add_argument("--listen-port", type=int, default=13579,
                        help="TCP control port (default: 13579)")
    parser.add_argument("--camera", default=None,
                        help="Camera name filter (default: all)")
    args = parser.parse_args()

    sender = MujocoVideoSender(
        zmq_host=args.zmq_host,
        zmq_port=args.zmq_port,
        listen_port=args.listen_port,
        camera_filter=args.camera,
    )
    sender.run()


if __name__ == "__main__":
    main()
