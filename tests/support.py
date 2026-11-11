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


def login(server):
    """起動時のトークン付き URL を開いてセッションを始め、その Cookie ヘッダーを返す。"""
    conn = http.client.HTTPConnection(*server.server_address)
    conn.request("GET", f"/?token={server.access_token}")
    response = conn.getresponse()
    response.read()
    conn.close()
    assert response.status == 303, response.status
    return {"Cookie": response.headers["Set-Cookie"].split(";", 1)[0]}


def session_cookie(server):
    """ブラウザーがトークンをセッションに移した後と同じ Cookie ヘッダーを返す（サーバーごとに1回）。"""
    cookie = getattr(server, "test_cookie", None)
    if cookie is None:
        cookie = server.test_cookie = login(server)
    return cookie


def exchange(server, method, path, body=None, headers=None, auth=True):
    """テスト用のラベル GUI に HTTP で要求し、状態コード、応答ヘッダー、本文を返す。

    auth=False で、アクセストークンを持たない相手を再現する。
    """
    conn = http.client.HTTPConnection(*server.server_address)
    sent = {**(session_cookie(server) if auth else {}), **(headers or {})}
    conn.request(method, path, body=body, headers=sent)
    response = conn.getresponse()
    result = response.status, response.headers, response.read()
    conn.close()
    return result


def request(server, method, path, body=None, headers=None, auth=True):
    """exchange の状態コードと本文だけを返す。"""
    status, _, data = exchange(server, method, path, body, headers, auth)
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
