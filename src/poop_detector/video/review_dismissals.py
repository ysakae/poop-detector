"""確認待ちから外した動画の台帳（review_dismissals）。

画角を変える前の動画など、ラベルを付けずに確認待ちから外したい動画を記録する。
外した動画は、自動判定が排泄の候補としていても、録画の整理（clip_storage）の対象になる。
外すのはラベルのない変換済みの動画だけで、ラベルや確定記録は書き換えない。
"""

from collections.abc import Iterable
from typing import TYPE_CHECKING

from ..records.events import utcnow

if TYPE_CHECKING:
    from .clips import ClipStore


class ReviewDismissals:
    """確認待ちから外した動画の記録と、確認待ちへの戻し。"""

    def __init__(self, store: "ClipStore") -> None:
        self.store = store

    def dismiss(self, clip_ids: Iterable[str]) -> int:
        """ラベルのない変換済みの動画を確認待ちから外し、新しく外した数を返す。"""
        now = utcnow()
        added = 0
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for clip_id in dict.fromkeys(clip_ids):
                cursor = db.execute(
                    """INSERT OR IGNORE INTO review_dismissals (clip_id, dismissed_at)
                       SELECT id, ? FROM clips WHERE id=? AND status='ready' AND label IS NULL""",
                    (now, clip_id),
                )
                added += cursor.rowcount
        return added

    def restore(self, clip_ids: Iterable[str]) -> int:
        """確認待ちに戻し、戻した数を返す。"""
        removed = 0
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for clip_id in dict.fromkeys(clip_ids):
                cursor = db.execute("DELETE FROM review_dismissals WHERE clip_id=?", (clip_id,))
                removed += cursor.rowcount
        return removed
