"""床に新しく現れた物の候補（実験的）と、その画像・人のラベルを保存する。"""

import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

import cv2

from ..atomic_file import write_bytes
from ..features.dog import Box
from ..frames import Frame
from .database import Database
from .events import utcnow

# 人が付けるラベル。嘔吐・下痢の判定（#274）も同じ名前を使う。
FLOOR_LABELS = {
    "foreign": "異物",
    "vomit": "嘔吐",
    "diarrhea": "下痢",
    "toy": "おもちゃ・寝具",
    "false": "誤検知",
}
IMAGE_DIR = "floor-objects"
# 保存する画像の幅の上限（画素）。
IMAGE_WIDTH = 960
_ID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True)
class FloorObject:
    """床の候補の1件。box は映像の幅・高さで割った 0〜1 の座標。"""

    id: str
    detected_at: str
    box: tuple[float, float, float, float]
    area_ratio: float
    label: str | None
    labeled_by: str | None
    notified: bool


class FloorObjects:
    """floor_objects テーブルと、data_dir/floor-objects/ の画像。"""

    def __init__(self, database: Database, root: str | Path) -> None:
        self.database = database
        self.root = Path(root)

    def add(self, frame: Frame, box: Box, area_ratio: float) -> str:
        """候補の枠を描いた画像を書いてから、行を足す。ID を返す。"""
        object_id = uuid4().hex
        height, width = frame.shape[:2]
        x1, y1, x2, y2 = box
        image = frame.copy()
        cv2.rectangle(image, (x1, y1), (x2, y2), (0, 0, 255), max(2, width // 320))
        scale = min(1.0, IMAGE_WIDTH / width)
        if scale < 1:
            image = cv2.resize(image, (round(width * scale), round(height * scale)))
        ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            raise OSError("床の候補の画像を作れません")
        (self.root / IMAGE_DIR).mkdir(parents=True, exist_ok=True)
        write_bytes(self.image_path(object_id), encoded.tobytes())
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO floor_objects (id, detected_at, x1, y1, x2, y2, area_ratio) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (object_id, utcnow(), x1 / width, y1 / height, x2 / width, y2 / height, area_ratio),
            )
        return object_id

    def image_path(self, object_id: str) -> Path:
        if not _ID.fullmatch(object_id):
            raise KeyError(object_id)
        return self.root / IMAGE_DIR / f"{object_id}.jpg"

    def list(self, limit: int = 50) -> list[FloorObject]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT * FROM floor_objects ORDER BY detected_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            FloorObject(
                row["id"],
                row["detected_at"],
                (row["x1"], row["y1"], row["x2"], row["y2"]),
                row["area_ratio"],
                row["label"],
                row["labeled_by"],
                row["notified_at"] is not None,
            )
            for row in rows
        ]

    def pending_count(self) -> int:
        with self.database.connect() as db:
            count: int = db.execute(
                "SELECT COUNT(*) FROM floor_objects WHERE label IS NULL"
            ).fetchone()[0]
        return count

    def label(self, object_id: str, label: str, user: str) -> None:
        if label not in FLOOR_LABELS:
            raise ValueError(label)
        with self.database.connect() as db:
            updated = db.execute(
                "UPDATE floor_objects SET label=?, labeled_at=?, labeled_by=? WHERE id=?",
                (label, utcnow(), user, object_id),
            ).rowcount
        if not updated:
            raise KeyError(object_id)

    def mark_notified(self, object_id: str) -> None:
        with self.database.connect() as db:
            db.execute("UPDATE floor_objects SET notified_at=? WHERE id=?", (utcnow(), object_id))
