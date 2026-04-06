"""ZMQ subscriber that receives and displays MuJoCo camera images.

Usage:
    # Start the sim loop with image publishing:
    python gear_sonic/scripts/run_sim_loop.py --enable-offscreen --enable-image-publish

    # In another terminal, run this viewer:
    python gear_sonic/scripts/camera_viewer.py
    python gear_sonic/scripts/camera_viewer.py --port 5555 --camera head_camera
"""

import argparse
import base64
import time

import cv2
import msgpack
import numpy as np
import zmq


def decode_image(encoded: str) -> np.ndarray:
    raw = base64.b64decode(encoded)
    arr = np.frombuffer(raw, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def main():
    parser = argparse.ArgumentParser(description="MuJoCo camera image viewer over ZMQ")
    parser.add_argument("--host", default="localhost", help="ZMQ host")
    parser.add_argument("--port", type=int, default=5555, help="ZMQ port")
    parser.add_argument("--camera", default=None, help="Show only this camera (default: all)")
    parser.add_argument("--save-dir", default=None, help="Directory to save frames")
    args = parser.parse_args()

    ctx = zmq.Context()
    sub = ctx.socket(zmq.SUB)
    sub.setsockopt(zmq.SUBSCRIBE, b"")
    sub.setsockopt(zmq.RCVHWM, 5)
    sub.connect(f"tcp://{args.host}:{args.port}")

    print(f"Connected to tcp://{args.host}:{args.port}, waiting for images...")

    frame_count = 0
    fps_start = time.time()
    fps_value = 0.0

    try:
        while True:
            try:
                packed = sub.recv(flags=zmq.NOBLOCK)
            except zmq.Again:
                time.sleep(0.001)
                continue

            data = msgpack.unpackb(packed, raw=False)

            frame_count += 1
            elapsed = time.time() - fps_start
            if elapsed >= 1.0:
                fps_value = frame_count / elapsed
                frame_count = 0
                fps_start = time.time()

            for key, value in data.items():
                if key in ("timestamps", "images"):
                    continue
                if isinstance(value, (str, bytes)):
                    if args.camera and key != args.camera:
                        continue
                    img = decode_image(value) if isinstance(value, str) else decode_image(value.decode())
                    if img is not None:
                        cv2.putText(
                            img,
                            f"{key} | {fps_value:.1f} fps",
                            (10, 30),
                            cv2.FONT_HERSHEY_SIMPLEX,
                            0.7,
                            (0, 255, 0),
                            2,
                        )
                        cv2.imshow(key, img)

                        if args.save_dir:
                            import os
                            os.makedirs(args.save_dir, exist_ok=True)
                            cv2.imwrite(
                                os.path.join(args.save_dir, f"{key}_{frame_count:06d}.jpg"), img
                            )

            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    except KeyboardInterrupt:
        print("\nViewer stopped.")
    finally:
        cv2.destroyAllWindows()
        sub.close()
        ctx.term()


if __name__ == "__main__":
    main()
