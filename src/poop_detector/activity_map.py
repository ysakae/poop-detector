"""サークルに固定した床面座標で犬の滞在と通過を日別に集計する。"""

import fcntl
import json
import logging
import math
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Self, TypeAlias

import cv2
import numpy as np
from numpy.typing import NDArray

from .dog import Box, Predict
from .frames import Frame, Shape
from .roi import MaskedROI, PolygonROI
from .timezones import local_now

# 床面を 0〜1 に正規化した座標と、GRID×GRID のマス目の位置。
FloorPoint: TypeAlias = tuple[float, float]
Cell: TypeAlias = tuple[int, int]

GRID = 20
LAYOUT_FILE = "activity-layout.json"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActivityLayout:
    """行動マップ用に登録したサークルの床面（多角形）と、その登録 ID。"""

    id: str
    roi: PolygonROI

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{32}", self.id):
            raise ValueError("床面の登録IDが不正です")

    @classmethod
    def load(cls, root: str | Path) -> Self:
        data = json.loads((Path(root) / LAYOUT_FILE).read_text())
        kind = MaskedROI if data.get("mode") == "mask" else PolygonROI
        return cls(data["id"], kind(data["points"]))

    def map_transform(self) -> NDArray[Any]:
        """映像の座標を床面の 0〜1 の座標へ移す射影変換。"""
        points = np.array(self.roi.points, dtype=np.float32)
        if isinstance(self.roi, MaskedROI):
            # 多角形は射影補正せず、背景画像と同じ外接矩形を座標の基準にする。
            left, top = points.min(axis=0)
            right, bottom = points.max(axis=0)
            points = np.array(
                [[left, top], [right, top], [right, bottom], [left, bottom]], dtype=np.float32
            )
        corners = np.array([[0, 0], [1, 0], [1, 1], [0, 1]], dtype=np.float32)
        transform: NDArray[Any] = cv2.getPerspectiveTransform(points, corners)
        return transform

    def map_outline(self) -> NDArray[Any] | None:
        """多角形の床面の外周を、マップ上の座標で返す。四隅の床面では None。"""
        if isinstance(self.roi, MaskedROI):
            points = cv2.perspectiveTransform(
                np.array([self.roi.points], dtype=np.float32), self.map_transform()
            )
            outline: NDArray[Any] = np.clip(points[0], 0, 1)
            return outline
        return None

    def background(self, root: str | Path) -> Path:
        return Path(root) / f"activity-{self.id}.jpg"

    def save(self, root: str | Path, frame: Frame) -> None:
        """背景画像を書いてから登録を置き換え、途中で失敗しても前の登録を壊さない。"""
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        floor = self.roi.crop(frame)
        scale = min(1, 640 / max(floor.shape[:2]))
        height, width = floor.shape[:2]
        floor = cv2.resize(floor, (max(1, round(width * scale)), max(1, round(height * scale))))
        if not cv2.imwrite(str(self.background(root)), floor):
            raise OSError("行動マップの背景を保存できません")
        pending = root / f".activity-{self.id}.json"
        pending.write_text(json.dumps({"id": self.id, **asdict(self.roi)}))
        pending.replace(root / LAYOUT_FILE)


class ActivityStore:
    """行動マップの集計を activity.sqlite3 に日別・マス目別で保存する。"""

    def __init__(self, root: str | Path) -> None:
        self.path = Path(root) / "activity.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS activity_coverage (
                    layout TEXT, day TEXT, sampled REAL, located REAL, PRIMARY KEY(layout, day));
                CREATE TABLE IF NOT EXISTS activity_cells (
                    layout TEXT, day TEXT, x INTEGER, y INTEGER, seconds REAL, entries INTEGER,
                    PRIMARY KEY(layout, day, x, y));
            """)

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=0.25)

    @contextmanager
    def recording_lock(self) -> Iterator[None]:
        """行動マップを記録する監視を1プロセスに限る。"""
        with self.path.with_name(".activity.lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("別の行動マップ監視が起動中です") from None
            yield

    def add(
        self, layout: str, day: date, elapsed: float, dwell: Cell | None, entry: Cell | None
    ) -> None:
        """elapsed 秒の観測を足す。dwell は滞在したマス、entry は新しく入ったマス。"""
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

    def summary(
        self, layout: str, first_day: date, last_day: date
    ) -> tuple[list[Any], float, float]:
        """期間の (マスごとの滞在秒数と通過回数, 観測秒数, 位置が分かった秒数) を返す。"""
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


def cell_at(point: FloorPoint) -> Cell:
    x, y = point
    return min(GRID - 1, int(x * GRID)), min(GRID - 1, int(y * GRID))


class ActivityRecorder:
    """犬の検出位置を床面の座標に変換し、滞在時間と通過回数を記録する。"""

    def __init__(self, store: ActivityStore, layout: ActivityLayout) -> None:
        self.store, self.layout = store, layout
        self.boundary = np.array(layout.roi.points, dtype=np.float32)
        self.transform = layout.map_transform()
        self.last_sample = float("-inf")
        self.interrupt()

    def interrupt(self) -> None:
        """映像が途切れたとき、前後の位置をつながないようにする。"""
        # (取得時刻, 日付, 床面の位置)
        self.previous: tuple[float, date, FloorPoint | None] | None = None
        self.anchor: FloorPoint | None = None

    def position(self, boxes: Sequence[Box], shape: Shape) -> FloorPoint | None:
        """床面の中にいる犬が1頭だけのとき、その位置を床面の座標で返す。"""
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
        mapped = cv2.perspectiveTransform(np.array([in_floor], dtype=np.float32), self.transform)
        x, y = np.clip(mapped[0, 0], 0, 1)
        return float(x), float(y)

    def update(
        self, frame: Frame, predict: Predict, tick: float, stamp: datetime | None = None
    ) -> None:
        """1秒ごとに犬の位置を求め、前回の位置との間を滞在と通過として記録する。"""
        if tick - self.last_sample < 1:
            return
        self.last_sample = tick
        day = (stamp or local_now()).astimezone().date()
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
        dwell: Cell | None = None
        entry: Cell | None = None
        if previous[2] is not None and point is not None:
            dwell = cell_at(previous[2])
            anchor = self.anchor if self.anchor is not None else point
            if cell_at(point) != cell_at(anchor) and math.dist(point, anchor) >= 1 / GRID:
                entry = cell_at(point)
                self.anchor = point
        else:
            self.anchor = point
        try:
            self.store.add(self.layout.id, day, elapsed, dwell, entry)
        except sqlite3.Error:
            logger.warning("行動マップの集計を保存できませんでした")
