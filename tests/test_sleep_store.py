"""睡眠の記録のストア（SleepStore）のテスト。"""

from datetime import UTC, datetime, timedelta

import pytest

from poop_detector.features.sleep_store import MinuteRecord, SleepStore
from poop_detector.records.database import events_database


def test_write_and_read_minutes(tmp_path):
    store = SleepStore(events_database(tmp_path))
    at = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
    store.write_minutes(
        [MinuteRecord(at, "asleep", 60), MinuteRecord(at + timedelta(minutes=1), "absent", 45)]
    )
    store.write_minutes([MinuteRecord(at, "awake", 50)])
    rows = store.minutes(at, at + timedelta(hours=1))
    assert [(r.state, r.observed_seconds) for r in rows] == [("awake", 50), ("absent", 45)]


def test_minutes_range_is_half_open_and_sorted(tmp_path):
    store = SleepStore(events_database(tmp_path))
    at = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
    store.write_minutes([MinuteRecord(at + timedelta(minutes=m), "asleep", 60) for m in (2, 0, 1)])
    rows = store.minutes(at, at + timedelta(minutes=2))
    assert [r.minute for r in rows] == [at, at + timedelta(minutes=1)]


def test_write_minutes_rejects_unknown_state(tmp_path):
    store = SleepStore(events_database(tmp_path))
    at = datetime(2026, 11, 20, 3, 0, tzinfo=UTC)
    with pytest.raises(ValueError, match="不正"):
        store.write_minutes([MinuteRecord(at, "dozing", 60)])


def test_detection_hours_accumulate(tmp_path):
    store = SleepStore(events_database(tmp_path))
    hour = datetime(2026, 11, 20, 3, tzinfo=UTC)
    store.add_detection(hour, 10, 4)
    store.add_detection(hour, 5, 5)
    assert store.detection_hours(hour, hour + timedelta(hours=1)) == {hour: (15, 9)}
