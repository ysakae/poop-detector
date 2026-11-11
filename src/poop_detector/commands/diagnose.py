"""不具合の報告に添える診断情報の表示。

報告は公開の場に貼られるので、接続情報は「設定あり・未設定」だけを示し、値・保存先の
パス・犬の名前・映像は出さない。
"""

import argparse
import platform
import shutil
import sqlite3
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import yaml

from ..config import load_connection_settings
from ..features.activity_map import LAYOUT_FILE
from ..platform_support import has_display
from ..records.database import schema_version
from ..video.review import default_judge
from .context import CommandContext

DATABASE_FILE = "events.sqlite3"


def package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "不明"


def os_summary() -> dict[str, str]:
    summary = {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
    }
    mac_version = platform.mac_ver()[0]
    if mac_version:
        summary["macos"] = mac_version
    if Path("/.dockerenv").exists():
        summary["container"] = "Docker"
    return summary


def connection_summary() -> dict[str, str]:
    """接続情報が設定されているかだけを返す。値そのものは返さない。"""
    settings = load_connection_settings()

    def present(key: str) -> str:
        return "設定あり" if settings[key].strip() else "未設定"

    return {
        "rtsp_url": present("POOP_RTSP_URL"),
        "ntfy_topic": present("POOP_NTFY_TOPIC"),
        "ntfy_early_topic": present("POOP_NTFY_EARLY_TOPIC"),
        "ntfy_server": "独自" if settings["POOP_NTFY_SERVER"].strip() else "既定",
    }


def registered_summary(context: CommandContext) -> dict[str, bool]:
    return {
        "roi": context.roi_path.is_file(),
        "baseline": context.baseline_path.is_file(),
        "tray_reference": context.tray_reference_path.is_file(),
        "outside_roi": context.outside_roi_path.is_file(),
        "activity_map": (context.root / LAYOUT_FILE).is_file(),
        "dog_model": context.dog_model_dir.is_dir() and any(context.dog_model_dir.iterdir()),
    }


def database_summary(root: Path) -> str:
    """スキーマの版を読み取り専用で読む。ないときに作ったり、移行したりしない。"""
    path = root / DATABASE_FILE
    if not path.is_file():
        return "未作成"
    try:
        db = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            return f"版{schema_version(db)}"
        finally:
            db.close()
    except sqlite3.Error:
        return "読み込めません"


def programs_summary() -> dict[str, str]:
    judge = default_judge()
    try:
        judge.check()
        judge_state = "使えます"
    except ValueError as error:
        # 判定器の check が書いた、ログインなどの案内の文言。
        judge_state = f"使えません（{error}）"
    return {
        "ffmpeg": "見つかりました" if shutil.which("ffmpeg") else "見つかりません",
        "video_judge": f"{judge.name}: {judge_state}",
        "display": "あり" if has_display() else "なし",
    }


def diagnose(_args: argparse.Namespace, context: CommandContext) -> int:
    """バージョン・環境・有効な機能・登録の有無を、Issue に貼れる YAML で表示する。"""
    config = context.config
    report: dict[str, Any] = {
        "poop_detector": package_version("poop-detector"),
        "python": platform.python_version(),
        "os": os_summary(),
        "libraries": {"opencv": cv2.__version__, "numpy": np.__version__},
        "features": config.enabled_features(),
        "connection": connection_summary(),
        "registered": registered_summary(context),
        "database": database_summary(context.root),
        "programs": programs_summary(),
    }
    print("# 不具合の報告に貼れる診断情報です。URL・トピック名・保存先のパスは含みません。")
    print(yaml.safe_dump(report, allow_unicode=True, sort_keys=False), end="")
    return 0
