"""確定した排泄記録を期間と件数で絞って読むことを確かめる（テストは日本時間）。"""

from datetime import UTC, date, datetime

import numpy as np

from poop_detector.records.events import Store


def _suspected(store: Store, timestamp: str, start: str | None, label: str = "feces") -> str:
    """差分検知の記録を作り、検知の時刻と実際の開始を指定して判定する。"""
    event_id = store.record(np.zeros((4, 4, 3), np.uint8), 0.1, None, 1)
    with store.connect() as db:
        db.execute("UPDATE events SET timestamp=? WHERE id=?", (timestamp, event_id))
    store.classify(event_id, label, start, None)
    return event_id


def test_records_between_uses_local_day_boundaries(tmp_path):
    store = Store(tmp_path)
    # 日本時間 10/31 23:59（前日）と 11/1 00:00（当日）、11/1 23:59（当日）と 11/2 00:00（翌日）。
    before = store.add_excretion("feces", "2026-10-31T14:59:00+00:00")
    first = store.add_excretion("urine", "2026-10-31T15:00:00+00:00")
    last = store.add_excretion("both", "2026-11-01T14:59:59+00:00")
    after = store.add_excretion("feces", "2026-11-01T15:00:00+00:00")

    rows = store.records_between(date(2026, 11, 1), date(2026, 11, 1))

    assert [row["id"] for row in rows] == [first, last]
    assert {before, after}.isdisjoint(row["id"] for row in rows)


def test_records_between_prefers_actual_start_over_detection_time(tmp_path):
    store = Store(tmp_path)
    # 検知は当日だが、実際の開始は前日。開始のない記録は検知の時刻で数える。
    moved = _suspected(store, "2026-11-01T01:00:00+00:00", "2026-10-31T14:00:00+00:00")
    detected = _suspected(store, "2026-11-01T02:00:00+00:00", None)

    assert [r["id"] for r in store.records_between(date(2026, 11, 1), date(2026, 11, 1))] == [
        detected
    ]
    assert [r["id"] for r in store.records_between(date(2026, 10, 31), date(2026, 10, 31))] == [
        moved
    ]


def test_records_between_skips_unconfirmed_and_matches_records(tmp_path):
    store = Store(tmp_path)
    _suspected(store, "2026-11-01T01:00:00+00:00", None, label="false")
    _suspected(store, "2026-11-01T02:00:00+00:00", None, label="unknown")
    store.add_excretion("feces", "2026-11-03T01:00:00+00:00", note="朝")
    store.add_excretion("urine", "2026-11-01T03:00:00+00:00")
    _suspected(store, "2026-11-02T05:00:00+00:00", "2026-11-02T04:00:00+00:00")

    rows = store.records_between(date(2026, 11, 1), date(2026, 11, 3))

    # 未確定は除き、全件の records と同じ並び・同じ中身になる。
    assert rows == store.records()
    assert [row["excretion_type"] for row in rows] == ["urine", "feces", "feces"]
    assert rows[-1]["note"] == "朝"


def test_latest_records_returns_the_newest_confirmed_in_order(tmp_path):
    store = Store(tmp_path)
    for day in range(1, 8):
        store.add_excretion("feces", f"2026-11-0{day}T01:00:00+00:00")
    # 検知は最後だが、実際の開始は最初。未確定は数えない。
    _suspected(store, "2026-11-09T01:00:00+00:00", "2026-10-30T01:00:00+00:00")
    _suspected(store, "2026-11-10T01:00:00+00:00", None, label="false")

    assert store.latest_records(3) == store.records()[-3:]
    assert store.latest_records(100) == store.records()
    assert store.latest_records(0) == []


def test_records_index_is_used_for_period_queries(tmp_path):
    store = Store(tmp_path)
    with store.connect() as db:
        # 式はテストの外から入らない定数だけ。
        query = f"EXPLAIN QUERY PLAN SELECT id FROM events WHERE feedback=1 AND {Store.RECORD_START} >= julianday(?)"  # noqa: S608
        plan = db.execute(
            query,
            ("2026-11-01T00:00:00+00:00",),
        ).fetchall()
    assert any("events_records_by_start" in row["detail"] for row in plan)


def test_last_record_of_kind_skips_future_and_other_kinds(tmp_path):
    store = Store(tmp_path)
    feces = store.add_excretion("feces", "2026-11-01T01:00:00+00:00")
    both = store.add_excretion("both", "2026-11-02T01:00:00+00:00")
    store.add_excretion("urine", "2026-11-03T01:00:00+00:00")
    store.add_excretion("feces", "2026-11-05T01:00:00+00:00")
    _suspected(store, "2026-11-04T01:00:00+00:00", None, label="false")
    now = datetime(2026, 11, 4, 12, tzinfo=UTC)

    # 未来の記録と未確定は除き、排便・排尿の両方は排便にも数える。
    assert store.last_record(("feces", "both"), now)["id"] == both
    assert store.last_record(("feces",), datetime(2026, 11, 1, 2, tzinfo=UTC))["id"] == feces
    assert store.last_record(("feces",), datetime(2026, 10, 1, tzinfo=UTC)) is None
