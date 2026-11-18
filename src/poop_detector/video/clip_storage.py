"""動画の保存容量と、古いファイルの整理（利用者が選んだもの・容量上限に近づいたときの自動整理）。"""

import fcntl
import logging
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from ..errors import AlreadyRunningError
from .thumbnails import thumbnail_path

if TYPE_CHECKING:
    # clips がこのモジュールを読み込むため、型の参照だけにする。
    from .clips import ClipStore

MIB = 1024**2
DEFAULT_DISK_MIB = 4096
RETENTION_DAYS = (1, 3, 7, 14, 30)  # 整理の画面で選べる古さ（日）
# 動画の自動判定（video_review）と整理を同時に行わないための排他のロックファイル。
REVIEW_LOCK = ".review.lock"
CLEANUP_LIMIT = 100  # 1回の整理で表示・削除する最大の本数（画面とデータベースの負荷を抑える）
# 使用量が上限のこの割合に達したら、録画の前に古い動画を自動で整理し、TARGET を下回るまで消す。
AUTO_CLEANUP_START = 0.9
AUTO_CLEANUP_TARGET = 0.8
# 自動整理は、この日数より古い動画だけを消す（直近の動画は人の確認と自動判定を待つ）。
AUTO_CLEANUP_MIN_DAYS = RETENTION_DAYS[0]
# 上限のこの割合を超える分は、差分検知と GUI からの保存要求のために空けておく。
RESERVE_RATIO = 0.95
RESERVED_SOURCES = frozenset({"detected", "manual"})
# 自動整理で先に消すきっかけ。トレー外の滞在、犬の出入り、ほかの順で、同じ順位は古い順。
CLEANUP_ORDER = (
    "CASE c.source WHEN 'outside_stay' THEN 0"
    " WHEN 'dog_visit' THEN 1 WHEN 'dog_enter' THEN 1 WHEN 'dog_exit' THEN 1 ELSE 2 END"
)
logger = logging.getLogger(__name__)

CANDIDATE_SQL = """
    SELECT c.id, c.path, c.requested_at, c.status, c.source, a.label AS review_label
    FROM clips c LEFT JOIN video_assessments a ON a.clip_id=c.id
    WHERE c.status IN ('ready', 'removing')
      AND julianday(c.requested_at) <= julianday(?)
      AND c.label IS NULL AND c.event_id IS NULL
      AND (a.label IS NULL OR a.label NOT IN ('feces', 'urine', 'both')
           OR EXISTS (SELECT 1 FROM review_dismissals d WHERE d.clip_id=c.id))
      AND NOT EXISTS (SELECT 1 FROM clip_confirmations k WHERE k.clip_id=c.id)
      AND NOT EXISTS (SELECT 1 FROM clip_protections p WHERE p.clip_id=c.id)
      AND NOT EXISTS (SELECT 1 FROM clips other WHERE other.path=c.path AND other.id<>c.id)
"""


def recording_usage(root: Path) -> int:
    """保存済みの動画ファイルの合計バイト数。"""
    used = 0
    for path in (root / "clips").glob("*.mp4"):
        try:
            used += path.stat().st_size
        except FileNotFoundError:
            # 別プロセスで整理や変換が完了した場合は、次回の計測に反映する。
            continue
    return used


def retention_days(value: str | int) -> int:
    """フォームの値などを、選べる保持日数のどれかに変換する。"""
    days = int(value)
    if days not in RETENTION_DAYS:
        raise ValueError("保持期間を選んでください")
    return days


def cutoff(days: str | int) -> str:
    return (datetime.now(UTC) - timedelta(days=retention_days(days))).isoformat()


def candidate_file(store: "ClipStore", row: sqlite3.Row) -> tuple[Path, int] | None:
    """整理してよい動画のパスとサイズ。clips/ の直下にある本人の動画だけを対象にする。"""
    if row["path"] != f"clips/{row['id']}.mp4":
        return None
    directory = store.root / "clips"
    path = directory / f"{row['id']}.mp4"
    if path.parent != directory or directory.is_symlink() or path.is_symlink():
        return None
    try:
        if path.is_file():
            return path, path.stat().st_size
    except FileNotFoundError:
        pass
    if row["status"] == "removing" and not path.exists():
        return path, 0
    return None


def cleanup_candidates(
    store: "ClipStore", days: int, order: str = "julianday(c.requested_at)"
) -> list[tuple[sqlite3.Row, int]]:
    with store.connect() as db:
        # order はこのモジュールの固定の文字列だけを受け取る。値はプレースホルダーで渡す。
        rows = db.execute(CANDIDATE_SQL + f" ORDER BY {order}", (cutoff(days),))
        candidates: list[tuple[sqlite3.Row, int]] = []
        for row in rows:
            file = candidate_file(store, row)
            if file is not None:
                candidates.append((row, file[1]))
                if len(candidates) == CLEANUP_LIMIT:
                    break
    return candidates


def cleanup_selected(store: "ClipStore", clip_ids: Iterable[str], days: int) -> tuple[int, int]:
    """選ばれた動画を消し、(消した件数, 消せなかった件数) を返す。"""
    ids = list(dict.fromkeys(clip_ids))
    if not 1 <= len(ids) <= CLEANUP_LIMIT:
        raise ValueError("整理する動画を選んでください")
    before = cutoff(days)
    removed = 0
    # 自動判定が動画を読み取っている間は整理を開始しない。
    with (store.root / REVIEW_LOCK).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AlreadyRunningError(
                "動画の自動判定中です。完了後にもう一度お試しください"
            ) from None
        for clip_id in ids:
            try:
                removed += remove_video(store, clip_id, before)
            except OSError:
                logger.warning("動画ファイルを整理できませんでした")
    return removed, len(ids) - removed


def free_space(store: "ClipStore", limit: int) -> int:
    """使用量が上限に近ければ古い動画を消して空きを作り、消した本数を返す。

    消すのは、整理の画面と同じ条件（ラベル・検知や確定記録へのひも付け・排泄候補の判定・
    保護がない）を満たし、AUTO_CLEANUP_MIN_DAYS より古い動画だけ。自動判定が動画を
    読んでいる間は消さず、次の録画のときにやり直す。
    """
    used = recording_usage(store.root)
    if used < limit * AUTO_CLEANUP_START:
        return 0
    removed = 0
    with (store.root / REVIEW_LOCK).open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("動画の自動判定中のため、録画容量の自動整理を次の録画まで延ばします")
            return 0
        before = cutoff(AUTO_CLEANUP_MIN_DAYS)
        order = f"{CLEANUP_ORDER}, julianday(c.requested_at)"
        for row, size in cleanup_candidates(store, AUTO_CLEANUP_MIN_DAYS, order):
            if used <= limit * AUTO_CLEANUP_TARGET:
                break
            try:
                if remove_video(store, row["id"], before):
                    removed += 1
                    used -= size
            except OSError:
                logger.warning("録画容量の自動整理で動画ファイルを消せませんでした")
    if removed:
        logger.info("録画容量が上限に近いため、古い動画を%d本整理しました", removed)
    return removed


def over_limit(used: int, limit: int, source: str) -> bool:
    """この使用量で、このきっかけの動画を保存すると上限を超えるか。"""
    if source in RESERVED_SOURCES:
        return used > limit
    return used > limit * RESERVE_RATIO


def remove_video(store: "ClipStore", clip_id: str, before: str) -> bool:
    query = CANDIDATE_SQL + " AND c.id=?"
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(query, (before, clip_id)).fetchone()
        if row is None or candidate_file(store, row) is None:
            return False
        # ファイルを消す前に状態を確定し、中断後も同じ操作で再開できるようにする。
        db.execute("UPDATE clips SET status='removing' WHERE id=?", (clip_id,))
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(query, (before, clip_id)).fetchone()
        file = candidate_file(store, row) if row is not None else None
        if file is None or row["status"] != "removing":
            return False
        # サムネイルを先に消す。途中で止まっても、動画の整理をやり直せば残らない。
        thumbnail_path(file[0]).unlink(missing_ok=True)
        file[0].unlink(missing_ok=True)
        db.execute("UPDATE clips SET status='removed',path=NULL WHERE id=?", (clip_id,))
    return True


def is_protected(store: "ClipStore", clip_id: str) -> bool:
    """自動整理の対象から外した動画か。"""
    with store.connect() as db:
        return (
            db.execute("SELECT 1 FROM clip_protections WHERE clip_id=?", (clip_id,)).fetchone()
            is not None
        )


def protect(store: "ClipStore", clip_id: str, protected: bool) -> None:
    """動画を自動整理の対象から外す（protected=False で戻す）。"""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT status FROM clips WHERE id=?", (clip_id,)).fetchone()
        if row is None or row["status"] != "ready":
            raise ValueError("保護する動画を確認してください")
        if protected:
            db.execute("INSERT OR IGNORE INTO clip_protections VALUES (?)", (clip_id,))
        else:
            db.execute("DELETE FROM clip_protections WHERE clip_id=?", (clip_id,))
