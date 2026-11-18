"""スマホからの体重入力と書き出しを検証する。"""

import re
from datetime import date

from support import request, submit

from poop_detector.records.weights import save_weight


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
    html = data.decode()
    assert "健診" in html
    assert '<text class="weight-axis-unit"' in html
    assert re.search(r'<text class="weight-tick weight-date-tick"[^>]+>3/1</text>', html)


def test_weight_chart_has_scaled_axes_and_focusable_measurements(gui):
    server, store, _ = gui
    for day, grams in (("2024-12-28", 1500), ("2024-12-29", 1630), ("2025-01-06", 1850)):
        save_weight(store.database, "", date.fromisoformat(day), grams, "")

    status, data = request(server, "GET", "/weight")
    assert status == 200
    html = data.decode()
    assert re.search(r'<text class="weight-axis-unit"[^>]+>g</text>', html)
    assert re.findall(r'<text class="weight-tick weight-gram-tick"[^>]+>([^<]+)</text>', html) == [
        "2,000",
        "1,800",
        "1,600",
        "1,400",
    ]
    dates = re.findall(r'<text class="weight-tick weight-date-tick"[^>]+>([^<]+)</text>', html)
    assert dates == ["2024/12/28", "2024/12/31", "2025/1/3", "2025/1/6"]
    points = re.findall(
        r'<g class="weight-data-point" transform="translate\((\d+) \d+\)" tabindex="0" '
        r'role="img" aria-label="([^"]+)">',
        html,
    )
    assert [label for _, label in points] == [
        "2024-12-28 1,500 g",
        "2024-12-29 1,630 g",
        "2025-01-06 1,850 g",
    ]
    first, middle, last = (int(x) for x, _ in points)
    assert first < middle < last
    assert abs((middle - first) * 9 - (last - first)) <= 5
    assert html.count('class="weight-tooltip"') == 3
    assert request(server, "GET", "/assets/weight_chart.js")[0] == 200


def test_weight_chart_keeps_equal_measurements_on_the_same_level(gui):
    server, store, _ = gui
    for day in (date(2024, 3, 1), date(2024, 3, 2)):
        save_weight(store.database, "", day, 1, "")

    status, data = request(server, "GET", "/weight")
    assert status == 200
    html = data.decode()
    heights = re.findall(r'<g class="weight-data-point" transform="translate\(\d+ (\d+)\)"', html)
    assert len(heights) == 2
    assert heights[0] == heights[1]
    assert re.search(r'<text class="weight-tick weight-gram-tick"[^>]+>0</text>', html)
