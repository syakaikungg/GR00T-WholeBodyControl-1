"""Forward MuJoCo camera images to PICO VR via XRoboToolkit.

Subscribes to the ZMQ camera publisher (port 5555) and sends JPEG frames
to the PICO headset through xrobotoolkit_sdk.send_bytes_to_device().

The PICO-side XRoboToolkit Unity Client receives frames via
PXREADeviceCustomMessage callback.

Wire format per frame:
    [camera_name_len: 1 byte][camera_name: N bytes][jpeg_data: rest]

Usage:
    source .venv_teleop/bin/activate
    python gear_sonic/scripts/camera_xrt_forwarder.py

    # With custom device ID:
    python gear_sonic/scripts/camera_xrt_forwarder.py --device-id MyDevice

    # Specific camera only:
    python gear_sonic/scripts/camera_xrt_forwarder.py --camera head_camera
"""

import argparse
import base64
import struct
import subprocess
import sys
import time
import threading

import cv2
import msgpack
import numpy as np
import zmq

try:
    import xrobotoolkit_sdk as xrt
except ImportError:
    xrt = None
    print("[WARN] xrobotoolkit_sdk not available. Install it to forward images to PICO.")


def decode_jpeg(encoded: str | bytes, quality: int = 70) -> bytes | None:
    if isinstance(encoded, bytes):
        encoded = encoded.decode()
    raw = base64.b64decode(encoded)
    arr = np.frombuffer(raw, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None
    _, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return buf.tobytes()


def pack_frame(camera_name: str, jpeg_bytes: bytes) -> bytes:
    """Pack camera name + JPEG into a single byte buffer."""
    name_bytes = camera_name.encode("utf-8")
    return struct.pack("B", len(name_bytes)) + name_bytes + jpeg_bytes


class XRTCameraForwarder:
    def __init__(
        self,
        device_id: str = "TestDevice",
        zmq_host: str = "localhost",
        zmq_port: int = 5555,
        target_fps: int = 15,
        jpeg_quality: int = 70,
        camera_filter: str | None = None,
    ):
        self.device_id = device_id
        self.zmq_host = zmq_host
        self.zmq_port = zmq_port
        self.target_fps = target_fps
        self.jpeg_quality = jpeg_quality
        self.camera_filter = camera_filter
        self.frame_interval = 1.0 / target_fps
        self._stop = threading.Event()

        self.frames_sent = 0
        self.frames_dropped = 0
        self.bytes_sent = 0

    def run(self):
        if xrt is None:
            print("[ERROR] xrobotoolkit_sdk not available")
            sys.exit(1)

        subprocess.Popen(
            ["bash", "/opt/apps/roboticsservice/runService.sh"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(1)
        xrt.init()
        print(f"[XRT] Initialized, device_id={self.device_id}")

        ctx = zmq.Context()
        sub = ctx.socket(zmq.SUB)
        sub.setsockopt(zmq.SUBSCRIBE, b"")
        sub.setsockopt(zmq.RCVHWM, 2)
        sub.connect(f"tcp://{self.zmq_host}:{self.zmq_port}")
        print(f"[ZMQ] Connected to tcp://{self.zmq_host}:{self.zmq_port}")
        print(f"[FWD] Forwarding at {self.target_fps} fps, JPEG quality={self.jpeg_quality}")
        if self.camera_filter:
            print(f"[FWD] Camera filter: {self.camera_filter}")

        last_send = 0.0
        try:
            while not self._stop.is_set():
                try:
                    packed = sub.recv(flags=zmq.NOBLOCK)
                except zmq.Again:
                    time.sleep(0.005)
                    continue

                now = time.time()
                if now - last_send < self.frame_interval:
                    continue

                data = msgpack.unpackb(packed, raw=False)
                for key, value in data.items():
                    if key in ("timestamps", "images"):
                        continue
                    if self.camera_filter and key != self.camera_filter:
                        continue
                    if not isinstance(value, (str, bytes)):
                        continue

                    jpeg = decode_jpeg(value, self.jpeg_quality)
                    if jpeg is None:
                        continue

                    frame = pack_frame(key, jpeg)
                    try:
                        xrt.send_bytes_to_device(self.device_id, frame)
                        self.frames_sent += 1
                        self.bytes_sent += len(frame)
                        last_send = now
                    except Exception as e:
                        self.frames_dropped += 1
                        if self.frames_dropped % 100 == 1:
                            print(f"[XRT] send error: {e}")

                if self.frames_sent > 0 and self.frames_sent % 100 == 0:
                    print(
                        f"[FWD] sent={self.frames_sent} dropped={self.frames_dropped} "
                        f"total={self.bytes_sent / 1024 / 1024:.1f} MB"
                    )

        except KeyboardInterrupt:
            print("\n[FWD] Stopping...")
        finally:
            sub.close()
            ctx.term()
            try:
                xrt.close()
            except Exception:
                pass
            print(
                f"[FWD] Done. sent={self.frames_sent} dropped={self.frames_dropped} "
                f"total={self.bytes_sent / 1024 / 1024:.1f} MB"
            )

    def stop(self):
        self._stop.set()


def main():
    parser = argparse.ArgumentParser(description="Forward MuJoCo camera to PICO via XRoboToolkit")
    parser.add_argument("--device-id", default="TestDevice", help="XRoboToolkit device ID (default: TestDevice)")
    parser.add_argument("--zmq-host", default="localhost", help="ZMQ camera source host")
    parser.add_argument("--zmq-port", type=int, default=5555, help="ZMQ camera source port")
    parser.add_argument("--fps", type=int, default=15, help="Target FPS (default: 15)")
    parser.add_argument("--quality", type=int, default=70, help="JPEG quality 1-100 (default: 70)")
    parser.add_argument("--camera", default=None, help="Camera name filter (default: all cameras)")
    args = parser.parse_args()

    forwarder = XRTCameraForwarder(
        device_id=args.device_id,
        zmq_host=args.zmq_host,
        zmq_port=args.zmq_port,
        target_fps=args.fps,
        jpeg_quality=args.quality,
        camera_filter=args.camera,
    )
    forwarder.run()


if __name__ == "__main__":
    main()
