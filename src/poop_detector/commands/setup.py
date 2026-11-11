"""カメラの確認と、監視範囲・行動マップの床面・基準画像の登録。"""

import argparse
from uuid import uuid4

import cv2

from ..activity_map import LAYOUT_FILE, ActivityLayout
from ..config import load_connection_settings
from ..frames import Frame
from ..platform_support import has_display
from ..roi import ROI, AnyROI, select_polygon
from ..scene import save_baseline
from ..tray_tracking import TrayTracker
from .context import CommandContext, CommandError, camera_source, open_camera

NO_DISPLAY = (
    "範囲の登録には画面が必要です。画面のあるマシンで登録し、"
    "data/ をこのマシンへコピーしてください（docs/setup.md）"
)


def require_display() -> None:
    if not has_display():
        raise CommandError(NO_DISPLAY)


def capture_frame(context: CommandContext) -> Frame:
    """カメラから1フレームを取る。接続できなければ ValueError（URL は表示しない）。"""
    source = camera_source(load_connection_settings())
    with open_camera(source, context.config) as camera:
        frame = camera.read()
    if frame is None:
        raise ValueError("映像を取得できません。EufyのRTSP設定と接続を確認してください")
    return frame


def check_camera(_args: argparse.Namespace, context: CommandContext) -> int:
    frame = capture_frame(context)
    print(f"フレーム取得成功: {frame.shape[1]}x{frame.shape[0]}")
    return 0


def setup_roi(args: argparse.Namespace, context: CommandContext) -> int:
    require_display()
    path = context.outside_roi_path if args.outside else context.roi_path
    # 先に接続情報を確かめてから、登録済みかどうかを確かめる（以前からの順序）。
    camera_source(load_connection_settings())
    if path.exists() and not args.replace:
        raise ValueError("ROIは登録済みです。更新する場合は--replaceを指定してください")
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
    camera_source(load_connection_settings())
    if context.baseline_path.exists() and not args.replace:
        raise ValueError("基準は登録済みです。更新する場合は--replaceを指定してください")
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
