"""監視の停止とカメラの不調の履歴（outages テーブル）と、通知の重複を防ぐ状態遷移。

停止は種類（monitor・camera）ごとに続いている行を1つまで持つ（部分一意インデックス）。
同じデータを2つの見張りが見ていても、行を作った側だけが停止を知らせ、行を閉じた側だけが
復旧を知らせるので、同じ停止で通知を繰り返さない。
"""

import sqlite3
import time
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Literal, Protocol, get_args

from ..records.database import Database

OutageKind = Literal["monitor", "camera"]
# 画面に出す種類の名前。
OUTAGE_KINDS: dict[str, str] = {"monitor": "監視", "camera": "カメラ"}


def utc_stamp(epoch: float) -> str:
    """UNIX 時刻を、保存用の UTC の ISO 8601 にする。"""
    return datetime.fromtimestamp(epoch, UTC).isoformat()


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def format_duration(seconds: float) -> str:
    """停止の長さを「約2時間5分」のように丸めて書く。"""
    minutes = int(seconds // 60)
    if minutes < 1:
        return "1分未満"
    days, rest = divmod(minutes, 24 * 60)
    hours, minutes = divmod(rest, 60)
    if days:
        return f"約{days}日{hours}時間" if hours else f"約{days}日"
    if hours:
        return f"約{hours}時間{minutes}分" if minutes else f"約{hours}時間"
    return f"約{minutes}分"


@dataclass(frozen=True)
class Outage:
    """1回の停止。ended_at が None なら続いている。notified_at は停止を知らせた時刻。"""

    id: int
    kind: str
    started_at: str
    notified_at: str | None
    ended_at: str | None

    def seconds(self, now: float | None = None) -> float:
        """停止の長さ（秒）。続いている停止は now（既定は現在）までの長さ。"""
        end = _epoch(self.ended_at) if self.ended_at else (time.time() if now is None else now)
        return max(0.0, end - _epoch(self.started_at))


class OutageLog:
    """outages テーブル。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    @staticmethod
    def _outage(row: sqlite3.Row) -> Outage:
        return Outage(
            row["id"], row["kind"], row["started_at"], row["notified_at"], row["ended_at"]
        )

    def begin(self, kind: str, started_at: str) -> tuple[Outage, bool]:
        """停止を記録し、(停止, 新しく作ったか) を返す。続いている停止があればそれを返す。"""
        if kind not in get_args(OutageKind):
            raise ValueError("停止の種類が不正です")
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM outages WHERE kind = ? AND ended_at IS NULL", (kind,)
            ).fetchone()
            if row is not None:
                return self._outage(row), False
            row = db.execute(
                "INSERT INTO outages (kind, started_at) VALUES (?, ?) RETURNING *",
                (kind, started_at),
            ).fetchone()
            return self._outage(row), True

    def current(self, kind: str) -> Outage | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM outages WHERE kind = ? AND ended_at IS NULL", (kind,)
            ).fetchone()
        return self._outage(row) if row else None

    def mark_notified(self, outage_id: int, at: str) -> None:
        with self.database.connect() as db:
            db.execute("UPDATE outages SET notified_at = ? WHERE id = ?", (at, outage_id))

    def end(self, outage_id: int, ended_at: str) -> Outage | None:
        """続いている停止を閉じ、閉じた停止を返す。ほかの見張りが閉じていれば None。"""
        with self.database.connect() as db:
            row = db.execute(
                "UPDATE outages SET ended_at = ? WHERE id = ? AND ended_at IS NULL RETURNING *",
                (ended_at, outage_id),
            ).fetchone()
        return self._outage(row) if row else None

    def close_open(self, kind: str, ended_at: str) -> None:
        """前回の監視が残した、続いたままの停止を閉じる（知らせない）。"""
        with self.database.connect() as db:
            db.execute(
                "UPDATE outages SET ended_at = ? WHERE kind = ? AND ended_at IS NULL",
                (ended_at, kind),
            )

    def recent(self, limit: int = 10) -> list[Outage]:
        """新しい順の停止。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM outages ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._outage(row) for row in rows]


class OutageAlerts(Protocol):
    """停止と復旧の通知（notifications.ToiletNotifier が実装する）。"""

    def send_outage(self, kind: str, since: datetime) -> bool: ...
    def send_recovery(self, kind: str, seconds: float) -> bool: ...


class OutageTracker:
    """1種類の停止を見張る。threshold 秒続いたら一度だけ知らせ、戻ったら長さと合わせて知らせる。

    時刻はすべて UNIX 時刻（秒）で受け取る。データベースに触れるのは、停止の始まりと終わりと、
    送れなかった通知の再送のときだけ（毎フレーム呼んでもよい）。
    """

    # 停止の通知を送れなかったとき（ネットワークの不調など）に、もう一度送るまでの秒数。
    RETRY_SECONDS = 300.0

    def __init__(
        self,
        log: OutageLog,
        kind: OutageKind,
        threshold: float,
        alerts: OutageAlerts,
    ) -> None:
        self.log, self.kind, self.threshold, self.alerts = log, kind, threshold, alerts
        # 続いている停止。GUI を開き直したときなどは、前の見張りが記録した停止を引き継ぐ。
        self.current = log.current(kind)
        # この見張りが作った停止なら、送れなかった通知を再送する。
        self.owner = False
        self.retry_at = float("-inf")

    def observe(self, down_since: float | None, now: float) -> None:
        """down_since は不調が始まった時刻。正常なら None。"""
        if down_since is None:
            self._recover(now)
        elif self.current is None:
            if now - down_since >= self.threshold:
                self.current, self.owner = self.log.begin(self.kind, utc_stamp(down_since))
                if self.owner:
                    self._alert(self.current, now)
        elif self.owner and self.current.notified_at is None and now >= self.retry_at:
            self._alert(self.current, now)

    def _alert(self, outage: Outage, now: float) -> None:
        since = datetime.fromisoformat(outage.started_at)
        if self.alerts.send_outage(self.kind, since):
            stamp = utc_stamp(now)
            self.log.mark_notified(outage.id, stamp)
            self.current = replace(outage, notified_at=stamp)
        else:
            self.retry_at = now + self.RETRY_SECONDS

    def _recover(self, now: float) -> None:
        if self.current is None:
            return
        ended = self.log.end(self.current.id, utc_stamp(now))
        self.current, self.owner = None, False
        # 停止を知らせた場合だけ、復旧も知らせる。
        if ended is not None and ended.notified_at is not None:
            self.alerts.send_recovery(self.kind, ended.seconds())
