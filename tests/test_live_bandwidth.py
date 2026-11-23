"""ライブ映像の通信を抑える工夫（コマ数の上限、見えない間は止める、映像の古さ）を検証する。"""

import asyncio
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from support import exchange, login

from poop_detector.gui import live_stream

STATIC = Path(__file__).resolve().parents[1] / "src" / "poop_detector" / "gui" / "static"


@pytest.mark.parametrize(
    ("value", "interval"),
    [
        ("2", 0.5),
        ("5", 0.2),
        ("30", 1 / 30),
        (None, None),
        ("", None),
        ("abc", None),
        ("0", None),
        ("31", None),
        ("-3", None),
        ("2.5", None),
    ],
)
def test_frame_rate_from_the_query(value, interval):
    found = live_stream.send_interval(value)
    assert found == (pytest.approx(interval) if interval else None)


class EndlessPreview:
    """呼ぶたびに新しいコマを返す偽のライブ映像。"""

    def __init__(self):
        self.count = 0

    def request_frame(self):
        pass

    def latest(self, last_stamp):
        self.count += 1
        return b"jpeg", str(self.count)


async def frames_within(seconds, interval, monkeypatch):
    monkeypatch.setattr(live_stream, "session_active", lambda gui, session_id: True)
    gui = SimpleNamespace(preview=EndlessPreview())
    sent = 0
    deadline = time.monotonic() + seconds
    async for chunk in live_stream.live_frames(gui, "session", interval):
        if time.monotonic() >= deadline:
            break
        sent += chunk.startswith(b"--frame\r\n")
    return sent


def test_stream_keeps_to_the_requested_frame_rate(monkeypatch):
    # 4コマ/秒を指定すると、1秒で5コマを超えて送らない（指定がなければ、新しいコマをすぐ送る）。
    assert asyncio.run(frames_within(1.0, 0.25, monkeypatch)) <= 5
    assert asyncio.run(frames_within(1.0, None, monkeypatch)) > 10


def test_live_panel_offers_a_data_saver(gui):
    server, _, _ = gui
    status, _, body = exchange(server, "GET", "/", headers=login(server), auth=False)
    assert status == 200
    panel = re.search(
        r'<section [^>]*aria-labelledby="live-title".*?</section>', body.decode(), re.S
    )
    assert panel
    actions = re.search(r'<div class="live-heading">(.*?)</div>', panel[0], re.S)
    assert actions, "映像の見出しに操作欄がありません"
    assert 'id="live-title"' in actions[1]
    assert re.search(r'<button[^>]*id="live-saver"[^>]*aria-pressed="false"[^>]*>', actions[1])
    assert 'id="live-age"' in panel[0]


def test_script_pauses_off_screen_and_lowers_the_frame_rate():
    script = (STATIC / "live.js").read_text("utf-8")
    # 画面から外れたら受け取りを止める。
    assert "IntersectionObserver" in script
    # データセーバーや遅い回線では、コマ数を落とす。
    for needle in ("saveData", "effectiveType", "/live/stream?fps="):
        assert needle in script
    # 「通信を節約」の選択は端末ごとに覚える（読めない環境でも動く）。
    assert "localStorage" in script
    assert "try {" in script
    # 映像が古いときは、何秒前の映像かを出す。
    assert "秒前の映像" in script
