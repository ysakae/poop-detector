"""室内画像と評価ラベルをローカル保存する。"""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import cv2


def utcnow():
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, root):
        self.root = Path(root)
        (self.root / "events").mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY, timestamp TEXT, kind TEXT, score REAL,
                detection_started TEXT, notified_at TEXT, processing_ms REAL,
                image TEXT, feedback INTEGER, acknowledged INTEGER DEFAULT 0,
                actual_start TEXT, actual_end TEXT)""")

            db.execute("""CREATE TABLE IF NOT EXISTS monitor_state (
                id INTEGER PRIMARY KEY CHECK(id=1), state TEXT, event_id TEXT)""")

    def monitor_state(self):
        with self.connect() as db:
            row = db.execute("SELECT state,event_id FROM monitor_state WHERE id=1").fetchone()
            return dict(row) if row else {"state": "EMPTY", "event_id": None}

    def set_monitor_state(self, state, event_id=None):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO monitor_state VALUES (1,?,?)", (state, event_id))

    def connect(self):
        db = sqlite3.connect(self.root / "events.sqlite3")
        db.row_factory = sqlite3.Row
        return db

    def record(self, frame, score, detection_started, processing_ms):
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
        return event_id

    def list(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM events ORDER BY timestamp")]

    def update(self, event_id, **values):
        allowed = {"feedback", "acknowledged", "notified_at", "actual_start", "actual_end"}
        if not values.keys() <= allowed:
            raise ValueError("更新項目が不正です")
        with self.connect() as db:
            cursor = db.execute(
                "UPDATE events SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                (*values.values(), event_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("イベントが見つかりません")

    def feedback(self, event_id, actual, actual_start=None, actual_end=None):
        self.update(
            event_id, feedback=int(actual), actual_start=actual_start, actual_end=actual_end
        )

    def missed(self, actual_start, actual_end=None):
        with self.connect() as db:
            db.execute(
                """INSERT INTO events (id,timestamp,kind,feedback,actual_start,actual_end)
                VALUES (?,?,?,?,?,?)""",
                (uuid4().hex, actual_start, "MISSED", 1, actual_start, actual_end),
            )

    def metrics(self):
        rows = self.list()
        detections = [r for r in rows if r["kind"] == "SUSPECTED"]
        tp = sum(r["feedback"] == 1 for r in detections)
        fp = sum(r["feedback"] == 0 for r in detections)
        missed = sum(r["kind"] == "MISSED" for r in rows)
        daily = {}
        delays = {"start_delay_seconds": [], "end_delay_seconds": []}
        for row in detections:
            if row["feedback"] == 0:
                date = row["timestamp"][:10]
                daily[date] = daily.get(date, 0) + 1
            if row["feedback"] == 1 and row["notified_at"]:
                for field, key in [
                    ("actual_start", "start_delay_seconds"),
                    ("actual_end", "end_delay_seconds"),
                ]:
                    if row[field]:
                        delays[key].append(
                            (
                                datetime.fromisoformat(row["notified_at"])
                                - datetime.fromisoformat(row[field])
                            ).total_seconds()
                        )
        return {
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + missed) if tp + missed else None,
            "true_positive": tp,
            "false_positive": fp,
            "missed": missed,
            "unlabeled": sum(r["feedback"] is None for r in detections),
            "false_positives_by_day_utc": daily,
            **delays,
        }
