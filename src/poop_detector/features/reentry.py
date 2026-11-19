"""トレーへの再侵入の判定と、排泄候補ごとの片付け記録。"""

import math
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

import cv2

from ..detection.roi import AnyROI, outline
from ..errors import InvalidInputError
from ..frames import Shape
from ..notifications import Delivery
from ..records.database import events_database
from ..records.events import Store, utcnow
from .dog import Box, overlaps_roi

Position = tuple[float, float]
Side = Literal["inside", "outside"]
FootQuality = Literal["good", "low"]

# 画像の面積に対してこれより小さい検出枠は、下辺中央と足元のずれが大きいとみなす。
SMALL_BOX_RATIO = 0.01


class Presence:
    """確実な退場の後、内外に一定時間続けている場合だけ境界通過とする。"""

    def __init__(self, seconds: float, max_gap: float) -> None:
        self.seconds = seconds
        self.max_gap = max_gap
        self.interrupt()

    def interrupt(self) -> None:
        self.side: Side | None = None
        self.candidate: Side | None = None
        self.since = self.last = float("-inf")

    def update(self, side: Side | None, tick: float) -> Side | None:
        if tick - self.last > self.max_gap:
            self.interrupt()
        self.last = tick
        if side != self.candidate:
            self.candidate, self.since = side, tick
        if side is None or side == self.side or tick - self.since < self.seconds:
            return None
        if self.side is None and side == "inside":
            return None
        self.side = side
        return side


def dog_position(
    box: Box, toilet: AnyROI, living: AnyROI | None, shape: Shape, margin: float
) -> tuple[Side | None, Position | None]:
    """検出枠の下辺中央を接地点と近似する。画面端や境界では保留する。"""
    x1, y1, x2, y2 = box
    height, width = shape[:2]
    if x1 <= 0 or y1 <= 0 or x2 >= width - 1 or y2 >= height - 1:
        return None, None
    point = ((x1 + x2) / 2, float(y2))
    distance = cv2.pointPolygonTest(outline(toilet, shape), point, True)
    band = min(width, height) * margin
    if distance > band:
        return "inside", (point[0] / (width - 1), point[1] / (height - 1))
    if distance >= -band or overlaps_roi(box, toilet, shape):
        return None, None
    if living is None or cv2.pointPolygonTest(outline(living, shape), point, False) >= 0:
        return "outside", None
    return None, None


def foot_quality(box: Box, shape: Shape, margin: float) -> FootQuality:
    """下辺中央を足元とみなす近似が崩れやすい検出枠を low とする。判定には使わない。"""
    x1, y1, x2, y2 = box
    height, width = shape[:2]
    band = min(width, height) * margin
    near_edge = min(x1, y1, width - 1 - x2, height - 1 - y2) <= band
    small = (x2 - x1) * (y2 - y1) < width * height * SMALL_BOX_RATIO
    return "low" if near_edge or small else "good"


class WasteTrack:
    """同じ候補とみなせる局所差分が二度続くときだけ位置を更新する。

    呼び出し側は、犬が生活エリアにいて、基準画像との比較が candidate のときだけ渡す。
    照明変化・縁の遮蔽・消失の間は呼ばずに最後の位置を保ち、片付けたとは判断しない。
    """

    def __init__(self) -> None:
        self.previous: Position | None = None

    def update(
        self, position: Position | None, last_known: Position, radius: float
    ) -> Position | None:
        previous, self.previous = self.previous, None
        if position is None or math.dist(position, last_known) > radius:
            return None
        self.previous = position
        if previous is not None and math.dist(position, previous) <= radius / 4:
            return position
        return None


def _validate_feedback_note(note: str | None) -> None:
    if note is not None and len(note) > 2000:
        raise InvalidInputError("メモは2000文字以内にしてください")


class ReentryStore:
    """既存の events.sqlite3 に片付け状態と再侵入をひも付ける。"""

    def __init__(self, root: Path) -> None:
        self.db = events_database(root)

    def save_event_feedback(
        self,
        event_id: str,
        *,
        label: str | None = None,
        location: str | None = None,
        stool: str | None = None,
        note: str | None = None,
    ) -> None:
        """排泄の分類と片付け候補の補足情報を同時に確定する。"""
        _validate_feedback_note(note)
        with self.db.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            candidate = db.execute(
                "SELECT 1 FROM reentry_waste WHERE event_id=?", (event_id,)
            ).fetchone()
            if candidate is None:
                raise InvalidInputError("排泄候補が見つかりません")
            if label:
                Store.classify_in(db, event_id, label, location=location)
            elif location:
                raise InvalidInputError("--location は --label と一緒に指定してください")
            db.execute(
                """UPDATE reentry_waste SET stool=COALESCE(?,stool), note=COALESCE(?,note)
                   WHERE event_id=?""",
                (stool, note, event_id),
            )

    def save_visit_feedback(
        self,
        visit_id: int,
        *,
        correct: str | None = None,
        stepped: str | None = None,
        note: str | None = None,
    ) -> None:
        """省略した項目は維持し、unknown は判定なしとして保存する。"""
        _validate_feedback_note(note)
        answers = {"yes": 1, "no": 0, "unknown": None}
        with self.db.connect() as db:
            changed = db.execute(
                """UPDATE reentry_visits
                   SET correct=CASE WHEN ? THEN ? ELSE correct END,
                       stepped=CASE WHEN ? THEN ? ELSE stepped END, note=COALESCE(?,note)
                   WHERE id=?""",
                (
                    correct is not None,
                    answers.get(correct or ""),
                    stepped is not None,
                    answers.get(stepped or ""),
                    note,
                    visit_id,
                ),
            ).rowcount
            if not changed:
                raise InvalidInputError("再侵入の記録が見つかりません")

    def register(self, event_id: str, configuration_id: str, position: Position | None) -> None:
        with self.db.connect() as db:
            self.register_in(db, event_id, configuration_id, position)

    @staticmethod
    def register_in(
        db: sqlite3.Connection, event_id: str, configuration_id: str, position: Position | None
    ) -> None:
        x, y = position if position is not None else (None, None)
        db.execute(
            """INSERT OR IGNORE INTO reentry_waste
               (event_id, configuration_id, status, position_x, position_y)
               VALUES (?, ?, ?, ?, ?)""",
            (event_id, configuration_id, "uncollected" if position else "unknown", x, y),
        )

    def pending(self, configuration_id: str) -> list[sqlite3.Row]:
        with self.db.connect() as db:
            return db.execute(
                "SELECT * FROM pending_waste WHERE configuration_id=?",
                (configuration_id,),
            ).fetchall()

    def update_position(self, event_id: str, position: Position) -> None:
        with self.db.connect() as db:
            db.execute(
                """UPDATE reentry_waste SET position_x=?, position_y=?
                   WHERE event_id IN (SELECT id FROM pending_waste WHERE id=?)""",
                (*position, event_id),
            )

    def advance_observation(self, event_id: str, previous: str | None, at: str) -> bool:
        with self.db.connect() as db:
            return (
                db.execute(
                    """UPDATE reentry_waste SET observed_until=?
                   WHERE event_id IN (SELECT id FROM pending_waste WHERE id=?)
                   AND observed_until IS ?""",
                    (at, event_id, previous),
                ).rowcount
                == 1
            )

    def collect(self, event_id: str) -> bool:
        with self.db.connect() as db:
            changed = db.execute(
                """UPDATE reentry_waste SET status='collected', collected_at=?, current_risk=NULL
                   WHERE event_id=? AND status!='collected'""",
                (utcnow(), event_id),
            ).rowcount
            if changed:
                db.execute(
                    """UPDATE reentry_visits SET interrupted=1
                       WHERE event_id=? AND ended_at IS NULL""",
                    (event_id,),
                )
            return changed == 1

    def claim(
        self, event_id: str, kind: str, at: str | None = None, clip_id: str | None = None
    ) -> bool:
        """保存済みの通知キーを再起動後も使う。"""
        with self.db.connect() as db:
            return (
                db.execute(
                    """INSERT OR IGNORE INTO reentry_notices (event_id, kind, occurred_at, clip_id)
                       SELECT id, ?, ?, ? FROM pending_waste WHERE id=?""",
                    (kind, at, clip_id, event_id),
                ).rowcount
                == 1
            )

    def due_notices(
        self, configuration_id: str, at: str, *, medium: bool = True
    ) -> list[sqlite3.Row]:
        with self.db.connect() as db:
            return db.execute(
                """SELECT n.event_id, n.kind, n.occurred_at, n.clip_id
                   FROM reentry_notices n JOIN pending_waste w ON w.id=n.event_id
                   WHERE w.configuration_id=? AND n.status='retry' AND n.next_attempt_at<=?
                     AND (? OR n.kind!='reentry')
                   ORDER BY n.next_attempt_at LIMIT 1""",
                (configuration_id, at, medium),
            ).fetchall()

    def claim_retry(self, event_id: str, kind: str, at: str) -> bool:
        with self.db.connect() as db:
            return (
                db.execute(
                    """UPDATE reentry_notices
                   SET status='unknown', attempts=attempts+1, next_attempt_at=NULL
                   WHERE event_id=? AND kind=? AND status='retry' AND next_attempt_at<=?
                     AND event_id IN (SELECT id FROM pending_waste)""",
                    (event_id, kind, at),
                ).rowcount
                == 1
            )

    def finish_notice(self, event_id: str, kind: str, result: Delivery, at: str) -> None:
        retry_at = (datetime.fromisoformat(at) + timedelta(minutes=1)).isoformat()
        with self.db.connect() as db:
            db.execute(
                """UPDATE reentry_notices
                   SET status=CASE WHEN ?='retry' AND attempts>=3 THEN 'failed' ELSE ? END,
                       next_attempt_at=CASE WHEN ?='retry' AND attempts<3 THEN ? END
                   WHERE event_id=? AND kind=? AND status='unknown'""",
                (result, result, result, retry_at, event_id, kind),
            )

    def start(self, event_id: str, at: str) -> int | None:
        with self.db.connect() as db:
            cursor = db.execute(
                """INSERT INTO reentry_visits (event_id, started_at, risk)
                   SELECT id, ?, 'MEDIUM' FROM pending_waste WHERE id=?""",
                (at, event_id),
            )
            return cursor.lastrowid if cursor.rowcount else None

    def finish(self, visit: int, at: str | None) -> None:
        with self.db.connect() as db:
            db.execute(
                """UPDATE reentry_visits SET ended_at=?, interrupted=?
                   WHERE id=? AND ended_at IS NULL AND interrupted=0""",
                (at, int(at is None), visit),
            )

    def high(self, visit: int, distance: float, quality: FootQuality = "good") -> bool:
        with self.db.connect() as db:
            return (
                db.execute(
                    """UPDATE reentry_visits SET risk='HIGH', distance=?, high_foot_quality=?
                   WHERE id=? AND risk='MEDIUM' AND interrupted=0 AND ended_at IS NULL
                   AND event_id IN (SELECT id FROM pending_waste)""",
                    (distance, quality, visit),
                ).rowcount
                == 1
            )

    def count_foot(self, visit: int, quality: FootQuality) -> None:
        """訪問中に足元を観測した回数と、そのうち品質が低い回数を数える。"""
        with self.db.connect() as db:
            db.execute(
                """UPDATE reentry_visits SET foot_samples=foot_samples+1,
                       low_foot_samples=low_foot_samples+?
                   WHERE id=? AND interrupted=0 AND ended_at IS NULL""",
                (int(quality == "low"), visit),
            )

    def attach_clip(self, visit: int, clip_id: str, *, high: bool) -> None:
        with self.db.connect() as db:
            if high:
                db.execute("UPDATE reentry_visits SET high_clip_id=? WHERE id=?", (clip_id, visit))
            else:
                db.execute("UPDATE reentry_visits SET clip_id=? WHERE id=?", (clip_id, visit))

    def set_risk(self, event_id: str, risk: str | None) -> None:
        with self.db.connect() as db:
            db.execute(
                """UPDATE reentry_waste SET current_risk=?
                   WHERE event_id=? AND status!='collected' AND current_risk IS NOT ?""",
                (risk, event_id, risk),
            )

    def interrupt_open(self, configuration_id: str) -> None:
        with self.db.connect() as db:
            db.execute(
                "UPDATE reentry_waste SET current_risk=NULL WHERE configuration_id=? AND current_risk IS NOT NULL",
                (configuration_id,),
            )
            db.execute(
                """UPDATE reentry_visits SET interrupted=1 WHERE ended_at IS NULL AND interrupted=0
                   AND event_id IN (SELECT event_id FROM reentry_waste WHERE configuration_id=?)""",
                (configuration_id,),
            )


class ReentryMonitor:
    """観測ごとに未回収の候補を確認し、排泄候補単位で再侵入を記録する。"""

    def __init__(
        self,
        store: ReentryStore,
        configuration_id: str,
        presence: Presence,
        high_distance: float,
        *,
        high_samples: int = 1,
    ) -> None:
        self.store, self.configuration_id = store, configuration_id
        self.presence, self.high_distance = presence, high_distance
        self.high_samples = high_samples
        self.nearby: dict[int, int] = {}
        self.nearby_low: dict[int, bool] = {}
        self.armed: set[str] = set()
        self.visits: dict[str, int] = {}
        self.coverage: dict[str, str | None] = {}
        self.last_coverage_tick = float("-inf")
        self.last_known = float("-inf")
        self.store.interrupt_open(configuration_id)

    def interrupt(self) -> None:
        if self.visits or self.armed:
            self.store.interrupt_open(self.configuration_id)
        self.visits.clear()
        self.armed.clear()
        self.presence.interrupt()
        self.nearby.clear()
        self.nearby_low.clear()
        self.coverage = dict.fromkeys(self.coverage)

    def record_coverage(
        self, rows: dict[str, sqlite3.Row], side: Side | None, tick: float, at: str
    ) -> None:
        now = datetime.fromisoformat(at)
        continues = 0 < tick - self.last_coverage_tick <= self.presence.max_gap
        self.last_coverage_tick = tick
        advanced: dict[str, str | None] = {}
        for event_id, row in rows.items():
            previous = self.coverage.get(event_id)
            if event_id in self.coverage and (not continues or previous is None):
                advanced[event_id] = None
                continue
            start = datetime.fromisoformat(previous or row["timestamp"])
            if (
                side is not None
                and 0 <= (now - start).total_seconds() <= self.presence.max_gap
                and self.store.advance_observation(event_id, previous, at)
            ):
                advanced[event_id] = at
            else:
                advanced[event_id] = None
        self.coverage = advanced

    def observe(
        self,
        side: Side | None,
        point: Position | None,
        tick: float,
        at: str,
        *,
        quality: FootQuality = "good",
    ) -> list[tuple[str, str, int]]:
        if tick - self.last_known > self.presence.max_gap:
            self.interrupt()
        if side is not None:
            self.last_known = tick
        change = self.presence.update(side, tick)
        rows = {row["id"]: row for row in self.store.pending(self.configuration_id)}
        self.record_coverage(rows, side, tick, at)
        for event_id in self.visits.keys() - rows.keys():
            self.store.finish(self.visits.pop(event_id), None)
        self.armed.intersection_update(rows)
        if change == "outside":
            for visit in self.visits.values():
                self.store.finish(visit, at)
            self.visits.clear()
        if side == self.presence.side == "outside":
            for event_id in rows.keys() - self.armed:
                self.store.set_risk(event_id, "LOW")
            self.armed.update(rows)
        notices: list[tuple[str, str, int]] = []
        if change == "inside":
            for event_id in sorted(self.armed):
                new_visit = self.store.start(event_id, at)
                if new_visit is not None:
                    self.visits[event_id] = new_visit
                    self.store.set_risk(event_id, "MEDIUM")
                    notices.append((event_id, "reentry", new_visit))
            self.armed.clear()
        notices.extend(self.near_waste(rows, side, point, quality))
        return notices

    def near_waste(
        self,
        rows: dict[str, sqlite3.Row],
        side: Side | None,
        point: Position | None,
        quality: FootQuality,
    ) -> list[tuple[str, str, int]]:
        """足元が推定位置に続けて近いと HIGH にする。品質は記録するが判定には使わない。"""
        notices: list[tuple[str, str, int]] = []
        nearby: dict[int, int] = {}
        nearby_low: dict[int, bool] = {}
        if side == "inside" and point is not None:
            for event_id, visit in self.visits.items():
                self.store.count_foot(visit, quality)
                row = rows[event_id]
                x, y = row["position_x"], row["position_y"]
                if x is None or y is None:
                    continue
                distance = math.hypot(point[0] - x, point[1] - y)
                if distance > self.high_distance:
                    continue
                nearby[visit] = self.nearby.get(visit, 0) + 1
                nearby_low[visit] = self.nearby_low.get(visit, False) or quality == "low"
                streak: FootQuality = "low" if nearby_low[visit] else "good"
                if nearby[visit] >= self.high_samples and self.store.high(visit, distance, streak):
                    self.store.set_risk(event_id, "HIGH")
                    notices.append((event_id, "high", visit))
        self.nearby, self.nearby_low = nearby, nearby_low
        return notices
