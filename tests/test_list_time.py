"""一覧の時刻（今日・きのう・月日）と、日ごとの見出しで区切る表示を検証する。"""

import re
from datetime import UTC, date, datetime, timedelta

from support import exchange, login

from poop_detector.gui.list_time import by_day, list_time

# 日本時間の 2026/11/17。conftest.py でタイムゾーンを日本時間に固定している。
TODAY = date(2026, 11, 17)


def utc(text: str) -> str:
    """日本時間の時刻を、データベースと同じ UTC の ISO 8601 にする。"""
    local = datetime.fromisoformat(text + "+09:00")
    return local.astimezone(UTC).isoformat()


def test_today_and_yesterday_are_named():
    shown = list_time(utc("2026-11-17T18:11:19"), TODAY)
    assert shown is not None
    assert (shown.text, shown.clock, shown.day) == ("今日 18:11", "18:11", "今日")
    assert shown.iso == "2026-11-17T18:11:19+09:00"
    assert shown.full == "2026/11/17 18:11:19"
    yesterday = list_time(utc("2026-11-16T15:40:00"), TODAY)
    assert yesterday is not None
    assert (yesterday.text, yesterday.day) == ("きのう 15:40", "きのう")


def test_older_days_show_the_date_and_other_years_add_the_year():
    shown = list_time(utc("2026-11-14T10:40:00"), TODAY)
    assert shown is not None
    assert (shown.text, shown.day) == ("11/14 10:40", "11月14日（土）")
    old = list_time(utc("2025-12-31T23:59:00"), TODAY)
    assert old is not None
    assert (old.text, old.day) == ("2025/12/31 23:59", "2025年12月31日（水）")


def test_day_boundary_follows_the_local_timezone():
    """UTC では同じ日でも、日本時間の 0 時をまたげば別の日になる。"""
    before = list_time("2026-11-16T14:59:59+00:00", TODAY)
    after = list_time("2026-11-16T15:00:00+00:00", TODAY)
    assert before is not None
    assert after is not None
    assert (before.day, before.clock) == ("きのう", "23:59")
    assert (after.day, after.clock) == ("今日", "00:00")


def test_empty_value_has_no_time():
    assert list_time(None, TODAY) is None
    assert list_time("", TODAY) is None


def test_by_day_keeps_the_order_and_splits_at_each_new_day():
    rows = [
        {"name": "a", "when": list_time(utc("2026-11-17T09:00:00"), TODAY)},
        {"name": "b", "when": list_time(utc("2026-11-17T08:00:00"), TODAY)},
        {"name": "c", "when": list_time(utc("2026-11-16T22:00:00"), TODAY)},
        {"name": "d", "when": None},
    ]
    groups = by_day(rows)
    assert [(group["day"], [row["name"] for row in group["rows"]]) for group in groups] == [
        ("今日", ["a", "b"]),
        ("きのう", ["c"]),
        ("日時不明", ["d"]),
    ]


def page(server, path, cookie):
    status, _, body = exchange(server, "GET", path, headers=cookie, auth=False)
    assert status == 200, path
    return body.decode()


def ready_clip(server, **values):
    clips = server.app.clips
    clip_id = clips.request(**values)
    clips.update(clip_id, status="ready", path=f"clips/{clip_id}.mp4", duration=30)
    return clip_id


def test_review_list_is_split_by_day_with_short_times(gui):
    server, _, _ = gui
    ready_clip(server, source="manual")
    html = page(server, "/review", login(server, role="member"))
    assert re.search(r'<h3 class="day-heading">今日</h3>', html)
    assert re.search(
        r'<time datetime="[0-9T:+-]+" title="\d{4}/\d\d/\d\d [\d:]+">\d\d:\d\d</time>', html
    )
    assert not re.search(r">\d{4}/\d\d/\d\d \d\d:\d\d:\d\d<", html)


def test_clip_list_and_records_are_split_by_day(gui):
    server, store, _ = gui
    ready_clip(server, source="manual")
    store.add_excretion("feces", (datetime.now(UTC) - timedelta(days=1)).isoformat())
    cookie = login(server, role="member")
    clips = page(server, "/clips", cookie)
    nav = re.search(r'id="clip-list".*?</nav>', clips, re.S)[0]
    assert '<h3 class="day-heading">今日</h3>' in nav
    records = page(server, "/records", cookie)
    assert '<h3 class="day-heading">きのう</h3>' in records
    assert re.search(r'<time datetime="[^"]+" title="[^"]+">\d\d:\d\d</time>', records)


def test_home_and_event_list_name_the_day_in_each_row(gui):
    server, store, _ = gui
    store.add_excretion("urine", datetime.now(UTC).isoformat())
    cookie = login(server, role="member")
    home = page(server, "/", cookie)
    assert re.search(r'<time datetime="[^"]+" title="[^"]+">今日 \d\d:\d\d</time>', home)
    events = page(server, "/records/events", cookie)
    nav = re.search(r'aria-label="イベント一覧".*?</nav>', events, re.S)[0]
    assert re.search(r">今日 \d\d:\d\d</time>", nav)
