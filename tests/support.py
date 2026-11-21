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

    def send(self, *, clip_id=None, detected_at=None):
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


def csrf_token(server, page="/records/new"):
    """画面のフォームに埋め込まれた CSRF トークンを取り出す（既定は、いつもフォームのある画面）。"""
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


class FakeAdvisor:
    """相談の作業スレッド用の偽の判定器。answer・lookup_output に値か例外を入れて変える。"""

    name = "偽の判定器"

    def __init__(self, answer=None, lookup_output=None):
        self.answer = {"answer": "量は今のままで大丈夫です"} if answer is None else answer
        self.lookup_output = lookup_output
        self.last_usage = None
        self.prompts = []

    @staticmethod
    def _result(value):
        if isinstance(value, BaseException):
            raise value
        return value

    def judge(self, folder, prompt, images, schema):
        self.prompts.append(prompt)
        return self._result(self.answer)

    def lookup(self, folder, prompt, schema):
        self.prompts.append(prompt)
        return self._result(self.lookup_output)


class NoLookupAdvisor(FakeAdvisor):
    """ウェブ検索の口を持たない判定器。"""

    lookup = None


def first_user(store):
    with store.database.connect() as db:
        return db.execute("SELECT id FROM users LIMIT 1").fetchone()[0]


def setup_guide(server, store, *, birth=True, food_active=True):
    """生後5か月・3.2 kg・小型（予想5 kg）・385 kcal のフード・給餌の予定3件を入れる。"""
    from datetime import timedelta

    from poop_detector.records.feeding_plans import save_plan
    from poop_detector.records.foods import active_foods, save_food, stop_food
    from poop_detector.records.profile import save_profile
    from poop_detector.records.weights import save_birth_date, save_weight
    from poop_detector.timezones import local_today

    request(server, "GET", "/")  # セッションのユーザーを作る
    today = local_today()
    database = store.database
    if birth:
        save_birth_date(database, (today - timedelta(days=152)).isoformat())
    save_weight(database, "", today, 3200, "")
    save_food(database, name="パピー", kcal_dk=3850, source_url="", user_id=first_user(store))
    (food,) = active_foods(database)
    save_profile(
        database,
        breed="",
        size_class="small",
        adult_grams=5000,
        neutered="",
        food_id=food.id,
    )
    for clock in ("08:00", "12:00", "18:00"):
        save_plan(database, clock)
    if not food_active:
        stop_food(database, food.id)
