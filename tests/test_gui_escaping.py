"""利用者やカメラ・Codex から来た文字列が、画面でエスケープされることを検証する。

テンプレートは自動でエスケープするので、特殊文字を含む値を各画面に通し、
生のタグとして出ないことを確かめる。
"""

import threading
from datetime import UTC, datetime

import numpy as np
import pytest
from jinja2 import UndefinedError
from support import request

from poop_detector.gui.server import make_server
from poop_detector.gui.templating import render_template
from poop_detector.records.events import Store
from poop_detector.video.clip_reviews import Assessment
from poop_detector.video.clips import ClipStore

EVIL = "<script>alert(\"x\")</script> & 'q' <b>太字</b>"
RAW = ("<script>alert", "<b>太字")


@pytest.fixture
def evil_gui(tmp_path):
    """メモ・判定の根拠・エラー・犬の名前に特殊文字を入れたラベル GUI。"""
    store = Store(tmp_path)
    event = store.record(np.full((50, 50, 3), 150, np.uint8), 0.1, None, 1)
    store.classify(event, "urine", start="2026-11-10T09:00:00+09:00", note=EVIL)
    clips = ClipStore(tmp_path)
    ready = clips.request("manual", event_id=event)
    (tmp_path / "clips" / f"{ready}.mp4").write_bytes(b"0")
    clips.update(ready, status="ready", path=f"clips/{ready}.mp4", duration=10)
    clips.assessments.save(
        ready,
        Assessment(label="urine", activity="other", end_seconds=3, reason=EVIL),
        observed_at=datetime.now(UTC).isoformat(),
    )
    failed = clips.request("manual")
    clips.update(failed, status="failed", error=EVIL)
    server = make_server(tmp_path, 0, pet_name="<b>ポチ</b>")
    thread = threading.Thread(target=server.serve_forever, args=(0.02,), daemon=True)
    thread.start()
    yield server, ready, failed
    server.shutdown()
    server.server_close()
    thread.join()


def pages(server, ready, failed):
    return [
        "/",
        "/records/events?filter=all",
        "/records",
        f"/clips?id={ready}",
        f"/clips?id={failed}",
        "/manage/storage",
        "/activity",
    ]


def test_special_characters_are_escaped_on_every_page(evil_gui):
    server, ready, failed = evil_gui
    for path in pages(server, ready, failed):
        status, body = request(server, "GET", path)
        text = body.decode()
        assert status == 200, path
        for raw in RAW:
            assert raw not in text, (path, raw)
        # 犬の名前は見出しにエスケープして出る。
        assert "&lt;b&gt;ポチ&lt;/b&gt;" in text, path


def test_escaped_values_are_still_shown(evil_gui):
    server, ready, failed = evil_gui
    assert "&lt;script&gt;alert" in request(server, "GET", "/records")[1].decode()
    assert "&lt;script&gt;alert" in request(server, "GET", f"/clips?id={ready}")[1].decode()
    assert "&lt;script&gt;alert" in request(server, "GET", f"/clips?id={failed}")[1].decode()


def test_templates_escape_by_default_and_reject_missing_values():
    page = render_template(
        "message.html",
        title="<i>t</i>",
        heading="<h1>",
        message=EVIL,
        back_url='/"><script>',
        back_text="戻る",
        wrap_link=False,
    )
    assert "<script>" not in page
    assert "&lt;i&gt;t&lt;/i&gt;" in page
    assert 'href="/&#34;&gt;&lt;script&gt;"' in page
    with pytest.raises(UndefinedError):
        render_template("message.html", title="t")
