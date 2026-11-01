"""継続確認と、一度空になるまでの重複通知抑制。"""


class Machine:
    def __init__(self, verify_seconds=3.0, clear_seconds=3.0):
        self.verify_seconds = verify_seconds
        self.clear_seconds = clear_seconds
        self.state = "EMPTY"
        self.started = None
        self.clear_started = None
        self.latched = False

    def interrupt(self):
        self.started = None
        self.clear_started = None
        if not self.latched:
            self.state = "OCCUPIED"

    def acknowledge(self):
        if self.latched:
            self.state = "ACKNOWLEDGED"

    def update(self, kind, now):
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
