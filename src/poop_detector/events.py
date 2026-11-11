"""室内画像と評価ラベルをローカル保存する。"""

import builtins
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal, TypeAlias, TypedDict, cast
from uuid import uuid4

import cv2
import numpy as np

from .database import events_database
from .frames import Frame

# classify で「指定なし（既存の値を保つ）」と None（値を消す）を区別する目印。
_UNSET: Final[Any] = object()
# events.kind。SUSPECTED は差分検知、MANUAL と MISSED は手入力、VIDEO は動画から確定した記録。
EventKind: TypeAlias = Literal["SUSPECTED", "MANUAL", "MISSED", "VIDEO"]
EXCRETION_LOCATIONS = {"tray": "トレー内", "outside": "トレー外"}
EXCRETION_TYPES = {
    "feces": "排便",
    "urine": "排尿",
    "both": "排便・排尿",
    "unspecified": "種類不明",
}


class EventRow(TypedDict):
    """Store.list が返す1件のイベント。排泄の種類とメモを結合済み。"""

    id: str
    timestamp: str
    kind: EventKind
    score: float | None
    detection_started: str | None
    notified_at: str | None
    processing_ms: float | None
    image: str | None
    feedback: int | None
    acknowledged: int
    actual_start: str | None
    actual_end: str | None
    location: str | None
    excretion_type: str
    note: str


class MonitorState(TypedDict):
    """監視側が最後に書いた状態と、そのときのイベント。"""

    state: str
    event_id: str | None


def validate_times(start: str | None, end: str | None) -> None:
    for value in (start, end):
        if value and datetime.fromisoformat(value).utcoffset() is None:
            raise ValueError("時刻にはタイムゾーンが必要です")
    if start and end and datetime.fromisoformat(end) < datetime.fromisoformat(start):
        raise ValueError("完了は開始以降を指定してください")


def utcnow() -> str:
    return datetime.now(UTC).isoformat()


def validate_location(location: str | None) -> None:
    if location and location not in EXCRETION_LOCATIONS:
        raise ValueError("排泄した場所が不正です")


class Store:
    """検知イベント、目視の判定、排泄の記録を events.sqlite3 に保存する。"""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        (self.root / "events").mkdir(parents=True, exist_ok=True)
        self.database = events_database(self.root)

    @staticmethod
    def encode_reference(frame: Frame) -> bytes:
        ok, image = cv2.imencode(".png", frame)
        if not ok:
            raise OSError("通知済み状態を保存できません")
        return image.tobytes()

    def load_reference(self, signature: str) -> Frame | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT image FROM detection_reference WHERE id=1 AND signature=?", (signature,)
            ).fetchone()
        if row is None:
            return None
        frame = cv2.imdecode(np.frombuffer(row["image"], np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("通知済み状態を読み込めません")
        return frame

    def save_reference(self, frame: Frame, signature: str) -> None:
        image = self.encode_reference(frame)
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO detection_reference VALUES (1,?,?)", (signature, image)
            )
            db.execute("INSERT OR REPLACE INTO monitor_state VALUES (1,'EMPTY',NULL)")

    def monitor_state(self) -> MonitorState:
        with self.connect() as db:
            row = db.execute("SELECT state,event_id FROM monitor_state WHERE id=1").fetchone()
            if row is None:
                return {"state": "EMPTY", "event_id": None}
            return {"state": row["state"], "event_id": row["event_id"]}

    def set_monitor_state(self, state: str, event_id: str | None = None) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO monitor_state VALUES (1,?,?)", (state, event_id))

    def connect(self) -> sqlite3.Connection:
        return self.database.connect()

    def record(
        self,
        frame: Frame,
        score: float,
        detection_started: str | None,
        processing_ms: float,
        reference: Frame | None = None,
        signature: str | None = None,
    ) -> str:
        """差分検知のイベントを画像付きで保存し、ID を返す。

        reference を渡すと、通知済み状態としても保存する（再起動時の復元用）。
        """
        encoded = self.encode_reference(reference) if reference is not None else None
        event_id = uuid4().hex
        image = f"events/{event_id}.jpg"
        if not cv2.imwrite(str(self.root / image), frame):
            raise OSError("イベント画像を保存できません")
        with self.connect() as db:
            db.execute(
                """INSERT INTO events
                (id,timestamp,kind,score,detection_started,processing_ms,image)
                VALUES (?,?,?,?,?,?,?)""",
                (event_id, utcnow(), "SUSPECTED", score, detection_started, processing_ms, image),
            )
            if encoded is not None:
                db.execute(
                    "INSERT OR REPLACE INTO detection_reference VALUES (1,?,?)",
                    (signature, encoded),
                )
        return event_id

    def list(self) -> list[EventRow]:
        with self.connect() as db:
            return [
                cast(EventRow, dict(row))
                for row in db.execute("""SELECT e.*,
                COALESCE(l.excretion_type, CASE WHEN e.feedback=1 THEN 'feces'
                ELSE 'unspecified' END) AS excretion_type, COALESCE(l.note,'') AS note
                FROM events e LEFT JOIN excretion_labels l ON e.id=l.event_id
                ORDER BY timestamp""")
            ]

    # list メソッドと名前が重なるため、組み込みの list は builtins から参照する。
    def records(self) -> builtins.list[EventRow]:
        """排泄と確定した記録を、実際の開始時刻の順に返す。"""
        return sorted(
            [r for r in self.list() if r["feedback"] == 1],
            key=lambda r: datetime.fromisoformat(r["actual_start"] or r["timestamp"]),
        )

    def classify(
        self,
        event_id: str,
        label: str,
        start: str | None = _UNSET,
        end: str | None = _UNSET,
        note: str = _UNSET,
        *,
        location: str | None = None,
    ) -> None:
        """目視の判定を保存する。start・end・note を省くと既存の値を保ち、location の空文字は消去。"""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            self._classify(db, event_id, label, start, end, note, location=location)

    @staticmethod
    def _classify(
        db: sqlite3.Connection,
        event_id: str,
        label: str,
        start: str | None = _UNSET,
        end: str | None = _UNSET,
        note: str = _UNSET,
        *,
        location: str | None = None,
    ) -> None:
        if label not in (*EXCRETION_TYPES, "false", "unknown"):
            raise ValueError("種類が不正です")
        row = db.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError("記録が見つかりません")
        previous = db.execute(
            "SELECT note FROM excretion_labels WHERE event_id=?", (event_id,)
        ).fetchone()
        start = row["actual_start"] if start is _UNSET else start
        end = row["actual_end"] if end is _UNSET else end
        note = (previous["note"] if previous else "") if note is _UNSET else note
        validate_times(start, end)
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("メモは2000文字以内にしてください")
        if row["kind"] in ("MANUAL", "MISSED") and (label in ("false", "unknown") or not start):
            raise ValueError("手動・見逃し記録には種類と開始時刻が必要です")
        if row["kind"] == "VIDEO" and label == "false":
            raise ValueError("動画からの記録を取り消す場合は保留を選択してください")
        feedback = 0 if label == "false" else None if label == "unknown" else 1
        excretion_type = label if label in EXCRETION_TYPES else "unspecified"
        validate_location(location)
        location = row["location"] if location is None else location or None
        db.execute(
            "UPDATE events SET feedback=?,actual_start=?,actual_end=?,location=? WHERE id=?",
            (feedback, start, end, location if feedback == 1 else None, event_id),
        )
        db.execute(
            "INSERT OR REPLACE INTO excretion_labels VALUES (?,?,?)",
            (event_id, excretion_type, note),
        )

    def add_excretion(
        self,
        excretion_type: str,
        start: str,
        end: str | None = None,
        note: str = "",
        missed: bool = False,
        *,
        location: str | None = None,
    ) -> str:
        if not start:
            raise ValueError("種類と開始時刻が必要です")
        with self.connect() as db:
            return self._insert_excretion(
                db,
                excretion_type,
                "MISSED" if missed else "MANUAL",
                start,
                start,
                end,
                note,
                location=location,
            )

    @staticmethod
    def _insert_excretion(
        db: sqlite3.Connection,
        excretion_type: str,
        kind: EventKind,
        timestamp: str,
        start: str | None,
        end: str | None,
        note: str,
        *,
        location: str | None = None,
    ) -> str:
        if excretion_type not in EXCRETION_TYPES:
            raise ValueError("排泄の種類を指定してください")
        validate_times(timestamp, None)
        validate_times(start, end)
        if not isinstance(note, str) or len(note) > 2000:
            raise ValueError("メモは2000文字以内にしてください")
        validate_location(location)
        event_id = uuid4().hex
        db.execute(
            """INSERT INTO events (id,timestamp,kind,feedback,actual_start,actual_end,location)
            VALUES (?,?,?,?,?,?,?)""",
            (event_id, timestamp, kind, 1, start, end, location or None),
        )
        db.execute("INSERT INTO excretion_labels VALUES (?,?,?)", (event_id, excretion_type, note))
        return event_id

    def update(self, event_id: str, **values: object) -> None:
        allowed = {"feedback", "acknowledged", "notified_at", "actual_start", "actual_end"}
        if not values.keys() <= allowed:
            raise ValueError("更新項目が不正です")
        with self.connect() as db:
            # 列名は上の allowed で許可したものだけ。値はプレースホルダーで渡す。
            cursor = db.execute(
                "UPDATE events SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",  # noqa: S608
                (*values.values(), event_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("イベントが見つかりません")

    def metrics(self) -> dict[str, Any]:
        """目視の判定から Precision・Recall・通知の遅れなどを集計する。"""
        rows = self.list()
        confirmed = [r for r in rows if r["feedback"] == 1]
        detections = [r for r in rows if r["kind"] == "SUSPECTED"]
        tp = sum(r["feedback"] == 1 for r in detections)
        fp = sum(r["feedback"] == 0 for r in detections)
        missed = sum(r["kind"] == "MISSED" for r in rows)
        daily: dict[str, int] = {}
        delays: dict[str, list[float]] = {"start_delay_seconds": [], "end_delay_seconds": []}
        for row in detections:
            if row["feedback"] == 0:
                date = row["timestamp"][:10]
                daily[date] = daily.get(date, 0) + 1
            notified_at = row["notified_at"]
            if row["feedback"] == 1 and notified_at:
                for actual, key in [
                    (row["actual_start"], "start_delay_seconds"),
                    (row["actual_end"], "end_delay_seconds"),
                ]:
                    if actual:
                        delays[key].append(
                            (
                                datetime.fromisoformat(notified_at) - datetime.fromisoformat(actual)
                            ).total_seconds()
                        )
        return {
            "excretion_counts": {
                "feces": sum(r["excretion_type"] in ("feces", "both") for r in confirmed),
                "urine": sum(r["excretion_type"] in ("urine", "both") for r in confirmed),
                "unknown": sum(r["excretion_type"] == "unspecified" for r in confirmed),
            },
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + missed) if tp + missed else None,
            "true_positive": tp,
            "false_positive": fp,
            "missed": missed,
            "unlabeled": sum(r["feedback"] is None for r in detections),
            "false_positives_by_day_utc": daily,
            **delays,
        }
