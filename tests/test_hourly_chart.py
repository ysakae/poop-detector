"""記録の画面の時間帯別の集計を、24時間の棒グラフと、たたんだ表で見せることを検証する。"""

import re

from support import request


def records_page(server, query="date=2026-11-08&days=7"):
    status, body = request(server, "GET", f"/records?{query}")
    assert status == 200
    return body.decode()


def seed(store):
    store.add_excretion("feces", "2026-11-08T07:10:00+09:00")
    store.add_excretion("feces", "2026-11-07T07:40:00+09:00")
    store.add_excretion("urine", "2026-11-08T07:20:00+09:00")
    store.add_excretion("urine", "2026-11-08T19:00:00+09:00")


def test_chart_has_a_column_for_every_hour_with_counts_as_text(gui):
    server, store, _ = gui
    seed(store)
    html = records_page(server)
    chart = re.search(r'<div class="hourly-chart".*?</div></figure>', html, re.S)[0]
    assert chart.count('class="hour-col"') == 24
    # 件数のある棒には数を添える（色だけで伝えない）。0件の棒には数を出さない。
    assert re.search(r'<span class="bar bar-feces level-10"><b>2</b></span>', chart)
    assert re.search(r'<span class="bar bar-urine level-5"><b>1</b></span>', chart)
    assert '<span class="bar bar-feces level-0"></span>' in chart
    # 軸の目盛りは3時間ごと。
    assert re.findall(r'<span class="hour-label">(\d+)</span>', chart) == [
        "0",
        "3",
        "6",
        "9",
        "12",
        "15",
        "18",
        "21",
    ]


def test_busiest_hours_are_named_in_words(gui):
    server, store, _ = gui
    seed(store)
    html = records_page(server)
    assert "多い時間帯：7時台（3件）、19時台（1件）" in html


def test_table_is_still_readable_but_folded(gui):
    server, store, _ = gui
    seed(store)
    html = records_page(server)
    folded = re.search(
        r"<details>\s*<summary>数で見る（1時間ごとの表）</summary>.*?</details>", html, re.S
    )
    assert folded
    assert 'id="hourly-stats"' in folded[0]
    # 時間帯別の集計そのものは、開いたまま（表だけをたたむ）。
    assert "<details open><summary>時間帯別の排便・排尿</summary>" not in html


def test_no_records_says_so_instead_of_an_empty_chart(gui):
    server, _, _ = gui
    html = records_page(server, "date=2020-01-01&days=7")
    assert "この期間の確定記録はありません" in html
    assert "多い時間帯" not in html
