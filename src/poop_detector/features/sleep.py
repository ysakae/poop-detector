"""1秒ごとの在室と静止の観測から、1分ごとの睡眠の状態を決める。"""

from collections import Counter
from datetime import UTC, datetime
from typing import Literal

from .sleep_store import MinuteRecord, SleepState

Presence = Literal["still", "moving", "left", "unknown"]

# 1分のうち観測できた秒数がこれに満たなければ、その分は unknown にする。
MIN_OBSERVED_SECONDS = 30
# 秒数が同じ状態が並んだときに採る順。睡眠を多く数えすぎないよう、起きている側を先にする。
TIE_ORDER: tuple[SleepState, ...] = ("awake", "absent", "asleep")

# 静止が続いているあいだ、asleep か awake かがまだ決まっていない秒。
_Second = tuple[datetime, SleepState | None]


def _minute_of(stamp: datetime) -> datetime:
    return stamp.astimezone(UTC).replace(second=0, microsecond=0)


def _summarize(minute: datetime, states: list[SleepState]) -> MinuteRecord:
    counts = Counter(states)
    observed = len(states) - counts["unknown"]
    if observed < MIN_OBSERVED_SECONDS:
        return MinuteRecord(minute, "unknown", float(observed))
    best = max(TIE_ORDER, key=lambda state: (counts[state], -TIE_ORDER.index(state)))
    return MinuteRecord(minute, best, float(observed))


class SleepJudge:
    """秒ごとの観測を受け取り、確定した分の状態を返す状態機械。

    observe は1回で1秒分と数える（呼び出し側がおよそ毎秒呼ぶ）。静止が still_seconds 続くと、
    静止の始まりまでさかのぼって asleep にする。静止の途中の stir_seconds 以内の動きは静止の秒
    として数え、超えたら静止を途切れさせる。left・unknown・interrupt も静止を途切れさせる。
    分は、その分の秒がすべて確定し、かつ後の分が始まった（または close した）ときに返す。
    """

    def __init__(self, still_seconds: float, stir_seconds: float) -> None:
        if still_seconds <= 0:
            raise ValueError("静止とみなす秒数は0より大きくしてください")
        if stir_seconds < 0:
            raise ValueError("身じろぎとみなす秒数は0以上にしてください")
        self.still_seconds = still_seconds
        self.stir_seconds = stir_seconds
        self._seconds: list[_Second] = []
        # 末尾の未確定の秒の数。未確定の秒はいつも末尾に続けて並ぶ。
        self._pending = 0
        self._run_length = 0
        self._stir = 0
        self._asleep = False
        self._minute: datetime | None = None

    def observe(self, stamp: datetime, presence: Presence) -> list[MinuteRecord]:
        """1秒分の観測を加え、確定した分を返す。"""
        minute = _minute_of(stamp)
        # 時計が戻っても分の並びを崩さないよう、いまの分より前には戻さない。
        if self._minute is not None and minute < self._minute:
            minute = self._minute
        self._minute = minute
        if presence == "still":
            self._observe_still(minute)
        elif presence == "moving":
            self._observe_moving(minute)
        else:
            self._break()
            self._seconds.append((minute, "absent" if presence == "left" else "unknown"))
        return self._emit(final=False)

    def interrupt(self) -> None:
        """静止の継続を捨てる（接続の途切れ）。未確定の秒は awake になる。"""
        self._break()

    # close の時刻は呼び出し側の区切りを示すだけで、残りの分はすべて確定させるため使わない。
    def close(self, stamp: datetime) -> list[MinuteRecord]:  # noqa: ARG002
        """未確定の秒を awake として、残りの分をすべて返す。"""
        self._break()
        records = self._emit(final=True)
        self._minute = None
        return records

    def _observe_still(self, minute: datetime) -> None:
        self._stir = 0
        self._run_length += 1
        self._append_pending(minute)
        if self._asleep or self._run_length >= self.still_seconds:
            self._asleep = True
            self._resolve("asleep")

    def _observe_moving(self, minute: datetime) -> None:
        if self._run_length > 0 and self._stir + 1 <= self.stir_seconds:
            self._stir += 1
            self._run_length += 1
            self._append_pending(minute)
            return
        if self._run_length > 0:
            # 身じろぎが長すぎた。この動きの秒も含めて、未確定の秒を awake にする。
            self._append_pending(minute)
            self._break()
            return
        self._seconds.append((minute, "awake"))

    def _append_pending(self, minute: datetime) -> None:
        self._seconds.append((minute, None))
        self._pending += 1

    def _resolve(self, state: SleepState) -> None:
        if self._pending == 0:
            return
        head = self._seconds[: -self._pending]
        tail: list[_Second] = [(minute, state) for minute, _ in self._seconds[-self._pending :]]
        self._seconds = head + tail
        self._pending = 0

    def _break(self) -> None:
        self._resolve("awake")
        self._run_length = 0
        self._stir = 0
        self._asleep = False

    def _emit(self, *, final: bool) -> list[MinuteRecord]:
        records: list[MinuteRecord] = []
        settled = len(self._seconds) - self._pending
        start = 0
        while start < settled:
            minute = self._seconds[start][0]
            end = start
            while end < len(self._seconds) and self._seconds[end][0] == minute:
                end += 1
            if end > settled or (not final and minute == self._minute):
                break
            states = [state for _, state in self._seconds[start:end] if state is not None]
            records.append(_summarize(minute, states))
            start = end
        self._seconds = self._seconds[start:]
        return records
