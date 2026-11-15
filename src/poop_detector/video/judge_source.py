"""動画の自動判定に使う判定器の出どころ。設定ファイルの選び直しを、監視を止めずに反映する。

`ReloadingJudge` は設定ファイルの更新時刻を見て、`review.provider` と `review.model` が
変わっていれば新しい判定器を作る。作れないとき（未ログインなど）は前の判定器のまま続ける。
ほかの設定項目は対象にせず、これまでどおり監視を起動し直すと反映する。
"""

import logging
import threading
import time
from collections.abc import Callable, Sequence
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

    def note(self, judge_failures: int) -> None:
        """判定器の呼び出しが続けて失敗している回数を受け取る（成功すると 0）。"""
        ...


class FixedJudge:
    """起動時に選んだ判定器を使い続ける（video-review サブコマンドなど）。"""

    def __init__(self, choice: JudgeChoice) -> None:
        self.choice = choice

    def current(self) -> JudgeChoice:
        return self.choice

    def note(self, judge_failures: int) -> None:
        del judge_failures


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

    def note(self, judge_failures: int) -> None:
        del judge_failures


class FallbackJudges:
    """判定器の呼び出しが失敗し続けたら、予備の判定器（review.fallback）に切り替える。

    利用上限に達した判定器を待つ間も、判定を続けるためのもの。予備は CLI の既定のモデルで使い、
    使える状態（インストールとログイン）のものだけを選ぶ。RETURN_SECONDS が過ぎたら主の判定器に
    戻して試し、まだ失敗するならもう一度切り替える。
    """

    # この回数だけ続けて失敗したら、次の判定器に切り替える。
    SWITCH_AFTER = 3
    # 予備に切り替えてから、主の判定器に戻して試すまでの秒数。
    RETURN_SECONDS = 3600.0

    def __init__(
        self,
        primary: JudgeSource,
        providers: Sequence[str],
        *,
        build: JudgeBuilder = _default_builder,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.primary, self.providers = primary, tuple(providers)
        self.build, self.clock = build, clock
        # いま使っている判定器の位置（0 は主、1 以降は providers の順）と、予備の判定器。
        self.position = 0
        self.spare: JudgeChoice | None = None
        self.return_at = float("inf")
        self.lock = threading.Lock()

    def current(self) -> JudgeChoice:
        with self.lock:
            if self.spare is not None and self.clock() >= self.return_at:
                logger.info("主の判定器に戻して試します")
                self.position, self.spare = 0, None
            return self.spare or self.primary.current()

    def note(self, judge_failures: int) -> None:
        self.primary.note(judge_failures)
        if judge_failures < self.SWITCH_AFTER:
            return
        with self.lock:
            count = len(self.providers) + 1
            for step in range(1, count):
                position = (self.position + step) % count
                if position == 0:
                    logger.warning("予備の判定器も失敗したため、主の判定器に戻します")
                    self.position, self.spare = 0, None
                    return
                provider = self.providers[position - 1]
                try:
                    judge = self.build(provider, "")
                    judge.check()
                except ValueError:
                    continue
                logger.warning(
                    "判定器の呼び出しが続けて失敗したため、予備の判定器（%s）に切り替えます",
                    provider,
                )
                self.position, self.spare = position, JudgeChoice(judge, provider, None)
                self.return_at = self.clock() + self.RETURN_SECONDS
                return
