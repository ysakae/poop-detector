"""確定記録の CSV の書き出しと、期間の集計（#90）を検証する。"""

import csv
import io
from datetime import date

import pytest

from poop_detector.errors import InvalidInputError
from poop_detector.records.events import Store
from poop_detector.records.export import (
    CSV_HEADER,
    parse_period,
    records_csv,
    summarize_period,
)


def rows(data):
    text = data.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path)
    # 日本時間 2026-11-01 08:30（UTC では前日の 23:30）
    store.add_excretion(
        "feces", "2026-10-31T23:30:00+00:00", "2026-10-31T23:31:30+00:00", "やや軟らかい"
    )
    store.add_excretion("urine", "2026-11-02T12:00:00+09:00", location="outside")
    store.add_excretion("both", "2026-11-02T21:15:00+09:00", missed=True, location="tray")
    # 期間の外
    store.add_excretion("urine", "2026-10-31T10:00:00+09:00")
    return store


def test_csv_lists_confirmed_records_of_the_period_in_local_time(store):
    data = records_csv(store.records(), date(2026, 11, 1), date(2026, 11, 2))
    assert data.startswith("﻿".encode())
    assert b"\r\n" in data
    table = rows(data)
    assert tuple(table[0]) == CSV_HEADER
    assert len(table) == 4
    first = dict(zip(CSV_HEADER, table[1], strict=True))
    assert first["日付"] == "2026-11-01"
    assert first["開始"] == "2026-11-01T08:30:00+09:00"
    assert first["完了"] == "2026-11-01T08:31:30+09:00"
    assert first["所要（秒）"] == "90"
    assert first["種類"] == "排便"
    assert first["場所"] == ""
    assert first["メモ"] == "やや軟らかい"
    assert first["記録の由来"] == "手入力"
    second = dict(zip(CSV_HEADER, table[2], strict=True))
    assert (second["種類"], second["場所"], second["完了"]) == ("排尿", "トレー外", "")
    third = dict(zip(CSV_HEADER, table[3], strict=True))
    assert (third["種類"], third["記録の由来"]) == ("排便・排尿", "見逃し")


def test_csv_neutralizes_cells_that_spreadsheets_would_run_as_formulas(tmp_path):
    store = Store(tmp_path)
    store.add_excretion("feces", "2026-11-01T08:00:00+09:00", note='=HYPERLINK("x")')
    store.add_excretion("feces", "2026-11-01T09:00:00+09:00", note="-1+2\n2行目")
    table = rows(records_csv(store.records(), date(2026, 11, 1), date(2026, 11, 1)))
    notes = [dict(zip(CSV_HEADER, row, strict=True))["メモ"] for row in table[1:]]
    assert notes == ['\'=HYPERLINK("x")', "'-1+2\n2行目"]


def test_csv_without_records_has_only_the_header(tmp_path):
    table = rows(records_csv(Store(tmp_path).records(), date(2026, 1, 1), date(2026, 1, 31)))
    assert table == [list(CSV_HEADER)]


def test_summary_counts_kinds_locations_hours_and_days(store):
    summary = summarize_period(store.records(), date(2026, 11, 1), date(2026, 11, 3))
    assert summary.total == 3
    assert summary.days == 3
    assert summary.recorded_days == 2
    # 排便・排尿は両方に数える
    assert (summary.feces, summary.urine, summary.unspecified) == (2, 2, 0)
    assert summary.locations == {"トレー内": 1, "トレー外": 1, "記録なし": 1}
    hours = {row.hour: (row.feces, row.urine, row.unspecified) for row in summary.hourly}
    assert len(summary.hourly) == 24
    assert hours[8] == (1, 0, 0)
    assert hours[12] == (0, 1, 0)
    assert hours[21] == (1, 1, 0)
    daily = {row.day: (row.total, row.feces, row.urine) for row in summary.daily}
    assert daily == {
        date(2026, 11, 1): (1, 1, 0),
        date(2026, 11, 2): (2, 1, 2),
        date(2026, 11, 3): (0, 0, 0),
    }
    assert summary.per_day == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("start", "end", "expected"),
    [
        ("2026-11-01", "2026-11-30", (date(2026, 11, 1), date(2026, 11, 30))),
        ("", "", (date(2026, 10, 14), date(2026, 11, 12))),
    ],
)
def test_parse_period_defaults_to_the_last_30_days(start, end, expected):
    assert parse_period(start, end, today=date(2026, 11, 12)) == expected


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ("2026-11-30", "2026-11-01"),
        ("11/01", "2026-11-30"),
        ("2000-01-01", "2026-11-30"),
    ],
)
def test_parse_period_rejects_reversed_malformed_or_too_long_periods(start, end):
    with pytest.raises(InvalidInputError):
        parse_period(start, end, today=date(2026, 11, 12))
