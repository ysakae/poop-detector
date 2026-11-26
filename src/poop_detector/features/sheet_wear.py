"""通知済み状態の汚れの積み重なりから、シートの交換どきを知らせる監視の拡張。"""

import logging
from typing import Literal, Protocol

from ..config import Config
from ..detection.difference import IncrementalDetector
from ..detection.monitoring import FrameView, MonitorExtension
from ..records.sheet_wear import SheetWearLog

logger = logging.getLogger(__name__)


class SheetNotifier(Protocol):
    """交換どきを知らせる通知先。"""

    def send_sheet_due(self, events: int, ratio: float) -> bool: ...


class SheetWearExtension(MonitorExtension):
    """通知済み状態を更新するたびに汚れの割合を記録し、上限に達したら一度だけ知らせる。"""

    def __init__(
        self, log: SheetWearLog, signature: str, config: Config, notifier: SheetNotifier
    ) -> None:
        self.log, self.signature, self.config, self.notifier = log, signature, config, notifier

    def on_reference(
        self,
        view: FrameView,
        detector: IncrementalDetector,
        reason: Literal["event", "settled"],
    ) -> None:
        ratio = detector.wear_ratio(view.valid_mask)
        self.log.record(self.signature, ratio, reason)
        status = self.log.status()
        if status.notified or not status.due(
            self.config.sheet_max_events, self.config.sheet_max_ratio
        ):
            return
        # 送れなかったときは残さず、次の更新でまた試す。
        if self.notifier.send_sheet_due(status.events, status.ratio):
            self.log.mark_notified()
            logger.info("シートの交換どきを知らせました")
