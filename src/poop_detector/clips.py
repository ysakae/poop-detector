"""上限付き動画バッファと、別プロセスのGUIから使える収集台帳。"""

import builtins
import fcntl
import itertools
import json
import logging
import math
import queue
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Literal, Self, TypeAlias, TypedDict, cast, get_args
from uuid import uuid4

import cv2

from .clip_storage import DEFAULT_DISK_MIB, MIB, recording_usage
from .events import (
    EXCRETION_LOCATIONS,
    EXCRETION_TYPES,
    EventRow,
    Store,
    ensure_location_column,
    utcnow,
    validate_location,
)
from .frames import Frame

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
SHORT_VISIT_SECONDS = 15

# 動画の保存要求のきっかけ。dog_visit は短い入退場をまとめた録画で、要求としては受け付けない。
RequestSource: TypeAlias = Literal[
    "manual", "detected", "dog_enter", "dog_exit", "early_alert", "outside_stay"
]
ClipSource: TypeAlias = RequestSource | Literal["dog_visit"]
ClipStatus: TypeAlias = Literal[
    "queued", "recording", "encoding", "ready", "failed", "removing", "removed"
]
# (取得時刻 monotonic, 取得時刻 UTC の ISO 8601, JPEG のバイト列)
Sample: TypeAlias = tuple[float, str, bytes]
Encoder: TypeAlias = Callable[[Sequence[Sample], Path, float], float]


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
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS clips (
                id TEXT PRIMARY KEY, event_id TEXT, source TEXT, requested_at TEXT,
                status TEXT, path TEXT, start_at TEXT, end_at TEXT, duration REAL,
                fps REAL, trigger_offset REAL, pre_available REAL, post_available REAL,
                interrupted INTEGER DEFAULT 0, error TEXT, label TEXT,
                actual_start_offset REAL, actual_end_offset REAL, activity TEXT DEFAULT 'other',
                note TEXT DEFAULT '', scene_signature TEXT, location TEXT)""")
            ensure_location_column(db, "clips")
            db.execute(
                "CREATE INDEX IF NOT EXISTS clips_by_status_time ON clips(status, requested_at)"
            )
            db.execute("""CREATE TABLE IF NOT EXISTS capture_status (
                id INTEGER PRIMARY KEY CHECK(id=1), heartbeat REAL, healthy INTEGER)""")

            db.execute("""CREATE TABLE IF NOT EXISTS codex_assessments (
                clip_id TEXT PRIMARY KEY, label TEXT, activity TEXT, start_seconds REAL,
                end_seconds REAL, reason TEXT, observed_at TEXT, error TEXT,
                assessed_at TEXT NOT NULL, notice_claimed INTEGER DEFAULT 0, notified_at TEXT,
                location TEXT)""")
            ensure_location_column(db, "codex_assessments")

            db.execute("""CREATE TABLE IF NOT EXISTS clip_confirmations (
                clip_id TEXT PRIMARY KEY, event_id TEXT NOT NULL)""")
            db.execute("""CREATE TABLE IF NOT EXISTS clip_protections (clip_id TEXT PRIMARY KEY)""")
            db.execute("""CREATE TABLE IF NOT EXISTS circling_reviews (
                clip_id TEXT PRIMARY KEY, candidates TEXT NOT NULL,
                sampled INTEGER NOT NULL, located INTEGER NOT NULL)""")

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
        with self.store.connect() as db:
            rows = db.execute(
                query + " ORDER BY c.label IS NULL, c.requested_at DESC LIMIT ?", (*params, limit)
            )
            return [cast(ClipRow, dict(row)) for row in rows]

    def circling_review(self, clip_id: str) -> dict[str, Any] | None:
        with self.store.connect() as db:
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
        with self.store.connect() as db:
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
        with self.store.connect() as db:
            return (
                db.execute("SELECT 1 FROM clip_protections WHERE clip_id=?", (clip_id,)).fetchone()
                is not None
            )

    def pin(self, clip_id: str, protected: bool) -> None:
        """動画を自動整理の対象から外す（protected=False で戻す）。"""
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM clips WHERE id=?", (clip_id,)).fetchone()
            if row is None or row["status"] != "ready":
                raise ValueError("保護する動画を確認してください")
            if protected:
                db.execute("INSERT OR IGNORE INTO clip_protections VALUES (?)", (clip_id,))
            else:
                db.execute("DELETE FROM clip_protections WHERE clip_id=?", (clip_id,))

    def queued(self) -> list[sqlite3.Row]:
        with self.store.connect() as db:
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
        with self.store.connect() as db:
            row = db.execute(
                "SELECT * FROM codex_assessments WHERE clip_id=?", (clip_id,)
            ).fetchone()
        return cast(AssessmentRow, dict(row)) if row else None

    def latest_assessment(self) -> dict[str, Any] | None:
        with self.store.connect() as db:
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
        with self.store.connect() as db:
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
        with self.store.connect() as db:
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
        with self.store.connect() as db:
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
        with self.store.connect() as db:
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
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM capture_status WHERE id=1").fetchone()
        return bool(row and row["healthy"] and time.time() - row["heartbeat"] < 10)

    def heartbeat(self, healthy: bool) -> None:
        with self.store.connect() as db:
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
        with self.store.connect() as db:
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
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(query)]

    def get(self, clip_id: str) -> ClipRow | None:
        with self.store.connect() as db:
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
        with self.store.connect() as db:
            # 列名は上の allowed で許可したものだけ。値はプレースホルダーで渡す。
            db.execute(
                "UPDATE clips SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",  # noqa: S608
                (*values.values(), clip_id),
            )

    def confirmation(self, clip_id: str) -> str | None:
        """動画から確定した記録の ID。"""
        with self.store.connect() as db:
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
        with self.store.connect() as db:
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
        with self.store.connect() as db:
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


def encode_mp4(samples: Sequence[Sample], path: Path, fps: float) -> float:
    """JPEG のサンプルを一定の FPS の MP4 にし、動画の秒数を返す。"""
    if not samples:
        raise ValueError("保存するフレームがありません")
    # カメラの揺れる取得間隔を一定FPSへ揃える。欠落区間は同じフレームで補間する。
    frames: list[bytes] = []
    index = 0
    count = max(1, round((samples[-1][0] - samples[0][0]) * fps) + 1)
    for step in range(count):
        target = samples[0][0] + step / fps
        while index + 1 < len(samples) and samples[index + 1][0] <= target + 1e-6:
            index += 1
        frames.append(samples[index][2])
    executable = shutil.which("ffmpeg")
    if not executable:
        raise OSError("FFmpegが見つかりません")
    # 連結JPEGをメモリーへ複製せず、一時ファイルからFFmpegへ渡す。
    with tempfile.TemporaryFile(dir=Path(path).parent) as spool:
        for frame in frames:
            spool.write(frame)
        spool.seek(0)
        # shutil.which で見つけた FFmpeg を、固定の引数と保存先のパスだけで起動する。
        subprocess.run(  # noqa: S603
            [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "image2pipe",
                "-framerate",
                str(fps),
                "-vcodec",
                "mjpeg",
                "-i",
                "pipe:0",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "24",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(path),
            ],
            stdin=spool,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
            timeout=30,
        )
    return count / fps


class ClipCollector:
    """フレームをメモリー上限付きで保持し、保存要求の前後を MP4 に変換する。"""

    def __init__(
        self,
        root: str | Path,
        before: float = 30,
        after: float = 30,
        fps: float = 5,
        max_width: int = 1280,
        memory_mb: float = 128,
        disk_mb: float = DEFAULT_DISK_MIB,
        encoder: Encoder = encode_mp4,
    ) -> None:
        self.store = ClipStore(root)
        self.before, self.after, self.fps = before, after, fps
        self.max_width = max_width
        self.memory_limit, self.disk_limit = int(memory_mb * 1024**2), int(disk_mb * MIB)
        self.encoder = encoder
        self.buffer: deque[Sample] = deque()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.queue: queue.Queue[str | None] = queue.Queue(maxsize=4)
        self.worker = threading.Thread(target=self._worker, daemon=True)
        self.generation: tuple[int, tuple[int, ...]] | None = None
        self.last_sample = float("-inf")
        self.last_frame = float("-inf")
        self.last_poll = float("-inf")
        self.scene_signature: str | None = None
        self.capture_lock: IO[str] | None = None

    def __enter__(self) -> Self:
        lock = (self.store.root / ".capture.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise ValueError("別の動画監視が起動中です") from None
        self.capture_lock = lock
        with self.store.store.connect() as db:
            # 前回の異常終了で残った処理中行を新しい録画と混ぜない。
            db.execute(
                "UPDATE clips SET status='failed',error='前回の監視が終了したため保存できませんでした' WHERE status IN ('queued','recording','encoding')"
            )
        self.worker.start()
        return self

    def _bytes(self) -> int:
        return sum(len(s[2]) for s in self.buffer) + sum(
            sum(len(s[2]) for s in job["samples"]) for job in self.jobs.values()
        )

    def feed(self, frame: Frame | None, now: float | None = None, generation: int = 0) -> None:
        """カメラのフレームを受け取り、clip_fps の間隔で JPEG にして保持する。"""
        now = time.monotonic() if now is None else now
        with self.lock:
            if frame is None:
                if self.buffer or self.jobs:
                    self._interrupt()
                return
            identity = (generation, frame.shape)
            if self.generation is not None and self.generation != identity:
                self._interrupt()
            self.generation = identity
            self.last_frame = now
            if now - self.last_sample < 1 / self.fps - 1e-6:
                return
            self.last_sample = now
            height, width = frame.shape[:2]
            scale = min(1, self.max_width / width)
            size = (max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2))
            resized = cv2.resize(frame, size) if size != (width, height) else frame
            ok, encoded = cv2.imencode(".jpg", resized, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ok:
                return
            sample = (now, utcnow(), encoded.tobytes())
            self.buffer.append(sample)
            while self.buffer and (now - self.buffer[0][0] > self.before + 1e-6):
                self.buffer.popleft()
            for job in self.jobs.values():
                if job["status"] == "recording" and job["trigger"] < now <= job["end"] + 1e-6:
                    job["samples"].append(sample)
            while self.buffer and self._bytes() > self.memory_limit:
                self.buffer.popleft()
            if self._bytes() > self.memory_limit:
                for clip_id in list(self.jobs):
                    if self.jobs[clip_id]["status"] == "recording":
                        self.store.update(
                            clip_id, status="failed", error="録画メモリー上限に達しました"
                        )
                        del self.jobs[clip_id]

    def _interrupt(self) -> None:
        for clip_id, job in list(self.jobs.items()):
            if job["status"] == "recording":
                self._finish(clip_id, interrupted=True)
        self.buffer.clear()
        self.last_sample = self.last_frame = float("-inf")

    def trigger(
        self,
        event_id: str | None = None,
        source: RequestSource = "detected",
        *,
        now: float | None = None,
    ) -> str | None:
        """監視側から保存を要求する。同時処理数を超えたら None を返す。"""
        now = time.monotonic() if now is None else now
        with self.lock:
            if source == "dog_exit" and event_id is None:
                for clip_id, job in reversed(self.jobs.items()):
                    if (
                        job["status"] == "recording"
                        and job["source"] == "dog_enter"
                        and 0 <= now - job["trigger"] <= SHORT_VISIT_SECONDS
                        and now <= job["end"]
                        and now - self.last_frame < 5
                    ):
                        job["end"] = now + self.after
                        job["source"] = "dog_visit"
                        return clip_id
            try:
                clip_id = self.store.request(source, event_id)
                self.poll(now=now, force=True)
                return clip_id
            except ValueError:
                logger.warning("動画保存の同時処理数を超えています")
                return None

    def poll(self, now: float | None = None, force: bool = False) -> None:
        """GUI からの保存要求を拾い、録画の終わった要求を変換に回す。"""
        now = time.monotonic() if now is None else now
        with self.lock:
            if not force and now - self.last_poll < 0.2:
                return
            self.last_poll = now
            self.store.heartbeat(now - self.last_frame < 5)
            for row in self.store.queued():
                clip_id = row["id"]
                if not self.buffer or now - self.last_frame >= 5:
                    self.store.update(
                        clip_id, status="failed", error="カメラ映像を取得できていません"
                    )
                    continue
                samples = [s for s in self.buffer if now - s[0] <= self.before + 1e-6]
                self.jobs[clip_id] = {
                    "samples": samples,
                    "source": row["source"],
                    "trigger": now,
                    "end": now + self.after,
                    "status": "recording",
                }
                if self._bytes() > self.memory_limit:
                    del self.jobs[clip_id]
                    self.store.update(
                        clip_id, status="failed", error="録画メモリー上限に達しました"
                    )
                else:
                    self.store.update(
                        clip_id, status="recording", scene_signature=self.scene_signature
                    )
            for clip_id, job in list(self.jobs.items()):
                if job["status"] == "recording" and now >= job["end"]:
                    self._finish(clip_id)

    def _finish(self, clip_id: str, interrupted: bool = False) -> None:
        job = self.jobs[clip_id]
        samples = job["samples"]
        if not samples:
            self.store.update(clip_id, status="failed", error="保存するフレームがありません")
            del self.jobs[clip_id]
            return
        job["interrupted"] = bool(job.get("interrupted") or interrupted)
        gaps = any(b[0] - a[0] > max(1, 3 / self.fps) for a, b in itertools.pairwise(samples))
        try:
            self.store.update(
                clip_id,
                status="encoding",
                source=job["source"],
                start_at=samples[0][1],
                end_at=samples[-1][1],
                fps=self.fps,
                trigger_offset=max(0, job["trigger"] - samples[0][0]),
                pre_available=max(0, job["trigger"] - samples[0][0]),
                post_available=max(0, samples[-1][0] - job["trigger"]),
                interrupted=int(
                    job["interrupted"] or gaps or samples[-1][0] < job["end"] - 1 / self.fps
                ),
            )
        except sqlite3.Error:
            logger.warning("動画の変換開始をDBに保存できません。次回再試行します")
            return
        job["status"] = "encoding"
        self.queue.put_nowait(clip_id)

    def _worker(self) -> None:
        while True:
            clip_id = self.queue.get()
            try:
                if clip_id is None:
                    return
                job = self.jobs[clip_id]
                path = self.store.root / "clips" / f"{clip_id}.mp4"
                temporary = path.with_suffix(".part.mp4")
                try:
                    used = recording_usage(self.store.root)
                    if used >= self.disk_limit:
                        raise OSError("動画保存容量上限")
                    duration = self.encoder(job["samples"], temporary, self.fps)
                    if used + temporary.stat().st_size > self.disk_limit:
                        raise OSError("動画保存容量上限")
                    temporary.replace(path)
                    self.store.update(
                        clip_id, status="ready", path=f"clips/{path.name}", duration=duration
                    )
                except (OSError, ValueError, subprocess.SubprocessError, sqlite3.Error):
                    try:
                        self.store.update(
                            clip_id,
                            status="failed",
                            error="動画保存に失敗しました。FFmpeg・保存容量・DBを確認してください",
                        )
                    except sqlite3.Error:
                        logger.error("動画の失敗状態をDBへ保存できませんでした")
                    try:
                        if temporary.exists():
                            temporary.unlink()
                    except OSError:
                        logger.error("動画の一時ファイルを片付けられませんでした")
                finally:
                    with self.lock:
                        self.jobs.pop(clip_id, None)
            finally:
                self.queue.task_done()

    def drain(self) -> None:
        self.queue.join()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        with self.lock:
            self._interrupt()
        self.store.heartbeat(False)
        self.queue.put(None)
        self.worker.join()
        if self.capture_lock is not None:
            self.capture_lock.close()
