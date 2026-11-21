"""ホームに出す、シートの使用の度合い（前回の交換からの排泄の回数と汚れの割合）。"""

from dataclasses import dataclass

from ..config import Config
from ..records.database import Database
from ..records.sheet_wear import SheetWearLog


@dataclass(frozen=True)
class SheetSummary:
    """前回の交換からの排泄の回数、汚れの割合（百分率の整数）、交換どきか。"""

    events: int
    percent: int
    due: bool


def sheet_summary(database: Database, config: Config) -> SheetSummary | None:
    """監視が汚れを測ったことがなければ None（手動の記録だけの使い方では出さない）。"""
    if not config.sheet_enabled:
        return None
    status = SheetWearLog(database).status()
    if not status.measured:
        return None
    return SheetSummary(
        status.events,
        round(status.ratio * 100),
        status.due(config.sheet_max_events, config.sheet_max_ratio),
    )
