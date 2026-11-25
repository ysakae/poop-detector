"""管理画面から要求された、現在の起動単位の再起動を管理する。"""

import logging
import secrets
import subprocess
import threading
from collections.abc import Callable

from .updates import Upgrade

logger = logging.getLogger(__name__)


class Restart:
    """起動ごとの識別子と、停止の要求を共有する。"""

    def __init__(self, command: str, upgrade: Upgrade | None = None) -> None:
        self.command = command
        self.upgrade = upgrade
        self.generation = secrets.token_urlsafe(18)
        self.requested = threading.Event()
        self.ready = threading.Event()
        self.stop_gui: Callable[[], None] | None = None
        self._lock = threading.Lock()

    def request(self, generation: str, record: Callable[[], None]) -> bool:
        with self._lock:
            if (
                generation != self.generation
                or self.requested.is_set()
                or (self.upgrade is not None and self.upgrade.phase == "preparing")
            ):
                return False
            record()
            self._stop()
            return True

    def update(self, generation: str, target: str, record: Callable[[], None]) -> bool:
        with self._lock:
            upgrade = self.upgrade
            if (
                upgrade is None
                or generation != self.generation
                or self.requested.is_set()
                or upgrade.phase == "preparing"
            ):
                return False
            record()
            upgrade.phase = "preparing"
            upgrade.target = target
            threading.Thread(
                target=self._prepare,
                args=(upgrade, target),
                name="poop-detector-update",
                daemon=True,
            ).start()
            return True

    def _prepare(self, upgrade: Upgrade, target: str) -> None:
        try:
            python = upgrade.prepare(target)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            logger.warning(
                "バージョン %s の更新準備に失敗しました（%s）", target, type(error).__name__
            )
            with self._lock:
                upgrade.phase = "failed"
            return
        with self._lock:
            upgrade.python = python
            upgrade.phase = "restarting"
            self._stop()

    def _stop(self) -> None:
        self.requested.set()
        if self.stop_gui is not None:
            self.stop_gui()
