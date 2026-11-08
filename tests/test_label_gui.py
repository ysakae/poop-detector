"""実HTTP経由でラベルの保存と画像の公開範囲を確認する。"""

import http.client
import re
import threading
from urllib.parse import urlencode

import numpy as np
import pytest

from poop_detector.events import Store


@pytest.fixture
def gui(tmp_path):
    from poop_detector.label_gui import make_server

    store = Store(tmp_path)
    event = store.record(np.full((50, 50, 3), 150, np.uint8), 0.1, None, 1)
    server = make_server(tmp_path, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, store, event
    server.shutdown()
    server.server_close()
    thread.join()


def request(server, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection(*server.server_address)
    conn.request(method, path, body=body, headers=headers or {})
    response = conn.getresponse()
    status, data = response.status, response.read()
    conn.close()
    return status, data


def post(server, fields, token=True):
    if token:
        _, page = request(server, "GET", "/")
        fields["csrf"] = re.search(r'name="csrf" value="([^"]+)"', page.decode())[1]
    return request(
        server,
        "POST",
        "/save",
        urlencode(fields),
        {"Content-Type": "application/x-www-form-urlencoded"},
    )


def test_gui_lists_image_and_saves_label(gui):
    server, store, event = gui
    status, data = request(server, "GET", "/")
    assert status == 200
    assert "排便だった".encode() in data
    assert event.encode() in data
    assert request(server, "GET", "/image?id=" + event)[0] == 200
    assert (
        post(
            server,
            {"id": event, "label": "true", "start": "2026-11-08T19:00", "end": "2026-11-08T19:01"},
        )[0]
        == 303
    )
    row = store.list()[0]
    assert row["feedback"] == 1
    assert row["actual_start"] == "2026-11-08T19:00:00+09:00"
    assert post(server, {"id": event, "label": "false"})[0] == 303
    assert store.list()[0]["actual_start"] == row["actual_start"]
    assert post(server, {"id": event, "label": "unknown"})[0] == 303
    assert store.list()[0]["feedback"] is None


def test_gui_rejects_invalid_time_and_missing_csrf(gui):
    server, store, event = gui
    assert post(server, {"id": event, "label": "true"}, token=False)[0] == 403
    assert post(server, {"id": event, "label": "true", "start": "不正"})[0] == 400
    assert (
        post(
            server,
            {"id": event, "label": "true", "start": "2026-11-08T19:01", "end": "2026-11-08T19:00"},
        )[0]
        == 400
    )
    assert store.list()[0]["feedback"] is None
    assert request(server, "GET", "/image?id=../../.env")[0] == 404
    assert request(server, "GET", "/.env")[0] == 404
    assert request(server, "GET", "/", headers={"Host": "evil.example"})[0] == 403


def test_gui_missed_and_filter(gui):
    server, store, event = gui
    assert post(server, {"action": "missed", "start": "2026-11-08T19:00"})[0] == 303
    assert store.metrics()["missed"] == 1
    assert post(server, {"id": event, "label": "false"})[0] == 303
    assert event.encode() not in request(server, "GET", "/?filter=unlabeled")[1]
    assert event.encode() in request(server, "GET", "/?filter=false")[1]
