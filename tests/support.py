"""複数のテストファイルで使う人工データと偽オブジェクト。

フィクスチャは conftest.py に、普通の関数やクラスはこのモジュールに置く。
"""

import http.client
import re
from urllib.parse import urlencode
from uuid import uuid4

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


def login(server, name=None, role="admin"):
    """ユーザーを作ってセッションを始め、その Cookie ヘッダーを返す（パスキーでのログインの代わり）。

    パスキーでのログインそのものは test_gui_passkeys.py が確かめる。
    """
    app = server.app
    user_id = app.accounts.users.create(name or f"テスト{uuid4().hex[:8]}", role)
    return {"Cookie": f"poop_detector_session={app.sessions.create(user_id)}"}


def session_cookie(server):
    """ログインしたブラウザーと同じ Cookie ヘッダーを返す（サーバーごとに1回だけユーザーを作る）。"""
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


def csrf_token(server, page="/records/events"):
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


def write_video(path, frame, count=10):
    path.parent.mkdir(parents=True, exist_ok=True)
    height, width = frame.shape[:2]
    writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 5, (width, height))
    assert writer.isOpened()
    for _ in range(count):
        writer.write(frame)
    writer.release()


def add_clip(
    clips,
    start,
    label,
    *,
    signature="scene-a",
    event_id=None,
    duration=2,
    activity="other",
    frame=None,
    location=None,
    **values,
):
    """保存済みの動画を1本登録し、人のラベルを付ける（frame を渡すと再生できる動画を書く）。"""
    from poop_detector.video.clip_labels import ClipLabel

    clip_id = clips.request(event_id=event_id)
    clips.update(
        clip_id,
        status="ready",
        path=f"clips/{clip_id}.mp4",
        duration=duration,
        start_at=start,
        scene_signature=signature,
        **values,
    )
    if frame is None:
        (clips.root / "clips" / f"{clip_id}.mp4").write_bytes(b"x")
    else:
        write_video(clips.root / "clips" / f"{clip_id}.mp4", frame)
    if label is not None:
        clips.labels.save(clip_id, ClipLabel(label, activity=activity, location=location))
    return clip_id
