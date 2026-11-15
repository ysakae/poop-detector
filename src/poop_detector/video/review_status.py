"""監視中の動画の自動判定の状況。管理 › 判定器とモデルに出す値を、台帳と設定から求める。

監視と GUI は別のプロセスでも動くので、監視のメモリーにある状態（待ち時間など）は使わず、
判定器を呼んだ記録、判定の台帳、停止の履歴だけから求める。
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from ..config import Config
from ..features.outages import OutageLog
from .clip_reviews import PRIORITY_NAMES
from .clips import ClipStore
from .providers import PROVIDER_NAMES
from .review import allowed_priority


@dataclass(frozen=True)
class ReviewStatus:
    """回数の上限と後回し、判定を待つ動画、判定器の失敗。"""

    # 直近1時間に判定器を呼んだ回数と、1時間あたりの上限（0 は上限なし）。
    used: int
    max_per_hour: int
    # 上限に達して、どの動画も判定を待っているか。
    capped: bool
    # 後回しにしているきっかけの名前。
    postponed: tuple[str, ...]
    # 24時間以内の未判定の動画の本数（きっかけの名前ごと）と、判定に失敗したままの本数。
    waiting: tuple[tuple[str, int], ...]
    failed: int
    # 監視の判定で、判定器の呼び出しが直近で続けて失敗している回数。
    judge_failures: int
    # 判定器の停止として知らせた停止が続いていれば、その始まり（UTC の ISO 8601）。
    outage_since: str | None
    # 予備の判定器の名前。
    fallback: tuple[str, ...]


def review_status(
    clips: ClipStore, outages: OutageLog, config: Config, now: datetime
) -> ReviewStatus:
    used = clips.judge_runs.count_since(now - timedelta(hours=1))
    cap = config.review_max_per_hour
    priority = allowed_priority(used, cap) if cap else len(PRIORITY_NAMES) - 1
    counts, failed = clips.assessments.waiting_counts((now - timedelta(days=1)).isoformat())
    outage = outages.current("judge")
    return ReviewStatus(
        used=used,
        max_per_hour=cap,
        capped=priority is None,
        postponed=() if priority is None else PRIORITY_NAMES[priority + 1 :],
        waiting=tuple(zip(PRIORITY_NAMES, counts, strict=True)),
        failed=failed,
        judge_failures=clips.judge_runs.trailing_failures(),
        outage_since=outage.started_at if outage else None,
        fallback=tuple(PROVIDER_NAMES.get(name, name) for name in config.fallback_providers),
    )
