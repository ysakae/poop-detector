"""GUI の稼働中、ごはんの相談の回答とカロリー探しを作る作業スレッド。"""

import logging
import shutil
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import Config
from ..features.advice.jobs import STALE_SECONDS
from ..features.advice.worker import AdviceWorker, JudgeChoice
from ..video.providers import build_judge, supports_lookup

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


def configured_lookup_judge(config_path: Path | None) -> JudgeChoice | None:
    """カロリー探しに使う判定器。主の判定器、review.fallback の順に、ウェブ検索に対応するものを選ぶ。

    予備の判定器には主のモデル名を渡さない（別の CLI のモデル名なので、それぞれの既定を使う）。
    どれも対応しなければ None。
    """
    config = _config(config_path)
    candidates = [(config.review_provider, config.review_model)]
    candidates += [(provider, "") for provider in config.fallback_providers]
    for provider, model in candidates:
        try:
            judge = build_judge(provider, model)
        except ValueError:
            continue
        if supports_lookup(judge):
            return judge, provider, model or None
    return None


def clean_workdir(workdir: Path, now: float) -> None:
    """強制終了などで残った一時フォルダーのうち、STALE_SECONDS より古いものを消す。

    消すのは作業スレッドが tempfile で作ったフォルダーだけ（workdir の直下）。新しいものは、
    別の GUI が使っている途中かもしれないので残す。
    """
    if not workdir.is_dir():
        return
    for child in workdir.iterdir():
        # リンクはたどらない（lstat）。別の GUI が途中で消したものは飛ばして、残りを続ける。
        try:
            if now - child.lstat().st_mtime <= STALE_SECONDS:
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        except OSError:
            continue


@contextmanager
def advice_worker(app: "GuiApp") -> Iterator[None]:
    """GUI の稼働中、相談の仕事を処理するスレッドを動かす。

    処理中にプロセスが終わった仕事は running のまま残り、expire_stale（STALE_SECONDS）が failed にする。
    """
    workdir = app.root / "advice-work"
    try:
        clean_workdir(workdir, time.time())
    except OSError as error:
        logger.warning("相談の作業フォルダーを掃除できませんでした（%s）", type(error).__name__)
    worker = AdviceWorker(
        app.store.database,
        app.store,
        lambda: configured_judge(app.config_path),
        lambda: _config(app.config_path).pet_name,
        workdir,
        choose_lookup=lambda: configured_lookup_judge(app.config_path),
    )
    stop = threading.Event()

    def run() -> None:
        while not stop.is_set():
            busy = False
            try:
                busy = worker.poll_once()
            except Exception as error:  # スレッドを止めないため、型名だけ記録して続ける
                logger.warning(
                    "ごはんの相談を処理する作業で問題が起きました（%s）", type(error).__name__
                )
            if not busy:
                stop.wait(POLL_SECONDS)

    thread = threading.Thread(target=run, daemon=True, name="advice-worker")
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)
