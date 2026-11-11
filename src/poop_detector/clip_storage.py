"""動画の保存容量と、利用者が選んだ古いファイルの整理。"""

import fcntl
import logging
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # clips がこのモジュールを読み込むため、型の参照だけにする。
    from .clips import ClipStore

MIB = 1024**2
DEFAULT_DISK_MIB = 4096
RETENTION_DAYS = (1, 3, 7, 14, 30)
CLEANUP_LIMIT = 100
logger = logging.getLogger(__name__)

CANDIDATE_SQL = """
    SELECT c.id, c.path, c.requested_at, c.status, c.source, a.label AS codex_label
    FROM clips c LEFT JOIN codex_assessments a ON a.clip_id=c.id
    WHERE c.status IN ('ready', 'removing')
      AND julianday(c.requested_at) <= julianday(?)
      AND c.label IS NULL AND c.event_id IS NULL
      AND (a.label IS NULL OR a.label NOT IN ('feces', 'urine', 'both'))
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


def cleanup_candidates(store: "ClipStore", days: int) -> list[tuple[sqlite3.Row, int]]:
    with store.connect() as db:
        rows = db.execute(CANDIDATE_SQL + " ORDER BY julianday(c.requested_at)", (cutoff(days),))
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
    # Codexが動画を読み取っている間は整理を開始しない。
    with (store.root / ".codex.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("動画の自動判定中です。完了後にもう一度お試しください") from None
        for clip_id in ids:
            try:
                removed += remove_video(store, clip_id, before)
            except OSError:
                logger.warning("動画ファイルを整理できませんでした")
    return removed, len(ids) - removed


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
        file[0].unlink(missing_ok=True)
        db.execute("UPDATE clips SET status='removed',path=NULL WHERE id=?", (clip_id,))
    return True
