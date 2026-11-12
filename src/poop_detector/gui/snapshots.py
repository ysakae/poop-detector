"""範囲の登録で、画面に見せた映像をそのまま保存に使うための一時置き場。

画面に返した映像を ID で預かり、保存のときに1回だけ取り出す。メモリーにだけ置き、
古いものと数の多いものは捨てる（映像を長く持ち続けない）。
"""

import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Literal

from ..frames import Frame

SNAPSHOT_SECONDS = 600
SNAPSHOT_LIMIT = 4
Source = Literal["camera", "monitor"]


@dataclass(frozen=True)
class Snapshot:
    """預かった映像。source はカメラから直接取ったか、監視のライブ映像か。"""

    frame: Frame
    source: Source
    taken: float


@dataclass
class Snapshots:
    """ID ごとの映像。複数のスレッド（要求）から使うので、ロックで守る。"""

    _items: dict[str, Snapshot] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def put(self, frame: Frame, source: Source) -> str:
        snapshot_id = secrets.token_hex(16)
        now = time.monotonic()
        with self._lock:
            fresh = {
                key: item
                for key, item in self._items.items()
                if now - item.taken < SNAPSHOT_SECONDS
            }
            newest = sorted(fresh.items(), key=lambda pair: pair[1].taken)[-(SNAPSHOT_LIMIT - 1) :]
            self._items = {**dict(newest), snapshot_id: Snapshot(frame.copy(), source, now)}
        return snapshot_id

    def take(self, snapshot_id: str) -> Snapshot | None:
        """預かった映像を取り出す（1回だけ）。古い・知らない ID なら None。"""
        with self._lock:
            item = self._items.pop(snapshot_id, None)
        if item is None or time.monotonic() - item.taken >= SNAPSHOT_SECONDS:
            return None
        return item
