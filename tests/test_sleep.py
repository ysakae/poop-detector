"""睡眠の判定（SleepJudge）のテスト。"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone

import pytest

from poop_detector.features.sleep import Presence, SleepJudge
from poop_detector.features.sleep_store import MinuteRecord

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
