"""インストール先の環境で、主要な機能が動くかを人工データで確かめる。

Linux のホストや Docker コンテナでの確認用です。カメラや実データは使いません。
    uv run python scripts/smoke-check.py
"""

import http.client
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import cv2
import numpy as np

from poop_detector.clips import encode_mp4
from poop_detector.config import Config, apply_timezone
from poop_detector.events import Store
from poop_detector.gui.server import make_server


def check(name, condition):
    print(f"{'OK' if condition else 'NG'}  {name}", flush=True)
    if not condition:
        raise SystemExit(1)


def check_video(root):
    frames = []
    for step in range(10):
        image = np.full((120, 160, 3), 200, np.uint8)
        cv2.circle(image, (20 + step * 12, 60), 10, (40, 60, 90), -1)
        ok, jpeg = cv2.imencode(".jpg", image)
        if not ok:
            raise RuntimeError("JPEG に変換できません")
        frames.append((step / 5, "2026-11-10T00:00:00+00:00", jpeg.tobytes()))
    path = root / "clips" / "smoke.mp4"
    path.parent.mkdir()
    encode_mp4(frames, path, 5)
    capture = cv2.VideoCapture(str(path))
    readable = capture.isOpened() and capture.read()[0]
    capture.release()
    check("FFmpeg で MP4 を作成し、OpenCV で読み出せる", readable)


def check_timezone():
    Config(timezone="Asia/Tokyo")
    apply_timezone("Asia/Tokyo")
    check("timezone: Asia/Tokyo を反映できる（タイムゾーン情報がある）", True)


def get_status(server, path, headers):
    conn = http.client.HTTPConnection(*server.server_address)
    conn.request("GET", path, headers=headers)
    status = conn.getresponse().status
    conn.close()
    return status


def check_gui(root):
    Store(root).record(np.full((60, 80, 3), 150, np.uint8), 0.1, None, 1)
    server = make_server(root, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host = {"Host": f"127.0.0.1:{server.server_port}"}
    # 起動時のトークンをブラウザーに移した後と同じ Cookie を付ける。
    session = {**host, "Cookie": f"poop_detector_session={server.access_token}"}
    try:
        for path in ("/", "/records", "/clips", "/activity"):
            check(f"ラベル GUI の {path} が表示できる", get_status(server, path, session) == 200)
        check("ラベル GUI はトークンなしの閲覧を拒否する", get_status(server, "/", host) == 403)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def check_cli(root):
    config = root / "config.yaml"
    config.write_text(f"data_dir: {root}\nnotifications: false\n")
    # 同じ Python で自分の CLI を起動するだけで、外部の入力は使わない。
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "poop_detector.main", "--config", str(config), "events"],
        capture_output=True,
        text=True,
        check=False,
    )
    check("CLI の events コマンドが動く", result.returncode == 0 and result.stdout.startswith("["))


def main():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        check_video(root)
        check_timezone()
        check_gui(root)
        check_cli(root)
    print("すべての確認に成功しました")


if __name__ == "__main__":
    main()
