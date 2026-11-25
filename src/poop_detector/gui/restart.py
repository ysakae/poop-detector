"""管理画面から要求された、現在の起動単位の再起動を管理する。"""

import secrets
import threading
from collections.abc import Callable


class Restart:
    """起動ごとの識別子と、停止の要求を共有する。"""

    def __init__(self, command: str) -> None:
        self.command = command
        self.generation = secrets.token_urlsafe(18)
        self.requested = threading.Event()
        self.ready = threading.Event()
        self.stop_gui: Callable[[], None] | None = None
        self._lock = threading.Lock()

    def request(self, generation: str, record: Callable[[], None]) -> bool:
        with self._lock:
            if generation != self.generation or self.requested.is_set():
                return False
            record()
            self.requested.set()
            if self.stop_gui is not None:
                self.stop_gui()
            return True
