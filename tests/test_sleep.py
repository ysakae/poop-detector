"""睡眠の判定（SleepJudge）と記録（SleepRecorder）のテスト。"""

import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone

import numpy as np
import pytest

from poop_detector.features.dog import Box, Predict
from poop_detector.features.sleep import Presence, SleepJudge, SleepRecorder
from poop_detector.features.sleep_motion import MotionSampler
from poop_detector.features.sleep_store import MinuteRecord, SleepStore
from poop_detector.frames import Frame
from poop_detector.records.database import events_database

START = datetime(2026, 11, 20, 0, 0, tzinfo=UTC)


class Feeder:
    """秒ごとに observe を呼び、確定した分を集める補助。"""

    def __init__(self, judge: SleepJudge, start: datetime = START) -> None:
        self.judge = judge
        self.now = start
        self.records: list[MinuteRecord] = []

    def feed(self, presence: Presence, seconds: int) -> list[MinuteRecord]:
        emitted: list[MinuteRecord] = []
        for _ in range(seconds):
            emitted.extend(self.judge.observe(self.now, presence))
            self.now += timedelta(seconds=1)
        self.records.extend(emitted)
        return emitted

    def close(self) -> list[MinuteRecord]:
        emitted = self.judge.close(self.now)
        self.records.extend(emitted)
        return emitted


def judge() -> SleepJudge:
    return SleepJudge(still_seconds=300, stir_seconds=10)


def summary(records: Sequence[MinuteRecord]) -> list[tuple[int, str, float]]:
    return [
        (int((r.minute - START).total_seconds() // 60), r.state, r.observed_seconds)
        for r in records
    ]


def test_still_for_five_minutes_becomes_asleep_from_start():
    feeder = Feeder(judge())
    feeder.feed("still", 6 * 60)
    # 6分目はまだ終わっていないので、確定するのは0〜4分の5分
    assert summary(feeder.records) == [(m, "asleep", 60) for m in range(5)]
    assert summary(feeder.close()) == [(5, "asleep", 60)]


def test_minutes_wait_until_stillness_is_decided():
    feeder = Feeder(judge())
    # 静止が5分に届くまでは、どの分もさかのぼって asleep になりうるので返さない
    assert feeder.feed("still", 299) == []
    assert summary(feeder.feed("still", 1)) == [(m, "asleep", 60) for m in range(4)]


def test_short_stillness_is_awake():
    feeder = Feeder(judge())
    feeder.feed("still", 4 * 60)
    feeder.feed("moving", 60)
    assert summary(feeder.records) == [(m, "awake", 60) for m in range(4)]
    assert summary(feeder.close()) == [(4, "awake", 60)]


def test_stir_within_ten_seconds_keeps_sleeping():
    feeder = Feeder(judge())
    feeder.feed("still", 3 * 60)
    feeder.feed("moving", 8)
    feeder.feed("still", 3 * 60)
    # moving の8秒を含む3分目も asleep。6分目（8秒だけ）は観測が30秒に満たない
    assert summary(feeder.records) == [(m, "asleep", 60) for m in range(6)]
    assert summary(feeder.close()) == [(6, "unknown", 8)]


@pytest.mark.parametrize(
    ("stir", "expected"),
    [(10, "asleep"), (11, "awake")],
)
def test_stir_longer_than_ten_seconds_breaks_stillness(stir, expected):
    feeder = Feeder(judge())
    feeder.feed("still", 3 * 60)
    feeder.feed("moving", stir)
    # 合わせて5分ちょうどになるように静止を続ける
    feeder.feed("still", 2 * 60 - stir)
    feeder.close()
    assert summary(feeder.records) == [(m, expected, 60) for m in range(5)]


def test_long_stir_after_sleep_keeps_earlier_sleep():
    feeder = Feeder(judge())
    feeder.feed("still", 6 * 60)
    feeder.feed("moving", 60)
    feeder.close()
    assert summary(feeder.records) == [(m, "asleep", 60) for m in range(6)] + [(6, "awake", 60)]


def test_short_stir_after_sleep_stays_asleep():
    feeder = Feeder(judge())
    feeder.feed("still", 6 * 60)
    feeder.feed("moving", 10)
    feeder.feed("still", 50)
    feeder.close()
    assert summary(feeder.records) == [(m, "asleep", 60) for m in range(7)]


def test_left_is_absent():
    feeder = Feeder(judge())
    feeder.feed("still", 2 * 60)
    feeder.feed("left", 2 * 60)
    feeder.close()
    # 短い静止は awake、出ていった秒は absent
    assert summary(feeder.records) == [
        (0, "awake", 60),
        (1, "awake", 60),
        (2, "absent", 60),
        (3, "absent", 60),
    ]


def test_left_after_sleep_keeps_sleep():
    feeder = Feeder(judge())
    feeder.feed("still", 5 * 60)
    feeder.feed("left", 60)
    feeder.close()
    assert summary(feeder.records) == [(m, "asleep", 60) for m in range(5)] + [(5, "absent", 60)]


@pytest.mark.parametrize(
    ("observed", "expected"),
    [(29, "unknown"), (30, "awake")],
)
def test_less_than_thirty_observed_seconds_is_unknown(observed, expected):
    feeder = Feeder(judge())
    feeder.feed("moving", observed)
    feeder.feed("unknown", 60 - observed)
    feeder.close()
    assert summary(feeder.records) == [(0, expected, observed)]


def test_majority_decides_mixed_minute():
    feeder = Feeder(judge())
    feeder.feed("left", 35)
    feeder.feed("moving", 25)
    feeder.close()
    assert summary(feeder.records) == [(0, "absent", 60)]


def test_unknown_breaks_stillness():
    feeder = Feeder(judge())
    feeder.feed("still", 3 * 60)
    feeder.feed("unknown", 60)
    feeder.feed("still", 3 * 60)
    feeder.close()
    assert summary(feeder.records) == [(m, "awake", 60) for m in range(3)] + [(3, "unknown", 0)] + [
        (m, "awake", 60) for m in range(4, 7)
    ]


def test_interrupt_discards_stillness():
    feeder = Feeder(judge())
    feeder.feed("still", 3 * 60)
    feeder.judge.interrupt()
    feeder.feed("still", 3 * 60)
    feeder.close()
    assert summary(feeder.records) == [(m, "awake", 60) for m in range(6)]


def test_close_returns_pending_minutes_as_awake():
    feeder = Feeder(judge())
    feeder.feed("still", 2 * 60)
    assert feeder.records == []
    assert summary(feeder.close()) == [(0, "awake", 60), (1, "awake", 60)]
    # 閉じたあとは何も残らない
    assert feeder.close() == []


def test_minutes_are_split_in_utc():
    jst = timezone(timedelta(hours=9))
    feeder = Feeder(judge(), start=datetime(2026, 11, 20, 9, 0, 30, tzinfo=jst))
    feeder.feed("moving", 60)
    feeder.close()
    assert [(r.minute, r.minute.tzinfo, r.observed_seconds) for r in feeder.records] == [
        (START, UTC, 30),
        (START + timedelta(minutes=1), UTC, 30),
    ]


@pytest.mark.parametrize(("still", "stir"), [(0, 10), (300, -1)])
def test_rejects_invalid_thresholds(still, stir):
    with pytest.raises(ValueError, match="秒"):
        SleepJudge(still_seconds=still, stir_seconds=stir)


def test_alternating_short_still_and_stir_never_sleeps():
    feeder = Feeder(judge())
    for _ in range(600 // 11 + 1):
        feeder.feed("still", 1)
        feeder.feed("moving", 10)
    feeder.close()
    assert feeder.records
    assert all(r.state != "asleep" for r in feeder.records)


def test_stirs_separated_by_a_minute_of_stillness_keep_sleeping():
    feeder = Feeder(judge())
    feeder.feed("still", 5 * 60)
    feeder.feed("moving", 8)
    feeder.feed("still", 60)
    feeder.feed("moving", 8)
    feeder.feed("still", 44)
    feeder.close()
    assert summary(feeder.records) == [(m, "asleep", 60) for m in range(7)]


def test_stirs_close_together_add_up_and_break_stillness():
    feeder = Feeder(judge())
    feeder.feed("still", 3 * 60)
    feeder.feed("moving", 6)
    feeder.feed("still", 20)
    feeder.feed("moving", 6)
    feeder.feed("still", 88)
    feeder.close()
    # 20秒の静止では身じろぎの枠が戻らず、6秒+6秒が10秒を超えて静止が途切れる
    assert summary(feeder.records) == [(m, "awake", 60) for m in range(5)]


def test_interrupt_after_sleep_keeps_earlier_sleep():
    feeder = Feeder(judge())
    feeder.feed("still", 6 * 60)
    feeder.judge.interrupt()
    feeder.feed("still", 60)
    feeder.close()
    assert summary(feeder.records) == [(m, "asleep", 60) for m in range(6)] + [(6, "awake", 60)]


def test_unknown_after_sleep_keeps_earlier_sleep():
    feeder = Feeder(judge())
    feeder.feed("still", 5 * 60)
    feeder.feed("unknown", 60)
    feeder.feed("still", 60)
    feeder.close()
    assert summary(feeder.records) == [(m, "asleep", 60) for m in range(5)] + [
        (5, "unknown", 0),
        (6, "awake", 60),
    ]


# --- SleepRecorder ---


class Ticker:
    """SleepRecorder.update を1秒ごとに呼ぶ補助。"""

    def __init__(self, recorder: SleepRecorder, start: datetime = START) -> None:
        self.recorder = recorder
        self.start = start
        self.tick = 0

    def run(self, seconds: int, predict: Predict, frame: Frame | None = None) -> None:
        image = still_frame() if frame is None else frame
        for _ in range(seconds):
            stamp = self.start + timedelta(seconds=self.tick)
            self.recorder.update(image, predict, float(self.tick), stamp)
            self.tick += 1

    def close(self) -> None:
        self.recorder.close(self.start + timedelta(seconds=self.tick))


DOG_BOX: Box = (60, 50, 90, 70)


def still_frame() -> np.ndarray:
    frame = np.zeros((120, 160, 3), dtype=np.uint8)
    frame[50:70, 60:90] = 255
    return frame


def always_dog(frame: Frame) -> list[Box]:
    return [DOG_BOX]


def recorder(tmp_path) -> tuple[SleepRecorder, SleepStore]:
    store = SleepStore(events_database(tmp_path))
    return SleepRecorder(store, judge(), MotionSampler(None)), store


def stored(store: SleepStore) -> list[tuple[int, str, float]]:
    return summary(store.minutes(START, START + timedelta(days=1)))


def test_recorder_writes_asleep_minutes(tmp_path):
    rec, store = recorder(tmp_path)
    Ticker(rec).run(6 * 60, always_dog)
    # 最初の1秒は比べる前の画像がないので moving、残りは静止。6分目は判定中で書かない
    assert stored(store) == [(m, "asleep", 60) for m in range(5)]


def test_close_flushes_pending_as_awake(tmp_path):
    rec, store = recorder(tmp_path)
    ticker = Ticker(rec)
    ticker.run(2 * 60, always_dog)
    assert stored(store) == []
    ticker.close()
    assert stored(store) == [(0, "awake", 60), (1, "awake", 60)]


def test_update_samples_at_most_once_per_second(tmp_path):
    rec, store = recorder(tmp_path)
    for tick in (0.0, 0.4, 0.9, 1.0, 1.5):
        rec.update(still_frame(), always_dog, tick, START)
    hours = store.detection_hours(START, START + timedelta(hours=1))
    assert hours == {START: (2.0, 2.0)}


def test_predict_error_is_unknown_and_logged(tmp_path, caplog):
    rec, store = recorder(tmp_path)

    def broken(frame: Frame) -> list[Box]:
        raise ValueError("推論の失敗")

    ticker = Ticker(rec)
    with caplog.at_level(logging.WARNING):
        ticker.run(60, broken)
    ticker.close()
    assert "犬検出に失敗" in caplog.text
    assert stored(store) == [(0, "unknown", 0)]
    assert store.detection_hours(START, START + timedelta(hours=1)) == {}


def test_detection_ratio_is_recorded(tmp_path):
    rec, store = recorder(tmp_path)

    def every_other(frame: Frame) -> list[Box]:
        every_other.calls += 1  # type: ignore[attr-defined]
        return [DOG_BOX] if every_other.calls % 2 == 1 else []  # type: ignore[attr-defined]

    every_other.calls = 0  # type: ignore[attr-defined]
    Ticker(rec).run(10, every_other)
    # 検出が外れても床面が静かなので居場所を保ち、10秒ともサークル内とみなす
    hours = store.detection_hours(START, START + timedelta(hours=1))
    assert hours == {START: (10.0, 5.0)}


def test_detection_hour_uses_local_hour_start(tmp_path):
    rec, store = recorder(tmp_path)
    local = timezone(timedelta(hours=5, minutes=30))
    stamp = datetime(2026, 11, 20, 3, 50, tzinfo=local)
    rec.update(still_frame(), always_dog, 0.0, stamp)
    hours = store.detection_hours(START - timedelta(days=1), START + timedelta(days=1))
    # テストのプロセスは日本時間なので、時の始まりは日本時間の 7:00（UTC の前日 22:00）
    assert list(hours) == [datetime(2026, 11, 19, 22, 0, tzinfo=UTC)]


def test_interrupt_breaks_stillness_and_forgets_location(tmp_path):
    rec, store = recorder(tmp_path)
    ticker = Ticker(rec)
    ticker.run(4 * 60, always_dog)
    rec.interrupt()
    ticker.run(2 * 60, always_dog)
    ticker.close()
    # 中断の前の4分は静止が5分に届かないので awake。中断で静止の継続は始めからやり直す
    assert stored(store) == [(m, "awake", 60) for m in range(6)]


def test_sqlite_error_is_logged_and_does_not_stop(tmp_path, caplog):
    class BrokenStore(SleepStore):
        def write_minutes(self, records: Sequence[MinuteRecord]) -> None:
            raise sqlite3.OperationalError("database is locked")

        def add_detection(
            self, hour: datetime, present_seconds: float, detected_seconds: float
        ) -> None:
            raise sqlite3.OperationalError("database is locked")

    rec = SleepRecorder(BrokenStore(events_database(tmp_path)), judge(), MotionSampler(None))
    ticker = Ticker(rec)
    with caplog.at_level(logging.WARNING):
        ticker.run(2 * 60, always_dog)
        ticker.close()
    assert "睡眠の記録を保存できませんでした" in caplog.text
