"""実HTTP経由でラベルの保存と画像の公開範囲を確認する。"""

import re
from urllib.parse import urlencode

from support import csrf_token, request


def post(server, fields, token=True):
    if token:
        fields = {**fields, "csrf": csrf_token(server)}
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


def test_gui_urine_and_excretion_log(gui):
    server, store, event = gui
    assert post(server, {"id": event, "label": "urine", "note": "濡れ跡を確認"})[0] == 303
    assert store.records()[0]["excretion_type"] == "urine"
    assert "濡れ跡を確認".encode() in request(server, "GET", "/records")[1]
    assert (
        post(server, {"action": "manual", "type": "feces", "start": "2026-11-08T23:10"})[0] == 303
    )
    assert len(store.records()) == 2
    assert store.metrics()["missed"] == 0
    assert event.encode() in request(server, "GET", "/?filter=urine")[1]
    assert post(server, {"action": "missed", "type": "both", "start": "2026-11-08T23:20"})[0] == 303
    assert store.metrics()["excretion_counts"] == {"feces": 2, "urine": 2, "unknown": 0}
    assert (
        post(server, {"action": "manual", "type": "wrong", "start": "2026-11-08T23:30"})[0] == 400
    )


def test_records_show_daily_counts_and_hourly_statistics_in_japan_time(gui):
    server, store, _ = gui
    store.add_excretion("feces", "2026-11-02T00:00:00+09:00")
    store.add_excretion("urine", "2026-11-01T23:59:59+09:00")
    store.add_excretion("both", "2026-11-07T15:00:00+00:00", missed=True)
    store.add_excretion("urine", "2026-11-08T00:59:59+09:00", location="outside")
    store.add_excretion("unspecified", "2026-11-08T08:00:00+09:00")
    store.add_excretion("both", "2026-11-08T15:00:00+00:00")

    status, body = request(server, "GET", "/records?date=2026-11-08&days=7")
    assert status == 200
    html = body.decode()
    assert "排便 1件 ／ 排尿 2件 ／ 種類不明 1件" in html
    assert "2026-11-02〜2026-11-08（7日間・記録あり2日）" in html
    hourly = html.split('id="hourly-stats"', 1)[1].split("</table>", 1)[0]
    assert re.search(r"00:00〜00:59</th>.*?</meter> 2件.*?</meter> 2件", hourly)
    assert re.search(r"08:00〜08:59</th>.*?</meter> 0件.*?</meter> 0件", hourly)


def test_records_stats_follow_confirmed_start_or_fall_back_to_record_time(gui):
    server, store, event = gui
    with store.connect() as db:
        db.execute("UPDATE events SET timestamp=? WHERE id=?", ("2026-11-07T15:25:00+00:00", event))
    url = "/records?date=2026-11-08&days=7"
    assert "排便 0件 ／ 排尿 0件".encode() in request(server, "GET", url)[1]
    store.classify(event, "urine", start=None)
    assert "排便 0件 ／ 排尿 1件".encode() in request(server, "GET", url)[1]
    store.classify(event, "urine", start="2026-11-07T23:55:00+09:00")
    assert "排便 0件 ／ 排尿 0件".encode() in request(server, "GET", url)[1]
    assert request(server, "GET", "/records?date=invalid&days=-1")[0] == 200
