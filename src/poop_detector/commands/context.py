"""複数のサブコマンドで使う準備処理と、コマンドの失敗の伝え方。"""

import argparse
import logging
import platform
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import timedelta
from functools import cached_property
from pathlib import Path

from ..capture.camera import Camera
from ..capture.live_preview import LivePreview
from ..config import Config
from ..detection.roi import ROI, AnyROI
from ..detection.scene import SceneFiles
from ..errors import UserError
from ..features.activity_map import ActivityLayout
from ..features.outages import OutageKind, OutageLog, OutageTracker
from ..features.tray_registration import TrayRegistration
from ..features.tray_tracking import TrayTracker
from ..frames import Frame
from ..notifications import (
    DEFAULT_NTFY_SERVER,
    FanOutNotifier,
    MacNotifier,
    NtfyNotifier,
    ToiletNotifier,
)
from ..records.database import events_database
from ..web_push import PushKeys, PushSubscriptions, WebPushNotifier, push_subject

logger = logging.getLogger(__name__)
# いつもと違う日が続いていないかを確かめる間隔（秒）。
TREND_CHECK_SECONDS = 1800
# 床の候補を判定器で確かめる間隔（秒）と、作業用の一時フォルダー（データの保存先の下）。
FLOOR_JUDGE_SECONDS = 60
FLOOR_WORKDIR = "floor-work"
DIGEST_WORKDIR = "digest-work"


class CommandError(UserError):
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

    @cached_property
    def scene(self) -> SceneFiles:
        return TrayRegistration(self.root).active_scene()

    @property
    def outside_roi_path(self) -> Path:
        return self.root / "outside-roi.json"

    @property
    def dog_model_dir(self) -> Path:
        return self.root / "models" / "dog-yolox"


# サブコマンドの処理。終了コード（成功 0、入力や設定の誤り 2）を返す。
Handler = Callable[[argparse.Namespace, CommandContext], int]


def load_tray_tracker(
    scene: SceneFiles, baseline: Frame, roi: AnyROI, enabled: bool
) -> TrayTracker | None:
    """--track-tray のときだけ、登録済みのトレーの画像から追従を準備する。"""
    if not enabled:
        return None
    return TrayTracker.from_baseline(scene.tray_reference_path, baseline, roi)


def load_outside_roi(context: CommandContext) -> AnyROI:
    """トレー外の検知に使う範囲を読む。

    床面と違う範囲（ベッドや餌皿を外すなど）を見張るときだけ、トレー外の範囲を登録する。
    登録していなければ、行動マップと同じサークルの床面を使う。
    """
    if context.outside_roi_path.is_file():
        return ROI.load(context.outside_roi_path)
    try:
        return ActivityLayout.load(context.root).roi
    except FileNotFoundError:
        raise CommandError(
            "先に管理 › 範囲の登録でサークルの床面を囲んでください"
            "（CLI では setup-activity-map。床面と違う範囲は setup-roi --outside）"
        ) from None


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


def with_web_push(notifier: ToiletNotifier, context: CommandContext) -> ToiletNotifier:
    """notifier に、GUI で購読した人の端末への Web Push を足す（購読がなければ何も送らない）。"""
    config = context.config
    push = WebPushNotifier(
        PushSubscriptions(events_database(context.root)),
        PushKeys(context.root),
        subject=push_subject(config.gui_public_url),
        enabled=config.notifications,
        pet_name=config.pet_name,
    )
    return FanOutNotifier([notifier, push])


def outage_tracker(
    context: CommandContext, kind: OutageKind, notifier: ToiletNotifier
) -> OutageTracker:
    """設定の watchdog.minutes 続いた停止を、notifier で知らせる見張りの状態。"""
    log = OutageLog(events_database(context.root))
    return OutageTracker(log, kind, context.config.watchdog_minutes * 60, notifier)


@contextmanager
def monitor_watchdog(context: CommandContext, notifier: ToiletNotifier) -> Iterator[None]:
    """GUI のサーバーの間、監視のハートビートを見張る（watchdog.enabled が false なら何もしない）。"""
    if not context.config.watchdog_enabled:
        yield
        return
    from ..features.watchdog import MonitorWatchdog, watching

    tracker = outage_tracker(context, "monitor", notifier)
    with watching(MonitorWatchdog(LivePreview(context.root), tracker).check):
        yield


@contextmanager
def trend_watch(context: CommandContext, notifier: ToiletNotifier) -> Iterator[None]:
    """GUI のサーバーの間、いつもと違う日が続いていないかを確かめる（trends.notify）。"""
    if not context.config.trends_notify:
        yield
        return
    from ..features.daily_trends import USUAL_DAYS, TrendNotices, gather
    from ..features.watchdog import watching
    from ..records.events import Store
    from ..timezones import local_today

    store = Store(context.root)
    notices = TrendNotices(store.database)

    def check() -> None:
        today = local_today()
        days = gather(context.root, store, today - timedelta(days=USUAL_DAYS + 2), today)
        notices.notify(days, today, notifier)

    with watching(check, TREND_CHECK_SECONDS):
        yield


@contextmanager
def floor_judge_watch(context: CommandContext, notifier: ToiletNotifier) -> Iterator[None]:
    """GUI のサーバーの間、床の候補を判定器で確かめる（floor.judge が true のときだけ）。"""
    config = context.config
    if not config.floor_judge:
        yield
        return
    from ..features.floor_judge import FloorJudgeWorker
    from ..features.watchdog import watching
    from ..records.floor_objects import FloorObjects
    from ..video.judge_runs import JudgeRunLog
    from ..video.providers import build_judge

    database = events_database(context.root)
    judge = build_judge(config.review_provider, config.review_model)
    worker = FloorJudgeWorker(
        FloorObjects(database, context.root),
        JudgeRunLog(database),
        lambda: (judge, config.review_provider, config.review_model or None),
        notifier,
        context.root / FLOOR_WORKDIR,
        pet_name=config.pet_name,
    )

    def check() -> None:
        # 待っている候補を、上限まで続けて判定する。
        while worker.poll_once():
            pass

    with watching(check, FLOOR_JUDGE_SECONDS):
        yield


@contextmanager
def digest_summary_watch(context: CommandContext) -> Iterator[None]:
    """GUI のサーバーの間、留守中のまとめの要約を作る（digest.summary が true のときだけ）。"""
    config = context.config
    if not config.digest_summary:
        yield
        return
    from ..features.digest_summary import DigestSummaryWorker
    from ..features.watchdog import watching
    from ..gui.clip_pages import SOURCES
    from ..records.away_periods import AwayPeriods
    from ..records.digest_summaries import DigestSummaries
    from ..records.events import Store
    from ..video.clips import ClipStore
    from ..video.judge_runs import JudgeRunLog
    from ..video.providers import build_judge

    store = Store(context.root)
    judge = build_judge(config.review_provider, config.review_model)
    worker = DigestSummaryWorker(
        store,
        ClipStore(context.root),
        context.root,
        DigestSummaries(store.database),
        AwayPeriods(store.database),
        JudgeRunLog(store.database),
        lambda: (judge, config.review_provider, config.review_model or None),
        context.root / DIGEST_WORKDIR,
        images=config.digest_summary_images,
        pet_name=config.pet_name,
        clip_labels=SOURCES,
    )

    def check() -> None:
        while worker.poll_once():
            pass

    with watching(check, FLOOR_JUDGE_SECONDS):
        yield


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
                public_url=config.gui_public_url,
            )
        except ValueError as error:
            # トピック名やサーバーの検証の文言で、接続先の URL そのものは含まない。
            raise CommandError(str(error)) from None
        return notifier, True
    on_mac = platform.system() == "Darwin"
    if config.notifications and not on_mac and sends:
        logger.warning("macOS 以外では、.env に POOP_NTFY_TOPIC を設定しないと通知は届きません")
    return MacNotifier(config.notifications and on_mac, pet_name=config.pet_name), False
