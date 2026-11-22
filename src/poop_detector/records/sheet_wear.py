"""シートの汚れの積み重なりを記録し、前回の交換からの使用の度合いを求める。"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, TypeAlias

from .database import Database
from .events import utcnow

# event: 排泄候補を記録した、settled: 清掃・乾燥で通知済み状態を更新した。
WearKind: TypeAlias = Literal["event", "settled"]
# auto: きれいなシートに戻った、scene: 画角かトレーが変わった。
ResetSource: TypeAlias = Literal["auto", "scene"]


@dataclass(frozen=True)
class SheetWear:
    """前回の交換からの排泄の回数と、いまの汚れの割合（監視範囲に占める差分画素の割合）。

    ratio は汚れの面積の目安で、確率でも尿の量でもない。
    """

    since: datetime | None
    events: int
    ratio: float
    notified: bool
    # 監視が一度でも汚れを測ったか（測っていなければ画面に出さない）。
    measured: bool = False

    def due(self, max_events: int, max_ratio: float) -> bool:
        """回数か汚れの割合が上限に達したか。上限が 0 の目安は使わない。"""
        by_events = max_events > 0 and self.events >= max_events
        by_ratio = max_ratio > 0 and self.ratio >= max_ratio
        return by_events or by_ratio


class SheetWearLog:
    """sheet_wear テーブルの台帳。交換はシート交換のお世話の記録（care_entries）でも数え直す。"""

    def __init__(self, database: Database) -> None:
        self.database = database

    def record(self, signature: str, ratio: float, kind: WearKind) -> None:
        """汚れの割合を1行足す。前の行と画角が違えば、先に数え直しの行を入れる。"""
        with self.database.connect() as db:
            last = db.execute(
                "SELECT scene_signature FROM sheet_wear ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if last is not None and last["scene_signature"] != signature:
                _insert(db, signature, "reset", None, "scene")
            _insert(db, signature, kind, min(max(ratio, 0.0), 1.0), None)

    def reset(self, signature: str, source: ResetSource) -> None:
        """シートを替えたとみなし、ここから数え直す。"""
        with self.database.connect() as db:
            _insert(db, signature, "reset", None, source)

    def mark_notified(self) -> None:
        """前回の交換から、交換どきを知らせたことを残す（同じ交換の間に繰り返さない）。"""
        with self.database.connect() as db:
            last = db.execute(
                "SELECT scene_signature FROM sheet_wear ORDER BY id DESC LIMIT 1"
            ).fetchone()
            _insert(db, last["scene_signature"] if last else None, "notified", None, None)

    def status(self) -> SheetWear:
        """前回の交換（自動の数え直しか、シート交換の記録の新しいほう）からの使用の度合い。"""
        with self.database.connect() as db:
            reset = db.execute(
                "SELECT id, at FROM sheet_wear WHERE kind = 'reset' ORDER BY id DESC LIMIT 1"
            ).fetchone()
            rows = db.execute(
                "SELECT at, kind, ratio FROM sheet_wear WHERE id > ? ORDER BY id",
                (reset["id"] if reset else 0,),
            ).fetchall()
            care = db.execute(
                "SELECT MAX(at) FROM care_entries WHERE kind = 'sheet' AND deleted_at IS NULL"
            ).fetchone()[0]
            measured = db.execute("SELECT 1 FROM sheet_wear LIMIT 1").fetchone() is not None
        stamps = [datetime.fromisoformat(value) for value in (reset and reset["at"], care) if value]
        since = max(stamps) if stamps else None
        # 時刻は文字列で比べず、datetime にしてから比べる（書き方の違いに左右されない）。
        rows = [row for row in rows if since is None or datetime.fromisoformat(row["at"]) > since]
        ratios = [row["ratio"] for row in rows if row["kind"] in ("event", "settled")]
        return SheetWear(
            since=since,
            events=sum(row["kind"] == "event" for row in rows),
            ratio=ratios[-1] if ratios else 0.0,
            notified=any(row["kind"] == "notified" for row in rows),
            measured=measured,
        )


def _insert(
    db: sqlite3.Connection,
    signature: str | None,
    kind: str,
    ratio: float | None,
    source: str | None,
) -> None:
    db.execute(
        "INSERT INTO sheet_wear (at, scene_signature, kind, ratio, source) VALUES (?, ?, ?, ?, ?)",
        (utcnow(), signature, kind, ratio, source),
    )
