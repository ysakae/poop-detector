"""対応する OS と、画面を表示できる環境かを判定する。"""

import os
import sys
from collections.abc import Mapping

SUPPORTED_PLATFORMS = ("darwin", "linux")


class UnsupportedPlatformError(ImportError):
    """対応していない OS で読み込まれた。"""


def ensure_supported(platform: str = sys.platform) -> None:
    # 排他制御に fcntl、タイムゾーンの反映に time.tzset を使うため、Windows では動かない。
    if not platform.startswith(SUPPORTED_PLATFORMS):
        raise UnsupportedPlatformError(
            "poop-detector は macOS と Linux に対応しています。"
            "Windows では WSL2 などの Linux 環境で実行してください。"
        )


def has_display(platform: str = sys.platform, environ: Mapping[str, str] = os.environ) -> bool:
    """監視領域の登録など、ウィンドウを開く操作ができる環境か。"""
    if platform == "darwin":
        return True
    return bool(environ.get("DISPLAY") or environ.get("WAYLAND_DISPLAY"))
