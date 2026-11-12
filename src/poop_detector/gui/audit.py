"""管理者の操作の監査ログ（誰が・いつ・何をしたか）。

audit_log テーブルに追記するだけで、書き換えや削除はしない。detail は画面に出す説明で、
登録用 URL・確認コード・認証情報は入れない（呼ぶ側が値を渡さない）。
"""

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ..records.database import Database

# 監視するマシンで実行したコマンド（users など）の操作者。
CLI_ACTOR = "cli"
CLI_ACTOR_NAME = "監視するマシンのコマンド"
RECENT_LIMIT = 200

ACTIONS: dict[str, str] = {
    "user.invite": "招待",
    "user.resend": "招待の再送",
    "user.cancel_invite": "招待の取り消し",
    "user.recover": "パスキーの復旧",
    "user.disable": "ユーザーの無効化",
    "user.enable": "ユーザーの有効化",
    "user.role": "役割の変更",
    "tray.begin": "トレーの交換の開始",
    "tray.cancel": "トレーの交換の取り消し",
    "tray.apply": "監視範囲と基準画像の登録",
    "clip.unprotect": "動画の保護の解除",
    "clip.cleanup": "動画の削除・整理",
    "settings.save": "設定の変更",
    "settings.restore": "設定を直前に戻す",
    "judge.select": "判定器とモデルの選択",
    "check.notification": "通知のテスト送信",
    "check.camera": "カメラの確認",
    "area.roi": "監視範囲と基準画像の登録",
    "area.outside": "トレー外の範囲の登録",
    "area.floor": "床面の登録",
}


@dataclass(frozen=True)
class AuditEntry:
    """監査ログの1件。actor は表示名（いないユーザーは ID のまま）。"""

    at: str
    actor: str
    action: str
    detail: str

    @property
    def label(self) -> str:
        return ACTIONS.get(self.action, self.action)


class AuditLog:
    """audit_log テーブル。"""

    def __init__(self, database: Database, clock: Callable[[], float]) -> None:
        self.database = database
        self.clock = clock

    def record(self, actor: str, action: str, detail: str = "") -> None:
        if action not in ACTIONS:
            raise ValueError(f"監査ログの操作の種類が登録されていません: {action}")
        stamp = datetime.fromtimestamp(self.clock(), UTC).isoformat(timespec="microseconds")
        with self.database.connect() as db:
            db.execute(
                "INSERT INTO audit_log (at, actor, action, detail) VALUES (?, ?, ?, ?)",
                (stamp, actor, action, detail),
            )

    def recent(self, limit: int = RECENT_LIMIT) -> list[AuditEntry]:
        """新しい順。"""
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT audit_log.at, audit_log.actor, users.name, audit_log.action, "
                "audit_log.detail FROM audit_log LEFT JOIN users ON users.id = audit_log.actor "
                "ORDER BY audit_log.id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [self._entry(row) for row in rows]

    @staticmethod
    def _entry(row: sqlite3.Row) -> AuditEntry:
        actor = CLI_ACTOR_NAME if row["actor"] == CLI_ACTOR else row["name"] or row["actor"]
        return AuditEntry(row["at"], actor, row["action"], row["detail"])
