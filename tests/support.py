"""複数のテストファイルで使う人工データと偽オブジェクト。

フィクスチャは conftest.py に、普通の関数やクラスはこのモジュールに置く。
"""

import http.client
import re
from urllib.parse import urlencode

import cv2
import numpy as np


def spot_scene():
    """明るい無地の基準画像と、中央に暗い小さな四角を置いた画像を返す。"""
    baseline = np.full((200, 200, 3), 200, np.uint8)
    changed = baseline.copy()
    cv2.rectangle(changed, (80, 80), (100, 100), (30, 30, 30), -1)
    return baseline, changed


class FrameSource:
    """monitor に渡す偽のカメラ。与えたフレームを順に返し、最後のフレームを繰り返す。

    None を含めると、その回は新しいフレームが届かなかった扱いになる。
    """

    generation = 1
    healthy = True

    def __init__(self, *frames):
        if not frames:
            raise ValueError("フレームを1枚以上渡してください")
        self.frames = frames
        self.count = 0

    def read(self):
        frame = self.frames[min(self.count, len(self.frames) - 1)]
        self.count += 1
        return None if frame is None else frame.copy()


class SilentNotifier:
    """送信したことにしない通知。呼ばれた回数だけ数える。"""

    def __init__(self):
        self.sent = 0

    def send(self):
        self.sent += 1
        return False


def request(server, method, path, body=None, headers=None):
    """テスト用のラベル GUI に HTTP で要求し、状態コードと本文を返す。"""
    conn = http.client.HTTPConnection(*server.server_address)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    status, data = response.status, response.read()
    conn.close()
    return status, data


def csrf_token(server, page="/"):
    """画面のフォームに埋め込まれた CSRF トークンを取り出す。"""
    _, body = request(server, "GET", page)
    return re.search(r'name="csrf" value="([^"]+)"', body.decode())[1]


def submit(server, path, fields, *, page="/clips"):
    """CSRF トークンを付けてフォームを送信する。"""
    fields = {**fields, "csrf": csrf_token(server, page)}
    return request(
        server,
        "POST",
        path,
        urlencode(fields, doseq=True),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )


def ready_clip(clips, *, event_id=None):
    """変換済みの30秒のクリップを1件登録し、ID を返す。"""
    clip = clips.request(event_id=event_id)
    clips.update(
        clip,
        status="ready",
        path=f"clips/{clip}.mp4",
        duration=30,
        start_at="2026-11-09T06:10:00+00:00",
    )
    return clip
