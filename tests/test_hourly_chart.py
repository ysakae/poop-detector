"""記録の画面の時間帯別の集計を、24時間の棒グラフと、たたんだ表で見せることを検証する。"""

import re

from support import request

from poop_detector.gui import pages


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


def test_busiest_hours_are_ranked_separately_for_feces_and_urine(gui):
    server, store, _ = gui
    seed(store)
    store.add_excretion("both", "2026-11-08T19:20:00+09:00")
    store.add_excretion("feces", "2026-11-08T21:10:00+09:00")
    html = records_page(server)
    assert "<strong>排便が多い時間帯</strong> 7時台（2件）、19時台（1件）" in html
    assert "<strong>排尿が多い時間帯</strong> 19時台（2件）、7時台（1件）" in html


def test_busiest_hours_show_when_one_type_has_no_records(gui):
    server, store, _ = gui
    store.add_excretion("urine", "2026-11-08T19:00:00+09:00")
    html = records_page(server)
    assert "<strong>排便が多い時間帯</strong> 記録なし" in html
    assert "<strong>排尿が多い時間帯</strong> 19時台（1件）" in html


def test_busiest_hours_render_with_previous_summary_format(gui, monkeypatch):
    server, store, _ = gui
    seed(store)
    current_record_stats = pages.record_stats

    def previous_record_stats(records, query):
        stats = current_record_stats(records, query)
        stats["busiest"] = "7時台（3件）、19時台（1件）"
        return stats

    # 稼働中のプロセスが新しいテンプレートだけを読み直す場合を再現する。
    monkeypatch.setattr(pages, "record_stats", previous_record_stats)
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
