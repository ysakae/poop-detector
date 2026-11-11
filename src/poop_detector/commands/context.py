"""複数のサブコマンドで使う準備処理と、コマンドの失敗の伝え方。"""

import argparse
import logging
import platform
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ..capture.camera import Camera
from ..config import Config
from ..detection.roi import ROI, AnyROI
from ..detection.scene import load_baseline
from ..features.tray_tracking import TrayTracker
from ..frames import Frame
from ..notifications import DEFAULT_NTFY_SERVER, MacNotifier, NtfyNotifier

logger = logging.getLogger(__name__)


class CommandError(Exception):
    """利用者に理由を伝えて終了コード 2 で終える失敗。

    メッセージはそのまま表示するので、外部の例外の文字列（RTSP の URL を含みうる）を入れない。
    """


@dataclass(frozen=True)
class CommandContext:
    """設定ファイルから読んだ設定と、データの保存先の場所。"""

    config: Config

    @property
    def root(self) -> Path:
        return Path(self.config.data_dir)

    @property
    def roi_path(self) -> Path:
        return self.root / "roi.json"

    @property
    def outside_roi_path(self) -> Path:
        return self.root / "outside-roi.json"

    @property
    def baseline_path(self) -> Path:
        return self.root / "baseline" / "empty.png"

    @property
    def tray_reference_path(self) -> Path:
        return self.root / "baseline" / "tray-reference.png"

    @property
    def dog_model_dir(self) -> Path:
        return self.root / "models" / "dog-yolox"


# サブコマンドの処理。終了コード（成功 0、入力や設定の誤り 2）を返す。
Handler = Callable[[argparse.Namespace, CommandContext], int]


def load_scene(context: CommandContext) -> tuple[AnyROI, Frame]:
    """登録済みの ROI と、空のトイレの基準画像を読む。"""
    roi = ROI.load(context.roi_path)
    return roi, load_baseline(context.baseline_path, roi)


def load_tray_tracker(
    context: CommandContext, baseline: Frame, roi: AnyROI, enabled: bool
) -> TrayTracker | None:
    """--track-tray のときだけ、登録済みのトレーの画像から追従を準備する。"""
    if not enabled:
        return None
    return TrayTracker.from_baseline(context.tray_reference_path, baseline, roi)


def load_outside_roi(context: CommandContext) -> AnyROI:
    """トレー外の検知に使う、トレーと周囲の床の範囲を読む。"""
    if not context.outside_roi_path.is_file():
        raise CommandError("先に setup-roi --outside でトレーと周囲の床を囲んでください")
    return ROI.load(context.outside_roi_path)


def check_limit(limit: int) -> None:
    if not 1 <= limit <= 50:
        raise CommandError("--limitは1〜50件で指定してください")


def camera_source(settings: Mapping[str, str]) -> str:
    source = settings["POOP_RTSP_URL"]
    if not source:
        raise CommandError(".envのPOOP_RTSP_URLにEufyアプリのRTSP URLを設定してください")
    return source


def open_camera(source: str, config: Config) -> Camera:
    """カメラを開く。テストではこのモジュールの Camera を偽のカメラに差し替える。"""
    return Camera(source, config.timeout_ms, config.retry_seconds)


def build_notifier(
    config: Config, settings: Mapping[str, str], *, sends: bool
) -> tuple[NtfyNotifier | MacNotifier, bool]:
    """通知先を選び、(通知, ntfy を使うか) を返す。

    sends は、そのコマンドが実際に通知を送るか（run と test-notification）。
    送らないコマンドでは .env のトピックを読まない。
    """
    topic = early_topic = ""
    if sends:
        topic = settings["POOP_NTFY_TOPIC"].strip()
        early_topic = settings["POOP_NTFY_EARLY_TOPIC"].strip()
    if early_topic and not topic:
        raise CommandError("見守り用トピックを使う場合は POOP_NTFY_TOPIC も設定してください")
    if topic:
        try:
            notifier = NtfyNotifier(
                topic,
                enabled=config.notifications,
                early_topic=early_topic,
                pet_name=config.pet_name,
                server=settings["POOP_NTFY_SERVER"].strip() or DEFAULT_NTFY_SERVER,
            )
        except ValueError as error:
            # トピック名やサーバーの検証の文言で、接続先の URL そのものは含まない。
            raise CommandError(str(error)) from None
        return notifier, True
    on_mac = platform.system() == "Darwin"
    if config.notifications and not on_mac and sends:
        logger.warning("macOS 以外では、.env に POOP_NTFY_TOPIC を設定しないと通知は届きません")
    return MacNotifier(config.notifications and on_mac, pet_name=config.pet_name), False
