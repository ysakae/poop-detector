"""GUI のサーバー側のセッション。

Cookie にはランダムな ID だけを入れ、データベース（gui_sessions）にはその SHA-256 だけを
保存する。データベースの中身が漏れても、そこから Cookie を作ることはできない。
有効期限は、無操作の時間と、始めてからの時間の2つ。
"""

import hashlib
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..records.database import Database

# 最後に使った時刻の書き込みを、この秒数に1回までにする（画像の読み込みのたびに書かない）。
TOUCH_INTERVAL_SECONDS = 60


@dataclass(frozen=True)
class SessionPolicy:
    """セッションの有効期限。設定の gui.session_idle_hours と gui.session_max_days から作る。"""

    idle_hours: float = 168
    max_days: float = 30

    @property
    def max_age_seconds(self) -> int:
        """Cookie の Max-Age。始めてからの有効期限と同じ。"""
        return int(self.max_days * 86400)


@dataclass(frozen=True)
class Session:
    """有効なセッション。user_id はパスキーでログインしたユーザー（起動トークンなら None）。"""

    user_id: str | None


def _stamp(moment: datetime) -> str:
    """文字列のままで前後を比べられるよう、桁をそろえた UTC の ISO 8601。"""
    return moment.isoformat(timespec="microseconds")


def _digest(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()


class SessionStore:
    """gui_sessions テーブル。clock はテストで差し替える（エポック秒を返す）。"""

    def __init__(
        self,
        database: Database,
        policy: SessionPolicy = SessionPolicy(),  # noqa: B008  不変のデータクラスの既定値
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.database = database
        self.policy = policy
        self.clock = clock

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock(), UTC)

    def _limits(self, now: datetime) -> tuple[str, str]:
        """これより前の last_seen と created_at は期限切れ、という境目の時刻。"""
        idle = now - timedelta(hours=self.policy.idle_hours)
        lifetime = now - timedelta(days=self.policy.max_days)
        return _stamp(idle), _stamp(lifetime)

    def create(self, user_id: str | None = None) -> str:
        """新しいセッションを始め、Cookie に入れる ID を返す。期限切れのセッションも片付ける。"""
        session_id = secrets.token_urlsafe(32)
        now = self._now()
        stamp = _stamp(now)
        idle, lifetime = self._limits(now)
        with self.database.connect() as db:
            db.execute(
                "DELETE FROM gui_sessions WHERE last_seen <= ? OR created_at <= ?", (idle, lifetime)
            )
            db.execute(
                "INSERT INTO gui_sessions VALUES (?, ?, ?, ?)",
                (_digest(session_id), stamp, stamp, user_id),
            )
        return session_id

    def check(self, session_id: str) -> bool:
        """有効なセッションか。"""
        return self.lookup(session_id) is not None

    def lookup(self, session_id: str) -> Session | None:
        """有効なセッションの中身。期限切れなら消して None。使ったときは最後に使った時刻を延ばす。"""
        if not session_id:
            return None
        key = _digest(session_id)
        now = self._now()
        idle, lifetime = self._limits(now)
        with self.database.connect() as db:
            row = db.execute(
                "SELECT created_at, last_seen, user_id FROM gui_sessions WHERE id = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if row["last_seen"] <= idle or row["created_at"] <= lifetime:
                db.execute("DELETE FROM gui_sessions WHERE id = ?", (key,))
                return None
            last_seen = datetime.fromisoformat(row["last_seen"])
            if (now - last_seen).total_seconds() >= TOUCH_INTERVAL_SECONDS:
                db.execute("UPDATE gui_sessions SET last_seen = ? WHERE id = ?", (_stamp(now), key))
        return Session(row["user_id"])

    def revoke(self, session_id: str) -> None:
        """ログアウト。セッションを消す。"""
        with self.database.connect() as db:
            db.execute("DELETE FROM gui_sessions WHERE id = ?", (_digest(session_id),))
