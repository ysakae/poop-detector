"""動画の保存要求と状態の台帳（監視と GUI の両方から使う）。"""

import logging
import sqlite3
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, TypeAlias, TypedDict, cast, get_args
from uuid import uuid4

from ..records.events import (
    Store,
    utcnow,
)

if TYPE_CHECKING:
    from .clip_labels import ClipLabels
    from .clip_reviews import AssessmentStore, CirclingReviews

CLIP_LABELS = {
    "feces": "排便",
    "urine": "排尿",
    "both": "両方",
    "none": "排泄なし",
    "uncertain": "判断できない",
}
ACTIVITIES = {
    "other": "その他・未分類",
    "feeding": "食事",
    "sitting": "座る",
    "lying": "伏せる・寝る",
    "sniffing": "匂いを嗅ぐ",
    "scratching": "体を掻く",
    "walking": "歩く・通過する",
    "excreting": "排泄動作",
}
logger = logging.getLogger(__name__)

# 動画の保存要求のきっかけ。dog_visit は短い入退場をまとめた録画で、要求としては受け付けない。
RequestSource: TypeAlias = Literal[
    "manual", "detected", "dog_enter", "dog_exit", "early_alert", "outside_stay"
]
ClipSource: TypeAlias = RequestSource | Literal["dog_visit"]
ClipStatus: TypeAlias = Literal[
    "queued", "recording", "encoding", "ready", "failed", "removing", "removed"
]


class ClipRow(TypedDict):
    """clips テーブルの1行。"""

    id: str
    event_id: str | None
    source: ClipSource
    requested_at: str
    status: ClipStatus
    path: str | None
    start_at: str | None
    end_at: str | None
    duration: float | None
    fps: float | None
    trigger_offset: float | None
    pre_available: float | None
    post_available: float | None
    interrupted: int
    error: str | None
    label: str | None
    actual_start_offset: float | None
    actual_end_offset: float | None
    activity: str
    note: str
    scene_signature: str | None
    location: str | None


class AssessmentRow(TypedDict):
    """video_assessments テーブルの1行。"""

    clip_id: str
    label: str | None
    activity: str | None
    start_seconds: float | None
    end_seconds: float | None
    reason: str | None
    observed_at: str | None
    error: str | None
    assessed_at: str
    notice_claimed: int
    notified_at: str | None
    location: str | None
    provider: str
    model: str | None


class ClipStore:
    """動画の保存要求・状態・ラベルを events.sqlite3 の clips テーブルで管理する。"""

    def __init__(self, root: str | Path) -> None:
        self.store = Store(root)
        self.root = Path(root)
        (self.root / "clips").mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """events.sqlite3 への接続（スキーマは Store を作ったときに最新になっている）。"""
        return self.store.connect()

    # 動画にひも付く台帳。それぞれ1つのテーブル（とその列）だけを扱う。
    @property
    def assessments(self) -> "AssessmentStore":
        """動画の自動判定（video_assessments）。"""
        from .clip_reviews import AssessmentStore

        return AssessmentStore(self.store.database)

    @property
    def circling(self) -> "CirclingReviews":
        """旋回候補の分析結果（circling_reviews）。"""
        from .clip_reviews import CirclingReviews

        return CirclingReviews(self.store.database)

    @property
    def labels(self) -> "ClipLabels":
        """人のラベルと、動画からの記録の確定（clip_confirmations）。"""
        from .clip_labels import ClipLabels

        return ClipLabels(self)

    def queued(self) -> list[sqlite3.Row]:
        with self.connect() as db:
            return db.execute(
                "SELECT id,source FROM clips WHERE status='queued' ORDER BY requested_at DESC"
            ).fetchall()

    def video_path(self, row: ClipRow | sqlite3.Row | None) -> Path | None:
        """変換済みの動画のパス。clips/ の外を指す行や、ファイルがない行は None。"""
        if not row or row["status"] != "ready" or not row["path"]:
            return None
        path = (self.root / row["path"]).resolve()
        if path.is_relative_to((self.root / "clips").resolve()) and path.is_file():
            return path
        return None

    def live(self) -> bool:
        """監視プロセスが動画を収集できる状態か（直近のハートビートで判断）。"""
        with self.connect() as db:
            row = db.execute("SELECT * FROM capture_status WHERE id=1").fetchone()
        return bool(row and row["healthy"] and time.time() - row["heartbeat"] < 10)

    def heartbeat(self, healthy: bool) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO capture_status VALUES (1,?,?)", (time.time(), int(healthy))
            )

    def request(
        self,
        source: RequestSource = "manual",
        event_id: str | None = None,
        require_live: bool = False,
    ) -> str:
        """動画の保存要求を queued で登録し、ID を返す。監視側の ClipCollector.poll が拾う。"""
        if source not in get_args(RequestSource):
            raise ValueError("保存要求の種類が不正です")
        if require_live and not self.live():
            raise ValueError("監視を動画保存有効で起動し、カメラ接続を確認してください")
        clip_id = uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            pending = db.execute(
                "SELECT count(*) FROM clips WHERE status IN ('queued','recording','encoding')"
            ).fetchone()[0]
            if pending >= 4:
                raise ValueError("保存処理中です。完了を待ってください")
            db.execute(
                """INSERT INTO clips(id,event_id,source,requested_at,status)
                VALUES (?,?,?,?,'queued')""",
                (clip_id, event_id, source, utcnow()),
            )
        return clip_id

    def list(self, include_assessments: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM clips ORDER BY requested_at DESC"
        if include_assessments:
            query = """SELECT c.*, a.label AS review_label, a.error AS review_error,
                a.location AS review_location
                FROM clips c LEFT JOIN video_assessments a ON a.clip_id=c.id
                ORDER BY c.requested_at DESC"""
        with self.connect() as db:
            return [dict(row) for row in db.execute(query)]

    def get(self, clip_id: str) -> ClipRow | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
        return cast(ClipRow, dict(row)) if row else None

    def update(self, clip_id: str, **values: object) -> None:
        allowed = {
            "status",
            "source",
            "path",
            "start_at",
            "end_at",
            "duration",
            "fps",
            "trigger_offset",
            "pre_available",
            "post_available",
            "interrupted",
            "error",
            "scene_signature",
        }
        if not values or not values.keys() <= allowed:
            raise ValueError("更新項目が不正です")
        with self.connect() as db:
            # 列名は上の allowed で許可したものだけ。値はプレースホルダーで渡す。
            db.execute(
                "UPDATE clips SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",  # noqa: S608
                (*values.values(), clip_id),
            )
