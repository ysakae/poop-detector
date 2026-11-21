"""GUI の稼働中、ごはんの相談の回答とカロリー探しを作る作業スレッド。"""

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Config
from ..features.advice.worker import AdviceWorker, JudgeChoice
from ..video.providers import build_judge

if TYPE_CHECKING:
    from .server import GuiApp

logger = logging.getLogger(__name__)

POLL_SECONDS = 2


def _config(config_path: Path | None) -> Config:
    return Config.load(config_path) if config_path is not None else Config()


def configured_judge(config_path: Path | None) -> JudgeChoice:
    """設定の review.provider・review.model から判定器を作る。呼ぶたびに設定を読み直す。"""
    config = _config(config_path)
    judge = build_judge(config.review_provider, config.review_model)
    return judge, config.review_provider, config.review_model or None


@contextmanager
def advice_worker(app: "GuiApp") -> Iterator[None]:
    worker = AdviceWorker(
        app.store.database,
        app.store,
        lambda: configured_judge(app.config_path),
        lambda: _config(app.config_path).pet_name,
        app.root / "advice-work",
    )
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            busy = False
            try:
                busy = worker.poll_once()
            except (OSError, sqlite3.Error, ValueError):
                logger.warning("ごはんの相談を処理する作業で問題が起きました")
            if not busy:
                stop.wait(POLL_SECONDS)

    thread = threading.Thread(target=run, daemon=True, name="advice-worker")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)
