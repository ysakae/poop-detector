"""判定器が使える状態かの確認を、しばらく使い回す（CLI のログイン状態の確認は数秒かかる）。"""

import threading
import time
from collections.abc import Callable

# 確認の結果を使い回す秒数。CLI にログインし直した直後は、この秒数だけ古い結果が出ることがある。
REUSE_SECONDS = 60.0


def check_judge(provider: str, model: str) -> str | None:
    """判定器が使えない理由。使えれば None。文言はこのパッケージの案内だけで、値を含まない。"""
    from .providers import build_judge

    try:
        build_judge(provider, model).check()
    except ValueError as error:
        return str(error)
    return None


class JudgeReadiness:
    """判定器ごとの確認の結果を、REUSE_SECONDS の間だけ覚えておく。"""

    def __init__(
        self,
        seconds: float = REUSE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        check: Callable[[str, str], str | None] = check_judge,
    ) -> None:
        self.seconds, self.clock, self.check = seconds, clock, check
        self.lock = threading.Lock()
        self.results: dict[tuple[str, str], tuple[float, str | None]] = {}

    def problem(self, provider: str, model: str) -> str | None:
        key = (provider, model)
        with self.lock:
            found = self.results.get(key)
        if found is not None and self.clock() - found[0] < self.seconds:
            return found[1]
        # 確認の間は鍵を持たない（数秒かかる確認で、ほかの画面を待たせない）。
        problem = self.check(provider, model)
        with self.lock:
            self.results = {**self.results, key: (self.clock(), problem)}
        return problem
