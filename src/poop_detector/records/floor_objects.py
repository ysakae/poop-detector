"""床に新しく現れた物の候補（実験的）と、その画像・人のラベルを保存する。"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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
# 判定器の呼び出しがこの秒数を過ぎても終わらない候補は、止まったとみなす。
JUDGE_STALE_SECONDS = 900


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
    # 判定器の結果（floor.judge）。judge_status は running・done・failed、まだなら None。
    judge_status: str | None = None
    judge_label: str | None = None
    judge_reason: str | None = None


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
                row["judge_status"],
                row["judge_label"],
                row["judge_reason"],
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

    def claim_unjudged(self, since: datetime) -> FloorObject | None:
        """since 以降に見つけた未判定の候補を古い順に1件取り、running にする。

        判定の途中で止まった候補（JUDGE_STALE_SECONDS より前から running）は failed にする。
        取り出しは BEGIN IMMEDIATE の中で行い、同じ候補を2つの処理が取らない。
        """
        stale = (datetime.now(UTC) - timedelta(seconds=JUDGE_STALE_SECONDS)).isoformat()
        with self.database.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE floor_objects SET judge_status='failed', judge_failure='timeout' "
                "WHERE judge_status='running' AND judge_started_at < ?",
                (stale,),
            )
            row = db.execute(
                "SELECT id FROM floor_objects WHERE judge_status IS NULL AND detected_at >= ? "
                "ORDER BY detected_at LIMIT 1",
                (since.astimezone(UTC).isoformat(),),
            ).fetchone()
            if row is None:
                return None
            db.execute(
                "UPDATE floor_objects SET judge_status='running', judge_started_at=? WHERE id=?",
                (utcnow(), row["id"]),
            )
        return next(item for item in self.list(1000) if item.id == row["id"])

    def save_judgement(self, object_id: str, label: str, reason: str) -> None:
        with self.database.connect() as db:
            db.execute(
                "UPDATE floor_objects SET judge_status='done', judge_label=?, judge_reason=?, "
                "judged_at=? WHERE id=?",
                (label, reason, utcnow(), object_id),
            )

    def fail_judgement(self, object_id: str, failure: str) -> None:
        with self.database.connect() as db:
            db.execute(
                "UPDATE floor_objects SET judge_status='failed', judge_failure=?, judged_at=? "
                "WHERE id=?",
                (failure, utcnow(), object_id),
            )

    def count_judged(self, labels: tuple[str, ...], since: datetime) -> int:
        """since 以降に判定器が labels のどれかとした候補の数。"""
        marks = ",".join("?" for _ in labels)
        with self.database.connect() as db:
            count: int = db.execute(
                # marks は ? を並べただけで、値はプレースホルダーで渡す。
                f"SELECT COUNT(*) FROM floor_objects WHERE judge_label IN ({marks}) "  # noqa: S608
                "AND judged_at >= ?",
                (*labels, since.astimezone(UTC).isoformat()),
            ).fetchone()[0]
        return count

    def mark_notified(self, object_id: str) -> None:
        with self.database.connect() as db:
            db.execute("UPDATE floor_objects SET notified_at=? WHERE id=?", (utcnow(), object_id))
