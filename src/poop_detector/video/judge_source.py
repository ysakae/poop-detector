"""動画の自動判定に使う判定器の出どころ。設定ファイルの選び直しを、監視を止めずに反映する。

`ReloadingJudge` は設定ファイルの更新時刻を見て、`review.provider` と `review.model` が
変わっていれば新しい判定器を作る。作れないとき（未ログインなど）は前の判定器のまま続ける。
ほかの設定項目は対象にせず、これまでどおり監視を起動し直すと反映する。
"""

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..config import Config, ConfigError

if TYPE_CHECKING:
    from .review import VideoJudge

logger = logging.getLogger(__name__)

# 判定器の名前とモデルから判定器を作る関数（既定は providers.build_judge）。
JudgeBuilder = Callable[[str, str], "VideoJudge"]


@dataclass(frozen=True)
class JudgeChoice:
    """判定に使う判定器と、判定の台帳に記録する判定器の名前（provider）とモデル。"""

    judge: "VideoJudge"
    provider: str
    model: str | None


class JudgeSource(Protocol):
    """動画ごとに、いま使う判定器を返すもの。"""

    def current(self) -> JudgeChoice: ...


class FixedJudge:
    """起動時に選んだ判定器を使い続ける（video-review サブコマンドなど）。"""

    def __init__(self, choice: JudgeChoice) -> None:
        self.choice = choice

    def current(self) -> JudgeChoice:
        return self.choice


def _default_builder(provider: str, model: str) -> "VideoJudge":
    from .providers import build_judge

    return build_judge(provider, model)


class ReloadingJudge:
    """設定ファイルで選び直した判定器とモデルを、次の動画の判定から使う。"""

    def __init__(
        self,
        path: str | Path,
        initial: JudgeChoice,
        *,
        build: JudgeBuilder = _default_builder,
    ) -> None:
        self.path = Path(path)
        self.choice = initial
        self.build = build
        self.stamp = self._stamp()
        self.lock = threading.Lock()

    def _stamp(self) -> tuple[int, int] | None:
        try:
            status = self.path.stat()
        except OSError:
            return None
        return status.st_mtime_ns, status.st_size

    def current(self) -> JudgeChoice:
        """設定ファイルが変わっていれば読み直し、いま使う判定器を返す。"""
        with self.lock:
            stamp = self._stamp()
            # ファイルが読めないときは、消えたのか書き換え中なのか分からないので変えない。
            if stamp is None or stamp == self.stamp:
                return self.choice
            # 失敗しても、次に設定ファイルが変わるまで同じ内容を試し直さない。
            self.stamp = stamp
            self.choice = self._reload()
            return self.choice

    def _reload(self) -> JudgeChoice:
        try:
            config = Config.load(self.path)
        except (ConfigError, OSError):
            logger.warning("設定ファイルを読めないため、前の判定器で判定を続けます")
            return self.choice
        provider, model = config.review_provider, config.review_model
        if (provider, model or None) == (self.choice.provider, self.choice.model):
            return self.choice
        try:
            judge = self.build(provider, model)
            judge.check()
        except ValueError as error:
            # インストールとログインの案内だけで、外部の出力は含まない。
            logger.warning("新しい判定器を使えないため、前の判定器で判定を続けます: %s", error)
            return self.choice
        logger.info("判定器を %s（モデル: %s）に切り替えました", provider, model or "既定")
        return JudgeChoice(judge, provider, model or None)
