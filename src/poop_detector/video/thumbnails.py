"""動画の一覧に出すサムネイル（代表の1コマの小さな JPEG）。

動画と同じ clips/ に、同じ名前の .jpg で置く。動画から作り直せるので、バックアップには入れない。
録画を整理するときは clip_storage が一緒に消す。
"""

import logging
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING

import cv2

from ..frames import Frame

if TYPE_CHECKING:
    from .clips import ClipRow, ClipStore

WIDTH = 240  # 一覧に出す幅（ピクセル）。高さは映像の縦横比に合わせる。
JPEG_QUALITY = 78
logger = logging.getLogger(__name__)


def thumbnail_path(video: Path) -> Path:
    """動画のファイルに対応するサムネイルの場所。"""
    return video.with_suffix(".jpg")


def _frame_at(video: Path, seconds: float) -> Frame | None:
    """動画の seconds 秒目の1コマ。そこを読めないときは先頭の1コマ。"""
    capture = cv2.VideoCapture(str(video))
    try:
        if not capture.isOpened():
            return None
        for position in (max(0.0, seconds) * 1000, 0.0):
            capture.set(cv2.CAP_PROP_POS_MSEC, position)
            ok, frame = capture.read()
            if ok and frame is not None and frame.size:
                return frame
        return None
    finally:
        capture.release()


def create_thumbnail(video: Path, seconds: float) -> Path | None:
    """動画から代表の1コマを切り出して保存する。読めない動画では作らない。"""
    frame = _frame_at(video, seconds)
    if frame is None:
        return None
    height, width = frame.shape[:2]
    size = (WIDTH, max(1, height * WIDTH // width))
    ok, encoded = cv2.imencode(
        ".jpg",
        cv2.resize(frame, size, interpolation=cv2.INTER_AREA),
        [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY],
    )
    if not ok:
        return None
    target = thumbnail_path(video)
    # 途中まで書いたファイルを見せないよう、別名で書いてから置き換える。
    temporary = target.with_suffix(".part.jpg")
    temporary.write_bytes(encoded.tobytes())
    temporary.replace(target)
    return target


def ensure_thumbnail(store: "ClipStore", row: "ClipRow | sqlite3.Row | None") -> Path | None:
    """変換済みの動画のサムネイル。まだなければ、操作・検知の位置の1コマから作る。"""
    # video_path は、clips/ の外を指す行やファイルのない行を None にする。
    video = store.video_path(row)
    if video is None or row is None:
        return None
    target = thumbnail_path(video)
    if target.is_file() and not target.is_symlink():
        return target
    try:
        return create_thumbnail(video, float(row["trigger_offset"] or 0))
    except (OSError, cv2.error):
        logger.warning("動画のサムネイルを作れませんでした")
        return None
