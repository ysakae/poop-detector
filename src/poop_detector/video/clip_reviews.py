"""保存した動画の後処理の結果（自動判定と、旋回候補の分析）の台帳。"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from ..records.database import Database
from ..records.events import utcnow, validate_location
from .clips import AssessmentRow, ClipRow

# 監視中に判定する順番（きっかけの優先度。0 が最も高い）。排泄の検知と滞在の通知を先に、
# トレー外の滞在を最後にする。判定器が止まって動画がたまったあとも、通知につながりやすい
# 動画から判定する。
PRIORITY_ORDER = (
    "CASE c.source WHEN 'outside_stay' THEN 2"
    " WHEN 'dog_visit' THEN 1 WHEN 'dog_enter' THEN 1 WHEN 'dog_exit' THEN 1 ELSE 0 END"
)
# 画面に出す、優先度（0・1・2）ごとのきっかけの名前。
PRIORITY_NAMES = ("検知・滞在の通知", "犬の出入り", "トレー外の滞在")


@dataclass(frozen=True)
class Assessment:
    """判定器が動画を見て返した判定（排泄の種類、行動、時刻、根拠、場所）。"""

    label: str | None = None
    activity: str | None = None
    start_seconds: float | None = None
    end_seconds: float | None = None
    reason: str | None = None
    location: str | None = None
    # 判定器の答えではなく、判定に使った画像の前後の差分の指標（#152）。判定の比較には含めない。
    endpoint_change: float | None = field(default=None, compare=False)
    stool_seen_seconds: float | None = None
    stool_clear_seconds: float | None = None
    # 動画中に新たに出た便の状態（review.STOOL_CONDITIONS。排便のときだけ）。
    stool_condition: str | None = None


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

    def count_condition(self, condition: str, since: datetime) -> int:
        """since 以降に判定した動画のうち、便の状態が condition のものの数。"""
        with self.connect() as db:
            count: int = db.execute(
                "SELECT COUNT(*) FROM video_assessments WHERE stool_condition=? "
                "AND julianday(assessed_at) >= julianday(?)",
                (condition, since.astimezone(UTC).isoformat()),
            ).fetchone()[0]
        return count

    def latest(self) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                """SELECT a.*, c.requested_at, c.label AS human_label, c.location AS human_location
                FROM video_assessments a JOIN clips c ON c.id=a.clip_id
                ORDER BY a.assessed_at DESC, c.requested_at DESC LIMIT 1"""
            ).fetchone()
        return dict(row) if row else None

    def pending(
        self,
        limit: int,
        since: str | None = None,
        clip_id: str | None = None,
        scene_signature: str | None = None,
        *,
        retry: Sequence[str] = (),
        max_priority: int | None = None,
        short_screened_since: str | None = None,
    ) -> list[ClipRow]:
        """自動判定を待つ動画。

        since を指定すると、それ以降の未判定の動画と retry に挙げた失敗済みの動画を、
        きっかけの優先度の高い順、同じ優先度では古い順に返す。max_priority を指定すると、
        それより優先度の低い（数の大きい）きっかけの動画を後回しにする。
        short_screened_since より後に計測した条件付きの短い出入りは、出入りの中で後ろに並べる。
        """
        conditions = ["c.status='ready'", "c.label IS NULL", "c.source IS NOT 'toilet_reentry'"]
        params: list[object] = []
        if since is not None:
            retried = ",".join("?" * len(retry))
            conditions.extend(
                [
                    "c.requested_at>=?",
                    f"(a.clip_id IS NULL OR (a.error IS NOT NULL AND c.id IN ({retried})))",
                ]
            )
            params.extend([since, *retry])
        else:
            conditions.append("(a.clip_id IS NULL OR a.error IS NOT NULL)")
        if scene_signature is not None:
            conditions.append("c.scene_signature=?")
            params.append(scene_signature)
        if clip_id is not None:
            conditions.append("c.id=?")
            params.append(clip_id)
        if max_priority is not None:
            conditions.append(f"{PRIORITY_ORDER} <= ?")
            params.append(max_priority)
        order = "c.requested_at DESC"
        if since is not None:
            order = f"{PRIORITY_ORDER}, c.requested_at ASC"
        if since is not None and short_screened_since is not None:
            from ..experimental.screening import SCREENING_VERSION

            short = """c.source IN ('dog_enter','dog_exit','dog_visit') AND c.interrupted=0
                AND EXISTS (
                    SELECT 1 FROM clip_screenings s WHERE s.clip_id=c.id
                        AND s.version=? AND s.screened_at>?
                        AND s.weak_tray_seconds>0 AND s.weak_tray_seconds<5-0.000001
                        AND s.weak_tray_frames<=3 AND s.sampled>s.unaligned
                        AND 10*s.unaligned<=s.sampled
                )"""
            order = f"{PRIORITY_ORDER}, ({short}), c.requested_at ASC"
            params.extend([SCREENING_VERSION, short_screened_since])
        # 条件はこの関数内の固定の文字列だけで組み立て、値はすべてプレースホルダーで渡す。
        with self.connect() as db:
            return [
                cast(ClipRow, dict(row))
                for row in db.execute(
                    """SELECT c.* FROM clips c
                    LEFT JOIN video_assessments a ON a.clip_id=c.id WHERE """  # noqa: S608
                    + " AND ".join(conditions)
                    + f" ORDER BY {order} LIMIT ?",
                    (*params, limit),
                )
            ]

    def waiting_counts(self, since: str) -> tuple[tuple[int, ...], int]:
        """since 以降の動画のうち、(優先度ごとの未判定の本数, 判定に失敗したままの本数)。"""
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT {PRIORITY_ORDER} AS priority,
                SUM(a.clip_id IS NULL) AS waiting, SUM(a.error IS NOT NULL) AS failed
                FROM clips c LEFT JOIN video_assessments a ON a.clip_id=c.id
                WHERE c.status='ready' AND c.label IS NULL AND c.requested_at>=?
                GROUP BY priority""",  # noqa: S608
                (since,),
            ).fetchall()
        waiting = {row["priority"]: row["waiting"] for row in rows}
        return (
            tuple(waiting.get(priority, 0) for priority in range(len(PRIORITY_NAMES))),
            sum(row["failed"] for row in rows),
        )

    def tray_stay_counts(self, since: str) -> list[tuple[str, int, int, int]]:
        """犬の出入りの動画本数、判定完了数、排泄候補数を滞在時間帯ごとに返す。"""
        with self.connect() as db:
            rows = db.execute(
                """SELECT CASE
                    WHEN c.tray_stay_seconds IS NULL OR c.interrupted THEN 4
                    WHEN c.tray_stay_seconds < 5 THEN 0
                    WHEN c.tray_stay_seconds < 10 THEN 1
                    WHEN c.tray_stay_seconds < 20 THEN 2
                    ELSE 3
                END AS band, COUNT(*) AS total,
                SUM(a.clip_id IS NOT NULL AND a.error IS NULL) AS completed,
                SUM(CASE WHEN a.error IS NULL AND a.label IN ('feces','urine','both')
                    THEN 1 ELSE 0 END) AS candidates
                FROM clips c LEFT JOIN video_assessments a ON a.clip_id=c.id
                WHERE c.source IN ('dog_enter','dog_exit','dog_visit')
                    AND c.status IN ('ready','removing','removed') AND c.requested_at>=?
                GROUP BY band""",
                (since,),
            ).fetchall()
        counts = {row["band"]: (row["total"], row["completed"], row["candidates"]) for row in rows}
        labels = ("5秒未満", "5秒以上10秒未満", "10秒以上20秒未満", "20秒以上", "計測なし")
        return [(label, *counts.get(band, (0, 0, 0))) for band, label in enumerate(labels)]

    def save(
        self,
        clip_id: str,
        assessment: "Assessment | None" = None,
        *,
        observed_at: str | None = None,
        stool_seen_at: str | None = None,
        stool_clear_at: str | None = None,
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
                (clip_id,label,activity,start_seconds,end_seconds,reason,observed_at,error,assessed_at,location,provider,model,endpoint_change,stool_seen_at,stool_clear_at,stool_condition)
                SELECT id,?,?,?,?,?,?,?,?,?,?,?,?,?,?,? FROM clips WHERE id=? AND label IS NULL AND status='ready'
                ON CONFLICT(clip_id) DO UPDATE SET label=excluded.label,activity=excluded.activity,
                start_seconds=excluded.start_seconds,end_seconds=excluded.end_seconds,
                reason=excluded.reason,observed_at=excluded.observed_at,error=excluded.error,
                assessed_at=excluded.assessed_at,location=excluded.location,
                provider=excluded.provider,model=excluded.model,
                endpoint_change=excluded.endpoint_change,stool_seen_at=excluded.stool_seen_at,
                stool_clear_at=excluded.stool_clear_at,stool_condition=excluded.stool_condition
                WHERE video_assessments.error IS NOT NULL""",
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
                        values.endpoint_change,
                        stool_seen_at,
                        stool_clear_at,
                        values.stool_condition,
                        clip_id,
                    ),
                ).rowcount
                == 1
            )

    def claim_notice(self, clip_id: str, label: str | None = None) -> bool:
        """通知の権利を取る。同じ記録や近い時刻の同じ判定を通知済みなら False。"""
        with self.connect() as db:
            return (
                db.execute(
                    """UPDATE video_assessments AS candidate
                SET notice_claimed=1,notice_label=COALESCE(:label,candidate.label)
                WHERE candidate.clip_id=:clip_id AND candidate.notice_claimed=0
                AND EXISTS (
                    SELECT 1 FROM clips c WHERE c.id=candidate.clip_id AND c.label IS NULL
                    AND NOT EXISTS (
                        SELECT 1 FROM video_assessments a JOIN clips previous ON previous.id=a.clip_id
                        WHERE a.notice_claimed=1
                        AND COALESCE(a.location,'tray')=COALESCE(candidate.location,'tray') AND (
                            (candidate.location IS NOT 'outside' AND c.event_id IS NOT NULL
                                AND c.event_id=previous.event_id)
                            OR (c.scene_signature IS previous.scene_signature
                                AND (COALESCE(a.notice_label,a.label)=COALESCE(:label,candidate.label)
                                    OR 'both' IN (COALESCE(a.notice_label,a.label),
                                        COALESCE(:label,candidate.label)))
                                AND ABS(julianday(a.observed_at)-julianday(candidate.observed_at))
                                    < 5.001/86400)
                        )
                    )
                )""",
                    {"clip_id": clip_id, "label": label},
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

    def claim_stool_notice(self, clip_id: str) -> bool:
        """同じトレーで便がなくなるまで、映像に残った便の通知を一度に限る。"""
        with self.connect() as db:
            claimed = (
                db.execute(
                    """UPDATE video_assessments AS candidate SET stool_notice_claimed=1
                    WHERE candidate.clip_id=? AND candidate.stool_notice_claimed<=0
                    AND candidate.stool_seen_at IS NOT NULL
                    AND EXISTS (
                        SELECT 1 FROM clips c WHERE c.id=candidate.clip_id AND c.label IS NULL
                        AND NOT EXISTS (
                            SELECT 1 FROM (
                                SELECT previous.scene_signature,
                                    COALESCE(a.stool_seen_at,a.observed_at) AS seen_at,
                                    a.stool_seen_at IS NULL AS legacy
                                FROM video_assessments a JOIN clips previous ON previous.id=a.clip_id
                                WHERE a.stool_notice_claimed=1 OR (
                                    a.notice_claimed=1
                                    AND COALESCE(a.notice_label,a.label) IN ('feces','both')
                                    AND a.location IS NOT 'outside'
                                )
                            ) AS sent
                            WHERE sent.scene_signature IS c.scene_signature
                            AND sent.seen_at IS NOT NULL
                            AND (NOT sent.legacy OR ABS(julianday(sent.seen_at)
                                - julianday(candidate.stool_seen_at)) < 10.0/1440)
                            AND NOT EXISTS (
                                SELECT 1 FROM video_assessments a JOIN clips other ON other.id=a.clip_id
                                WHERE other.scene_signature IS c.scene_signature
                                AND julianday(a.stool_clear_at) >
                                    MIN(julianday(sent.seen_at), julianday(candidate.stool_seen_at))
                                AND julianday(a.stool_clear_at) <
                                    MAX(julianday(sent.seen_at), julianday(candidate.stool_seen_at))
                            )
                        )
                    )""",
                    (clip_id,),
                ).rowcount
                == 1
            )
        if not claimed:
            self.defer_stool_notice(clip_id)
        return claimed

    def defer_stool_notice(self, clip_id: str) -> None:
        """片付けの映像が後から判定されたときに、重複扱いした候補を照合し直せるようにする。"""
        with self.connect() as db:
            db.execute(
                "UPDATE video_assessments SET stool_notice_claimed=-1 "
                "WHERE clip_id=? AND stool_notice_claimed=0",
                (clip_id,),
            )

    def deferred_stool_notices(self, clip_id: str, since: str) -> list[tuple[str, str, bool, bool]]:
        with self.connect() as db:
            return [
                (
                    row["id"],
                    row["stool_seen_at"],
                    bool(row["interrupted"]),
                    not row["new_stool"],
                )
                for row in db.execute(
                    """SELECT c.id,a.stool_seen_at,c.interrupted,
                        a.label IN ('feces','both') AND a.location IS NOT 'outside' AS new_stool
                    FROM video_assessments a
                    JOIN clips c ON c.id=a.clip_id
                    JOIN clips cleared ON cleared.scene_signature IS c.scene_signature
                    WHERE cleared.id=? AND c.requested_at>=? AND c.status='ready'
                        AND c.label IS NULL AND a.stool_notice_claimed=-1
                    ORDER BY julianday(a.stool_seen_at)""",
                    (clip_id, since),
                )
            ]

    def finish_stool_notice(self, clip_id: str, sent: bool) -> None:
        stamp = utcnow() if sent else None
        with self.connect() as db:
            db.execute(
                "UPDATE video_assessments SET stool_notice_claimed=?,stool_notified_at=? "
                "WHERE clip_id=?",
                (int(sent), stamp, clip_id),
            )

    def has_nearby_stool_notice(self, clip_id: str, observed_at: str) -> bool:
        """前後の動画が先に判定されたとき、同じ便を続けて知らせるのを防ぐ。"""
        with self.connect() as db:
            return (
                db.execute(
                    """SELECT 1 FROM clips c JOIN clips later ON later.scene_signature IS c.scene_signature
                    JOIN video_assessments a ON a.clip_id=later.id AND a.stool_notice_claimed=1
                    WHERE c.id=? AND julianday(a.stool_seen_at) BETWEEN
                        julianday(?, '-5 seconds') AND julianday(?, '+10 minutes')
                    AND NOT EXISTS (
                        SELECT 1 FROM video_assessments clean JOIN clips other ON other.id=clean.clip_id
                        WHERE other.scene_signature IS c.scene_signature
                        AND julianday(clean.stool_clear_at)
                            > MIN(julianday(?),julianday(a.stool_seen_at))
                        AND julianday(clean.stool_clear_at)
                            < MAX(julianday(?),julianday(a.stool_seen_at))
                    ) LIMIT 1""",
                    (clip_id, observed_at, observed_at, observed_at, observed_at),
                ).fetchone()
                is not None
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

    def labeled(self, scene_signature: str, labels: Sequence[str], limit: int) -> list[ClipRow]:
        """人がラベルを付けた、いまの画角の動画。新しい順に limit 本まで。"""
        with self.connect() as db:
            rows = db.execute(
                """SELECT * FROM clips WHERE status='ready' AND interrupted=0
                AND scene_signature=? AND label IN (SELECT value FROM json_each(?))
                ORDER BY requested_at DESC LIMIT ?""",
                (scene_signature, json.dumps(list(labels)), limit),
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
