"""動画の保存要求・状態・ラベル・Codex の判定の台帳（監視と GUI の両方から使う）。"""

import builtins
import json
import logging
import math
import sqlite3
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, TypeAlias, TypedDict, cast, get_args
from uuid import uuid4

from .events import (
    EXCRETION_LOCATIONS,
    EXCRETION_TYPES,
    EventRow,
    Store,
    utcnow,
    validate_location,
)

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
    """codex_assessments テーブルの1行。"""

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


class ClipStore:
    """動画の保存要求・状態・ラベルを events.sqlite3 の clips テーブルで管理する。"""

    def __init__(self, root: str | Path) -> None:
        self.store = Store(root)
        self.root = Path(root)
        (self.root / "clips").mkdir(parents=True, exist_ok=True)

    def connect(self) -> sqlite3.Connection:
        """events.sqlite3 への接続（スキーマは Store を作ったときに最新になっている）。"""
        return self.store.connect()

    def circling_targets(
        self, scene_signature: str, limit: int, clip_id: str | None = None
    ) -> list[ClipRow]:
        """旋回分析の対象にする動画。clip_id を指定すると分析済みでも返す。"""
        query = """SELECT c.* FROM clips c
            LEFT JOIN circling_reviews r ON r.clip_id=c.id
            WHERE c.status='ready' AND c.interrupted=0 AND c.scene_signature=?"""
        params: list[object] = [scene_signature]
        if clip_id is None:
            query += " AND r.clip_id IS NULL"
        else:
            query += " AND c.id=?"
            params.append(clip_id)
        with self.connect() as db:
            rows = db.execute(
                query + " ORDER BY c.label IS NULL, c.requested_at DESC LIMIT ?", (*params, limit)
            )
            return [cast(ClipRow, dict(row)) for row in rows]

    def circling_review(self, clip_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM circling_reviews WHERE clip_id=?", (clip_id,)
            ).fetchone()
        return {**row, "candidates": json.loads(row["candidates"])} if row else None

    def save_circling_review(
        self,
        clip_id: str,
        candidates: Sequence[tuple[float, float]],
        sampled: int,
        located: int,
    ) -> bool:
        with self.connect() as db:
            return (
                db.execute(
                    """INSERT INTO circling_reviews SELECT id, ?, ?, ? FROM clips
                WHERE id=? AND status='ready'
                ON CONFLICT(clip_id) DO UPDATE SET candidates=excluded.candidates,
                    sampled=excluded.sampled, located=excluded.located""",
                    (json.dumps(candidates), sampled, located, clip_id),
                ).rowcount
                == 1
            )

    def is_pinned(self, clip_id: str) -> bool:
        with self.connect() as db:
            return (
                db.execute("SELECT 1 FROM clip_protections WHERE clip_id=?", (clip_id,)).fetchone()
                is not None
            )

    def pin(self, clip_id: str, protected: bool) -> None:
        """動画を自動整理の対象から外す（protected=False で戻す）。"""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM clips WHERE id=?", (clip_id,)).fetchone()
            if row is None or row["status"] != "ready":
                raise ValueError("保護する動画を確認してください")
            if protected:
                db.execute("INSERT OR IGNORE INTO clip_protections VALUES (?)", (clip_id,))
            else:
                db.execute("DELETE FROM clip_protections WHERE clip_id=?", (clip_id,))

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

    def assessment(self, clip_id: str) -> AssessmentRow | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM codex_assessments WHERE clip_id=?", (clip_id,)
            ).fetchone()
        return cast(AssessmentRow, dict(row)) if row else None

    def latest_assessment(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT a.*, c.requested_at, c.label AS human_label, c.location AS human_location
                FROM codex_assessments a JOIN clips c ON c.id=a.clip_id
                ORDER BY a.assessed_at DESC, c.requested_at DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def pending_assessments(
        self,
        limit: int,
        since: str | None = None,
        clip_id: str | None = None,
        scene_signature: str | None = None,
    ) -> list[ClipRow]:
        """Codex の判定を待つ動画。since を指定すると、それ以降の未判定の動画だけを古い順に返す。"""
        conditions = ["c.status='ready'", "c.label IS NULL"]
        params: list[object] = []
        if since is not None:
            conditions.extend(["c.requested_at>=?", "a.clip_id IS NULL"])
            params.append(since)
        else:
            conditions.append("(a.clip_id IS NULL OR a.error IS NOT NULL)")
        if scene_signature is not None:
            conditions.append("c.scene_signature=?")
            params.append(scene_signature)
        if clip_id is not None:
            conditions.append("c.id=?")
            params.append(clip_id)
        order = "ASC" if since is not None else "DESC"
        # 条件はこの関数内の固定の文字列だけで組み立て、値はすべてプレースホルダーで渡す。
        with self.connect() as db:
            return [
                cast(ClipRow, dict(row))
                for row in db.execute(
                    """SELECT c.* FROM clips c
                    LEFT JOIN codex_assessments a ON a.clip_id=c.id WHERE """  # noqa: S608
                    + " AND ".join(conditions)
                    + f" ORDER BY c.requested_at {order} LIMIT ?",
                    (*params, limit),
                )
            ]

    def save_assessment(
        self,
        clip_id: str,
        label: str | None = None,
        activity: str | None = None,
        start_seconds: float | None = None,
        end_seconds: float | None = None,
        reason: str | None = None,
        observed_at: str | None = None,
        error: str | None = None,
        location: str | None = None,
    ) -> bool:
        """判定を保存する。既存の判定は失敗していたときだけ上書きし、保存できたら True。"""
        validate_location(location)
        with self.connect() as db:
            return (
                db.execute(
                    """INSERT INTO codex_assessments
                (clip_id,label,activity,start_seconds,end_seconds,reason,observed_at,error,assessed_at,location)
                SELECT id,?,?,?,?,?,?,?,?,? FROM clips WHERE id=? AND label IS NULL AND status='ready'
                ON CONFLICT(clip_id) DO UPDATE SET label=excluded.label,activity=excluded.activity,
                start_seconds=excluded.start_seconds,end_seconds=excluded.end_seconds,
                reason=excluded.reason,observed_at=excluded.observed_at,error=excluded.error,
                assessed_at=excluded.assessed_at,location=excluded.location WHERE codex_assessments.error IS NOT NULL""",
                    (
                        label,
                        activity,
                        start_seconds,
                        end_seconds,
                        reason,
                        observed_at,
                        error,
                        utcnow(),
                        location,
                        clip_id,
                    ),
                ).rowcount
                == 1
            )

    def claim_assessment_notice(self, clip_id: str) -> bool:
        """通知の権利を取る。同じ記録や近い時刻の同じ判定を通知済みなら False。"""
        with self.connect() as db:
            return (
                db.execute(
                    """UPDATE codex_assessments AS candidate SET notice_claimed=1
                WHERE candidate.clip_id=? AND candidate.notice_claimed=0
                AND EXISTS (
                    SELECT 1 FROM clips c WHERE c.id=candidate.clip_id AND c.label IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM codex_assessments a JOIN clips previous ON previous.id=a.clip_id
                        WHERE a.notice_claimed=1
                        AND COALESCE(a.location,'tray')=COALESCE(candidate.location,'tray') AND (
                            (candidate.location IS NOT 'outside' AND c.event_id IS NOT NULL
                                AND c.event_id=previous.event_id)
                            OR (c.scene_signature IS previous.scene_signature
                                AND (a.label=candidate.label OR 'both' IN (a.label,candidate.label))
                                AND ABS(julianday(a.observed_at)-julianday(candidate.observed_at))
                                    < 5.001/86400)
                        )
                    )
                )""",
                    (clip_id,),
                ).rowcount
                == 1
            )

    def finish_assessment_notice(self, clip_id: str, sent: bool) -> None:
        stamp = utcnow() if sent else None
        with self.connect() as db:
            db.execute(
                "UPDATE codex_assessments SET notice_claimed=?,notified_at=? WHERE clip_id=?",
                (int(sent), stamp, clip_id),
            )
            if sent:
                db.execute(
                    """UPDATE events SET notified_at=? WHERE id=(
                        SELECT c.event_id FROM clips c JOIN codex_assessments a ON a.clip_id=c.id
                        WHERE c.id=? AND a.location IS NOT 'outside'
                    )""",
                    (stamp, clip_id),
                )

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
            query = """SELECT c.*, a.label AS codex_label, a.error AS codex_error,
                a.location AS codex_location
                FROM clips c LEFT JOIN codex_assessments a ON a.clip_id=c.id
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

    def confirmation(self, clip_id: str) -> str | None:
        """動画から確定した記録の ID。"""
        with self.connect() as db:
            row = db.execute(
                "SELECT event_id FROM clip_confirmations WHERE clip_id=?", (clip_id,)
            ).fetchone()
        return row["event_id"] if row else None

    # 以下では list メソッドと名前が重なるため、組み込みの list は builtins から参照する。
    def record_candidates(self, clip: ClipRow) -> builtins.list[EventRow]:
        """動画の時間帯の前後30秒に重なる、確定記録と検知イベント。"""
        start = datetime.fromisoformat(clip["start_at"] or clip["requested_at"])
        margin = timedelta(seconds=30)
        end = start + timedelta(seconds=clip["duration"] or 0)
        return [
            record
            for record in self.store.list()
            if record["id"] == clip["event_id"]
            or (
                (record["feedback"] == 1 or record["kind"] == "SUSPECTED")
                and datetime.fromisoformat(record["actual_start"] or record["timestamp"])
                <= end + margin
                and datetime.fromisoformat(
                    record["actual_end"] or record["actual_start"] or record["timestamp"]
                )
                >= start - margin
            )
        ]

    def record_videos(self) -> dict[str, builtins.list[str]]:
        """記録の ID ごとの、ひも付いた動画の ID。"""
        links: dict[str, builtins.list[str]] = {}
        with self.connect() as db:
            for row in db.execute(
                """SELECT c.id, COALESCE(k.event_id, c.event_id) AS record_id
                FROM clips c LEFT JOIN clip_confirmations k ON k.clip_id=c.id
                WHERE c.status='ready' AND c.path IS NOT NULL ORDER BY c.requested_at"""
            ):
                if row["record_id"]:
                    links.setdefault(row["record_id"], []).append(row["id"])
        return links

    def label(
        self,
        clip_id: str,
        label: str,
        start: float | None = None,
        end: float | None = None,
        activity: str = "other",
        note: str = "",
        *,
        record_id: str | None = None,
        location: str | None = None,
    ) -> None:
        """動画にラベルを付ける。record_id を渡すと排泄の記録としても確定する（"new" で新規）。"""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM clips WHERE id=?", (clip_id,)).fetchone()
            if (
                not row
                or row["status"] != "ready"
                or label not in CLIP_LABELS
                or activity not in ACTIVITIES
            ):
                raise ValueError("保存済み動画とラベルを確認してください")
            for value in (start, end):
                if value is not None and (
                    not math.isfinite(value) or not 0 <= value <= row["duration"]
                ):
                    raise ValueError("開始・完了は動画内の秒数で指定してください")
            if start is not None and end is not None and end < start:
                raise ValueError("完了は開始以降を指定してください")
            if len(note) > 2000:
                raise ValueError("メモは2000文字以内です")
            validate_location(location)
            location = row["location"] if location is None else location or None
            if label in ("none", "uncertain"):
                start = end = location = None
            if record_id is not None:
                location = self._confirm(db, row, record_id, label, start, end, note, location)
            db.execute(
                """UPDATE clips SET label=?,actual_start_offset=?,actual_end_offset=?,
                activity=?,note=?,location=? WHERE id=?""",
                (label, start, end, activity, note, location, clip_id),
            )

    def _confirm(
        self,
        db: sqlite3.Connection,
        clip: sqlite3.Row,
        record_id: str,
        label: str,
        start: float | None,
        end: float | None,
        note: str,
        location: str | None,
    ) -> str | None:
        if label not in ("feces", "urine", "both"):
            raise ValueError("確定記録には排便・排尿・両方のいずれかを選んでください")
        link = db.execute(
            "SELECT event_id FROM clip_confirmations WHERE clip_id=?", (clip["id"],)
        ).fetchone()
        if link:
            if record_id not in ("new", link["event_id"]):
                raise ValueError("この動画はすでに別の記録にひも付いています")
            record_id = link["event_id"]

        if location == "outside" and record_id == clip["event_id"]:
            raise ValueError("トレー外の排泄は、トレー内の検知とは別の記録を選んでください")
        if not clip["start_at"] and (start is not None or end is not None):
            raise ValueError("録画の開始日時が分からないため、動画内の時刻を変換できません")
        captured = datetime.fromisoformat(clip["start_at"] or clip["requested_at"])
        start_at = (captured + timedelta(seconds=start)).isoformat() if start is not None else None
        end_at = (captured + timedelta(seconds=end)).isoformat() if end is not None else None

        if record_id == "new":
            if clip["event_id"] and location != "outside":
                raise ValueError("対応する検知記録を保存先に選んでください")
            record_id = self.store._insert_excretion(
                db,
                label,
                "VIDEO",
                start_at or end_at or captured.isoformat(),
                start_at,
                end_at,
                note,
                location=location,
            )
        else:
            record = db.execute(
                """SELECT e.*, l.excretion_type, l.note FROM events e
                LEFT JOIN excretion_labels l ON l.event_id=e.id WHERE e.id=?""",
                (record_id,),
            ).fetchone()
            if record is None:
                raise ValueError("保存先の記録を選んでください")
            known_location = record["location"]
            if record["kind"] == "SUSPECTED" and known_location is None:
                known_location = "tray"
            if location and known_location and location != known_location:
                raise ValueError(f"保存先の場所は{EXCRETION_LOCATIONS[known_location]}です")
            location = location or known_location
            if record["feedback"] == 1:
                existing = record["excretion_type"] or "feces"
                if existing not in (label, "both", "unspecified"):
                    raise ValueError(f"確定記録の種類は{EXCRETION_TYPES[existing]}です")
                if location and record["location"] is None:
                    db.execute("UPDATE events SET location=? WHERE id=?", (location, record_id))
            else:
                self.store._classify(
                    db,
                    record_id,
                    label,
                    start_at or record["actual_start"],
                    end_at or record["actual_end"],
                    note or record["note"] or "",
                    location=location,
                )
        if link is None:
            db.execute("INSERT INTO clip_confirmations VALUES (?,?)", (clip["id"], record_id))
        return location
