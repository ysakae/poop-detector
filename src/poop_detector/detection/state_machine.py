"""継続確認と、一度空になるまでの重複通知抑制。"""

from typing import Literal, TypeAlias

from .difference import ObservationKind

MachineState: TypeAlias = Literal["EMPTY", "OCCUPIED", "VERIFYING", "SUSPECTED"]


class Machine:
    """候補が続いた時間で通知を判断し、同じ跡での重複通知を抑える状態遷移。"""

    def __init__(self, verify_seconds: float = 3.0, clear_seconds: float = 3.0) -> None:
        self.verify_seconds = verify_seconds
        self.clear_seconds = clear_seconds
        self.state: MachineState = "EMPTY"
        self.started: float | None = None
        self.clear_started: float | None = None
        self.latched = False

    def interrupt(self) -> None:
        self.started = None
        self.clear_started = None
        if not self.latched:
            self.state = "OCCUPIED"

    def update(self, kind: ObservationKind, now: float) -> bool:
        """判定を1つ進め、通知すべきときだけ True を返す。"""
        if kind in ("occupied", "unstable"):
            self.interrupt()
            return False
        if kind == "empty":
            self.started = None
            if self.clear_started is None:
                self.clear_started = now
            if now - self.clear_started >= self.clear_seconds:
                self.state = "EMPTY"
                self.latched = False
            return False
        self.clear_started = None
        if self.latched:
            return False
        if self.started is None:
            self.started = now
            self.state = "VERIFYING"
        if now - self.started >= self.verify_seconds:
            self.state = "SUSPECTED"
            self.latched = True
            return True
        return False
