"""睡眠の画面の値（sleep_view）のテスト。テストは日本時間で動く。"""

from datetime import UTC, date, datetime, time, timedelta, timezone

import pytest

from poop_detector.features.sleep_store import MinuteRecord, SleepState, SleepStore
from poop_detector.gui.sleep_page import sleep_view
from poop_detector.records.database import events_database

JST = timezone(timedelta(hours=9))
DAY = date(2026, 11, 20)
DAYTIME = (time(9, 0), time(18, 0))


def put(store: SleepStore, day: date, hour: int, start: int, count: int, state: SleepState):
    first = datetime(day.year, day.month, day.day, hour, start, tzinfo=JST)
    store.write_minutes(
        [MinuteRecord(first + timedelta(minutes=i), state, 60) for i in range(count)]
    )


def fill_hour(store: SleepStore, day: date, hour: int, asleep: int) -> None:
    put(store, day, hour, 0, asleep, "asleep")
    if asleep < 60:
        put(store, day, hour, asleep, 60 - asleep, "awake")


def make(tmp_path) -> SleepStore:
    return SleepStore(events_database(tmp_path))


@pytest.mark.parametrize(
    ("asleep", "level"), [(0, 0), (10, 1), (20, 2), (40, 3), (50, 4), (15, 1), (16, 2), (46, 4)]
)
def test_levels_by_asleep_minutes(tmp_path, asleep, level):
    store = make(tmp_path)
    fill_hour(store, DAY, 3, asleep)
    view = sleep_view(store, DAY, 7, DAYTIME)
    cell = view.rows[3][-1]
    assert (cell.asleep_minutes, cell.observed_minutes, cell.level) == (asleep, 60, level)


def test_hour_with_mostly_unknown_is_no_record(tmp_path):
    store = make(tmp_path)
    put(store, DAY, 3, 0, 29, "asleep")
    put(store, DAY, 4, 0, 31, "asleep")
    view = sleep_view(store, DAY, 7, DAYTIME)
    assert view.rows[3][-1].level is None
    assert view.rows[4][-1].level == 3


def test_daytime_minutes_counted(tmp_path):
    store = make(tmp_path)
    fill_hour(store, DAY, 8, 60)
    fill_hour(store, DAY, 9, 30)
    fill_hour(store, DAY, 17, 10)
    fill_hour(store, DAY, 18, 60)
    summary = sleep_view(store, DAY, 7, DAYTIME).summaries[-1]
    assert summary.asleep_minutes == 160
    assert summary.daytime_minutes == 40


def test_days_and_rows_shape(tmp_path):
    view = sleep_view(make(tmp_path), DAY, 28, DAYTIME)
    assert view.days[0] == DAY - timedelta(days=27)
    assert view.days[-1] == DAY
    assert len(view.rows) == 24
    assert all(len(row) == 28 for row in view.rows)
    assert view.rows[5][2].day == view.days[2]
    assert view.rows[5][2].hour == 5


def test_period_must_be_known(tmp_path):
    with pytest.raises(ValueError, match="期間"):
        sleep_view(make(tmp_path), DAY, 10, DAYTIME)


def fill_day(store: SleepStore, day: date, asleep_per_hour: int) -> None:
    for hour in range(24):
        fill_hour(store, day, hour, asleep_per_hour)


def test_unreliable_day_excluded_from_trend(tmp_path):
    store = make(tmp_path)
    fill_day(store, DAY, 30)  # 直近: 720 分
    put(store, DAY - timedelta(days=1), 0, 0, 60, "asleep")  # 記録が足りない日
    fill_day(store, DAY - timedelta(days=10), 20)  # 普段: 480 分
    trend = sleep_view(store, DAY, 7, DAYTIME).trend
    assert trend.recent_minutes == 720
    assert trend.usual_minutes == 480
    assert trend.diff_minutes == 240
    assert trend.diff_ratio == pytest.approx(0.5)


def test_trend_reads_beyond_period(tmp_path):
    store = make(tmp_path)
    fill_day(store, DAY - timedelta(days=30), 10)
    fill_day(store, DAY, 10)
    trend = sleep_view(store, DAY, 7, DAYTIME).trend
    assert trend.usual_minutes == 240


def test_sleep_across_midnight_splits_into_days(tmp_path):
    store = make(tmp_path)
    prev = DAY - timedelta(days=1)
    put(store, prev, 23, 30, 30, "asleep")
    put(store, DAY, 0, 0, 30, "asleep")
    view = sleep_view(store, DAY, 7, DAYTIME)
    assert view.rows[23][-2].asleep_minutes == 30
    assert view.rows[0][-1].asleep_minutes == 30
    assert view.rows[23][-1].asleep_minutes == 0


def test_absent_counts_as_observed_awake(tmp_path):
    store = make(tmp_path)
    put(store, DAY, 3, 0, 60, "absent")
    cell = sleep_view(store, DAY, 7, DAYTIME).rows[3][-1]
    assert (cell.asleep_minutes, cell.observed_minutes, cell.level) == (0, 60, 0)


def test_detection_ratio_by_local_hour(tmp_path):
    store = make(tmp_path)
    store.add_detection(datetime(2026, 11, 19, 18, 0, tzinfo=UTC), 100, 25)  # 日本時間3時
    store.add_detection(datetime(2026, 11, 19, 19, 0, tzinfo=UTC), 0, 0)
    view = sleep_view(store, DAY, 7, DAYTIME)
    assert view.rows[3][-1].detection_ratio == 0.25
    assert view.rows[4][-1].detection_ratio is None
    assert view.rows[5][-1].detection_ratio is None


def test_empty_period_renders(tmp_path):
    view = sleep_view(make(tmp_path), DAY, 91, DAYTIME)
    assert len(view.days) == 91
    assert all(cell.level is None for row in view.rows for cell in row)
    assert all(not s.reliable and s.asleep_minutes == 0 for s in view.summaries)
    assert (view.trend.recent_minutes, view.trend.usual_minutes) == (None, None)
    assert (view.trend.diff_minutes, view.trend.diff_ratio) == (None, None)
    assert view.period == 91


# ---- 画面（/sleep） ----


def _seed(server, days: int = 10) -> None:
    from poop_detector.timezones import local_today

    store = server.app.sleep
    today = local_today()
    for back in range(days):
        day = today - timedelta(days=back)
        for hour in range(24):
            fill_hour(store, day, hour, 50 if hour < 6 else 10)


def _page(server, query: str = "") -> str:
    from support import request

    status, body = request(server, "GET", "/sleep" + query)
    assert status == 200
    return body.decode()


def test_sleep_page_shows_grid_and_totals(gui):
    server, _, _ = gui
    _seed(server)
    html = _page(server)
    assert html.count('class="sleep-row"') == 24
    assert "少ない" in html
    assert "多い" in html
    assert "記録なし" in html
    assert "sleep-l4" in html
    # 6時間×50分＋18時間×10分 = 8時間0分
    assert "8時間0分" in html
    assert "直近7日の平均 8時間0分" in html
    assert "睡眠 50分／記録 60分" in html
    assert "睡眠の記録がありません" not in html


def test_sleep_page_empty_state(gui):
    server, _, _ = gui
    assert "睡眠の記録がありません" in _page(server)


@pytest.mark.parametrize("query", ["?days=5", "?days=abc", "?days=", ""])
def test_sleep_page_invalid_days_falls_back_to_28(gui, query):
    server, _, _ = gui
    html = _page(server, query)
    assert 'data-period="28"' in html


@pytest.mark.parametrize("days", ["7", "28", "91"])
def test_sleep_page_accepts_periods(gui, days):
    server, _, _ = gui
    _seed(server, 3)
    html = _page(server, f"?days={days}")
    assert f'data-period="{days}"' in html
    assert html.count('class="sleep-row"') == 24


def test_sleep_page_requires_login(gui):
    from support import request

    server, _, _ = gui
    assert request(server, "GET", "/sleep", auth=False)[0] == 403


def test_sleep_page_is_visible_to_members(gui):
    from support import exchange, login

    server, _, _ = gui
    status, _, body = exchange(
        server, "GET", "/sleep", headers=login(server, "家族", role="member"), auth=False
    )
    assert status == 200
    assert 'href="/sleep"' in body.decode()


def test_duration_and_trend_text():
    from poop_detector.gui.sleep_page import duration_text, signed_duration_text

    assert duration_text(750) == "12時間30分"
    assert duration_text(45) == "45分"
    assert duration_text(120) == "2時間0分"
    assert duration_text(0) == "0分"
    assert signed_duration_text(40) == "+40分"
    assert signed_duration_text(-65) == "-1時間5分"
