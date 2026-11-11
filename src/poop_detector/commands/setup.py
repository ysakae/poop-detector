"""カメラの確認と、監視範囲・行動マップの床面・基準画像の登録。"""

import argparse
import time
from uuid import uuid4

import cv2
import numpy as np

from ..capture.live_preview import LivePreview
from ..config import load_connection_settings
from ..detection.roi import ROI, AnyROI, select_polygon
from ..detection.scene import save_baseline
from ..errors import CameraUnavailableError
from ..features.activity_map import LAYOUT_FILE, ActivityLayout
from ..features.tray_registration import TrayRegistration
from ..features.tray_tracking import TrayTracker
from ..frames import Frame
from ..platform_support import has_display
from .context import CommandContext, CommandError, camera_source, open_camera

NO_DISPLAY = (
    "範囲の登録には画面が必要です。画面のあるマシンで登録し、"
    "data/ をこのマシンへコピーしてください（docs/setup.md）"
)


def require_display() -> None:
    if not has_display():
        raise CommandError(NO_DISPLAY)


def capture_frame(context: CommandContext) -> Frame:
    """カメラから1フレームを取る。接続できなければ CameraUnavailableError（URL は表示しない）。"""
    source = camera_source(load_connection_settings())
    with open_camera(source, context.config) as camera:
        frame = camera.read()
    if frame is None:
        raise CameraUnavailableError("映像を取得できません。EufyのRTSP設定と接続を確認してください")
    return frame


def check_camera(_args: argparse.Namespace, context: CommandContext) -> int:
    frame = capture_frame(context)
    print(f"フレーム取得成功: {frame.shape[1]}x{frame.shape[0]}")
    return 0


def setup_roi(args: argparse.Namespace, context: CommandContext) -> int:
    require_display()
    if not args.outside and context.scene_root != context.root:
        raise CommandError("監視中のトレー登録を使ってください（tray-registration）")
    path = context.outside_roi_path if args.outside else context.roi_path
    # 先に接続情報を確かめてから、登録済みかどうかを確かめる（以前からの順序）。
    camera_source(load_connection_settings())
    if path.exists() and not args.replace:
        raise CommandError("ROIは登録済みです。更新する場合は--replaceを指定してください")
    frame = capture_frame(context)
    roi: AnyROI
    if args.outside:
        roi = select_polygon(frame, "トレーと監視する周囲の床を指定")
    elif args.polygon:
        roi = select_polygon(frame)
    else:
        rect = cv2.selectROI("トイレ領域を選択してEnter、取消はEsc", frame)
        cv2.destroyAllWindows()
        roi = ROI.from_pixels(rect, frame.shape[1], frame.shape[0])
    roi.save(path)
    print("ROIを保存しました")
    return 0


def setup_activity_map(args: argparse.Namespace, context: CommandContext) -> int:
    require_display()
    camera_source(load_connection_settings())
    if (context.root / LAYOUT_FILE).exists() and not args.replace:
        raise CommandError("床面は登録済みです。画角を変えた場合は --replace を指定してください")
    frame = capture_frame(context)
    floor = select_polygon(frame, "サークルの床面を多角形で指定")
    ActivityLayout(uuid4().hex, floor).save(context.root, frame)
    print("行動マップの床面を登録しました")
    return 0


def capture_baseline(args: argparse.Namespace, context: CommandContext) -> int:
    if context.scene_root != context.root:
        raise CommandError("監視中のトレー登録を使ってください（tray-registration）")
    camera_source(load_connection_settings())
    if context.baseline_path.exists() and not args.replace:
        raise CommandError("基準は登録済みです。更新する場合は--replaceを指定してください")
    frame = capture_frame(context)
    roi = ROI.load(context.roi_path)
    if args.track_tray or context.config.track_tray_enabled:
        # トレーの縁を確認できない画像は、追従の基準にしない（TrayReferenceError）。
        TrayTracker(frame, roi)
        path = context.tray_reference_path
        path.parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(path), frame):
            raise OSError("トレー追従用の基準画像を保存できません")
    save_baseline(context.baseline_path, roi.crop(frame), roi)
    print("基準画像を保存しました")
    return 0


REGISTRATION_MESSAGES = {
    "idle": "トレー交換の要求はありません",
    "begin": "監視が交換開始を受け付けるのを待っています",
    "paused": "監視を一時保留しました。空のトレーを用意してください",
    "apply": "新しい基準を検証中です",
    "applied": "新しいトレーの基準を反映しました",
    "failed": "新しい基準の反映に失敗しました",
    "cancel": "監視が取消を受け付けるのを待っています",
    "cancelled": "トレーの交換を取り消しました",
}


def registration_roi(args: argparse.Namespace, context: CommandContext) -> AnyROI | None:
    if args.roi:
        return ROI.load(args.roi)
    if not args.polygon:
        return None
    require_display()
    preview = LivePreview(context.root)
    for _ in range(30):
        snapshot = preview.read()
        if snapshot is not None:
            frame = cv2.imdecode(np.frombuffer(snapshot[0], dtype=np.uint8), cv2.IMREAD_COLOR)
            if frame is not None:
                return select_polygon(frame)
        time.sleep(0.1)
    raise CameraUnavailableError("監視のライブ映像が取得できません")


def tray_registration(args: argparse.Namespace, context: CommandContext) -> int:
    """交換の開始、反映、取消を稼働中の監視に依頼する。"""
    registration = TrayRegistration(context.root)
    if (args.polygon and args.roi) or (args.action != "apply" and (args.polygon or args.roi)):
        raise CommandError("--polygon と --roi は apply でどちらか一方を指定してください")
    if args.action == "begin":
        registration.begin()
    elif args.action == "apply":
        registration.apply(registration_roi(args, context))
    elif args.action == "cancel":
        registration.cancel()

    status = registration.status()
    if args.action != "status":
        deadline = time.monotonic() + 10
        while status["phase"] in ("begin", "apply", "cancel") and time.monotonic() < deadline:
            time.sleep(0.1)
            status = registration.status()
    print(status["error"] or REGISTRATION_MESSAGES[status["phase"]])
    return 2 if status["phase"] in ("begin", "apply", "failed", "cancel") else 0
