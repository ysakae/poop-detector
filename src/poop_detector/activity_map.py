"""サークルに固定した床面座標で犬の滞在と通過を日別に集計する。"""

import fcntl
import json
import logging
import math
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

from .record_stats import JST
from .roi import PolygonROI

GRID = 20
LAYOUT_FILE = "activity-layout.json"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivityLayout:
    id: str
    roi: PolygonROI

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{32}", self.id):
            raise ValueError("床面の登録IDが不正です")

    @classmethod
    def load(cls, root):
        data = json.loads((Path(root) / LAYOUT_FILE).read_text())
        return cls(data["id"], PolygonROI(data["points"]))

    def background(self, root):
        return Path(root) / f"activity-{self.id}.jpg"

    def save(self, root, frame):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        floor = self.roi.crop(frame)
        scale = min(1, 640 / max(floor.shape[:2]))
        floor = cv2.resize(floor, tuple(max(1, round(n * scale)) for n in floor.shape[1::-1]))
        if not cv2.imwrite(str(self.background(root)), floor):
            raise OSError("行動マップの背景を保存できません")
        pending = root / f".activity-{self.id}.json"
        pending.write_text(json.dumps({"id": self.id, "points": self.roi.points}))
        pending.replace(root / LAYOUT_FILE)


class ActivityStore:
    def __init__(self, root):
        self.path = Path(root) / "activity.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS activity_coverage (
                    layout TEXT, day TEXT, sampled REAL, located REAL, PRIMARY KEY(layout, day));
                CREATE TABLE IF NOT EXISTS activity_cells (
                    layout TEXT, day TEXT, x INTEGER, y INTEGER, seconds REAL, entries INTEGER,
                    PRIMARY KEY(layout, day, x, y));
            """)

    def connect(self):
        return sqlite3.connect(self.path, timeout=0.25)

    @contextmanager
    def recording_lock(self):
        with self.path.with_name(".activity.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("別の行動マップ監視が起動中です") from None
            yield

    def add(self, layout, day, elapsed, dwell, entry):
        with self.connect() as db:
            db.execute(
                """INSERT INTO activity_coverage VALUES (?, ?, ?, ?)
                ON CONFLICT(layout, day) DO UPDATE SET
                    sampled=sampled+excluded.sampled, located=located+excluded.located""",
                (layout, day.isoformat(), elapsed, elapsed if dwell is not None else 0),
            )
            for cell, seconds, entries in ((dwell, elapsed, 0), (entry, 0, 1)):
                if cell is not None:
                    db.execute(
                        """INSERT INTO activity_cells VALUES (?, ?, ?, ?, ?, ?)
                        ON CONFLICT(layout, day, x, y) DO UPDATE SET
                            seconds=seconds+excluded.seconds, entries=entries+excluded.entries""",
                        (layout, day.isoformat(), *cell, seconds, entries),
                    )

    def summary(self, layout, first_day, last_day):
        params = (layout, first_day.isoformat(), last_day.isoformat())
        with self.connect() as db:
            db.execute("BEGIN")
            cells = db.execute(
                """SELECT x, y, SUM(seconds), SUM(entries) FROM activity_cells
                WHERE layout=? AND day BETWEEN ? AND ? GROUP BY x, y ORDER BY y, x""",
                params,
            ).fetchall()
            sampled, located = db.execute(
                """SELECT COALESCE(SUM(sampled),0), COALESCE(SUM(located),0) FROM activity_coverage
                WHERE layout=? AND day BETWEEN ? AND ?""",
                params,
            ).fetchone()
        return cells, sampled, located


def cell_at(point):
    return tuple(min(GRID - 1, int(value * GRID)) for value in point)


class ActivityRecorder:
    def __init__(self, store, layout):
        self.store, self.layout = store, layout
        self.boundary = np.float32(layout.roi.points)
        self.transform = cv2.getPerspectiveTransform(
            self.boundary, np.float32([[0, 0], [1, 0], [1, 1], [0, 1]])
        )
        self.last_sample = float("-inf")
        self.interrupt()

    def interrupt(self):
        self.previous = None
        self.anchor = None

    def position(self, boxes, shape):
        if not boxes or min(shape[:2]) < 2:
            return None
        height, width = shape[:2]
        centers = [
            ((x1 + x2) / (2 * (width - 1)), (y1 + y2) / (2 * (height - 1)))
            for x1, y1, x2, y2 in boxes
        ]
        in_floor = [p for p in centers if cv2.pointPolygonTest(self.boundary, p, False) >= 0]
        if len(in_floor) != 1:
            return None
        mapped = cv2.perspectiveTransform(np.float32([in_floor]), self.transform)[0, 0]
        return tuple(np.clip(mapped, 0, 1))

    def update(self, frame, predict, tick, stamp=None):
        if tick - self.last_sample < 1:
            return
        self.last_sample = tick
        day = (stamp or datetime.now(JST)).astimezone(JST).date()
        try:
            point = self.position(predict(frame), frame.shape)
        except (cv2.error, OSError, ValueError):
            logger.warning("行動マップ用の犬検出に失敗しました")
            point = None
        previous = self.previous
        self.previous = (tick, day, point)
        if previous is None or day != previous[1] or tick - previous[0] > 2.5:
            self.anchor = point
            return
        elapsed = tick - previous[0]
        dwell = entry = None
        if previous[2] is not None and point is not None:
            dwell = cell_at(previous[2])
            if cell_at(point) != cell_at(self.anchor) and math.dist(point, self.anchor) >= 1 / GRID:
                entry = cell_at(point)
                self.anchor = point
        else:
            self.anchor = point
        try:
            self.store.add(self.layout.id, day, elapsed, dwell, entry)
        except sqlite3.Error:
            logger.warning("行動マップの集計を保存できませんでした")
