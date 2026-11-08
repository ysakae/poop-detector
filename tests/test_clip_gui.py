"""動画の再生・ラベル付けと手動保存要求のHTTP確認。"""

import re
from urllib.parse import urlencode

from test_label_gui import gui as gui
from test_label_gui import request


def submit(server, path, fields):
    _, body = request(server, "GET", "/clips")
    fields["csrf"] = re.search(r'name="csrf" value="([^"]+)"', body.decode())[1]
    return request(
        server,
        "POST",
        path,
        urlencode(fields),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )


def test_video_range_and_labels(gui):
    from poop_detector.clips import ClipStore

    server, store, _ = gui
    clips = ClipStore(store.root)
    clip = clips.request()
    (store.root / "clips" / (clip + ".mp4")).write_bytes(b"0123456789abcdef")
    clips.update(clip, status="ready", path=f"clips/{clip}.mp4", duration=10)
    assert b"<video" in request(server, "GET", "/clips?id=" + clip)[1]
    assert request(server, "GET", "/clip-video?id=" + clip, headers={"Range": "bytes=2-5"}) == (
        206,
        b"2345",
    )
    assert (
        request(server, "GET", "/clip-video?id=" + clip, headers={"Range": "bytes=100-"})[0] == 416
    )
    assert (
        submit(
            server,
            "/clips/label",
            {"id": clip, "label": "feces", "start": "2", "end": "4", "activity": "excreting"},
        )[0]
        == 303
    )
    assert clips.get(clip)["actual_start_offset"] == 2
    assert (
        submit(server, "/clips/label", {"id": clip, "label": "urine", "start": "-1", "end": "4"})[0]
        == 400
    )
    assert request(server, "GET", "/clip-video?id=../../.env")[0] == 404


def test_manual_request_requires_running_camera(gui):
    from poop_detector.clips import ClipStore

    server, store, _ = gui
    assert submit(server, "/clips/request", {})[0] == 409
    clips = ClipStore(store.root)
    clips.heartbeat(True)
    assert submit(server, "/clips/request", {})[0] == 303
    assert clips.list()[0]["source"] == "manual"
