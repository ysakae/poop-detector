"""スマホからの体重入力と書き出しを検証する。"""

import re

from support import request, submit


def test_family_records_and_corrects_weight(gui):
    server, _, _ = gui
    assert request(server, "GET", "/weight", auth=False)[0] == 403
    assert request(server, "GET", "/weight")[0] == 200
    assert (
        submit(server, "/weight", {"action": "profile", "birth": "2024-01-01"}, page="/weight")[0]
        == 303
    )
    for day, value, unit in (("2024-03-01", "1500", "g"), ("2024-03-08", "1.85", "kg")):
        assert (
            submit(server, "/weight", {"day": day, "value": value, "unit": unit}, page="/weight")[0]
            == 303
        )
    _, page = request(server, "GET", "/weight")
    html = page.decode()
    assert "1,850 g" in html
    assert "+350 g" in html
    assert "9週" in html
    assert '<svg class="weight-chart"' in html
    # 最新の体重は大きな数字で、前回との差は色付きのラベルで示す。
    assert re.search(r'体重</span><strong class="stat-value">1,850<span class="stat-unit">g', html)
    assert re.search(r'<span class="chip chip-[a-z]+">前回比 \+350 g</span>', html)
    assert 'style="' not in html.split('<svg class="weight-chart"', 1)[1].split("</svg>", 1)[0]
    identifier = re.search(r'href="/weight\?id=([a-f0-9]+)"', html)[1]
    assert (
        submit(
            server,
            "/weight",
            {"id": identifier, "day": "2024-03-08", "value": "1855", "unit": "g"},
            page="/weight",
        )[0]
        == 303
    )
    assert "1,855 g" in request(server, "GET", "/weight")[1].decode()


def test_weight_invalid_and_export(gui):
    server, _, _ = gui
    assert (
        submit(
            server, "/weight", {"day": "2024-03-01", "value": "NaN", "unit": "kg"}, page="/weight"
        )[0]
        == 400
    )
    assert (
        submit(
            server,
            "/weight",
            {"day": "2024-03-01", "value": "1850", "unit": "g", "note": "健診"},
            page="/weight",
        )[0]
        == 303
    )
    period = {"from": "2024-03-01", "to": "2024-03-02"}
    status, data = submit(server, "/manage/export/weights.csv", period, page="/manage/export")
    assert status == 200
    assert "2024-03-01,1850,健診" in data.decode("utf-8-sig")
    status, data = request(server, "GET", "/manage/export/summary?from=2024-03-01&to=2024-03-02")
    assert status == 200
    assert "健診" in data.decode()
