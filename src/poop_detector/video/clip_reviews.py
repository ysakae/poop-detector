"""保存した動画の後処理の結果（自動判定と、旋回候補の分析）の台帳。"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, cast

from ..records.database import Database
from ..records.events import utcnow, validate_location
from .clips import AssessmentRow, ClipRow


@dataclass(frozen=True)
class Assessment:
    """判定器が動画を見て返した判定（排泄の種類、行動、時刻、根拠、場所）。"""

    label: str | None = None
    activity: str | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    reason: str | None = None
    location: str | None = None


@dataclass(frozen=True)
class JudgeUsage:
    """判定器・モデルごとの判定の件数と、直近の判定の時刻と成否。"""

    provider: str
    model: str | None
    total: int
    errors: int
    last_at: str
    last_failed: bool


class AssessmentStore:
    """video_assessments テーブル。判定の保存と、同じ排泄を二重に通知しないための権利の管理。"""

    def __init__(self, database: Database) -> None:
        self.connect = database.connect

    def get(self, clip_id: str) -> AssessmentRow | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM video_assessments WHERE clip_id=?", (clip_id,)
            ).fetchone()
        return cast(AssessmentRow, dict(row)) if row else None

    def latest(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT a.*, c.requested_at, c.label AS human_label, c.location AS human_location
                FROM video_assessments a JOIN clips c ON c.id=a.clip_id
                ORDER BY a.assessed_at DESC, c.requested_at DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def by_judge(self) -> list[JudgeUsage]:
        """判定器・モデルごとの件数。直近に使ったものから並べる。"""
        with self.connect() as db:
            rows = db.execute(
                """SELECT provider, model, COUNT(*) AS total,
                SUM(error IS NOT NULL) AS errors, MAX(assessed_at) AS last_at,
                (SELECT b.error IS NOT NULL FROM video_assessments b
                 WHERE b.provider IS a.provider AND b.model IS a.model
                 ORDER BY b.assessed_at DESC, b.rowid DESC LIMIT 1) AS last_failed
                FROM video_assessments a GROUP BY provider, model ORDER BY last_at DESC"""
            ).fetchall()
        return [
            JudgeUsage(
                row["provider"],
                row["model"],
                row["total"],
                row["errors"],
                row["last_at"],
                bool(row["last_failed"]),
            )
            for row in rows
        ]

    def pending(
        self,
        limit: int,
        since: str | None = None,
        clip_id: str | None = None,
        scene_signature: str | None = None,
    ) -> list[ClipRow]:
        """自動判定を待つ動画。since を指定すると、それ以降の未判定の動画だけを古い順に返す。"""
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
                    LEFT JOIN video_assessments a ON a.clip_id=c.id WHERE """  # noqa: S608
                    + " AND ".join(conditions)
                    + f" ORDER BY c.requested_at {order} LIMIT ?",
                    (*params, limit),
                )
            ]

    def save(
        self,
        clip_id: str,
        assessment: "Assessment | None" = None,
        *,
        observed_at: str | None = None,
        error: str | None = None,
        provider: str = "codex",
        model: str | None = None,
    ) -> bool:
        """判定（失敗なら error）を保存する。既存の判定は失敗していたときだけ上書きし、保存できたら True。"""
        values = assessment or Assessment()
        label, activity = values.label, values.activity
        start_seconds, end_seconds = values.start_seconds, values.end_seconds
        reason, location = values.reason, values.location
        validate_location(location)
        with self.connect() as db:
            return (
                db.execute(
                    """INSERT INTO video_assessments
                (clip_id,label,activity,start_seconds,end_seconds,reason,observed_at,error,assessed_at,location,provider,model)
                SELECT id,?,?,?,?,?,?,?,?,?,?,? FROM clips WHERE id=? AND label IS NULL AND status='ready'
                ON CONFLICT(clip_id) DO UPDATE SET label=excluded.label,activity=excluded.activity,
                start_seconds=excluded.start_seconds,end_seconds=excluded.end_seconds,
                reason=excluded.reason,observed_at=excluded.observed_at,error=excluded.error,
                assessed_at=excluded.assessed_at,location=excluded.location,
                provider=excluded.provider,model=excluded.model WHERE video_assessments.error IS NOT NULL""",
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
                        provider,
                        model,
                        clip_id,
                    ),
                ).rowcount
                == 1
            )

    def claim_notice(self, clip_id: str) -> bool:
        """通知の権利を取る。同じ記録や近い時刻の同じ判定を通知済みなら False。"""
        with self.connect() as db:
            return (
                db.execute(
                    """UPDATE video_assessments AS candidate SET notice_claimed=1
                WHERE candidate.clip_id=? AND candidate.notice_claimed=0
                AND EXISTS (
                    SELECT 1 FROM clips c WHERE c.id=candidate.clip_id AND c.label IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM video_assessments a JOIN clips previous ON previous.id=a.clip_id
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

    def finish_notice(self, clip_id: str, sent: bool) -> None:
        stamp = utcnow() if sent else None
        with self.connect() as db:
            db.execute(
                "UPDATE video_assessments SET notice_claimed=?,notified_at=? WHERE clip_id=?",
                (int(sent), stamp, clip_id),
            )
            if sent:
                db.execute(
                    """UPDATE events SET notified_at=? WHERE id=(
                        SELECT c.event_id FROM clips c JOIN video_assessments a ON a.clip_id=c.id
                        WHERE c.id=? AND a.location IS NOT 'outside'
                    )""",
                    (stamp, clip_id),
                )


class CirclingReviews:
    """circling_reviews テーブル。保存した動画の旋回候補の分析結果。"""

    def __init__(self, database: Database) -> None:
        self.connect = database.connect

    def targets(
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

    def get(self, clip_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM circling_reviews WHERE clip_id=?", (clip_id,)
            ).fetchone()
        return {**row, "candidates": json.loads(row["candidates"])} if row else None

    def save(
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
